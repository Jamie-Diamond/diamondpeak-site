"""The Dev tab (api/dev_chat.py): coach only, its own history and session, and restarts
done by the API after the reply, never by the model. No CLI or systemd is run here."""
import json

import pytest

import dev_chat
import server
from test_server import FakeSink, dev, env  # noqa: F401  (env is the fixture)

H = {"x-peak": "1"}


def events(body: str) -> list:
    out = []
    for block in body.split("\n\n"):
        kind = next((l[7:] for l in block.splitlines() if l.startswith("event: ")), None)
        data = next((l[6:] for l in block.splitlines() if l.startswith("data: ")), None)
        if kind:
            out.append((kind, json.loads(data)["text"]))
    return out


@pytest.fixture
def cli(monkeypatch):
    """engine._stream_once, faked: each call pops the next (final, sid) and records how
    it was called."""
    import engine
    calls, replies = [], []

    def fake(prompt, model, extra, cwd, env=None, run=None):
        calls.append({"prompt": prompt, "extra": extra, "cwd": cwd})
        final, sid = replies.pop(0)
        yield ("status", "Bash", "git status")
        if final:
            yield ("chunk", final[:5])
        return (final, final, sid, 0 if final else 1, None, None)

    monkeypatch.setattr(engine, "_stream_once", fake)
    return calls, replies


@pytest.fixture
def systemd(monkeypatch):
    ran = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(dev_chat.subprocess, "run", lambda cmd, **k: ran.append(cmd) or R())
    return ran


def test_split_restart():
    assert dev_chat.split_restart("Done.\n<restart>api</restart>") == ("Done.", "api")
    assert dev_chat.split_restart("Sure. <restart> VM </restart>") == ("Sure.", "vm")
    assert dev_chat.split_restart("No restart here") == ("No restart here", None)


def test_dev_is_coach_only(env, monkeypatch):
    dev(monkeypatch, "kat@example.com")
    assert env.get("/api/dev/history").status_code == 403
    assert env.post("/api/dev/chat", json={"text": "hi"}, headers=H).status_code == 403
    assert env.post("/api/dev/restart", json={"what": "vm"}, headers=H).status_code == 403
    dev(monkeypatch, "coach@example.com")
    assert env.post("/api/dev/chat", json={"text": "hi"}).status_code == 400      # no app header
    assert env.post("/api/dev/restart", json={"what": "db"}, headers=H).status_code == 400


def test_a_dev_turn_has_its_own_history_and_session(env, monkeypatch, cli, systemd):
    calls, replies = cli
    dev(monkeypatch, "coach@example.com")
    replies += [("Fixed it and pushed.", "s1"), ("Yes, still there.", "s1")]
    r = env.post("/api/dev/chat", json={"text": "fix the calendar"}, headers=H)
    ev = events(r.text)
    assert ("status", "Bash · git status") in ev and ev[-2:] == [("message", "Fixed it and pushed."), ("done", "")]
    assert calls[0]["cwd"] == str(server.CC.parent) and "--resume" not in calls[0]["extra"]
    assert dev_chat.DEV_PROMPT in calls[0]["extra"]
    env.post("/api/dev/chat", json={"text": "still there?"}, headers=H)
    assert calls[1]["extra"][-2:] == ["--resume", "s1"] and calls[1]["prompt"] == "still there?"
    h = env.get("/api/dev/history").json()
    assert [(m["who"], m["text"]) for m in h["history"]] == [
        ("me", "fix the calendar"), ("coach", "Fixed it and pushed."),
        ("me", "still there?"), ("coach", "Yes, still there.")]
    assert h["busy"] is False and systemd == []
    # the coaching chat never sees any of it
    assert not (server.CC / "athletes" / "jamie" / "telegram" / "history.json").exists()


def test_a_lost_session_starts_fresh_with_recent_context(env, monkeypatch, cli, systemd):
    calls, replies = cli
    dev(monkeypatch, "coach@example.com")
    replies += [("First.", "s1"), ("", None), ("Second.", "s2")]
    env.post("/api/dev/chat", json={"text": "one"}, headers=H)
    env.post("/api/dev/chat", json={"text": "two"}, headers=H)
    assert "--resume" in calls[1]["extra"] and "--resume" not in calls[2]["extra"]
    assert "Jamie: one" in calls[2]["prompt"] and "You: First." in calls[2]["prompt"]
    assert calls[2]["prompt"].endswith("two")
    assert json.loads((server.CC / "athletes" / "jamie" / "dev-chat" / "session.json").read_text())[
        "session_id"] == "s2"


def test_restart_api_marker_is_done_by_the_api_after_the_reply(env, monkeypatch, cli, systemd):
    calls, replies = cli
    dev(monkeypatch, "coach@example.com")
    replies.append(("Restarting it for you.\n<restart>api</restart>", "s1"))
    ev = events(env.post("/api/dev/chat", json={"text": "restart the api"}, headers=H).text)
    assert ("message", "Restarting it for you.") in ev
    assert ("message", dev_chat.SAID["api"]) in ev
    assert len(systemd) == 1 and systemd[0][0] == "systemd-run" and "--reboot" not in systemd[0]
    assert "--max-wait" in systemd[0]


