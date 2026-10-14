"""Post-A-race transition week: no quality, no bricks, no unsolicited push.

plan_audit only stopped the under-training alarm (fbc516f3); nothing stopped the builder
emitting VO2 / sweetspot / bricks inside the transition protocol.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "scripts"))
_spec = importlib.util.spec_from_file_location("stage1_plan", REPO / "scripts" / "stage1-plan.py")
s1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s1)
import plan_tools as pt  # noqa: E402

RACE = date(2026, 9, 20)
CFG = {"plan_start": "2026-05-04", "race_date": RACE.isoformat(), "race_name": "IM Italy",
       "race_distance": "Full Ironman",
       "phase_tss": {"base_end_week": 8, "build_end_week": 14, "peak_end_week": 18},
       "ctl_targets": {"phase_ctl": {"base": 85, "build": 100, "peak": 115}}}


def _proposal():
    return {"sessions": [
        {"date": "2026-09-29", "sport": "Bike", "name": "VO2 5x3", "segments": [
            {"minutes": 15, "zone": "Z1"}, {"minutes": 15, "zone": "Z5"}, {"minutes": 10, "zone": "Z1"}]},
        {"date": "2026-09-30", "sport": "Bike", "name": "Sweetspot 2x20", "segments": [
            {"minutes": 40, "zone": "Z3"}]},
        {"date": "2026-10-02", "sport": "Brick", "name": "Brick", "segments": [
            {"minutes": 60, "zone": "Z2"}, {"minutes": 20, "zone": "Z2"}]},
        {"date": "2026-10-03", "sport": "Run", "name": "Easy run", "segments": [
            {"minutes": 40, "zone": "Z2"}]},
    ]}


def test_wc_28_sep_is_a_post_race_week():
    brief = pt.required_tss(CFG, 100.0, today=date(2026, 9, 28))
    assert s1._post_race(brief)


def test_wc_28_sep_has_no_vo2_sweetspot_or_brick():
    brief = pt.required_tss(CFG, 100.0, today=date(2026, 9, 28))
    out = s1.post_race_gate(brief, _proposal())
    sessions = out["sessions"]
    assert not any(x["sport"] == "Brick" for x in sessions)
    for x in sessions:
        assert not any(w in x["name"].lower() for w in ("vo2", "sweetspot", "brick"))
        cut = 0.76 if x["sport"] == "Bike" else 0.85
        assert all((s1._seg_if(x["sport"], g) or 0) < cut for g in x["segments"])


def test_other_weeks_are_untouched():
    p = _proposal()
    assert s1.post_race_gate({"phase": "build", "week_type": "normal"}, p) == _proposal()


def test_post_race_push_needs_a_request():
    src = (REPO / "scripts" / "stage1-plan.py").read_text()
    assert "_post_race(brief) and not args.requested" in src
    assert s1.exit_code_for({"pushed": False, "held_post_race": True}, True) == 0
