"""Status alerts go to the developer log, never Jamie's chat (1 Oct 2026)."""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))


def test_alerts_are_logged_not_sent(tmp_path, monkeypatch):
    import coach_alert
    importlib.reload(coach_alert)
    monkeypatch.setenv("CC_ALERT_DRY_RUN", "0")
    monkeypatch.delenv("CC_CHAT_ALERTS", raising=False)
    ran = []

    class FakeSubprocess:
        @staticmethod
        def run(*a, **k):
            ran.append(a)
            raise AssertionError("must not message the coach")
    monkeypatch.setattr(coach_alert, "subprocess", FakeSubprocess)
    logged = []
    monkeypatch.setattr(coach_alert.ops_log, "alert", lambda src, msg, **k: logged.append(msg))
    monkeypatch.setattr(coach_alert.ops_log, "log_outbound", lambda *a, **k: None)
    monkeypatch.setattr(coach_alert, "_read_state", lambda: {})
    monkeypatch.setattr(coach_alert, "_write_state", lambda st: None)
    action = coach_alert.send(coach_alert.PLAN_HARD_FAIL, "Kathryn's plan breaks a rule", key="k1")
    assert action == "logged" and not ran
    assert logged and "plan_hard_fail" in logged[0] and "Kathryn" in logged[0]
