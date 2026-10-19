"""Per-company configuration (company.yaml). Non-secret; secrets live in .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .settings import get_settings


class _Base(BaseModel):
    model_config = {"extra": "forbid"}


class CompanyInfo(_Base):
    id: str
    name: str
    timezone: str = "Asia/Kuala_Lumpur"
    erp_customer_code: str | None = None


class AsrConfig(_Base):
    model: str = "ilmu-asr-v4.2"
    language: str | None = None  # ISO-639-1 hint; leave empty for mixed-language meetings
    prompt_terms: list[str] = Field(default_factory=list)  # company jargon, product names
    vad: Literal["webrtc", "energy"] = "webrtc"
    max_segment_seconds: float = 30.0
    min_silence_seconds: float = 0.5
    concurrency: int = 4


class LlmConfig(_Base):
    model: str = "ilmu-claw"
    temperature: float = 0.2
    max_transcript_chars_per_call: int = 300_000


class VoiceConfig(_Base):
    diarization: Literal["ecapa", "none"] = "ecapa"
    voiceprints_allowed: bool = True
    voiceprints_default_on: bool = False
    cluster_threshold: float = 0.55  # cosine similarity to join a speaker cluster
    match_threshold: float = 0.60  # cosine similarity to auto-label a cluster with a saved voiceprint


class EmailConfig(_Base):
    enabled: bool = False


class WhatsAppConfig(_Base):
    enabled: bool = False
    # Business-initiated messages outside the 24h window must use an approved template.
    # Template body params: {{1}} = message title, {{2}} = link.
    template_name: str | None = None
    template_language: str = "en"


class TelegramConfig(_Base):
    enabled: bool = False
    bot_username: str | None = None


class NotifiersConfig(_Base):
    email: EmailConfig = Field(default_factory=EmailConfig)
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class RemindersConfig(_Base):
    enabled: bool = True
    days_before: list[int] = Field(default_factory=lambda: [2, 0])
    overdue_every_days: int = 2
    send_hour: int = 9  # local time


class TasksConfig(_Base):
    sink: str = "internal"
    reminders: RemindersConfig = Field(default_factory=RemindersConfig)


class RetentionConfig(_Base):
    transcript_days: int = 7
    minutes_days: int = 90
    tasks_days: int = 90


class AuthConfig(_Base):
    method: Literal["password", "otp", "password+otp"] = "password"
    remember_device: bool = True  # False = signed out when the app/browser is fully closed
    session_days: int = 90
    logout_on_app_update: bool = True
    otp_channels: list[Literal["email", "telegram", "whatsapp"]] = Field(
        default_factory=lambda: ["email", "telegram", "whatsapp"])


class ConnectorConfig(_Base):
    provider: str = "internal"


class CentralConfig(_Base):
    """Placeholder for the future central portal (agents catalogue, billing/plans)."""

    enabled: bool = False
    url: str | None = None


class CompanyConfig(_Base):
    company: CompanyInfo
    output_language: str = "en"
    asr: AsrConfig = Field(default_factory=AsrConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    directory: ConnectorConfig = Field(default_factory=ConnectorConfig)
    calendar: ConnectorConfig = Field(default_factory=lambda: ConnectorConfig(provider="none"))
    notifiers: NotifiersConfig = Field(default_factory=NotifiersConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    central: CentralConfig = Field(default_factory=CentralConfig)
    minutes_template: str | None = None  # path to a custom Jinja template


def load_company(path: Path) -> CompanyConfig:
    return CompanyConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@lru_cache
def get_company() -> CompanyConfig:
    return load_company(get_settings().company_config)
