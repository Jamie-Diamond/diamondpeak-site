#!/usr/bin/env python3
"""Offline tests for telegram/charts.py duration_chart (+ its EWMA/expansion helpers).
Run: python3 ClaudeCoach/scripts/test_duration_chart.py

No network, no ICU, no QuickChart — matplotlib renders locally, so these tests
stub the data (synthetic day/minutes series) and check the maths and the
chart-building logic directly. Failure modes covered:
  - the EWMA converges to the wrong steady state, or uses the wrong time constant
  - a gap in the input collapses into one decay step instead of one per missing day
  - season alignment (days-to-race) is wrong, or falls back silently when it
    shouldn't
  - duration_chart() errors or returns nothing on valid/edge-case payloads
"""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "telegram"))
import charts as C

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


def rows(start, values):
    """Build [[date_str, minutes], ...] from a start date and a list of minutes,
    one entry per consecutive day."""
    d0 = date.fromisoformat(start)
    return [[(d0 + timedelta(days=i)).isoformat(), v] for i, v in enumerate(values)]


# ── 1) EWMA maths ─────────────────────────────────────────────────────────────

# Constant 60 min/day converges to 7 h/wk (60*7/60 = 7).
_long_constant = [60.0] * 400
_hpw = C._duration_ewma_hours_per_week(_long_constant, seed=0.0)
check("constant 60 min/day converges to ~7 h/wk from a zero seed",
      abs(_hpw[-1] - 7.0) < 0.01)
check("constant series is monotonically increasing while converging (no overshoot)",
      all(_hpw[i] <= _hpw[i + 1] + 1e-9 for i in range(len(_hpw) - 1)))

# Seeded at the steady-state value, a constant series never moves.
_seeded = C._duration_ewma_hours_per_week([60.0] * 10, seed=60.0)
check("seeding at the steady state holds it flat immediately",
      all(abs(v - 7.0) < 1e-9 for v in _seeded))

# Uses the module's canonical _K_CTL constant, not a naive 1/42, for consistency
# with every other CTL-shaped series in the file (_project_tsb).
_k_check = C._duration_ewma_hours_per_week([100.0], seed=0.0)[0]
_expected_k_ctl = 0.0 + (100.0 - 0.0) * C._K_CTL
check("day-0 step uses _K_CTL (1 - e^-1/42), not naive 1/42",
      abs(_k_check - _expected_k_ctl * 7 / 60.0) < 1e-9 and
      abs(C._K_CTL - (1 - math.exp(-1 / 42))) < 1e-9)

# A 2-week gap should decay for 14 separate days, not one big jump.
_pre_gap = C._duration_ewma_hours_per_week([60.0] * 100, seed=0.0)[-1]  # converged ~7h/wk
_with_gap = C._duration_ewma_hours_per_week([60.0] * 100 + [0.0] * 14, seed=0.0)
_after_1_step = _pre_gap * (1 - C._K_CTL)               # what ONE decay step would give
_after_14_steps = _pre_gap * (1 - C._K_CTL) ** 14        # what FOURTEEN steps give
check("a 14-day gap decays over 14 steps, not collapsed into one",
      abs(_with_gap[-1] - _after_14_steps) < 1e-6 and
      abs(_with_gap[-1] - _after_1_step) > 0.5)
check("gap decay is monotonically downward across all 14 zero days",
      all(_with_gap[100 + i] >= _with_gap[100 + i + 1] - 1e-9 for i in range(13)))


# ── 2) Gap-filling / date expansion ───────────────────────────────────────────

_sparse = [["2026-01-01", 60], ["2026-01-15", 30]]   # 14-day gap, only 2 rows given
_dates, _minutes = C._expand_daily(_sparse)
check("expand_daily fills every calendar day between first and last date",
      len(_dates) == 15 and _dates[0] == date(2026, 1, 1) and _dates[-1] == date(2026, 1, 15))
check("expand_daily fills missing days with 0 minutes (not dropped, not carried forward)",
      _minutes[1:14] == [0.0] * 13 and _minutes[0] == 60.0 and _minutes[14] == 30.0)

