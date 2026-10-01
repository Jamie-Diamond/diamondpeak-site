"""Next-race setup by the coach bot (Jamie, 30 Sep 2026): "the coach bot can do this with
the athlete as one event finishes" - block weeks, Fitness targets and blueprint for the
next A-race, previewed with the athlete and written only on their OK."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402

TODAY = date(2026, 9, 30)
BRIGHTON = {"race_name": "Brighton Marathon", "race_date": "2027-04-04", "race_goal": "sub 3",
            "plan_start": "2026-11-02",
            "day_rules": {"swim_days": ["Tue"], "bike_days": ["Thu", "Sat"]},
            "ctl_targets": {"maintenance_ctl_band": [65, 75], "phase_ctl": {"peak": 112}}}


def _cfg(tmp_path, entry):
    p = tmp_path / "athletes.json"
    p.write_text(json.dumps({"x": entry}))
    return p


def test_brighton_preview_matches_what_jamie_agreed(tmp_path):
    out = pt.race_setup("x", today=TODAY, path=_cfg(tmp_path, BRIGHTON))
    assert out["applied"] is False and "--apply" in out["next"]
    assert out["level"] == 1 and out["plan_start"] == "2026-11-02" and out["weeks"] == 22
    assert out["phase_tss"] == {"base_end_week": 6, "build_end_week": 14,
                                "specific_end_week": 17, "peak_end_week": 19}
    assert out["fitness_targets"]["phase_ctl"] == {"base": 84, "build": 94,
                                                   "specific": 100, "peak": 105}
    assert out["fitness_targets"]["race_min"] == 94
    assert out["weekly_volume"] == [80, 110]


def test_apply_writes_the_block_and_keeps_other_targets(tmp_path):
    p = _cfg(tmp_path, BRIGHTON)
    out = pt.race_setup("x", apply=True, today=TODAY, path=p)
    c = json.loads(p.read_text())["x"]
    assert out["applied"] and c["ctl_targets"]["phase_ctl"]["peak"] == 105
    assert c["ctl_targets"]["maintenance_ctl_band"] == [65, 75]
    assert c["phase_tss"]["peak_end_week"] == 19


def test_a_tri_uses_its_total_level(tmp_path):
    e = {"race_name": "70.3 Weymouth", "race_date": "2027-09-12", "race_goal": "5:00"}
    out = pt.race_setup("x", today=TODAY, path=_cfg(tmp_path, e))
    assert out["level"] == 2 and out["fitness_targets"]["into_taper"] == 65
    assert out["plan_start"] == "2026-10-05"            # next Monday


def test_a_past_race_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        pt.race_setup("x", today=TODAY, path=_cfg(tmp_path, dict(BRIGHTON, race_date="2026-09-19")))


def test_the_bot_is_told_to_preview_then_apply_on_ok(tmp_path):
    b = pt.fitness_choice_prompt_block("x", "Jamie", path=_cfg(tmp_path, BRIGHTON))
    assert "race-setup --athlete x" in b and "ONLY on their OK" in b


def test_the_athletes_own_number_wins_and_is_kept(tmp_path):
    p = _cfg(tmp_path, BRIGHTON)
    out = pt.race_setup("x", apply=True, today=TODAY, path=p, into_taper=95)
    assert out["fitness_targets"]["phase_ctl"]["peak"] == 95
    again = pt.race_setup("x", today=TODAY, path=p)               # re-run, no override
    assert again["fitness_targets"]["phase_ctl"]["peak"] == 95
