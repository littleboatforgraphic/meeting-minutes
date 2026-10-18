"""Turn a speaker-labelled transcript into structured minutes and action items with the LLM."""

from __future__ import annotations

import json
import logging
from datetime import date

from openai import BadRequestError, OpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from .llm import metering

log = logging.getLogger(__name__)

NO_DEADLINE_REMARK = "No deadline stated in meeting"


class ActionItem(BaseModel):
    task: str
    owner: str | None = Field(None, description="person responsible, exactly as named in the meeting")
    due_as_said: str | None = Field(None, description="deadline wording from the meeting, e.g. 'next Friday'")
    due_date: date | None = Field(None, description="resolved deadline YYYY-MM-DD, null if none was stated")
    remark: str | None = None
    timestamp: str | None = Field(None, description="hh:mm:ss in the transcript where it was agreed")

    @field_validator("due_date", mode="before")
    @classmethod
    def _lenient_date(cls, v):
        if v in ("", None, "null", "N/A"):
            return None
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None


class AgendaItem(BaseModel):
    title: str
    discussion: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)


class MinutesDraft(BaseModel):
    title: str
    summary: str
    agenda: list[AgendaItem] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    other_business: list[str] = Field(default_factory=list)
    next_meeting: str | None = None


SYSTEM = """You are the meeting secretary of {company}. You write formal, concise minutes in {language}.
The transcript is speech-to-text of a meeting held in mixed English, Malay, Mandarin and Cantonese; it may contain
recognition errors. Speakers are labelled by name when known, otherwise "Speaker N".
Rules:
- Group the discussion into agenda items in the order they were discussed. Use the stated agenda if there is one.
- Record decisions separately from discussion.
- Action items: every task someone agreed or was asked to do. owner = the person responsible, written exactly as
  named in the meeting (null if nobody was named). Never invent owners or deadlines.
- Deadlines: due_as_said = the wording used; due_date = that deadline resolved to YYYY-MM-DD using the meeting date
  {meeting_date} ({weekday}) in timezone {timezone}. If no deadline was stated, due_as_said and due_date are null.
- timestamp = the [hh:mm:ss] where the action was agreed.
- Write everything in {language}, even if it was said in another language. Keep names, product codes and numbers as said.
Return only JSON matching the schema."""


def transcript_text(lines: list[tuple[float, str, str]]) -> str:
    """lines: (start_seconds, speaker_name, text)."""
    out = []
    for start, speaker, text in lines:
        if not text.strip():
            continue
        s = int(start)
        out.append(f"[{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}] {speaker}: {text.strip()}")
    return "\n".join(out)


def _split(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts, cur, size = [], [], 0
    for line in text.splitlines():
        if size + len(line) > max_chars and cur:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


def _schema() -> dict:
    return {"name": "minutes", "schema": MinutesDraft.model_json_schema()}


def _call(client: OpenAI, model: str, temperature: float, messages: list[dict], op: str) -> MinutesDraft:
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                response_format={"type": "json_schema", "json_schema": _schema()}, **metering(op))
        except BadRequestError:
            # Model/provider without json_schema support: fall back to JSON mode.
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                response_format={"type": "json_object"}, **metering(op))
        content = resp.choices[0].message.content or ""
        try:
            return MinutesDraft.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as e:
            log.warning("minutes JSON invalid (attempt %d): %s", attempt + 1, e)
            messages = [*messages, {"role": "assistant", "content": content},
                        {"role": "user", "content": f"That JSON is invalid ({str(e)[:300]}). Return corrected JSON only."}]
    raise ValueError("LLM did not return valid minutes JSON")


def extract_minutes(client: OpenAI, *, model: str, temperature: float, company_name: str, language: str,
                    meeting_title: str, meeting_date: date, timezone: str, attendees: list[str], transcript: str,
                    max_chars: int = 300_000) -> MinutesDraft:
    system = SYSTEM.format(company=company_name, language=_language_name(language), meeting_date=meeting_date.isoformat(),
                           weekday=meeting_date.strftime("%A"), timezone=timezone)
    header = (f"Meeting title: {meeting_title}\nDate: {meeting_date.isoformat()}\n"
              f"Attendees: {', '.join(attendees) or 'unknown'}\n\n")
    parts = _split(transcript, max_chars)
    if len(parts) == 1:
        draft = _call(client, model, temperature,
                      [{"role": "system", "content": system}, {"role": "user", "content": header + "Transcript:\n" + transcript}],
                      "minutes-extract")
    else:
        partials = []
        for i, part in enumerate(parts, 1):
            partials.append(_call(client, model, temperature, [
                {"role": "system", "content": system},
                {"role": "user", "content": f"{header}This is part {i} of {len(parts)} of the transcript.\n\nTranscript:\n{part}"},
            ], "minutes-extract-part"))
        merged = "\n\n".join(f"Part {i}:\n{p.model_dump_json()}" for i, p in enumerate(partials, 1))
        draft = _call(client, model, temperature, [
            {"role": "system", "content": system},
            {"role": "user", "content": header + "Merge these partial minutes of one meeting into one set of minutes. "
             "Combine agenda items that continue across parts, keep every action item once.\n\n" + merged},
        ], "minutes-merge")
    return finalize(draft)


def finalize(draft: MinutesDraft) -> MinutesDraft:
    for item in draft.action_items:
        if item.due_date is None:
            remark = (item.remark or "").strip()
            if NO_DEADLINE_REMARK.lower() not in remark.lower():
                item.remark = f"{remark}; {NO_DEADLINE_REMARK}" if remark else NO_DEADLINE_REMARK
    return draft


def _language_name(code: str) -> str:
    return {"en": "English", "ms": "Malay", "zh": "Simplified Chinese"}.get(code, code)
