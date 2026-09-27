# Onboarding baseline + HR quality

**Decided:** 27 Sep 2026 (Jamie). **Built:** Telegram bot first; carried into the web app
per `docs/app-transition-plan.md`.

## Why

| Problem | Effect |
|---|---|
| Onboarding took whatever threshold Intervals.icu held, or asked the athlete to type one | Every zone, "% Pace" step and load number built on a guess |
| Every HR number assumed the sensor was right | Wrist HR locks to cadence, reads high early, drops out, and the coach treated it as fact |

## What a new athlete gets

1. About 10 onboarding questions. New: **what they wear for HR** (strap / wrist / mix /
   none), **power meter** (only if Intervals.icu shows none), and **which Intervals.icu
   thresholds came from a test in the last 6 weeks**. Threshold gap questions now ask
   for a *tested* number or "no".
2. On `/approve`: a **baseline block** is pushed to their calendar. Tests only for the
   sports they race, spaced (bike and run 48 h apart, a clear day between tests), one
   week unless the start day makes spacing impossible, then two.

| Sport | Test | Result |
|---|---|---|
| Bike, with power | Ramp, +20 W/min from 40% of any estimated FTP | FTP = 75% of best minute |
| Bike, no power, strap | 30-min TT | LTHR = average HR of last 20 min |
| Bike, no power, no strap | none | stays RPE |
| Run | 30-min TT | threshold pace (and LTHR if HR trusted) from last 20 min |
| Swim | 400 + 200 | CSS = (D400 - D200) / (T400 - T200) |

3. Each result comes back as a Telegram message with **Set my zones / Test went wrong**.
   Only a tap writes Intervals.icu sport settings (the renderer's source), profile.json
   and `baseline.json`.
4. The block closes the Monday after its window with a summary. The normal weekly plan
   builds from then; any sport still untested is rendered **RPE-only** (step cues like
   "Hard but sustainable, RPE 7", no % target) until tested. The coach can book a missed
   test: `scripts/baseline-week.py book`.

Established athletes (no `baseline.json`) are untouched by every gate.

## HR quality

`lib/hr_quality.py` judges each activity from its raw streams: missing, dropouts,
flatline, spikes, cadence lock (HR on cadence AND following its noise), early spike
(first minutes high per unit effort), implausible max. Verdicts `ok / suspect / bad /
none` go to `athletes/<slug>/hr-quality-log.json`.

`bad` and `none` are excluded from: activity-watcher debrief HR lines and DECOUPLING,
run-durability decoupling, heat dose, HR zone distribution, realised TID, the run
threshold fit, weekly EF, dashboard EF and decoupling trend. An activity not in the log
behaves exactly as before.

Athletes who declared wrist / none get a coach-prompt rule: prescribe by power, pace or
RPE, never by HR.

### Audit of existing athletes (27 Sep 2026, 365 days, non-swim)

| Athlete | Activities | Bad | No HR | Strap (RR recorded) |
|---|---|---|---|---|
| Jamie | 288 | 1 | 3 | 148 |
| Kathryn | 217 | 1 | 2 | 35 |
| Calum | 29 | 0 | 3 | 0 |

The problem is small for the current three; the value is for new athletes on wrist HR.
The detector was tuned on this history and synthetic artefacts; there are no known-bad
real activities to test its recall against.

## Files

| File | Role |
|---|---|
| `lib/baseline.py` | state, scheduling, test workouts, result maths, confirm/apply, close |
| `scripts/baseline-week.py` | `start` / `book` / `record` / `status` |
| `lib/hr_quality.py` | per-activity HR verdicts, log, prompt rule |
| `scripts/hr-quality-audit.py` | backfill + report |
| `scripts/test_baseline.py`, `test_hr_quality.py`, `test_hr_consumers.py`, `test_onboarding_baseline.py` | offline tests |
