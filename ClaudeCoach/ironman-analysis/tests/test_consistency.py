"""lib/consistency.py: missed-session line, picked by cause (Jamie, 14 Oct 2026)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import consistency as c  # noqa: E402

T = date(2026, 10, 14)


def ev(day, typ="Ride", load=60, mins=60, name="Plan"):
    return {"category": "WORKOUT", "type": typ, "start_date_local": f"{day}T00:00:00",
            "icu_training_load": load, "moving_time": mins * 60, "name": name}


def act(day, typ="Ride", load=60, mins=60):
    return {"type": typ, "start_date_local": f"{day}T08:00:00",
            "icu_training_load": load, "moving_time": mins * 60}


def wellness(days=30, bad=None):
    rows = []
    for i in range(days, -1, -1):
        d = date.fromordinal(T.toordinal() - i).isoformat()
        r = {"id": d, "hrv": 60, "restingHR": 50, "sleepSecs": 8 * 3600}
        if bad and d in bad:
            r["sleepSecs"] = 5 * 3600
        rows.append(r)
    return rows


def test_overshoot():
    assert c.overshoot(ev("x", load=74), act("x", load=91))
    assert not c.overshoot(ev("x", load=74), act("x", load=80))
    assert c.overshoot(ev("x", load=0, mins=45), act("x", load=0, mins=215))
    assert not c.overshoot(ev("x", load=60, mins=60), act("x", load=62, mins=70))


def test_nothing_missed():
    assert c.assess([ev("2026-10-12")], [act("2026-10-12")], wellness(), T) is None


def test_training_case_after_overshoot():
    events = [ev("2026-10-10", load=50), ev("2026-10-12")]
    acts = [act("2026-10-10", load=120)]
    assert c.assess(events, acts, wellness(), T)["case"] == "training"


def test_lifestyle_case_when_sleep_short():
    r = c.assess([ev("2026-10-12")], [], wellness(bad={"2026-10-12"}), T)
    assert r["case"] == "lifestyle" and "sleep, stress and alcohol" in r["line"]


def test_ask_when_neither():
    r = c.assess([ev("2026-10-12")], [], wellness(), T)
    assert r["case"] == "ask" and r["line"].endswith("?")


def test_strength_matches_workout_type():
    assert c.assess([ev("2026-10-12", typ="WeightTraining")], [act("2026-10-12", typ="Workout")],
                    wellness(), T) is None


def test_once_a_week(tmp_path):
    assert c.due(tmp_path, T)
    c.mark(tmp_path, T, "ask")
    assert not c.due(tmp_path, date(2026, 10, 20))
    assert c.due(tmp_path, date(2026, 10, 21))


def test_overshoot_after_the_miss_is_not_the_cause():
    events = [ev("2026-10-11"), ev("2026-10-12", load=50)]
    acts = [act("2026-10-12", load=200)]
    assert c.assess(events, acts, wellness(), T)["case"] == "ask"


def test_long_strength_is_not_an_overshoot():
    assert not c.overshoot(ev("x", typ="WeightTraining", load=0, mins=40),
                           act("x", typ="WeightTraining", load=19, mins=56))
