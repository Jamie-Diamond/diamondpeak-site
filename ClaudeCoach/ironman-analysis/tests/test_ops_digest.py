"""Tests for ops_log + the ops-digest gap/failure detection.

Hermetic: ops_log writes are redirected to tmp_path; the digest is fed
synthetic run-status entries — no Telegram, no real logs.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import ops_log  # noqa: E402


@pytest.fixture(scope="module")
def digest():
    spec = importlib.util.spec_from_file_location(
        "ops_digest", REPO / "scripts" / "ops-digest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def logs(monkeypatch, tmp_path):
    monkeypatch.setattr(ops_log, "ALERT_LOG", tmp_path / "ops-alerts.log")
    monkeypatch.setattr(ops_log, "RUN_STATUS", tmp_path / "run-status.jsonl")
    return tmp_path


ATHLETES = {
    "jamie":   {"active": True},
    "kathryn": {"active": True, "daily_prescription": False},
    "old":     {"active": False},
}


def _e(script, athlete="", ok=True, detail=""):
    return {"ts": "2026-06-09T07:00:00", "script": script,
            "athlete": athlete, "ok": ok, "detail": detail}


class TestOpsLog:
    def test_record_run_appends_jsonl(self, logs):
        ops_log.record_run("morning-checkin", athlete="jamie", ok=True, detail="card sent")
        rows = [json.loads(l) for l in ops_log.RUN_STATUS.read_text().splitlines()]
        assert rows[-1]["script"] == "morning-checkin"
        assert rows[-1]["ok"] is True

    def test_alert_writes_both_files(self, logs):
        ops_log.alert("watchdog", "claude CLI exit 1", athlete="jamie")
        assert "claude CLI exit 1" in ops_log.ALERT_LOG.read_text()
        rows = [json.loads(l) for l in ops_log.RUN_STATUS.read_text().splitlines()]
        assert rows[-1]["ok"] is False


class TestBuildDigest:
    def all_clean_entries(self):
        return [
            _e("morning-checkin", "jamie", detail="card sent"),
            _e("morning-checkin", "kathryn", detail="card sent"),
            _e("daily-prescription", "jamie", detail="prescribed"),
            _e("watchdog", "jamie", detail="silent"),
        ]

    def test_all_clean_is_silent(self, digest):
        assert digest.build_digest(self.all_clean_entries(), ATHLETES) == []

    def test_failures_are_listed(self, digest):
        entries = self.all_clean_entries() + [
            _e("activity-watcher", "jamie", ok=False, detail="Telegram send failed after retry")]
        lines = digest.build_digest(entries, ATHLETES)
        assert any("Telegram send failed" in l for l in lines)

    def test_inactive_athletes_ignored(self, digest):
        lines = digest.build_digest(self.all_clean_entries(), ATHLETES)
        assert not any("old" in l for l in lines)


DAILY_SILENT = {
    "night-before-brief": "silent (empty tags — model chose silence)",
    "evening-checkin":    "silent",
    "capture-reminder":   "silent (nothing unlogged)",
    "session-sync":       "silent (empty history)",
}


class TestGapLines:
    """Gap detection moved out of build_digest into gap_lines() on 28 Jul 2026, so
    the daily/weekly windows and the Telegram routing decision could be driven from
    coach_alert.DELIVERABLES rather than hard-coded here."""

    def today(self, drop=(), fail=()):
        out = [_e("watchdog", "jamie", detail="silent")]
        for slug in ("jamie", "kathryn"):
            out.append(_e("morning-checkin", slug, detail="card sent"))
            for script, detail in DAILY_SILENT.items():
                out.append(_e(script, slug, detail=detail))
        out.append(_e("daily-prescription", "jamie", detail="prescribed"))
        out = [e for e in out if (e["script"], e["athlete"]) not in drop]
        for e in out:
            if (e["script"], e["athlete"]) in fail:
                e["ok"], e["detail"] = False, "boom"
        return out

    def week(self):
        return [_e(s, slug, detail="sent")
                for s in ("weekly-summary", "stage1-plan")
                for slug in ("jamie", "kathryn")]

    def call(self, digest, **kw):
        today = self.today(**kw)
        return digest.gap_lines(today, today + self.week(), ATHLETES)

    def test_all_present_no_gap_no_telegram(self, digest):
        assert self.call(digest) == ([], [])

    def test_silence_recorded_as_success_is_not_a_gap(self, digest):
        # every DAILY_SILENT entry above is a legitimate "sent nothing" run
        gaps, tg = self.call(digest)
        assert not any("night-before" in l or "capture reminder" in l for l in gaps)
        assert tg == []

    def test_missing_morning_card_flagged_and_telegrammed(self, digest):
        gaps, tg = self.call(digest, drop={("morning-checkin", "kathryn")})
        assert any("morning card" in l and "kathryn" in l for l in gaps)
        assert tg == ["morning card for kathryn"]

    def test_missing_watchdog_flagged_but_not_telegrammed(self, digest):
        gaps, tg = self.call(digest, drop={("watchdog", "jamie")})
        assert any("watchdog" in l for l in gaps)
        assert tg == []

    def test_missing_session_sync_flagged_but_not_telegrammed(self, digest):
        gaps, tg = self.call(digest, drop={("session-sync", "jamie")})
        assert any("session sync" in l and "jamie" in l for l in gaps)
        assert tg == []

    def test_recorded_failure_is_not_also_a_gap(self, digest):
        gaps, tg = self.call(digest, fail={("night-before-brief", "jamie")})
        assert gaps == [] and tg == []

    def test_missing_prescription_respects_optout(self, digest):
        # kathryn has daily_prescription=False — her absence is not a gap
        gaps, _ = self.call(digest)
        assert not any("prescription" in l for l in gaps)

    def test_weekly_gap_is_log_only(self, digest):
        today = self.today()
        week = today + [e for e in self.week() if e["script"] != "weekly-summary"]
        gaps, tg = digest.gap_lines(today, week, ATHLETES)
        assert any("weekly summary" in l for l in gaps)
        assert tg == []

    def test_weekly_plan_gap_is_log_only(self, digest):
        today = self.today()
        week = today + [e for e in self.week() if e["script"] != "stage1-plan"]
        gaps, tg = digest.gap_lines(today, week, ATHLETES)
        assert any("weekly plan" in l for l in gaps)
        assert tg == []

    def test_inactive_athlete_never_gapped(self, digest):
        gaps, _ = self.call(digest)
        assert not any("old" in l for l in gaps)


class TestCoachAlertRouting:
    """Only the two approved reasons may Telegram; everything else is refused."""

    def test_two_reasons_only(self):
        import coach_alert
        assert set(coach_alert.REASONS) == {coach_alert.DELIVERABLE_MISSING,
                                           coach_alert.CLAUDE_AUTH_FAILED}

    def test_unapproved_reason_refused(self, logs, monkeypatch):
        import coach_alert
        monkeypatch.setenv("CC_ALERT_DRY_RUN", "1")
        monkeypatch.setattr(coach_alert, "STATE", logs / "coach-alert-state.json")
        assert coach_alert.send("git_sync_stuck", "text") == "refused"
        assert coach_alert.send(coach_alert.DELIVERABLE_MISSING, "text") == "dry-run"

    def test_only_daily_deliverables_route_to_telegram(self):
        import coach_alert
        tg = {d["script"] for d in coach_alert.DELIVERABLES if d["telegram"]}
        assert tg == {"morning-checkin", "daily-prescription",
                      "night-before-brief", "evening-checkin"}
        assert all(d["window"] == "daily"
                   for d in coach_alert.DELIVERABLES if d["telegram"])

    def test_ops_log_cannot_send(self):
        import ast
        src = (REPO / "lib" / "ops_log.py").read_text()
        assert "subprocess" not in src        # nothing left to spawn notify.py with
        # and no import of anything that could, comments/docstrings aside
        imported = {n.name.split(".")[0] for node in ast.walk(ast.parse(src))
                    if isinstance(node, ast.Import) for n in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(ast.parse(src))
                     if isinstance(node, ast.ImportFrom) and node.module}
        assert imported == {"json", "re", "datetime", "pathlib"}


class TestFailuresInWindow:
    """ESCALATE_AFTER counted CONSECUTIVE failures on a success-cleared counter, so
    the seven interleaved push failures of 24-27 Jul never escalated. Replay the
    real episode against the replacement."""

    FAILS = ["2026-07-24T17:00", "2026-07-24T23:00", "2026-07-25T11:00",
             "2026-07-26T10:00", "2026-07-26T12:00", "2026-07-26T16:00",
             "2026-07-27T00:00"]

    def _replay(self, monkeypatch, logs, fails):
        from datetime import datetime, timedelta
        monkeypatch.setattr(ops_log, "SYNC_STATE", logs / "git-sync-state")
        actions, t = [], datetime(2026, 7, 24, 0, 0)
        want = set(fails)
        while t <= datetime(2026, 7, 27, 12, 0):
            if t.strftime("%Y-%m-%dT%H:%M") in want:
                actions.append((t, ops_log.sync_failure("replay", "push failed", now=t)))
            else:
                ops_log.sync_ok("replay", now=t)
            t += timedelta(hours=1)
        return actions

    def test_real_episode_escalates_on_day_two(self, logs, monkeypatch):
        acts = self._replay(monkeypatch, logs, self.FAILS)
        esc = [t for t, a in acts if a == "escalate"]
        assert len(esc) == 1
        assert esc[0].strftime("%Y-%m-%dT%H:%M") == "2026-07-25T11:00"

    def test_isolated_blip_stays_transient(self, logs, monkeypatch):
        acts = self._replay(monkeypatch, logs, self.FAILS[:1])
        assert [a for _, a in acts] == ["transient"]

    def test_pair_within_window_stays_transient(self, logs, monkeypatch):
        acts = self._replay(monkeypatch, logs, self.FAILS[:2])
        assert [a for _, a in acts] == ["transient", "transient"]


class TestAuthFailureKind:
    """Same CLI error, three provenances, three severities."""

    class _Fake:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    def _tty(self, monkeypatch, claude_call, tty):
        fake = type("S", (), {"stdin": self._Fake(tty), "stderr": self._Fake(tty)})
        monkeypatch.setattr(claude_call, "sys", fake)

    def test_production_expiry(self, monkeypatch):
        import claude_call
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-live-x")
        self._tty(monkeypatch, claude_call, False)
        assert claude_call.auth_failure_kind() == "token-expired"

    def test_interactive_is_not_an_outage(self, monkeypatch):
        import claude_call
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-live-x")
        self._tty(monkeypatch, claude_call, True)
        assert claude_call.auth_failure_kind() == "interactive"

    def test_hand_run_without_token(self, monkeypatch):
        import claude_call
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert claude_call.auth_failure_kind() == "no-token"


class TestSendFailureDoesNotEatTheCooldown:
    """An alarm that fails quietly is the bug this file exists to kill: banking the
    cooldown before the send would let one Telegram API error silence the next 6-12h."""

    def _ca(self, monkeypatch, logs, rc):
        import coach_alert
        monkeypatch.setenv("CC_ALERT_DRY_RUN", "0")
        monkeypatch.setattr(coach_alert, "STATE", logs / "coach-alert-state.json")
        monkeypatch.setattr(ops_log, "ALERT_LOG", logs / "ops-alerts.log")
        monkeypatch.setattr(ops_log, "RUN_STATUS", logs / "run-status.jsonl")
        result = type("R", (), {"returncode": rc, "stderr": b"telegram 400"})
        monkeypatch.setattr(coach_alert, "subprocess",
                            type("S", (), {"run": staticmethod(lambda *a, **k: result)}))
        return coach_alert

    def test_failed_send_retries_next_tick(self, logs, monkeypatch):
        ca = self._ca(monkeypatch, logs, rc=1)
        assert ca.send(ca.CLAUDE_AUTH_FAILED, "x", key="k") == "send-failed"
        assert ca.send(ca.CLAUDE_AUTH_FAILED, "x", key="k") == "send-failed"
        assert "coach NOT told" in ops_log.ALERT_LOG.read_text()

    def test_successful_send_banks_the_cooldown(self, logs, monkeypatch):
        ca = self._ca(monkeypatch, logs, rc=0)
        assert ca.send(ca.CLAUDE_AUTH_FAILED, "x", key="k") == "sent"
        assert ca.send(ca.CLAUDE_AUTH_FAILED, "x", key="k") == "cooldown"


class TestStage1PlanHeartbeat:
    """stage1-plan's heartbeat is gated on --push: a hand dry-run must NOT satisfy the
    weekly gap check, or a Wednesday experiment masks the Sunday cron never running."""

    def _run(self, monkeypatch, logs, argv):
        import importlib.util
        monkeypatch.setattr(ops_log, "RUN_STATUS", logs / "run-status.jsonl")
        monkeypatch.setattr(ops_log, "ALERT_LOG", logs / "ops-alerts.log")
        spec = importlib.util.spec_from_file_location(
            "stage1", REPO / "scripts" / "stage1-plan.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod, "ops_log", ops_log)
        monkeypatch.setattr(mod.sl, "planning_brief",
                            lambda *a, **k: {"event_unknown": True})
        monkeypatch.setattr(mod, "_load_prior_zones", lambda *a, **k: {})
        base = logs / "base"
        (base / "config").mkdir(parents=True)
        (base / "config" / "athletes.json").write_text(
            json.dumps({"testa": {"active": True, "chat_id": "1"}}))
        monkeypatch.setattr(mod, "BASE", base)
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            mod.main()
        if not ops_log.RUN_STATUS.exists():
            return []
        return [json.loads(l) for l in ops_log.RUN_STATUS.read_text().splitlines()]

    def test_push_run_records_the_failure(self, logs, monkeypatch):
        rows = self._run(monkeypatch, logs,
                         ["stage1-plan.py", "--athlete", "testa", "--push"])
        assert [(r["script"], r["ok"], r["detail"]) for r in rows] == \
            [("stage1-plan", False, "event unknown — cannot plan")]

    def test_dry_run_records_nothing(self, logs, monkeypatch):
        assert self._run(monkeypatch, logs,
                         ["stage1-plan.py", "--athlete", "testa"]) == []