def test_restart_vm_marker_only_offers_a_button(env, monkeypatch, cli, systemd):
    calls, replies = cli
    dev(monkeypatch, "coach@example.com")
    replies.append(("OK, tap to confirm.<restart>vm</restart>", "s1"))
    env.post("/api/dev/chat", json={"text": "reboot the vm"}, headers=H)
    assert systemd == []
    last = env.get("/api/dev/history").json()["history"][-1]
    assert last["text"] == "OK, tap to confirm." and last["buttons"] == dev_chat.VM_BUTTON
    r = env.post("/api/dev/restart", json={"what": "vm"}, headers=H)
    assert r.status_code == 200 and r.json()["text"] == dev_chat.SAID["vm"]
    assert systemd[0][0] == "systemd-run" and systemd[0][-1] == "--reboot"


def _jamie_chat(history, outbox):
    a = server.CC / "athletes" / "jamie"
    (a / "telegram").mkdir(parents=True, exist_ok=True)
    (a / "telegram" / "history.json").write_text(json.dumps(history))
    (a / "web-outbox.jsonl").write_text("".join(json.dumps(o) + "\n" for o in outbox))


def test_each_message_shows_in_one_tab(env, monkeypatch):
    dev(monkeypatch, "coach@example.com")
    monkeypatch.setattr(server.chat, "CC", server.CC)
    run = {"user": "How was my run?", "assistant": "Steady.", "ts": "2026-10-04T08:00:00"}
    code = {"user": "Ship the tracking switch", "assistant": "Shipped.", "ts": "2026-10-04T09:00:00"}
    _jamie_chat([run, code], [
        {"id": "20261004000442-aaaaaa", "ts": "2026-10-04T00:04:42", "text": "Bug fix ready",
         "buttons": [[{"text": "Merge", "data": "bf:yes:1"}]], "source": "bug-fixer"},
        {"id": "20261004070017-bbbbbb", "ts": "2026-10-04T07:00:17", "text": "Good morning",
         "source": "notify"},
        {"id": "20261004080000-cccccc", "ts": "2026-10-04T08:10:00", "text": "Merged & deployed",
         "source": "web-turn", "tab": "dev"}])
    server.chat.move_to_dev("jamie", [server.chat.history_key(code)])
    coach = [m["text"] for m in env.get("/api/chat/history").json()["history"]]
    devs = env.get("/api/dev/history").json()["history"]
    assert coach == ["Good morning", "How was my run?", "Steady."]
    assert [m["text"] for m in devs] == ["Bug fix ready", "Merged & deployed", "Ship the tracking switch", "Shipped."]
    assert devs[0]["id"] == "20261004000442-aaaaaa" and devs[0]["buttons"][0][0]["data"] == "bf:yes:1"


def test_a_tap_in_dev_is_kept_in_dev(env, monkeypatch):
    import outbox
    monkeypatch.setattr(outbox, "ATHLETES_CONFIG", server.ATHLETES_CONFIG)
    monkeypatch.setattr(outbox, "BASE", server.CC)
    outbox._CACHE.update(mtime=None, data={})
    mid = outbox.record("111", "✅ Merged & deployed", source="web-turn", tab="dev")
    e = [json.loads(l) for l in (server.CC / "athletes" / "jamie" / "web-outbox.jsonl").read_text().splitlines()]
    assert e[-1]["id"] == mid and e[-1]["tab"] == "dev"
    assert server.chat.is_dev(e[-1], "o:" + mid, set())


def test_dev_button_taps_say_which_tab(env, monkeypatch):
    dev(monkeypatch, "coach@example.com")
    got = []
    monkeypatch.setattr(server.chat, "start_turn",
                        lambda cid, label="", **k: got.append(k) or FakeSink([("done", "")]))
    env.post("/api/chat/button", json={"data": "bf:yes:1", "tab": "dev"}, headers=H)
    env.post("/api/chat/button", json={"data": "bf:yes:1"}, headers=H)
    assert [k.get("tab") for k in got] == ["dev", None]


def test_the_bug_fixers_edit_reply_goes_to_the_bot_from_dev(env, monkeypatch, cli):
    dev(monkeypatch, "coach@example.com")
    b = server.chat.bot()
    monkeypatch.setitem(b._PENDING_BUG_EDIT, "111", "rid1")
    got = []
    monkeypatch.setattr(server.chat, "start_turn",
                        lambda cid, label="", **k: got.append((cid, k)) or FakeSink([("done", "")]))
    env.post("/api/dev/chat", json={"text": "keep the old wording"}, headers=H)
    assert got == [("111", {"text": "keep the old wording", "tab": "dev"})]
    assert cli[0] == []                                   # the dev CLI never ran
    assert dev_chat.history("jamie")[-1]["text"] == "keep the old wording"
