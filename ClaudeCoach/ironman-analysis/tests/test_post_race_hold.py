"""Post-race recovery hold — easy only until the athlete says they are ready.

Jamie, 27 Sep 2026, after IM Italy: "until you tell Coach you're ready, your weekly plan
is easy aerobic only, with no bricks, no quality and no 'X% off target' messages."
Before this, week 4 after the race flipped into the next block on the calendar alone,
and a recovery week that missed its fraction-of-maintenance target by more than 12%
sent "I'm *not* happy with it: load 21.3% off target" (21 Sep) and "18.7%" (27 Sep).

Pins: opt-in only (no flag = unchanged); the hold keeps week 4+ a post_race week at the
week-3 level; ready releases it from the next Monday, and the block counts its weeks
from there; bookings inside the hold are surfaced, never silently dropped; the load
verdict never fails a post_race week; the weekly and morning messages; the chat block.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402

RACE = "2026-09-19"
RACE_D = date(2026, 9, 19)
CFG = {
    "plan_start": "2026-05-04",
    "race_date": RACE,
    "race_name": "IM Italy",
    "phase_tss": {"base_end_week": 6, "build_end_week": 12, "peak_end_week": 17},
    "ctl_targets": {"phase_ctl": {"base": 80, "build": 95, "peak": 110},
                    "maintenance_ctl_band": [65, 75]},
    "max_ctl_ramp_per_week": 4.0,
    "offseason": {
        "focus": "power and speed",
        "bookings": [
            {"week_start": "2026-10-19", "sport": "Ride", "name": "FTP test", "match": "FTP"},
            {"date": "2026-11-14", "sport": "Run", "name": "5k PB attempt (parkrun)",
             "match": "5k"},
        ]},
}
HOLD = dict(CFG, post_race_hold=True)
WK2, WK3, WK4, WK5 = (date(2026, 9, 28), date(2026, 10, 5),
                      date(2026, 10, 12), date(2026, 10, 19))
CTL = 90.0


def _req(cfg, day):
    return pt.required_tss(cfg, CTL, today=day)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestOptIn:
    def test_without_the_flag_week4_is_the_offseason_block(self):
        r = _req(CFG, WK4)
        assert r["week_type"] == "offseason"
        assert r["offseason_week"] == 1
        assert not r.get("recovery_hold")

    def test_without_the_flag_there_is_no_ready_prompt(self):
        assert not _req(CFG, WK3)["ready_prompt"]


class TestHold:
    def test_week4_stays_a_post_race_week(self):
        r = _req(HOLD, WK4)
        assert r["week_type"] == "post_race"
        assert r["recovery_hold"] is True
        assert r["weekly_tss_floor"] == 0
        assert "offseason_week" not in r
        assert "RECOVERY HOLD" in r["note"] and "no bricks" in r["note"]

    def test_held_week_sits_at_the_week3_level(self):
        assert _req(HOLD, WK4)["required_weekly_tss"] == _req(HOLD, WK3)["required_weekly_tss"]
        assert _req(HOLD, WK5)["required_weekly_tss"] == _req(HOLD, WK3)["required_weekly_tss"]

    def test_weeks_1_to_3_are_unchanged_by_the_flag(self):
        for d in (WK2, WK3):
            a, b = _req(CFG, d), _req(HOLD, d)
            assert a["required_weekly_tss"] == b["required_weekly_tss"]
            assert a["week_type"] == b["week_type"] == "post_race"

    def test_ready_prompt_from_week3_only(self):
        assert not _req(HOLD, WK2)["ready_prompt"]
        assert _req(HOLD, WK3)["ready_prompt"]
        assert _req(HOLD, WK4)["ready_prompt"]

    def test_a_held_week_does_not_ask_for_the_next_race(self):
        r = _req(HOLD, WK4)
        assert r["needs_next_race"] is False
        assert r["needs_maintenance_target"] is False

    def test_a_booking_inside_the_hold_is_surfaced_not_planned(self):
        r = _req(HOLD, WK5)
        assert [b["name"] for b in r["held_bookings"]] == ["FTP test"]
        assert "bookings" not in r
        assert "FTP test" in r["note"] and "must NOT be scheduled" in r["note"]


class TestReady:
    def test_ready_before_week4_starts_the_block_on_time(self):
        r = _req(dict(HOLD, post_race_ready="2026-10-11"), WK4)
        assert r["week_type"] == "offseason"
        assert r["offseason_week"] == 1

    def test_ready_midweek_starts_the_block_next_monday(self):
        cfg = dict(HOLD, post_race_ready="2026-10-14")         # a Wednesday in week 4
        assert _req(cfg, WK4)["week_type"] == "post_race"      # the week in progress
        r = _req(cfg, WK5)
        assert r["week_type"] == "offseason"
        assert r["offseason_week"] == 1                        # intro doses not skipped

    def test_a_ready_date_from_before_the_race_does_not_count(self):
        r = _req(dict(HOLD, post_race_ready="2026-09-01"), WK4)
        assert r["week_type"] == "post_race" and r["recovery_hold"]

    def test_ready_in_week2_does_not_shorten_recovery(self):
        cfg = dict(HOLD, post_race_ready="2026-09-28")
        assert _req(cfg, WK2)["week_type"] == "post_race"
        assert _req(cfg, WK3)["week_type"] == "post_race"
        assert _req(cfg, WK4)["week_type"] == "offseason"


class TestReadyCommand:
    def _write(self, tmp_path, cfg):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"jamie": cfg}))
        return p

    def test_sets_backs_up_and_reports_the_block_start(self, tmp_path):
        p = self._write(tmp_path, HOLD)
        out = pt.set_post_race_ready("jamie", when="2026-10-14", path=p)
        assert json.loads(p.read_text())["jamie"]["post_race_ready"] == "2026-10-14"
        assert list(tmp_path.glob("athletes.json.bak-post-race-ready-*"))
        assert out["block_starts"] == "2026-10-19"
        assert out["bookings_to_redate"] == []

    def test_a_late_ready_lists_bookings_that_fell_in_the_hold(self, tmp_path):
        p = self._write(tmp_path, HOLD)
        out = pt.set_post_race_ready("jamie", when="2026-10-21", path=p)
        assert out["block_starts"] == "2026-10-26"
        assert [b["name"] for b in out["bookings_to_redate"]] == ["FTP test"]

    def test_undo_clears_it(self, tmp_path):
        p = self._write(tmp_path, dict(HOLD, post_race_ready="2026-10-11"))
        out = pt.set_post_race_ready("jamie", undo=True, path=p)
        assert "post_race_ready" not in json.loads(p.read_text())["jamie"]
        assert out["post_race_ready"] is None


class TestDailySurfaces:
    def test_recovery_covers_weeks_1_to_3_for_everyone(self):
        assert pt.in_post_race_recovery(CFG, date(2026, 9, 25))
        assert pt.in_post_race_recovery(CFG, date(2026, 10, 10))
        assert not pt.in_post_race_recovery(CFG, date(2026, 10, 13))
        assert not pt.in_post_race_recovery(CFG, date(2026, 9, 18))   # race not yet run

    def test_recovery_continues_while_held(self):
        assert pt.in_post_race_recovery(HOLD, date(2026, 10, 20))
        assert not pt.in_post_race_recovery(
            dict(HOLD, post_race_ready="2026-10-14"), date(2026, 10, 20))

    def test_morning_card_drops_the_quality_cue_in_recovery(self):
        mc = _load(REPO / "scripts" / "morning-checkin.py", "morning_checkin")
        normal = mc._build_prompt("jamie", "Jamie", "", "", None, [])
        held = mc._build_prompt("jamie", "Jamie", "", "", None, [], post_race_recovery=True)
        assert "good day for quality work" in normal
        assert "good day for quality work" not in held
        assert "never a cue for quality work" in held

    def test_chat_block_only_while_held(self, tmp_path):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"jamie": HOLD, "kathryn": CFG}))
        day = date(2026, 10, 13)
        block = pt.recovery_hold_prompt_block("jamie", "Jamie", path=p, today=day)
        assert "post-race-ready --athlete jamie" in block
        assert "off-season block" in block
        assert pt.recovery_hold_prompt_block("kathryn", "Kathryn", path=p, today=day) == ""
        p.write_text(json.dumps({"jamie": dict(HOLD, post_race_ready="2026-10-12")}))
        assert pt.recovery_hold_prompt_block("jamie", "Jamie", path=p, today=day) == ""


class TestStage1Messages:
    @classmethod
    def setup_class(cls):
        cls.s1 = _load(REPO / "scripts" / "stage1-plan.py", "stage1")

    def test_a_post_race_week_is_never_failed_on_load(self):
        assert self.s1._load_on_target({"week_type": "post_race"}, 21.3)
        assert self.s1._load_on_target({"week_type": "post_race"}, -40.0)
        assert not self.s1._load_on_target({"week_type": "build"}, 18.7)
        assert self.s1._load_on_target({"week_type": "build"}, 11.0)

    def _built(self):
        return {"week_start": "2026-10-12", "total_tss": 300,
                "sessions": [{"date": "2026-10-13", "name": "Easy spin",
                              "duration_min": 60}]}

    def test_post_race_week_message_has_no_target_and_says_how_to_end_it(self):
        brief = {"phase": "transition", "week_type": "post_race", "weekly_tss_target": 250,
                 "ready_prompt": True, "recovery_hold": True,
                 "next_block": "off-season block",
                 "held_bookings": [{"name": "FTP test"}]}
        msg = self.s1._week_message(brief, self._built())
        assert "(target" not in msg
        assert "Reply *ready*" in msg and "off-season block" in msg
        assert "FTP test" in msg

    def test_week3_message_warns_recovery_carries_on(self):
        brief = {"phase": "transition", "week_type": "post_race", "weekly_tss_target": 250,
                 "ready_prompt": True, "recovery_hold": False,
                 "next_block": "off-season block"}
        assert "carries on after this week" in self.s1._week_message(brief, self._built())

    def test_a_normal_week_keeps_its_target_and_no_ready_line(self):
        brief = {"phase": "build", "week_type": "build", "weekly_tss_target": 700}
        msg = self.s1._week_message(brief, self._built())
        assert "(target 700)" in msg and "ready" not in msg
