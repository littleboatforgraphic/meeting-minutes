from datetime import date

import numpy as np
from fakes import silence, tone

from meeting_minutes import voice
from meeting_minutes.directory import normalize_phone, read_rows
from meeting_minutes.extraction import NO_DEADLINE_REMARK, ActionItem, MinutesDraft, finalize
from meeting_minutes.people import Candidate, resolve
from meeting_minutes.segmentation import find_segments


def test_segments_split_on_silence():
    pcm = silence(0.5) + tone(300, 2) + silence(1) + tone(500, 1.5) + silence(1)
    segs, consumed = find_segments(pcm, 10.0, final=False, vad="energy")
    assert len(segs) == 2
    assert abs(segs[0].start - 10.5) < 0.3 and abs(segs[0].end - 12.5) < 0.3
    assert consumed >= segs[1].end - 0.2


def test_open_speech_waits_for_more_audio_unless_final():
    pcm = silence(0.5) + tone(300, 2)
    segs, consumed = find_segments(pcm, 0.0, final=False, vad="energy")
    assert segs == [] and consumed <= 0.6
    segs, consumed = find_segments(pcm, 0.0, final=True, vad="energy")
    assert len(segs) == 1 and consumed >= 2.4


def test_long_speech_is_cut_at_max_len():
    segs, _ = find_segments(tone(300, 70), 0.0, final=True, vad="energy", max_len=30)
    assert len(segs) == 3 and all(s.end - s.start <= 30.5 for s in segs)


def test_resolve_names_and_ambiguity():
    c = [Candidate(1, "Ali bin Ahmad", ("Ali",), True), Candidate(2, "Tan Mei Ling", ("Mei Ling",), True),
         Candidate(3, "Tan Mei Hua", (), False)]
    assert resolve("Encik Ali", c)[0] == 1
    assert resolve("mei ling", c)[0] == 2
    assert resolve(None, c) == (None, [])
    pid, plausible = resolve("Tan Mei", c)
    assert pid is None or pid == 2


def test_missing_deadline_gets_remark():
    d = finalize(MinutesDraft(title="t", summary="s", action_items=[
        ActionItem(task="a"), ActionItem(task="b", due_date=date(2026, 1, 2)), ActionItem(task="c", remark="urgent")]))
    assert d.action_items[0].remark == NO_DEADLINE_REMARK
    assert d.action_items[1].remark is None
    assert d.action_items[2].remark == f"urgent; {NO_DEADLINE_REMARK}"
    assert finalize(d).action_items[0].remark == NO_DEADLINE_REMARK  # idempotent


def test_lenient_due_date():
    assert ActionItem(task="x", due_date="N/A").due_date is None
    assert ActionItem(task="x", due_date="2026-11-03T00:00").due_date == date(2026, 11, 3)


def test_phone_and_directory_import():
    assert normalize_phone("012-345 6789") == "60123456789"
    assert normalize_phone("+65 9123 4567") == "6591234567"
    rows = read_rows(b"Name,Email,Mobile,Nickname\nAli bin Ahmad,ALI@x.com,0123456789,Ali;Abang\n,,,\n", "hr.csv")
    assert rows == [{"name": "Ali bin Ahmad", "email": "ALI@x.com", "phone": "0123456789", "aliases": "Ali;Abang"}]


def test_cluster_and_match():
    a, b = np.eye(4)[0], np.eye(4)[1]
    labels = voice.cluster([a, a * 0.9 + b * 0.05, b, None], 0.5, [1, 1, 1, 1])
    assert labels[0] == labels[1] != labels[2] and labels[3] is None
    m = voice.match_speakers({"S1": a, "S2": b}, {7: voice.normalize(a), 8: voice.normalize(np.eye(4)[2])}, 0.6)
    assert m == {"S1": (7, m["S1"][1])}


def test_directory_does_not_merge_people_with_different_emails(env):
    from meeting_minutes.db import Person, session_scope
    from meeting_minutes.directory import upsert_people

    with session_scope() as db:
        upsert_people(db, [{"name": "Lim Wei", "email": "lim.wei@acme.my"}])
        stats = upsert_people(db, [{"name": "Lim Wei", "email": "wei.lim@acme.my"}])
        assert stats == {"created": 1, "updated": 0}
        emails = sorted(p.email for p in db.query(Person).all())
        assert emails == ["lim.wei@acme.my", "wei.lim@acme.my"]


def test_long_transcript_is_split_and_merged():
    from datetime import date

    from fakes import MINUTES, FakeLLM

    from meeting_minutes.extraction import extract_minutes

    llm = FakeLLM(MINUTES)
    draft = extract_minutes(llm, model="m", temperature=0, company_name="Acme", language="en", meeting_title="T",
                            meeting_date=date(2026, 10, 20), timezone="Asia/Kuala_Lumpur", attendees=[],
                            transcript="\n".join(f"line {i}" for i in range(50)), max_chars=60)
    assert len(llm.calls) > 2 and draft.title == MINUTES["title"]


def test_directory_new_email_with_existing_phone(env):
    from meeting_minutes.db import Person, session_scope
    from meeting_minutes.directory import upsert_people

    with session_scope() as db:
        upsert_people(db, [{"name": "Lim Wei", "email": "lim.wei@acme.my", "phone": "012-3334444"}])
        upsert_people(db, [{"name": "Lim Wei", "email": "wei.lim@acme.my", "phone": "012-3334444"}])
        people = {p.email: p.phone for p in db.query(Person).all()}
        assert people == {"lim.wei@acme.my": "60123334444", "wei.lim@acme.my": None}
