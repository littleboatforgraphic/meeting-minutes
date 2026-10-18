"""Outbound notifications: email (company SMTP), WhatsApp (company number), Telegram (company bot)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from ..company import CompanyConfig
from ..db import Notification, Person
from ..settings import Settings

log = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime: str


@dataclass
class Message:
    subject: str
    text: str
    html: str | None = None
    link: str | None = None
    link_label: str = "Open"
    attachments: list[Attachment] = field(default_factory=list)
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, callback data) for chat buttons


class Channel(Protocol):
    name: str

    def can_reach(self, person: Person) -> bool: ...

    def send(self, person: Person, msg: Message) -> None: ...


class Dispatcher:
    """Email if the company has email; chat on WhatsApp if the company has a number, else Telegram."""

    def __init__(self, channels: dict[str, Channel]):
        self.channels = channels

    def chat_channel(self, person: Person) -> Channel | None:
        for name in ("whatsapp", "telegram"):
            ch = self.channels.get(name)
            if ch and ch.can_reach(person):
                return ch
        return None

    def targets(self, person: Person) -> list[Channel]:
        out = []
        email = self.channels.get("email")
        if email and email.can_reach(person):
            out.append(email)
        chat = self.chat_channel(person)
        if chat:
            out.append(chat)
        return out

    def send(self, db: Session, person: Person, msg: Message, kind: str, meeting_id: int | None = None,
             channels: list[Channel] | None = None) -> list[str]:
        sent = []
        for ch in channels if channels is not None else self.targets(person):
            try:
                ch.send(person, msg)
                db.add(Notification(person_id=person.id, channel=ch.name, kind=kind, meeting_id=meeting_id, status="sent"))
                sent.append(ch.name)
            except Exception as e:  # one failed channel must not stop the others
                log.warning("notify %s via %s failed: %s", person.id, ch.name, e)
                db.add(Notification(person_id=person.id, channel=ch.name, kind=kind, meeting_id=meeting_id,
                                    status="failed", error=str(e)[:1000]))
        return sent

    def send_first(self, db: Session, person: Person, msg: Message, kind: str, order: list[str]) -> str | None:
        """Send on the first channel in `order` that reaches the person (used for one-time codes)."""
        for name in order:
            ch = self.channels.get(name)
            if ch and ch.can_reach(person) and self.send(db, person, msg, kind, channels=[ch]):
                return name
        return None


def build_dispatcher(settings: Settings, company: CompanyConfig) -> Dispatcher:
    channels: dict[str, Channel] = {}
    n = company.notifiers
    if n.email.enabled and settings.smtp_host and settings.smtp_from:
        from .email_smtp import EmailChannel

        channels["email"] = EmailChannel(settings)
    if n.whatsapp.enabled and settings.whatsapp_token and settings.whatsapp_phone_number_id:
        from .whatsapp import WhatsAppChannel

        channels["whatsapp"] = WhatsAppChannel(settings.whatsapp_token, settings.whatsapp_phone_number_id,
                                               n.whatsapp.template_name, n.whatsapp.template_language)
    if n.telegram.enabled and settings.telegram_bot_token:
        from .telegram import TelegramChannel

        channels["telegram"] = TelegramChannel(settings.telegram_bot_token)
    return Dispatcher(channels)
