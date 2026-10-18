"""Data retention: audio deleted after transcription, transcripts after 7 days, minutes/tasks after 90 days."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .company import CompanyConfig
from .db import AuthSession, LinkToken, LoginChallenge, Meeting, Task, Utterance
from .settings import Settings


def purge(db: Session, company: CompanyConfig, settings: Settings, now: datetime) -> dict[str, int]:
    r = company.retention
    stats: dict[str, int] = {}

    cutoff = now - timedelta(days=r.transcript_days)
    old = [m.id for m in db.scalars(select(Meeting).where(
        Meeting.transcript_purged.is_(False), Meeting.ended_at.is_not(None), Meeting.ended_at < cutoff))]
    if old:
        db.execute(delete(Utterance).where(Utterance.meeting_id.in_(old)))
        db.execute(update(Meeting).where(Meeting.id.in_(old)).values(transcript_purged=True))
    stats["transcripts"] = len(old)

    cutoff = now - timedelta(days=r.tasks_days)
    stats["tasks"] = db.execute(delete(Task).where(Task.created_at < cutoff)).rowcount or 0

    cutoff = now - timedelta(days=r.minutes_days)
    old_meetings = list(db.scalars(select(Meeting).where(Meeting.started_at < cutoff)))
    for m in old_meetings:
        db.execute(update(Task).where(Task.meeting_id == m.id).values(meeting_id=None))
        db.delete(m)
    stats["meetings"] = len(old_meetings)

    # Safety net: audio of meetings that are no longer recording/processing, or older than a day.
    audio_root = settings.data_dir / "audio"
    removed = 0
    if audio_root.exists():
        for d in audio_root.iterdir():
            m = db.get(Meeting, int(d.name)) if d.name.isdigit() else None
            if m is None or m.status not in ("recording", "processing") or m.started_at < now - timedelta(days=1):
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    stats["audio_dirs"] = removed

    db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
    db.execute(delete(LoginChallenge).where(LoginChallenge.expires_at < now))
    db.execute(delete(LinkToken).where(LinkToken.expires_at < now))
    db.flush()
    return stats
