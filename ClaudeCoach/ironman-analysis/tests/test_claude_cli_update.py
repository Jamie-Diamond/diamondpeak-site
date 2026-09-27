"""scripts/claude-cli-update.py and lib/model_aliases.py - the weekly job that keeps
the model aliases on the newest models, and the footer label that says which one.

Nothing here spawns the CLI or npm: every external step is a monkeypatched seam.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import coach_alert     # noqa: E402
import model_aliases   # noqa: E402
import ops_log         # noqa: E402

MODELS = {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1",
          "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001"}


@pytest.fixture(scope="module")
def upd():
    spec = importlib.util.spec_from_file_location(
        "claude_cli_update", REPO / "scripts" / "claude-cli-update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(model_aliases, "RESOLVED_FILE", tmp_path / "claude-models.json")
    monkeypatch.setattr(model_aliases, "_cache", {"mtime": None, "data": {}})
    monkeypatch.setattr(ops_log, "ALERT_LOG", tmp_path / "ops-alerts.log")
    monkeypatch.setattr(ops_log, "RUN_STATUS", tmp_path / "run-status.jsonl")
    return tmp_path


def _write(path, models, version="2.1.283"):
    path.write_text(json.dumps({"cli_version": version, "models": models}))


class TestFooterLabel:
    def test_alias_shows_the_version_it_resolved_to(self, state):
        _write(model_aliases.RESOLVED_FILE, MODELS)
        assert model_aliases.label("opus") == "O5.5"
        assert model_aliases.label("sonnet") == "S5"
        assert model_aliases.label("haiku") == "H4.5"
        assert model_aliases.label("fable") == "F5.1"

    def test_unresolved_alias_falls_back_to_its_initial(self, state):
        assert model_aliases.label("opus") == "O"

    def test_a_full_id_needs_no_state_file(self, state):
        assert model_aliases.label("claude-opus-4-8") == "O4.8"
        assert model_aliases.resolved("claude-opus-4-8") == "claude-opus-4-8"

    def test_a_rewritten_file_is_picked_up_without_a_restart(self, state):
        _write(model_aliases.RESOLVED_FILE, MODELS)
        assert model_aliases.label("opus") == "O5.5"
        _write(model_aliases.RESOLVED_FILE, {**MODELS, "opus": "claude-opus-6"})
        model_aliases._cache["mtime"] = None   # same-second rewrite in a test
        assert model_aliases.label("opus") == "O6"


class TestParsing:
    def test_stream_gives_model_result_and_tools(self, upd):
        lines = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [
                {"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "rate_limit_event"},
            {"type": "result", "is_error": False, "result": "cc-smoke-7431"},
        ]
        got = upd.parse_stream("\n".join(json.dumps(l) for l in lines) + "\nnot json")
        assert got == {"model": "claude-opus-5-5", "result": "cc-smoke-7431",
                       "is_error": False, "tools": ["Bash"]}

    def test_no_result_event_is_an_error(self, upd):
        assert upd.parse_stream("")["is_error"] is True

    def test_version_order_is_numeric(self, upd):
        assert upd.newer("2.1.300", "2.1.283")
        assert not upd.newer("2.1.283", "2.1.283")
        assert upd.newer("2.10.0", "2.9.9")


class TestChangeMessage:
    def test_first_run_is_silent(self, upd):
        assert upd.change_lines({}, MODELS) == []

    def test_no_change_is_silent(self, upd):
        assert upd.change_lines(MODELS, dict(MODELS)) == []

    def test_a_changed_alias_is_named_in_plain_words(self, upd):
        new = {**MODELS, "opus": "claude-opus-6"}
        assert upd.change_lines(MODELS, new) == ["Opus 5.5 → Opus 6"]
        assert "Opus 5.5 → Opus 6" in upd.change_message(upd.change_lines(MODELS, new))


class TestMain:
    @pytest.fixture
    def world(self, upd, state, monkeypatch):
        """A fake box: live CLI 2.1.283, npm offers `latest`, smoke returns `smoke`."""
        w = {"live": "2.1.283", "latest": "2.1.283", "smoke": (MODELS, []),
             "swapped": [], "sent": [], "swap_err": ""}
        monkeypatch.setattr(upd, "cli_version", lambda b: w["live"])
        monkeypatch.setattr(upd, "latest_version", lambda: w["latest"])
        canary = state / "canary/node_modules/.bin/claude"
        canary.parent.mkdir(parents=True)
        monkeypatch.setattr(upd, "install_canary", lambda v: canary)
        monkeypatch.setattr(upd, "smoke", lambda b: (w.__setitem__("binary", b) or w["smoke"]))
        monkeypatch.setattr(upd, "swap", lambda v: w["swapped"].append(v) or w["swap_err"])
        monkeypatch.setattr(upd.coach_alert, "send",
                            lambda reason, text, key="": w["sent"].append((reason, text)) or "sent")
        return w

    def _runs(self):
        return [json.loads(l) for l in ops_log.RUN_STATUS.read_text().splitlines()]

    def test_nothing_new_retests_the_live_cli_and_records_the_map(self, upd, world):
        assert upd.main() == 0
        assert world["binary"] == upd.CLAUDE and world["swapped"] == []
        assert json.loads(model_aliases.RESOLVED_FILE.read_text())["models"] == MODELS
        assert self._runs()[-1]["detail"] == "checked ok"
        assert world["sent"] == []                 # first run: nothing to compare

    def test_a_new_cli_that_passes_is_installed_and_a_model_change_is_told(self, upd, world):
        _write(model_aliases.RESOLVED_FILE, MODELS)
        world["latest"] = "2.1.300"
        world["smoke"] = ({**MODELS, "opus": "claude-opus-6"}, [])
        assert upd.main() == 0
        assert world["binary"] != upd.CLAUDE       # tested the canary, not the live CLI
        assert world["swapped"] == ["2.1.300"]
        saved = json.loads(model_aliases.RESOLVED_FILE.read_text())
        assert saved["cli_version"] == "2.1.300" and saved["models"]["opus"] == "claude-opus-6"
        assert [r for r, _ in world["sent"]] == [coach_alert.MODEL_CHANGED]

    def test_a_new_cli_that_fails_never_touches_the_live_one(self, upd, world):
        _write(model_aliases.RESOLVED_FILE, MODELS)
        world["latest"] = "2.1.300"
        world["smoke"] = ({}, ["opus: API Error: 400"])
        assert upd.main() == 1
        assert world["swapped"] == [] and world["sent"] == []
        assert json.loads(model_aliases.RESOLVED_FILE.read_text())["models"] == MODELS
        last = self._runs()[-1]
        assert last["ok"] is False and "staying on 2.1.283" in last["detail"]
        assert coach_alert.classify(last) == coach_alert.FAILURE

    def test_a_failed_swap_restores_the_old_version(self, upd, world):
        world["latest"] = "2.1.300"
        world["swap_err"] = "reports 2.1.283 after installing 2.1.300"
        assert upd.main() == 1
        assert world["swapped"] == ["2.1.300", "2.1.283"]
        assert not model_aliases.RESOLVED_FILE.exists()


def test_the_job_is_a_watched_deliverable():
    d = next(d for d in coach_alert.DELIVERABLES if d["script"] == "claude-cli-update")
    assert d["telegram"] is False and d["cron_cmd"] == "claude-cli-update.py"
    assert coach_alert.OUTCOME_CLASS["claude-cli-update"] == coach_alert.FAILURE
