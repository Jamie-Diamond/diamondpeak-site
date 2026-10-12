"""T6 drift: on the pedals, and only while building durability."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
def test_t6_drift_on_the_pedals_and_only_while_building():
    """Jamie's bug, 12 Oct 2026: a coasting-inflated gravel ride drift told him not to add
    volume in his off-season."""
    import watchdog_checks as wc
    # steady power, HR drifting up 10% in the second half, with coasting (0 W) in it
    w = [200] * 1000 + [0] * 300 + [200] * 1000
    hr = [130] * 1000 + [125] * 300 + [143] * 1000
    v = wc.on_pedal_decoupling(w, hr)
    assert v is not None and 8.5 < v < 9.5                 # coasting ignored
    assert wc.on_pedal_decoupling([0] * 2000, [130] * 2000) is None
    assert set(wc.NO_DURABILITY_WEEKS) >= {"offseason", "post_race"}
    from datetime import date
    acts = [{"type": "Ride", "moving_time": 3600, "start_date_local": "2026-10-10T08:00",
             "icu_intensity": 65, "decoupling": 14.9, "decoupling_on_pedal": 4.0, "id": "a"}]
    assert wc.t6_decoupling(acts, date(2026, 10, 12)) is None   # on-pedal wins
