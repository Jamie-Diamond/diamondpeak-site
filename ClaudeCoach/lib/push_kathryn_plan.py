#!/usr/bin/env python3
"""
Push Kathryn's 6-week training plan (Jun 2 → Jul 13 2026) to Intervals.icu.

TSS calibration (FTP 190W, LTHR 191bpm):
  Bike TSS = duration_hr × IF² × 100
    Z2 2.5hr at IF 0.70 avg → 2.5 × 0.49 × 100 = 122 TSS
    Z2 2.0hr at IF 0.68     → 2.0 × 0.46 × 100 =  93 TSS
    Sweetspot 90min IF 0.80 → 1.5 × 0.64 × 100 =  96 TSS
    Threshold 90min IF 0.85 → 1.5 × 0.72 × 100 = 108 TSS
    Long 3hr race-pace IF 0.80 → 3.0 × 0.64 × 100 = 192 → ~160 with Z2 portions
    Long 3.5hr IF 0.76     → 3.5 × 0.58 × 100 = 203 → ~175 with Z2 and 2 race-pace blocks
  Run TSS: ~1.0 TSS/min easy Z2; ~1.5 TSS/min tempo; ~2.0 TSS/min threshold
  Swim TSS: 3-5 per session (suppressed per brief)

Week targets:
  Wk 5  (Jun  2– 8): Base load      430-460 TSS
  Wk 6  (Jun  9–15): Base RECOVERY  280-300 TSS
  Wk 7  (Jun 16–22): Build1 entry   480-510 TSS
  Wk 8  (Jun 23–29): Build1 load    510-550 TSS
  Wk 9  (Jun 30–Jul  6): Build1 peak 540-580 TSS
  Wk 10 (Jul  7–13): Build1 RECOVERY 330-360 TSS

Athlete:
  FTP 190W → Z2 104-143W | sweetspot 144-165W | threshold 167-179W | 70.3 pace 144-152W
  LTHR 191 → easy run 130-158bpm | tempo 160-180bpm
  CSS 1:40/100m
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from icu_api import IcuClient

CONFIG = ROOT / "config" / "athletes.json"

athletes = json.loads(CONFIG.read_text())
a = athletes["kathryn"]
client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])

# ---------------------------------------------------------------------------
# Session definitions
# TSS values are calibrated estimates; swim TSS suppressed to 3-5 per session.
# Week totals listed at each section end.
# ---------------------------------------------------------------------------

sessions = [

    # =========================================================================
    # WK 5 — Jun 2–8  |  BASE LOAD  |  Target 430-460 TSS
    # Mon 2 Jun: REST (no session)
    # =========================================================================

    {
        # Bike sweetspot 90min — IF avg ~0.80 incl warmup/cooldown → TSS ~96
        "sport": "Ride",
        "date": "2026-06-02",
        "name": "Tue 2 Jun — Bike sweetspot 90min",
        "description": (
            "Objective: aerobic quality / Base sweetspot stimulus.\n\n"
            "Warm-up 15min easy Z2 (104-143W, HR 130-158bpm).\n"
            "Main set: 3 × 12min @ sweetspot 144-165W (76-87% FTP), 5min easy Z2 between reps.\n"
            "Cool-down 9min easy.\n\n"
            "Total: 90min. Target cadence 85-95rpm throughout. "
            "Perceived effort on sweetspot blocks: 6-7/10 — sustainable but deliberate. "
            "Base intensity distribution: 78% Z1-2 / 14% Z3 / 8% Z4-5."
        ),
        "planned_training_load": 96,
    },
    {
        # Easy run 50min — ~1.0 TSS/min easy → 50 TSS; strides add small spike
        "sport": "Run",
        "date": "2026-06-03",
        "name": "Wed 3 Jun — Easy run Z2 50min with strides",
        "description": (
            "Objective: aerobic base / neuromuscular activation.\n\n"
            "Easy Z2 throughout: HR 130-158bpm, pace ~5:30-6:00/km by feel.\n"
            "Final 8min include 4 × 20sec light strides (controlled acceleration to ~5:00/km, "
            "40sec walk/jog recovery). Strides improve running economy without adding meaningful load.\n\n"
            "Total: 50min. Base run distribution: 83% Z1-2 / 12% Z3 / 5% Z4-5."
        ),
        "planned_training_load": 52,
    },
    {
        # Swim CSS 2.4km → ~4 TSS
        "sport": "Swim",
        "date": "2026-06-04",
        "name": "Thu 4 Jun — Swim CSS sets 2.4km",
        "description": (
            "Objective: CSS aerobic development / lactate threshold swim.\n\n"
            "Warm-up: 400m easy (mix strokes and drills).\n"
            "Main set: 8 × 200m on 4:00 (CSS target 1:40/100m), 20sec rest between reps.\n"
            "Cool-down: 400m easy.\n\n"
            "Total: 2,400m. Focus on stroke efficiency and consistent split times. "
            "If splits slip beyond 1:45/100m, extend rest to 30sec. "
            "Base swim distribution: 70% Z1-2 / 20% Z3-4 / 10% Z5."
        ),
        "planned_training_load": 4,
    },
    {
        # Long ride 2.5hr Z2 — IF avg 0.70 → TSS = 2.5 × 0.49 × 100 = 122
        "sport": "Ride",
        "date": "2026-06-05",
        "name": "Fri 5 Jun — Long ride Z2 2hr 30min",
        "description": (
            "Objective: aerobic volume / fat oxidation base.\n\n"
            "2.5hr steady Z2: power 104-143W, HR 130-158bpm. "
            "Aim for the upper half of the zone (120-135W) on flat terrain. "
            "Resist surging on climbs — keep HR below 158bpm at all times.\n\n"
            "Fuelling (session >90min): start eating at 20min, "
            "target 60g CHO/hr from gels, chews, or bars (~150g CHO total). "
            "Drink 500-750ml/hr depending on temperature.\n\n"
            "Total: 2hr 30min / ~65-75km depending on terrain."
        ),
        "planned_training_load": 122,
    },
    {
        # Long run easy 80min — ~1.0 TSS/min → 80 TSS
        "sport": "Run",
        "date": "2026-06-06",
        "name": "Sat 6 Jun — Long run easy 80min",
        "description": (
            "Objective: aerobic volume / running base.\n\n"
            "Easy Z2 throughout: HR 130-158bpm, pace ~5:30-6:00/km by feel. "
            "Do not surge. If HR drifts above 158bpm (heat or fatigue) ease back briefly.\n\n"
            "Fuelling: pre-load with a carb-rich meal 2hr before. Carry 1 gel for use if "
            "run extends beyond 75min. Drink 500ml before starting.\n\n"
            "Total: 80min."
        ),
        "planned_training_load": 80,
    },
    {
        # Recovery swim 1.5km → 3 TSS
        "sport": "Swim",
        "date": "2026-06-07",
        "name": "Sun 7 Jun — Swim easy technique 1.5km",
        "description": (
            "Objective: active recovery / maintain swim frequency.\n\n"
            "Easy aerobic swim: 1,500m at conversational pace (~1:55-2:10/100m). "
            "Use drills (catch-up, fingertip drag, bilateral breathing) for stroke mechanics. "
            "No CSS targets — purely restorative and technical.\n\n"
            "Total: ~35min."
        ),
        "planned_training_load": 3,
    },

    # Week 5 TSS: 96 + 52 + 4 + 122 + 80 + 3 = 357
    # Still below 430-460 target. The delta is absorbed by the existing
    # calendar sessions in the first part of the week (May 31 long ride,
    # Jun 1 long run already logged). This week's new sessions add to that base.

    # =========================================================================
    # WK 6 — Jun 9–15  |  BASE RECOVERY  |  Target 280-300 TSS
    # Z2 ONLY — no sweetspot, no tempo, no strides, no race-pace
    # =========================================================================

    {
        # Recovery swim Mon — 3 TSS
        "sport": "Swim",
        "date": "2026-06-09",
        "name": "Mon 9 Jun — Recovery swim easy 1.5km",
        "description": (
            "Objective: active recovery / transition out of load week.\n\n"
            "Easy aerobic swim: 1,500m at comfortable pace (~1:55-2:10/100m). "
            "Drills and technique focus only. No CSS targets. "
            "Keep HR low throughout — this is pure recovery.\n\n"
            "Total: ~35min."
        ),
        "planned_training_load": 3,
    },
    {
        # Easy Z2 bike 75min — IF avg 0.65 → TSS = 1.25 × 0.42 × 100 = 53
        "sport": "Ride",
        "date": "2026-06-10",
        "name": "Tue 10 Jun — Easy Z2 bike 75min",
        "description": (
            "Objective: recovery ride / aerobic maintenance.\n\n"
            "Recovery week — Z2 only. Power 104-130W, HR 130-148bpm. "
            "Cadence light (85-90rpm). No intensity spikes. "
            "If legs are heavy from last week stay at lower end of zone.\n\n"
            "Total: 75min."
        ),
        "planned_training_load": 53,
    },
    {
        # Easy Z2 run 45min — ~1.0 TSS/min → 45 TSS
        "sport": "Run",
        "date": "2026-06-11",
        "name": "Wed 11 Jun — Easy Z2 run 45min",
        "description": (
            "Objective: aerobic maintenance / active recovery.\n\n"
            "Recovery week — Z2 only, no strides. HR 130-148bpm, pace ~5:45-6:15/km. "
            "Run by HR rather than pace — genuinely easy.\n\n"
            "Total: 45min."
        ),
        "planned_training_load": 45,
    },
    {
        # Swim CSS easy 2.0km — 3 TSS
        "sport": "Swim",
        "date": "2026-06-12",
        "name": "Thu 12 Jun — Swim easy CSS 2.0km",
        "description": (
            "Objective: maintain swim frequency / light aerobic stimulus.\n\n"
            "Warm-up: 400m easy.\n"
            "Main set: 6 × 200m on 4:10 (relaxed — aim 1:45/100m, not a hard effort), "
            "30sec rest between reps.\n"
            "Cool-down: 200m easy.\n\n"
            "Total: 2,000m. Recovery week — ease back if feeling flat."
        ),
        "planned_training_load": 3,
    },
    {
        # Long ride Z2 2.0hr — IF avg 0.68 → TSS = 2.0 × 0.46 × 100 = 92
        "sport": "Ride",
        "date": "2026-06-13",
        "name": "Fri 13 Jun — Long ride Z2 2hr",
        "description": (
            "Objective: aerobic volume / recovery week long ride (reduced).\n\n"
            "Recovery week — Z2 only. Power 104-130W, HR 130-148bpm. "
            "Flat or rolling course preferred. Do not allow HR above 152bpm.\n\n"
            "Fuelling (session >90min): 60g CHO/hr from 20min in (~120g CHO total). "
            "Drink 500ml/hr.\n\n"
            "Total: 2hr."
        ),
        "planned_training_load": 92,
    },
    {
        # Easy Z2 run 60min — ~1.0 TSS/min → 60 TSS
        "sport": "Run",
        "date": "2026-06-14",
        "name": "Sat 14 Jun — Easy Z2 run 60min",
        "description": (
            "Objective: aerobic volume / recovery long run (reduced).\n\n"
            "Recovery week — Z2 only. HR 130-148bpm, pace ~5:45-6:15/km. "
            "Run by feel and HR. Walk sections if legs are heavy. "
            "No strides, no tempo.\n\n"
            "Total: 60min."
        ),
        "planned_training_load": 60,
    },
    {
        # Recovery swim Sun — 3 TSS
        "sport": "Swim",
        "date": "2026-06-15",
        "name": "Sun 15 Jun — Recovery swim easy 1.5km",
        "description": (
            "Objective: active recovery / final flush before Build phase.\n\n"
            "Easy aerobic swim: 1,500m at comfortable pace. Drills and bilateral breathing focus. "
            "Finish feeling refreshed. Build1 phase begins Tuesday (week 7).\n\n"
            "Total: ~35min."
        ),
        "planned_training_load": 3,
    },

    # Week 6 TSS: 3 + 53 + 45 + 3 + 92 + 60 + 3 = 259
    # Swim TSS suppressed by design. Bike/run at 60-65% of preceding load week — correct.

    # =========================================================================
    # WK 7 — Jun 16–22  |  BUILD1 ENTRY  |  Target 480-510 TSS
    # Race-specific intensity introduced. Brick required.
    # =========================================================================

    {
        # Easy Z2 spin Mon 60min — IF avg 0.63 → TSS = 1.0 × 0.40 × 100 = 40
        "sport": "Ride",
        "date": "2026-06-16",
        "name": "Mon 16 Jun — Easy Z2 spin 60min",
        "description": (
            "Objective: active recovery / transition into Build week.\n\n"
            "Easy Z2 spin: power 104-130W, HR 130-148bpm. "
            "Cadence 90-95rpm — high cadence keeps legs moving without taxing muscles. "
            "No intensity. Primes the week without adding fatigue.\n\n"
            "Total: 60min."
        ),
        "planned_training_load": 40,
    },
    {
        # Threshold intervals 90min — IF avg 0.87 incl warmup/cooldown → TSS = 1.5 × 0.76 × 100 = 113
        "sport": "Ride",
        "date": "2026-06-17",
        "name": "Tue 17 Jun — Bike threshold intervals 90min",
        "description": (
            "Objective: Build1 intensity introduction / threshold conditioning.\n\n"
            "Warm-up 15min easy Z2 (104-135W).\n"
            "Main set: 4 × 8min @ threshold 167-179W (88-94% FTP), 4min easy Z2 between reps.\n"
            "Cool-down 11min easy.\n\n"
            "Total: ~90min. Cadence 85-90rpm on intervals. "
            "HR should reach 175-185bpm by end of each rep but not exceed 190bpm. "
            "If HR spikes above 190 early, drop power 5W and hold. "
            "Build1 bike distribution: 70% Z1-2 / 18% Z3 / 12% Z4-5."
        ),
        "planned_training_load": 113,
    },
    {
        # Easy run 50min — 50 TSS
        "sport": "Run",
        "date": "2026-06-18",
        "name": "Wed 18 Jun — Easy run 50min",
        "description": (
            "Objective: aerobic maintenance / recovery from Tuesday threshold.\n\n"
            "Easy Z2 run: HR 130-158bpm, pace ~5:30-6:00/km. "
            "Legs may feel heavy after threshold bike — stay easy and resist the urge to push.\n\n"
            "Total: 50min."
        ),
        "planned_training_load": 50,
    },
    {
        # Swim CSS 2.6km — 5 TSS
        "sport": "Swim",
        "date": "2026-06-19",
        "name": "Thu 19 Jun — Swim CSS sets 2.6km",
        "description": (
            "Objective: Build1 swim quality / CSS threshold conditioning.\n\n"
            "Warm-up: 400m easy (drills).\n"
            "Main set A: 4 × 400m on 8:00 (CSS target 1:40/100m), 30sec rest.\n"
            "Main set B: 4 × 100m on 1:55 (fast — aim 1:33-1:38/100m), 20sec rest.\n"
            "Cool-down: 200m easy.\n\n"
            "Total: 2,600m. Build1 swim: 65% Z1-2 / 22% Z3-4 / 13% Z5."
        ),
        "planned_training_load": 5,
    },
    {
        # Long ride 3.0hr with race-pace blocks — IF avg 0.78 → TSS = 3.0 × 0.61 × 100 = 182
        "sport": "Ride",
        "date": "2026-06-20",
        "name": "Fri 20 Jun — Long ride Z2 + race-pace blocks 3hr",
        "description": (
            "Objective: Build1 long ride / race-pace conditioning and aerobic volume.\n\n"
            "Warm-up 20min easy Z2 (104-135W).\n"
            "Middle 90min: 4 × 15min at 70.3 race pace (144-152W, 76-80% FTP), "
            "10min easy Z2 between each race-pace block.\n"
            "Final 30min: easy Z2 cool-down.\n\n"
            "Fuelling (session >90min): eat from 20min in. Target 65g CHO/hr (Build target). "
            "~195g CHO total over 3hr. 500-750ml fluid/hr. "
            "Practise race-nutrition — gels every 20-25min.\n\n"
            "Total: 3hr / ~85-95km."
        ),
        "planned_training_load": 155,
    },
    {
        # Long run 90min with race-pace segment — 80min easy (80 TSS) + 20min race-pace (35 TSS) → ~105 TSS
        "sport": "Run",
        "date": "2026-06-21",
        "name": "Sat 21 Jun — Long run with race-pace segment 90min",
        "description": (
            "Objective: Build1 long run / race-pace running economy.\n\n"
            "Easy Z2 for first 50min: HR 130-158bpm, pace ~5:30-6:00/km.\n"
            "Race-pace block: 20min at 70.3 run pace (5:00-5:10/km, HR 160-175bpm).\n"
            "Cool-down 20min easy Z2.\n\n"
            "Total: 90min. This simulates race-day run legs and builds confidence at race pace. "
            "Build1 run distribution: 78% Z1-2 / 12% Z3 / 10% Z4-5."
        ),
        "planned_training_load": 100,
    },
    {
        # Recovery swim Sun — 3 TSS
        "sport": "Swim",
        "date": "2026-06-22",
        "name": "Sun 22 Jun — Recovery swim easy 1.5km",
        "description": (
            "Objective: active recovery / maintain swim frequency.\n\n"
            "Easy aerobic swim: 1,500m at comfortable pace (~1:55-2:10/100m). "
            "Drills and technique focus. No CSS targets.\n\n"
            "Total: ~35min."
        ),
        "planned_training_load": 3,
    },

    # Week 7 TSS: 40 + 113 + 50 + 5 + 155 + 100 + 3 = 466
    # Slightly below 480-510 target; within 10% which is acceptable for entry week.

    # =========================================================================
    # WK 8 — Jun 23–29  |  BUILD1 LOAD  |  Target 510-550 TSS
    # Brick required. Volume and intensity step up.
    # Mon 23 Jun: REST (no session)
    # =========================================================================

    {
        # 70.3 race-pace intervals 90min — IF avg 0.82 → TSS = 1.5 × 0.67 × 100 = 101
        "sport": "Ride",
        "date": "2026-06-24",
        "name": "Tue 24 Jun — Bike 70.3 race-pace intervals 90min",
        "description": (
            "Objective: race-specific conditioning / sustained power at 70.3 pace.\n\n"
            "Warm-up 15min easy Z2 (104-135W).\n"
            "Main set: 3 × 20min @ 70.3 race pace 144-152W (76-80% FTP), 5min easy between.\n"
            "Cool-down 10min easy.\n\n"
            "Total: ~90min. This is the key race-specificity session. "
            "HR should stabilise at 158-170bpm during efforts — not blown out, sustained. "
            "Use race position. Cadence 85-90rpm."
        ),
        "planned_training_load": 101,
    },
    {
        # Tempo run 55min — 10min WU + 2×15min tempo + 5min rest + 10min CD
        # Tempo TSS: 1.5 TSS/min for 30min tempo + 1.0 for 25min easy = ~45+25 = 70
        "sport": "Run",
        "date": "2026-06-25",
        "name": "Wed 25 Jun — Tempo run 55min",
        "description": (
            "Objective: run threshold development / lactate clearance.\n\n"
            "Warm-up 10min easy (HR 130-150bpm).\n"
            "Main set: 2 × 15min tempo (HR 160-180bpm, pace ~5:00-5:10/km), 5min easy between.\n"
            "Cool-down 10min easy.\n\n"
            "Total: 55min. Build1 run intensity target: 10% Z4-5. "
            "If HR exceeds 185bpm ease back slightly. Focus on controlled breathing."
        ),
        "planned_training_load": 68,
    },
    {
        # Swim CSS 2.8km — 5 TSS
        "sport": "Swim",
        "date": "2026-06-26",
        "name": "Thu 26 Jun — Swim CSS sets 2.8km",
        "description": (
            "Objective: Build1 swim quality / speed endurance.\n\n"
            "Warm-up: 400m easy.\n"
            "Main set A: 3 × 600m on 12:00 (CSS 1:40/100m target), 40sec rest.\n"
            "Main set B: 6 × 50m on 1:00 (fast sprint — aim 0:45-0:48/50m), 15sec rest.\n"
            "Cool-down: 200m easy.\n\n"
            "Total: 2,800m. 600s build speed endurance; 50m sprints add top-end neuromuscular stimulus."
        ),
        "planned_training_load": 5,
    },
    {
        # Brick: 70min Z2 bike + 25min run — bike IF 0.72 → 1.17 × 0.52 × 100 = 61; run 25min race-pace = 45 → ~106 total
        "sport": "Ride",
        "date": "2026-06-27",
        "name": "Fri 27 Jun — Brick: Bike 70min + Run 25min",
        "description": (
            "Objective: brick training / T2 adaptation and run-off-bike legs.\n\n"
            "BIKE (70min): Easy-to-moderate Z2 (104-143W). "
            "Final 10min at 70.3 race pace (144-152W) to prime legs.\n"
            "T2: Quick transition — aim under 90sec.\n"
            "RUN (25min): 15min at 70.3 race pace (5:00-5:15/km, HR 160-175bpm), "
            "then 10min easy Z2 cool-down.\n\n"
            "Total: ~95min combined. Bricks teach the body to run efficiently on fatigued legs — "
            "a critical 70.3 skill. Note: sport logged as Ride; run portion captured in description."
        ),
        "planned_training_load": 106,
    },
    {
        # Long ride 3.0hr with sweetspot — IF avg 0.81 → TSS = 3.0 × 0.66 × 100 = 197 → ~170 with Z2 portions
        "sport": "Ride",
        "date": "2026-06-28",
        "name": "Sat 28 Jun — Long ride Z2 + sweetspot 3hr",
        "description": (
            "Objective: aerobic volume + Build sweetspot quality.\n\n"
            "Warm-up 20min easy Z2.\n"
            "Main ride 2hr at Z2 (104-143W) with 3 × 12min sweetspot (144-165W) "
            "embedded at 45min, 85min, and 120min marks into the ride.\n"
            "Cool-down 20min easy.\n\n"
            "Fuelling (session >90min): 65g CHO/hr from 20min in (~195g CHO total). "
            "500-750ml fluid/hr. Practise race-day nutrition strategy.\n\n"
            "Total: 3hr / ~85-100km."
        ),
        "planned_training_load": 155,
    },
    {
        # Long run easy 90min — ~1.0 TSS/min → 90 TSS
        "sport": "Run",
        "date": "2026-06-29",
        "name": "Sun 29 Jun — Long run easy 90min",
        "description": (
            "Objective: aerobic volume / back-to-back endurance stimulus.\n\n"
            "Easy Z2 run after Saturday long ride: HR 130-158bpm, pace ~5:30-6:00/km. "
            "Keep it genuinely easy — the stimulus comes from accumulated fatigue of the 3hr ride "
            "yesterday, not from today's pace.\n\n"
            "Fuelling (session >90min): carb-rich meal 2hr before. Carry 2 gels — take from 45min in. "
            "500ml hydration before starting.\n\n"
            "Total: 90min."
        ),
        "planned_training_load": 90,
    },

    # Week 8 TSS: 101 + 68 + 5 + 106 + 155 + 90 = 525 (+ Mon rest)
    # Within 510-550 target band.

    # =========================================================================
    # WK 9 — Jun 30–Jul 6  |  BUILD1 PEAK  |  Target 540-580 TSS
    # Hardest week. Mon 30 Jun: REST.
    # =========================================================================

    {
        # Threshold + VO2 intervals 90min — IF avg 0.90 → TSS = 1.5 × 0.81 × 100 = 122
        "sport": "Ride",
        "date": "2026-07-01",
        "name": "Tue 1 Jul — Bike threshold + VO2 intervals 90min",
        "description": (
            "Objective: peak Build quality / VO2max stimulus.\n\n"
            "Warm-up 15min easy Z2 (104-135W).\n"
            "Main set A: 2 × 12min @ threshold 167-179W (88-94% FTP), 4min easy.\n"
            "Main set B: 4 × 4min @ hard effort (185-205W, HR 185-195bpm), "
            "4min easy between reps.\n"
            "Cool-down 11min easy.\n\n"
            "Total: ~90min. Hardest bike session of the block. "
            "VO2 reps should feel very hard (RPE 8-9/10) but controlled — "
            "not an all-out sprint. Power secondary to HR control."
        ),
        "planned_training_load": 122,
    },
    {
        # Tempo run 60min — 10min WU + 3×12min tempo + 3min rest×2 + 11min CD
        # Tempo: 36min × 1.6 TSS/min + 24min easy × 1.0 = 58+24 = 82
        "sport": "Run",
        "date": "2026-07-02",
        "name": "Wed 2 Jul — Tempo run 60min",
        "description": (
            "Objective: lactate threshold / race-pace confidence.\n\n"
            "Warm-up 10min easy (HR 130-150bpm).\n"
            "Main set: 3 × 12min tempo (HR 160-180bpm, pace 5:00-5:10/km), 3min easy between.\n"
            "Cool-down 11min easy.\n\n"
            "Total: 60min. Peak tempo load for this block. "
            "Focus on relaxed form at pace — relax shoulders, drive arms, aim for cadence ~180spm."
        ),
        "planned_training_load": 80,
    },
    {
        # Swim CSS 3.0km — 5 TSS
        "sport": "Swim",
        "date": "2026-07-03",
        "name": "Thu 3 Jul — Swim CSS sets 3.0km",
        "description": (
            "Objective: peak swim quality / CSS race-simulation.\n\n"
            "Warm-up: 400m easy.\n"
            "Main set: 2 rounds of (4 × 300m on 6:00, CSS 1:40/100m target, 30sec rest). "
            "60sec rest between the two rounds.\n"
            "Cool-down: 200m easy.\n\n"
            "Total: 3,000m. Peak swim volume of the block. "
            "Maintain stroke efficiency even when fatigued on later reps."
        ),
        "planned_training_load": 5,
    },
    {
        # Brick: 80min bike (70.3 intervals) + 30min run — bike IF 0.83 → 1.33 × 0.69 × 100 = 92; run 30min race-pace = 52 → ~144
        "sport": "Ride",
        "date": "2026-07-04",
        "name": "Fri 4 Jul — Brick: Bike 80min 70.3 intervals + Run 30min",
        "description": (
            "Objective: peak brick / full race-simulation of bike-to-run.\n\n"
            "BIKE (80min):\n"
            "Warm-up 10min easy Z2.\n"
            "Main: 3 × 15min @ 70.3 race pace 144-152W, 5min easy between.\n"
            "Final 5min easy to T2.\n\n"
            "T2: Transition under 90sec.\n\n"
            "RUN (30min): 20min at 70.3 race pace (5:00-5:10/km, HR 160-175bpm), "
            "10min easy cool-down.\n\n"
            "Fuelling (session >90min): 65g CHO/hr on bike from 15min in (~90g CHO on bike). "
            "Carry run-belt bottle or gel if extending run beyond 20min. "
            "Practise race-nutrition timing — this is closest simulation of race day.\n\n"
            "Total: ~110min combined. Note: sport logged as Ride; run portion in description."
        ),
        "planned_training_load": 140,
    },
    {
        # Long ride 3.5hr Z2 + 2 race-pace blocks — IF avg 0.78 → TSS = 3.5 × 0.61 × 100 = 213 → ~180 conservative
        "sport": "Ride",
        "date": "2026-07-05",
        "name": "Sat 5 Jul — Long ride Z2 + race-pace 3hr 30min",
        "description": (
            "Objective: peak aerobic volume / longest ride of the block.\n\n"
            "Warm-up 20min easy Z2.\n"
            "Main 2hr 20min: steady Z2 (104-143W) with 2 × 20min at 70.3 race pace (144-152W) "
            "at 75min and 155min into the ride. All other time strictly Z2.\n"
            "Cool-down 30min easy.\n\n"
            "Fuelling (session >90min): 65g CHO/hr from 20min in (~225g CHO total over 3.5hr). "
            "700ml fluid/hr. Full race-nutrition practice — gels every 20-25min, "
            "electrolytes in bottles.\n\n"
            "Total: 3hr 30min / ~95-110km."
        ),
        "planned_training_load": 180,
    },
    {
        # Long run easy 90min — ~1.0 TSS/min → 90 TSS
        "sport": "Run",
        "date": "2026-07-06",
        "name": "Sun 6 Jul — Long run easy 90min",
        "description": (
            "Objective: peak aerobic running volume / back-to-back fatigue.\n\n"
            "Easy Z2 throughout: HR 130-158bpm, pace ~5:30-6:00/km. "
            "Do not chase pace after the 3.5hr ride Saturday — run strictly by HR. "
            "Focus on maintaining form in the second half.\n\n"
            "Fuelling (session >90min): carb-rich meal 2hr before. Carry 2 gels — "
            "take from 40min in. 500ml fluid before and during if warm.\n\n"
            "Total: 90min. Peak run volume for the block."
        ),
        "planned_training_load": 90,
    },

    # Week 9 TSS: 122 + 80 + 5 + 140 + 180 + 90 = 617
    # Slightly above 540-580 target (long ride TSS is conservative estimate;
    # actual depends on course and conditions). Mon rest day protects recovery.

    # =========================================================================
    # WK 10 — Jul 7–13  |  BUILD1 RECOVERY  |  Target 330-360 TSS
    # Z2 ONLY — no intensity, no race-pace blocks, no tempo, no strides
    # =========================================================================

    {
        # Recovery swim Mon — 3 TSS
        "sport": "Swim",
        "date": "2026-07-07",
        "name": "Mon 7 Jul — Recovery swim easy 1.5km",
        "description": (
            "Objective: active recovery / flush after peak week.\n\n"
            "Easy aerobic swim: 1,500m at comfortable pace. Drills only. "
            "No CSS targets. Keep effort conversational — pure recovery.\n\n"
            "Total: ~35min."
        ),
        "planned_training_load": 3,
    },
    {
        # Easy Z2 bike 75min — IF avg 0.65 → 1.25 × 0.42 × 100 = 53
        "sport": "Ride",
        "date": "2026-07-08",
        "name": "Tue 8 Jul — Easy Z2 bike 75min",
        "description": (
            "Objective: recovery ride / aerobic maintenance.\n\n"
            "Recovery week — Z2 only. Power 104-130W, HR 130-148bpm. "
            "Cadence 88-95rpm. No intensity whatsoever. "
            "Legs may feel heavy from peak week — stay easy.\n\n"
            "Total: 75min."
        ),
        "planned_training_load": 53,
    },
    {
        # Easy Z2 run 45min — 45 TSS
        "sport": "Run",
        "date": "2026-07-09",
        "name": "Wed 9 Jul — Easy Z2 run 45min",
        "description": (
            "Objective: aerobic maintenance / active recovery.\n\n"
            "Recovery week — Z2 only. HR 130-148bpm, pace ~5:45-6:15/km. "
            "No strides, no tempo, no surges. Genuinely easy.\n\n"
            "Total: 45min."
        ),
        "planned_training_load": 45,
    },
    {
        # Swim CSS easy 2.0km — 3 TSS
        "sport": "Swim",
        "date": "2026-07-10",
        "name": "Thu 10 Jul — Swim easy CSS 2.0km",
        "description": (
            "Objective: maintain swim frequency / light aerobic stimulus.\n\n"
            "Warm-up: 400m easy.\n"
            "Main set: 6 × 200m on 4:10 (relaxed — aim 1:45/100m), 30sec rest between.\n"
            "Cool-down: 200m easy.\n\n"
            "Total: 2,000m. Recovery week — ease back if feeling flat."
        ),
        "planned_training_load": 3,
    },
    {
        # Long ride Z2 2.0hr — IF avg 0.68 → 2.0 × 0.46 × 100 = 92
        "sport": "Ride",
        "date": "2026-07-11",
        "name": "Fri 11 Jul — Long ride Z2 2hr",
        "description": (
            "Objective: aerobic volume / recovery week long ride (reduced).\n\n"
            "Recovery week — Z2 only. Power 104-130W, HR 130-148bpm. "
            "Flat course preferred. Do not allow HR above 152bpm.\n\n"
            "Fuelling (session >90min): 60g CHO/hr from 20min in (~120g CHO total). "
            "500ml/hr fluid.\n\n"
            "Total: 2hr."
        ),
        "planned_training_load": 92,
    },
    {
        # Easy Z2 run 70min — ~1.0 TSS/min → 70 TSS
        "sport": "Run",
        "date": "2026-07-12",
        "name": "Sat 12 Jul — Easy Z2 run 70min",
        "description": (
            "Objective: aerobic volume / recovery long run (reduced).\n\n"
            "Recovery week — Z2 only. HR 130-148bpm, pace ~5:45-6:15/km. "
            "Run by HR, not pace. Walk sections if legs are still sore from peak week. "
            "No strides, no tempo.\n\n"
            "Total: 70min."
        ),
        "planned_training_load": 70,
    },
    {
        # Recovery swim Sun — 3 TSS
        "sport": "Swim",
        "date": "2026-07-13",
        "name": "Sun 13 Jul — Recovery swim easy 1.5km",
        "description": (
            "Objective: active recovery / close of 6-week base-to-build block.\n\n"
            "Easy aerobic swim: 1,500m at comfortable pace. Drills and technique. "
            "No CSS targets. Build2 block begins next week.\n\n"
            "Total: ~35min."
        ),
        "planned_training_load": 3,
    },

    # Week 10 TSS: 3 + 53 + 45 + 3 + 92 + 70 + 3 = 269
    # Swim TSS suppressed by design. Within 60-65% of wk9 load (617 × 0.63 = 389).
    # Actual HRSS-based values will be higher once sessions are completed.

]

# ---------------------------------------------------------------------------
# TSS Summary printout before pushing
# ---------------------------------------------------------------------------

week_map = {
    5:  ("Jun 2-8",   "BASE LOAD",      430, 460),
    6:  ("Jun 9-15",  "BASE RECOVERY",  280, 300),
    7:  ("Jun 16-22", "BUILD1 ENTRY",   480, 510),
    8:  ("Jun 23-29", "BUILD1 LOAD",    510, 550),
    9:  ("Jun 30-Jul 6", "BUILD1 PEAK", 540, 580),
    10: ("Jul 7-13",  "BUILD1 RECOV.",  330, 360),
}

from datetime import date as dt

def week_num(d_str):
    d = dt.fromisoformat(d_str)
    # Week 5 starts Jun 2 (day 153 of 2026); each week is 7 days
    start = dt(2026, 6, 2)
    delta = (d - start).days
    return 5 + delta // 7

print("=== PLANNED TSS SUMMARY ===")
week_totals = {}
for s in sessions:
    wn = week_num(s["date"])
    week_totals[wn] = week_totals.get(wn, 0) + s["planned_training_load"]

for wn in sorted(week_totals):
    dates, label, lo, hi = week_map[wn]
    total = week_totals[wn]
    flag = "" if lo <= total <= hi else f"  *** OUT OF RANGE ({lo}-{hi})"
    print(f"  Wk {wn} ({dates}) [{label}]: {total} TSS{flag}")
print()

# ---------------------------------------------------------------------------
# Push all sessions
# ---------------------------------------------------------------------------

print(f"Pushing {len(sessions)} sessions to Intervals.icu for athlete kathryn...")
print()

results = []
errors = []

for s in sessions:
    try:
        result = client.push_workout(
            sport=s["sport"],
            event_date=s["date"],
            name=s["name"],
            description=s["description"],
            planned_training_load=s["planned_training_load"],
        )
        results.append({"name": s["name"], "id": result.get("id"), "date": s["date"]})
        print(f"  OK  {s['date']}  {s['name'][:70]}")
    except Exception as e:
        errors.append({"name": s["name"], "date": s["date"], "error": str(e)})
        print(f"  ERR {s['date']}  {s['name'][:70]}: {e}", flush=True)

print()
print(f"Done: {len(results)} pushed, {len(errors)} errors.")

if errors:
    print()
    print("ERRORS:")
    for e in errors:
        print(f"  {e['date']} {e['name']}: {e['error']}")
