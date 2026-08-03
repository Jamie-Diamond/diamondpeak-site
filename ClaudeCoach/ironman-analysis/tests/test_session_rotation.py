"""Session rotation puts the rules back in front of the model.

Guards the 2026-08-03 fix. `_resume_prompt` deliberately sends NO system prompt and
NO rules - the CLI session already carries them - so the entire 82KB / 123-rule
surface is injected exactly once, at turn 1. With SESSION_MAX_TURNS at 30 that meant
the rules could be 29 turns of dense analysis behind the current question; Jamie's
`.chat_session.json` read turns=9 at the point quality collapsed on 3 Aug 2026.

Rotation is the only thing that re-injects them, so these tests pin the two
properties the fix depends on:

  1. a session rotates at the cap, and the rotated prompt CONTAINS the rules
  2. the cap is readable from telegram/config.json, so it can be tuned (or put back
     to 30) without a deploy

Without (1) the fix silently does nothing. Without (2) there is no way to back it
out quickly on a bot Jamie depends on daily.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import engine  # noqa: E402

RULE_MARKER = "[perm] MARKER-RULE-ONLY-IN-FULL-PROMPT"
SHARED_MARKER = "[perm] MARKER-SHARED-RULE"


@pytest.fixture
def athlete(tmp_path):
    """A minimal athlete tree: system prompt, own rules, shared rules."""
    a = tmp_path / "athletes" / "tester"
    a.mkdir(parents=True)
    (a / "system_prompt.txt").write_text("You are a coach. MARKER-SYSTEM-PROMPT\n")
    (a / "persistent-rules.md").write_text(
        "# Persistent coaching rules\n" + RULE_MARKER + " never regress this.\n")
    shared = tmp_path / "athletes" / "_shared"
    shared.mkdir(parents=True)
    (shared / "persistent-rules.md").write_text(SHARED_MARKER + " applies to everyone.\n")
    return a / "system_prompt.txt"


def _state(sp_file, turns, fp):
    """Write session state as _load_session expects it."""
    engine._save_session(sp_file, {"session_id": "sess-abc", "fp": fp,
                                   "turns": turns, "started": time.time(),
                                   "last_seen": ""})


def test_full_prompt_carries_rules_and_resume_does_not(athlete):
    """The premise of the whole fix. If a resumed prompt ever DID carry the rules,
    lowering the turn cap would be pointless churn."""
    full = engine._assemble("hello", [], athlete, "Tester", "")
    assert RULE_MARKER in full
    assert SHARED_MARKER in full
    assert "MARKER-SYSTEM-PROMPT" in full

    resumed = engine._resume_prompt("hello", [], "Tester", "", "")
    assert RULE_MARKER not in resumed
    assert SHARED_MARKER not in resumed
    assert "MARKER-SYSTEM-PROMPT" not in resumed


@pytest.mark.parametrize("turns", [1, 5, 7, 11])
def test_under_cap_resumes(athlete, turns):
    fp = engine._prompt_fingerprint(athlete)
    _state(athlete, turns, fp)
    _extra, _prompt, mode, _st = engine._plan_session(
        "hello", {"session_max_turns": 12}, [], athlete, "Tester", "")
    assert mode == "resume", f"turn {turns} should still resume under a cap of 12"


@pytest.mark.parametrize("turns", [12, 13, 30])
def test_at_or_over_cap_rotates_and_reinjects_rules(athlete, turns):
    """The assertion that matters: at the cap the session rotates AND the rules are
    back in the prompt."""
    fp = engine._prompt_fingerprint(athlete)
    _state(athlete, turns, fp)
    _extra, prompt, mode, _st = engine._plan_session(
        "hello", {"session_max_turns": 12}, [], athlete, "Tester", "")
    assert mode == "new", f"turn {turns} should rotate at a cap of 12"
    assert RULE_MARKER in prompt
    assert SHARED_MARKER in prompt


def test_cap_is_config_driven_so_it_can_be_backed_out(athlete):
    """Same state, three caps, three outcomes - no deploy involved. 30 restores the
    pre-2026-08-03 behaviour exactly."""
    fp = engine._prompt_fingerprint(athlete)
    _state(athlete, 15, fp)
    modes = {}
    for cap in (12, 20, 30):
        _e, _p, mode, _s = engine._plan_session(
            "hello", {"session_max_turns": cap}, [], athlete, "Tester", "")
        modes[cap] = mode
    assert modes == {12: "new", 20: "resume", 30: "resume"}, modes


def test_default_cap_is_12(athlete):
    """No config key: falls back to the module default, which the fix set to 12."""
    assert engine.SESSION_MAX_TURNS == 12
    fp = engine._prompt_fingerprint(athlete)
    _state(athlete, 12, fp)
    _e, _p, mode, _s = engine._plan_session("hello", {}, [], athlete, "Tester", "")
    assert mode == "new"


def test_garbage_cap_falls_back_instead_of_crashing(athlete):
    """A typo in config.json must not take the bot down mid-conversation."""
    fp = engine._prompt_fingerprint(athlete)
    _state(athlete, 5, fp)
    for bad in ("twelve", None, [], {}):
        _e, _p, mode, _s = engine._plan_session(
            "hello", {"session_max_turns": bad}, [], athlete, "Tester", "")
        assert mode == "resume", f"cap={bad!r} should fall back to the default"


def test_changed_rules_rotate_immediately(athlete):
    """Unchanged behaviour, retained because the turn cap now interacts with it: a
    mid-session rule edit must not keep coaching on the old surface until the cap."""
    fp = engine._prompt_fingerprint(athlete)
    _state(athlete, 2, fp)
    rules = athlete.parent / "persistent-rules.md"
    rules.write_text(rules.read_text() + "[perm] a brand new rule.\n")
    _e, prompt, mode, _s = engine._plan_session(
        "hello", {"session_max_turns": 12}, [], athlete, "Tester", "")
    assert mode == "new"
    assert "a brand new rule" in prompt


class TestTurnIndexLogging:
    """_finish_session increments st["turns"] IN PLACE. _log_timing is called after
    it, so reading the turn index at that point reports the NEXT turn rather than the
    one just served - the logged turn ran one ahead of the session file on 3 Aug 2026
    (log said turn=12, disk said turns=11). Harmless to rotation, which reads the
    loaded state, but it makes the only instrument for judging the rotation change
    off by one."""

    def test_turn_index_is_the_turn_being_served(self, athlete):
        st = {"session_id": "s", "fp": "f", "turns": 4, "started": time.time()}
        assert engine._turn_index(st) == 5, "5th reply on a session holding 4 turns"

    def test_new_and_stateless_sessions_are_turn_one(self):
        assert engine._turn_index(None) == 1
        assert engine._turn_index({}) == 1

    def test_finish_session_mutates_in_place_so_order_matters(self, athlete):
        """Pins the actual mechanism, so a future refactor that moves the read back
        after _finish_session fails here instead of silently skewing the logs."""
        st = {"session_id": "s", "fp": "f", "turns": 4,
              "started": time.time(), "last_seen": ""}
        before = engine._turn_index(st)
        engine._finish_session(athlete, "resume", st, "s")
        after = engine._turn_index(st)
        assert before == 5
        assert after == 6, "_finish_session incremented st in place"
        assert st["turns"] == 5, "the turn just served is now recorded on disk"

    def test_garbage_turns_value_does_not_raise(self):
        for bad in ({"turns": "four"}, {"turns": None}, {"turns": []}):
            assert engine._turn_index(bad) == 1
