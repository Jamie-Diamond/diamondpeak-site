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


class TestDayRuleSingleSource:
    """day_rules in athletes.json is THE single source — it drives both the prompt's
    HARD rule lines and the validate_plan backstop, so they cannot drift (WS E)."""

    def test_renders_allowed_and_forbidden(self, gp):
        s = gp.hard_day_rule_lines({"swim_days": ["Tue", "Thu"],
                                    "bike_days": ["Fri", "Sat", "Sun"]})
        assert "SWIM RULE — HARD: Swims ONLY on Tuesday, Thursday." in s
        assert "CYCLING RULE — HARD: Bike sessions ONLY on Friday, Saturday, Sunday." in s
        assert "Monday" in s  # forbidden days listed

    def test_empty_without_rules(self, gp):
        assert gp.hard_day_rule_lines({}) == ""
        assert gp.hard_day_rule_lines(None) == ""

    def test_prompt_reflects_this_athletes_day_rules(self, gp):
        # Prove the prompt is generated from the athlete's own day_rules, not
        # hardcoded — so what the planner is told == what the validator enforces.
        cfg = {"name": "T", "race_distance": "Full Ironman", "plan_start": "2026-04-27",
               "phase_tss": {"base_end_week": 6, "build_end_week": 10,
                             "specific_end_week": 14, "peak_end_week": 17},
               "ctl_targets": {"race_min": 95,
                               "phase_ctl": {"base": 70, "build": 82, "specific": 90, "peak": 95}},
               "day_rules": {"swim_days": ["Mon", "Wed"], "bike_days": ["Sat"]}}
        prof = {"race_distance": "Full Ironman", "race_date": "2026-09-19", "max_hours_per_week": 15}
        p = gp.build_prompt("t", cfg, prof, ctl_today=70.0)
        assert "Swims ONLY on Monday, Wednesday" in p
        assert "Bike sessions ONLY on Saturday" in p

    def test_template_does_not_default_wednesday_to_strength(self, gp):
        # Regression: the model picked strength on Wed because the template OFFERED
        # "Strength or Run". Wed must lead with the run; strength is a capped extra.
        cfg = {"name": "T", "race_distance": "Full Ironman", "plan_start": "2026-04-27",
               "phase_tss": {"base_end_week": 6, "build_end_week": 10,
                             "specific_end_week": 14, "peak_end_week": 17},
               "ctl_targets": {"race_min": 95,
                               "phase_ctl": {"base": 70, "build": 82, "specific": 90, "peak": 95}},
               "day_rules": {"swim_days": ["Tue", "Thu"], "bike_days": ["Fri", "Sat", "Sun"],
                             "run_days": ["Tue", "Wed", "Sat", "Sun"], "strength_max": 2}}
        prof = {"race_distance": "Full Ironman", "race_date": "2026-09-19", "max_hours_per_week": 15}
        p = gp.build_prompt("t", cfg, prof, ctl_today=70.0)
        assert "Wednesday: Strength or Run" not in p          # bad option removed
        assert "Wednesday: Run (Z2)" in p                     # run-first
        assert "Thursday: Swim only" in p
        assert "never more than 2 strength" in p              # hard cap, from strength_max
        assert "Runs ONLY on Tuesday, Wednesday, Saturday, Sunday" in p   # run_days HARD line

    def test_no_day_rules_gives_flexible_template_not_jamie_pattern(self, gp):
        # An athlete WITHOUT day_rules must NOT inherit another athlete's day
        # pattern: no HARD day-rule lines, no Tue/Thu fallback, no day-pinned
        # skeleton, and no "bike locked to Fri–Sun" cross-training assertion.
        cfg = {"name": "Flex", "race_distance": "Full Ironman", "plan_start": "2026-04-27",
               "phase_tss": {"base_end_week": 6, "build_end_week": 10,
                             "specific_end_week": 14, "peak_end_week": 17},
               "ctl_targets": {"race_min": 95,
                               "phase_ctl": {"base": 70, "build": 82, "specific": 90, "peak": 95}}}
        prof = {"race_distance": "Full Ironman", "race_date": "2026-09-19", "max_hours_per_week": 12}
        p = gp.build_prompt("flex", cfg, prof, ctl_today=70.0)
        assert "RULE — HARD: Swims ONLY on" not in p
        assert "Swims on TUESDAY and THURSDAY only" not in p   # old fallback retired
        assert "- Tuesday: Swim (aerobic/CSS)" not in p        # Jamie skeleton absent
        assert "NO fixed training days" in p                   # flexible header present
        assert "bike is locked to Fri–Sun" not in p            # day-agnostic cross-training
        assert "no-Mon–Thu-cycling rule" not in p


