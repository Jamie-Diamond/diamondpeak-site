"""A chat reply's own cost reaches BOTH the allowance ledger and the chat shadow (17 Oct 2026).

chat_limits.run_cost remembers each CLI session's running total, so it must run once per
reply: _shadow and _meter each calling it made the second caller see the run's own total
as "the last one on record" and get $0 for every resumed turn (12 of 23 shadow records,
and the allowance charged $0 for the same turns on call_claude's path).

R1 / R2 are real result events (trimmed), a fresh Haiku turn and its --resume, captured on
the VM on 17 Oct 2026 with `claude -p --output-format stream-json --verbose`: the resumed
total_cost_usd and modelUsage are the session's running totals, usage is the turn's own.
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

import chat_limits  # noqa: E402
import chat_shadow  # noqa: E402
import engine  # noqa: E402

SID = "ba8f343b-1961-4d1b-b6d1-d82e8c89ac1d"
R1 = {"type": "result", "result": "OK", "session_id": SID, "total_cost_usd": 0.00140427,
      "usage": {"input_tokens": 2, "cache_creation_input_tokens": 6449,
                "cache_read_input_tokens": 11227, "output_tokens": 4},
      "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 4,
                                          "cacheReadInputTokens": 11227,
                                          "cacheCreationInputTokens": 6449,
                                          "costUSD": 0.00140427}}}
R2 = {"type": "result", "result": "OK", "session_id": SID, "total_cost_usd": 0.00224212,
      "usage": {"input_tokens": 2, "cache_creation_input_tokens": 3466,
                "cache_read_input_tokens": 14245, "output_tokens": 4},
      "modelUsage": {"claude-haiku-5-5": {"inputTokens": 4, "outputTokens": 8,
                                          "cacheReadInputTokens": 25472,
                                          "cacheCreationInputTokens": 9915,
                                          "costUSD": 0.00224212}}}
OWN2 = 0.00224212 - 0.00140427


@pytest.fixture
def athlete(tmp_path, monkeypatch):
    a = tmp_path / "athletes" / "tester"
    a.mkdir(parents=True)
    (a / "system_prompt.txt").write_text("You are a coach.\n")
    monkeypatch.setattr(chat_limits, "BASE", tmp_path)
    monkeypatch.setattr(engine, "_chat_limits", chat_limits)
    shadow = []
    monkeypatch.setattr(chat_shadow, "active", lambda today=None: True)
    monkeypatch.setattr(chat_shadow, "launch", lambda *a, **k: shadow.append(a[-1]))
    return a / "system_prompt.txt", shadow


def spent():
    return chat_limits._load("tester")[date.today().strftime("%Y-%m")]["usd"]


def test_call_claude_resumed_turn_costs_its_own_share_in_both(athlete, monkeypatch, tmp_path):
    sp, shadow = athlete
    for r in (R1, R2):
        class P:
            stdout, stderr, returncode = json.dumps(r), "", 0
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
        engine.call_claude("hi", {"project_dir": str(tmp_path)}, [], system_prompt_file=sp,
                           athlete_name="Tester")
    assert shadow == [pytest.approx(0.00140427, abs=1e-6), pytest.approx(OWN2, abs=1e-6)]
    assert spent() == pytest.approx(0.00140427 + OWN2, abs=1e-6)


def test_stream_resumed_turn_costs_its_own_share_in_both(athlete, monkeypatch, tmp_path):
    sp, shadow = athlete

    def fake_stream(r):
        def gen(*a, **k):
            engine._cost_add(r)
            return r["result"], r["result"], SID, 0, None, None
            yield  # noqa: unreachable - makes this a generator
        return gen
    for r in (R1, R2):
        monkeypatch.setattr(engine, "_stream_once", fake_stream(r))
        events = list(engine.stream_claude("hi", {"project_dir": str(tmp_path)}, [],
                                           system_prompt_file=sp, athlete_name="Tester"))
        assert events[-1] == ("final", "OK")
    assert shadow == [pytest.approx(0.00140427, abs=1e-6), pytest.approx(OWN2, abs=1e-6)]
    assert spent() == pytest.approx(0.00140427 + OWN2, abs=1e-6)


def test_reply_cost_worked_out_once(athlete, monkeypatch):
    sp, _ = athlete
    calls = []
    real = chat_limits.run_cost
    monkeypatch.setattr(chat_limits, "run_cost", lambda s, r: calls.append(r) or real(s, r))
    engine._cost_reset()
    engine._cost_add(R1)
    assert engine._reply_usd(sp) == engine._reply_usd(sp) == pytest.approx(0.00140427, abs=1e-6)
    assert len(calls) == 1
    engine._cost_add(R2)                    # a further run in the same reply: only it is priced
    assert engine._reply_usd(sp) == pytest.approx(0.00140427 + OWN2, abs=1e-6)
    assert len(calls) == 2
    engine._cost_reset()
    assert engine._reply_usd(sp) == 0
