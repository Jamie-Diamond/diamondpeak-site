"""Guardrail arming regression tests (methodology audit P0-3/P0-4, 5 Jul 2026).

The audit's central finding was that every deterministic safeguard ran advisory:
validate_week's ramp and TSS hard checks silently no-opped when their inputs
weren't passed, the prescription backstop was shadow-only, and the ankle gate
mis-read "file exists but no ankle block" as "not cleared" for every uninjured
athlete. These tests pin the armed behaviour so a refactor can never silently
disarm a gate again:

  1. armed hard checks FIRE on a breaching week;
  2. a check whose input is missing lands in report.skipped (fail-noisy),
     never a silent pass;
  3. _ankle_state is athlete-scoped: uninjured -> unrestricted, structured
     block -> verbatim, profile-listed injury without structured state ->
     FAIL CLOSED;
  4. the authoritative prompt binds (no modulate.py call path) and the
     backstop default is authoritative;
  5. tss_ceiling stays single-sourced in primitives.blueprint.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from primitives.validate_plan import validate_week
from primitives.blueprint import tss_ceiling, IF_TARGETS

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
DP = REPO / "scripts" / "daily-prescription.py"


@pytest.fixture(scope="module")
def dp():
    spec = importlib.util.spec_from_file_location("daily_prescription_g", DP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _week(load):
    return [{"start_date_local": "2026-07-06T00:00:00", "type": "Ride",
             "category": "WORKOUT", "load_target": load, "name": "big ride"}]


WS = date(2026, 7, 6)


class TestArmedChecksFire:
    def test_tss_cap_breach_is_hard(self):
        rep = validate_week(_week(900), WS, weekly_tss_cap=500.0)
        assert any(v.code == "weekly_tss_cap" and v.severity == "hard"
                   for v in rep.violations)

    def test_ramp_breach_is_hard(self):
        rep = validate_week(_week(900), WS, ctl_today=60.0, ramp_cap=5.0)
        assert any(v.code == "ctl_ramp" and v.severity == "hard"
                   for v in rep.violations)

    def test_compliant_week_is_clean_and_unskipped(self):
        rep = validate_week(_week(300), WS, weekly_tss_cap=500.0,
                            ctl_today=60.0, ramp_cap=5.0)
        assert rep.ok
        assert rep.skipped == []


class TestFailNoisy:
    def test_missing_tss_cap_is_recorded(self):
        rep = validate_week(_week(900), WS, ctl_today=60.0, ramp_cap=5.0)
        assert any("weekly_tss_cap" in s and "SKIPPED" in s for s in rep.skipped)

    def test_missing_ctl_is_recorded(self):
        rep = validate_week(_week(900), WS, weekly_tss_cap=500.0, ramp_cap=5.0)
        assert any("ctl_ramp" in s and "SKIPPED" in s for s in rep.skipped)

    def test_skipped_is_never_a_violation(self):
        # A skipped check must not block a plan — it must only be surfaced.
        rep = validate_week(_week(300), WS)
        assert rep.ok
        assert len(rep.skipped) >= 2


class TestAnkleStateScoping:
    """(pain, cleared) truth table — the Kathryn false-block class of bug."""

    def _write(self, tmp_path, slug, profile=None, state=None):
        adir = tmp_path / "athletes" / slug
        adir.mkdir(parents=True)
        if profile is not None:
            (adir / "profile.json").write_text(json.dumps(profile))
        if state is not None:
            (adir / "current-state.json").write_text(json.dumps(state))

    def test_uninjured_athlete_is_unrestricted(self, dp, monkeypatch, tmp_path):
        # State file EXISTS but has no ankle block (the exact shape that used to
        # return cleared=False and gate every quality run).
        self._write(tmp_path, "a", profile={"injuries": []},
                    state={"last_updated": "2026-07-05"})
        monkeypatch.setattr(dp, "BASE", tmp_path)
        assert dp._ankle_state("a") == (0, True)

    def test_structured_block_is_reported_verbatim(self, dp, monkeypatch, tmp_path):
        self._write(tmp_path, "a", profile={"injuries": ["ankle"]},
                    state={"ankle": {"pain_during": 3, "pain_next_morning": 1,
                                     "four_pain_free_weeks_reached": False}})
        monkeypatch.setattr(dp, "BASE", tmp_path)
        assert dp._ankle_state("a") == (3, False)

    def test_cleared_block(self, dp, monkeypatch, tmp_path):
        self._write(tmp_path, "a", profile={"injuries": ["ankle"]},
                    state={"ankle": {"pain_during": 0,
                                     "four_pain_free_weeks_reached": True}})
        monkeypatch.setattr(dp, "BASE", tmp_path)
        assert dp._ankle_state("a") == (0, True)

    def test_profile_injury_without_state_fails_closed(self, dp, monkeypatch, tmp_path):
        self._write(tmp_path, "a", profile={"injuries": ["ankle sprain"]},
                    state={"last_updated": "2026-07-05"})
        monkeypatch.setattr(dp, "BASE", tmp_path)
        monkeypatch.setattr(dp, "LOG_FILE", tmp_path / "p.log")
        assert dp._ankle_state("a") == (0, False)
        assert "failing CLOSED" in (tmp_path / "p.log").read_text()

    def test_missing_files_is_unrestricted(self, dp, monkeypatch, tmp_path):
        (tmp_path / "athletes" / "a").mkdir(parents=True)
        monkeypatch.setattr(dp, "BASE", tmp_path)
        assert dp._ankle_state("a") == (0, True)


class TestAuthoritativeBackstop:
    def test_default_mode_is_authoritative(self, dp, monkeypatch):
        monkeypatch.delenv("PRESCRIPTION_BACKSTOP", raising=False)
        assert dp._backstop_mode() == "authoritative"

    def test_shadow_and_off_still_selectable(self, dp, monkeypatch):
        monkeypatch.setenv("PRESCRIPTION_BACKSTOP", "shadow")
        assert dp._backstop_mode() == "shadow"
        monkeypatch.setenv("PRESCRIPTION_BACKSTOP", "off")
        assert dp._backstop_mode() == "off"

    def _rx(self):
        class RX:
            go = False; swapped_to_z2 = False; modified = False
            target_intensity = 0.65
            interval_count = None; interval_duration_min = None; recovery_min = None
            total_duration_min = 50
            applied_rules = ["R1"]; reasoning_trails = ["trail"]
            summary = "BLOCKED by R1 (ankle). Swap to easy or rest."
        return RX()

    def test_authoritative_prompt_binds(self, dp):
        blk = dp._engine_block_text(
            {"_name": "Tempo Run", "session_type": "run_quality",
             "total_duration_min": 50, "target_intensity": 1.0}, self._rx())
        p = dp.build_prompt("jamie", "Jamie", "Race", engine_block=blk)
        assert "ENGINE PRESCRIPTION (deterministic — BINDING)" in p
        assert "do NOT call modulate.py" in p
        assert "never a harder one" in p            # events-mismatch is tighten-only
        assert "Step 5 — Call the modulation engine" not in p

    def test_engine_block_is_tighten_only_on_heat(self, dp):
        blk = dp._engine_block_text(
            {"_name": "Tempo Run", "session_type": "run_quality",
             "total_duration_min": 50, "target_intensity": 1.0}, self._rx())
        assert "REDUCE" in blk
        assert "NEVER increase, restore, or un-block" in blk

    def test_legacy_prompt_unchanged_without_block(self, dp):
        p = dp.build_prompt("jamie", "Jamie", "Race")
        assert "ENGINE PRESCRIPTION" not in p
        assert "Step 5 — Call the modulation engine" in p


class TestTssCeilingSingleSource:
    def test_values(self):
        assert tss_ceiling(10, "Base 1") == round(10 * 100 * IF_TARGETS["base"] ** 2, 0)
        assert tss_ceiling(12, "Peak") == round(12 * 100 * IF_TARGETS["peak"] ** 2, 0)
        assert tss_ceiling(10, "Taper") is None

    def test_generate_blueprint_reuses_primitive(self):
        src = (REPO / "scripts" / "generate-blueprint.py").read_text()
        assert "def tss_ceiling" not in src   # moved to primitives.blueprint
        assert "tss_ceiling" in src           # still imported/used
