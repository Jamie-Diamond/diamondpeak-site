"""Tests for lib/strength_builder.py: planned strength sessions built from the exercise
library (config/exercise-library.json), their Intervals.icu text, and the hand-over file
Peak's player reads (athletes/<slug>/strength-plan.json, strength_log.planned()).

  python3 -m pytest -q ClaudeCoach/lib/test_strength_builder.py
"""
from __future__ import annotations

import copy
import itertools
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strength_builder as sb  # noqa: E402

LIB = sb.load_library()
EX = LIB["exercises"]
DAY = "2026-10-19"                                   # a Monday
MODULES = (["swim", "bike", "run"], ["run"], ["bike"], ["swim"], [])
PHASES = ("base", "build", "specific", "peak", "taper", "offseason")
CORE = {"anti_extension", "anti_rotation", "lateral_core"}


def _build(profile=None, **kw):
    args = dict(date=DAY, phase="build", slot="A", tier="C", sports=["swim", "bike", "run"], level=2)
    args.update(kw)
    return sb.build(profile or {}, LIB, **args)


def _items(s):
    return [it for p in s["parts"] for it in p["items"]]


def _ids(s, with_alt=True):
    out = []
    for it in _items(s):
        out.append(it["ex"])
        if with_alt and it.get("alt"):
            out.append(it["alt"])
    return out


def _grid(**fixed):
    """Every module x slot x phase x level x tier, minus what the caller fixes."""
    keys = dict(sports=MODULES, slot=("A", "B"), phase=PHASES, level=(1, 2, 3), tier=("A", "B", "C"))
    for k, v in fixed.items():
        keys[k] = (v,)
    for combo in itertools.product(*keys.values()):
        yield dict(zip(keys, combo))


def _knee_example():
    return copy.deepcopy(next(e for e in LIB["issues"]["examples"] if e["id"] == "knee_ligament_rehab"))


# ── the rules ──

def test_tier_c_never_gets_gym_kit():
    kit = set(LIB["equipment_tiers"]["C"]["kit"])
    for kw in _grid(tier="C"):
        s = _build(**kw)
        for ex in _ids(s):
            assert set(EX[ex]["equipment"]) <= kit, (kw, ex, EX[ex]["equipment"])


def test_every_ex_and_alt_id_is_in_the_library():
    knee = _knee_example()["issue"]
    for kw in _grid():
        for prof in ({}, {"injuries": [knee]}):
            s = _build(prof, **kw)
            if s:
                assert all(ex in EX for ex in _ids(s)), kw
                assert all(it["alt"] != it["ex"] for it in _items(s))


def test_triathlete_gets_swim_shoulder_work():
    for tier, slot, phase in itertools.product("ABC", "AB", PHASES):
        s = _build(tier=tier, slot=slot, phase=phase)
        pats = {EX[x]["pattern"] for x in _ids(s, with_alt=False)}
        assert "scapular_cuff" in pats or "straight_arm_pulldown" in _ids(s, with_alt=False), (tier, slot, phase)


def test_balance_in_every_session():
    for kw in _grid():
        s = _build(**kw)
        assert "single_leg_balance" in _ids(s, with_alt=False), kw


def test_push_and_pull_in_every_session():
    for kw in _grid():
        pats = {EX[x]["pattern"] for x in _ids(_build(**kw), with_alt=False)}
        assert pats & {"push_horizontal", "push_vertical"}, kw
        assert pats & {"pull_horizontal", "pull_vertical", "scapular_cuff"}, kw


@pytest.mark.parametrize("status", ["rehab", "active"])
def test_no_plyos_with_a_current_knee_rehab_issue(status):
    prof = {"injuries": [{"location": "right knee", "description": "ACL repair", "status": status}]}
    for kw in _grid(level=2, tier="A"):
        s = _build(prof, **kw)
        assert not any(EX[x]["pattern"] == "plyometric" for x in _ids(s)), kw
        assert not any(p["title"] == "Power" for p in s["parts"])


def test_managed_knee_or_cleared_rehab_still_gets_plyos_in_build():
    for inj in ({"location": "knee", "status": "managed"},
                {"location": "knee", "status": "resolved"}):
        s = _build({"injuries": [inj]}, sports=["run"], level=2)
        assert any(EX[x]["pattern"] == "plyometric" for x in _ids(s, with_alt=False)), inj


