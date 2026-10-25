"""FastAPI app: JSON API for the Meeting app, chat webhooks, central-portal placeholder, static PWA."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import __version__, auth, pipeline
from ..db import Attendee, Meeting, Person, Task, Utterance, init_db, new_session, utcnow
from ..directory import normalize_phone, read_rows, upsert_people
from ..render import hms, render_minutes_html, render_pdf, render_transcript_text
from ..tasks import can_edit, set_status

STATIC = Path(__file__).parent / "static"
MAX_CHUNK = 8 * 1024 * 1024
MAX_UPLOAD = 300 * 1024 * 1024


def get_db():
    db = new_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def svc_of(request: Request) -> pipeline.Services:
    return request.app.state.svc


def current_user(request: Request, db: Session = Depends(get_db)) -> Person:
    svc = svc_of(request)
    token = request.cookies.get(auth.COOKIE)
    if not token and request.headers.get("authorization", "").lower().startswith("bearer "):
        token = request.headers["authorization"][7:]
    person = auth.session_person(db, svc.company, svc.settings.secret_key, token)
    if not person:
        raise HTTPException(401, "not signed in")
    return person


def admin_user(user: Person = Depends(current_user)) -> Person:
    if not user.is_admin:
        raise HTTPException(403, "admin only")
    return user


# ---------- schemas ----------

class LoginIn(BaseModel):
    identifier: str
    password: str | None = None


class OtpIn(BaseModel):
    challenge: str
    code: str


class PasswordIn(BaseModel):
    password: str = Field(min_length=8)


class ChangePasswordIn(PasswordIn):
    current_password: str | None = None


class PersonIn(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    telegram_username: str | None = None
    erp_user_id: str | None = None
    aliases: list[str] = []
    role: str = "member"
    voiceprint_opt_out: bool = False
    active: bool = True


class MeetingIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    chair_id: int | None = None
    attendee_ids: list[int] = []
    voiceprints_on: bool | None = None


class DraftIn(BaseModel):
    minutes: dict | None = None
    speakers: dict[str, int | None] | None = None


class StatusIn(BaseModel):
    status: str


# ---------- serializers ----------

def person_out(p: Person, full: bool = False) -> dict:
    d = {"id": p.id, "name": p.name, "role": p.role}
    if full:
        d.update(email=p.email, phone=p.phone, telegram_username=p.telegram_username,
                 telegram_linked=bool(p.telegram_chat_id), erp_user_id=p.erp_user_id, aliases=p.aliases or [],
                 voiceprint_opt_out=p.voiceprint_opt_out, active=p.active, has_password=bool(p.password_hash))
    return d


def iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def meeting_out(m: Meeting) -> dict:
    return {"id": m.id, "title": m.title, "status": m.status, "source": m.source, "chair_id": m.chair_id,
            "chair": m.chair.name if m.chair else None, "started_at": iso(m.started_at), "ended_at": iso(m.ended_at),
            "audio_seconds": round(m.audio_seconds, 1), "processed_seconds": round(m.processed_seconds, 1),
            "voiceprints_on": m.voiceprints_on, "error": m.error, "approved_at": iso(m.approved_at),
            "distributed_at": iso(m.distributed_at), "transcript_purged": m.transcript_purged,
            "attendee_ids": [a.person_id for a in m.attendees]}


def task_out(t: Task) -> dict:
    return {"id": t.id, "meeting_id": t.meeting_id, "meeting": t.meeting.title if t.meeting else None,
            "title": t.title, "owner_id": t.owner_id, "owner": t.owner.name if t.owner else t.owner_as_said,
            "due_date": t.due_date.isoformat() if t.due_date else None, "due_as_said": t.due_as_said,
            "remark": t.remark, "status": t.status, "evidence": hms(t.evidence_seconds) if t.evidence_seconds else None}


def can_view(m: Meeting, u: Person) -> bool:
    return u.is_admin or u.id in (m.chair_id, m.created_by_id) or any(a.person_id == u.id for a in m.attendees)


def can_manage(m: Meeting, u: Person) -> bool:
    return u.is_admin or u.id in (m.chair_id, m.created_by_id)


def can_read_content(m: Meeting, u: Person) -> bool:
    """Attendees see minutes and transcript only after the chair approved them."""
    return m.approved_at is not None or can_manage(m, u)


def get_meeting(db: Session, mid: int, u: Person, manage: bool = False) -> Meeting:
    m = db.get(Meeting, mid)
    if not m or not can_view(m, u):
        raise HTTPException(404, "meeting not found")
    if manage and not can_manage(m, u):
        raise HTTPException(403, "only the chair, the organiser or an admin can do this")
    return m


def _set_cookie(resp: Response, svc: pipeline.Services, token: str) -> None:
    a = svc.company.auth
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=svc.settings.secure_cookies,
                    max_age=a.session_days * 86400 if a.remember_device else None)


def create_app(svc: pipeline.Services | None = None) -> FastAPI:
    svc = svc or pipeline.default_services()
    svc.settings.check_secret()
    init_db()
    app = FastAPI(title="meeting-minutes", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.svc = svc
    secret = svc.settings.secret_key

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/api/config")
    def config():
        c = svc.company
        return {"company": c.company.name, "version": __version__, "auth_method": c.auth.method,
                "remember_device": c.auth.remember_device, "voiceprints_allowed": c.voice.voiceprints_allowed,
                "voiceprints_default_on": c.voice.voiceprints_default_on, "output_language": c.output_language,
                "telegram_bot": c.notifiers.telegram.bot_username if c.notifiers.telegram.enabled else None,
                "timezone": c.company.timezone}

    # ----- auth -----
    @app.post("/api/auth/login")
    def login(body: LoginIn, response: Response, db: Session = Depends(get_db)):
        method = svc.company.auth.method
        person = auth.find_person(db, body.identifier)
        if method in ("password", "password+otp"):
            if not person or not body.password or not auth.verify_password(person, body.password):
                raise HTTPException(401, "wrong login or password")
        elif not person:
            raise HTTPException(401, "not found in the company directory")
        if method == "password":
            _set_cookie(response, svc, auth.create_session(db, svc.company, secret, person))
            db.commit()
            return {"ok": True, "user": person_out(person)}
        try:
            cid = auth.start_otp(db, svc.company, svc.dispatcher, secret, person)
        except auth.AuthError as e:
            raise HTTPException(400, str(e)) from None
        db.commit()
        return {"ok": False, "otp_challenge": cid}

    @app.post("/api/auth/otp")
    def otp(body: OtpIn, response: Response, db: Session = Depends(get_db)):
        try:
            person = auth.verify_otp(db, secret, body.challenge, body.code)
        except auth.AuthError as e:
            db.commit()
            raise HTTPException(401, str(e)) from None
        _set_cookie(response, svc, auth.create_session(db, svc.company, secret, person))
        db.commit()
        return {"ok": True, "user": person_out(person)}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, db: Session = Depends(get_db)):
        auth.end_session(db, secret, request.cookies.get(auth.COOKIE))
        db.commit()
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    @app.post("/api/auth/invite/{token}")
    def accept_invite(token: str, body: PasswordIn, response: Response, db: Session = Depends(get_db)):
        try:
            person = auth.use_link_token(db, secret, token, "invite")
            person.password_hash = auth.hash_password(body.password)
        except auth.AuthError as e:
            raise HTTPException(400, str(e)) from None
        if svc.company.auth.method != "password":
            db.commit()  # one-time codes are required: the person signs in normally
            return {"ok": True, "sign_in": True}
        _set_cookie(response, svc, auth.create_session(db, svc.company, secret, person))
        db.commit()
        return {"ok": True, "user": person_out(person)}

    @app.get("/api/me")
    def me(user: Person = Depends(current_user)):
        return person_out(user, full=True)

    @app.post("/api/me/password")
    def change_password(body: ChangePasswordIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        user = db.merge(user)
        if user.password_hash and not auth.verify_password(user, body.current_password or ""):
            raise HTTPException(403, "current password is wrong")
        user.password_hash = auth.hash_password(body.password)
        db.commit()
        return {"ok": True}

    @app.post("/api/me/telegram-link")
    def telegram_link(user: Person = Depends(current_user), db: Session = Depends(get_db)):
        bot = svc.company.notifiers.telegram.bot_username
        if not (svc.company.notifiers.telegram.enabled and bot):
            raise HTTPException(400, "Telegram is not enabled for this company")
        token = auth.create_link_token(db, secret, db.merge(user), "telegram", days=1)
        db.commit()
        return {"url": f"https://t.me/{bot}?start={token}"}

    # ----- people -----
    @app.get("/api/people")
    def people(user: Person = Depends(current_user), db: Session = Depends(get_db)):
        q = select(Person).order_by(Person.name)
        if not user.is_admin:
            q = q.where(Person.active.is_(True))
        return [person_out(p, full=user.is_admin) for p in db.scalars(q)]

    def _apply_person(p: Person, body: PersonIn) -> None:
        if body.role not in ("member", "admin"):
            raise HTTPException(422, "role must be member or admin")
        p.name, p.role, p.aliases = body.name.strip(), body.role, [a.strip() for a in body.aliases if a.strip()]
        p.email = body.email.strip().lower() if body.email else None
        p.phone = normalize_phone(body.phone)
        p.telegram_username = body.telegram_username.lstrip("@") if body.telegram_username else None
        p.erp_user_id, p.voiceprint_opt_out, p.active = body.erp_user_id, body.voiceprint_opt_out, body.active

    @app.post("/api/people")
    def add_person(body: PersonIn, _: Person = Depends(admin_user), db: Session = Depends(get_db)):
        p = Person(name=body.name)
        _apply_person(p, body)
        db.add(p)
        db.commit()
        return person_out(p, full=True)

    @app.put("/api/people/{pid}")
    def edit_person(pid: int, body: PersonIn, _: Person = Depends(admin_user), db: Session = Depends(get_db)):
        p = db.get(Person, pid) or (_ for _ in ()).throw(HTTPException(404, "not found"))
        _apply_person(p, body)
        if p.voiceprint_opt_out:
            from ..db import Voiceprint

            db.query(Voiceprint).filter(Voiceprint.person_id == p.id).delete()
        db.commit()
        return person_out(p, full=True)

    @app.post("/api/people/{pid}/invite")
    def invite(pid: int, _: Person = Depends(admin_user), db: Session = Depends(get_db)):
        p = db.get(Person, pid) or (_ for _ in ()).throw(HTTPException(404, "not found"))
        token = auth.create_link_token(db, secret, p, "invite")
        db.commit()
        return {"url": f"{svc.settings.public_base_url}/#/invite/{token}"}

    @app.post("/api/people/import")
    async def import_people(file: UploadFile = File(...), _: Person = Depends(admin_user),
                            db: Session = Depends(get_db)):
        try:
            stats = upsert_people(db, read_rows(await file.read(), file.filename or "people.csv"))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        db.commit()
        return stats

    # ----- meetings -----
    @app.get("/api/meetings")
    def meetings(user: Person = Depends(current_user), db: Session = Depends(get_db)):
        q = select(Meeting).order_by(Meeting.started_at.desc()).limit(200)
        if not user.is_admin:
            mine = select(Attendee.meeting_id).where(Attendee.person_id == user.id)
            q = q.where(or_(Meeting.chair_id == user.id, Meeting.created_by_id == user.id, Meeting.id.in_(mine)))
        return [meeting_out(m) for m in db.scalars(q)]

    @app.post("/api/meetings")
    def start_meeting(body: MeetingIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        vc = svc.company.voice
        vp = vc.voiceprints_default_on if body.voiceprints_on is None else body.voiceprints_on
        m = Meeting(title=body.title.strip(), chair_id=body.chair_id or user.id, created_by_id=user.id,
                    voiceprints_on=bool(vp and vc.voiceprints_allowed))
        ids = sorted(set(body.attendee_ids) | {m.chair_id})
        m.attendees = [Attendee(person_id=i) for i in ids if db.get(Person, i)]
        db.add(m)
        db.commit()
        return meeting_out(m)

    @app.get("/api/meetings/{mid}")
    def meeting_detail(mid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user)
        people = pipeline.people_map(db)
        out = meeting_out(m)
        out["can_manage"] = can_manage(m, user)
        if not can_read_content(m, user):
            return {**out, "minutes": None, "speakers": [], "transcript": [], "tasks": []}
        out["minutes"] = m.minutes
        out["speakers"] = [{"label": s.label, "person_id": s.person_id, "auto_matched": s.auto_matched,
                            "score": s.match_score, "seconds": s.seconds} for s in m.speakers]
        spk = {s.label: people[s.person_id].name for s in m.speakers if s.person_id in people}
        out["transcript"] = [{"t": hms(u.start), "speaker": u.speaker_label,
                              "name": spk.get(u.speaker_label or "", u.speaker_label), "text": u.text}
                             for u in m.utterances]
        out["tasks"] = [task_out(t) for t in db.scalars(select(Task).where(Task.meeting_id == m.id).order_by(Task.id))]
        return out

    @app.get("/api/meetings/{mid}/live")
    def live(mid: int, after: int = 0, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user)
        utts = db.scalars(select(Utterance).where(Utterance.meeting_id == m.id, Utterance.id > after)
                          .order_by(Utterance.id).limit(200)) if can_read_content(m, user) else []
        return {"status": m.status, "audio_seconds": round(m.audio_seconds, 1),
                "processed_seconds": round(m.processed_seconds, 1),
                "lines": [{"id": u.id, "t": hms(u.start), "text": u.text} for u in utts]}

    @app.post("/api/meetings/{mid}/chunks")
    async def chunk(mid: int, request: Request, seq: int = Query(ge=0), ext: str = Query("pcm", pattern="^[a-z0-9]{2,5}$"),
                    user: Person = Depends(current_user), db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user, manage=True)
        if m.status != "recording":
            raise HTTPException(409, f"meeting is {m.status}")
        data = await request.body()
        if not data or len(data) > MAX_CHUNK:
            raise HTTPException(413, "chunk empty or too large")
        pipeline.ingest_chunk(db, svc, m, seq, data, f".{ext}")
        db.commit()
        return {"next_seq": m.next_chunk_seq, "audio_seconds": round(m.audio_seconds, 1)}

    @app.post("/api/meetings/{mid}/stop")
    def stop(mid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user, manage=True)
        pipeline.stop_recording(db, m)
        db.commit()
        return meeting_out(m)

    @app.post("/api/meetings/{mid}/upload")
    async def upload(mid: int, file: UploadFile = File(...), user: Person = Depends(current_user),
                     db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user, manage=True)
        if m.status != "recording" or m.audio_seconds > 0:
            raise HTTPException(409, "upload a file only to a new meeting")
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        m.source = "upload"
        try:
            pipeline.ingest_file(db, svc, m, data, Path(file.filename or "a.bin").suffix or ".bin")
        except Exception as e:
            raise HTTPException(422, f"could not read the audio file: {e}") from None
        db.commit()
        return meeting_out(m)

    @app.put("/api/meetings/{mid}/draft")
    def save_draft(mid: int, body: DraftIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user, manage=True)
        if m.status != "draft":
            raise HTTPException(409, f"meeting is {m.status}")
        try:
            pipeline.apply_edits(db, svc, m, body.minutes, body.speakers)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        db.commit()
        return {"ok": True, "minutes": m.minutes}

    @app.post("/api/meetings/{mid}/approve")
    def approve(mid: int, body: DraftIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user, manage=True)
        try:
            pipeline.approve(db, svc, m, db.merge(user), body.minutes, body.speakers)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        db.commit()
        return meeting_out(m)

    @app.post("/api/meetings/{mid}/retry")
    def retry(mid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        from ..jobs import enqueue

        m = get_meeting(db, mid, user, manage=True)
        if m.status != "failed":
            raise HTTPException(409, "only failed meetings can be retried")
        kind = "distribute" if m.approved_at else "finalize"
        m.status, m.error = ("approved" if m.approved_at else "processing"), None
        enqueue(db, kind, m.id)
        db.commit()
        return meeting_out(m)

    @app.get("/api/meetings/{mid}/minutes.pdf")
    def minutes_pdf(mid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user)
        if not m.minutes or not can_read_content(m, user):
            raise HTTPException(404, "no minutes yet")
        pdf = render_pdf(render_minutes_html(svc.company, m, pipeline.people_map(db)))
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="minutes-{m.id}.pdf"'})

    @app.get("/api/meetings/{mid}/transcript.txt")
    def transcript_txt(mid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        m = get_meeting(db, mid, user)
        if not can_read_content(m, user):
            raise HTTPException(404, "no transcript yet")
        return PlainTextResponse(render_transcript_text(svc.company, m, pipeline.people_map(db)))

    # ----- tasks -----
    @app.get("/api/tasks")
    def tasks(scope: str = "mine", user: Person = Depends(current_user), db: Session = Depends(get_db)):
        q = select(Task).order_by(Task.status, Task.due_date.is_(None), Task.due_date, Task.id)
        if scope == "all" and user.is_admin:
            pass
        elif scope == "chaired":
            q = q.join(Meeting, Meeting.id == Task.meeting_id).where(Meeting.chair_id == user.id)
        else:
            q = q.where(Task.owner_id == user.id)
        return [task_out(t) for t in db.scalars(q.limit(500))]

    @app.patch("/api/tasks/{tid}")
    def update_task(tid: int, body: StatusIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
        t = db.get(Task, tid)
        if not t or not can_edit(t, user):
            raise HTTPException(404, "task not found")
        try:
            set_status(db, svc.company, t, body.status)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        db.commit()
        return task_out(t)

    # ----- central portal placeholder -----
    @app.get("/api/central/info")
    def central_info(request: Request, db: Session = Depends(get_db)):
        tok = svc.settings.central_ui_token
        if not (svc.company.central.enabled and tok) or request.headers.get("authorization") != f"Bearer {tok}":
            raise HTTPException(404, "not found")
        since = utcnow() - timedelta(days=30)
        recent = select(Meeting).where(Meeting.started_at >= since).subquery()
        return {"agent": "meeting-minutes", "version": __version__, "company_id": svc.company.company.id,
                "company": svc.company.company.name, "erp_customer_code": svc.company.company.erp_customer_code,
                "people": db.scalar(select(func.count(Person.id)).where(Person.active.is_(True))),
                "meetings_30d": db.scalar(select(func.count()).select_from(recent)),
                "audio_seconds_30d": round(db.scalar(select(func.coalesce(func.sum(recent.c.audio_seconds), 0))), 1)}

    from .webhooks import router as webhook_router

    app.include_router(webhook_router(svc))

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
