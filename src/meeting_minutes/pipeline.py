"""Meeting pipeline: audio chunks -> speech segments -> ASR -> speakers -> minutes draft -> approval -> distribution."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import voice
from .asr import OpenAITranscriber, Transcriber, build_prompt
from .audio import MeetingAudio, decode_bytes, pcm_seconds, pcm_to_wav
from .company import CompanyConfig, get_company
from .db import Meeting, Notification, Person, Speaker, Task, Utterance, Voiceprint, utcnow
from .extraction import MinutesDraft, extract_minutes, transcript_text
from .extraction import finalize as finalize_draft
from .llm import make_client
from .notify import Attachment, Dispatcher, Message, build_dispatcher
from .people import Candidate, resolve
from .render import local_time, render_minutes_html, render_pdf, render_transcript_text, transcript_lines
from .segmentation import find_segments
from .settings import Settings, get_settings
from .tasks import get_sink, task_buttons, task_line

log = logging.getLogger(__name__)

WINDOW_SECONDS = 600.0


@dataclass
class Services:
    settings: Settings
    company: CompanyConfig
    _transcriber: Transcriber | None = None
    _embedder: voice.Embedder | None = None
    _llm: object | None = None
    _dispatcher: Dispatcher | None = None
    extra: dict = field(default_factory=dict)

    @cached_property
    def transcriber(self) -> Transcriber:
        return self._transcriber or OpenAITranscriber(self.llm, self.company.asr.model, self.company.asr.language)

    @cached_property
    def embedder(self) -> voice.Embedder:
        return self._embedder or voice.get_embedder(self.company.voice.diarization, str(self.settings.data_dir / "models"))

    @cached_property
    def llm(self):
        return self._llm or make_client(self.settings)

    @cached_property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher or build_dispatcher(self.settings, self.company)

    def audio(self, meeting_id: int) -> MeetingAudio:
        return MeetingAudio(self.settings.data_dir, meeting_id)

    def url(self, path: str) -> str:
        return f"{self.settings.public_base_url}/#{path}"


def default_services() -> Services:
    return Services(get_settings(), get_company())


def people_map(db: Session) -> dict[int, Person]:
    return {p.id: p for p in db.scalars(select(Person))}


def participant_ids(meeting: Meeting) -> list[int]:
    ids = [a.person_id for a in meeting.attendees]
    if meeting.chair_id and meeting.chair_id not in ids:
        ids.append(meeting.chair_id)
    return ids


# ---------- intake ----------

def ingest_chunk(db: Session, svc: Services, meeting: Meeting, seq: int, data: bytes, suffix: str) -> None:
    from .jobs import enqueue

    a = svc.audio(meeting.id)
    meeting.next_chunk_seq = a.add_chunk(seq, data, suffix, meeting.next_chunk_seq)
    meeting.audio_seconds = a.seconds()
    enqueue(db, "process_audio", meeting.id)


def ingest_file(db: Session, svc: Services, meeting: Meeting, data: bytes, suffix: str) -> None:
    from .jobs import enqueue

    pcm = decode_bytes(data, suffix)
    svc.audio(meeting.id).add_pcm(pcm)
    meeting.audio_seconds = pcm_seconds(pcm)
    meeting.ended_at = meeting.started_at + timedelta(seconds=meeting.audio_seconds)
    meeting.status = "processing"
    enqueue(db, "finalize", meeting.id)


def stop_recording(db: Session, meeting: Meeting) -> None:
    from .jobs import enqueue

    if meeting.status != "recording":
        return
    meeting.status = "processing"
    meeting.ended_at = utcnow()
    enqueue(db, "finalize", meeting.id)


# ---------- transcription ----------

def process_audio(db: Session, svc: Services, meeting: Meeting, final: bool = False) -> int:
    """Transcribe new speech since the last call. Commits after each window so the live preview updates."""
    a = svc.audio(meeting.id)
    cfg = svc.company.asr
    people = people_map(db)
    prompt = build_prompt(svc.company, [people[i].name for i in participant_ids(meeting) if i in people])
    added = 0
    while True:
        total = a.seconds()
        start = meeting.processed_seconds
        if total - start <= 0.05:
            break
        end = min(total, start + WINDOW_SECONDS)
        last_window = end >= total
        pcm = a.read(start, end)
        segs, consumed = find_segments(pcm, start, final=final and last_window, vad=cfg.vad,
                                       min_silence=cfg.min_silence_seconds, max_len=cfg.max_segment_seconds)
        slices = [a.read(s.start, s.end) for s in segs]
        embeddings = [svc.embedder.embed(p) for p in slices]
        with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:
            texts = list(pool.map(lambda p: svc.transcriber.transcribe(pcm_to_wav(p), prompt), slices))
        for s, e, t in zip(segs, embeddings, texts, strict=True):
            if t:
                db.add(Utterance(meeting_id=meeting.id, start=s.start, end=s.end, text=t,
                                 embedding=voice.to_bytes(e) if e is not None else None))
                added += 1
        meeting.processed_seconds = max(consumed, start)
        db.commit()
        if last_window or consumed <= start:
            break
    return added


def diarize(db: Session, svc: Services, meeting: Meeting) -> None:
    db.execute(delete(Speaker).where(Speaker.meeting_id == meeting.id))
    utts = list(db.scalars(select(Utterance).where(Utterance.meeting_id == meeting.id).order_by(Utterance.start)))
    embs = [voice.from_bytes(u.embedding) if u.embedding else None for u in utts]
    weights = [max(0.1, u.end - u.start) for u in utts]
    labels = voice.cluster(embs, svc.company.voice.cluster_threshold, weights)
    if all(lab is None for lab in labels):
        labels = [0] * len(utts)  # no speaker embeddings: one unlabelled speaker
    else:
        # utterances too short for an embedding inherit the previous speaker
        prev = 0
        for i, lab in enumerate(labels):
            labels[i] = prev = lab if lab is not None else prev
    for u, lab in zip(utts, labels, strict=True):
        u.speaker_label = f"S{lab + 1}"
    cents = voice.centroids(embs, labels, weights)
    seconds: dict[int, float] = {}
    for lab, w in zip(labels, weights, strict=True):
        seconds[lab] = seconds.get(lab, 0.0) + w
    speakers = {f"S{lab + 1}": Speaker(meeting_id=meeting.id, label=f"S{lab + 1}", seconds=round(sec, 1),
                                       centroid=voice.to_bytes(cents[lab]) if lab in cents else None)
                for lab, sec in sorted(seconds.items())}
    if meeting.voiceprints_on and svc.company.voice.voiceprints_allowed and cents:
        allowed = set(participant_ids(meeting))
        q = select(Voiceprint, Person).join(Person, Person.id == Voiceprint.person_id).where(
            Person.active.is_(True), Person.voiceprint_opt_out.is_(False))
        prints = {p.id: voice.from_bytes(vp.embedding) for vp, p in db.execute(q) if not allowed or p.id in allowed}
        matches = voice.match_speakers({f"S{k + 1}": v for k, v in cents.items()}, prints,
                                       svc.company.voice.match_threshold)
        for label, (pid, score) in matches.items():
            speakers[label].person_id, speakers[label].auto_matched, speakers[label].match_score = pid, True, score
    db.add_all(speakers.values())
    db.flush()


def finalize(db: Session, svc: Services, meeting: Meeting) -> None:
    """After the meeting: flush remaining audio, label speakers, delete audio, draft minutes, ask the chair."""
    process_audio(db, svc, meeting, final=True)
    diarize(db, svc, meeting)
    svc.audio(meeting.id).delete()
    db.refresh(meeting)
    draft_minutes(db, svc, meeting)
    meeting.status = "draft"
    db.flush()
    notify_chair(db, svc, meeting)


def _candidates(db: Session, meeting: Meeting) -> list[Candidate]:
    att = set(participant_ids(meeting))
    return [Candidate(p.id, p.name, tuple(p.aliases or ()), p.id in att)
            for p in db.scalars(select(Person).where(Person.active.is_(True)))]


def draft_minutes(db: Session, svc: Services, meeting: Meeting) -> None:
    people = people_map(db)
    text = transcript_text(transcript_lines(meeting, people))
    start = local_time(meeting.started_at, svc.company.company.timezone)
    if not text.strip():
        draft = finalize_draft(MinutesDraft(title=meeting.title, summary="No speech was detected in the recording."))
    else:
        draft = extract_minutes(
            svc.llm, model=svc.company.llm.model, temperature=svc.company.llm.temperature,
            company_name=svc.company.company.name, language=svc.company.output_language, meeting_title=meeting.title,
            meeting_date=start.date(), timezone=svc.company.company.timezone,
            attendees=[people[i].name for i in participant_ids(meeting) if i in people], transcript=text,
            max_chars=svc.company.llm.max_transcript_chars_per_call)
    data = draft.model_dump(mode="json")
    cands = _candidates(db, meeting)
    for item in data["action_items"]:
        pid, plausible = resolve(item.get("owner"), cands)
        item["owner_id"], item["owner_candidates"] = pid, plausible
    meeting.minutes = data


def notify_chair(db: Session, svc: Services, meeting: Meeting) -> None:
    chair = db.get(Person, meeting.chair_id) if meeting.chair_id else None
    if not chair:
        return
    n = len((meeting.minutes or {}).get("action_items", []))
    msg = Message(subject=f"Draft minutes ready: {meeting.title}",
                  text=f"The draft minutes ({n} action item(s)) are ready for your review and approval. "
                       "Nothing is sent to attendees until you approve.",
                  link=svc.url(f"/meetings/{meeting.id}"), link_label="Review and approve")
    svc.dispatcher.send(db, chair, msg, "approval_request", meeting.id)


# ---------- approval and distribution ----------

def apply_edits(db: Session, svc: Services, meeting: Meeting, minutes: dict | None, speakers: dict[str, int | None] | None
                ) -> None:
    if minutes is not None:
        draft = finalize_draft(MinutesDraft.model_validate(minutes))
        data = draft.model_dump(mode="json")
        for item, raw in zip(data["action_items"], minutes.get("action_items", []), strict=True):
            item["owner_id"] = raw.get("owner_id")
            item["owner_candidates"] = raw.get("owner_candidates", [])
        meeting.minutes = data
    if speakers is not None:
        for s in meeting.speakers:
            if s.label in speakers:
                new = speakers[s.label]
                if new != s.person_id:
                    s.person_id, s.auto_matched, s.match_score = new, False, None
    db.flush()


def learn_voiceprints(db: Session, svc: Services, meeting: Meeting) -> int:
    if not (meeting.voiceprints_on and svc.company.voice.voiceprints_allowed):
        return 0
    learned = 0
    for s in meeting.speakers:
        person = db.get(Person, s.person_id) if s.person_id else None
        if not person or person.voiceprint_opt_out or not s.centroid or s.seconds < 5:
            continue
        vp = db.scalar(select(Voiceprint).where(Voiceprint.person_id == person.id))
        vec, n = voice.merge_voiceprint(voice.from_bytes(vp.embedding) if vp else None, vp.samples if vp else 0,
                                        voice.from_bytes(s.centroid))
        if vp:
            vp.embedding, vp.samples, vp.updated_at = voice.to_bytes(vec), n, utcnow()
        else:
            db.add(Voiceprint(person_id=person.id, embedding=voice.to_bytes(vec), samples=n))
        learned += 1
    db.flush()
    return learned


def approve(db: Session, svc: Services, meeting: Meeting, approver: Person, minutes: dict | None = None,
            speakers: dict[str, int | None] | None = None) -> None:
    from .jobs import enqueue

    if meeting.status != "draft":
        raise ValueError(f"meeting is {meeting.status}, not draft")
    apply_edits(db, svc, meeting, minutes, speakers)
    learn_voiceprints(db, svc, meeting)
    db.execute(delete(Task).where(Task.meeting_id == meeting.id))
    for item in meeting.minutes.get("action_items", []):
        hms = item.get("timestamp") or ""
        try:
            h, m_, s = (int(x) for x in hms.split(":"))
            ev = h * 3600 + m_ * 60 + s
        except ValueError:
            ev = None
        db.add(Task(meeting_id=meeting.id, title=item["task"], owner_id=item.get("owner_id"),
                    owner_as_said=item.get("owner"), due_date=_date(item.get("due_date")),
                    due_as_said=item.get("due_as_said"), remark=item.get("remark"), evidence_seconds=ev))
    meeting.status, meeting.approved_at, meeting.approved_by_id = "approved", utcnow(), approver.id
    db.flush()
    enqueue(db, "distribute", meeting.id)


def _date(v):
    from datetime import date

    return date.fromisoformat(v) if v else None


def distribute(db: Session, svc: Services, meeting: Meeting) -> int:
    people = people_map(db)
    html = render_minutes_html(svc.company, meeting, people)
    pdf = render_pdf(html)
    safe = "".join(c if c.isalnum() else "-" for c in meeting.title)[:60].strip("-") or "meeting"
    files = [Attachment(f"minutes-{safe}.pdf", pdf, "application/pdf")]
    if not meeting.transcript_purged:
        files.append(Attachment(f"transcript-{safe}.txt",
                                render_transcript_text(svc.company, meeting, people).encode(), "text/plain"))
    tasks = list(db.scalars(select(Task).where(Task.meeting_id == meeting.id).order_by(Task.id)))
    sink = get_sink(svc.company)
    for t in tasks:
        t.external_ref = sink.push(t, people.get(t.owner_id)) or t.external_ref
    participants = set(participant_ids(meeting))
    recipients = participants | {t.owner_id for t in tasks if t.owner_id}
    summary = (meeting.minutes or {}).get("summary", "")
    done = set(db.scalars(select(Notification.person_id).where(
        Notification.kind == "minutes", Notification.meeting_id == meeting.id, Notification.status == "sent")))
    sent = 0
    for pid in sorted(recipients):
        person = people.get(pid)
        if not person or not person.active or pid in done:  # already sent before a crash/retry
            continue
        mine = [t for t in tasks if t.owner_id == pid]
        body = summary + "\n\n" + ("Your tasks:\n" + "\n".join(f"- {task_line(t)}" for t in mine) if mine
                                   else "You have no tasks from this meeting.")
        if pid in participants:
            msg = Message(subject=f"Minutes: {meeting.title}", text=body, html=html, attachments=files,
                          link=svc.url(f"/meetings/{meeting.id}"), link_label="Open minutes", buttons=task_buttons(mine))
        else:  # assigned a task but not in the meeting: their tasks only, not the minutes/transcript
            msg = Message(subject=f"Tasks from: {meeting.title}",
                          text="Your tasks:\n" + "\n".join(f"- {task_line(t)}" for t in mine),
                          link=svc.url("/tasks"), link_label="My tasks", buttons=task_buttons(mine))
        if svc.dispatcher.send(db, person, msg, "minutes", meeting.id):
            sent += 1
        db.commit()  # record each send so a retried job skips this person
    meeting.status, meeting.distributed_at = "distributed", utcnow()
    db.flush()
    return sent
