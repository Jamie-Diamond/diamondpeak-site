"""Tests for the Peak backend: who can read what.

Run with the API's own Python (the system python3 has no fastapi):
    /opt/claudecoach-api/venv/bin/python3 -m pytest ClaudeCoach/api
"""
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
    # The chat archive resolves athletes/ itself: without this, a test run on the VM
    # writes its fake messages into the real athletes' chats (4 Oct 2026).
    import chat_archive                     # lib/, on sys.path once server is imported
    monkeypatch.setattr(chat_archive, "CC", cc)
    monkeypatch.setattr(server.dev_chat, "CC", cc)
    server.chat.bot()                       # imported from the real tree, then cached
    monkeypatch.setattr(server.chat, "CC", cc)
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
    sub = {"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}}
    assert env.post("/api/push/subscribe", json={"subscription": sub},
                    headers={"x-peak": "1"}).status_code == 403
    assert env.post("/api/chat", json={"text": "hi"}, headers={"x-peak": "1"}).status_code == 403


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
    assert env.post("/api/chat/button", json={"data": "r:1:jamie:7", "item": "20260930073200-abc123"},
                    headers=h).status_code == 200
    assert env.post("/api/chat/button", json={"data": "x", "item": "../../etc"}, headers=h).status_code == 200
    assert seen == [("111", ["audio"], None), ("111", ["image", "text"], "lunch"),
                    ("111", ["button", "item"], "r:1:jamie:7"), ("111", ["button", "item"], "x")]


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
    # ...but the sign-up questions' own buttons work
    assert env.post("/api/chat/button", json={"data": "ob:icu_has:no"}, headers=h).status_code == 200
    assert seen[-1] == (cid, ["button", "item"], "ob:icu_has:no")

    # the sign-up chat (kept by lib/outbox.py) survives a reload, screenshots included
    import outbox
    monkeypatch.setattr(outbox, "ATHLETES_CONFIG", server.ATHLETES_CONFIG)
    monkeypatch.setattr(outbox, "SIGNUP_DIR", server.CC / "config" / "web-signup")
    outbox._CACHE.update(mtime=None, data={})
    outbox.record(cid, "Do you already use *Intervals.icu*?")
    outbox.record(cid, "", photo=b"PNG")
    outbox.record_answer(cid, "No, not yet")
    hist = env.get("/api/chat/history").json()["history"]
    assert [(i["who"], i["text"]) for i in hist] == [
        ("coach", "Do you already use *Intervals.icu*?"), ("coach", ""), ("me", "No, not yet")]
    assert env.get(f"/api/media/{hist[1]['photo']}").content == b"PNG"

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


def test_a_tapped_message_is_edited_in_place_and_saved(tmp_path, monkeypatch):
    import chat
    s = chat.Sink()
    s.chat_id, s.edit_msg_id, s.edit_item = "111", chat._TAPPED_MSG_ID, "20260930073200-abc123"
    saved = []
    import outbox
    monkeypatch.setattr(outbox, "update", lambda *a: saved.append(a) or True)
    kb = {"inline_keyboard": [[{"text": "📊 Intervals", "callback_data": "drill:intervals:1:jamie"}]]}
    s.handle("editMessageText", {"message_id": chat._TAPPED_MSG_ID, "text": "✓ Pain 0/10 logged",
                                 "reply_markup": kb})
    kind, text, extra = s.q.get_nowait()
    assert (kind, text, extra["item"]) == ("edit", "✓ Pain 0/10 logged", "20260930073200-abc123")
    assert extra["buttons"][0][0]["data"] == "drill:intervals:1:jamie"
    assert saved and saved[0][2] == "✓ Pain 0/10 logged"


def test_timeline_hides_a_buttons_own_question_and_edited_duplicates(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    monkeypatch.setattr(server.chat, "CC", server.CC)
    a = server.CC / "athletes" / "kathryn"
    (a / "telegram").mkdir(parents=True)
    (a / "telegram" / "history.json").write_text(json.dumps([
        {"user": "", "assistant": "Injury pain during (0-10)", "ts": "2026-09-30T07:00:00"},
        {"user": "Analyse the interval structure of activity i1. Fetch...", "assistant": "4x5 at 300W",
         "ts": "2026-09-30T07:33:00"},
        {"user": "Find the 3 most similar past sessions to activity i1", "assistant": "Faster than last week",
         "ts": "2026-09-30T07:34:00", "kind": "drill"}]))
    (a / "web-outbox.jsonl").write_text(json.dumps(
        {"id": "20260930070000-aaaaaa", "ts": "2026-09-30T07:00:00", "text": "✓ Pain 0/10 logged",
         "orig_text": "Injury pain during (0-10)", "buttons": []}) + "\n")
    items = [(m["who"], m["text"]) for m in env.get("/api/chat/history").json()["history"]]
    assert items == [("coach", "✓ Pain 0/10 logged"), ("coach", "4x5 at 300W"), ("coach", "Faster than last week")]


# ── Log it card ──

def test_log_form_reads_the_quick_log_buttons_and_always_offers_rpe():
    import chat
    entry = {"buttons": [[{"text": str(i), "data": f"p:i9:kathryn:{i}"} for i in range(6)],
                         [{"text": "60g/hr", "data": "c:i9:kathryn:60"}],
                         [{"text": "📊 Intervals", "data": "drill:intervals:i9:kathryn"}]]}
    assert chat.log_form(entry) == {"activity": "i9", "slug": "kathryn", "fields": ["r", "p", "c"]}
    assert chat.log_form({"buttons": [[{"text": "x", "data": "drill:hr:i9:k"}]]}) is None
    assert chat.log_form({"form": {"activity": "a"}}) == {"activity": "a"}


def test_log_session_saves_through_the_bots_handler_carbs_first(tmp_path, monkeypatch):
    import chat, outbox
    cfg = tmp_path / "athletes.json"
    cfg.write_text(json.dumps({"kathryn": {"chat_id": "222"}}))
    monkeypatch.setattr(outbox, "BASE", tmp_path)
    monkeypatch.setattr(outbox, "ATHLETES_CONFIG", cfg)
    outbox._CACHE.update(mtime=None, data={})
    kb = {"inline_keyboard": [[{"text": "RPE 7", "callback_data": "r:i9:kathryn:7"}],
                              [{"text": "60", "callback_data": "c:i9:kathryn:60"}]]}
    item = outbox.record("222", "Quick log — Ride:", kb)
    calls = []

    class FakeBot:
        def load_config(self): return {"bot_token": "t"}
        def load_athletes(self): return {"222": {"slug": "kathryn"}}
        def log(self, m): pass
        def _handle_quick_log(self, token, cid, data, mid, athletes):
            calls.append(data)
            return True

    monkeypatch.setattr(chat, "bot", lambda: FakeBot())
    res = chat.log_session("222", item, {"r": 8, "c": 70, "p": 3})
    assert calls == ["c:i9:kathryn:70", "r:i9:kathryn:8"]            # p wasn't on the card
    assert res["text"] == "✓ Logged: RPE 8 · 70 g/hr carbs"
    saved = outbox.read("kathryn")[0]
    assert saved["logged"] == {"c": 70, "r": 8} and saved["form"]["fields"] == ["r", "c"]
    assert [b["data"].split(":")[1] for b in saved["buttons"][0]] == ["intervals", "nutrition", "hr", "compare"]
    assert chat.log_form(saved) == saved["form"]                        # still editable after save


def test_chat_log_endpoint_validates(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    h = {"x-peak": "1"}
    got = []
    monkeypatch.setattr(server.chat, "log_session", lambda cid, item, v: got.append((cid, item, v)) or {"text": "ok"})
    ok_item = "20260930073229-4e264d"
    assert env.post("/api/chat/log", json={"item": "bad", "values": {"r": 5}}, headers=h).status_code == 400
    assert env.post("/api/chat/log", json={"item": ok_item, "values": {"r": 11}}, headers=h).status_code == 400
    assert env.post("/api/chat/log", json={"item": ok_item, "values": {}}, headers=h).status_code == 400
    assert env.post("/api/chat/log", json={"item": ok_item, "values": {"r": 7, "x": 1}}, headers=h).status_code == 200
    assert got == [("222", ok_item, {"r": 7})]


# ── Connect Strava (lib/strava_link.py) ──

@pytest.fixture()
def strava(env, monkeypatch):
    import outbox
    import strava_link
    cc = server.CC
    app_cfg = cc / "config" / "strava_app.json"
    app_cfg.write_text(json.dumps({"client_id": 247758, "client_secret": "s3cret"}))
    monkeypatch.setattr(outbox, "BASE", cc)
    monkeypatch.setattr(outbox, "ATHLETES_CONFIG", server.ATHLETES_CONFIG)
    monkeypatch.setattr(outbox, "SIGNUP_DIR", cc / "config" / "web-signup")
    outbox._CACHE.update(mtime=None, data={})
    monkeypatch.setattr(strava_link, "BASE", cc)
    monkeypatch.setattr(strava_link, "APP_CONFIG", app_cfg)
    got = []
    monkeypatch.setattr(strava_link, "exchange", lambda code: got.append(code) or {
        "access_token": "a", "refresh_token": "r", "expires_at": 9, "athlete": {"id": 5}})
    return strava_link, outbox, got


def test_connect_strava_signing_up_then_copying_on(env, strava, monkeypatch):
    strava_link, outbox, got = strava
    h = {"x-peak": "1"}
    dev(monkeypatch, "coach@example.com")
    cid = env.post("/api/admin/invite", json={"email": "robin@example.com"}, headers=h).json()["chat_id"]
    dev(monkeypatch, "robin@example.com")
    r = env.get("/api/strava/connect")
    assert r.status_code in (302, 307) and r.headers["location"].startswith("https://www.strava.com/oauth/authorize")
    state = r.headers["location"].split("state=")[1]
    assert "activity%3Aread_all" in r.headers["location"]

    assert env.get("/api/strava/callback", params={"state": "web-x.1.zz", "code": "c",
                                                   "scope": "read,activity:read_all"}).status_code == 400
    assert "One more tick" in env.get("/api/strava/callback", params={
        "state": state.replace("%2E", "."), "code": "c", "scope": "read"}).text
    r = env.get("/api/strava/callback", params={"state": state.replace("%2E", "."), "code": "c",
                                                "scope": "read,activity:read_all"})
    assert r.status_code == 200 and "Strava connected" in r.text and got == ["c"]
    tok = json.loads((server.CC / "config" / "web-signup" / cid / "strava_tokens.json").read_text())
    assert tok["refresh_token"] == "r" and tok["strava_athlete_id"] == 5
    assert "Check again" in outbox.read_chat(cid)[-1]["text"]
    assert env.get("/api/me").json()["strava"] == "copying"


def test_connect_strava_never_replaces_jamies_own_link_and_respects_the_cap(env, strava, monkeypatch):
    strava_link, outbox, got = strava
    (server.CC / "athletes" / "jamie" / "strava_tokens.json").write_text("{}")
    dev(monkeypatch, "coach@example.com")
    assert env.get("/api/me").json()["strava"] == "linked"
    assert "already connected" in env.get("/api/strava/connect").text

    dev(monkeypatch, "kat@example.com")
    monkeypatch.setattr(strava_link, "CAPACITY", 1)
    assert "Strava is full" in env.get("/api/strava/connect").text
    monkeypatch.setattr(strava_link, "CAPACITY", 10)
    loc = env.get("/api/strava/connect").headers["location"]
    state = loc.split("state=")[1].replace("%2E", ".")
    env.get("/api/strava/callback", params={"state": state, "code": "k", "scope": "activity:read_all"})
    assert json.loads(server.ATHLETES_CONFIG.read_text())["kathryn"]["strava_bridge"] is True
    assert (server.CC / "athletes" / "kathryn" / "strava_tokens.json").exists()


def test_intervals_status_is_checked_and_cached(env, monkeypatch):
    ath = json.loads(server.ATHLETES_CONFIG.read_text())
    ath["kathryn"].update(icu_api_key="k1", icu_athlete_id="i22")
    server.ATHLETES_CONFIG.write_text(json.dumps(ath))
    server._ICU_STATUS.clear()
    calls = []
    monkeypatch.setattr(server, "_icu_check", lambda aid, key: calls.append(key) or
                        ("ok" if key == "k1" else "rejected"))
    dev(monkeypatch, "kat@example.com")
    assert env.get("/api/icu/status").json() == {"state": "ok"}
    assert env.get("/api/icu/status").json() == {"state": "ok"} and calls == ["k1"]   # cached
    ath["kathryn"]["icu_api_key"] = "k2"                 # key changed: checked again at once
    server.ATHLETES_CONFIG.write_text(json.dumps(ath))
    assert env.get("/api/icu/status").json() == {"state": "rejected"} and calls == ["k1", "k2"]
    dev(monkeypatch, "coach@example.com")                # jamie has no key in this fixture
    assert env.get("/api/icu/status").json() == {"state": "missing"}


def test_settings_follow_the_athlete_on_screen(env, monkeypatch):
    import coaching_prefs
    import planning_pause
    monkeypatch.setattr(coaching_prefs, "BASE", server.CC)
    monkeypatch.setattr(planning_pause, "BASE", server.CC)
    monkeypatch.setattr(server, "_icu_check", lambda aid, key: "ok")
    (server.CC / "athletes" / "kathryn").mkdir(parents=True, exist_ok=True)
    (server.CC / "athletes" / "kathryn" / "profile.json").write_text("{}")
    (server.CC / "athletes" / "jamie" / "strava_tokens.json").write_text("{}")
    h = {"x-peak": "1"}
    dev(monkeypatch, "coach@example.com")
    kat = env.get("/api/settings/kathryn").json()
    assert kat["strava"] is None and kat["prefs"] == {"heat": True, "fuelling": True}
    assert env.get("/api/settings/jamie").json()["strava"] == "linked"
    r = env.post("/api/settings/kathryn", json={"fuelling": False, "tracking_only": True}, headers=h).json()
    assert r["prefs"]["fuelling"] is False and r["tracking_only"] is True
    assert json.loads((server.CC / "athletes" / "kathryn" / "profile.json").read_text())["fuelling_coaching"] is False
    assert json.loads(server.ATHLETES_CONFIG.read_text())["kathryn"]["planning_paused"] is True

    dev(monkeypatch, "kat@example.com")
    assert env.get("/api/settings/jamie").status_code == 403
    assert env.post("/api/settings/kathryn", json={"heat": False}, headers=h).json()["prefs"]["heat"] is False
    r = env.post("/api/settings/kathryn", json={"tracking_only": False}, headers=h)
    assert r.status_code == 200 and r.json()["tracking_only"] is False


def test_disconnect_strava_only_for_a_peak_link(env, monkeypatch):
    import coaching_prefs
    import planning_pause
    import strava_link
    monkeypatch.setattr(coaching_prefs, "BASE", server.CC)
    monkeypatch.setattr(planning_pause, "BASE", server.CC)
    monkeypatch.setattr(server, "_icu_check", lambda aid, key: "ok")
    monkeypatch.setattr(strava_link, "BASE", server.CC)
    revoked = []
    monkeypatch.setattr(strava_link, "disconnect", lambda slug: revoked.append(slug) or
                        (server.CC / "athletes" / slug / "strava_tokens.json").unlink() or True)
    h = {"x-peak": "1"}
    (server.CC / "athletes" / "kathryn").mkdir(parents=True, exist_ok=True)
    (server.CC / "athletes" / "kathryn" / "strava_tokens.json").write_text("{}")
    (server.CC / "athletes" / "jamie" / "strava_tokens.json").write_text("{}")
    ath = json.loads(server.ATHLETES_CONFIG.read_text())
    ath["kathryn"]["strava_bridge"] = True
    server.ATHLETES_CONFIG.write_text(json.dumps(ath))

    dev(monkeypatch, "coach@example.com")
    assert env.post("/api/strava/disconnect/jamie", headers=h).status_code == 409   # his own link
    dev(monkeypatch, "kat@example.com")
    r = env.post("/api/strava/disconnect/kathryn", headers=h)
    assert r.status_code == 200 and r.json()["strava"] is None and revoked == ["kathryn"]
    assert "strava_bridge" not in json.loads(server.ATHLETES_CONFIG.read_text())["kathryn"]


def test_a_signup_chat_keeps_each_coach_message_once(env, monkeypatch):
    """Sign-up messages are kept as they are sent (chat._keep_signup); the end-of-turn
    keeper must not add them again (30 Sep 2026: every sign-up text showed twice)."""
    import outbox
    kept = []
    monkeypatch.setattr(outbox, "signing_up", lambda cid: True)
    monkeypatch.setattr(outbox, "record", lambda cid, text, *a, **k: kept.append(text))
    monkeypatch.setattr(server.chat, "_history_texts", lambda cid: ([], []))
    sink = server.chat.Sink()
    sink.chat_id = "web-0a1b2c3d4e"
    sink.handle("sendMessage", {"chat_id": sink.chat_id, "text": "What's your target race?"})
    server.chat._keep_unsaved(sink.chat_id, sink, [])
    assert kept == ["What's your target race?"]


def test_a_telegram_reply_shows_once_in_peak(env, monkeypatch):
    """history.json keeps a Telegram reply without its footer, the outbox with it: one
    bubble. A same-text reply from another day is not hidden."""
    adir = server.CC / "athletes" / "jamie"
    (adir / "telegram").mkdir(parents=True, exist_ok=True)
    (adir / "telegram" / "history.json").write_text(json.dumps([
        {"user": "Logged it?", "assistant": "Logged.", "ts": "2026-09-28T08:00:00"},
        {"user": "receipt", "assistant": "That's an M&S receipt.", "ts": "2026-09-30T21:32:30",
         "kind": "image"},
        {"user": "and today?", "assistant": "Logged.", "ts": "2026-09-30T21:40:00"}]))
    (adir / "web-outbox.jsonl").write_text(
        json.dumps({"id": "20260930213230-aaaaaa", "ts": "2026-09-30T21:32:30", "source": "bot",
                    "text": "That's an M&S receipt.\n_45 days to 5k PB · O5.5_"}) + "\n" +
        json.dumps({"id": "20260930214000-bbbbbb", "ts": "2026-09-30T21:40:01", "source": "bot",
                    "text": "Logged.\n_45 days to 5k PB · O5.5_"}) + "\n")
    monkeypatch.setattr(server.chat, "CC", server.CC)
    coach = [i["text"] for i in server.chat.timeline("jamie") if i["who"] == "coach"]
    assert coach.count("Logged.") == 1                   # the 28 Sep one, from history
    assert sum("M&S" in t for t in coach) == 1
    assert sum(t.startswith("Logged.\n_") for t in coach) == 1


def test_delete_a_message_and_the_coach_reads_an_athletes_chat(env, monkeypatch):
    import contextlib
    import outbox
    adir = server.CC / "athletes" / "kathryn"
    (adir / "telegram").mkdir(parents=True, exist_ok=True)
    (adir / "telegram" / "history.json").write_text(json.dumps([
        {"user": "Test", "assistant": "Got it. Test received.", "ts": "2026-09-30T06:20:00"},
        {"user": "how was my run?", "assistant": "Solid.", "ts": "2026-09-30T09:00:00"}]))
    (adir / "web-outbox.jsonl").write_text(json.dumps(
        {"id": "20260930070000-abcdef", "ts": "2026-09-30T07:00:00", "text": "Morning card"}) + "\n")
    (adir / ".chat_session.json").write_text("{}")
    monkeypatch.setattr(server.chat, "CC", server.CC)
    monkeypatch.setattr(outbox, "BASE", server.CC)
    monkeypatch.setattr(outbox, "ATHLETES_CONFIG", server.ATHLETES_CONFIG)
    outbox._CACHE.update(mtime=None, data={})

    class B:
        @staticmethod
        def _chat_lock(cid):
            return contextlib.nullcontext()
    monkeypatch.setattr(server.chat, "bot", lambda: B)
    h = {"x-peak": "1"}

    dev(monkeypatch, "coach@example.com")                     # Jamie viewing Kathryn: read only
    r = env.get("/api/chat/history", params={"slug": "kathryn"}).json()
    assert r["readonly"] is True and [i["text"] for i in r["history"]][:2] == ["Test", "Got it. Test received."]
    dev(monkeypatch, "kat@example.com")
    assert env.get("/api/chat/history", params={"slug": "jamie"}).status_code == 403

    hist = env.get("/api/chat/history").json()["history"]
    keys = {i["text"]: i["key"] for i in hist}
    for t in ("Test", "Got it. Test received.", "Morning card"):
        assert env.post("/api/chat/delete", json={"key": keys[t]}, headers=h).status_code == 200
    left = [i["text"] for i in env.get("/api/chat/history").json()["history"]]
    assert left == ["how was my run?", "Solid."]
    logged = [json.loads(l) for l in (adir / "deleted-messages.jsonl").read_text().splitlines()]
    assert [(d["who"], d["text"]) for d in logged] == [
        ("me", "Test"), ("coach", "Got it. Test received."), ("coach", "Morning card")]
    assert all(d["by"] == "kat@example.com" and d["deleted_at"] for d in logged)
    assert not (adir / ".chat_session.json").exists()          # the coach restarts without them
    import engine
    lines = engine.render_history(json.loads((adir / "telegram" / "history.json").read_text()), "Kathryn")
    assert not any("Test" in l for l in lines) and any("how was my run?" in l for l in lines)
    assert env.post("/api/chat/delete", json={"key": "o:20260930070000-abcdef"}, headers=h).status_code == 200
    assert env.post("/api/chat/delete", json={"key": "../x"}, headers=h).status_code == 400


def test_food_chat_is_only_for_athletes_with_food_tracking(env, monkeypatch):
    seen = []
    monkeypatch.setattr(server.food, "enabled", lambda slug: slug == "jamie")
    monkeypatch.setattr(server.food, "history", lambda slug: [{"who": "me", "text": "eggs"}])
    monkeypatch.setattr(server.food, "start_turn", lambda slug, **k: seen.append((slug, k)) or FakeSink([("done", "")]))
    h = {"x-peak": "1"}
    dev(monkeypatch, "kat@example.com")
    assert env.get("/api/food/history").json() == {"enabled": False, "history": []}
    assert env.post("/api/food/chat", json={"text": "eggs"}, headers=h).status_code == 403
    dev(monkeypatch, "coach@example.com")
    assert env.get("/api/food/history").json()["history"] == [{"who": "me", "text": "eggs"}]
    assert env.post("/api/food/chat", json={"text": "2 eggs"}, headers=h).status_code == 200
    assert env.post("/api/food/button", json={"data": "confirm"}, headers=h).status_code == 200
    assert env.post("/api/food/button", json={"data": "rm -rf"}, headers=h).status_code == 400
    assert seen == [("jamie", {"text": "2 eggs"}), ("jamie", {"button": "confirm"})]
