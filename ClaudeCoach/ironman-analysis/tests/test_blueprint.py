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

from primitives.blueprint import validate_blueprint, is_valid, SCHEMA_VERSION

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

    def test_stub_event_has_empty_distribution_but_still_valid(self, gb):
        prof = {**FIXTURE_PROFILE, "race_distance": "Sportive"}
        phases = self._phases(gb, date(2026, 5, 12))
        data = gb.build_blueprint_data("tester", prof, phases, 70.0, None)
        # Sportive has no DISTRIBUTION entry yet (WS D) — empty, but valid shape.
        assert all(p["distribution"] == {} for p in data["phases"])
        assert validate_blueprint(data) == []
