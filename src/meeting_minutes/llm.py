"""OpenAI-compatible client pointed at llm-gateway (this company's key)."""

from __future__ import annotations

from openai import OpenAI

from .settings import Settings


class LlmNotConfiguredError(RuntimeError):
    pass


def make_client(settings: Settings) -> OpenAI:
    if not settings.llm_base_url or not settings.llm_api_key:
        raise LlmNotConfiguredError("LLM_BASE_URL and LLM_API_KEY must be set (llm-gateway key for this company)")
    return OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, max_retries=4, timeout=300)


def metering(op: str, user: str | None = None) -> dict:
    """Gateway usage tags (see llm-gateway README)."""
    extra: dict = {"extra_body": {"metadata": {"tags": [f"op:{op}", "agent:meeting-minutes"]}}}
    if user:
        extra["user"] = user
    return extra
