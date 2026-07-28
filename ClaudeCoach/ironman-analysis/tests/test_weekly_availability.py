"""Tests for lib/weekly_availability.py and the cap precedence it feeds.

The mechanism's whole value is that it does NOT guess. So the tests that matter are
the ones where a figure must NOT be produced: no declaration, a declaration for a
different week, an undated legacy file, a nonsense number. Every one of those must
resolve to None and let the caller fall back to config — because the alternative is a
number nobody confirmed silently deciding how hard an athlete trains.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "ironman-analysis"))

import weekly_availability as wa                               # noqa: E402
import plan_builder as pb                                      # noqa: E402

MON = "2026-08-03"          # a Monday
NEXT_MON = "2026-08-10"
PHASE_PEAK = {"name": "Peak", "tss_ceiling": 1083}
PHASE_TAPER = {"name": "Taper"}


def _athlete(tmp_path, *, max_hours=15, avail=None):
    d = tmp_path / "athletes" / "jamie"
    (d / "reference").mkdir(parents=True, exist_ok=True)
    (d / "profile.json").write_text(json.dumps(
        {} if max_hours is None else {"max_hours_per_week": max_hours}))
    if avail is not None:
        (d / wa.FILENAME).write_text(avail if isinstance(avail, str) else json.dumps(avail))
    return tmp_path


@pytest.fixture
def based(tmp_path, monkeypatch):
    """Point both modules' default BASE at a throwaway tree."""
    monkeypatch.setattr(wa, "BASE", tmp_path)
    monkeypatch.setattr(pb, "BASE", tmp_path)
    return tmp_path


class TestResolution:
    def test_a_missing_file_yields_no_declaration(self, based):
        _athlete(based)
        assert wa.for_week("jamie", MON) is None
        assert wa.hours_for_week("jamie", MON) is None
        assert wa.has_declaration("jamie", MON) is False

    def test_unparseable_file_degrades_to_none_rather_than_raising(self, based):
        _athlete(based, avail="{not json")
        assert wa.hours_for_week("jamie", MON) is None

    def test_a_declaration_resolves_for_its_own_week(self, based):
        _athlete(based, avail={"declarations": [{"week_start": MON, "hours": 17.5}]})
        assert wa.hours_for_week("jamie", MON) == 17.5

    def test_any_day_inside_the_week_resolves_the_same_record(self, based):
        _athlete(based, avail={"declarations": [{"week_start": MON, "hours": 17.5}]})
        assert wa.hours_for_week("jamie", "2026-08-06") == 17.5      # the Thursday

    def test_a_declaration_does_not_leak_into_the_next_week(self, based):
        """The single most important test in the file. A figure that persists silently
        into a week the athlete never confirmed is exactly max_hours_per_week's defect."""
        _athlete(based, avail={"declarations": [{"week_start": MON, "hours": 17.5}]})
        assert wa.hours_for_week("jamie", NEXT_MON) is None

    def test_no_week_asked_means_no_declaration_applies(self, based):
        """macro_projection.py:338's ceiling lambda discards its week and so reaches
        the resolver with None. One real week's hours must not bound every projected
        week."""
        _athlete(based, avail={"declarations": [{"week_start": MON, "hours": 17.5}]})
        assert wa.hours_for_week("jamie", None) is None

    def test_a_single_dated_object_is_accepted(self, based):
        _athlete(based, avail={"week_start": MON, "hours": 14})
        assert wa.hours_for_week("jamie", MON) == 14.0
        assert wa.hours_for_week("jamie", NEXT_MON) is None

    def test_a_corrupt_week_start_is_skipped_not_fatal(self, based):
        _athlete(based, avail={"declarations": [{"week_start": "not-a-date", "hours": 20},
                                               {"week_start": MON, "hours": 17.5}]})
        assert wa.hours_for_week("jamie", MON) == 17.5


