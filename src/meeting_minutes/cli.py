"""`mm` command line: serve, worker, people admin, offline processing, retention."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select


def _svc():
    from .pipeline import default_services

    return default_services()


def cmd_check(args) -> int:
    from .company import get_company
    from .notify import build_dispatcher
    from .settings import get_settings

    s, c = get_settings(), get_company()
    print(f"company: {c.company.id} ({c.company.name}), tz {c.company.timezone}")
    print(f"asr: {c.asr.model}  llm: {c.llm.model}  gateway: {'set' if s.llm_base_url else 'NOT SET'}")
    print(f"auth: {c.auth.method}  channels: {sorted(build_dispatcher(s, c).channels) or 'none'}")
    print(f"db: {s.db_url.split('@')[-1]}  data: {s.data_dir}")
    if s.secret_key == "dev-insecure-secret":
        print("WARNING: MM_SECRET_KEY is not set")
    return 0


def cmd_init_db(args) -> int:
    from .db import init_db

    init_db()
    print("database ready")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .db import init_db

    init_db()
    uvicorn.run("meeting_minutes.web.app:create_app", factory=True, host=args.host, port=args.port,
                proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_worker(args) -> int:
    from .db import init_db
    from .jobs import worker_loop

    init_db()
    svc = _svc()
    svc.settings.check_secret()
    worker_loop(svc)
    return 0


def cmd_add_person(args) -> int:
    from .auth import hash_password
    from .db import init_db, session_scope
    from .directory import upsert_people

    init_db()
    rec = {k: v for k, v in {"name": args.name, "email": args.email, "phone": args.phone,
                             "telegram_username": args.telegram, "erp_user_id": args.erp,
                             "aliases": args.aliases, "role": "admin" if args.admin else None}.items() if v}
    with session_scope() as db:
        upsert_people(db, [rec])
        from .db import Person

        p = db.scalar(select(Person).where(Person.name == args.name))
        if args.password:
            p.password_hash = hash_password(getpass.getpass(f"Password for {p.name}: "))
        print(f"person #{p.id} {p.name} ({p.role})")
    return 0


def cmd_import_people(args) -> int:
    from .db import init_db, session_scope
    from .directory import read_rows, upsert_people

    init_db()
    path = Path(args.file)
    with session_scope() as db:
        print(upsert_people(db, read_rows(path.read_bytes(), path.name)))
    return 0


def cmd_invite(args) -> int:
    from .auth import create_link_token
    from .db import Person, init_db, session_scope
    from .settings import get_settings

    init_db()
    s = get_settings()
    with session_scope() as db:
        p = db.get(Person, args.person_id)
        if not p:
            print("no such person", file=sys.stderr)
            return 1
        token = create_link_token(db, s.secret_key, p, "invite", days=args.days)
        print(f"{s.public_base_url}/#/invite/{token}")
    return 0


def cmd_process(args) -> int:
    """Process an audio file end-to-end without the web app (for testing / back-office uploads)."""
    from . import jobs, pipeline
    from .db import Attendee, Meeting, init_db, session_scope

    init_db()
    svc = _svc()
    path = Path(args.file)
    with session_scope() as db:
        m = Meeting(title=args.title, chair_id=args.chair, source="upload", status="processing",
                    voiceprints_on=args.voiceprints)
        m.attendees = [Attendee(person_id=i) for i in args.attendee or []]
        db.add(m)
        db.flush()
        pipeline.ingest_file(db, svc, m, path.read_bytes(), path.suffix)
        mid = m.id
    while jobs.run_next(svc):
        pass
    with session_scope() as db:
        m = db.get(Meeting, mid)
        print(json.dumps({"meeting_id": mid, "status": m.status, "error": m.error, "minutes": m.minutes},
                         indent=2, ensure_ascii=False))
    return 0


def cmd_purge(args) -> int:
    from .db import init_db, session_scope, utcnow
    from .retention import purge

    init_db()
    svc = _svc()
    with session_scope() as db:
        print(purge(db, svc.company, svc.settings, utcnow() if not args.now else datetime.fromisoformat(args.now)))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="mm", description="Meeting secretary agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="show the loaded company config").set_defaults(fn=cmd_check)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(fn=cmd_serve)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    sp = sub.add_parser("add-person")
    sp.add_argument("--name", required=True)
    for f in ("email", "phone", "telegram", "erp", "aliases"):
        sp.add_argument(f"--{f}")
    sp.add_argument("--admin", action="store_true")
    sp.add_argument("--password", action="store_true", help="prompt for a password")
    sp.set_defaults(fn=cmd_add_person)
    sp = sub.add_parser("import-people", help="import an HR Excel/CSV directory")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_import_people)
    sp = sub.add_parser("invite", help="print a one-time link for a person to set a password")
    sp.add_argument("person_id", type=int)
    sp.add_argument("--days", type=int, default=7)
    sp.set_defaults(fn=cmd_invite)
    sp = sub.add_parser("process", help="process an audio file end-to-end")
    sp.add_argument("file")
    sp.add_argument("--title", default="Meeting")
    sp.add_argument("--chair", type=int)
    sp.add_argument("--attendee", type=int, action="append")
    sp.add_argument("--voiceprints", action="store_true")
    sp.set_defaults(fn=cmd_process)
    sp = sub.add_parser("purge", help="apply retention now")
    sp.add_argument("--now")
    sp.set_defaults(fn=cmd_purge)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
