"""Telegram and WhatsApp webhooks: account linking, "done" replies/buttons, voice notes / audio files as meetings."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth, pipeline
from ..db import Attendee, Meeting, Person, Task, new_session, utcnow
from ..directory import normalize_phone
from ..tasks import can_edit, set_status, task_line

log = logging.getLogger(__name__)
DONE_RE = re.compile(r"^/?done\s*#?(\d+)$", re.I)
HELP = ("Send me a voice note or audio file of a meeting and I will draft the minutes for you.\n"
        "Reply 'done 12' to mark task #12 done, or /tasks to list your open tasks.")


def mark_done(db: Session, svc: pipeline.Services, person: Person, task_id: int) -> str:
    t = db.get(Task, task_id)
    if not t or not can_edit(t, person):
        return f"Task #{task_id} not found."
    set_status(db, svc.company, t, "done")
    return f"Marked done: {task_line(t)}"


def open_tasks_text(db: Session, person: Person) -> str:
    ts = list(db.scalars(select(Task).where(Task.owner_id == person.id, Task.status.in_(("open", "in_progress")))
                         .order_by(Task.due_date.is_(None), Task.due_date)))
    return "Your open tasks:\n" + "\n".join(f"- {task_line(t)}" for t in ts) if ts else "You have no open tasks."


def audio_meeting(db: Session, svc: pipeline.Services, person: Person, data: bytes, suffix: str, source: str) -> str:
    m = Meeting(title=f"Recording {utcnow():%d %b %Y %H:%M} UTC", chair_id=person.id, created_by_id=person.id,
                source=source, voiceprints_on=svc.company.voice.voiceprints_default_on and
                svc.company.voice.voiceprints_allowed)
    m.attendees = [Attendee(person_id=person.id)]
    db.add(m)
    db.flush()
    pipeline.ingest_file(db, svc, m, data, suffix)
    return (f"Got it ({m.audio_seconds / 60:.0f} min). I'll send you the draft minutes for approval when ready. "
            f"Add attendees in the app: {svc.url(f'/meetings/{m.id}')}")


def router(svc: pipeline.Services) -> APIRouter:
    r = APIRouter()

    def chan(name: str):
        ch = svc.dispatcher.channels.get(name)
        if not ch:
            raise HTTPException(404, f"{name} is not enabled")
        return ch

    # ----- Telegram -----
    @r.post("/webhooks/telegram")
    async def telegram(request: Request):
        expected = svc.settings.telegram_webhook_secret
        got = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not expected or not hmac.compare_digest(expected, got):
            raise HTTPException(403, "bad secret")
        api = chan("telegram").api
        upd = await request.json()
        db = new_session()
        try:
            reply = handle_telegram(db, svc, api, upd)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("telegram update failed")
            reply = None
        finally:
            db.close()
        if reply:
            chat_id, text = reply
            try:
                api.send_text(chat_id, text)
            except Exception:
                log.exception("telegram reply failed")
        return {"ok": True}

    # ----- WhatsApp -----
    @r.get("/webhooks/whatsapp")
    def whatsapp_verify(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
                        challenge: str = Query("", alias="hub.challenge")):
        vt = svc.settings.whatsapp_verify_token
        if mode == "subscribe" and vt and hmac.compare_digest(vt, token):
            return PlainTextResponse(challenge)
        raise HTTPException(403, "verification failed")

    @r.post("/webhooks/whatsapp")
    async def whatsapp(request: Request):
        body = await request.body()
        secret = svc.settings.whatsapp_app_secret
        sig = request.headers.get("x-hub-signature-256", "")
        if not secret or not hmac.compare_digest(
                "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), sig):
            raise HTTPException(403, "bad signature")
        ch = chan("whatsapp")
        payload = await request.json()
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for msg in change.get("value", {}).get("messages", []):
                    db = new_session()
                    try:
                        text = handle_whatsapp(db, svc, ch.api, msg)
                        db.commit()
                    except Exception:
                        db.rollback()
                        log.exception("whatsapp message failed")
                        text = None
                    finally:
                        db.close()
                    if text:
                        try:
                            ch.api.send({"to": msg["from"], "type": "text", "text": {"body": text[:4000]}})
                        except Exception:
                            log.exception("whatsapp reply failed")
        return {"ok": True}

    return r


def handle_telegram(db: Session, svc: pipeline.Services, api, upd: dict) -> tuple[str, str] | None:
    if cq := upd.get("callback_query"):
        chat_id = str(cq["from"]["id"])
        person = db.scalar(select(Person).where(Person.telegram_chat_id == chat_id, Person.active.is_(True)))
        text = "Please link your account in the app first."
        m = re.fullmatch(r"task:(\d+):done", cq.get("data", ""))
        if person and m:
            text = mark_done(db, svc, person, int(m.group(1)))
        try:
            api.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text[:190]})
        except Exception:
            log.warning("answerCallbackQuery failed")
        return (chat_id, text)
    msg = upd.get("message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return None
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        token = text[6:].strip()
        if not token:
            return (chat_id, "Open the Meeting app > Me > Link Telegram to connect your account.\n\n" + HELP)
        try:
            person = auth.use_link_token(db, svc.settings.secret_key, token, "telegram")
        except auth.AuthError as e:
            return (chat_id, str(e))
        for other in db.scalars(select(Person).where(Person.telegram_chat_id == chat_id, Person.id != person.id)):
            other.telegram_chat_id = None
        db.flush()
        person.telegram_chat_id = chat_id
        person.telegram_username = msg.get("from", {}).get("username") or person.telegram_username
        return (chat_id, f"Linked to {person.name}. " + HELP)
    person = db.scalar(select(Person).where(Person.telegram_chat_id == chat_id, Person.active.is_(True)))
    if not person:
        return (chat_id, "This Telegram account is not linked. Open the Meeting app > Me > Link Telegram.")
    if m := DONE_RE.match(text):
        return (chat_id, mark_done(db, svc, person, int(m.group(1))))
    if text.lower() in ("/tasks", "tasks"):
        return (chat_id, open_tasks_text(db, person))
    media = msg.get("voice") or msg.get("audio") or (
        msg.get("document") if (msg.get("document") or {}).get("mime_type", "").startswith(("audio/", "video/"))
        else None)
    if media:
        if media.get("file_size", 0) > 20 * 1024 * 1024:
            return (chat_id, "Telegram bots can only download files up to 20 MB. Please use the Meeting app instead.")
        data = api.download(media["file_id"])
        suffix = "." + (media.get("file_name", "").rsplit(".", 1)[-1] if "." in media.get("file_name", "") else "ogg")
        return (chat_id, audio_meeting(db, svc, person, data, suffix, "telegram"))
    return (chat_id, HELP)


def handle_whatsapp(db: Session, svc: pipeline.Services, api, msg: dict) -> str | None:
    phone = normalize_phone(msg.get("from"))
    person = db.scalar(select(Person).where(Person.phone == phone, Person.active.is_(True))) if phone else None
    if not person:
        return "This number is not in the company directory. Please ask your admin to add you."
    kind = msg.get("type")
    if kind == "interactive":
        rid = (msg["interactive"].get("button_reply") or {}).get("id", "")
        m = re.fullmatch(r"task:(\d+):done", rid)
        return mark_done(db, svc, person, int(m.group(1))) if m else None
    if kind == "text":
        text = msg["text"]["body"].strip()
        if m := DONE_RE.match(text):
            return mark_done(db, svc, person, int(m.group(1)))
        if text.lower() in ("tasks", "/tasks"):
            return open_tasks_text(db, person)
        return HELP
    if kind in ("audio", "document", "video"):
        media = msg[kind]
        if kind == "document" and not media.get("mime_type", "").startswith(("audio/", "video/")):
            return HELP
        data, mime = api.download_media(media["id"])
        sub = mime.split("/")[-1].split(";")[0]
        suffix = {"mpeg": ".mp3", "ogg": ".ogg", "mp4": ".m4a", "aac": ".aac", "amr": ".amr"}.get(sub, f".{sub}")
        return audio_meeting(db, svc, person, data, suffix, "whatsapp")
    return HELP
