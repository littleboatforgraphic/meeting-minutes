from fakes import make_services, two_speaker_audio
from sqlalchemy import select

from meeting_minutes import jobs, pipeline
from meeting_minutes.db import Attendee, Meeting, Notification, Person, Task, Voiceprint, session_scope
from meeting_minutes.extraction import NO_DEADLINE_REMARK


def seed(db):
    ali = Person(name="Ali bin Ahmad", aliases=["Ali"], email="ali@acme.my", role="admin")
    mei = Person(name="Tan Mei Ling", aliases=["Mei Ling"], telegram_chat_id="555")
    db.add_all([ali, mei])
    db.flush()
    return ali.id, mei.id


def record_meeting(svc, ali, mei, voiceprints=True, chunk_seconds=2.0):
    with session_scope() as db:
        m = Meeting(title="Weekly production", chair_id=ali, created_by_id=ali, voiceprints_on=voiceprints)
        m.attendees = [Attendee(person_id=ali), Attendee(person_id=mei)]
        db.add(m)
        db.flush()
        mid = m.id
    audio = two_speaker_audio()
    step = int(16000 * 2 * chunk_seconds)
    chunks = [audio[i:i + step] for i in range(0, len(audio), step)]
    order = list(range(len(chunks)))
    order[0], order[1] = order[1], order[0]  # out-of-order arrival
    for seq in order:
        with session_scope() as db:
            pipeline.ingest_chunk(db, svc, db.get(Meeting, mid), seq, chunks[seq], ".pcm")
        while jobs.run_next(svc):
            pass
    with session_scope() as db:
        pipeline.stop_recording(db, db.get(Meeting, mid))
    while jobs.run_next(svc):
        pass
    return mid


def test_full_meeting_flow(env):
    svc = make_services(["Ali: we need a Saturday shift.", "Mei Ling: I will call the supplier."])
    with session_scope() as db:
        ali, mei = seed(db)
    mid = record_meeting(svc, ali, mei)

    with session_scope() as db:
        m = db.get(Meeting, mid)
        assert m.status == "draft", m.error
        assert [u.text for u in m.utterances] == ["Ali: we need a Saturday shift.", "Mei Ling: I will call the supplier."]
        assert [u.speaker_label for u in m.utterances] == ["S1", "S2"]
        assert not svc.audio(mid).dir.exists(), "audio must be deleted after transcription"
        items = m.minutes["action_items"]
        assert [i["owner_id"] for i in items] == [ali, mei]
        assert items[1]["remark"] == NO_DEADLINE_REMARK
        assert "Tan Mei Ling" in svc.transcriber.calls[0]  # attendee names prime the ASR
        kinds = [n.kind for n in db.scalars(select(Notification))]
        assert kinds == ["approval_request"]
        assert svc.dispatcher.channels["email"].sent[0][0] == "Ali bin Ahmad"
        assert not db.scalars(select(Task)).all(), "no tasks before approval"

        pipeline.approve(db, svc, m, db.get(Person, ali), speakers={"S1": ali, "S2": mei})
    while jobs.run_next(svc):
        pass

    with session_scope() as db:
        m = db.get(Meeting, mid)
        assert m.status == "distributed"
        tasks = db.scalars(select(Task).order_by(Task.id)).all()
        assert [(t.owner_id, str(t.due_date)) for t in tasks] == [(ali, "2026-10-23"), (mei, "None")]
        assert tasks[1].remark == NO_DEADLINE_REMARK
        assert db.scalar(select(Voiceprint).where(Voiceprint.person_id == ali)) is not None
    email = svc.dispatcher.channels["email"].sent
    tg = svc.dispatcher.channels["telegram"].sent
    assert email[-1][0] == "Ali bin Ahmad" and "Saturday shift roster" in email[-1][1].text
    assert tg[-1][0] == "Tan Mei Ling" and "steel supplier" in tg[-1][1].text
    assert any(a.filename.endswith(".pdf") and a.content.startswith(b"%PDF") for a in email[-1][1].attachments)
    assert tg[-1][1].buttons and tg[-1][1].buttons[0][1].endswith(":done")
    before = len(email) + len(tg)
    with session_scope() as db:  # a retried distribute job must not resend
        assert pipeline.distribute(db, svc, db.get(Meeting, mid)) == 0
    assert len(email) + len(tg) == before


def test_voiceprints_auto_label_next_meeting(env):
    svc = make_services(["one", "two", "three", "four"])
    with session_scope() as db:
        ali, mei = seed(db)
    mid = record_meeting(svc, ali, mei)
    with session_scope() as db:
        pipeline.approve(db, svc, db.get(Meeting, mid), db.get(Person, ali), speakers={"S1": ali, "S2": mei})
    while jobs.run_next(svc):
        pass
    mid2 = record_meeting(svc, ali, mei)
    with session_scope() as db:
        sp = {s.label: (s.person_id, s.auto_matched) for s in db.get(Meeting, mid2).speakers}
    assert sp == {"S1": (ali, True), "S2": (mei, True)}


def test_voiceprints_off_for_meeting(env):
    svc = make_services(["one", "two", "three", "four"])
    with session_scope() as db:
        ali, mei = seed(db)
    mid = record_meeting(svc, ali, mei, voiceprints=False)
    with session_scope() as db:
        pipeline.approve(db, svc, db.get(Meeting, mid), db.get(Person, ali), speakers={"S1": ali, "S2": mei})
        assert db.scalars(select(Voiceprint)).all() == []


def test_failed_llm_marks_meeting_failed(env):
    svc = make_services(["hello"])
    svc.llm.chat.completions.create = lambda **kw: (_ for _ in ()).throw(RuntimeError("gateway down"))
    with session_scope() as db:
        ali, mei = seed(db)
    mid = record_meeting(svc, ali, mei)
    for _ in range(3):
        with session_scope() as db:
            from meeting_minutes.db import Job

            for j in db.scalars(select(Job).where(Job.status == "queued")):
                j.run_after = j.created_at
        while jobs.run_next(svc):
            pass
    with session_scope() as db:
        m = db.get(Meeting, mid)
        assert m.status == "failed" and "gateway down" in m.error
