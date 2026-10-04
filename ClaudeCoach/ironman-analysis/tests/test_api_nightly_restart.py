"""scripts/api-nightly-restart.py - the midnight restart of Peak's API that waits for
in-flight replies first. Nothing here touches systemd: every external step is a
monkeypatched seam.
"""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import coach_alert     # noqa: E402
import ops_log         # noqa: E402


@pytest.fixture(scope="module")
def rst():
    spec = importlib.util.spec_from_file_location(
        "api_nightly_restart", REPO / "scripts" / "api-nightly-restart.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def logs(monkeypatch, tmp_path):
    monkeypatch.setattr(ops_log, "ALERT_LOG", tmp_path / "ops-alerts.log")
    monkeypatch.setattr(ops_log, "RUN_STATUS", tmp_path / "run-status.jsonl")
    return tmp_path


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _runs(logs):
    f = logs / "run-status.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def test_busy_pids_ignores_the_main_process(rst, monkeypatch, tmp_path):
    procs = tmp_path / "cgroup.procs"
    procs.write_text("406558\n412415\n")
    monkeypatch.setattr(rst, "CGROUP_PROCS", procs)
    monkeypatch.setattr(rst, "main_pid", lambda: "406558")
    assert rst.busy_pids() == ["412415"]
    procs.write_text("406558\n")
    assert rst.busy_pids() == []


def test_waits_for_a_reply_in_flight_then_goes(rst, monkeypatch):
    seq = iter([["412415"], ["412415"], []])
    monkeypatch.setattr(rst, "busy_pids", lambda: next(seq))
    c = Clock()
    assert rst.wait_for_idle(sleep=c.sleep, clock=c) is True
    assert c.t == 2 * rst.POLL_SECS


def test_gives_up_waiting_after_the_cap(rst, monkeypatch):
    monkeypatch.setattr(rst, "busy_pids", lambda: ["412415"])
    c = Clock()
    assert rst.wait_for_idle(sleep=c.sleep, clock=c) is False
    assert c.t >= rst.MAX_WAIT_SECS


def _systemctl(rc):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=rc, stdout="", stderr="boom")
    return run, calls


def test_clean_restart_writes_the_heartbeat(rst, monkeypatch, logs):
    run, calls = _systemctl(0)
    monkeypatch.setattr(rst.subprocess, "run", run)
    monkeypatch.setattr(rst, "wait_for_idle", lambda *a: True)
    monkeypatch.setattr(rst, "wait_until_up", lambda: True)
    assert rst.main([]) == 0
    assert ["systemctl", "restart", "claudecoach-api"] in calls
    d = next(d for d in coach_alert.DELIVERABLES if d["script"] == rst.SCRIPT)
    assert [r["detail"] for r in _runs(logs) if r["ok"]] == [d["detail"]]
    assert not (logs / "ops-alerts.log").exists()


def test_restart_still_happens_when_never_idle(rst, monkeypatch, logs):
    run, calls = _systemctl(0)
    monkeypatch.setattr(rst.subprocess, "run", run)
    monkeypatch.setattr(rst, "wait_for_idle", lambda *a: False)
    monkeypatch.setattr(rst, "wait_until_up", lambda: True)
    assert rst.main([]) == 0
    assert ["systemctl", "restart", "claudecoach-api"] in calls


def test_api_not_back_is_an_ops_alert(rst, monkeypatch, logs):
    run, _ = _systemctl(0)
    monkeypatch.setattr(rst.subprocess, "run", run)
    monkeypatch.setattr(rst, "wait_for_idle", lambda *a: True)
    monkeypatch.setattr(rst, "wait_until_up", lambda: False)
    assert rst.main([]) == 1
    assert "Peak is down" in (logs / "ops-alerts.log").read_text()
    assert [r["ok"] for r in _runs(logs)] == [False]


def test_failed_systemctl_is_an_ops_alert(rst, monkeypatch, logs):
    run, _ = _systemctl(1)
    monkeypatch.setattr(rst.subprocess, "run", run)
    monkeypatch.setattr(rst, "wait_for_idle", lambda *a: True)
    assert rst.main([]) == 1
    assert "exited 1" in (logs / "ops-alerts.log").read_text()


def test_registered_as_a_quiet_daily_deliverable(rst):
    d = next(d for d in coach_alert.DELIVERABLES if d["script"] == rst.SCRIPT)
    assert d["telegram"] is False and d["cron_cmd"] == "api-nightly-restart.py"
    assert d["cron"] == "0 0 * * *"
    assert coach_alert.OUTCOME_CLASS[rst.SCRIPT] == coach_alert.FAILURE


def test_on_demand_reboot_reboots_and_does_not_restart_the_api(rst, monkeypatch, logs):
    run, calls = _systemctl(0)
    monkeypatch.setattr(rst.subprocess, "run", run)
    waits = []
    monkeypatch.setattr(rst, "wait_for_idle", lambda *a: waits.append(a) or True)
    assert rst.main(["--reboot", "--max-wait", "300"]) == 0
    assert calls == [["systemctl", "reboot"]] and waits[0][0] == 300
