"""Smoke tests for scripts/generate-plan.py:build_prompt (remediation WS D/F).

The prompt builder is otherwise only checked by golden-diff during development;
these hermetic cases guard the structural invariants that unit-less code drifts
on. Synthetic cfg/profile only — no athlete files, no network (ctl is injected).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
GEN_PLAN = REPO / "scripts" / "generate-plan.py"


@pytest.fixture(scope="module")
def gp():
    spec = importlib.util.spec_from_file_location("gen_plan", GEN_PLAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Athlete shapes the event-agnostic load path must all handle (WS D):
MULTISPORT_CONFIGURED = (
    {"name": "Tri", "race_distance": "Full Ironman",
     "plan_start": "2026-04-27",
     "phase_tss": {"base_end_week": 6, "build_end_week": 10,
                   "specific_end_week": 14, "peak_end_week": 17},
     "ctl_targets": {"race_min": 95,
                     "phase_ctl": {"base": 70, "build": 82, "specific": 90, "peak": 95}}},
    {"race_distance": "Full Ironman", "race_date": "2026-09-19",
     "max_hours_per_week": 15, "ftp_watts": 290},
)
CYCLING_NO_BASIS = (
    {"name": "Survivor", "race_distance": "Sportive"},
    {"race_distance": "Sportive", "race_date": "2026-08-29", "max_hours_per_week": 4},
)
CYCLING_WITH_TARGET = (  # the "just add config" case — must NOT NameError
    {"name": "Racer", "race_distance": "Sportive", "ctl_targets": {"race_min": 45}},
    {"race_distance": "Sportive", "race_date": "2026-08-29", "max_hours_per_week": 8},
)


class TestBuildPromptSmoke:
    def test_all_shapes_build_without_error(self, gp):
        for cfg, prof in (MULTISPORT_CONFIGURED, CYCLING_NO_BASIS, CYCLING_WITH_TARGET):
            for replan in (False, True):
                p = gp.build_prompt("smoke", cfg, prof, ctl_today=40.0, replan=replan)
                assert isinstance(p, str) and len(p) > 1000

    def test_no_basis_cycling_has_no_load_accountability(self, gp):
        # Survival Sportive with no CTL basis → no authoritative target, no nag.
        cfg, prof = CYCLING_NO_BASIS
        p = gp.build_prompt("smoke", cfg, prof, ctl_today=10.0)
        assert "## LOAD ACCOUNTABILITY" not in p
        assert "NO CTL target" in p          # honest readiness framing instead
        assert "CEILING" in p                # availability-capped

    def test_cycling_with_target_gets_load_accountability(self, gp):
        # A configured CTL basis on a cycling event reaches the load block — and
        # must not crash on the ctl_base/build/spec/peak that used to live only
        # inside the multisport branch (the latent NameError WS D's hoist fixed).
        cfg, prof = CYCLING_WITH_TARGET
        p = gp.build_prompt("smoke", cfg, prof, ctl_today=20.0)
        assert "## LOAD ACCOUNTABILITY" in p

    def test_multisport_configured_gets_load_accountability(self, gp):
        cfg, prof = MULTISPORT_CONFIGURED
        p = gp.build_prompt("smoke", cfg, prof, ctl_today=70.0)
        assert "## LOAD ACCOUNTABILITY" in p
        assert "Specific" in p               # full periodisation present
