"""Speech-to-text through llm-gateway's /audio/transcriptions (Ilmu ASR by default; model is configurable)."""

from __future__ import annotations

from typing import Protocol

from openai import OpenAI

from .company import CompanyConfig


class Transcriber(Protocol):
    def transcribe(self, wav: bytes, prompt: str | None = None) -> str: ...


class OpenAITranscriber:
    # Ilmu limits: 70 min / 25 MB per request. Segments are <= max_segment_seconds, far below that.
    def __init__(self, client: OpenAI, model: str, language: str | None = None):
        self.client, self.model, self.language = client, model, language

    def transcribe(self, wav: bytes, prompt: str | None = None) -> str:
        kwargs = {"model": self.model, "file": ("segment.wav", wav, "audio/wav"), "response_format": "json"}
        if self.language:
            kwargs["language"] = self.language
        if prompt:
            kwargs["prompt"] = prompt
        result = self.client.audio.transcriptions.create(**kwargs)
        return (getattr(result, "text", None) or "").strip()


def build_prompt(company: CompanyConfig, names: list[str]) -> str:
    parts = [f"Meeting at {company.company.name}. Mixed English, Malay, Mandarin and Cantonese."]
    if names:
        parts.append("Attendees: " + ", ".join(names) + ".")
    if company.asr.prompt_terms:
        parts.append("Terms: " + ", ".join(company.asr.prompt_terms) + ".")
    return " ".join(parts)[:900]
