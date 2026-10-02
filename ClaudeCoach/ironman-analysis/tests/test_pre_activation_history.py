"""Sessions from before an athlete was approved are history, not news (Fred, 1 Oct 2026:
his 29 and 30 Sep strength sessions arrived with his Garmin history after approval and
were debriefed with "Quick log - RPE", then asked about again at the evening check-in).

scripts/activity-watcher.py _log_pre_activation_history writes them to the session log
as quiet history entries, so the new-activity gate never sees them and the RPE nudge
skips them. Athletes with no `activated` time are untouched.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "ironman-analysis"))


def _watcher():
    spec = importlib.util.spec_from_file_location("activity_watcher",
                                                  REPO / "scripts" / "activity-watcher.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ACTS = [
    {"id": "i1", "start_date_local": "2026-09-29T06:42:18", "type": "WeightTraining",
     "name": "Strength", "icu_training_load": 17, "moving_time": 2940},
    {"id": "i2", "start_date_local": "2026-09-30T06:42:49", "type": "WeightTraining",
     "name": "Strength", "icu_training_load": 18, "moving_time": 2820},
    {"id": "i3", "start_date_local": "2026-10-02T06:47:17", "type": "VirtualRide",
     "name": "Zwift", "icu_training_load": 40, "moving_time": 3600},
]


class FakeIcu:
    def __init__(self, *a):
        pass

    def get_training_history(self, days=3):
        return ACTS


def test_sessions_before_approval_are_logged_as_history(tmp_path, monkeypatch):
    w = _watcher()
    import icu_api
    monkeypatch.setattr(icu_api, "IcuClient", FakeIcu)
    log = tmp_path / "session-log.json"
    log.write_text("[]")
    cfg = {"icu_athlete_id": "i9", "icu_api_key": "k", "activated": "2026-10-01T20:32:41"}
    assert w._log_pre_activation_history("fred", cfg, log) == 2
    rows = json.loads(log.read_text())
    assert [r["activity_id"] for r in rows] == ["i1", "i2"]          # not i3, after approval
    assert all(r["history"] and r["stub"] and r["rpe"] is None for r in rows)
    assert rows[0]["sport"] == "Strength" and rows[0]["date"] == "2026-09-29"
    assert w._log_pre_activation_history("fred", cfg, log) == 0      # once only


def test_no_activation_time_changes_nothing(tmp_path, monkeypatch):
    w = _watcher()
    import icu_api
    monkeypatch.setattr(icu_api, "IcuClient", FakeIcu)
    log = tmp_path / "session-log.json"
    log.write_text("[]")
    assert w._log_pre_activation_history("jamie", {"icu_athlete_id": "x", "icu_api_key": "k"}, log) == 0
    assert json.loads(log.read_text()) == []