class TestLegacyFlatFile:
    """The pre-existing Phase 5a shape: day-shape keys, no dates, no hours."""

    def test_it_still_supplies_day_shape(self, based):
        _athlete(based, avail={"unavailable_days": ["Wed"], "run_days": ["Tue", "Sat"]})
        assert wa.day_shape("jamie", MON) == {"unavailable_days": ["Wed"],
                                              "run_days": ["Tue", "Sat"]}

    def test_it_can_never_supply_hours(self, based):
        _athlete(based, avail={"unavailable_days": ["Wed"], "hours": 25})
        assert wa.hours_for_week("jamie", MON) is None

    def test_a_dated_declaration_supplies_only_its_own_day_keys(self, based):
        _athlete(based, avail={"declarations": [
            {"week_start": MON, "hours": 12, "unavailable_days": ["Thu", "Fri"]}]})
        assert wa.day_shape("jamie", MON) == {"unavailable_days": ["Thu", "Fri"]}
        assert wa.day_shape("jamie", NEXT_MON) is None


class TestBadNumbers:
    @pytest.mark.parametrize("bad", [0, 0.5, 41, 400, "lots", None, ""])
    def test_an_out_of_band_or_non_numeric_figure_yields_none(self, based, bad):
        _athlete(based, avail={"declarations": [{"week_start": MON, "hours": bad}]})
        assert wa.hours_for_week("jamie", MON) is None

    def test_writing_an_out_of_band_figure_is_refused(self, based):
        _athlete(based)
        with pytest.raises(ValueError):
            wa.record("jamie", MON, hours=400)
        assert wa.hours_for_week("jamie", MON) is None

    def test_a_bad_figure_falls_back_to_config_rather_than_removing_the_ceiling(self, based):
        _athlete(based, max_hours=15,
                 avail={"declarations": [{"week_start": MON, "hours": 400}]})
        assert pb._weekly_tss_cap("jamie", PHASE_PEAK, week_start=MON) == 778.0


class TestRecord:
    def test_it_round_trips(self, based):
        _athlete(based)
        wa.record("jamie", MON, hours=17.5, constraints="away Thu-Fri", source="telegram-reply")
        d = wa.for_week("jamie", MON)
        assert d["hours"] == 17.5 and d["constraints"] == "away Thu-Fri"
        assert d["source"] == "telegram-reply" and d["declared_at"]

    def test_a_non_monday_week_start_is_normalised(self, based):
        _athlete(based)
        wa.record("jamie", "2026-08-06", hours=12)
        assert wa.for_week("jamie", MON)["week_start"] == MON

    def test_re_declaring_the_same_week_replaces_rather_than_duplicates(self, based):
        _athlete(based)
        wa.record("jamie", MON, hours=12)
        wa.record("jamie", MON, hours=17.5)
        raw = wa.load_raw("jamie")
        assert len(raw["declarations"]) == 1
        assert wa.hours_for_week("jamie", MON) == 17.5

    def test_history_is_pruned_to_the_retention_window(self, based):
        _athlete(based)
        first = date.fromisoformat(MON)
        for i in range(wa._KEEP + 4):
            wa.record("jamie", first + timedelta(days=7 * i), hours=10 + i)
        assert len(wa.load_raw("jamie")["declarations"]) == wa._KEEP

    def test_the_current_and_next_week_both_survive_a_new_declaration(self, based):
        """plan_audit audits the current AND next week every morning, so both must
        still resolve after the Sunday build writes a new one."""
        _athlete(based)
        wa.record("jamie", MON, hours=12)
        wa.record("jamie", NEXT_MON, hours=17.5)
        assert wa.hours_for_week("jamie", MON) == 12
        assert wa.hours_for_week("jamie", NEXT_MON) == 17.5

    def test_a_legacy_flat_file_is_preserved_not_destroyed(self, based):
        _athlete(based, avail={"unavailable_days": ["Wed"]})
        wa.record("jamie", MON, hours=12)
        assert wa.load_raw("jamie")["legacy_day_shape"] == {"unavailable_days": ["Wed"]}