def test_knee_rehab_example_gets_only_an_upper_body_and_core_top_up():
    ex = _knee_example()
    prof = {"injuries": [ex["issue"]]}
    s = _build(prof, tier="A", sports=["bike"], slot="A")
    assert s["kind"] == "top_up"
    pats = {EX[x]["pattern"] for x in _ids(s)}
    assert pats <= {"push_horizontal", "push_vertical", "pull_horizontal", "pull_vertical",
                    "scapular_cuff"} | CORE, pats
    assert pats & {"push_horizontal", "push_vertical"} and pats & {"pull_horizontal", "pull_vertical"}
    # The library's own expected output for this example.
    want = [it["ex"] for it in ex["builder_output"]["top_up"]["items"]]
    assert _ids(s, with_alt=False) == want
    assert s["minutes"] == ex["builder_output"]["top_up"]["minutes"]
    assert {"bone_impact", "grip"} <= set(s["gaps"])
    # Asked again with that top-up already in the week: nothing more to add.
    assert _build(prof, tier="A", sports=["bike"], slot="B", week_logged=[s]) is None


def test_protocol_items_count_toward_coverage_and_the_sport_block_skips_them():
    ank = copy.deepcopy(LIB["issues"]["examples"][1]["issue"])     # outside the cap
    s = _build({"injuries": [ank]}, sports=["run"], tier="C", slot="A")
    assert s["kind"] == "library"                                    # cap not used
    sport = next(p for p in s["parts"] if p["title"] == "Sport")
    sport_ids = [it["ex"] for it in sport["items"]]
    assert "calf_raise" not in sport_ids and "bent_knee_calf_raise" in sport_ids
    assert any("Ankle:" in (it.get("note") or "") for it in _items(s))   # cautions shown


def test_two_counted_protocol_sessions_fill_the_cap_for_any_issue():
    sh = {"location": "shoulder", "status": "rehab", "source": "physio",
          "protocol": {"summary": "x", "sessions": [
              {"title": "Cuff", "per_week": 2, "items": [{"ex": "band_external_rotation"},
                                                         {"ex": "band_pull_apart"}]}]}}
    s = _build({"injuries": [sh]}, tier="A", sports=["run"])
    assert s["kind"] == "top_up"
    assert "band_external_rotation" not in _ids(s, with_alt=False)
    assert _build({"injuries": [sh]}, tier="A", sports=["run"], cap=3)["kind"] == "library"


def test_avoid_list_is_never_prescribed():
    inj = {"location": "low back", "status": "managed", "avoid": ["back_squat", "romanian_deadlift"]}
    s = _build({"injuries": [inj]}, tier="A", slot="A")
    assert not {"back_squat", "romanian_deadlift"} & set(_ids(s))


def test_a_caution_that_says_swap_swaps_while_the_issue_is_active():
    s = _build({"injuries": [{"location": "lower back", "status": "active"}]}, tier="A", slot="A")
    assert "back_squat" not in _ids(s, with_alt=False)              # "Swap to goblet squat ..."
    managed = _build({"injuries": [{"location": "lower back", "status": "managed"}]}, tier="A", slot="A")
    sq = next(it for it in _items(managed) if it["ex"] == "back_squat")
    assert sq["note"].startswith("Back: Swap")                      # managed: shown, not swapped


def test_plyos_start_in_build_not_base():
    assert not any(p["title"] == "Power" for p in _build(phase="base")["parts"])
    assert any(p["title"] == "Power" for p in _build(phase="build")["parts"])


def test_new_lifter_gets_the_learning_block_and_calf_lead_in():
    s = _build(sports=["bike"], level=1, tier="B")
    main = next(p for p in s["parts"] if p["title"] == "Main")["items"]
    assert all(it["rpe"] == "6-7" and it["sets"] == 2 for it in main)
    assert not any(p["title"] == "Power" for p in s["parts"])
    assert {"calf_raise", "bent_knee_calf_raise"} <= set(_ids(s, with_alt=False))
    # Four weeks in, a level-1 cyclist is past both.
    later = _build({"strength_programme": {"started": "2026-09-01"}}, sports=["bike"], level=1, tier="B")
    assert any(p["title"] == "Power" for p in later["parts"])


def test_no_band_stored_drops_band_work():
    s = _build({"strength_programme": {"kit": []}}, tier="C")
    assert not any("band" in EX[x]["equipment"] or "mini_band" in EX[x]["equipment"] for x in _ids(s))


def test_no_strength_in_race_week():
    assert _build({"race_date": "2026-10-25"}, phase="taper") is None
    assert _build({"race_date": "2026-11-01"}, phase="taper") is not None
    assert _build(phase="race_week") is None


