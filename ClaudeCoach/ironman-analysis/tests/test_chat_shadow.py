"""lib/chat_shadow.py: the chat model picker, judged side by side and never sent (16 Oct 2026).

Nothing here spawns `claude`: the picker and the twins are faked at claude_call.run_claude.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import chat_shadow as cs  # noqa: E402
import claude_call  # noqa: E402
import engine  # noqa: E402


class TestTwins:

    def test_picker_differs_and_opus_answered(self):
        assert cs.twins("opus", "haiku") == ["haiku", "sonnet"]

    def test_picker_agrees_with_sonnet_no_twin(self):
        assert cs.twins("sonnet", "sonnet") == []

    def test_opus_always_gets_a_sonnet_twin(self):
        assert cs.twins("opus", "opus") == ["sonnet"]
        assert cs.twins("claude-opus-5-5", None) == ["sonnet"]

    def test_picker_wants_opus_where_sonnet_answered(self):
        assert cs.twins("sonnet", "opus") == ["opus"]


class TestParsePick:

    def test_json_in_prose(self):
        assert cs.parse_pick('Sure: {"model": "Haiku", "why": "logging a weight"}') == \
            ("haiku", "logging a weight")

    @pytest.mark.parametrize("text", ["", "sonnet", '{"model": "gpt"}', "{broken"])
    def test_unusable_answer_is_no_pick(self, text):
        assert cs.parse_pick(text)[0] is None


def test_never_active_under_pytest():
    assert not cs.active(date(2026, 10, 17))


@pytest.fixture
def armed(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "SHADOW_DIR", tmp_path / "chat-shadow")
    monkeypatch.setattr(cs, "active", lambda today=None: True)
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: spawned.append((cmd, kw)))
    return spawned


class TestLaunch:

    def test_writes_the_turn_and_detaches(self, armed, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
        sp = tmp_path / "athletes" / "fred" / "system_prompt.txt"
        cs.launch("Felt good, 7/10", [{"user": "hi", "assistant": "hello"}], sp, "Fred",
                  "ctx", "opus", "Nice work.", 0.21)
        [f] = list((tmp_path / "chat-shadow").glob("*/chat-fred-*.json"))
        rec = json.loads(f.read_text())
        assert rec["real"] == {"model": "opus", "reply": "Nice work.", "cost": 0.21}
        [(cmd, kw)] = armed
        assert cmd[-2:] == ["--run", str(f)] and kw["start_new_session"]
        assert "ANTHROPIC_API_KEY" not in kw["env"], "shadow runs are dev work: never on the key"

    def test_daily_cap(self, armed, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "DAILY_CAP", 2)
        sp = tmp_path / "athletes" / "fred" / "system_prompt.txt"
        for i in range(4):
            monkeypatch.setattr(cs, "datetime", type("D", (), {
                "now": staticmethod(lambda i=i: __import__("datetime").datetime(2026, 10, 17, 9, 0, i))}))
            cs.launch(f"m{i}", [], sp, "Fred", "", "sonnet", "r")
        assert len(armed) == 2

    def test_nothing_for_an_empty_reply(self, armed, tmp_path):
        cs.launch("hi", [], tmp_path / "athletes/fred/system_prompt.txt", "Fred", "", "sonnet", "")
        assert armed == []


class TestRun:

    def test_picker_then_twins_read_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "SHADOW_DIR", tmp_path / "cs")
        monkeypatch.setattr(cs, "_maybe_tell", lambda: None)
        monkeypatch.setattr(engine, "_assemble", lambda *a: "FULL PROMPT")
        calls = []

        class R:
            def __init__(self, out):
                self.stdout, self.returncode = out, 0

        def fake(prompt, model, **kw):
            calls.append((model, kw["allowed_tools"], kw["env"].get("CC_READ_ONLY"),
                          kw["no_session_persistence"], prompt[:40]))
            return R(json.dumps({"result": f"<telegram>{model} reply</telegram>",
                                 "total_cost_usd": 0.1}))
        monkeypatch.setattr(claude_call, "run_claude", fake)
        monkeypatch.setattr(cs, "pick", lambda m, h, slug: {
            "model": "haiku", "why": "logging", "secs": 0.6, "cost": 0.0003,
            "fallback": None})
        f = tmp_path / "rec.json"
        f.write_text(json.dumps({"slug": "fred", "message": "Weight 72.4", "history": [],
                                 "sp_file": "x/fred/system_prompt.txt", "athlete_name": "Fred",
                                 "context": "", "real": {"model": "opus", "reply": "ok"},
                                 "pick": None, "twins": {}}))
        cs._run(str(f))
        rec = json.loads(f.read_text())
        assert rec["pick"]["model"] == "haiku" and rec["pick"]["why"] == "logging"
        assert sorted(rec["twins"]) == ["haiku", "sonnet"]
        assert rec["twins"]["sonnet"]["text"] == "<telegram>sonnet reply</telegram>"
        # every call read-only and stateless; the twins carry the never-sent note
        assert all(c[2] == "1" and c[3] for c in calls)
        assert all(c[4].startswith("SHADOW RE-RUN") for c in calls)
        assert "Write" not in cs.TOOLS and "Edit" not in cs.TOOLS


def test_engine_hands_a_finished_turn_to_the_shadow(tmp_path, monkeypatch):
    a = tmp_path / "athletes" / "tester"
    a.mkdir(parents=True)
    (a / "system_prompt.txt").write_text("You are a coach.\n")
    monkeypatch.setattr(engine, "_meter", lambda sp: None)
    got = []
    monkeypatch.setattr(cs, "active", lambda today=None: True)
    monkeypatch.setattr(cs, "launch", lambda *a, **k: got.append(a))

    class R:
        stdout, stderr, returncode = json.dumps({"result": "answer", "session_id": "s"}), "", 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    out = engine.call_claude("hello", {"project_dir": str(tmp_path)}, [],
                             system_prompt_file=a / "system_prompt.txt", athlete_name="Tester")
    assert out == "answer"
    [(msg, hist, sp, name, ctx, model, text, cost)] = got
    assert (msg, model, text) == ("hello", engine.MODEL_OPUS, "answer")


def test_report_keeps_the_would_change_line():
    t = "<telegram>45 g/hr it is.</telegram>\nWOULD CHANGE: Saturday's ride description"
    assert cs._tg(t) == "45 g/hr it is.\n[WOULD CHANGE: Saturday's ride description]"


def test_a_fallback_pick_is_no_pick(tmp_path, monkeypatch):
    """The picker timing out must not read as the picker choosing Sonnet."""
    monkeypatch.setattr(cs, "_maybe_tell", lambda: None)
    monkeypatch.setattr(engine, "_assemble", lambda *a: "P")
    monkeypatch.setattr(cs, "pick", lambda m, h, slug: {
        "model": "sonnet", "why": "", "secs": 1.5, "cost": None, "fallback": "timeout 1.5s"})
    asked = []
    monkeypatch.setattr(cs, "_ask", lambda p, m, *a: asked.append(m) or {"text": "x"})
    monkeypatch.setattr(cs, "SHADOW_DIR", tmp_path / "cs")
    f = tmp_path / "cs" / "2026-10-17" / "chat-fred-090000.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"slug": "fred", "ts": "2026-10-17T09:00:00", "message": "hi",
                             "history": [], "sp_file": "x/fred/system_prompt.txt",
                             "athlete_name": "Fred", "context": "",
                             "real": {"model": "sonnet", "reply": "ok", "cost": 0.1},
                             "pick": None, "twins": {}}))
    cs._run(str(f))
    assert asked == []                      # sonnet answered; no pick = no twin
    assert json.loads(f.read_text())["pick"]["fallback"] == "timeout 1.5s"
    assert "sonnet (fallback: timeout 1.5s)" in cs.report(7)


class TestPick:
    """pick(): one direct Messages API call on the business key, Sonnet on anything else."""

    @pytest.fixture
    def billing(self, monkeypatch):
        import claude_billing
        rec = []
        monkeypatch.setattr(claude_billing, "_key", lambda: "sk-test")
        monkeypatch.setattr(claude_billing, "tripped", lambda: False)
        monkeypatch.setattr(claude_billing, "record",
                            lambda *a, **k: rec.append((a, k)))
        return rec

    @staticmethod
    def answer(text, usage=None):
        return {"content": [{"type": "text", "text": text}],
                "usage": usage or {"input_tokens": 600, "output_tokens": 20}}

    def test_picks_and_ledgers_its_cost(self, billing, monkeypatch):
        sent = {}

        def post(body, key, timeout):
            sent.update(body=body, key=key)
            return self.answer('{"model": "opus", "why": "moves a race"}')
        monkeypatch.setattr(cs, "_post", post)
        p = cs.pick("Move my race to May", [{"user": "hi", "assistant": "hello"}], "fred")
        assert (p["model"], p["why"], p["fallback"]) == ("opus", "moves a race", None)
        assert sent["key"] == "sk-test"
        assert sent["body"]["model"] == "claude-haiku-5-5"
        assert sent["body"]["max_tokens"] <= 100
        assert sent["body"]["thinking"] == {"type": "disabled"}   # else no answer in 60-80
        assert "Move my race to May" in sent["body"]["messages"][0]["content"]
        assert "athlete: hi" in sent["body"]["messages"][0]["content"]
        # Haiku 5.5: 600 in at $0.10/M + 20 out at $0.50/M
        assert p["cost"] == pytest.approx(600 * 0.10 / 1e6 + 20 * 0.50 / 1e6)
        [(args, kw)] = billing
        assert args[0] is None and args[2:4] == ("fred", "chat-picker")
        assert kw["usd"] == pytest.approx(p["cost"])

    def test_key_never_reaches_the_environment(self, billing, monkeypatch):
        import os
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(cs, "_post", lambda b, k, t: self.answer('{"model": "haiku"}'))
        cs.pick("done", [], "fred")
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_timeout_falls_back_to_sonnet_fast(self, billing, monkeypatch):
        import time
        monkeypatch.setattr(cs, "_post", lambda b, k, t: time.sleep(1.0) or self.answer(
            '{"model": "haiku"}'))
        t0 = time.time()
        p = cs.pick("done", [], "fred", timeout=0.2)
        assert time.time() - t0 < 0.6
        assert p["model"] == "sonnet" and p["fallback"].startswith("timeout")

    def test_http_error_falls_back(self, billing, monkeypatch):
        def boom(b, k, t):
            raise OSError("connection refused")
        monkeypatch.setattr(cs, "_post", boom)
        p = cs.pick("done", [], "fred")
        assert p["model"] == "sonnet" and "connection refused" in p["fallback"]
        assert billing == []                 # nothing reached the API, nothing to bill

    def test_unreadable_answer_falls_back(self, billing, monkeypatch):
        monkeypatch.setattr(cs, "_post", lambda b, k, t: self.answer("I think sonnet"))
        p = cs.pick("done", [], "fred")
        assert p["model"] == "sonnet" and p["fallback"].startswith("unreadable")

    @pytest.mark.parametrize("key,trip", [("", False), ("sk-test", True)])
    def test_no_key_or_tripped_breaker_never_calls(self, monkeypatch, key, trip):
        import claude_billing
        monkeypatch.setattr(claude_billing, "_key", lambda: key)
        monkeypatch.setattr(claude_billing, "tripped", lambda: trip)
        monkeypatch.setattr(cs, "_post", lambda *a: pytest.fail("must not call the API"))
        p = cs.pick("done", [], "fred")
        assert p["model"] == "sonnet" and p["fallback"] == "no key"