class TestCapPrecedence:
    def test_no_declaration_uses_the_config_fallback(self, based):
        _athlete(based, max_hours=15)
        assert pb._weekly_tss_cap("jamie", PHASE_PEAK, week_start=MON) == 778.0
        assert pb.cap_source("jamie", PHASE_PEAK, week_start=MON) == "hours"

    def test_a_declaration_outranks_the_config_fallback(self, based):
        _athlete(based, max_hours=15, avail={"declarations": [{"week_start": MON, "hours": 17.5}]})
        assert pb._weekly_tss_cap("jamie", PHASE_PEAK, week_start=MON) == 907.0
        assert pb.cap_source("jamie", PHASE_PEAK, week_start=MON) == "declared"

    def test_the_declaration_does_not_move_another_week(self, based):
        _athlete(based, max_hours=15, avail={"declarations": [{"week_start": MON, "hours": 17.5}]})
        assert pb._weekly_tss_cap("jamie", PHASE_PEAK, week_start=NEXT_MON) == 778.0

    def test_the_week_less_caller_is_byte_identical_to_the_config_path(self, based):
        _athlete(based, max_hours=15, avail={"declarations": [{"week_start": MON, "hours": 17.5}]})
        assert pb._weekly_tss_cap("jamie", PHASE_PEAK) == 778.0

    def test_an_athlete_with_no_config_hours_falls_to_the_phase_ceiling(self, based):
        """Kathryn: max_hours_per_week is null by a permanent rule and must stay so
        absent a declaration."""
        _athlete(based, max_hours=None)
        assert pb._weekly_tss_cap("jamie", PHASE_PEAK, week_start=MON) == 1083.0
        assert pb.cap_source("jamie", PHASE_PEAK, week_start=MON) == "phase"

    def test_a_declaration_gives_an_uncapped_athlete_a_ceiling_for_that_week_only(self, based):
        """Her rule forbids reinstating a FIXED cap and requires building to the hours
        she confirms each week — a one-week figure is the second, not the first."""
        _athlete(based, max_hours=None,
                 avail={"declarations": [{"week_start": MON, "hours": 9}]})
        assert pb._weekly_tss_cap("jamie", PHASE_PEAK, week_start=MON) == 467.0
        assert pb._weekly_tss_cap("jamie", PHASE_PEAK, week_start=NEXT_MON) == 1083.0

    def test_a_taper_still_carries_no_ceiling_even_with_a_declaration(self, based):
        _athlete(based, max_hours=15, avail={"declarations": [{"week_start": MON, "hours": 17.5}]})
        assert pb._weekly_tss_cap("jamie", PHASE_TAPER, week_start=MON) is None


class TestSundayAsk:
    def test_it_is_asked_when_no_declaration_exists(self, based):
        _athlete(based)
        assert "how many hours" in wa.sunday_hours_ask("jamie", MON).lower()

    def test_it_states_the_no_reply_fallback(self, based):
        _athlete(based, max_hours=15)
        assert "usual 15 hours" in wa.sunday_hours_ask("jamie", MON)

    def test_an_athlete_with_no_config_hours_gets_the_other_fallback_sentence(self, based):
        _athlete(based, max_hours=None)
        ask = wa.sunday_hours_ask("jamie", MON)
        assert "full week the plan calls for" in ask and "usual" not in ask

    def test_it_is_silent_once_the_athlete_has_answered(self, based):
        _athlete(based, avail={"declarations": [{"week_start": MON, "hours": 12}]})
        assert wa.sunday_hours_ask("jamie", MON) == ""

    def test_it_is_silent_while_the_illness_flag_is_active(self, based):
        _athlete(based)
        (based / "athletes" / "jamie" / "current-state.json").write_text(json.dumps(
            {"illness": {"condition": "chest infection", "status": "active",
                         "started": (date.today() - timedelta(days=2)).isoformat()}}))
        assert wa.sunday_hours_ask("jamie", MON) == ""

    def test_the_beginner_variant_carries_no_load_or_ceiling_jargon(self, based):
        """Calum's coaching_level is beginner: no TSS/IF/Load framing anywhere."""
        _athlete(based)
        ask = wa.sunday_hours_ask("jamie", MON, coaching_level="beginner").lower()
        for jargon in ("load", "tss", "ceiling", "intensity", "zone", "ftp"):
            assert f" {jargon} " not in f" {ask} "

    def test_an_unknown_level_falls_back_to_mid_rather_than_failing(self, based):
        _athlete(based)
        assert wa.sunday_hours_ask("jamie", MON, coaching_level="nonsense") == \
               wa.sunday_hours_ask("jamie", MON, coaching_level="mid")
