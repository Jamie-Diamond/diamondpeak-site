"""Tests for lib/illness.py — the structured illness/compromised flag — plus the
heat-surfacing gate in lib/heat.py.

The failure these exist to prevent: 26 Jul 2026, Kathryn (on antibiotics, recovering
from tonsillitis) rode 76 min and reported her fuelling; the reply opened "You rode
76min at zero carbs" and closed "works against you", never acknowledging she had
trained. Her illness was prose in current-state.md, which no prompt reads.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import heat            # noqa: E402
import illness         # noqa: E402
import engine          # noqa: E402

TODAY = date(2026, 7, 28)


@pytest.fixture
def athlete(monkeypatch, tmp_path):
    """Isolated athlete dir with no illness flag set. NEVER a real athlete's files."""
    monkeypatch.setattr(illness, "BASE", tmp_path)
    adir = tmp_path / "athletes" / "x"
    adir.mkdir(parents=True)
    (adir / "profile.json").write_text(json.dumps({"coaching_level": "mid"}))
    (adir / "current-state.json").write_text(json.dumps({"last_updated": "2026-07-20"}))
    return adir


class TestSchema:
    def test_no_flag_is_none_and_no_block(self, athlete):
        assert illness.state_from_dir(athlete, TODAY) is None
        assert illness.prompt_block_from_dir(athlete, TODAY) == ""
        assert illness.weekly_card_line("x", TODAY) == ""

    def test_set_and_read_back(self, athlete):
        st = illness.set_illness_in(athlete, condition="tonsillitis",
                                    started="2026-07-24",
                                    expected_until="2026-08-02",
                                    note="on antibiotics", today=TODAY)
        assert st["active"] and st["status"] == "active"
        assert st["training_gate"] == "none"
        assert st["days_in"] == 4
        raw = json.loads((athlete / "current-state.json").read_text())["illness"]
        assert raw["condition"] == "tonsillitis"
        assert raw["started"] == "2026-07-24"
        assert "logged" in raw

    def test_set_preserves_other_keys(self, athlete):
        (athlete / "current-state.json").write_text(json.dumps(
            {"menstrual_cycle": {"last_period_start": "2026-07-01"}, "weight_readings": [1]}))
        illness.set_illness_in(athlete, condition="cold", today=TODAY)
        st = json.loads((athlete / "current-state.json").read_text())
        assert st["menstrual_cycle"]["last_period_start"] == "2026-07-01"
        assert st["weight_readings"] == [1]

    def test_resolved_suppresses_nothing(self, athlete):
        illness.set_illness_in(athlete, condition="cold", status="resolved",
                              started="2026-07-24", today=TODAY)
        assert illness.state_from_dir(athlete, TODAY)["active"] is False
        assert illness.prompt_block_from_dir(athlete, TODAY) == ""

    def test_clear_marks_resolved_and_stops_suppressing(self, athlete):
        illness.set_illness_in(athlete, condition="flu", today=TODAY)
        assert illness.prompt_block_from_dir(athlete, TODAY) != ""
        assert illness.clear_illness_in(athlete, TODAY) is True
        assert illness.prompt_block_from_dir(athlete, TODAY) == ""
        raw = json.loads((athlete / "current-state.json").read_text())["illness"]
        assert raw["status"] == "resolved" and raw["resolved_on"] == TODAY.isoformat()
        assert illness.clear_illness_in(athlete, TODAY) is False

    def test_a_forgotten_flag_lapses_but_not_on_the_expiry_day(self, athlete):
        illness.set_illness_in(athlete, condition="flu", started="2026-07-01",
                              expected_until="2026-07-10", today=TODAY)
        # recovery slips: still suppressing the day after the guess expires
        assert illness.state_from_dir(athlete, date(2026, 7, 11))["active"] is True
        assert illness.state_from_dir(athlete, date(2026, 7, 11))["needs_review"] is True
        # ...but a forgotten flag must not soften the coaching for ever
        late = illness.state_from_dir(athlete, date(2026, 7, 20))
        assert late["active"] is False and late["lapsed"] is True

    def test_open_ended_flag_asks_for_a_review_but_keeps_suppressing(self, athlete):
        illness.set_illness_in(athlete, condition="cold", started="2026-07-01",
                              today=TODAY)
        st = illness.state_from_dir(athlete, TODAY)
        assert st["active"] is True and st["needs_review"] is True
        assert "STATUS CHECK DUE" in illness.prompt_block_from_dir(athlete, TODAY)

    def test_bad_input_raises_rather_than_writing_a_flag_that_never_lapses(self, athlete):
        with pytest.raises(ValueError):
            illness.set_illness_in(athlete, status="poorly", today=TODAY)
        with pytest.raises(ValueError):
            illness.set_illness_in(athlete, training_gate="no_running", today=TODAY)
        with pytest.raises(ValueError):
            illness.set_illness_in(athlete, started="2026-09-01", today=TODAY)
        with pytest.raises(ValueError):
            illness.set_illness_in(athlete, started="2026-07-24",
                                   expected_until="2026-07-01", today=TODAY)
        assert "illness" not in json.loads((athlete / "current-state.json").read_text())

    def test_block_without_a_start_date_is_dropped(self, athlete):
        (athlete / "current-state.json").write_text(json.dumps(
            {"illness": {"status": "active", "condition": "flu"}}))
        assert illness.state_from_dir(athlete, TODAY) is None


