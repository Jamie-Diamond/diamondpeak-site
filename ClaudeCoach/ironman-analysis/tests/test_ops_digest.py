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

    def test_missing_morning_card_flagged(self, digest):
        entries = [e for e in self.all_clean_entries()
                   if not (e["script"] == "morning-checkin" and e["athlete"] == "kathryn")]
        lines = digest.build_digest(entries, ATHLETES)
        assert any("no morning card" in l and "kathryn" in l for l in lines)

    def test_missing_prescription_respects_optout(self, digest):
        # kathryn has daily_prescription=False — her absence is not a gap
        lines = digest.build_digest(self.all_clean_entries(), ATHLETES)
        assert not any("prescription" in l for l in lines)

    def test_missing_watchdog_flagged(self, digest):
        entries = [e for e in self.all_clean_entries() if e["script"] != "watchdog"]
        lines = digest.build_digest(entries, ATHLETES)
        assert any("watchdog did not run" in l for l in lines)

    def test_failures_are_listed(self, digest):
        entries = self.all_clean_entries() + [
            _e("activity-watcher", "jamie", ok=False, detail="Telegram send failed after retry")]
        lines = digest.build_digest(entries, ATHLETES)
        assert any("Telegram send failed" in l for l in lines)

    def test_inactive_athletes_ignored(self, digest):
        lines = digest.build_digest(self.all_clean_entries(), ATHLETES)
        assert not any("old" in l for l in lines)
