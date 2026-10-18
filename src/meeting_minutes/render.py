"""Render approved minutes to HTML/PDF and the transcript to text."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, PackageLoader, select_autoescape

from .company import CompanyConfig
from .db import Meeting, Person


def _env(custom: str | None) -> Environment:
    loader = FileSystemLoader(str(Path(custom).parent)) if custom else PackageLoader("meeting_minutes", "templates")
    return Environment(loader=loader, autoescape=select_autoescape(["html"]))


def local_time(dt: datetime | None, tz: str) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))


def hms(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def minutes_context(company: CompanyConfig, meeting: Meeting, people: dict[int, Person]) -> dict:
    m = meeting.minutes or {}
    tz = company.company.timezone
    attendees = sorted((a.person.name for a in meeting.attendees), key=str.lower)
    items = []
    for i, a in enumerate(m.get("action_items", []), 1):
        owner = people.get(a.get("owner_id")) if a.get("owner_id") else None
        items.append({**a, "no": i, "owner_name": owner.name if owner else (a.get("owner") or "Unassigned")})
    return {
        "company": company.company,
        "meeting": meeting,
        "m": m,
        "items": items,
        "attendees": attendees,
        "chair": people.get(meeting.chair_id).name if meeting.chair_id in people else "",
        "started": local_time(meeting.started_at, tz),
        "ended": local_time(meeting.ended_at, tz),
        "approved_by": people.get(meeting.approved_by_id).name if meeting.approved_by_id in people else None,
        "approved_at": local_time(meeting.approved_at, tz),
    }


def render_minutes_html(company: CompanyConfig, meeting: Meeting, people: dict[int, Person]) -> str:
    env = _env(company.minutes_template)
    name = Path(company.minutes_template).name if company.minutes_template else "minutes.html"
    return env.get_template(name).render(**minutes_context(company, meeting, people))


def render_pdf(html: str) -> bytes:
    from weasyprint import HTML

    return HTML(string=html).write_pdf()


def transcript_lines(meeting: Meeting, people: dict[int, Person]) -> list[tuple[float, str, str]]:
    names = {s.label: (people[s.person_id].name if s.person_id in people else f"Speaker {s.label.lstrip('S')}")
             for s in meeting.speakers}
    return [(u.start, names.get(u.speaker_label, "Speaker"), u.text) for u in meeting.utterances if u.text.strip()]


def render_transcript_text(company: CompanyConfig, meeting: Meeting, people: dict[int, Person]) -> str:
    from .extraction import transcript_text

    start = local_time(meeting.started_at, company.company.timezone)
    head = f"{company.company.name}\n{meeting.title}\n{start:%d %b %Y %H:%M}\n\n" if start else ""
    return head + transcript_text(transcript_lines(meeting, people)) + "\n"