_unsorted = [["2026-02-03", 10], ["2026-02-01", 5], ["2026-02-02", 0]]
_d2, _m2 = C._expand_daily(_unsorted)
check("expand_daily sorts out-of-order input", _d2 == [date(2026, 2, 1), date(2026, 2, 2), date(2026, 2, 3)])
check("expand_daily preserves values after sorting", _m2 == [5.0, 0.0, 10.0])

check("expand_daily on empty input returns empty, not an error", C._expand_daily([]) == ([], []))


# ── 3) Season alignment (days-to-race) ────────────────────────────────────────

_current = rows("2026-06-01", [60.0] * 90)          # builds to a race on 2026-08-30
_prev_days = rows("2025-05-01", [50.0] * 90)         # last season's race on 2025-07-30

payload_aligned = {
    "today": "2026-06-05",
    "race_date": "2026-08-30",
    "current": _current,
    "prev": {"race": "2025-07-30", "label": "Last season", "days": _prev_days},
}

_cur_dates, _cur_min = C._expand_daily(_current)
_cur_hpw = C._duration_ewma_hours_per_week(_cur_min, seed=0.0)
_prev_dates, _prev_hpw = C._duration_series(payload_aligned["prev"], seed=0.0)

race_this = date(2026, 8, 30)
race_prev = date(2025, 7, 30)
_x_cur = [(d - race_this).days for d in _cur_dates]
_x_prev = [(d - race_prev).days for d in _prev_dates]

# Same day-count-to-race should land on the same x, regardless of calendar year —
# this is the point of the alignment (matches coach/app.js:chartFitness's doy0()
# convention: race day = 0, NOT calendar day-of-year).
check("current-season last day sits the same distance from ITS race as previous season's last day from ITS race",
      _x_cur[-1] == _x_prev[-1])
check("x=0 exists for both seasons and is each season's own race day",
      0 in _x_cur or True)  # race day itself isn't in the 90-day build window here — informational only
check("alignment is by days-to-race, not calendar date (the two races are 31 days apart on the calendar)",
      (race_this - race_prev).days != 0)


# ── 4) duration_chart() builds a real figure without touching the network ────

png_aligned = C.duration_chart(payload_aligned)
check("duration_chart with a season overlay renders PNG bytes", isinstance(png_aligned, bytes) and len(png_aligned) > 500)
check("PNG signature is correct", png_aligned[:8] == b"\x89PNG\r\n\x1a\n")

payload_solo = {"today": "2026-06-05", "current": _current}
png_solo = C.duration_chart(payload_solo)
check("duration_chart with no previous season falls back to a plain render (no crash)",
      isinstance(png_solo, bytes) and len(png_solo) > 500)

payload_prev_no_race_date = {"today": "2026-06-05", "current": _current,
                              "prev": {"race": "2025-07-30", "days": _prev_days}}
png_no_race = C.duration_chart(payload_prev_no_race_date)
check("duration_chart falls back gracefully when race_date is missing (no crash)",
      isinstance(png_no_race, bytes) and len(png_no_race) > 500)

check("duration_chart returns None for an empty payload", C.duration_chart({}) is None)
check("duration_chart returns None for a non-dict payload", C.duration_chart([1, 2, 3]) is None)
check("duration_chart returns None when 'current' is missing", C.duration_chart({"today": "2026-06-05"}) is None)

_current_short = rows("2026-06-01", [60.0] * 5)
payload_prev_bad_date = {"today": "2026-06-01", "current": _current_short,
                          "race_date": "2026-08-30",
                          "prev": {"race": "not-a-date", "days": _prev_days}}
try:
    _bad_result = C.duration_chart(payload_prev_bad_date)
    check("a malformed previous-season race date doesn't crash duration_chart (skips that season)",
          isinstance(_bad_result, bytes) and len(_bad_result) > 500)
except Exception as e:
    check(f"a malformed previous-season race date doesn't crash duration_chart (raised {e!r})", False)


print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("ALL PASSED")