class TestSuppressionContent:
    @pytest.fixture
    def block(self, athlete):
        illness.set_illness_in(athlete, condition="tonsillitis", started="2026-07-26",
                               note="on antibiotics", today=TODAY)
        return illness.prompt_block_from_dir(athlete, TODAY, first_name="Kathryn")

    def test_acknowledgement_is_required(self, block):
        assert "ACKNOWLEDGE THE TRAINING FIRST" in block
        assert "Never open on a number they fell short on" in block

    def test_fuelling_and_compliance_are_suppressed(self, block):
        assert "fuelling / carb-intake flags" in block
        assert "plan-adherence and compliance criticism" in block
        assert "SUSPENDED, not deleted" in block

    def test_safety_is_not_suppressed(self, block):
        assert "STILL FULLY IN FORCE" in block
        assert "injury hard-gates" in block
        assert "load ceilings" in block
        assert "medical escalation" in block
        assert "pain gate" in block

    def test_the_flag_alone_does_not_reduce_the_plan(self, block):
        assert "TRAINING GATE: none set" in block
        assert "the blueprint still governs" in block

    def test_gates_only_reduce_the_plan_when_set(self, athlete):
        illness.set_illness_in(athlete, condition="flu", training_gate="no_quality",
                              today=TODAY)
        b = illness.prompt_block_from_dir(athlete, TODAY)
        assert "no quality work" in b and "Z1–2 only" in b
        illness.set_illness_in(athlete, condition="flu", training_gate="no_training",
                              today=TODAY)
        assert "no training while this flag is active" in \
            illness.prompt_block_from_dir(athlete, TODAY)

    def test_a_direct_question_is_still_answered(self, block):
        assert "If they ASK about fuelling or compliance, answer straight" in block

    def test_data_is_still_recorded(self, block):
        assert "Log the data exactly as normal" in block

    def test_weekly_card_carries_it_once(self, athlete):
        illness.set_illness_in(athlete, condition="tonsillitis", started="2026-07-26",
                               today=TODAY)
        line = illness.weekly_card_line("x", TODAY)
        assert "ACTIVE" in line and "ONCE" in line


