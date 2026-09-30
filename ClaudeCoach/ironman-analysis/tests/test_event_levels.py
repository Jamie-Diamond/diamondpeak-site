"""Event levels and bespoke events (Jamie, 30 Sep 2026).

"90k a week is a crazy target for most people ... make sure we work for a range of
fitnesses" across 5k/10k/half/marathon, sprint/olympic/70.3/Ironman, sportive and gravel;
and "if I say I have a 30k race ... blend it", "a triathlon with a 4k swim and 10k bike
... create a temp blueprint for bespoke events".
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402
import race_fitness as rf  # noqa: E402

ALL = ["5k", "10k", "half_marathon", "marathon", "sprint", "olympic", "70_3", "ironman",
       "sportive", "gravel"]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestTables:
    def test_every_event_has_four_ordered_levels(self):
        assert sorted(rf.events()) == sorted(ALL)
        for ev in ALL:
            rows = rf.level_table(ev)
            assert [r["level"] for r in rows] == [1, 2, 3, 4], ev
            fit = [r["fitness_at_taper"] for r in rows]
            assert all(lo <= hi for lo, hi in fit), ev
            assert fit == sorted(fit, reverse=True), f"{ev}: fitness must fall with level"
            goals = [r["goal_max_s"] for r in rows if r["goal_max_s"]]
            assert goals == sorted(goals), ev

    def test_a_four_hour_marathoner_is_not_given_sub3_mileage(self):
        l4 = rf.level_table("marathon")[3]
        assert l4["run_km"][1] <= 45
        assert rf.level_table("marathon")[0]["run_km"][0] >= 80

    def test_every_event_has_an_intensity_split_with_a_taper(self):
        dist = _load("gb", "scripts/generate-blueprint.py").DISTRIBUTION
        for ev in ALL:
            d = dist.get(rf._DIST_NAME[ev])
            assert d and d.get("taper") and d.get("base"), ev

    def test_blueprint_section_matches_the_data(self):
        render = _load("rel", "scripts/render-event-levels.py").render()
        assert render in (REPO / "blueprints" / "blueprint.md").read_text(), \
            "blueprint.md §4.5 is stale: run scripts/render-event-levels.py --write"


class TestNames:
    @pytest.mark.parametrize("name,key", [
        ("Brighton Marathon", "marathon"), ("Royal Parks Half Marathon", "half_marathon"),
        ("London 10k", "10k"), ("Parkrun 5k", "5k"), ("Sprint", "sprint"),
        ("Olympic", "olympic"), ("70.3 Emilia Romagna", "70_3"),
        ("IM Italy Emilia-Romagna", "ironman"), ("Gravel Race", "gravel"),
        ("Sportive — 134 km / 4,700 m", "sportive")])
    def test_names_map(self, name, key):
        assert rf.levels_key(name) == key

    def test_planner_and_generator_know_sprint_and_gravel(self):
        import session_library as sl
        from primitives.blueprint import event_key
        assert sl.event_key({"race_distance": "Sprint"}) == "sprint"
        assert sl.event_key({"race_name": "Gravel Race"}) == "sportive"
        assert event_key("Gravel Race") == "Sportive"
        assert event_key("Brighton Marathon") == "Marathon"


class TestPickingTheLevel:
    CFG = {"race_name": "Brighton Marathon", "race_date": "2027-04-04"}

    def test_goal_time_decides(self):
        assert rf.athlete_level(dict(self.CFG, race_goal="sub 3"), {}, "marathon")["level"] == 1
        assert rf.athlete_level(dict(self.CFG, race_goal="4:30"), {}, "marathon")["level"] == 4

    def test_a_stale_goal_from_another_race_is_ignored(self):
        prof = {"race_name": "IM Italy", "a_goal": "Sub 9:30", "run_threshold_pace_per_km": "4:02"}
        lv = rf.athlete_level(self.CFG, prof, "marathon")
        assert lv["source"] == "threshold" and lv["level"] == 2        # ~3:01 predicted

    def test_fitness_then_default(self):
        assert rf.athlete_level({}, {}, "ironman", fitness=100)["source"] == "current_fitness"
        assert rf.athlete_level({}, {}, "sportive")["level"] == rf.DEFAULT_LEVEL
        assert rf.athlete_level({"event_level": 1}, {}, "gravel")["label"] == "Competitive"

    @pytest.mark.parametrize("text,event,s", [
        ("Sub 19:30", "5k", 1170), ("sub 40", "10k", 2400), ("1:40", "half_marathon", 6000),
        ("Sub 9:30", "ironman", 34200), ("3:15:00", "marathon", 11700), ("Finish", "ironman", None)])
    def test_parse_goal(self, text, event, s):
        assert rf.parse_goal_s(text, event) == s


class TestBespoke:
    def test_a_30k_run_is_a_half_marathon_marathon_blend(self):
        ev = rf.bespoke_event("30k", run_km=30)
        assert ev["blended_from"]["a"] == "half_marathon" and ev["blended_from"]["b"] == "marathon"
        hm, m = rf.level_table("half_marathon")[1], rf.level_table("marathon")[1]
        l2 = ev["levels"][1]
        assert hm["run_km"][0] <= l2["run_km"][0] <= m["run_km"][0]
        assert hm["long_run_km"][1] <= l2["long_run_km"][1] <= m["long_run_km"][1]
        assert set(ev["distribution"]["peak"]) == {"Run", "Swim", "Bike"}
        assert "Half Marathon" in rf.describe_blend(ev) and "Marathon" in rf.describe_blend(ev)

    def test_a_4k_swim_10k_bike_is_a_swim_bike_temp_blueprint(self):
        ev = rf.bespoke_event("Swim-bike", swim_km=4, bike_km=10)
        assert ev["kind"] == "tri" and ev["sports"] == ["swim", "bike"]
        assert set(ev["distribution"]["peak"]) == {"Swim", "Bike"}
        assert ev["long_swim_m"] >= 4000
        assert "long_run_km" not in ev["levels"][0]

    def test_outside_the_tables_says_so(self):
        assert rf.bespoke_event("Ultra", run_km=50)["notes"]
        with pytest.raises(ValueError):
            rf.bespoke_event("Swim", swim_km=5)

    def test_it_is_used_for_the_race_it_was_made_for_only(self):
        ev = dict(rf.bespoke_event("30k", run_km=30), race_name="Town 30k")
        cfg = {"race_name": "Town 30k", "bespoke_event": ev}
        assert rf.event_def(cfg, "marathon") is ev
        assert rf.event_def(dict(cfg, race_name="Other race"), "marathon")["label"] == "Marathon"

    def test_cli_writes_and_clears(self, tmp_path):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"x": {"race_name": "Town 30k", "race_date": "2027-05-01"}}))
        out = pt.set_bespoke_event("x", run_km=30, path=p)
        assert "blend of" in out["blend"]
        assert json.loads(p.read_text())["x"]["bespoke_event"]["race_name"] == "Town 30k"
        pt.set_bespoke_event("x", clear=True, path=p)
        assert "bespoke_event" not in json.loads(p.read_text())["x"]

    def test_race_level_cli(self, tmp_path):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"x": {"race_name": "Brighton Marathon", "race_goal": "sub 3",
                                       "race_date": "2027-04-04"}}))
        out = pt.race_level("x", path=p)
        assert out["level"] == 1 and out["fitness_kind"] == "running"


def test_an_aquabike_is_a_swim_bike_blend_with_no_bricks():
    ev = rf.bespoke_event("Aquabike", swim_km=3.8, bike_km=100)
    assert ev["sports"] == ["swim", "bike"] and ev["blended_from"]["b"] == "70_3"
    assert set(ev["distribution"]["peak"]) == {"Swim", "Bike"}
    src = (REPO / "lib" / "session_library.py").read_text()
    assert 'bespoke and "run" not in (bespoke.get("sports") or [])' in src
