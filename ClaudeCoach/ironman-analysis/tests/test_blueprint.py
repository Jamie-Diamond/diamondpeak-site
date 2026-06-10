"""Tests for the structured training-blueprint sidecar (remediation WS B).

Covers:
    - primitives/blueprint.py:validate_blueprint (shape contract)
    - generate-blueprint.py:build_blueprint_data end-to-end against a fixture
      profile, validated through the same contract the script enforces.
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

from primitives.blueprint import (
    validate_blueprint, is_valid, SCHEMA_VERSION, canonical_phases, current_phase,
    event_sports, is_multisport, event_key, EVENT_SPORTS, CYCLING_EVENTS,
    resolve_phases, phase_structure, assign_dates,
)

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
GEN_BLUEPRINT = REPO / "scripts" / "generate-blueprint.py"


def _load_gen_blueprint():
    spec = importlib.util.spec_from_file_location("gen_blueprint", GEN_BLUEPRINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gb():
    return _load_gen_blueprint()


def _good_blueprint() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "slug": "tester",
        "generated": "2026-05-12",
        "event_type": "Full Ironman",
        "race_date": "2026-09-19",
        "phases": [
            {"name": "Base", "family": "base", "start": "2026-05-12",
             "end": "2026-06-22", "weeks": 6},
            {"name": "Peak", "family": "peak", "start": "2026-06-23",
             "end": "2026-07-06", "weeks": 2},
        ],
        "tests": [{"type": "ftp", "label": "FTP Baseline", "date": "2026-05-12"}],
    }


class TestValidateBlueprint:
    def test_good_blueprint_passes(self):
        assert validate_blueprint(_good_blueprint()) == []
        assert is_valid(_good_blueprint())

    def test_not_a_dict(self):
        assert validate_blueprint([]) == ["blueprint must be a dict"]

    def test_missing_top_level_key(self):
        bp = _good_blueprint()
        del bp["event_type"]
        assert any("event_type" in e for e in validate_blueprint(bp))

    def test_empty_phases(self):
        bp = _good_blueprint()
        bp["phases"] = []
        assert any("non-empty list" in e for e in validate_blueprint(bp))

    def test_phase_missing_key(self):
        bp = _good_blueprint()
        del bp["phases"][0]["weeks"]
        assert any("phase[0] missing key: weeks" in e for e in validate_blueprint(bp))

    def test_invalid_family(self):
        bp = _good_blueprint()
        bp["phases"][0]["family"] = "recovery"
        assert any("family invalid" in e for e in validate_blueprint(bp))

    def test_bad_date(self):
        bp = _good_blueprint()
        bp["phases"][0]["start"] = "12/05/2026"
        assert any("not an ISO date" in e for e in validate_blueprint(bp))

    def test_end_before_start(self):
        bp = _good_blueprint()
        bp["phases"][0]["end"] = "2026-04-01"
        assert any("precedes start" in e for e in validate_blueprint(bp))

    def test_wrong_schema_version(self):
        bp = _good_blueprint()
        bp["schema_version"] = 99
        assert any("schema_version" in e for e in validate_blueprint(bp))


FIXTURE_PROFILE = {
    "name": "Test Athlete",
    "slug": "tester",
    "race_name": "IM Test",
    "race_date": "2026-09-19",
    "race_distance": "Full Ironman",
    "max_hours_per_week": 15,
    "ftp_watts": 300,
    "swim_css_per_100m": 95,
    "course_type": "rolling",
    "race_conditions": "hot",
}


class TestBuildBlueprintData:
    def _phases(self, gb, start: date):
        weeks = 18
        phases = gb.phase_structure(weeks)
        phases = gb.assign_dates(phases, start)
        return phases

    def test_output_validates(self, gb):
        phases = self._phases(gb, date(2026, 5, 12))
        data = gb.build_blueprint_data("tester", FIXTURE_PROFILE, phases, 79.0, None)
        assert validate_blueprint(data) == []

    def test_phase_content_populated_for_full_ironman(self, gb):
        phases = self._phases(gb, date(2026, 5, 12))
        data = gb.build_blueprint_data("tester", FIXTURE_PROFILE, phases, 79.0, None)
        base = next(p for p in data["phases"] if p["family"] == "base")
        assert base["tss_ceiling"] == 634          # 15 * 100 * 0.65^2
        assert base["if_target"] == 0.65
        assert base["distribution"].get("Bike", "").startswith("80%")
        assert "g CHO/hr" in base["fuelling"]
        assert base["brick_min"] == "1"

    def test_heat_protocol_structured(self, gb):
        phases = self._phases(gb, date(2026, 5, 12))
        data = gb.build_blueprint_data("tester", FIXTURE_PROFILE, phases, 79.0, None)
        heat = data["env_protocols"]["heat"]
        assert heat["active"] is True
        # 4 weeks before race day
        assert heat["starts"] == (date(2026, 9, 19) - timedelta(weeks=4)).isoformat()

    def test_dates_serialised_as_iso_strings(self, gb):
        phases = self._phases(gb, date(2026, 5, 12))
        data = gb.build_blueprint_data("tester", FIXTURE_PROFILE, phases, None, None)
        assert all(isinstance(p["start"], str) for p in data["phases"])
        # round-trips through json
        import json
        assert validate_blueprint(json.loads(json.dumps(data))) == []

    def test_unknown_event_has_empty_distribution_but_still_valid(self, gb):
        # A genuinely-unsupported event (no DISTRIBUTION entry) → empty, valid shape.
        prof = {**FIXTURE_PROFILE, "race_distance": "Marathon"}
        phases = self._phases(gb, date(2026, 5, 12))
        data = gb.build_blueprint_data("tester", prof, phases, 70.0, None)
        assert all(p["distribution"] == {} for p in data["phases"])
        assert validate_blueprint(data) == []


SPORTIVE_PROFILE = {
    "name": "Cyclist", "slug": "cyc", "race_name": "Gran Fondo",
    "race_date": "2026-08-29", "race_distance": "Sportive",
    "max_hours_per_week": 8, "ftp_watts": 260, "course_type": "hilly",
}


class TestSportiveProfile:
    """WS D — cycling events: bike-only, FTP-only tests, no bricks."""

    def _phases(self, gb):
        ph = gb.phase_structure(14)
        ph = gb.assign_dates(ph, date(2026, 5, 26))
        return ph

    def test_event_sports_and_normalisation(self, gb):
        assert gb.event_sports("Sportive") == ["bike"]
        assert gb.event_sports("Gravel") == ["bike"]
        assert gb.event_sports("Full Ironman") == ["swim", "bike", "run"]
        assert gb._event_key("Gravel") == "Sportive"     # cycling synonyms share content
        assert gb._event_key("70.3") == "70.3"

    def test_bike_only_distribution_and_no_bricks(self, gb):
        data = gb.build_blueprint_data("cyc", SPORTIVE_PROFILE, self._phases(gb), 60.0, None)
        assert data["sports"] == ["bike"]
        base = next(p for p in data["phases"] if p["family"] == "base")
        assert set(base["distribution"].keys()) == {"Bike"}   # no Swim/Run
        assert base["brick_min"] is None                       # bricks N/A for bike-only
        assert "rides" in base["fuelling"]

    def test_ftp_only_tests(self, gb, monkeypatch):
        # Scheduling is OFF by default (see TestPerformanceTestPolicy); this
        # exercises the scheduling LOGIC with it switched on.
        monkeypatch.setattr(gb, "SCHEDULE_PERFORMANCE_TESTS", True)
        data = gb.build_blueprint_data("cyc", SPORTIVE_PROFILE, self._phases(gb), 60.0, None)
        test_types = {t["type"] for t in data["tests"]}
        assert test_types == {"ftp"}                           # no lthr (run) / css (swim)

    def test_validates(self, gb):
        data = gb.build_blueprint_data("cyc", SPORTIVE_PROFILE, self._phases(gb), 60.0, None)
        assert validate_blueprint(data) == []

    def test_render_marks_bricks_not_applicable(self, gb):
        md = gb.render_blueprint("cyc", SPORTIVE_PROFILE, self._phases(gb), 60.0, None, None)
        assert "single-discipline event" in md


# Canonical phase config (jamie-shaped: includes a specific phase).
JAMIE_PHASE_TSS = {"base_end_week": 5, "build_end_week": 10,
                   "specific_end_week": 14, "peak_end_week": 17}


class TestCanonicalPhases:
    def test_jamie_shape_five_phases(self):
        ph = canonical_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, date(2026, 9, 19))
        assert [p["name"] for p in ph] == ["Base", "Build", "Specific", "Peak", "Taper"]
        assert [p["family"] for p in ph] == ["base", "build", "specific", "peak", "taper"]

    def test_windows_anchored_to_plan_start(self):
        ph = canonical_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, date(2026, 9, 19))
        base = ph[0]
        assert base["start"] == date(2026, 4, 27)
        assert base["end"] == date(2026, 5, 31)      # plan_start + 5w - 1d
        assert base["weeks"] == 5
        # contiguous: each phase starts the day after the previous ends
        for a, b in zip(ph, ph[1:]):
            assert b["start"] == a["end"] + timedelta(days=1)
        assert ph[-1]["end"] == date(2026, 9, 19)    # taper ends on race day

    def test_no_specific_when_unconfigured(self):
        tss = {"base_end_week": 8, "build_end_week": 14, "peak_end_week": 17}
        ph = canonical_phases(date(2026, 5, 4), tss, date(2026, 9, 20))
        assert [p["name"] for p in ph] == ["Base", "Build", "Peak", "Taper"]

    def test_empty_when_no_config(self):
        assert canonical_phases(None, JAMIE_PHASE_TSS, date(2026, 9, 19)) == []
        assert canonical_phases(date(2026, 4, 27), None, date(2026, 9, 19)) == []
        assert canonical_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, None) == []


class TestResolvePhases:
    """resolve_phases is the single phase-window resolver shared by the blueprint
    generator and the planner (WS D), so the two never disagree on Calum."""

    def test_configured_uses_canonical(self):
        # A configured athlete gets canonical_phases anchored to plan_start —
        # identical to calling canonical_phases directly.
        got = resolve_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, date(2026, 9, 19),
                             today=date(2026, 6, 8))
        want = canonical_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, date(2026, 9, 19))
        assert [(p["name"], p["start"], p["end"]) for p in got] == \
               [(p["name"], p["start"], p["end"]) for p in want]

    def test_unconfigured_derives_from_race_date(self):
        # No plan_start/phase_tss → auto-derive anchored to today, last phase
        # extended to race day (calum's path: ~12 weeks out → no Specific phase).
        today = date(2026, 6, 8)
        race  = date(2026, 8, 29)
        got = resolve_phases(None, None, race, today=today)
        assert got[0]["start"] == today
        assert got[-1]["end"] == race
        assert "Specific" not in [p["name"] for p in got]
        # contiguous
        for a, b in zip(got, got[1:]):
            assert b["start"] == a["end"] + timedelta(days=1)

    def test_matches_inline_autoderive(self):
        # Equivalent to the generator's old inline fallback, exactly.
        today, race = date(2026, 6, 8), date(2026, 8, 29)
        inline = assign_dates(phase_structure(int((race - today).days / 7)), today)
        inline[-1]["end"] = race
        got = resolve_phases(None, None, race, today=today)
        assert [(p["name"], p["start"], p["end"]) for p in got] == \
               [(p["name"], p["start"], p["end"]) for p in inline]


class TestCurrentPhase:
    def _bp(self):
        phases = canonical_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, date(2026, 9, 19))
        return {"phases": [{"name": p["name"], "family": p["family"],
                            "start": p["start"].isoformat(), "end": p["end"].isoformat()}
                           for p in phases]}

    def test_date_inside_window(self):
        # 2026-06-08 is in Build (2026-06-01..2026-07-05)
        assert current_phase(self._bp(), date(2026, 6, 8))["name"] == "Build"

    def test_window_boundaries_inclusive(self):
        assert current_phase(self._bp(), date(2026, 4, 27))["name"] == "Base"   # first day
        assert current_phase(self._bp(), date(2026, 5, 31))["name"] == "Base"   # last day of Base
        assert current_phase(self._bp(), date(2026, 6, 1))["name"] == "Build"   # first day of Build

    def test_before_first_clamps_to_first(self):
        assert current_phase(self._bp(), date(2026, 1, 1))["name"] == "Base"

    def test_after_last_clamps_to_last(self):
        assert current_phase(self._bp(), date(2027, 1, 1))["name"] == "Taper"

    def test_none_when_no_phases(self):
        assert current_phase({}, date(2026, 6, 8)) is None
        assert current_phase({"phases": []}, date(2026, 6, 8)) is None


class TestSpecificPhaseContent:
    """A specific phase reuses build-family content (remediation 2026-06-07)."""

    def test_specific_phase_gets_own_content_and_validates(self, gb):
        # Superseded 2026-06-10 (Jamie sign-off, docs/specific-phase-proposal.md):
        # Specific no longer reuses Build content — it carries its own rows.
        phases = canonical_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, date(2026, 9, 19))
        data = gb.build_blueprint_data("jamie", FIXTURE_PROFILE, phases, 90.0, None)
        assert validate_blueprint(data) == []          # 'specific' is a valid family
        spec = next(p for p in data["phases"] if p["family"] == "specific")
        build = next(p for p in data["phases"] if p["family"] == "build")
        assert spec["if_target"] == 0.70               # between build 0.68 and peak 0.72
        assert spec["distribution"] != build["distribution"]
        assert spec["distribution"]["Bike"].startswith("72%")
        assert spec["brick_min"] == "2–3"
        assert spec["tss_ceiling"] > build["tss_ceiling"]   # higher IF -> higher ceiling

    def test_render_blueprint_handles_specific_without_crashing(self, gb):
        phases = canonical_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, date(2026, 9, 19))
        md = gb.render_blueprint("jamie", FIXTURE_PROFILE, phases, 90.0, None, None)
        assert "Specific" in md
        assert "| Specific |" in md                    # brick table row present


class TestCssSeconds:
    """swim_css_per_100m appears as both int seconds and 'm:ss' strings."""

    def test_mmss_string(self, gb):
        assert gb._css_seconds("1:39") == 99

    def test_integer_seconds(self, gb):
        assert gb._css_seconds(99) == 99

    def test_none_and_empty(self, gb):
        assert gb._css_seconds(None) is None
        assert gb._css_seconds("") is None

    def test_unparseable(self, gb):
        assert gb._css_seconds("fast") is None

    def test_render_with_mmss_css_does_not_crash(self, gb):
        prof = {**FIXTURE_PROFILE, "swim_css_per_100m": "1:39"}
        phases = canonical_phases(date(2026, 4, 27), JAMIE_PHASE_TSS, date(2026, 9, 19))
        md = gb.render_blueprint("jamie", prof, phases, 90.0, None, None)
        assert "**CSS:** 1:39/100m" in md


class TestEventSports:
    """The event→sports partition is the single source shared by the planner
    (multisport-vs-cycling branch) and the blueprint generator (tests, bricks,
    distribution rows). Lock its behaviour and self-consistency (WS D)."""

    def test_triathlon_events_are_multisport(self):
        for ev in ("Full Ironman", "70.3"):
            assert event_sports(ev) == ["swim", "bike", "run"]
            assert is_multisport(ev) is True

    def test_cycling_events_are_bike_only(self):
        for ev in ("Sportive", "Gravel"):
            assert event_sports(ev) == ["bike"]
            assert is_multisport(ev) is False

    def test_unknown_event_defaults_to_triathlon(self):
        # Conservative default: an unrecognised event is treated as full
        # triathlon (don't silently drop swim/run from someone's plan).
        assert event_sports("Marathon") == ["swim", "bike", "run"]
        assert is_multisport("Marathon") is True
        assert is_multisport("") is True

    def test_cycling_set_is_self_consistent(self):
        # Every CYCLING_EVENTS member must be bike-only and key to 'Sportive' —
        # the two sets agreeing is the invariant that fixed the Gran Fondo gap.
        for ev in CYCLING_EVENTS:
            assert event_sports(ev) == ["bike"], ev
            assert is_multisport(ev) is False, ev
            assert event_key(ev) == "Sportive", ev

    def test_event_key_passthrough_for_non_cycling(self):
        assert event_key("Full Ironman") == "Full Ironman"
        assert event_key("70.3") == "70.3"

    def test_roster_partition_matches_legacy_selector(self):
        # The live roster's race_distance values must produce the same
        # multisport/cycling split the old swim-or-run-threshold heuristic gave:
        # jamie (Full Ironman) + kathryn (70.3) multisport, calum (Sportive) not.
        assert is_multisport("Full Ironman") is True
        assert is_multisport("70.3") is True
        assert is_multisport("Sportive") is False


class TestBaselineAnchoring:
    """Regression guard: baseline tests anchor to PLAN START, never date.today().

    Anchoring baselines to the regeneration day re-dated FTP/CSS/LTHR baselines
    to 'today' on every run, so the activity-watcher nudged mid-plan athletes to
    'redo your baseline test today' each time the blueprint was regenerated
    (it spammed two live athletes on 2026-06-08). Baselines must sit at the plan
    start so a mid-plan athlete's are historical and never 'due'."""

    def _phases(self, gb):
        # A plan that started well in the past — a mid-plan athlete.
        return gb.canonical_phases(
            date(2026, 4, 27),
            {"base_end_week": 6, "build_end_week": 10,
             "specific_end_week": 14, "peak_end_week": 17},
            date(2026, 9, 19),
        )

    def test_baselines_anchor_to_plan_start_not_today(self, gb, monkeypatch):
        # Scheduling is OFF by default now; this guards the anchoring LOGIC with
        # it switched on (so re-enabling can never reintroduce the re-dating bug).
        monkeypatch.setattr(gb, "SCHEDULE_PERFORMANCE_TESTS", True)
        phases = self._phases(gb)
        plan_start = phases[0]["start"]
        evs = gb._test_events(phases, ["swim", "bike", "run"])
        baselines = [e for e in evs if "Baseline" in e["label"]]
        assert baselines, "expected FTP/LTHR/CSS baselines"
        for b in baselines:
            assert b["date"] == plan_start.isoformat(), b
            # And — the whole point — not stamped with the current date.
            assert b["date"] != date.today().isoformat(), b

    def test_no_phases_falls_back_to_today(self, gb, monkeypatch):
        # Defensive: with no phases (shouldn't happen) it must not crash.
        monkeypatch.setattr(gb, "SCHEDULE_PERFORMANCE_TESTS", True)
        evs = gb._test_events([], ["bike"])
        assert all("date" in e for e in evs)


class TestPerformanceTestPolicy:
    """Standing coach decision (2026-06-08): no system-scheduled FTP/LTHR/CSS
    field tests — thresholds come from intervals.icu. _test_events must be empty
    by default so nothing is scheduled, pushed to the calendar, or nudged."""

    def test_no_tests_scheduled_by_default(self, gb):
        phases = gb.canonical_phases(
            date(2026, 4, 27),
            {"base_end_week": 6, "build_end_week": 10,
             "specific_end_week": 14, "peak_end_week": 17},
            date(2026, 9, 19),
        )
        assert gb._test_events(phases, ["swim", "bike", "run"]) == []
        assert gb.SCHEDULE_PERFORMANCE_TESTS is False   # default stays off