class TestConversationalCapture:
    def test_recognises_the_real_case(self):
        assert illness.looks_like_illness_statement(
            "I've got tonsillitis, doc put me on a 7 day course of antibiotics")

    def test_parses_it(self):
        p = illness.parse_illness_message(
            "I've got tonsillitis, doc put me on a 7 day course of antibiotics", TODAY)
        assert p["condition"] == "tonsillitis"
        assert p["status"] == "active"
        assert p["started"] == TODAY.isoformat()
        assert p["expected_until"] == "2026-08-04"

    def test_recovering_and_resolved(self):
        assert illness.parse_illness_message("still recovering from the flu", TODAY)["status"] \
            == "recovering"
        assert illness.parse_illness_message("all better now after that cold", TODAY)["status"] \
            == "resolved"

    def test_since_yesterday_and_n_days(self):
        assert illness.parse_illness_message("ill since yesterday", TODAY)["started"] \
            == "2026-07-27"
        assert illness.parse_illness_message("had a cold for 3 days", TODAY)["started"] \
            == "2026-07-25"

    @pytest.mark.parametrize("text", [
        "how should I train if I get ill?",
        "not ill, just tired",
        "the weather was cold",
        "planning the run",
        "",
    ])
    def test_does_not_fire_on_non_statements(self, text):
        assert illness.looks_like_illness_statement(text) is False


class TestEngineIntegration:
    """The chat path — the surface that produced the verbatim failure."""

    @pytest.fixture
    def sp(self, athlete):
        f = athlete / "system_prompt.txt"
        f.write_text("You are ClaudeCoach.")
        return f

    def _prompt(self, sp, athlete_name="Kathryn"):
        return engine.build_prompt(
            "Rode 76min, no carbs", [], engine.system_prompt_with_level(sp),
            athlete_name, "Phase: Build",
            illness_block=engine.load_illness_block(sp, athlete_name))

    def test_unset_prompt_is_byte_identical_to_no_block(self, sp):
        """No flag => the illness work adds NOTHING to the prompt."""
        assert engine.load_illness_block(sp, "Kathryn") == ""
        with_helper = self._prompt(sp)
        without = engine.build_prompt("Rode 76min, no carbs", [],
                                      engine.system_prompt_with_level(sp),
                                      "Kathryn", "Phase: Build")
        assert with_helper == without

    def test_set_flag_reaches_the_chat_prompt(self, sp, athlete):
        illness.set_illness_in(athlete, condition="tonsillitis",
                               started="2026-07-26", note="on antibiotics")
        p = self._prompt(sp)
        assert "ILLNESS / COMPROMISED STATE — ACTIVE" in p
        assert "ACKNOWLEDGE THE TRAINING FIRST" in p
        assert "fuelling / carb-intake flags" in p
        # and the fuelling rule it must outrank is still present, so the ordering
        # (illness block last) is what does the work
        assert p.index("ILLNESS / COMPROMISED STATE") > p.index("TRAINING-NUMBER ACCURACY")

    def test_session_fingerprint_rotates_when_the_flag_changes(self, sp, athlete):
        """The bug that would have made this ticket cosmetic: --resume never
        re-injects the rule blocks, so a state change MUST rotate the session."""
        before = engine._prompt_fingerprint(sp)
        illness.set_illness_in(athlete, condition="tonsillitis", started="2026-07-26")
        during = engine._prompt_fingerprint(sp)
        assert during and during != before
        illness.clear_illness_in(athlete)
        assert engine._prompt_fingerprint(sp) not in ("", during)

    def test_engine_survives_a_corrupt_state_file(self, sp, athlete):
        (athlete / "current-state.json").write_text("{not json")
        assert engine.load_illness_block(sp, "Kathryn") == ""


