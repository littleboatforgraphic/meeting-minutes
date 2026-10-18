"""Test doubles: synthetic audio, fake ASR/LLM/embedder/channels, and an isolated company environment."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
SR = 16000


def tone(freq: float, seconds: float, amp: int = 8000) -> bytes:
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SR * seconds)


def setup_env(tmp_path: Path, monkeypatch, **overrides) -> None:
    from meeting_minutes import company, db, settings

    cfg = yaml.safe_load((ROOT / "config/company.example.yaml").read_text())
    cfg["asr"]["vad"] = "energy"
    cfg["notifiers"]["whatsapp"]["enabled"] = True
    for dotted, value in overrides.items():
        node = cfg
        *path, last = dotted.split("__")
        for p in path:
            node = node.setdefault(p, {})
        node[last] = value
    (tmp_path / "company.yaml").write_text(yaml.safe_dump(cfg))
    env = {"MM_COMPANY_CONFIG": str(tmp_path / "company.yaml"), "MM_DATA_DIR": str(tmp_path / "data"),
           "MM_SECRET_KEY": "test-secret", "MM_PUBLIC_BASE_URL": "https://mm.example.com",
           "TELEGRAM_WEBHOOK_SECRET": "tg-secret", "WHATSAPP_VERIFY_TOKEN": "wa-verify",
           "WHATSAPP_APP_SECRET": "wa-secret", "CENTRAL_UI_TOKEN": "central-token"}
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "data").mkdir(exist_ok=True)
    reset_caches()
    db.init_db()
    _ = (company, settings)


def reset_caches() -> None:
    from meeting_minutes import company, db, settings

    try:
        db.get_engine().dispose()
    except Exception:
        pass
    for f in (settings.get_settings, company.get_company, db.get_engine, db._sessionmaker):
        f.cache_clear()


class FakeTranscriber:
    """Returns the scripted text for each speech segment in order."""

    def __init__(self, texts: list[str]):
        self.texts, self.calls = list(texts), []

    def transcribe(self, wav: bytes, prompt: str | None = None) -> str:
        self.calls.append(prompt)
        return self.texts[len(self.calls) - 1] if len(self.calls) <= len(self.texts) else "..."


class FakeEmbedder:
    """'Voice' = dominant frequency bucket, so tones of different pitch are different speakers."""

    def embed(self, pcm: bytes):
        x = np.frombuffer(pcm, "<i2").astype(float)
        if len(x) < SR // 2:
            return None
        freq = np.argmax(np.abs(np.fft.rfft(x))) * SR / len(x)
        v = np.zeros(16)
        v[int(freq // 100) % 16] = 1.0
        return v


class FakeLLM:
    def __init__(self, minutes: dict):
        self.minutes, self.calls = minutes, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.minutes)))])


class FakeChannel:
    def __init__(self, name: str):
        self.name, self.sent = name, []
        self.api = SimpleNamespace(sent=[], send_text=lambda chat_id, text, buttons=None: self.api.sent.append((chat_id, text)),
                                   call=lambda method, data=None, files=None: {},
                                   send=lambda payload: self.api.sent.append(payload))

    def can_reach(self, person) -> bool:
        return bool({"email": person.email, "telegram": person.telegram_chat_id, "whatsapp": person.phone}[self.name])

    def send(self, person, msg) -> None:
        self.sent.append((person.name, msg))


MINUTES = {
    "title": "Weekly production",
    "summary": "Reviewed output and the mesh order backlog.",
    "agenda": [{"title": "Production output", "discussion": ["Line 2 down 10%"], "decisions": ["Add a Saturday shift"]}],
    "action_items": [
        {"task": "Prepare the Saturday shift roster", "owner": "Mr Ali", "due_as_said": "next Friday",
         "due_date": "2026-10-23", "remark": None, "timestamp": "00:00:01"},
        {"task": "Follow up with the steel supplier", "owner": "Mei Ling", "due_as_said": None, "due_date": None,
         "remark": None, "timestamp": "00:00:05"},
    ],
    "other_business": [],
    "next_meeting": None,
}


def make_services(transcripts: list[str] | None = None, minutes: dict | None = None):
    from meeting_minutes.company import get_company
    from meeting_minutes.notify import Dispatcher
    from meeting_minutes.pipeline import Services
    from meeting_minutes.settings import get_settings

    channels = {n: FakeChannel(n) for n in ("email", "telegram", "whatsapp")}
    return Services(get_settings(), get_company(), _transcriber=FakeTranscriber(transcripts or []),
                    _embedder=FakeEmbedder(), _llm=FakeLLM(minutes or MINUTES), _dispatcher=Dispatcher(channels))


def two_speaker_audio() -> bytes:
    """Speaker A (300 Hz) 3 s, pause, speaker B (900 Hz) 3 s, pause."""
    return silence(0.5) + tone(300, 6) + silence(1.0) + tone(900, 6) + silence(1.0)
