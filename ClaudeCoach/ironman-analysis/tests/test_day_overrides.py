"""Tests for lib/day_overrides.py — the coach-directed day-rule override register.

The register is the only thing standing between "the coach asked for this" and a hard
invariant breach, so its failure mode matters more than its happy path: every way it can
be wrong must grant NOTHING.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import day_overrides as do                                    # noqa: E402


def _reg(tmp_path, blob):
    p = tmp_path / "athletes" / "jamie" / "reference" / "day-rules-overrides.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(blob if isinstance(blob, str) else json.dumps(blob))
    return tmp_path


class TestLoad:
    def test_a_missing_register_is_empty_not_an_error(self, tmp_path):
        assert do.load("jamie", tmp_path) == {}

    def test_corrupt_json_grants_nothing(self, tmp_path):
        assert do.load("jamie", _reg(tmp_path, "{not json")) == {}

    def test_a_valid_entry_loads(self, tmp_path):
        base = _reg(tmp_path, {"swim:2026-07-29": "Coach-directed 27 Jul."})
        assert do.load("jamie", base) == {"swim:2026-07-29": "Coach-directed 27 Jul."}

    def test_a_note_key_is_allowed_and_ignored(self, tmp_path):
        base = _reg(tmp_path, {"_note": "how this works", "swim:2026-07-29": "x"})
        assert list(do.load("jamie", base)) == ["swim:2026-07-29"]

    def test_bad_keys_and_empty_notes_are_dropped(self, tmp_path):
        base = _reg(tmp_path, {"swim:2026-07-29": "  ", "run:29-07-2026": "x",
                               "yoga:2026-07-29": "x", "swim": "x", 
                               "swim:2026-07-30": 1})
        assert do.load("jamie", base) == {}

    def test_a_top_level_list_grants_nothing(self, tmp_path):
        assert do.load("jamie", _reg(tmp_path, ["swim:2026-07-29"])) == {}


class TestRecord:
    def test_record_then_load_round_trips(self, tmp_path):
        do.record("jamie", tmp_path, "swim", date(2026, 7, 29), "Coach-directed.")
        assert do.load("jamie", tmp_path) == {"swim:2026-07-29": "Coach-directed."}

    def test_an_icu_ride_type_maps_onto_the_bike_family(self, tmp_path):
        assert do.record("jamie", tmp_path, "GravelRide", "2026-07-30", "x") == \
               "bike:2026-07-30"

    def test_a_note_is_mandatory(self, tmp_path):
        # An override with no provenance is indistinguishable from silently widening the
        # rule, which is the failure this whole mechanism exists to prevent.
        try:
            do.record("jamie", tmp_path, "swim", "2026-07-29", "   ")
        except ValueError:
            return
        raise AssertionError("an override without a note must be refused")

    def test_an_unknown_sport_is_refused(self, tmp_path):
        try:
            do.record("jamie", tmp_path, "yoga", "2026-07-29", "x")
        except ValueError:
            return
        raise AssertionError("an unknown family must be refused")

    def test_recording_preserves_existing_entries(self, tmp_path):
        do.record("jamie", tmp_path, "swim", "2026-07-29", "first")
        do.record("jamie", tmp_path, "run", "2026-07-31", "second")
        assert sorted(do.load("jamie", tmp_path)) == ["run:2026-07-31", "swim:2026-07-29"]

    def test_it_recovers_from_a_corrupt_register_rather_than_crashing_the_bot(self, tmp_path):
        _reg(tmp_path, "{broken")
        do.record("jamie", tmp_path, "swim", "2026-07-29", "x")
        assert do.load("jamie", tmp_path) == {"swim:2026-07-29": "x"}


def test_the_family_list_matches_the_validator():
    # Two modules read the same register; a divergence here silently unexcuses a whole
    # sport.
    sys.path.insert(0, str(REPO / "ironman-analysis"))
    from primitives.validate_plan import _SPORT_FAMILY
    assert set(_SPORT_FAMILY.values()) == set(do.FAMILIES)
