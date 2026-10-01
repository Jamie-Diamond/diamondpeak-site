"""Ride indoors (lib/indoor.py; Jamie, 1 Oct 2026: "give riders the option to ride indoors
for rides below 2hrs ... suggest the type of Zwift workout for them").

Pinned: only planned rides under 2 hours, today or later, can move; the switch changes
the event type only and is remembered per date; a re-plan of that day keeps it indoors
(IcuClient.push_workout); the Zwift line matches the athlete's Zwift link; the suggested
Zwift workout type comes from the session.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import indoor  # noqa: E402
from icu_api import IcuClient  # noqa: E402

TODAY = date(2026, 10, 2)


def _ev(i="e1", day="2026-10-03", mins=60, typ="Ride", name="Sweetspot 3x10"):
    return {"id": i, "category": "WORKOUT", "type": typ, "name": name,
            "start_date_local": f"{day}T00:00:00", "moving_time": mins * 60}


class FakeClient:
    def __init__(self, events, profile=None):
        self.events, self.profile, self.edits = events, profile or {}, []

    def get_events(self, start, end, category=None):
        return self.events

    def edit_workout(self, event_id, **fields):
        self.edits.append((event_id, fields))
        return {}

    def get_athlete_profile(self):
        return self.profile


def test_only_short_planned_rides_from_today_qualify():
    assert indoor.eligible(_ev(mins=119), TODAY)
    assert not indoor.eligible(_ev(mins=120), TODAY)                  # 2 h stays outside
    assert not indoor.eligible(_ev(day="2026-10-01"), TODAY)          # yesterday
    assert not indoor.eligible(_ev(typ="Run"), TODAY)
    assert indoor.eligible(_ev(typ="VirtualRide"), TODAY)             # can come back out


def test_zwift_type_from_the_session():
    assert indoor.kind(_ev(name="Sweetspot 3x10")) == "sweet-spot"
    assert indoor.kind(_ev(name="VO2 5x3")) == "VO2 max"
    assert indoor.kind(_ev(name="Over-unders")) == "threshold"
    assert indoor.kind(_ev(name="Aerobic ride")) == "endurance"
    assert indoor.suggestion(_ev(mins=58)) == ("Or ride one of Zwift's own sweet-spot workouts "
                                              "of about 60 min.")


def test_zwift_status_from_the_intervals_profile():
    assert indoor.zwift_status({"zwift_user_id": "x", "zwift_upload_workouts": True}) == "on"
    assert indoor.zwift_status({"zwift_user_id": "x", "zwift_upload_workouts": False}) == "no_upload"
    assert indoor.zwift_status({"zwift_user_id": None}) == "off"
    assert "Workouts → Custom → Intervals.icu" in indoor.where_text("on")
    assert "Connect" in indoor.where_text("off")


def _base(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "athletes.json").write_text(json.dumps(
        {"fred": {"icu_athlete_id": "i9", "icu_api_key": "k"}}))
    (tmp_path / "athletes" / "fred").mkdir(parents=True)
    return tmp_path


def test_switch_indoors_then_back(tmp_path):
    base = _base(tmp_path)
    c = FakeClient([_ev()], {"zwift_user_id": "z", "zwift_upload_workouts": True})
    out = indoor.switch("fred", c, "e1", True, base=base, today=TODAY)
    assert c.edits == [("e1", {"type": "VirtualRide"})]
    assert out["indoor"] and out["zwift"] == "on" and "sweet-spot" in out["suggestion"]
    assert indoor.is_indoor("fred", "2026-10-03", base)
    c.events = [_ev(typ="VirtualRide")]
    back = indoor.switch("fred", c, "e1", False, base=base, today=TODAY)
    assert c.edits[-1] == ("e1", {"type": "Ride"}) and back == {
        "indoor": False, "date": "2026-10-03", "message": "Back outdoors."}
    assert not indoor.is_indoor("fred", "2026-10-03", base)


def test_switch_refuses_long_rides_and_missing_sessions(tmp_path):
    base = _base(tmp_path)
    with pytest.raises(ValueError):
        indoor.switch("fred", FakeClient([_ev(mins=150)]), "e1", True, base=base, today=TODAY)
    with pytest.raises(LookupError):
        indoor.switch("fred", FakeClient([]), "e1", True, base=base, today=TODAY)


def test_a_replan_of_that_day_stays_indoors(tmp_path, monkeypatch):
    base = _base(tmp_path)
    indoor.switch("fred", FakeClient([_ev()]), "e1", True, base=base, today=TODAY)
    monkeypatch.setattr(indoor, "BASE", base)
    sent = []
    c = IcuClient("i9", "k")
    monkeypatch.setattr(c, "_post", lambda path, payload: sent.append(payload) or {})
    c.push_workout("Ride", "2026-10-03", "Endurance 60")
    c.push_workout("Ride", "2026-10-04", "Endurance 60")
    c.push_workout("Run", "2026-10-03", "Easy run")
    assert [p["type"] for p in sent] == ["VirtualRide", "Ride", "Run"]
    assert indoor.sticky_type("someone-else", "Ride", "2026-10-03", base) == "Ride"