class TestHeatSurfacingGate:
    """heat_silent gates SURFACING only. The dose model must be untouched."""

    @pytest.fixture
    def blueprint(self, monkeypatch, tmp_path):
        monkeypatch.setattr(heat, "BASE", tmp_path)
        ref = tmp_path / "athletes" / "k" / "reference"
        ref.mkdir(parents=True)
        (ref / "training-blueprint.json").write_text(json.dumps(
            {"env_protocols": {"heat": {"active": True, "starts": "2026-01-01"}}}))
        return ref

    def test_unset_is_unchanged(self, blueprint):
        st = heat.state("k", {})
        assert st["active"] and st["in_protocol_window"]
        assert st["silent"] is False and st["surface"] is True
        assert heat.surfacing_allowed("k", {}) is True

    def test_silent_gates_surfacing_but_not_the_model(self, blueprint):
        st = heat.state("k", {"heat_silent": True})
        assert st["surface"] is False and st["silent"] is True
        # active must NOT be touched: activity-watcher credits ambient exposure off it
        assert st["active"] is True and st["in_protocol_window"] is True
        assert heat.surfacing_allowed("k", {"heat_silent": True}) is False

    def test_before_the_window_nothing_surfaces_anyway(self, monkeypatch, tmp_path):
        monkeypatch.setattr(heat, "BASE", tmp_path)
        ref = tmp_path / "athletes" / "k" / "reference"
        ref.mkdir(parents=True)
        (ref / "training-blueprint.json").write_text(json.dumps(
            {"env_protocols": {"heat": {"active": True, "starts": "2099-01-01"}}}))
        assert heat.state("k", {})["surface"] is False

    def test_calum_kill_switch_behaviour_is_unchanged(self, blueprint):
        st = heat.state("k", {"heat_protocol": False})
        assert st == {"active": False, "starts": None, "in_protocol_window": False,
                      "maintenance": False, "silent": False, "surface": False}

    def test_maintenance_key_is_unaffected_by_silence(self, blueprint):
        assert heat.state("k", {"heat_maintenance": True, "heat_silent": True})[
            "maintenance"] is True


class TestHeatModelUnchanged:
    """The dose curve, multipliers and score must be bit-identical to main's heat.py."""

    @pytest.fixture(scope="class")
    def old_heat(self, tmp_path_factory):
        import subprocess
        d = tmp_path_factory.mktemp("oldheat")
        src = subprocess.run(
            ["git", "-C", str(REPO.parent), "show", "main:ClaudeCoach/lib/heat.py"],
            capture_output=True, text=True, check=True).stdout
        f = d / "heat_old.py"
        f.write_text(src)
        spec = importlib.util.spec_from_file_location("heat_old", str(f))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_base_dose_identical(self, old_heat):
        for mins in range(0, 300, 5):
            assert heat.base_dose(mins) == old_heat.base_dose(mins), mins

    def test_multipliers_identical(self, old_heat):
        for t in (18, 22, 25, 28, 30, 34, 38, 42):
            for hr in (None, 110, 130, 150, 170):
                for dp in (None, 8, 12, 16, 20, 24):
                    for mins in (30, 60, 210):
                        a = heat.dose_multipliers(t, avg_hr=hr, dew_point_c=dp,
                                                  mins=mins, tss=mins)
                        b = old_heat.dose_multipliers(t, avg_hr=hr, dew_point_c=dp,
                                                      mins=mins, tss=mins)
                        assert a == b, (t, hr, dp, mins)

    def test_constants_identical(self, old_heat):
        names = [n for n in dir(old_heat)
                 if n.isupper() and isinstance(getattr(old_heat, n), (int, float, str))]
        assert names
        for n in names:
            assert getattr(heat, n) == getattr(old_heat, n), n

    def test_score_identical_on_a_synthetic_log(self, monkeypatch, tmp_path, old_heat):
        adir = tmp_path / "athletes" / "k"
        adir.mkdir(parents=True)
        (adir / "heat-log.json").write_text(json.dumps([
            {"date": "2026-07-10", "dose": 1.0},
            {"date": "2026-07-14"},
            {"date": "2026-07-20", "dose": 1.6},
            {"date": "2026-07-26", "dose": 0.5},
        ]))
        monkeypatch.setattr(heat, "BASE", tmp_path)
        monkeypatch.setattr(old_heat, "BASE", tmp_path)
        for d in (date(2026, 7, 15), date(2026, 7, 21), TODAY, date(2026, 8, 30)):
            assert heat.acclimation_score("k", d) == old_heat.acclimation_score("k", d), d