def test_taper_and_peak_are_lighter_than_build():
    def sets(s):
        return sum(it["sets"] for it in _items(s))
    b, p, t = (_build(phase=ph, tier="A") for ph in ("build", "peak", "taper"))
    assert sets(t) < sets(p) < sets(b)
    assert t["minutes"] < b["minutes"]


def test_minutes_trims_but_keeps_balance():
    full = _build(tier="A")
    short = _build(tier="A", minutes=25)
    assert short["minutes"] < full["minutes"] and short["minutes"] <= 30
    assert "single_leg_balance" in _ids(short, with_alt=False)
    warm = next(p for p in short["parts"] if p["title"] == "Warm-up")["items"]
    assert sb.RAMP in warm[-1]["note"]                              # ramp-up note survives the trim
    assert sum(sb.RAMP in (it.get("note") or "") for it in warm) == 1


def test_hand_over_shape_and_determinism():
    s = _build(tier="B", slot="B", sports=["run"])
    assert s == _build(tier="B", slot="B", sports=["run"])
    assert s["date"] == DAY and s["title"].startswith("Strength B") and isinstance(s["minutes"], int)
    for p in s["parts"]:
        assert p["order"] in ("suggested", "any") and p["items"]
        for it in p["items"]:
            assert isinstance(it["sets"], int) and isinstance(it["reps"], str)
            assert isinstance(it["rest_s"], int) and "alt" in it


# ── describe() ──

def test_describe_one_line_per_exercise():
    s = {"title": "Strength A · Gym", "minutes": 40, "parts": [
        {"title": "Main", "order": "suggested",
         "items": [{"ex": "back_squat", "sets": 3, "reps": "5", "rest_s": 120, "alt": None}]}]}
    assert "Back squat 3 × 5 · rest 2 min" in sb.describe(s, LIB).splitlines()


def test_describe_is_safe_for_intervals_and_readable():
    s = _build(tier="A")
    text = sb.describe(s, LIB)
    lines = text.splitlines()
    assert lines[0] == "Strength A · Gym · about 40 min"
    assert not any(ln.lstrip().startswith("-") for ln in lines)    # never a workout step
    assert "RPE" not in text                                        # athletes tap easy/right/hard
    assert "1a Back squat 3 × 4-6 · rest 1 min" in lines
    assert any(ln.startswith("Leg swings 10-15") for ln in lines)   # warm-up, one set: no "1 ×"
    taper = sb.describe(_build(tier="A", phase="taper"), LIB).splitlines()
    assert "1a Back squat 1 × 3 · rest 1 min" in taper              # a single working set says so
    names = [EX[it["ex"]]["name"] for it in _items(s)]
    assert all(any(n in ln for ln in lines) for n in names)         # every exercise listed
    assert sb.describe(None, LIB) == ""


def test_rest_wording():
    assert [sb._rest(x) for x in (0, 30, 60, 90, 120, 150)] == ["", "30 s", "1 min", "90 s", "2 min", "2.5 min"]


# ── write_plan() ──

def test_write_plan_keeps_other_events_and_drops_old_ones(tmp_path):
    today = date.today()
    old = {"date": (today - timedelta(days=15)).isoformat(), "title": "Old", "parts": []}
    other = {"date": (today + timedelta(days=2)).isoformat(), "title": "Other", "parts": []}
    p = tmp_path / "athletes" / "fred" / "strength-plan.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"111": old, "222": other}))
    s = _build(date=(today + timedelta(days=1)).isoformat())
    plan = sb.write_plan("fred", 333, s, base=tmp_path)
    assert set(plan) == {"222", "333"}
    assert json.loads(p.read_text()) == plan
    assert not list(p.parent.glob("*.tmp"))                         # atomic: nothing left behind
    sb.write_plan("fred", "333", None, base=tmp_path)               # None removes that event
    assert set(json.loads(p.read_text())) == {"222"}


def test_write_plan_is_what_peaks_player_reads(tmp_path):
    import strength_log
    s = _build(date=date.today().isoformat())
    sb.write_plan("jamie", 145440926, s, base=tmp_path)
    got = strength_log.planned("jamie", base=tmp_path)["145440926"]
    assert got["parts"] == s["parts"] and got["date"] == s["date"]


def test_write_plan_refuses_a_bad_slug_or_undated_session(tmp_path):
    with pytest.raises(ValueError):
        sb.write_plan("../x", 1, _build(), base=tmp_path)
    with pytest.raises(ValueError):
        sb.write_plan("fred", 1, {"title": "x", "parts": []}, base=tmp_path)
