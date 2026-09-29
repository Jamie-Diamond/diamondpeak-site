"""Setting the NEXT A-race must not end post-race recovery or taper 27 weeks out
(Brighton Marathon set after IM Italy, 29 Sep 2026)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import plan_tools as pt  # noqa: E402


def _cfg(**kw):
    c = {"plan_start": "2026-04-27", "race_date": "2027-04-04",
         "phase_tss": {"base_end_week": 5, "build_end_week": 10,
                       "specific_end_week": 14, "peak_end_week": 19},
         "ctl_targets": {"race_min": 97, "phase_ctl": {"base": 85, "build": 95,
                                                       "specific": 105, "peak": 112}},
         "races": [{"name": "IM Italy", "date": "2026-09-19", "priority": "A"},
                   {"name": "Brighton Marathon", "date": "2027-04-04", "priority": "A"}]}
    c.update(kw)
    return c


def test_finished_a_race_still_drives_recovery():
    r = pt.required_tss(_cfg(), 95.3, today=date(2026, 9, 29))
    assert r["week_type"] == "post_race"
    assert r["race_date"] == "2026-09-19"
    assert pt.in_post_race_recovery(_cfg(), today=date(2026, 9, 29))


def test_never_tapers_months_out_on_a_stale_plan_start():
    cfg = _cfg(races=[])                      # no finished race to fall back on
    r = pt.required_tss(cfg, 95.3, today=date(2026, 9, 29))
    assert r.get("week_type") != "taper"
    assert "error" in r


def test_new_plan_start_ends_the_old_race_recovery():
    cfg = _cfg(plan_start="2026-11-02")
    r = pt.required_tss(cfg, 80.0, today=date(2026, 11, 10))
    assert r["week_type"] == "base"
