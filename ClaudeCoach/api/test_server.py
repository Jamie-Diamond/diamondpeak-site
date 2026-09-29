"""Tests for the Peak backend: who can read what. Run: pytest ClaudeCoach/api/test_server.py"""
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cc = tmp_path / "ClaudeCoach"
    (cc / "config").mkdir(parents=True)
    (cc / "athletes" / "jamie").mkdir(parents=True)
    (cc / "public").mkdir()
    app_dir = tmp_path / "coach"
    app_dir.mkdir()
    (app_dir / "app.html").write_text("<html>peak</html>")
    (cc / "config" / "athletes.json").write_text(json.dumps({
        "jamie": {"name": "Jamie Diamond", "active": True, "chat_id": "111"},
        "kathryn": {"name": "Kathryn X", "active": True, "chat_id": "222"},
        "old": {"name": "Old", "active": False},
    }))
    (cc / "config" / "pending.json").write_text("[]")
    monkeypatch.setattr(server, "PENDING_FILE", cc / "config" / "pending.json")
    monkeypatch.setattr(server.push, "SUBS_FILE", tmp_path / "subs.json")
    access = cc / "config" / "web-access.json"
    access.write_text(json.dumps({"users": {
        "Coach@Example.com": {"slug": "jamie", "coach": True},
        "kat@example.com": {"slug": "kathryn"},
    }}))
    (cc / "athletes" / "jamie" / "training-data.json").write_text('{"who":"jamie-private"}')
    (cc / "training-data-kathryn.json").write_text('{"who":"kathryn-private"}')
    (cc / "public" / "training-data-kathryn.json").write_text('{"who":"kathryn-PUBLIC"}')
    (cc / "public" / "session-library.json").write_text('{"lib":1}')
    monkeypatch.setattr(server, "CC", cc)
    monkeypatch.setattr(server, "APP_DIR", app_dir)
    monkeypatch.setattr(server, "PUBLIC_DIR", cc / "public")
    monkeypatch.setattr(server, "ATHLETES_CONFIG", cc / "config" / "athletes.json")
    monkeypatch.setattr(server, "ACCESS_CONFIG", access)
    monkeypatch.setenv("CC_PUSH_WATCH", "0")
    for k in ("CF_ACCESS_TEAM", "CF_ACCESS_AUD", "CC_API_DEV_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    return TestClient(server.app, follow_redirects=False)


def dev(monkeypatch, email):
    monkeypatch.setenv("CC_API_DEV_EMAIL", email)


# ── fail closed ──

def test_no_identity_mode_refuses_everything(env):
    assert env.get("/api/me").status_code == 503
    assert env.get("/ClaudeCoach/public/training-data-jamie.json").status_code == 503
    assert env.get("/coach/app.html").status_code == 503


def test_dev_mode_refuses_requests_that_came_through_cloudflare(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    r = env.get("/ClaudeCoach/public/training-data-kathryn.json", headers={"cf-ray": "abc"})
    assert r.status_code == 503


def test_unknown_email_sees_nothing(env, monkeypatch):
    dev(monkeypatch, "stranger@example.com")
    assert env.get("/api/me").status_code == 403
    assert env.get("/ClaudeCoach/public/training-data-jamie.json").status_code == 403


# ── who sees what ──

def test_athlete_sees_only_their_own_private_file(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    me = env.get("/api/me").json()
    assert me["athletes"] == [{"slug": "kathryn", "name": "Kathryn"}]
    r = env.get("/ClaudeCoach/public/training-data-kathryn.json")
    assert r.status_code == 200 and r.json() == {"who": "kathryn-private"}
    assert env.get("/ClaudeCoach/public/training-data-jamie.json").status_code == 403


def test_coach_sees_every_active_athlete_and_email_case_is_ignored(env, monkeypatch):
    dev(monkeypatch, "coach@example.com")
    slugs = [a["slug"] for a in env.get("/api/me").json()["athletes"]]
    assert slugs == ["jamie", "kathryn"]
    assert env.get("/ClaudeCoach/public/training-data-jamie.json").json() == {"who": "jamie-private"}
    assert env.get("/ClaudeCoach/public/training-data-old.json").status_code == 403


def test_path_tricks_are_refused(env, monkeypatch):
    dev(monkeypatch, "coach@example.com")
    assert env.get("/ClaudeCoach/public/training-data-..%2Fjamie.json").status_code in (403, 404)
    assert env.get("/coach/..%2F..%2FClaudeCoach%2Fconfig%2Fathletes.json").status_code == 404


def test_app_is_served_and_other_tools_redirect_to_the_public_site(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    assert env.get("/coach/app.html").text == "<html>peak</html>"
    assert env.get("/").headers["location"] == "/coach/app.html"
    r = env.get("/cycling/ftp-calculator.html")
    assert r.status_code == 307 and r.headers["location"] == "https://diamondpeak.uk/cycling/ftp-calculator.html"


# ── production: Cloudflare Access JWT ──

@pytest.fixture()
def cf(env, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("CF_ACCESS_TEAM", "team")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud123")
    monkeypatch.setenv("CC_API_DEV_EMAIL", "coach@example.com")  # must be ignored in production
    monkeypatch.setattr(server._jwks, "key_for", lambda token, team: key.public_key())

    def token(email="kat@example.com", aud="aud123", iss="https://team.cloudflareaccess.com", exp=3600):
        return jwt.encode({"email": email, "aud": aud, "iss": iss, "exp": int(time.time()) + exp},
                          key, algorithm="RS256")
    return env, token


def test_valid_access_token_identifies_the_athlete(cf):
    client, token = cf
    r = client.get("/api/me", headers={"cf-access-jwt-assertion": token()})
    assert r.status_code == 200 and r.json()["email"] == "kat@example.com"


def test_missing_wrong_audience_or_expired_token_is_refused(cf):
    client, token = cf
    assert client.get("/api/me").status_code == 401  # dev email is NOT a fallback
    for bad in (token(aud="other"), token(iss="https://evil.cloudflareaccess.com"), token(exp=-10)):
        assert client.get("/api/me", headers={"cf-access-jwt-assertion": bad}).status_code == 401


# ── pull-to-refresh ──

@pytest.fixture()
def refresh_env(env, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_last_refresh", {})
    monkeypatch.setattr(server, "run_refresh", lambda slug: calls.append(slug) or 0)
    return env, calls


def test_refresh_rebuilds_only_your_own_athlete(refresh_env, monkeypatch):
    client, calls = refresh_env
    dev(monkeypatch, "kat@example.com")
    assert client.post("/api/refresh/kathryn", headers={"x-peak": "1"}).status_code == 200
    assert client.post("/api/refresh/jamie", headers={"x-peak": "1"}).status_code == 403
    assert calls == ["kathryn"]


def test_refresh_needs_the_app_header_and_is_rate_limited(refresh_env, monkeypatch):
    client, calls = refresh_env
    dev(monkeypatch, "kat@example.com")
    assert client.post("/api/refresh/kathryn").status_code == 400
    assert client.post("/api/refresh/kathryn", headers={"x-peak": "1"}).status_code == 200
    assert client.post("/api/refresh/kathryn", headers={"x-peak": "1"}).status_code == 429
    assert calls == ["kathryn"]


def test_refresh_reports_busy_and_failure(refresh_env, monkeypatch):
    client, _ = refresh_env
    dev(monkeypatch, "coach@example.com")
    monkeypatch.setattr(server, "run_refresh", lambda slug: 3)
    assert client.post("/api/refresh/jamie", headers={"x-peak": "1"}).status_code == 409
    monkeypatch.setattr(server, "run_refresh", lambda slug: 1)
    assert client.post("/api/refresh/kathryn", headers={"x-peak": "1"}).status_code == 502


def test_nutrition_is_served_from_the_private_file(env, monkeypatch):
    dev(monkeypatch, "coach@example.com")
    assert env.get("/ClaudeCoach/public/nutrition-jamie.json").status_code == 404
    (server.CC / "athletes" / "jamie" / "nutrition-app.json").write_text('{"n":1}')
    assert env.get("/ClaudeCoach/public/nutrition-jamie.json").json() == {"n": 1}


# ── chat ──

class FakeSink:
    def __init__(self, events):
        import queue
        self.q = queue.Queue()
        self.consumer_gone = False
        for e in events:
            self.q.put(e if len(e) == 3 else (e[0], e[1], {}))


def fake_turns(monkeypatch, events=(("done", ""),)):
    seen = []
    monkeypatch.setattr(server.chat, "start_turn",
                        lambda cid, label="", **k: seen.append((cid, sorted(k), k.get("text") or k.get("button")))
                        or FakeSink(list(events)))
    return seen


def test_chat_talks_as_yourself_even_when_coach(env, monkeypatch):
    dev(monkeypatch, "coach@example.com")
    seen = fake_turns(monkeypatch, [("status", "Thinking…"), ("draft", "Hel"),
                                    ("message", "Hello", {"buttons": [[{"text": "OK", "data": "x"}]]}),
                                    ("done", "")])
    r = env.post("/api/chat", json={"text": "hi"}, headers={"x-peak": "1"})
    assert r.status_code == 200 and seen == [("111", ["text"], "hi")]
    body = r.text
    assert "event: status" in body and "event: draft" in body and '"buttons"' in body
    assert body.index("event: message") < body.index("event: done")


def test_chat_refuses_without_header_empty_text_or_unknown_user(env, monkeypatch):
    fake_turns(monkeypatch)
    dev(monkeypatch, "kat@example.com")
    assert env.post("/api/chat", json={"text": "hi"}).status_code == 400
    assert env.post("/api/chat", json={"text": "  "}, headers={"x-peak": "1"}).status_code == 400
    dev(monkeypatch, "stranger@example.com")
    assert env.post("/api/chat", json={"text": "hi"}, headers={"x-peak": "1"}).status_code == 403


def test_chat_busy_is_a_409(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    def busy(cid, label="", **k):
        raise server.chat.Busy(cid)
    monkeypatch.setattr(server.chat, "start_turn", busy)
    assert env.post("/api/chat", json={"text": "hi"}, headers={"x-peak": "1"}).status_code == 409


def test_voice_photo_and_buttons_reach_the_turn_as_yourself(env, monkeypatch):
    dev(monkeypatch, "coach@example.com")
    seen = fake_turns(monkeypatch)
    audio = b"x" * 2000
    h = {"x-peak": "1"}
    assert env.post("/api/chat/voice", content=audio, headers=h).status_code == 200
    assert env.post("/api/chat/photo?caption=lunch", content=audio, headers=h).status_code == 200
    assert env.post("/api/chat/button", json={"data": "r:1:jamie:7"}, headers=h).status_code == 200
    assert seen == [("111", ["audio"], None), ("111", ["image", "text"], "lunch"),
                    ("111", ["button"], "r:1:jamie:7")]


def test_uploads_are_checked(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    fake_turns(monkeypatch)
    assert env.post("/api/chat/voice", content=b"x" * 2000).status_code == 400      # no header
    assert env.post("/api/chat/voice", content=b"x", headers={"x-peak": "1"}).status_code == 400
    big = b"x" * (13 * 1024 * 1024)
    assert env.post("/api/chat/photo", content=big, headers={"x-peak": "1"}).status_code == 413


def test_timeline_merges_scheduled_messages_and_drops_their_history_copy(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    monkeypatch.setattr(server.chat, "CC", server.CC)
    a = server.CC / "athletes" / "kathryn"
    (a / "telegram").mkdir(parents=True)
    (a / "telegram" / "history.json").write_text(json.dumps([
        {"user": "q", "assistant": "a", "ts": "2026-09-29T10:00:00"},
        {"user": "", "assistant": "Morning card"},
        {"user": "q2", "assistant": "a2", "ts": "2026-09-29T12:00:00"}]))
    (a / "web-outbox.jsonl").write_text(json.dumps(
        {"id": "x1", "ts": "2026-09-29T11:00:00", "text": "Morning card",
         "buttons": [[{"text": "RPE 7", "data": "r:1:kathryn:7"}]], "photo": None}) + "\n")
    r = env.get("/api/chat/history").json()
    texts = [(m["who"], m["text"]) for m in r["history"]]
    assert texts == [("me", "q"), ("coach", "a"), ("coach", "Morning card"), ("me", "q2"), ("coach", "a2")]
    assert r["history"][2]["buttons"][0][0]["data"] == "r:1:kathryn:7"


def test_invited_athlete_signs_up_through_chat_then_waits_for_approval(env, monkeypatch):
    dev(monkeypatch, "coach@example.com")
    h = {"x-peak": "1"}
    r = env.post("/api/admin/invite", json={"email": "New@Example.com"}, headers=h)
    assert r.status_code == 200
    cid = r.json()["chat_id"]
    assert cid.startswith("web-") and json.loads(server.PENDING_FILE.read_text()) == [cid]
    assert env.post("/api/admin/invite", json={"email": "new@example.com"}, headers=h).status_code == 409

    dev(monkeypatch, "new@example.com")
    me = env.get("/api/me").json()
    assert me["state"] == "onboarding" and me["athletes"] == []
    seen = fake_turns(monkeypatch)
    assert env.post("/api/chat", json={"text": "hi"}, headers=h).status_code == 200
    assert seen == [(cid, ["text"], "hi")]
    assert env.post("/api/chat/button", json={"data": "x"}, headers=h).status_code == 409

    # the bot's onboarding finished: an inactive athlete with that chat id
    ath = json.loads(server.ATHLETES_CONFIG.read_text())
    ath["newbie"] = {"name": "New Person", "active": False, "chat_id": cid}
    server.ATHLETES_CONFIG.write_text(json.dumps(ath))
    assert env.get("/api/me").json()["state"] == "waiting"
    assert env.post("/api/chat", json={"text": "hi"}, headers=h).status_code == 409

    ath["newbie"]["active"] = True
    server.ATHLETES_CONFIG.write_text(json.dumps(ath))
    me = env.get("/api/me").json()
    assert me["state"] == "active" and me["own"] == "newbie"
    assert [a["slug"] for a in me["athletes"]] == ["newbie"]


def test_admin_is_coach_only_and_switches_telegram(env, monkeypatch):
    h = {"x-peak": "1"}
    dev(monkeypatch, "kat@example.com")
    assert env.get("/api/admin/athletes").status_code == 403
    assert env.post("/api/admin/telegram", json={"slug": "kathryn", "on": False}, headers=h).status_code == 403
    dev(monkeypatch, "coach@example.com")
    assert env.post("/api/admin/telegram", json={"slug": "kathryn", "on": False}, headers=h).status_code == 200
    assert json.loads(server.ATHLETES_CONFIG.read_text())["kathryn"]["telegram"] is False
    rows = {a["slug"]: a for a in env.get("/api/admin/athletes").json()["athletes"]}
    assert rows["kathryn"]["telegram"] is False and rows["kathryn"]["emails"] == ["kat@example.com"]
    assert env.post("/api/admin/telegram", json={"slug": "kathryn", "on": True}, headers=h).status_code == 200
    assert "telegram" not in json.loads(server.ATHLETES_CONFIG.read_text())["kathryn"]


def test_admin_link_gives_an_existing_athlete_web_access(env, monkeypatch):
    h = {"x-peak": "1"}
    dev(monkeypatch, "coach@example.com")
    assert env.post("/api/admin/link", json={"email": "k2@example.com", "slug": "kathryn"},
                    headers=h).status_code == 200
    dev(monkeypatch, "k2@example.com")
    me = env.get("/api/me").json()
    assert me["own"] == "kathryn" and me["state"] == "active" and not me["coach"]


def test_media_is_your_own_only(env, monkeypatch):
    m = server.CC / "athletes" / "kathryn" / "web-media"
    m.mkdir(parents=True)
    (m / "20260929-abc123.png").write_bytes(b"png")
    dev(monkeypatch, "kat@example.com")
    assert env.get("/api/media/20260929-abc123.png").content == b"png"
    assert env.get("/api/media/..%2F..%2Fx.png").status_code == 404
    dev(monkeypatch, "coach@example.com")                      # jamie's chat, not kathryn's
    assert env.get("/api/media/20260929-abc123.png").status_code == 404


def test_sink_turns_telegram_calls_into_events():
    import chat
    s = chat.Sink()
    ph = s.handle("sendMessage", {"text": "…", "disable_notification": True})["result"]["message_id"]
    s.handle("editMessageText", {"message_id": ph, "text": "Checking intervals.icu..."})
    s.handle("sendChatAction", {"action": "typing"})
    s.handle("sendMessage", {"text": "Your reply"})
    got = [s.q.get_nowait() for _ in range(s.q.qsize())]
    assert [(k, t) for k, t, _ in got] == [("status", "Thinking…"), ("status", "Checking intervals.icu..."),
                                           ("message", "Your reply")]
