"""Deployment settings and secrets, read from the environment (.env of this company's stack)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    company_config: Path = field(default_factory=lambda: Path(_env("MM_COMPANY_CONFIG", "config/company.yaml")))
    data_dir: Path = field(default_factory=lambda: Path(_env("MM_DATA_DIR", "data")))
    database_url: str | None = field(default_factory=lambda: _env("DATABASE_URL"))
    secret_key: str = field(default_factory=lambda: _env("MM_SECRET_KEY", "dev-insecure-secret"))
    public_base_url: str = field(default_factory=lambda: _env("MM_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"))

    llm_base_url: str | None = field(default_factory=lambda: _env("LLM_BASE_URL"))
    llm_api_key: str | None = field(default_factory=lambda: _env("LLM_API_KEY"))

    smtp_host: str | None = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_user: str | None = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str | None = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from: str | None = field(default_factory=lambda: _env("SMTP_FROM"))
    smtp_security: str = field(default_factory=lambda: _env("SMTP_SECURITY", "starttls"))  # starttls | ssl | none

    telegram_bot_token: str | None = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_webhook_secret: str | None = field(default_factory=lambda: _env("TELEGRAM_WEBHOOK_SECRET"))

    whatsapp_token: str | None = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    whatsapp_phone_number_id: str | None = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    whatsapp_verify_token: str | None = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN"))
    whatsapp_app_secret: str | None = field(default_factory=lambda: _env("WHATSAPP_APP_SECRET"))

    central_ui_token: str | None = field(default_factory=lambda: _env("CENTRAL_UI_TOKEN"))

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'meeting-minutes.db'}"

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
