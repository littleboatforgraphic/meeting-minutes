from __future__ import annotations

import httpx

from ..db import Person
from . import Message

GRAPH = "https://graph.facebook.com/v21.0"


class WhatsAppAPI:
    def __init__(self, token: str, phone_number_id: str, client: httpx.Client | None = None):
        self.token, self.phone_id = token, phone_number_id
        self.http = client or httpx.Client(timeout=60, headers={"Authorization": f"Bearer {token}"})

    def send(self, payload: dict) -> dict:
        r = self.http.post(f"{GRAPH}/{self.phone_id}/messages", json={"messaging_product": "whatsapp", **payload})
        if r.status_code >= 400:
            raise RuntimeError(f"whatsapp send {r.status_code}: {r.text[:300]}")
        return r.json()

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        info = self.http.get(f"{GRAPH}/{media_id}")
        info.raise_for_status()
        meta = info.json()
        r = self.http.get(meta["url"])
        r.raise_for_status()
        return r.content, meta.get("mime_type", "application/octet-stream")


class WhatsAppChannel:
    """Text with a link to the app. Outside the 24h customer-service window Meta only delivers approved templates,
    so set notifiers.whatsapp.template_name (body params: {{1}} title, {{2}} link). Documents stay in the app/email."""

    name = "whatsapp"

    def __init__(self, token: str, phone_number_id: str, template_name: str | None, template_language: str,
                 api: WhatsAppAPI | None = None):
        self.api = api or WhatsAppAPI(token, phone_number_id)
        self.template_name, self.template_language = template_name, template_language

    def can_reach(self, person: Person) -> bool:
        return bool(person.phone)

    def send(self, person: Person, msg: Message) -> None:
        to = "".join(ch for ch in person.phone if ch.isdigit())
        if self.template_name:
            params = [{"type": "text", "text": msg.subject[:200]}, {"type": "text", "text": msg.link or msg.text[:900]}]
            self.api.send({"to": to, "type": "template", "template": {
                "name": self.template_name, "language": {"code": self.template_language},
                "components": [{"type": "body", "parameters": params}]}})
        else:
            body = f"*{msg.subject}*\n\n{msg.text}" + (f"\n\n{msg.link}" if msg.link else "")
            self.api.send({"to": to, "type": "text", "text": {"body": body[:4000], "preview_url": False}})