class TestWeekdayLabelGuard:
    """Deterministic fix for the LLM's miscomputed weekday words (dates are right,
    the day-of-week drifts). 2026-06-29 is a Monday, so 'Sun 29 Jun' must become
    'Mon 29 Jun'; a correct label yields no change."""

    def test_corrects_wrong_weekday(self, gp):
        out = gp.corrected_weekday_name("Sun 29 Jun — Long run easy 90min", "2026-06-29")
        assert out == "Mon 29 Jun — Long run easy 90min"

    def test_no_change_when_correct(self, gp):
        # 2026-06-13 is a Saturday.
        assert gp.corrected_weekday_name("Sat 13 Jun — Long ride", "2026-06-13") is None

    def test_no_leading_weekday_is_left_alone(self, gp):
        assert gp.corrected_weekday_name("Long ride Z2 4hr", "2026-06-29") is None

    def test_handles_datetime_prefix(self, gp):
        out = gp.corrected_weekday_name("Tue 1 Jul — Bike threshold", "2026-07-01T06:00:00")
        assert out == "Wed 1 Jul — Bike threshold"   # 1 Jul 2026 is a Wednesday


class TestBackstopValidate:
    """_backstop_validate is the function actually wired into the live send path
    (WS E warn mode). Exercise it directly — env-mode parsing, breach aggregation,
    and the log writes — so the safety net can't be silently dead. The network
    re-fetch is stubbed; we only test the wrapper logic, not icu_fetch."""

    def _patch(self, gp, monkeypatch, tmp_path, events):
        monkeypatch.setattr(gp, "_refetch_window_events", lambda slug, ws: events)
        log = tmp_path / "gp.log"
        monkeypatch.setattr(gp, "LOG_FILE", log)
        return log

    def _breaching_events(self):
        # A ride on Monday (forbidden by the day_rules below).
        return [{"start_date_local": "2026-06-15T00:00:00", "type": "Ride",
                 "load_target": 80, "category": "WORKOUT", "name": "Mon ride"}]

    def test_logs_breach_in_warn_mode(self, gp, monkeypatch, tmp_path):
        log = self._patch(gp, monkeypatch, tmp_path, self._breaching_events())
        monkeypatch.setenv("ENFORCE_VALIDATION", "warn")
        cfg = {"max_ctl_ramp_per_week": 4, "day_rules": {"bike_days": ["Fri", "Sat", "Sun"]}}
        res = gp._backstop_validate("smoke", cfg, ctl_today=40.0, replan=False)
        text = log.read_text()
        assert "VALIDATION (warn)" in text
        assert "ride_forbidden_day" in text
        assert "UNCHANGED" in text            # warn mode states it does not gate
        assert res["mode"] == "warn" and res["hard"]   # returns the breaches

    def test_block_mode_returns_hard_breaches(self, gp, monkeypatch, tmp_path):
        self._patch(gp, monkeypatch, tmp_path, self._breaching_events())
        monkeypatch.setenv("ENFORCE_VALIDATION", "block")
        cfg = {"max_ctl_ramp_per_week": 4, "day_rules": {"bike_days": ["Fri", "Sat", "Sun"]}}
        res = gp._backstop_validate("smoke", cfg, ctl_today=40.0, replan=False)
        assert res["mode"] == "block"
        assert any(v.code == "ride_forbidden_day" for v in res["hard"])

    def test_logs_clean_when_no_breach(self, gp, monkeypatch, tmp_path):
        clean = [{"start_date_local": "2026-06-19T00:00:00", "type": "Ride",
                  "load_target": 80, "category": "WORKOUT"}]   # Friday — allowed
        log = self._patch(gp, monkeypatch, tmp_path, clean)
        monkeypatch.setenv("ENFORCE_VALIDATION", "warn")
        cfg = {"max_ctl_ramp_per_week": 5, "day_rules": {"bike_days": ["Fri", "Sat", "Sun"]}}
        gp._backstop_validate("smoke", cfg, ctl_today=40.0, replan=False)
        assert "clean" in log.read_text()

    def test_off_switch_skips_entirely(self, gp, monkeypatch, tmp_path):
        log = self._patch(gp, monkeypatch, tmp_path, self._breaching_events())
        monkeypatch.setenv("ENFORCE_VALIDATION", "0")
        gp._backstop_validate("smoke", {"day_rules": {"bike_days": ["Fri"]}},
                              ctl_today=40.0, replan=False)
        assert not log.exists()               # nothing logged when disabled

    def test_soft_fails_on_refetch_error(self, gp, monkeypatch, tmp_path):
        def boom(slug, ws):
            raise RuntimeError("icu down")
        monkeypatch.setattr(gp, "_refetch_window_events", boom)
        monkeypatch.setattr(gp, "LOG_FILE", tmp_path / "gp.log")
        monkeypatch.setenv("ENFORCE_VALIDATION", "warn")
        # Must not raise — plan delivery can never break on a validator error.
        res = gp._backstop_validate("smoke", {}, ctl_today=40.0, replan=False)
        assert res["hard"] == []

    def test_coach_alert_logs_loudly_without_coach_chat(self, gp, monkeypatch, tmp_path):
        log = tmp_path / "gp.log"
        monkeypatch.setattr(gp, "LOG_FILE", log)
        monkeypatch.delenv("COACH_CHAT_ID", raising=False)
        from primitives.validate_plan import Violation
        gp._coach_alert("smoke", [Violation("ride_forbidden_day", "hard", "Ride on Monday")])
        text = log.read_text()
        assert "COACH ALERT" in text and "WITHHELD" in text
