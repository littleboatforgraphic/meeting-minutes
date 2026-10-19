import hashlib
import hmac
import json

import pytest
from fakes import make_services, two_speaker_audio
from fastapi.testclient import TestClient
from sqlalchemy import select

from meeting_minutes import auth, jobs
from meeting_minutes.db import Person, Task, session_scope
from meeting_minutes.web.app import create_app


def add_people():
    with session_scope() as db:
        ali = Person(name="Ali bin Ahmad", aliases=["Ali"], email="ali@acme.my", role="admin",
                     password_hash=auth.hash_password("ali-password"))
        mei = Person(name="Tan Mei Ling", aliases=["Mei Ling"], phone="60123456789",
                     password_hash=auth.hash_password("mei-password"))
        db.add_all([ali, mei])
        db.flush()
        return ali.id, mei.id


def client(svc):
    return TestClient(create_app(svc), base_url="https://testserver")


def login(c, ident, pw):
    r = c.post("/api/auth/login", json={"identifier": ident, "password": pw})
    assert r.status_code == 200, r.text
    return r


def drain(svc):
    while jobs.run_next(svc):
        pass


def test_record_approve_distribute_via_api(env):
    svc = make_services(["Saturday shift.", "Call supplier."])
    ali, mei = add_people()
    c = client(svc)
    assert c.get("/api/meetings").status_code == 401
    login(c, "ali@acme.my", "ali-password")
    assert c.get("/api/config").json()["auth_method"] == "password"
    m = c.post("/api/meetings", json={"title": "Weekly", "attendee_ids": [mei], "voiceprints_on": True}).json()
    assert m["chair_id"] == ali and m["voiceprints_on"] is True and sorted(m["attendee_ids"]) == [ali, mei]
    audio = two_speaker_audio()
    step = 16000 * 2 * 3
    for seq, i in enumerate(range(0, len(audio), step)):
        r = c.post(f"/api/meetings/{m['id']}/chunks?seq={seq}&ext=pcm", content=audio[i:i + step],
                   headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text
        drain(svc)
    assert c.get(f"/api/meetings/{m['id']}/live").json()["lines"][0]["text"] == "Saturday shift."
    c.post(f"/api/meetings/{m['id']}/stop")
    drain(svc)
    d = c.get(f"/api/meetings/{m['id']}").json()
    assert d["status"] == "draft" and d["can_manage"]
    assert len(d["transcript"]) == 2 and len(d["speakers"]) == 2

    mei_c = client(svc)
    login(mei_c, "012-3456789", "mei-password")
    assert mei_c.get(f"/api/meetings/{m['id']}").status_code == 200  # attendee can view
    assert mei_c.post(f"/api/meetings/{m['id']}/approve", json={}).status_code == 403

    minutes = d["minutes"]
    minutes["action_items"][1]["due_date"] = "2026-10-30"
    r = c.put(f"/api/meetings/{m['id']}/draft", json={"minutes": minutes, "speakers": {"S1": ali, "S2": mei}})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/meetings/{m['id']}/approve", json={}).status_code == 200
    drain(svc)
    assert c.get(f"/api/meetings/{m['id']}").json()["status"] == "distributed"
    assert c.get(f"/api/meetings/{m['id']}/minutes.pdf").content.startswith(b"%PDF")

    mine = mei_c.get("/api/tasks").json()
    assert [(t["title"], t["due_date"]) for t in mine] == [("Follow up with the steel supplier", "2026-10-30")]
    assert mei_c.patch(f"/api/tasks/{mine[0]['id']}", json={"status": "done"}).json()["status"] == "done"
    ali_task = c.get("/api/tasks").json()[0]["id"]
    assert mei_c.patch(f"/api/tasks/{ali_task}", json={"status": "done"}).status_code == 404


def test_wrong_password_and_logout(env):
    svc = make_services()
    add_people()
    c = client(svc)
    assert c.post("/api/auth/login", json={"identifier": "ali@acme.my", "password": "nope"}).status_code == 401
    login(c, "ali@acme.my", "ali-password")
    assert c.get("/api/me").status_code == 200
    c.post("/api/auth/logout")
    assert c.get("/api/me").status_code == 401


@pytest.mark.parametrize("method", ["otp", "password+otp"])
def test_otp_login(make_env, method):
    make_env(auth__method=method)
    svc = make_services()
    add_people()
    c = client(svc)
    body = {"identifier": "60123456789"} | ({"password": "mei-password"} if method == "password+otp" else {})
    r = c.post("/api/auth/login", json=body).json()
    assert r["ok"] is False
    sent = svc.dispatcher.channels["whatsapp"].sent[-1][1].text
    code = sent.split("code is ")[1][:6]
    assert c.post("/api/auth/otp", json={"challenge": r["otp_challenge"], "code": "000000" if code != "000000" else "111111"}).status_code == 401
    assert c.post("/api/auth/otp", json={"challenge": r["otp_challenge"], "code": code}).status_code == 200
    assert c.get("/api/me").json()["name"] == "Tan Mei Ling"


def test_session_dropped_after_app_update(env, monkeypatch):
    svc = make_services()
    add_people()
    c = client(svc)
    login(c, "ali@acme.my", "ali-password")
    monkeypatch.setattr(auth, "__version__", "9.9.9")
    assert c.get("/api/me").status_code == 401


def test_people_admin_and_import(env):
    svc = make_services()
    add_people()
    c = client(svc)
    login(c, "ali@acme.my", "ali-password")
    r = c.post("/api/people/import", files={"file": ("hr.csv", b"Name,Email,Phone\nLee Ah Kow,lee@acme.my,016-1112222\n")})
    assert r.json() == {"created": 1, "updated": 0}
    people = c.get("/api/people").json()
    lee = next(p for p in people if p["name"] == "Lee Ah Kow")
    assert lee["phone"] == "60161112222"
    url = c.post(f"/api/people/{lee['id']}/invite").json()["url"]
    token = url.split("/invite/")[1]
    c2 = client(svc)
    assert c2.post(f"/api/auth/invite/{token}", json={"password": "lee-password"}).status_code == 200
    assert c2.post("/api/people", json={"name": "X"}).status_code == 403
    assert c2.post(f"/api/auth/invite/{token}", json={"password": "lee-password"}).status_code == 400


def test_central_placeholder(make_env):
    make_env(central__enabled=True)
    c = client(make_services())
    assert c.get("/api/central/info").status_code == 404
    info = c.get("/api/central/info", headers={"Authorization": "Bearer central-token"}).json()
    assert info["agent"] == "meeting-minutes" and info["company_id"] == "acme"


def test_telegram_link_and_done(env):
    svc = make_services()
    ali, mei = add_people()
    c = client(svc)
    with session_scope() as db:
        token = auth.create_link_token(db, "test-secret", db.get(Person, mei), "telegram")
        db.add(Task(title="Call supplier", owner_id=mei))
    hook = "/webhooks/telegram"
    msg = lambda text: {"message": {"chat": {"id": 777, "type": "private"}, "from": {"username": "meiling"}, "text": text}}  # noqa: E731
    assert c.post(hook, json=msg("/start x")).status_code == 403
    h = {"X-Telegram-Bot-Api-Secret-Token": "tg-secret"}
    assert c.post(hook, json=msg(f"/start {token}"), headers=h).status_code == 200
    api = svc.dispatcher.channels["telegram"].api
    assert "Linked to Tan Mei Ling" in api.sent[-1][1]
    c.post(hook, json=msg("done 1"), headers=h)
    assert "Marked done" in api.sent[-1][1]
    with session_scope() as db:
        assert db.scalar(select(Task)).status == "done"
        assert db.get(Person, mei).telegram_chat_id == "777"


def test_whatsapp_signature_and_done(env):
    svc = make_services()
    ali, mei = add_people()
    with session_scope() as db:
        db.add(Task(title="Call supplier", owner_id=mei))
    c = client(svc)
    r = c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wa-verify", "hub.challenge": "42"})
    assert r.text == "42"
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": "60123456789", "type": "interactive", "interactive": {"button_reply": {"id": "task:1:done"}}}]}}]}]}).encode()
    assert c.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 403
    sig = "sha256=" + hmac.new(b"wa-secret", body, hashlib.sha256).hexdigest()
    assert c.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig,
                                                                "Content-Type": "application/json"}).status_code == 200
    with session_scope() as db:
        assert db.scalar(select(Task)).status == "done"


def test_static_app_served(env):
    c = client(make_services())
    assert "Meeting Minutes" in c.get("/").text
    assert c.get("/app.js").status_code == 200
