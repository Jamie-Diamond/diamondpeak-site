"""lib/claude_billing.py: which Claude account pays for a call (16 Oct 2026).

The two failures that matter:
  * the API key reaching a call it should not (dev work, Jamie's own coaching): the CLI
    always prefers the key, so that call quietly bills the credit;
  * a refused key (credit used up) reaching an athlete as text, or not being retried.

Everything drives fake subprocess calls. Nothing spawns `claude` or touches the network.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import claude_billing as cb  # noqa: E402
import claude_call  # noqa: E402
import engine  # noqa: E402

KEY = "sk-ant-api03-test"
REFUSED = "Credit balance is too low"


@pytest.fixture
def billing(tmp_path, monkeypatch):
    """Isolated config, key file, trip file and athletes list. Returns a setter."""
    cfg = tmp_path / "claude-billing.json"
    key = tmp_path / "key"
    monkeypatch.setattr(cb, "CONFIG", cfg)
    monkeypatch.setattr(cb, "KEY_FILE", key)
    monkeypatch.setattr(cb, "TRIP_FILE", tmp_path / "tripped.json")
    monkeypatch.setattr(cb, "_athletes", lambda: {"jamie": {}, "fred": {}, "tester": {}})
    monkeypatch.delenv(cb.KEY_VAR, raising=False)
    alerts, told = [], []
    monkeypatch.setattr(cb, "_tell_jamie", lambda reason: told.append(reason))
    import ops_log
    monkeypatch.setattr(ops_log, "alert", lambda *a, **k: alerts.append(a))

    def _set(enabled=True, api=("fred", "tester"), sub=("jamie",), with_key=True):
        cfg.write_text(json.dumps({"enabled": enabled,
                                   "api_athletes": api if api == "all" else list(api),
                                   "subscription_athletes": list(sub)}))
        if with_key:
            key.write_text(KEY + "\n")
        elif key.exists():
            key.unlink()
        return alerts, told
    return _set


class TestRoute:

    def test_switch_off_keeps_everyone_on_max(self, billing):
        billing(enabled=False)
        assert cb.route("fred") == "subscription"

    def test_listed_athlete_goes_on_the_key(self, billing):
        billing()
        assert cb.route("fred") == "api"

    def test_unlisted_athlete_stays_on_max(self, billing):
        billing(api=("tester",))
        assert cb.route("fred") == "subscription"

    def test_subscription_list_beats_all(self, billing):
        billing(api="all")
        assert cb.route("jamie") == "subscription"
        assert cb.route("fred") == "api"

    def test_all_means_registered_athletes_only(self, billing):
        billing(api="all")
        assert cb.route("dev") == "subscription"
        assert cb.route("-tmp-cc-prune") == "subscription"

    def test_no_key_file_stays_on_max(self, billing):
        billing(with_key=False)
        assert cb.route("fred") == "subscription"

    def test_unnamed_call_stays_on_max(self, billing):
        billing(api="all")
        assert cb.route(None) == "subscription"
        assert cb.route("") == "subscription"

    def test_tripped_breaker_keeps_everyone_on_max_for_an_hour(self, billing):
        billing()
        cb.TRIP_FILE.write_text(json.dumps({"at": time.time() - 60}))
        assert cb.route("fred") == "subscription"
        cb.TRIP_FILE.write_text(json.dumps({"at": time.time() - cb.TRIP_SECONDS - 1}))
        assert cb.route("fred") == "api"

    def test_the_repo_config_keeps_jamie_and_dev_work_on_max(self):
        cfg = json.loads((REPO / "config" / "claude-billing.json").read_text())
        assert isinstance(cfg["enabled"], bool)
        assert "jamie" in cfg["subscription_athletes"]


class TestEnvFor:

    def test_unnamed_call_is_left_exactly_as_given(self, billing):
        billing(api="all")
        assert cb.env_for(None, None) is None
        e = {"A": "1"}
        assert cb.env_for(None, e) is e

    def test_key_added_for_an_athlete_on_it(self, billing):
        billing()
        e = {"CC_ATHLETE_SCOPE": "fred"}
        out = cb.env_for("fred", e)
        assert out[cb.KEY_VAR] == KEY
        assert out["CC_ATHLETE_SCOPE"] == "fred"
        assert cb.KEY_VAR not in e, "the caller's env must never be mutated"
        assert cb.KEY_VAR not in os.environ

    def test_inherited_key_is_stripped_for_an_athlete_on_max(self, billing, monkeypatch):
        billing()
        monkeypatch.setenv(cb.KEY_VAR, "leaked")
        out = cb.env_for("jamie", None)
        assert cb.KEY_VAR not in out
        assert os.environ[cb.KEY_VAR] == "leaked"


class TestBillingFailure:

    @pytest.mark.parametrize("text", [
        "Credit balance is too low",
        "Invalid API key · Fix external API key",
        'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}',
        "You've hit your org's monthly spend limit",
        "This organization has been disabled.",
    ])
    def test_refusals_are_recognised(self, text):
        assert cb.is_billing_failure(text)

    def test_json_output_is_read_by_its_result(self):
        out = json.dumps({"type": "result", "is_error": True, "result": REFUSED,
                          "usage": {"x": "y" * 900}})
        assert cb.is_billing_failure(cb.failure_text(out))

    @pytest.mark.parametrize("text", [
        "",
        "Easy 45 min Z2 today. Fuel 60g/h.",
        "Your credit balance is too low? " + "a long coaching reply " * 40,
        '{"load": 401, "name": "Threshold"}',
    ])
    def test_answers_are_not_refusals(self, text):
        assert not cb.is_billing_failure(cb.failure_text(text))

    def test_failed_needs_the_key_in_the_env(self, billing):
        assert not cb.failed({}, REFUSED)
        assert cb.failed({cb.KEY_VAR: KEY}, REFUSED)


class _R:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture
def spawns(monkeypatch):
    made = []

    def _install(*results):
        queue = list(results)

        def _run(cmd, **kw):
            made.append({"cmd": cmd, "env": kw.get("env")})
            return queue.pop(0) if queue else _R("(unexpected extra call)")
        monkeypatch.setattr(subprocess, "run", _run)
        return made
    return _install


def _model(call):
    return call["cmd"][call["cmd"].index("--model") + 1]


class TestRun:

    def test_refused_key_retries_once_on_max(self, billing, spawns):
        alerts, told = billing()
        made = spawns(_R(REFUSED, returncode=1), _R("Aim: Z2.\nHeld Z2."))
        r = cb.run(["claude", "-p", "--model", "haiku"], athlete="fred",
                   input="x", capture_output=True, text=True)
        assert r.stdout == "Aim: Z2.\nHeld Z2."
        assert len(made) == 2
        assert made[0]["env"][cb.KEY_VAR] == KEY
        assert cb.KEY_VAR not in made[1]["env"]
        assert cb.tripped()
        assert len(alerts) == 1 and len(told) == 1

    def test_one_outage_is_logged_once_not_hourly(self, billing, spawns):
        alerts, told = billing()
        cb.trip("fred", REFUSED)
        # An hour later the key is tried again and refused again: same outage.
        st = json.loads(cb.TRIP_FILE.read_text())
        st["at"] = time.time() - cb.TRIP_SECONDS - 5
        cb.TRIP_FILE.write_text(json.dumps(st))
        cb.trip("fred", REFUSED)
        assert len(alerts) == 1 and len(told) == 1

    def test_working_key_is_one_call(self, billing, spawns):
        billing()
        made = spawns(_R("fine"))
        r = cb.run(["claude", "-p", "--model", "haiku"], athlete="fred", text=True)
        assert r.stdout == "fine" and len(made) == 1
        assert not cb.tripped()

    def test_athlete_on_max_never_sees_the_key(self, billing, spawns):
        billing()
        made = spawns(_R("fine"))
        cb.run(["claude", "-p", "--model", "haiku"], athlete="jamie", text=True)
        assert cb.KEY_VAR not in made[0]["env"]


class TestRunClaude:

    def test_athlete_on_the_key_and_refusal_retries_same_model_on_max(self, billing, spawns):
        billing()
        made = spawns(_R(REFUSED, returncode=1), _R("the card"))
        r = claude_call.run_claude("p", model=claude_call.SONNET, label="fred",
                                   athlete="fred")
        assert r.stdout == "the card" and not r.limited and not r.auth_failed
        assert [_model(c) for c in made] == ["sonnet", "sonnet"]
        assert made[0]["env"][cb.KEY_VAR] == KEY
        assert cb.KEY_VAR not in made[1]["env"]

    def test_spend_limit_is_not_read_as_a_model_cap(self, billing, spawns):
        billing()
        made = spawns(_R("Error: spend limit reached", returncode=1), _R("ok"))
        r = claude_call.run_claude("p", model=claude_call.SONNET, athlete="fred")
        assert r.stdout == "ok" and r.model == "sonnet"
        assert [_model(c) for c in made] == ["sonnet", "sonnet"]

    def test_unnamed_call_inherits_env_untouched(self, billing, spawns):
        billing(api="all")
        made = spawns(_R("ok"))
        claude_call.run_claude("p", model=claude_call.SONNET, label="bug-fixer")
        assert made[0]["env"] is None

    def test_scoped_env_without_athlete_is_not_billed(self, billing, spawns):
        # lib/shadow.py passes CC_ATHLETE_SCOPE for a dev comparison run: scope alone
        # must not put it on the key.
        billing(api="all")
        made = spawns(_R("ok"))
        env = {"CC_ATHLETE_SCOPE": "fred"}
        claude_call.run_claude("p", model=claude_call.HAIKU, env=env, fallback=[])
        assert made[0]["env"] is env and cb.KEY_VAR not in env


@pytest.fixture
def athlete(tmp_path):
    a = tmp_path / "athletes" / "tester"
    a.mkdir(parents=True)
    (a / "system_prompt.txt").write_text("You are a coach.\n")
    return a / "system_prompt.txt"


def _ok(text, sid="sess-1", rc=0):
    return _R(json.dumps({"result": text, "session_id": sid}), returncode=rc)


@pytest.fixture(autouse=True)
def _no_meter(monkeypatch):
    # engine._meter writes REPO/athletes/<slug>/chat-usage.json; keep the repo clean.
    monkeypatch.setattr(engine, "_meter", lambda sp_file: None)


class TestChat:

    def test_scoped_env_carries_the_key_for_an_athlete_on_it(self, billing, athlete):
        billing()
        assert engine.scoped_env(athlete)[cb.KEY_VAR] == KEY

    def test_dev_style_slug_is_never_billed(self, billing, tmp_path):
        billing(api="all")
        sp = tmp_path / "athletes" / "jamie" / "system_prompt.txt"
        assert cb.KEY_VAR not in engine.scoped_env(sp)

    def test_refused_key_retries_on_max_and_keeps_the_session(self, billing, athlete,
                                                                tmp_path, spawns):
        billing()
        engine._save_session(athlete, {
            "session_id": "sess-old", "fp": engine._prompt_fingerprint(athlete),
            "turns": 3, "started": time.time(), "last_seen": ""})
        made = spawns(_ok(REFUSED, sid="sess-old", rc=1), _ok("the answer", sid="sess-old"))
        out = engine.call_claude("hello", {"project_dir": str(tmp_path)}, [],
                                 system_prompt_file=athlete, athlete_name="Tester")
        assert out == "the answer"
        assert len(made) == 2
        assert made[0]["env"][cb.KEY_VAR] == KEY
        assert cb.KEY_VAR not in made[1]["env"]
        assert "--resume" in made[1]["cmd"], "a refused key must not cost the conversation"


class _FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = io.StringIO("".join(json.dumps(l) + "\n" for l in lines))
        self.stdin = io.StringIO()
        self.returncode = rc

    def wait(self, timeout=None):
        return self.returncode


class TestStream:

    def test_refusal_never_streams_and_the_turn_is_answered_on_max(self, billing, athlete,
                                                                    tmp_path, monkeypatch):
        billing()
        envs = []
        procs = [
            _FakeProc([{"type": "assistant", "message": {"content": [
                          {"type": "text", "text": REFUSED}]}},
                       {"type": "result", "result": REFUSED, "session_id": "s"}], rc=1),
            _FakeProc([{"type": "assistant", "message": {"content": [
                          {"type": "text", "text": "Real answer"}]}},
                       {"type": "result", "result": "Real answer", "session_id": "s"}]),
        ]

        def _popen(cmd, **kw):
            envs.append(kw.get("env"))
            return procs.pop(0)
        monkeypatch.setattr(subprocess, "Popen", _popen)
        events = list(engine.stream_claude("hi", {"project_dir": str(tmp_path)}, [],
                                           system_prompt_file=athlete, athlete_name="Tester"))
        shown = [e[1] for e in events if e[0] in ("chunk", "final")]
        assert REFUSED not in " ".join(shown)
        assert events[-1] == ("final", "Real answer")
        assert envs[0][cb.KEY_VAR] == KEY and cb.KEY_VAR not in envs[1]
