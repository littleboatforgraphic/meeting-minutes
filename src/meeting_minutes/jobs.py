"""Small DB-backed job queue + periodic jobs (reminders, retention). One worker process per company stack."""

from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import pipeline, retention, tasks
from .db import Job, KeyValue, Meeting, new_session, utcnow

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def enqueue(db: Session, kind: str, meeting_id: int | None = None, payload: dict | None = None,
            delay: float = 0.0) -> None:
    exists = db.scalar(select(Job).where(Job.kind == kind, Job.meeting_id == meeting_id, Job.status == "queued"))
    if exists:
        return
    db.add(Job(kind=kind, meeting_id=meeting_id, payload=payload or {},
               run_after=utcnow() + timedelta(seconds=delay)))
    db.flush()


def _run(db: Session, svc: pipeline.Services, job: Job) -> None:
    meeting = db.get(Meeting, job.meeting_id) if job.meeting_id else None
    if job.kind in ("process_audio", "finalize", "distribute") and meeting is None:
        return
    if job.kind == "process_audio":
        if meeting.status == "recording":
            pipeline.process_audio(db, svc, meeting)
    elif job.kind == "finalize":
        pipeline.finalize(db, svc, meeting)
    elif job.kind == "distribute":
        pipeline.distribute(db, svc, meeting)
    else:
        raise ValueError(f"unknown job kind {job.kind}")


def run_next(svc: pipeline.Services) -> bool:
    db = new_session()
    try:
        job = db.scalar(select(Job).where(Job.status == "queued", Job.run_after <= utcnow())
                        .order_by(Job.run_after, Job.id).limit(1))
        if not job:
            return False
        job.status, job.attempts = "running", job.attempts + 1
        db.commit()
        try:
            _run(db, svc, job)
            job.status = "done"
            db.commit()
        except Exception as e:
            db.rollback()
            job = db.get(Job, job.id)
            job.last_error = traceback.format_exc()[-4000:]
            log.exception("job %s %s failed", job.kind, job.id)
            if job.attempts < MAX_ATTEMPTS:
                job.status, job.run_after = "queued", utcnow() + timedelta(seconds=30 * job.attempts)
            else:
                job.status = "failed"
                if job.meeting_id and job.kind in ("finalize", "distribute"):
                    m = db.get(Meeting, job.meeting_id)
                    if m:
                        m.status, m.error = "failed", f"{job.kind}: {e}"[:2000]
            db.commit()
        return True
    finally:
        db.close()


def _kv(db: Session, key: str) -> str | None:
    row = db.get(KeyValue, key)
    return row.value if row else None


def _set_kv(db: Session, key: str, value: str) -> None:
    row = db.get(KeyValue, key)
    if row:
        row.value = value
    else:
        db.add(KeyValue(key=key, value=value))


def run_periodic(svc: pipeline.Services, now: datetime | None = None) -> None:
    now = now or utcnow()
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(svc.company.company.timezone))
    db = new_session()
    try:
        today = local.date().isoformat()
        if local.hour >= svc.company.tasks.reminders.send_hour and _kv(db, "reminders_on") != today:
            n = tasks.run_reminders(db, svc.company, svc.dispatcher, local.date(), svc.settings.public_base_url)
            _set_kv(db, "reminders_on", today)
            db.commit()
            log.info("reminders sent: %d", n)
        hour = now.strftime("%Y-%m-%dT%H")
        if _kv(db, "purged_hour") != hour:
            stats = retention.purge(db, svc.company, svc.settings, now)
            _set_kv(db, "purged_hour", hour)
            db.commit()
            log.info("retention purge: %s", stats)
    finally:
        db.close()


def requeue_running() -> int:
    """Jobs left 'running' by a worker that died; there is one worker per stack, so at startup they are stale."""
    db = new_session()
    try:
        n = db.execute(update(Job).where(Job.status == "running").values(status="queued", run_after=utcnow())).rowcount
        db.commit()
        return n or 0
    finally:
        db.close()


def worker_loop(svc: pipeline.Services, idle_sleep: float = 2.0) -> None:
    log.info("worker started; requeued %d interrupted job(s)", requeue_running())
    last_periodic = 0.0
    while True:
        if time.monotonic() - last_periodic > 60:
            try:
                run_periodic(svc)
            except Exception:
                log.exception("periodic jobs failed")
            last_periodic = time.monotonic()
        if not run_next(svc):
            time.sleep(idle_sleep)
