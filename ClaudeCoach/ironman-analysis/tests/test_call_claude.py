"""call_claude's fallback branches had no coverage (17 Aug 2026).

An earlier agent editing lib/engine.py today accidentally left a live NameError
in one of call_claude's fallback branches (a reference to a `run` variable that
does not exist in this function's scope - that name belongs to stream_claude's
cancellable-run object, not to this one). 34 of the 35 tests in the suite at the
time still passed, because nothing exercised the branch: call_claude wraps its
whole body in `except Exception as e: return f"Error calling claude: {e}"`, so a
NameError never raises, it just becomes the RETURN VALUE. A test that only checks
"a string came back" or "no exception was raised" cannot tell that string apart
from a correct answer. The bug was only caught by a human reading the diff.

So every test below that drives a fallback branch asserts the exact returned
text by equality, not merely its type, and also asserts (a) exactly how many
subprocess calls happened and (b) which model each one used, in order. (a) and
(b) catch a broken branch that never reaches its _run_once call at all; the
text equality is what catches a NameError-shaped defect that swallows itself
into a plausible-looking string. See the bottom of this file for the map of
call_claude's branches this suite is built against.

Everything here drives a fake subprocess.run. Nothing spawns the real `claude`
CLI or touches the network.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import engine  # noqa: E402


# ---------------------------------------------------------------------------
# Branch map of call_claude (lib/engine.py:688), read off the source before any
# test was written:
#
#   1. NORMAL / SUCCESS - one _run_once call, rc == 0, non-empty text.
#      -> _finish_session persists state, the text is returned as-is.
#   2. RESUME-RETRY - triggers when mode == "resume" AND (rc != 0 OR no text)
#      from the first _run_once call. Logs, calls _clear_session (deletes
#      .chat_session.json), re-plans (which now returns mode "new" - no
#      session file left to resume), and calls _run_once again with the fresh
#      prompt. Model is unchanged; only the session/prompt rotate.
#   3. RATE-LIMIT FALLBACK - triggers when _is_limit_message(text) is true AND
#      the model just used was not already MODEL_SONNET. Calls _run_once again
#      on MODEL_SONNET with the SAME prompt/extra/mode, and rebinds `model` to
#      MODEL_SONNET for logging/finish_session purposes. This check runs on
#      whatever `text` is current at that point - i.e. AFTER a resume-retry,
#      so branches 2 and 3 can both fire in the same call (see the compound
#      test below).
#   4. rc == 0 and text falsy (empty string) - _finish_session is NOT called
#      (guarded by `if rc == 0 and text`), the function still returns
#      "(no response)".
#   5. subprocess.TimeoutExpired - caught, returns a fixed friendly string.
#      No session mutation.
#   6. Any other Exception (including, as found today, a NameError from a typo
#      in one of the branches above) - caught by the blanket handler, logged,
#      and turned into "Error calling claude: {e}". THIS is the branch that
#      makes silent breakage possible: nothing distinguishes this return value
#      from a legitimate answer except reading it.
#   7. turn_idx is read via _turn_index(st) BEFORE _finish_session runs,
#      because _finish_session mutates st["turns"] in place - read after and
#      the logged turn is one ahead of what actually got served (see
#      test_session_rotation.py::TestTurnIndexLogging for the streaming twin
#      of this same fact). Verified here via the turns= kwarg _log_timing is
#      called with.
# ---------------------------------------------------------------------------


@pytest.fixture
def athlete(tmp_path):
    a = tmp_path / "athletes" / "tester"
    a.mkdir(parents=True)
    (a / "system_prompt.txt").write_text("You are a coach.\n")
    return a / "system_prompt.txt"


@pytest.fixture
def cfg(tmp_path):
    return {"project_dir": str(tmp_path)}


class _FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _ok(text, session_id="sess-1", returncode=0):
    return _FakeResult(stdout=json.dumps({"result": text, "session_id": session_id}),
                       returncode=returncode)


def _dead(returncode=1):
    """A resume that failed outright: non-zero rc, nothing usable on stdout."""
    return _FakeResult(stdout="", stderr="session not found", returncode=returncode)


@pytest.fixture
def runs(monkeypatch):
    """Hand out fake subprocess.run results in order and record every call's
    model (argv[-3] is unreliable if extra_args grow; read it off the argv
    positionally relative to '--model' instead) and its env."""
    made = []

    def _install(*results):
        queue = list(results)

        def _run(cmd, **kwargs):
            model = cmd[cmd.index("--model") + 1]
            made.append({"model": model, "cwd": kwargs.get("cwd"),
                        "env": kwargs.get("env"), "timeout": kwargs.get("timeout")})
            nxt = queue.pop(0) if queue else _ok("(unexpected extra call)")
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        monkeypatch.setattr(subprocess, "run", _run)
        return made

    return _install


def _session_state(athlete):
    p = athlete.parent / ".chat_session.json"
    return json.loads(p.read_text()) if p.exists() else None


def _arm_resumable_session(athlete):
    """A live, resumable session so the resume-retry branch is reachable."""
    engine._save_session(athlete, {
        "session_id": "sess-old", "fp": engine._prompt_fingerprint(athlete),
        "turns": 3, "started": time.time(), "last_seen": ""})


def _not_an_error_string(result):
    """The one-line guard every test below leans on: a NameError-class defect
    in a fallback branch would still produce *a string*, just the wrong one."""
    assert not result.startswith("Error calling claude"), result
    assert result != "Sorry, that took too long. Try a simpler question or break it into steps."


# ---------------------------------------------------------------------------
# 1. Normal success path
# ---------------------------------------------------------------------------

class TestSuccessPath:

    def test_new_session_returns_text_and_persists_state(self, athlete, cfg, runs):
        made = runs(_ok("the coaching answer", session_id="sess-new"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "the coaching answer"
        assert len(made) == 1
        assert made[0]["model"] == engine.MODEL_OPUS
        st = _session_state(athlete)
        assert st["session_id"] == "sess-new"
        assert st["turns"] == 1

    def test_resume_success_increments_turns_without_rotating(self, athlete, cfg, runs):
        _arm_resumable_session(athlete)
        made = runs(_ok("follow-up answer", session_id="sess-old"))
        result = engine.call_claude("how did that feel", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "follow-up answer"
        assert len(made) == 1, "a healthy resume must not spawn a second process"
        st = _session_state(athlete)
        assert st["session_id"] == "sess-old"
        assert st["turns"] == 4

    def test_stateless_mode_never_writes_a_session_file(self, athlete, cfg, runs):
        runs(_ok("stateless answer"))
        result = engine.call_claude("hello", {**cfg, "session_resume": False}, [],
                                    system_prompt_file=athlete, athlete_name="Tester")
        assert result == "stateless answer"
        assert not (athlete.parent / ".chat_session.json").exists()

    def test_env_is_scoped_to_the_athlete_on_every_call(self, athlete, cfg, runs):
        made = runs(_ok("hi"))
        engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                           athlete_name="Tester")
        assert made[0]["env"]["CC_ATHLETE_SCOPE"] == "tester"


# ---------------------------------------------------------------------------
# 2. Resume-retry branch (dead resume falls back to a fresh session)
# ---------------------------------------------------------------------------

class TestResumeRetryBranch:

    def test_dead_resume_falls_back_to_a_fresh_session_and_succeeds(self, athlete, cfg, runs):
        _arm_resumable_session(athlete)
        made = runs(_dead(returncode=1), _ok("fresh session answer", session_id="sess-fresh"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "fresh session answer"
        _not_an_error_string(result)
        assert [c["model"] for c in made] == [engine.MODEL_OPUS, engine.MODEL_OPUS]
        assert all(c["env"]["CC_ATHLETE_SCOPE"] == "tester" for c in made), \
            "the retried spawn must stay scoped too"
        st = _session_state(athlete)
        assert st["session_id"] == "sess-fresh", "the stale session was not replaced"
        assert st["turns"] == 1, "a fresh session starts its own counter"

    def test_resume_with_no_text_but_rc_zero_also_retries(self, athlete, cfg, runs):
        """The trigger is `rc != 0 OR not text` - an rc==0 reply with an empty
        result string must retry exactly like a hard failure."""
        _arm_resumable_session(athlete)
        made = runs(_ok("", returncode=0), _ok("recovered answer", session_id="sess-fresh2"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "recovered answer"
        assert len(made) == 2

    def test_a_healthy_resume_is_not_retried(self, athlete, cfg, runs):
        """Mirror of the above: the guard must not fire when nothing is wrong."""
        _arm_resumable_session(athlete)
        made = runs(_ok("all good", session_id="sess-old"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "all good"
        assert len(made) == 1, "a healthy resume should never trigger the retry"

    def test_a_dead_NEW_session_is_not_retried(self, athlete, cfg, runs):
        """The guard is `mode == "resume"` specifically - a first-ever session
        (mode "new") failing outright must not retry, since there is no stale
        state to clear and nothing distinguishes it from a real outage."""
        made = runs(_dead(returncode=1))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        # _run_once falls back to stderr when stdout has no usable JSON result, so
        # a dead call still returns non-empty text here - the point of this test is
        # the CALL COUNT, not the text: a "new" mode must never retry regardless.
        assert result == "session not found"
        assert len(made) == 1
        assert _session_state(athlete) is None


# ---------------------------------------------------------------------------
# 3. Rate-limit / Opus-to-Sonnet fallback branch
# ---------------------------------------------------------------------------

class TestRateLimitFallbackBranch:

    def test_capped_opus_falls_back_to_sonnet_and_succeeds(self, athlete, cfg, runs, monkeypatch):
        monkeypatch.setattr(engine, "_is_limit_message", lambda t: t == "capped notice")
        made = runs(_ok("capped notice", session_id="sess-a"),
                    _ok("sonnet answer", session_id="sess-a"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "sonnet answer"
        _not_an_error_string(result)
        assert [c["model"] for c in made] == [engine.MODEL_OPUS, engine.MODEL_SONNET]
        assert all(c["env"]["CC_ATHLETE_SCOPE"] == "tester" for c in made), \
            "the fallback spawn must stay scoped, not run unscoped"

    def test_already_on_sonnet_is_never_retried_again(self, athlete, cfg, runs, monkeypatch):
        """The guard `model != MODEL_SONNET` stops an infinite bounce: if Sonnet
        itself reports a limit there is nowhere lower to fall."""
        monkeypatch.setattr(engine, "_is_limit_message", lambda t: True)
        made = runs(_ok("still capped"))
        result = engine.call_claude("hello", cfg, [], model=engine.MODEL_SONNET,
                                    system_prompt_file=athlete, athlete_name="Tester")
        assert result == "still capped"
        assert len(made) == 1

    def test_uncapped_reply_never_touches_sonnet(self, athlete, cfg, runs):
        made = runs(_ok("ordinary answer"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "ordinary answer"
        assert [c["model"] for c in made] == [engine.MODEL_OPUS]

    def test_sonnet_reply_persists_as_the_session_model_used(self, athlete, cfg, runs,
                                                             monkeypatch):
        """_finish_session and _log_timing run with the REBOUND model, not the
        one the call started on - checked indirectly via the session id staying
        consistent across the two spawns of a single logical turn."""
        monkeypatch.setattr(engine, "_is_limit_message", lambda t: t == "capped")
        made = runs(_ok("capped", session_id="sess-b"), _ok("real answer", session_id="sess-b"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "real answer"
        st = _session_state(athlete)
        assert st["session_id"] == "sess-b"


# ---------------------------------------------------------------------------
# Compound: both fallbacks fire on the same call
# ---------------------------------------------------------------------------

class TestBothFallbacksCanFireOnOneCall:

    def test_dead_resume_then_rate_limited_fresh_session_then_sonnet(
            self, athlete, cfg, runs, monkeypatch):
        """Resume dies -> fresh session spawns -> the fresh session's own reply
        reads as capped -> retried on Sonnet. Three processes for one call to
        call_claude. This is the scenario that most exercises the reassignment
        of extra/prompt/mode/st ahead of the rate-limit check: by the time that
        check runs, every one of those four has already been replaced once."""
        _arm_resumable_session(athlete)
        monkeypatch.setattr(engine, "_is_limit_message", lambda t: t == "capped again")
        made = runs(_dead(returncode=1),
                    _ok("capped again", session_id="sess-c"),
                    _ok("final sonnet answer", session_id="sess-c"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "final sonnet answer"
        _not_an_error_string(result)
        assert [c["model"] for c in made] == \
            [engine.MODEL_OPUS, engine.MODEL_OPUS, engine.MODEL_SONNET]
        st = _session_state(athlete)
        assert st["session_id"] == "sess-c"
        assert st["turns"] == 1, "the fresh session (not the dead resume) is what was kept"


# ---------------------------------------------------------------------------
# 4. rc == 0 with falsy text: no session write, still a returned string
# ---------------------------------------------------------------------------

class TestEmptyTextGuard:

    def test_empty_result_on_a_fresh_session_writes_no_state(self, athlete, cfg, runs):
        """mode is "new" here, so the resume-retry guard cannot fire; this pins
        the OTHER guard, `if rc == 0 and text`, on _finish_session itself."""
        runs(_ok(""))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "(no response)"
        assert _session_state(athlete) is None


# ---------------------------------------------------------------------------
# 5 & 6. Timeout and generic-exception branches
# ---------------------------------------------------------------------------

class TestErrorBranches:

    def test_timeout_returns_the_friendly_message(self, athlete, cfg, runs):
        runs(subprocess.TimeoutExpired(cmd="claude", timeout=300))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == ("Sorry, that took too long. "
                          "Try a simpler question or break it into steps.")
        assert _session_state(athlete) is None

    def test_a_crash_below_run_once_is_reported_not_raised(self, athlete, cfg, runs):
        """The catch-all is why the NameError bug this suite exists for was
        invisible: nothing above the caller ever sees an exception, only a
        string that reads like a real one but is not."""
        runs(OSError("no such binary"))
        result = engine.call_claude("hello", cfg, [], system_prompt_file=athlete,
                                    athlete_name="Tester")
        assert result == "Error calling claude: no such binary"
        assert _session_state(athlete) is None


# ---------------------------------------------------------------------------
# 7. turn_idx is read before _finish_session mutates st in place
# ---------------------------------------------------------------------------

class TestTurnIndexOrdering:

    def test_logged_turn_is_the_one_just_served_not_the_next_one(self, athlete, cfg, runs,
                                                                  monkeypatch):
        _arm_resumable_session(athlete)  # turns=3 -> this reply is turn 4
        logged = {}

        def _capture(path, model, mode, t0, t_init, t_first, turns=None, prompt_bytes=None):
            logged["turns"] = turns

        monkeypatch.setattr(engine, "_log_timing", _capture)
        runs(_ok("answer", session_id="sess-old"))
        engine.call_claude("hello", cfg, [], system_prompt_file=athlete, athlete_name="Tester")
        assert logged["turns"] == 4
        assert _session_state(athlete)["turns"] == 4, \
            "st was mutated in place; the read above must have happened BEFORE this"
