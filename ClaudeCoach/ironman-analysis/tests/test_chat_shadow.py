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
            if model == "haiku" and kw["allowed_tools"] == "":
                return R(json.dumps({"result": '{"model": "haiku", "why": "logging"}',
                                     "total_cost_usd": 0.002}))
            return R(json.dumps({"result": f"<telegram>{model} reply</telegram>",
                                 "total_cost_usd": 0.1}))
        monkeypatch.setattr(claude_call, "run_claude", fake)
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
        assert all(c[4].startswith("SHADOW RE-RUN") for c in calls[1:])
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
