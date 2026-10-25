from datetime import timedelta

from fakes import make_services
from sqlalchemy import select

from meeting_minutes.db import Meeting, Task, Utterance, session_scope, utcnow
from meeting_minutes.retention import purge


def test_transcript_week_minutes_three_months(env):
    svc = make_services()
    now = utcnow()
    with session_scope() as db:
        m = Meeting(title="Old", status="distributed", started_at=now - timedelta(days=8), ended_at=now - timedelta(days=8),
                    minutes={"title": "Old", "summary": "s"})
        db.add(m)
        db.flush()
        db.add(Utterance(meeting_id=m.id, start=0, end=1, text="hi"))
        db.add(Task(meeting_id=m.id, title="t"))
        fresh = Meeting(title="New", status="draft", started_at=now)
        db.add(fresh)
        db.flush()
        db.add(Utterance(meeting_id=fresh.id, start=0, end=1, text="keep"))
    with session_scope() as db:
        purge(db, svc.company, svc.settings, now)
    with session_scope() as db:
        assert [u.text for u in db.scalars(select(Utterance))] == ["keep"]
        old = db.scalar(select(Meeting).where(Meeting.title == "Old"))
        assert old.transcript_purged and old.minutes
        assert db.scalar(select(Task)) is not None
    with session_scope() as db:
        purge(db, svc.company, svc.settings, now + timedelta(days=85))
    with session_scope() as db:
        old = db.scalar(select(Meeting).where(Meeting.title == "Old"))
        assert old.minutes is None  # minutes gone, row kept so its tasks stay linked (chair view)
        assert db.scalar(select(Task)).meeting_id == old.id  # tasks keep their own 90 days from creation
    with session_scope() as db:
        purge(db, svc.company, svc.settings, now + timedelta(days=91))
    with session_scope() as db:
        assert db.scalar(select(Task)) is None
        assert [m.title for m in db.scalars(select(Meeting))] == ["New"]
