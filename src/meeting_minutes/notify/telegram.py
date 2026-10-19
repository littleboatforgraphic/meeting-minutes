from __future__ import annotations

import html
import json

import httpx

from ..db import Person
from . import Message

API = "https://api.telegram.org"


class TelegramAPI:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.token = token
        self.http = client or httpx.Client(timeout=60)

    def call(self, method: str, data: dict | None = None, files: dict | None = None) -> dict:
        r = self.http.post(f"{API}/bot{self.token}/{method}", data=data, files=files)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body.get('description')}")
        return body["result"]

    def download(self, file_id: str) -> bytes:
        info = self.call("getFile", {"file_id": file_id})
        r = self.http.get(f"{API}/file/bot{self.token}/{info['file_path']}")
        r.raise_for_status()
        return r.content

    def send_text(self, chat_id: str, text: str, buttons: list[list[dict]] | None = None) -> None:
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if buttons:
            data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        self.call("sendMessage", data)


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, api: TelegramAPI | None = None):
        self.api = api or TelegramAPI(token)

    def can_reach(self, person: Person) -> bool:
        return bool(person.telegram_chat_id)

    def send(self, person: Person, msg: Message) -> None:
        text = f"<b>{html.escape(msg.subject)}</b>\n\n{html.escape(msg.text)}"[:4000]
        rows: list[list[dict]] = [[{"text": label, "callback_data": data}] for label, data in msg.buttons[:20]]
        if msg.link and msg.link.startswith("https://"):
            rows.append([{"text": msg.link_label, "url": msg.link}])
        elif msg.link:
            text += f"\n\n{html.escape(msg.link)}"
        self.api.send_text(person.telegram_chat_id, text, rows or None)
        for a in msg.attachments:
            self.api.call("sendDocument", {"chat_id": person.telegram_chat_id},
                          files={"document": (a.filename, a.content, a.mime)})
