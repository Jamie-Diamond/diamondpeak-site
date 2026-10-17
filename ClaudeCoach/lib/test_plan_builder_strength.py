"""plan_builder's strength days: the name the planner gives a day decides what the exercise
library writes for it (17 Oct 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_builder as pb  # noqa: E402
import strength_builder as sb  # noqa: E402

LIB = sb.load_library()
PHYSIO = {"injuries": [{"description": "Knee surgery rehab", "location": "knee", "status": "active",
                        "set_by": "physio",
                        "sessions": [{"title": "Hamstrings", "match": ["hamstring"],
                                      "items": [{"ex": "hip_thrust", "sets": 4, "reps": "8", "kg": 75}]}]}]}


def _ids(sess):
    return [it["ex"] for p in sess["parts"] for it in p["items"]]


def test_named_tier_and_slot_are_used():
    s = pb._strength_for("jamie", {"date": "2026-10-21", "name": "Strength B (Tier A)", "minutes": 40},
                         {"race_distance": "Full Ironman"}, LIB, [], "build", 2)
    assert s["slot"] == "B" and s["tier"] == "A" and s["module"] == "triathlon"
    assert all(i in LIB["exercises"] for i in _ids(s))


def test_physio_day_keeps_the_physio_session_as_set():
    s = pb._strength_for("fred", {"date": "2026-10-19", "name": "Strength: hamstrings", "minutes": 50},
                         PHYSIO, LIB, [], "build", 2)
    assert s["kind"] == "protocol" and s["title"] == "Hamstrings"
    first = s["parts"][0]["items"][0]
    assert first == {"ex": "hip_thrust", "sets": 4, "reps": "8", "kg": 75, "physio": True}
    assert len(s["parts"]) == 1                    # nothing added on top of the physio's work


def test_sports_from_the_race():
    assert pb._athlete_sports({"race_distance": "70.3"}) == ["swim", "bike", "run"]
    assert pb._athlete_sports({"race_distance": "Sportive"}) == ["bike"]
    assert pb._athlete_sports({"sports": ["Run"]}) == ["run"]
    assert pb._athlete_sports({}) == []
