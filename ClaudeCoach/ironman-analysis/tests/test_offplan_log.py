"""Tests for lib/offplan_log.py — the weekly rule-adherence roll-up.

The fixtures below are trimmed from the three live current-state.md files
(jamie, kathryn, calum, 27 Jul 2026): three different bullet shapes, which is the
whole reason the parser reads the date off the bullet and the verdict out of the
prose.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import offplan_log  # noqa: E402

JAMIE = """## Other niggles

- [Location]: [pain 1-10]

## Off-plan in last 7 days

- **2026-07-26 GO** — Dorney Olympic Triathlon (B-race), race day: no rules fired, execute as planned. Ankle 2/10, quality_cleared false — within physio tolerance (<=5/10), not race-blocking for the run leg.
- **2026-07-25 MODIFIED** — Pre-Race Opener Brick 32 min (brick): R3 + R5 fired. Net: 75%->65% FTP average.
- **2026-07-23 GO** — Long Ride — Taper (moderate Z2) 150 min (bike_z2): no rules fired, execute as planned.
- **2026-07-22 GO** — Strength — Session A (Tier C) 40 min (strength): no rules fired, execute as planned. Calendar also shows a 15-min "Easy Aerobic Run" event today (78-88% pace) that the engine prescription did not evaluate and that breaches Jamie's own 40-min run minimum (persistent-rules #11) — flagged as a likely stray/duplicate from the taper-week resync, not executed, no push made.
- **2026-07-21 GO** — Long Run — Taper (Z2 progression) 118 min (run_long): no rules fired, execute as planned.
- **2026-06-28 BLOCKED** — Long run ~15km (run_long): R1 fired. Ankle 3/10 last run.

## Open actions

| Action | Owner |
|---|---|
"""

KATHRYN = """## Off-plan in last 7 days

- 2026-05-17: run_easy 60min — SKIPPED (sore throat, fatigue)
- 2026-05-18: Rest day — no session in calendar (illness monitoring day)
- 2026-05-20: bike_threshold (sweet spot) — MODIFIED (R3). 2x15min @ 88% reduced to 1x15min @ 83%.
- 2026-05-21: run_easy 56min with strides — GO, execute as planned (no rules fired).

## Heat acclimation log
"""

CALUM = """## Off-plan in last 7 days
- 2026-07-27: Rest day — no session planned in calendar (events endpoint checked, empty for today).
- 2026-07-26 (correction): 2026-07-25 Long Ride — Race-Intensity Finish (195 min) — corrected to NOT completed (watchdog checked history endpoint; ctlLoad=0/atlLoad=0 for 07-25).
- 2026-07-26: Rest day — no session planned in calendar (events endpoint checked, empty for today).
- 2026-07-25: Long Ride — Race-Intensity Finish (195 min) — GO, execute as planned, no rules fired. Last session RPE 7 carried forward (Wed 22 Jul and Thu 23 Jul sessions both confirmed NOT completed, no RPE given since).
- 2026-07-22: Sweetspot Intervals 100min — SWAPPED to comfortable/easy steady effort throughout by engine (R2 ATL-swap rule).
- 2026-07-21: Mont Blanc Day 3 — Endurance (300 min) — GO, execute as planned, no rules fired.

## Something else
"""


class TestParse:
    def test_bold_tag_form(self):
        e = offplan_log.parse_entries(JAMIE)
        assert [x["date"] for x in e] == ["2026-07-26", "2026-07-25", "2026-07-23",
                                          "2026-07-22", "2026-07-21", "2026-06-28"]
        assert [x["verdict"] for x in e] == ["as_prescribed", "modified", "as_prescribed",
                                             "as_prescribed", "as_prescribed", "stood_down"]

    def test_colon_form_verdict_from_prose(self):
        v = {x["date"]: x["verdict"] for x in offplan_log.parse_entries(KATHRYN)}
        assert v == {"2026-05-17": "stood_down", "2026-05-18": "rest",
                     "2026-05-20": "modified", "2026-05-21": "as_prescribed"}

    def test_stops_at_next_heading(self):
        assert all(x["date"].startswith("2026-0") for x in offplan_log.parse_entries(JAMIE))
        assert "Open actions" not in "".join(x["text"] for x in offplan_log.parse_entries(JAMIE))

    def test_missing_section(self):
        assert offplan_log.parse_entries("# nothing here") == []

    def test_correction_keyed_to_the_day_it_corrects(self):
        c = [x for x in offplan_log.parse_entries(CALUM) if x["corrects"]]
        assert len(c) == 1
        assert c[0]["date"] == "2026-07-26" and c[0]["corrects"] == "2026-07-25"
        assert c[0]["verdict"] == "not_completed"

    def test_other_days_not_completed_does_not_reclassify_a_go_day(self):
        v = {x["date"]: x["verdict"] for x in offplan_log.parse_entries(CALUM)
             if not x["corrects"]}
        assert v["2026-07-25"] == "as_prescribed"


class TestRollup:
    def test_window_and_counts(self):
        r = offplan_log.week_rollup(JAMIE, "2026-07-20", "2026-07-26")
        assert r["days"] == 5
        assert r["counts"] == {"as_prescribed": 4, "modified": 1}
        assert "5 days logged" in r["line"]
        assert "4 ran as prescribed" in r["line"]
        assert "1 adjusted by your own rules" in r["line"]
        assert "2026-06-28" not in r["line"]

    def test_names_the_breach_the_log_named(self):
        r = offplan_log.week_rollup(JAMIE, "2026-07-20", "2026-07-26")
        assert len(r["breaches"]) == 1
        assert "40-min run minimum" in r["breaches"][0]
        assert r["breaches"][0].startswith("2026-07-22")

    def test_clean_week_has_no_breaches(self):
        r = offplan_log.week_rollup(KATHRYN, "2026-05-17", "2026-05-23")
        assert r["breaches"] == []
        assert "no breach" in offplan_log.prompt_block(r)

    def test_correction_overrides_the_day_it_corrects(self):
        r = offplan_log.week_rollup(CALUM, "2026-07-20", "2026-07-26")
        assert r["days"] == 4                       # 21, 22, 25, 26
        assert r["counts"] == {"as_prescribed": 1, "modified": 1,
                               "not_completed": 1, "rest": 1}
        assert "1 not completed" in r["line"]

    def test_empty_window_returns_none(self):
        assert offplan_log.week_rollup(JAMIE, "2026-01-01", "2026-01-07") is None
        assert offplan_log.week_rollup("no section", "2026-07-20", "2026-07-26") is None

    def test_prompt_block_tells_the_model_to_omit_when_empty(self):
        assert "omit" in offplan_log.prompt_block(None)

    def test_singular_plural(self):
        r = offplan_log.week_rollup(KATHRYN, "2026-05-18", "2026-05-18")
        assert "1 day logged" in r["line"] and "1 rest day" in r["line"]
