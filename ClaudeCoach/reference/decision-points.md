# Decision-points calendar — pre-wired branches

**Purpose:** the build has known fork-in-the-road dates. Each one has a test result that should *automatically* update downstream prescriptions. This file pre-wires the branches so Claude doesn't make ad-hoc decisions when results land.

## Calendar

| Date | Event | Decision driven |
|---|---|---|
| **Within 2 weeks (by ~10 May)** | CSS swim test #1 | Updates swim pace bands |
| **Within 2 weeks (W1 or W2)** | Bike LTHR test (20-min) | Calibrates bike-specific HR zones (currently use run-derived 170, likely 5–10 bpm too high) |
| **6 May 2026** | Athlete turns 31 | AG bracket M30–34 confirmed for race day |
| **End May (~31 May)** | FTP retest (bike) | Updates all bike power targets |
| **Mid May–Jun** | Precision Hydration sweat test | Replaces sodium assumption with data |
| **Mid May (~15 May)** | Heat acclimation start | Triggers heat-protocol cadence |
| **Mid June (~15 June)** | Ankle clearance review | Determines whether quality run can start |
| **Mid July (~15 July)** | FTP retest (bike) #2 | Updates bike power targets again |
| **Mid July (~15 July)** | CSS swim test #2 | Confirms swim adaptation, refines race pace |
| **Mid Aug (~15 Aug)** | Long brick race-rehearsal | Verifies fitness and pacing before peak |
| **By mid-May** | Bike gearing decision | 52T × 10–33 forces above-cap power on Bertinoro climbs. Decide: swap chainring (46T/48T sub-compact) OR change cassette OR accept and re-budget pacing. |
| **Race week (~15 Sept)** | Wetsuit ruling | Final swim execution decision |
| **D-7 (12 Sept)** | Final heat-protocol intensity check | Maintain or wind down |

---

## Branches

### B1. CSS swim test #1 — within 2 weeks

**Output:** new CSS pace per 100 m.

| Result | Action |
|---|---|
| CSS faster than expected (<1:35/100m) | Lift weekly volume target. CSS-paced sets at new pace. Keep technique focus. |
| CSS as expected (1:35–1:42/100m) | Continue current swim plan. Build OWS volume from May. |
| CSS slower than expected (>1:42/100m) | Add a third weekly swim if schedule allows. Investigate technique blockers (Ochy-equivalent for swim — video). |
| **All cases:** | Update `current-state.md` with the result. Compute Z2/Z3/Z4 swim bands from CSS. Re-test mid-July. |

### B1b. Bike LTHR test — W1 or W2

**Trigger condition:** persistent mismatch between power TiZ and HR TiZ on outdoor rides (e.g. Sat 25 Apr T-147: 29% Z3 power vs 5% Z3 HR). Suggests run-derived LTHR 170 is too high for cycling.

**Format:** 20-min all-out steady-state effort on flat/false-flat, after 15 min progressive warm-up. Either standalone in W1 (replace Fri sweet spot) or appended to a Friday quality session in W2.

**Output:** average HR over the final 20 min × 0.95 ≈ bike LTHR.

| Result | Action |
|---|---|
| Bike LTHR ≥ 168 | Keep current zones (LTHR 170 is close enough). |
| Bike LTHR 160–167 (most likely) | Update Intervals.icu → Settings → Ride → LTHR. New HR Z2 ceiling ~140 (vs 146). Re-baseline TiZ reads on prior rides. |
| Bike LTHR < 160 | Sanity-check the test (was it a true max effort?). Re-run before locking in. |
| **All cases:** | Update anchor values in `rules.md`. Re-tag the bike HR zones line as calibrated. |

### B2. FTP retest — end May

**Output:** new bike FTP in W (and W/kg if weight has moved).

| Result | Action |
|---|---|
| FTP holds at 316 ± 5 W | Build proceeds as planned. NP target stays ~225 W (IF 0.71). |
| FTP rises 320–325 W | Lift bike race target NP to ~228–230 W. Recompute every bike target in `templates/session-library.md`. |
| FTP rises 326+ W (Oct 2025 ramp-test was 326) | Lift NP target to 232+ W. Bike time gain extends to -3 to -4 min beyond pacing fix. |
| FTP drops below 310 W | Investigate cause (sleep deficit? CTL ramp? overreach?). Don't re-test for 4 weeks. Hold current bike targets. |
| **All cases:** | Update `current-state.md`. Recompute IF bands in `templates/session-library.md`. Push next-week sessions with revised targets via IcuSync. |

### B3. Precision Hydration sweat test — May/June

**Output:** sweat rate (L/hr) + sweat sodium (mg/L).

| Result | Action |
|---|---|
| Sodium ≤500 mg/L | Lower-end of plan range (1,000 mg/hr in heat). Use PH1000 product line. |
| Sodium 500–1,000 mg/L | Mid-range — current 1,000–1,500 mg/hr plan stands. Use PH1500 product line. |
| Sodium >1,000 mg/L (heavy salty sweater) | Upper-range (1,500–2,000 mg/hr). Use PH1500 + extra. Keep dedicated electrolyte bottle separate from Maurten. |
| Sweat rate >1.5 L/hr | Increase race-day fluid target to ≥1 L/hr; carry larger bottle from start. |
| **All cases:** | Update sodium protocol in `current-state.md` and the project plan. Test the new dose in two long bike rides before locking in. |

