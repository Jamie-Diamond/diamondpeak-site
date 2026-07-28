"""Tests for ops_log + the ops-digest gap/failure detection.

Hermetic: ops_log writes are redirected to tmp_path; the digest is fed
synthetic run-status entries — no Telegram, no real logs.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta
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


# Gap detection is time-dependent as of 28 Jul 2026 — a deliverable is judged only
# once its scheduled time has passed (coach_alert.due_status). Every gap/weekly test
# therefore injects an explicit `now` instead of inheriting the wall clock, which
# would make the suite pass or fail depending on the hour it is run at.
#
# NOW is a Wednesday evening after the 21:30 digest slot, and after every `since`
# in DELIVERABLES, so all ten deliverables are DUE and the tests exercise the gap
# logic rather than the due gate. The due gate has its own class below.
NOW = datetime(2026, 8, 5, 21, 30)

ATHLETES = {
    "jamie":   {"active": True},
    "kathryn": {"active": True, "daily_prescription": False},
    "old":     {"active": False},
}


def _e(script, athlete="", ok=True, detail="", outcome=None):
    e = {"ts": "2026-08-05T07:00:00", "script": script,
         "athlete": athlete, "ok": ok, "detail": detail}
    if outcome:
        e["outcome"] = outcome
    return e


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

    def test_sync_ok_writes_a_run_status_heartbeat(self, logs, monkeypatch):
        # 28 Jul 2026: sync_failure() already wrote a heartbeat on both its
        # branches, but a clean sync_ok() run wrote nothing to RUN_STATUS —
        # only sync_failure's counters. A gap check that reads run-status.jsonl
        # (coach_alert.DELIVERABLES' backup-config entry) would see a job that
        # always succeeds as ALWAYS missing without this.
        monkeypatch.setattr(ops_log, "SYNC_STATE", logs / "git-sync-state")
        ops_log.sync_ok("backup-config")
        rows = [json.loads(l) for l in ops_log.RUN_STATUS.read_text().splitlines()]
        assert rows[-1]["script"] == "backup-config"
        assert rows[-1]["ok"] is True


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
        out = [_e("watchdog", "jamie", detail="silent"),
               _e("backup-config", detail="sync ok")]
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
        """Gaps only. gap_lines also returns informational "ℹ not judged" lines
        (a deliverable whose scheduled time has not passed, or that has no crontab
        entry) — those are the OPPOSITE of a gap and are asserted in
        TestDueWindows, so they are filtered out here."""
        today = self.today(**kw)
        gaps, tg = digest.gap_lines(today, today + self.week(), ATHLETES, now=NOW)
        return [l for l in gaps if l.startswith("⚠")], tg

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

    def test_missing_backup_config_flagged_and_telegrammed(self, digest):
        # 28 Jul 2026: config/athletes.json.enc is the only backup of the
        # intervals.icu keys — a missing nightly heartbeat is condition 1.
        gaps, tg = self.call(digest, drop={("backup-config", "")})
        assert any("config backup" in l for l in gaps)
        assert tg == ["config backup"]

    def test_missing_watchdog_flagged_but_not_telegrammed(self, digest):
        gaps, tg = self.call(digest, drop={("watchdog", "jamie")})
        assert any("watchdog" in l for l in gaps)
        assert tg == []

    def test_missing_session_sync_flagged_but_not_telegrammed(self, digest):
        gaps, tg = self.call(digest, drop={("session-sync", "jamie")})
        assert any("session sync" in l and "jamie" in l for l in gaps)
        assert tg == []

    def test_recorded_failure_IS_a_gap_and_alerts(self, digest):
        """DELIBERATE REVERSAL, 28 Jul 2026 — do not "fix" this back.

        This test used to be test_recorded_failure_is_not_also_a_gap and asserted
        `gaps == [] and tg == []`, on the reasoning that a recorded failure is
        already a ✗ digest line and a gap line would be the same fact twice. That
        reasoning encoded the alarm's central blind spot: the ✗ line is log-only,
        so a night-before-brief that failed and logged it produced NO Telegram,
        and the only jobs that could alarm were the ones that died before reaching
        ops_log. A failed deliverable is a missed deliverable.
        """
        gaps, tg = self.call(digest, fail={("night-before-brief", "jamie")})
        assert any("night-before brief" in l and "jamie" in l for l in gaps)
        assert tg == ["night-before brief for jamie"]

    def test_a_failed_run_reads_as_no_SUCCESSFUL_deliverable(self, digest):
        # Wording matters now that a failure produces both a ✗ line and a gap
        # line: "no X heartbeat" would be a false statement (there IS one).
        gaps, _ = self.call(digest, fail={("night-before-brief", "jamie")})
        assert any(l.startswith("⚠ no successful ") for l in gaps)

    def test_missing_prescription_respects_optout(self, digest):
        # kathryn has daily_prescription=False — her absence is not a gap
        gaps, _ = self.call(digest)
        assert not any("prescription" in l for l in gaps)

    def test_weekly_gap_is_log_only(self, digest):
        today = self.today()
        week = today + [e for e in self.week() if e["script"] != "weekly-summary"]
        gaps, tg = digest.gap_lines(today, week, ATHLETES, now=NOW)
        assert any("weekly summary" in l for l in gaps)
        assert tg == []

    def test_weekly_plan_gap_is_log_only(self, digest):
        today = self.today()
        week = today + [e for e in self.week() if e["script"] != "stage1-plan"]
        gaps, tg = digest.gap_lines(today, week, ATHLETES, now=NOW)
        assert any("weekly plan" in l for l in gaps)
        assert tg == []

    def test_inactive_athlete_never_gapped(self, digest):
        gaps, _ = self.call(digest)
        assert not any("old" in l for l in gaps)


class TestOkFalseSemantics:
    """ok=False means BOTH "I failed" and "I ran fine and found something".

    Treating it as failure naively makes the alarm fire on week one from a working
    script; treating it as success is the blind spot the alarm exists to close.
    These tests pin the distinction. See lib/coach_alert.py above OUTCOME_CLASS.
    """

    # The genuine historical strings, verbatim from run-status.jsonl. The
    # weekly-summary ones were written by code that has since been corrected to
    # ok=True, and the 26 Jul pair is still inside the 7-day weekly window — so
    # these are exactly what a naive ok=False rule would have alarmed on, on day
    # one, from six weeks of history.
    BENIGN = ("realised TID missing_quality: no moderate/high time recorded "
              "vs target 30% — the week collapsed to all-easy")
    REAL   = "claude CLI exit 1 — no card generated"

    def test_ok_true_is_neither_class(self):
        import coach_alert
        assert coach_alert.classify(_e("morning-checkin", "jamie", detail="card sent")) == ""

    def test_finding_class_script(self):
        import coach_alert
        e = _e("weekly-summary", "calum", ok=False, detail=self.BENIGN)
        assert coach_alert.classify(e) == coach_alert.FINDING

    def test_failure_class_script(self):
        import coach_alert
        e = _e("morning-checkin", "jamie", ok=False, detail=self.REAL)
        assert coach_alert.classify(e) == coach_alert.FAILURE

    def test_unknown_script_is_unclassified(self):
        import coach_alert
        assert coach_alert.classify(_e("brand-new-job", ok=False)) == "unclassified"

    def test_record_level_outcome_beats_the_table_both_ways(self):
        """The escape hatch that keeps a NEW benign case safe.

        Someone adding a benign ok=False to a FAILURE-class script is the one
        genuinely dangerous case, because the per-script default would alarm on
        it. They pass outcome=ops_log.FINDING and that wins — and symmetrically a
        real failure inside a FINDING-class script can mark itself FAILURE.
        """
        import coach_alert
        benign_in_failure_script = _e("evening-checkin", "jamie", ok=False,
                                      detail="found something", outcome=ops_log.FINDING)
        real_in_finding_script = _e("weekly-summary", "jamie", ok=False,
                                    detail="crashed", outcome=ops_log.FAILURE)
        assert coach_alert.classify(benign_in_failure_script) == coach_alert.FINDING
        assert coach_alert.classify(real_in_finding_script) == coach_alert.FAILURE

    def test_every_monitored_deliverable_is_classified(self):
        """Adding a deliverable without classifying it FAILS THE BUILD.

        Fixture-free on purpose — this reads DELIVERABLES and OUTCOME_CLASS only,
        never the live log, so it is a real invariant rather than a snapshot of
        what happens to be on disk.
        """
        import coach_alert
        unclassified = [d["script"] for d in coach_alert.DELIVERABLES
                        if d["script"] not in coach_alert.OUTCOME_CLASS]
        assert unclassified == [], (
            f"monitored deliverables with no OUTCOME_CLASS entry: {unclassified} — "
            f"an ok=False from these would be silently treated as a success")

    def test_classification_values_are_valid(self):
        import coach_alert
        assert set(coach_alert.OUTCOME_CLASS.values()) <= {coach_alert.FAILURE,
                                                          coach_alert.FINDING}

    def test_record_run_stores_and_omits_outcome(self, logs):
        ops_log.record_run("weekly-summary", ok=False, detail="x", outcome=ops_log.FINDING)
        ops_log.record_run("weekly-summary", ok=False, detail="x")
        rows = [json.loads(l) for l in ops_log.RUN_STATUS.read_text().splitlines()]
        assert rows[-2]["outcome"] == ops_log.FINDING
        # Absent, not null — the line stays byte-identical to the 1000+ already
        # written, so no history has to be migrated.
        assert "outcome" not in rows[-1]


class TestBenignFindingDoesNotAlarm:
    """The regression that would have discredited this alarm in week one."""

    # Composed, not inherited: subclassing TestGapLines would re-collect and
    # re-run every one of its tests under a second name.
    today = staticmethod(lambda **kw: TestGapLines().today(**kw))
    week = staticmethod(lambda: TestGapLines().week())

    def test_weekly_drift_finding_is_not_a_gap(self, digest):
        today = self.today()
        week = [e for e in today + self.week() if e["script"] != "weekly-summary"]
        # The real 19/26 Jul shape: weekly-summary ran and recorded a drift FINDING
        # as ok=False. It is not a missed weekly summary.
        week += [_e("weekly-summary", slug, ok=False,
                    detail=TestOkFalseSemantics.BENIGN) for slug in ("jamie", "kathryn")]
        gaps, tg = digest.gap_lines(today, week, ATHLETES, now=NOW)
        assert not any("weekly summary" in l for l in gaps)
        assert tg == []

    def test_weekly_real_failure_is_a_gap(self, digest):
        today = self.today()
        week = [e for e in today + self.week() if e["script"] != "weekly-summary"]
        week += [_e("weekly-summary", slug, ok=False, detail="crashed",
                    outcome=ops_log.FAILURE) for slug in ("jamie", "kathryn")]
        gaps, _ = digest.gap_lines(today, week, ATHLETES, now=NOW)
        assert any("weekly summary" in l for l in gaps)

    def test_unclassified_failure_is_loud_but_never_alarms(self, digest):
        today = self.today() + [_e("brand-new-job", ok=False, detail="who knows")]
        gaps, tg = digest.gap_lines(today, today + self.week(), ATHLETES, now=NOW)
        assert tg == []          # cannot interrupt the coach
        lines = digest.unclassified_lines(today)
        assert len(lines) == 1 and "brand-new-job" in lines[0]
        assert "OUTCOME_CLASS" in lines[0]   # ...but names the fix

    def test_unclassified_line_folds_repeats_per_script(self, digest):
        today = [_e("brand-new-job", ok=False, detail=f"n{i}") for i in range(5)]
        assert len(digest.unclassified_lines(today)) == 1

    def test_classified_entries_produce_no_unclassified_line(self, digest):
        today = self.today(fail={("night-before-brief", "jamie")})
        today.append(_e("weekly-summary", "jamie", ok=False,
                        detail=TestOkFalseSemantics.BENIGN))
        assert digest.unclassified_lines(today) == []


class TestBackupConfigHeartbeat:
    """Task 2's registration is only real if a FAILED backup fails the check.

    sync_failure's first consecutive failure records ok=TRUE with detail
    "transient git-sync failure (1st): ..." because the next tick will heal it.
    backup-config runs once at 23:50, so its next tick is 24h away — that ok=True
    would report a night with no backup as clean.
    """

    def test_the_registered_detail_is_what_sync_ok_actually_writes(self, logs, monkeypatch):
        """Not "backup-config" assumed — the string the code emits.

        backup-config.sh calls git_sync_ok "backup-config" (lib_git_alert.sh),
        which calls ops_log.sync_ok("backup-config"). Both the script name and the
        detail are asserted against the real code path, because guessing either
        makes the gap check report a false miss every single night.
        """
        import coach_alert
        monkeypatch.setattr(ops_log, "SYNC_STATE", logs / "git-sync-state")
        ops_log.sync_ok("backup-config")
        row = json.loads(ops_log.RUN_STATUS.read_text().splitlines()[-1])
        d = next(x for x in coach_alert.DELIVERABLES if x["script"] == "backup-config")
        assert row["script"] == d["script"]
        assert row["detail"] == d["detail"] == "sync ok"
        assert d["window"] == "daily" and d["per_athlete"] is False and d["telegram"] is True

    def test_transient_failure_does_not_satisfy_the_check(self, digest, logs, monkeypatch):
        monkeypatch.setattr(ops_log, "SYNC_STATE", logs / "git-sync-state")
        ops_log.sync_failure("backup-config", "push to dpc_private failed")
        row = json.loads(ops_log.RUN_STATUS.read_text().splitlines()[-1])
        assert row["ok"] is True and "transient" in row["detail"]   # the trap
        row["ts"] = "2026-08-05T07:00:00"   # inside NOW's window — see NOW above
        gaps, tg = digest.gap_lines([row], [row], {}, now=NOW)
        assert any("config backup" in l for l in gaps)
        assert "config backup" in tg

    def test_per_athlete_false_matches_sync_oks_empty_athlete(self, digest):
        # sync_ok records athlete="" while per_athlete=False checks athlete=None
        # ("any"). A mismatch here would gap every night with the job running fine.
        row = _e("backup-config", athlete="", detail="sync ok")
        gaps, tg = digest.gap_lines([row], [row], {}, now=NOW)
        assert not any("config backup" in l for l in gaps) and tg == []


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

    def test_daily_and_weekly_deliverables_route_to_telegram(self):
        # Changed 28 Jul 2026: weekly deliverables now Telegram too (owner
        # approved), but only via ops-digest.py's separate weekly_alerts() path —
        # gap_lines() itself still only surfaces DAILY items (asserted in
        # TestGapLines.test_weekly_gap_is_log_only / test_weekly_plan_gap_is_log_only).
        import coach_alert
        daily_tg = {d["script"] for d in coach_alert.DELIVERABLES
                    if d["telegram"] and d["window"] == "daily"}
        weekly_tg = {d["script"] for d in coach_alert.DELIVERABLES
                     if d["telegram"] and d["window"] == "weekly"}
        assert daily_tg == {"morning-checkin", "daily-prescription",
                             "night-before-brief", "evening-checkin",
                             "backup-config"}
        assert weekly_tg == {"weekly-summary", "stage1-plan"}

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


class TestWeeklyAlerts:
    """WEEKLY deliverables Telegram once per occurrence (28 Jul 2026 change), not
    once per evening the 7-day window still shows them missing, and once per
    SCRIPT rather than once per athlete."""

    def _week(self, missing=()):
        # jamie + kathryn heartbeats present for both weekly scripts, except
        # the (script, athlete) pairs listed in `missing`.
        out = []
        for slug in ("jamie", "kathryn"):
            for script in ("weekly-summary", "stage1-plan"):
                if (script, slug) in missing:
                    continue
                out.append(_e(script, slug, detail="sent"))
        return out

    def test_single_miss_is_one_message_not_one_per_athlete(self, digest, logs, monkeypatch):
        import coach_alert
        monkeypatch.setenv("CC_ALERT_DRY_RUN", "1")
        monkeypatch.setattr(coach_alert, "STATE", logs / "coach-alert-state.json")
        week = self._week(missing={("weekly-summary", "jamie"), ("weekly-summary", "kathryn")})
        alerted = digest.weekly_alerts(week, ATHLETES, now=NOW)
        assert len(alerted) == 1
        assert "weekly summary" in alerted[0]
        assert "jamie" in alerted[0] and "kathryn" in alerted[0]

    def test_repeat_run_same_occurrence_does_not_re_alert(self, digest, logs, monkeypatch):
        import coach_alert
        monkeypatch.setenv("CC_ALERT_DRY_RUN", "0")
        monkeypatch.setattr(coach_alert, "STATE", logs / "coach-alert-state.json")
        result = type("R", (), {"returncode": 0, "stderr": b""})
        monkeypatch.setattr(coach_alert, "subprocess",
                            type("S", (), {"run": staticmethod(lambda *a, **k: result)}))
        week = self._week(missing={("weekly-summary", "jamie"), ("weekly-summary", "kathryn")})
        first = digest.weekly_alerts(week, ATHLETES, now=NOW)
        # A later run (e.g. the next evening's digest) with the SAME miss still
        # unresolved must not send a second message.
        second = digest.weekly_alerts(week, ATHLETES, now=NOW)
        assert len(first) == 1
        assert second == []

    def _stub_sender(self, monkeypatch, logs):
        """A send that "succeeds" without a subprocess, so the cooldown is
        genuinely BANKED.

        This matters, and it is why this helper exists (28 Jul 2026). Under
        CC_ALERT_DRY_RUN=1 send() returns "dry-run" BEFORE writing the state file,
        so no cooldown is ever banked — correct behaviour (a dry run must not
        silence a real alert) but it makes every cooldown assertion vacuous. The
        recovery test below used to run in dry-run and so passed whether or not
        clear_cooldown() did anything at all.
        """
        import coach_alert
        monkeypatch.setenv("CC_ALERT_DRY_RUN", "0")
        monkeypatch.setattr(coach_alert, "STATE", logs / "coach-alert-state.json")
        monkeypatch.setattr(ops_log, "ALERT_LOG", logs / "ops-alerts.log")
        monkeypatch.setattr(ops_log, "RUN_STATUS", logs / "run-status.jsonl")
        result = type("R", (), {"returncode": 0, "stderr": b""})
        monkeypatch.setattr(coach_alert, "subprocess",
                            type("S", (), {"run": staticmethod(lambda *a, **k: result)}))
        return coach_alert

    def test_one_miss_is_one_message_across_all_seven_evenings(self, digest, logs, monkeypatch):
        # The 7-day weekly window means a single Sunday miss still reads as
        # "missing" on all seven following evenings. Routed through the daily
        # per-date key that would be SEVEN Telegrams for one incident.
        ca = self._stub_sender(monkeypatch, logs)
        week = self._week(missing={("weekly-summary", "jamie"), ("weekly-summary", "kathryn")})
        sent = [digest.weekly_alerts(week, ATHLETES, now=NOW) for _ in range(7)]
        assert len(sent[0]) == 1
        assert all(s == [] for s in sent[1:]), f"re-alerted on a later evening: {sent}"
        assert list(json.loads(ca.STATE.read_text())) == [
            f"{ca.DELIVERABLE_MISSING}|weekly:weekly-summary"]

    def test_recovery_clears_cooldown_for_next_occurrence(self, digest, logs, monkeypatch):
        ca = self._stub_sender(monkeypatch, logs)
        missing_week = self._week(missing={("weekly-summary", "jamie"), ("weekly-summary", "kathryn")})
        present_week = self._week()
        assert len(digest.weekly_alerts(missing_week, ATHLETES, now=NOW)) == 1
        assert json.loads(ca.STATE.read_text())    # cooldown really was banked
        # Resolved: no alert, and the cooldown for this key is cleared.
        assert digest.weekly_alerts(present_week, ATHLETES, now=NOW) == []
        assert json.loads(ca.STATE.read_text()) == {}   # ...cleared, not just unread
        # A brand new occurrence (e.g. next Sunday) alerts again immediately,
        # rather than being silenced by the leftover 168h cooldown.
        assert len(digest.weekly_alerts(missing_week, ATHLETES, now=NOW)) == 1

    def test_without_recovery_the_cooldown_would_silence_next_week(self, digest, logs, monkeypatch):
        # The negative control for the test above: with clear_cooldown() stubbed
        # out, the 168h cooldown outlives the incident and swallows the following
        # week's genuine miss. This is what clear_cooldown() exists to prevent.
        ca = self._stub_sender(monkeypatch, logs)
        monkeypatch.setattr(ca, "clear_cooldown", lambda *a, **k: None)
        missing_week = self._week(missing={("weekly-summary", "jamie"), ("weekly-summary", "kathryn")})
        assert len(digest.weekly_alerts(missing_week, ATHLETES, now=NOW)) == 1
        digest.weekly_alerts(self._week(), ATHLETES, now=NOW)
        assert digest.weekly_alerts(missing_week, ATHLETES, now=NOW) == []   # silenced — the bug

    def test_stage1_plan_failing_for_all_athletes_is_one_message(self, digest, logs, monkeypatch):
        # Same root cause (one crashed Sunday plan build) affecting every
        # athlete must be one Telegram message, not three.
        import coach_alert
        monkeypatch.setenv("CC_ALERT_DRY_RUN", "1")
        monkeypatch.setattr(coach_alert, "STATE", logs / "coach-alert-state.json")
        week = self._week(missing={("stage1-plan", "jamie"), ("stage1-plan", "kathryn")})
        alerted = digest.weekly_alerts(week, ATHLETES, now=NOW)
        assert len(alerted) == 1
        assert "weekly plan" in alerted[0]


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


class TestDueWindows:
    """A deliverable may only be judged once it could actually have happened.

    Both false alarms the 28 Jul alarm shipped with were this one fault seen from
    two angles: backup-config runs at 23:50 and was checked at 21:30 (2h20m too
    early, every night, for ever), and stage1-plan's 7-day weekly window reached
    back to a Sunday two days before its heartbeat instrumentation existed. Neither
    job was broken. Both would have Telegrammed on night one.
    """

    today = staticmethod(lambda **kw: TestGapLines().today(**kw))
    week = staticmethod(lambda: TestGapLines().week())

    DIGEST = 21, 30   # the crontab slot this whole mechanism is timed against

    # --- the schedule parser ------------------------------------------------
    def test_field_forms_used_by_the_real_crontab(self):
        import coach_alert as ca
        assert ca._cron_field("*/30", 0, 59) == {0, 30}
        assert ca._cron_field("7-22/2", 0, 23) == {7, 9, 11, 13, 15, 17, 19, 21}
        assert ca._cron_field("6-9", 0, 23) == {6, 7, 8, 9}
        assert ca._cron_field("0", 0, 7) == {0}
        assert ca._cron_field("1,3,5", 0, 7) == {1, 3, 5}

    @pytest.mark.parametrize("spec", ["", "0 5 * *", "0 5 * * * *", "0 5 1 * *",
                                      "0 5 * 3 *", "banana 5 * * *", "0 5-x * * *",
                                      "0 */0 * * *", "99 5 * * *", "0 5 * * 9"])
    def test_an_unreadable_or_unsupported_spec_raises(self, spec):
        """It must RAISE, not guess.

        This is half the answer to "a deliverable added later with a bad time must
        not silently produce a nightly false alarm": a schedule this parser cannot
        read cannot produce a wrong due time, because it fails
        test_every_deliverable_declares_a_parseable_schedule below instead.
        """
        import coach_alert as ca
        with pytest.raises(ValueError):
            ca.parse_cron(spec)

    def test_sunday_is_both_0_and_7(self):
        import coach_alert as ca
        assert ca.parse_cron("0 18 * * 0")["dow"] == ca.parse_cron("0 18 * * 7")["dow"]

    def test_last_due_walks_back_past_days_the_job_does_not_run(self):
        import coach_alert as ca
        # Wednesday evening -> the previous Sunday's 18:00 weekly-plan slot.
        assert ca.last_due("0 18 * * 0", NOW) == datetime(2026, 8, 2, 18, 0)

    def test_last_due_skips_an_occurrence_that_may_still_be_running(self):
        import coach_alert as ca
        # evening-checkin at 21:00 has the tightest real margin to the 21:30 digest.
        # Asked AT 21:00 the run has not finished, so the answer is yesterday's.
        assert ca.last_due("0 21 * * *", datetime(2026, 8, 5, 21, 0)) \
            == datetime(2026, 8, 4, 21, 0)
        assert ca.last_due("0 21 * * *", NOW) == datetime(2026, 8, 5, 21, 0)

    # --- FAULT 1: judged before it could have run ---------------------------
    def test_a_job_scheduled_after_the_digest_is_not_judged_on_the_current_day(self):
        import coach_alert as ca
        d = next(x for x in ca.DELIVERABLES if x["script"] == "backup-config")
        due, state = ca.due_status(d, NOW)
        assert d["cron"] == "50 23 * * *"          # after the 21:30 digest
        assert state == ca.DUE                      # ...but still checked
        assert due == datetime(2026, 8, 4, 23, 50)  # on YESTERDAY's cycle

    def test_the_original_false_alarm_is_gone(self, digest):
        """21:30 on 28 Jul 2026 — the night the alarm would have cried wolf.

        Everything present that the evening's jobs would have written by 21:30;
        backup-config (23:50) and stage1-plan (Sunday, instrumented 28 Jul 11:44)
        cannot have. Neither may reach Telegram.
        """
        import coach_alert as ca
        night = datetime(2026, 7, 28, 21, 30)
        today = [e for e in self.today() if e["script"] != "backup-config"]
        # weekly-summary ran on Sunday 26 Jul, inside the 7-day window — its own
        # gap is a separate (real) concern and would drown out what this asserts.
        today += [_e("weekly-summary", slug, detail="sent")
                  for slug in ("jamie", "kathryn", "calum")]
        for e in today:
            e["ts"] = "2026-07-28T20:35:00"
        gaps, tg = digest.gap_lines(today, today, ATHLETES, now=night)
        assert tg == [], f"would have Telegrammed: {tg}"
        assert not any("config backup" in l and l.startswith("⚠") for l in gaps)
        assert any("config backup" in l and l.startswith("ℹ") for l in gaps)
        assert digest.weekly_alerts(today, ATHLETES, now=night) == []

    def test_a_genuine_backup_failure_the_following_night_DOES_alert(self, digest):
        """The other half: the gate must not have turned the check off.

        29 Jul 21:30. backup-config's 28 Jul 23:50 slot is now both past and after
        its instrumentation, so a night with no successful backup is a real miss
        and reaches Telegram — 24-hour detection, where the alternative was never.
        """
        night = datetime(2026, 7, 29, 21, 30)
        today = [e for e in self.today() if e["script"] != "backup-config"]
        today += [_e("weekly-summary", slug, detail="sent")
                  for slug in ("jamie", "kathryn", "calum")]
        for e in today:
            e["ts"] = "2026-07-29T20:35:00"
        gaps, tg = digest.gap_lines(today, today, ATHLETES, now=night)
        assert "config backup" in tg
        assert any("config backup" in l and "23:50" in l for l in gaps)

    def test_a_failed_backup_is_judged_on_the_previous_cycles_window(self, digest):
        """A "sync ok" from BEFORE the 23:50 slot must not satisfy it — otherwise
        the previous-cycle window would accept yesterday's success for ever."""
        night = datetime(2026, 7, 29, 21, 30)
        stale = _e("backup-config", detail="sync ok")
        stale["ts"] = "2026-07-28T09:00:00"      # before the 28 Jul 23:50 run
        fresh = dict(stale, ts="2026-07-29T00:01:00")   # after it
        assert "config backup" in digest.gap_lines([stale], [stale], {}, now=night)[1]
        assert "config backup" not in digest.gap_lines([fresh], [fresh], {}, now=night)[1]

    # --- FAULT 2: judged over a window predating its own instrumentation ----
    def test_a_newly_instrumented_deliverable_is_not_judged_on_earlier_cycles(self):
        import coach_alert as ca
        d = next(x for x in ca.DELIVERABLES if x["script"] == "stage1-plan")
        # The night the alarm shipped: last Sunday slot was 26 Jul 18:00, two days
        # before the heartbeat existed.
        due, state = ca.due_status(d, datetime(2026, 7, 28, 21, 30))
        assert (due, state) == (datetime(2026, 7, 26, 18, 0), ca.PRE_INSTRUMENTATION)
        # It ages out on its own — no seeded heartbeats, no edit to the audit trail.
        due, state = ca.due_status(d, datetime(2026, 8, 2, 21, 30))
        assert (due, state) == (datetime(2026, 8, 2, 18, 0), ca.DUE)

    def test_every_newly_instrumented_deliverable_got_the_grace(self):
        """FIVE deliverables were instrumented on 28 Jul, not the one that was
        spotted: night-before-brief, capture-reminder, session-sync, backup-config
        and stage1-plan had zero entries between them in 1075 lines of history.
        Each must be silent about every cycle before its own instrumentation."""
        import coach_alert as ca
        new = [d for d in ca.DELIVERABLES if d["since"].startswith("2026-07-28")]
        # capture-reminder was the fifth; it is deregistered (see coach_alert).
        assert {d["script"] for d in new} == {
            "night-before-brief", "session-sync", "backup-config", "stage1-plan"}
        for d in new:
            since = datetime.fromisoformat(d["since"])
            due, state = ca.due_status(d, since - timedelta(minutes=1))
            assert state == ca.PRE_INSTRUMENTATION, d["script"]

    def test_pre_instrumentation_is_visible_not_silent(self, digest):
        """Not judged must never be confusable with checked and fine, or a
        mis-declared deliverable could sit unchecked for ever."""
        import coach_alert as ca
        d = next(x for x in ca.DELIVERABLES if x["script"] == "stage1-plan")
        line = digest.not_due_line(d, datetime(2026, 7, 26, 18, 0),
                                   ca.PRE_INSTRUMENTATION)
        assert line.startswith("ℹ") and "not judged yet" in line
        assert "weekly plan" in line and "2026-07-28" in line

    def test_not_expected_never_clears_a_banked_cooldown(self, digest, logs, monkeypatch):
        """A cooldown banked for a real, still-unfixed miss must survive a cycle we
        did not actually check — otherwise the gate would silently re-arm and
        re-Telegram the same incident."""
        import coach_alert as ca
        monkeypatch.setattr(ca, "STATE", logs / "coach-alert-state.json")
        monkeypatch.setattr(ops_log, "ALERT_LOG", logs / "ops-alerts.log")
        ca._write_state({f"{ca.DELIVERABLE_MISSING}|weekly:stage1-plan": "2026-07-28T21:30:00"})
        # 28 Jul: stage1-plan is pre-instrumentation, so it is not checked at all.
        week = [_e("weekly-summary", slug, detail="sent") for slug in ATHLETES]
        assert digest.weekly_alerts(week, ATHLETES, now=datetime(2026, 7, 28, 21, 30)) == []
        assert f"{ca.DELIVERABLE_MISSING}|weekly:stage1-plan" in ca._read_state()

    # --- the declarations must match reality --------------------------------
    def test_every_deliverable_declares_a_parseable_schedule_and_a_since(self):
        import coach_alert as ca
        for d in ca.DELIVERABLES:
            assert d["cron"] and d["cron_cmd"], d["script"]
            ca.parse_cron(d["cron"])                      # raises if unreadable
            datetime.fromisoformat(d["since"])            # raises if malformed
            assert ca.last_due(d["cron"], NOW) is not None, d["script"]
        ca.parse_cron(ca.DIGEST_CRON)

    def test_declared_schedules_match_the_live_crontab(self):
        """The other half of "a bad time cannot silently false-alarm": declaring
        05:00 for a job that really runs at 23:50 fails HERE rather than mis-timing
        the gap check for ever. Compared as parsed value sets, so `*/30` and
        `0,30` are the same schedule.

        Skips only where there is no crontab to read (a dev machine). On the VM,
        where the digest actually runs, it executes.
        """
        import coach_alert as ca
        try:
            out = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                                 timeout=20)
        except Exception as exc:                                   # pragma: no cover
            pytest.skip(f"no crontab available: {exc}")
        if out.returncode != 0:                                    # pragma: no cover
            pytest.skip("crontab -l unavailable")
        lines = [l for l in out.stdout.splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
        def schedule_of(cmd_fragment):
            # Leading "/" so `watchdog.py` does not also match `bot-watchdog.py`.
            hits = [l for l in lines if "/" + cmd_fragment in l]
            assert len(hits) == 1, f"{cmd_fragment}: {len(hits)} crontab lines"
            return " ".join(hits[0].split()[:5])
        for d in ca.DELIVERABLES:
            live = schedule_of(d["cron_cmd"])
            assert ca.parse_cron(live) == ca.parse_cron(d["cron"]), \
                f"{d['script']}: declared {d['cron']!r}, crontab says {live!r}"
        assert ca.parse_cron(schedule_of("ops-digest.py")) == ca.parse_cron(ca.DIGEST_CRON)

    def test_a_retired_job_is_deregistered_not_left_to_gap(self, digest):
        """capture-reminder: retired 28 Jul 2026 (commit 2ba93c0), cron entry gone,
        main() a no-op, its ask folded into the 21:00 check-in as Case A2.

        Left registered it would have gapped every night for ever. Deregistered,
        its work is still monitored — by evening-checkin, on the same 21:00
        schedule and at telegram=True where capture-reminder was False.
        """
        import coach_alert as ca
        assert "capture-reminder" not in {d["script"] for d in ca.DELIVERABLES}
        # the vestigial heartbeat evening-checkin still writes must stay classified,
        # or an ok=False one would surface as a loud UNCLASSIFIED digest line
        assert ca.OUTCOME_CLASS["capture-reminder"] == ca.FAILURE
        assert digest.unclassified_lines(
            [_e("capture-reminder", "jamie", ok=False, detail="x")]) == []
        # the capture ask is now covered by a deliverable on the same schedule
        ec = next(d for d in ca.DELIVERABLES if d["script"] == "evening-checkin")
        assert ec["cron"] == "0 21 * * *" and ec["telegram"] is True
        gaps, tg = digest.gap_lines(self.today(), self.today() + self.week(),
                                    ATHLETES, now=NOW)
        assert not any("capture reminder" in l for l in gaps)

    def test_no_deliverable_is_registered_without_a_live_cron_entry(self):
        """The generalisation of the capture-reminder case: a retired job must be
        removed from DELIVERABLES, never left registered and unscheduled. This is
        the same assertion as test_declared_schedules_match_the_live_crontab, named
        separately because it is the property that matters operationally — a
        deliverable with no cron entry can only ever produce a false gap line."""
        self.test_declared_schedules_match_the_live_crontab()

    def test_a_deliverable_registered_with_no_crontab_entry_is_caught(self):
        """The remaining hole in the cross-check above — registering something that
        is not scheduled at all — closes because zero matching lines is a failure,
        not a pass."""
        import coach_alert as ca
        d = dict(ca.DELIVERABLES[0], script="ghost", cron_cmd="no-such-job.py")
        with pytest.raises(AssertionError):
            saved, ca.DELIVERABLES = ca.DELIVERABLES, [d]
            try:
                self.test_declared_schedules_match_the_live_crontab()
            finally:
                ca.DELIVERABLES = saved