### B4. Heat acclimation start — mid May (~15 May)

**Output:** session 1 of the Zurawlew protocol logged.

| Status | Action |
|---|---|
| Session 1 done by 15 May | Trigger 4–6/wk cadence. Log every session in `current-state.md`. |
| Slipped to late May | Compress: target 6 sessions in first 10 days (research-supported short-cycle adaptation). |
| Slipped to early June | **Risk-register entry escalates.** Heat tolerance becomes the binding question for race-day plan. |
| **All cases:** | Track sessions cumulative + days-since-last (decay 7–10 days) in `current-state.md`. |

### B5. Ankle clearance — mid June

**Trigger condition:** 4 consecutive weeks of pain ≤2/10 during run AND 0/10 next morning.

| Status | Action |
|---|---|
| Cleared (4 pain-free weeks logged) | **Quality run unlocked.** Add R3 (race-pace intervals) and R4 (tempo) to the schedule. Long-run progression to 28–32 km. Race-pace bricks resume. |
| Almost cleared (2–3 pain-free weeks) | Hold quality. Continue R1 + R5 (easy + long Z1–Z2 only). Re-evaluate weekly. |
| Not cleared (recurring pain) | **Quality off the table for 2026 build.** Reassess goal — 9:30 floor becomes 9:45 (B-goal). Bike + swim absorb the run-fitness shortfall. Physio escalation. |
| **All cases:** | Update `current-state.md`. If cleared, push the new run sessions via IcuSync. |

### B6. FTP retest #2 + CSS test #2 — mid July

**Output:** updated FTP and CSS.

| Result | Action |
|---|---|
| Both at or above mid-May/early-May results | Build is delivering. Hold targets. |
| Either stagnant | Investigate: sleep, fuel quality, life stress, ramp too aggressive. |
| Either dropping | **Yellow flag.** Recovery week, then re-test in 10 days. |
| **All cases:** | Re-baseline session library. This is the last credible re-targeting before peak — what we measure now is what we race on. |

### B7. Long brick race-rehearsal — mid August

**Format:** 4–5 hr bike at IM pace + 60–90 min run at IM pace, in heat where possible.

| Result | Action |
|---|---|
| Decoupling on bike <5%, run holds 4:58–5:05/km, fuelling clean | Confirm 9:30 fitness is in place. Shift focus to taper. |
| Decoupling 5–10%, run pace drifts | Aerobic durability gap. Two more long Z2 rides before taper. |
| Decoupling >10%, run pace collapses | Race-day target shifts to B-goal 9:45. **Don't push fitness in last 4 weeks** — preserve, don't extend. |
| **All cases:** | Race-day pacing rules locked in based on this rehearsal. |

### B7b. Bike gearing decision — by mid-May

**Output:** chainring/cassette swapped, or pacing-plan adjusted.

| Path | Action |
|---|---|
| Swap to 46T or 48T sub-compact (recommended) | New chainring + fitting before 1 June. Verify on a hilly ride: cadence on 6–7% climbs at IF 0.78 should sit 75–85 rpm. Race target NP stays 225 W; climb cap stays 246 W. |
| Swap cassette to 11-36 or 11-40 | Confirm rear derailleur cage compatibility first (long cage often required). New cassette + fitting before 1 June. |
| Accept and re-budget | NP target drops to **220 W** (vs 225 W) to preserve matches. Climb cap remains 246 W *target* but plan to break it briefly on steepest pitches; soften first 90 min to IF 0.65. **Bike time gain reduces from -11 min to ~-7 min** vs 2025. Goal floor shifts toward 9:35–9:40. |
| **All cases:** | Update `rules.md` gearing line. Update `course.md` Bertinoro pacing. Verify on a hilly training ride before locking in. |

### B8. Wetsuit decision — race week

**Trigger:** announcement at race briefing (or sea-temperature reading day-of).

| Status | Action |
|---|---|
| Wetsuit legal | Use it. Standard swim plan. |
| Wetsuit illegal | **Non-wetsuit plan must be rehearsed.** Assume slightly slower swim (+1–2 min), more conservative early-bike pacing if T1 felt rushed. |
| Optional (mixed-temp swim — 22.0–24.5°C edge case) | Optional. Wear it (warmth, buoyancy, time gain) unless heat-management on swim is genuinely the dominant concern. |
| **All cases:** | No new gear decision in race week. Use the wetsuit you've trained in for ≥6 sessions or none at all. |

### B9. Final heat-protocol intensity — D-7

**Trigger:** Saturday 12 Sept, 7 days out.

| Status | Action |
|---|---|
| 14+ heat sessions banked | Maintain 3–4 sessions in final 7 days. Don't add more. |
| 10–13 sessions banked | Maintain 4 sessions in final 7 days. |
| <10 sessions banked | Compress: 5–6 sessions in final 7 days. Adaptation has decayed; need to rebuild. |
| **All cases:** | Last hot bath D-1. None on D-0. |

---

## Use pattern

When a result lands (FTP test, sweat test, ankle review etc.), Claude consults this file *first*, applies the branch, then updates `current-state.md` and `templates/session-library.md`. **No ad-hoc decisions on retest day** — the rule is pre-wired.
