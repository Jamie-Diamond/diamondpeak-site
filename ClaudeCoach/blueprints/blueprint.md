# ClaudeCoach Training Blueprint — Universal Methodology

_Version 1.0 — May 2026_

This document defines the universal training methodology used by `generate-blueprint.py` to produce a personalised `training-blueprint.md` for each athlete. All examples use **Athlete A** (full-distance triathlete, 15 hr/week max) and **Athlete B** (half-distance triathlete, 11 hr/week max) as illustrative references.

---

## 1. Mesocycle Algorithm

### 1.1 Phase Structure by Weeks-to-Race

The training plan is divided into phases anchored to race date. The number of phases and their length is determined by `weeks_to_race` at plan generation time.

| Weeks to race | Phase structure |
|---|---|
| ≥ 24 | Base1 (6w) → Base2 (4w) → Base3 (4w) → Build1 (4w) → Build2 (4w) → Peak (2w) → Taper (2–3w) |
| 20–23 | Base1 (6w) → Base2 (4w) → Build1 (4w) → Build2 (4w) → Peak (2w) → Taper (2–3w) |
| 16–19 | Base (6w) → Build1 (4w) → Build2 (4w) → Peak (2w) → Taper (2w) |
| 12–15 | Base (4w) → Build (4w) → Peak (2w) → Taper (2w) |
| 8–11 | Compressed Base (3w) → Build (3w) → Peak (2w) → Taper (2w) |
| < 8 | Crisis: Build (skip base) → Peak → Taper; flag to athlete |

When the algorithm produces more than one Base phase, each successive base builds on the previous: Base1 is aerobic foundation, Base2 adds volume, Base3 introduces limited aerobic threshold.

### 1.2 Phase Definitions

**Base** — aerobic foundation, high volume, polarised distribution. No race-pace intervals. Targets CTL growth of 5–8 TSS/day over the phase. Recovery weeks at 60–65% of preceding peak TSS.

**Build** — race-specific intensity introduced. Volume holds or drops slightly. CTL maintained while aerobic power and economy improve. Bricks increase in frequency.

**Peak** — quality over quantity. Volume reduces by 15–20% vs build. Intensity maintained or sharpened. Final long race-simulation sessions in first half of peak.

**Taper** — volume cut to 40–50% of peak week. Intensity touches maintained (2–3 short sharp sessions). TSS/day drops, CTL declines, TSB rises toward +5 to +15 by race eve.

### 1.3 TSS Ceiling Formula

Maximum sustainable weekly TSS is capped to prevent accumulation injury:

```
max_weekly_tss = max_hours_per_week × 100 × IF²
```

Intensity Factor (IF) targets by phase:

| Phase | IF target | TSS ceiling (Athlete A, 15 hr) | TSS ceiling (Athlete B, 11 hr) |
|---|---|---|---|
| Base | 0.65 | 634 | 465 |
| Build | 0.68 | 694 | 509 |
| Peak | 0.72 | 778 | 570 |
| Taper | — | 40–50% of peak week | 40–50% of peak week |

The ceiling is a hard upper bound. Actual weekly TSS targets start lower and ramp toward the ceiling progressively.

### 1.4 Ramp Rules

- Maximum ramp: **+10% TSS/week**, or **+5 TSS/day CTL**, whichever is smaller.
- Load block: **3 weeks progressive load, 1 week recovery** (or 2+1 for athletes aged ≥ 45 or with elevated injury risk).
- Recovery week TSS: 60–65% of the immediately preceding week's TSS.
- If an athlete misses >30% of a week's planned load, the following week is treated as a recovery week regardless of schedule.

---

## 2. Phase Entry Fitness Check

When `generate-blueprint.py` runs, it fetches the athlete's live CTL from intervals.icu and compares it to the expected CTL at the start of each phase.

**Expected CTL targets (entry to phase):**

| Phase | CTL target (Athlete A) | CTL target (Athlete B) |
|---|---|---|
| Base | 55–70 | 40–55 |
| Build | 70–85 | 55–65 |
| Peak | 80–95 | 60–75 |
| Taper start | 85–100 | 65–80 |

If the athlete's current CTL is **> phase target × 1.10** (i.e. significantly above), the script outputs an `AWAITING_DECISION` block and presents four resolution options:

```
AWAITING_DECISION: athlete={slug} current_ctl={n} phase_target_ctl={m}

Your fitness ({n} CTL) is significantly above the recommended entry level for {phase} ({m} CTL).
This means you have built a solid base ahead of schedule. Choose how to handle this:

A  Taper down first — reduce volume for 1–2 weeks to lower fatigue (TSB), then enter phase on schedule.
   Best for: athletes who feel flat or fatigued despite good numbers.

B  Increase quality now — enter Build early, adding race-pace work to convert fitness to form.
   Best for: athletes feeling sharp, healthy, and training well.

C  Hold and compress — maintain current load, but compress the next phase by 1 week.
   Best for: athletes who want to stay the course but acknowledge they are ahead.

D  Custom — flag for manual coach review.

Run with: python3 generate-blueprint.py --athlete {slug} --fitness-choice [A|B|C|D]
```

If CTL is **< phase target × 0.85** (significantly below), the script logs a warning but proceeds, noting that phase TSS targets will be moderated to the athlete's actual fitness level.

---

## 3. Intensity Distribution

Distribution is expressed as a **weekly average per sport**, not a per-session requirement. Some sessions will be pure Z1–2; others will hit Z4–5. The weekly average across all sessions of that sport should land within the target band.

### 3.1 Zone Definitions

**Cycling (power-based):**

| Zone | % FTP | Description |
|---|---|---|
| Z1 | < 55% | Active recovery |
| Z2 | 55–75% | Aerobic endurance |
| Z3 | 76–87% | Tempo / sweet spot |
| Z4 | 88–94% | Threshold |
| Z5 | 95–105% | VO₂ max |
| Z6 | 106–120% | Anaerobic capacity |
| Z7 | > 120% | Neuromuscular |

**Running (HR-based, % LTHR):**

| Zone | % LTHR | Description |
|---|---|---|
| Z1 | < 68% | Easy / recovery |
| Z2 | 68–83% | Aerobic base |
| Z3 | 84–94% | Tempo |
| Z4 | 95–105% | Threshold |
| Z5 | > 105% | Speed / VO₂ |

**Swimming (pace-based, relative to CSS):**

| Zone | Pace (vs CSS/100m) | Description |
|---|---|---|
| Z1 | > CSS + 1:20 | Easy drill/recovery |
| Z2 | CSS + 0:15 to + 1:20 | Aerobic |
| Z3 | CSS + 0:05 to + 0:15 | Tempo |
| Z4 | CSS – 0:05 to + 0:05 | Threshold / CSS sets |
| Z5 | Sub CSS | Speed |

### 3.2 Weekly Distribution Targets by Phase and Sport

Percentages refer to time in zone across all sessions of that sport for the week.

**Full Ironman:**

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 70% Z1–2 / 20% Z3–4 / 10% Z5 | 80% Z1–2 / 12% Z3 / 8% Z4–5 | 85% Z1–2 / 10% Z3 / 5% Z4–5 |
| Build | 65% Z1–2 / 25% Z3–4 / 10% Z5 | 75% Z1–2 / 15% Z3 / 10% Z4–5 | 80% Z1–2 / 12% Z3 / 8% Z4–5 |
| Peak | 60% Z1–2 / 25% Z3–4 / 15% Z5 | 70% Z1–2 / 15% Z3 / 15% Z4–5 | 75% Z1–2 / 12% Z3 / 13% Z4–5 |

**70.3 / Half Ironman:**

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 70% Z1–2 / 20% Z3–4 / 10% Z5 | 78% Z1–2 / 14% Z3 / 8% Z4–5 | 83% Z1–2 / 12% Z3 / 5% Z4–5 |
| Build | 65% Z1–2 / 22% Z3–4 / 13% Z5 | 70% Z1–2 / 18% Z3 / 12% Z4–5 | 78% Z1–2 / 12% Z3 / 10% Z4–5 |
| Peak | 58% Z1–2 / 25% Z3–4 / 17% Z5 | 65% Z1–2 / 18% Z3 / 17% Z4–5 | 72% Z1–2 / 14% Z3 / 14% Z4–5 |

---

## 4. Event Profiles

Each event profile defines the race-specific demands that shape the build and peak phases. The base phase methodology is shared across all events; the divergence begins in Build.

### 4.1 Full Ironman

**Race demands:** 3.8km swim / 180km bike / 42.2km run. Total duration 9–17 hours. Dominantly aerobic (Z2). Bike pacing critical — target IF 0.68–0.72. Run is a controlled Z2 effort with final-km surge only.

**Key sessions:**
- Long ride: 4–6 hours at Z2 with fuelling practice. Frequency: weekly in build/peak.
- Long run: 2–3.5 hours. Cap at 32km in build; pull back to 26km in peak.
- Swim: 3–4 km sets including 1×1500m continuous sub-race-pace effort.
- Bricks: minimum 1/week in build, 2/week in peak. Standard brick = 90–120 min ride + 30–40 min run.
- Race simulation: 1× in late build (5hr ride + 60 min run); 1× in peak (4hr ride + 45 min run).

**Taper:**
- Week –3: 70% of peak volume.
- Week –2: 55% of peak volume.
- Race week: 40% of peak volume. Last long session ≥ 10 days out.

**Goal time → pacing inputs:**

| Target finish | Bike IF | Run pace (vs threshold) | Swim pace |
|---|---|---|---|
| Sub 9:30 | 0.78–0.82 | –20% | CSS – 0:05 |
| Sub 10:30 | 0.72–0.76 | –25% | CSS |
| Sub 11:30 | 0.68–0.72 | –30% | CSS + 0:10 |
| Finish | 0.65–0.68 | –35% | CSS + 0:15 |

### 4.2 70.3 / Half Ironman

**Race demands:** 1.9km swim / 90km bike / 21.1km run. Total duration 4–7 hours. Bike at IF 0.78–0.85. Run at half-marathon effort with tempo element; accept Z3 in second half.

**Key sessions:**
- Long ride: 3–4.5 hours. Include race-pace 20–40 min blocks.
- Long run: 90–150 min. Include 20–30 min at race-pace.
- Swim: 2–3 km sets. Include 2×750m at race pace.
- Bricks: minimum 1/week in build, 2/week in peak. Standard brick = 60–90 min ride + 20–30 min run.
- Race simulation: 1× in late build (3hr ride + 30 min run); 1× in peak (2.5hr ride + 20 min run).

**Taper:**
- Week –2: 60% of peak volume.
- Race week: 45% of peak volume.

**Goal time → pacing inputs:**

| Target finish | Bike IF | Run pace (vs threshold) | Swim pace |
|---|---|---|---|
| Sub 4:30 | 0.85–0.90 | –10% | CSS – 0:05 |
| Sub 5:00 | 0.80–0.85 | –12% | CSS |
| Sub 5:30 | 0.76–0.80 | –15% | CSS + 0:05 |
| Finish | 0.72–0.76 | –20% | CSS + 0:10 |

### 4.3 Other Event Types (Stubs)

The following events share the mesocycle algorithm and ramp rules. Their phase-specific distribution, key sessions, and taper ratios differ and will be expanded in a future version of this blueprint.

| Event | Status | Key divergence from ironman methodology |
|---|---|---|
| Marathon | Stub | Run-dominant; bike and swim as cross-training only in base. Peak = 3×20 min race-pace sessions. |
| Half Marathon | Stub | Higher Z4–5 run proportion in peak. Short taper (7–10 days). |
| 10k | Stub | Significant Z5–7 work in build/peak. Taper = 5–7 days. |
| 5k | Stub | Speed-dominant; Z5–7 forms 25% of weekly run volume in peak. |
| Ultramarathon | Stub | Volume-dominant; IF ceiling lower (0.60 base, 0.64 peak). Time-on-feet over pace. |
| Duathlon | Stub | Brick-heavy from base (run–bike–run format). No swim block. |
| Aquathlon | Stub | Swim–run format. Bike as cross-training. Transitions and pacing across disciplines key. |
| Road Sportive / Gran Fondo | Stub | Bike-only. Climbing-weighted if route is hilly. No run/swim block after base. |
| Gravel Race | Stub | Extended Z2–3 bike with power management. Bike handling skills integrated. |
| Endurance Swim | Stub | Swim-dominant. Run/bike as active recovery only. |

---

## 5. Brick Session Protocol

A brick session is any session in which a run immediately follows a bike, within 5 minutes of dismount. The goal is neuromuscular adaptation to the bike–run transition and practice of race-pace run mechanics on tired legs.

### Session Types

| Type | Bike | Run | When |
|---|---|---|---|
| Short brick | 30–45 min Z2 | 10–20 min easy | Base; early build |
| Standard brick | 60–120 min Z2–3 | 20–40 min Z2 | Build |
| Quality brick | 60–90 min with Z3–4 intervals | 20–30 min with Z3 blocks | Late build; peak |
| Long brick | 3–5 hrs Z2 (race simulation) | 45–90 min Z2 | Peak |

### Frequency

| Phase | Minimum bricks/phase | Notes |
|---|---|---|
| Base | 1 | Short brick only |
| Build | 2–3 | Mix of standard and quality |
| Peak | 3–4 | Include at least 1 long brick |

### Flexibility

The run component of a brick can follow any long bike session. The brick is classified by the **bike session type**, not the run. A recovery-week long ride followed by a short easy run still counts as a brick and is encouraged.

The 5-minute dismount-to-run rule applies to race simulations. For regular training bricks, a 5–10 minute transition is acceptable to allow for nutrition, shoe change, and brief mobility.

---

## 6. Environmental Protocols

Environmental protocols are **parallel layers** overlaid on the standard phase plan. They do not replace phase structure; they adjust session execution and add targeted adaptation sessions.

### 6.1 Heat Acclimation

**Trigger:** `race_conditions = hot` in athlete profile (ambient temperature at race venue > 28°C or significant humidity).

**Protocol — passive sauna (preferred where practical):**
- Start: 3–4 weeks before race (earlier if race is in weeks 4–8 from plan start).
- Frequency: 3–4 × per week.
- Duration: 20–30 min at 80–90°C, immediately post-exercise (heart rate ≤ 110 bpm on entry).
- Hydration: 500ml electrolyte drink before entering; replace all sweat lost.

**Protocol — outdoor training in heat:**
- Perform 2–3 sessions/week in hottest part of day (peak ambient ≥ 25°C).
- HR cap: add 5 bpm to all zone boundaries. Accept pace/power reduction.
- Cooling strategy: pre-cooling vest for runs >45 min; cold towel at aid stations.

**Adaptation markers:** resting HR should drop 3–5 bpm within 2 weeks; plasma volume expansion typically 10–15% after 10–14 days.

**Race-day execution:** pre-cool 30–45 min before start. Plan for HR to run 5–8 bpm higher than training equivalent. Reduce bike IF by 0.02–0.03 vs temperate target.

### 6.2 Altitude

**Trigger:** `altitude_m > 1500` in profile (live-high or train-high protocol) or race at altitude.

**Live-high train-low:** if athlete lives at altitude > 2000m, training sessions may need to remain at lower elevation for quality. Zone power targets unchanged but HR will be elevated.

**Train-high:** if training at 1500–2500m, reduce intensity targets by 5–8%. Allow 10–14 days full acclimatisation before quality sessions.

**Race at altitude:** taper at altitude is preferred. If flying in <48 hours before, the acute phase is preferable to arriving 2–5 days out (worst window for performance).

### 6.3 Cold Water

**Trigger:** race swim temperature < 15°C or `race_conditions = cold_water` in profile.

**Adaptation sessions:** 2–4 cold open water swims in the 4 weeks before race. Water temperature targets: week 4 ≥ race temp + 3°C, week 1 = race temp.

**Execution:** wetsuit mandatory if available. Focus on breathing control in the first 400m. Bilateral breathing reduces hyperventilation risk.

---

## 7. Course Modifiers

Course modifiers adjust the bike volume and intensity distribution in build and peak based on race terrain.

| Modifier | Elevation (per 100km) | Bike distribution adjustment |
|---|---|---|
| Flat | < 500m | Standard as per event profile |
| Rolling | 500–1000m | +5% Z3 in Build; add 1×45 min sweet-spot/week |
| Hilly | 1000–2000m | +10% Z4–5 in Build; add climbing repeats (2×20 min Z4); reduce overall volume 5% |
| Mountainous | > 2000m | Specialist climbing blocks; reduce weekly volume 10%; all long rides on hilly terrain |

For run course modifiers (significant elevation):
- Hilly run course: add 1×90 min trail/hilly run/week in build; include 4×5 min uphill repeats Z4.
- Flat course: prioritise pace work over terrain variety.

---

## 8. Fuelling Protocol

Fuelling is a **parallel protocol layer** that progresses across phases. The goal is race-day gut tolerance at target intake rate, developed through systematic gut training.

### 8.1 Phase-Progressive CHO Targets (g/hr)

| Phase | Ironman bike + run target | 70.3 bike + run target | Session type |
|---|---|---|---|
| Base | 40–55 | 40–55 | All sessions > 60 min |
| Build | 60–75 | 55–65 | All sessions > 45 min |
| Peak | 75–90 | 65–75 | All sessions; race simulation at race rate |
| Taper (race sim) | 80–90 | 70–80 | Race-simulation sessions only |

### 8.2 Product Progression

- Base: introduce primary fuelling format (gels, bars, or real food). Alternate products to identify tolerability.
- Build: standardise to 2 product types maximum. Test under race conditions (higher HR, heat, fatigue).
- Peak: use race-day products exclusively on key sessions. Zero experimentation.

### 8.3 Hydration Targets

| Conditions | Fluid target (ml/hr) | Electrolytes |
|---|---|---|
| Temperate (< 20°C) | 500–650 | ~400mg sodium/hr |
| Warm (20–28°C) | 650–800 | ~600mg sodium/hr |
| Hot (> 28°C) | 800–1000 | ~800–1000mg sodium/hr |

Sweat rate test recommended in Base (weigh before/after 60 min effort; difference in grams = ml sweat lost).

### 8.4 Pre-session and Recovery Nutrition

- Pre-session (>90 min): 60–90g CHO 2–3 hours before; 30g in final 30 min if needed.
- Recovery: 1g protein/kg body weight within 30 min of session end; CHO to match glycogen needs.

---

## 9. Recovery Triggers

These are automatic flags that modify the following week's plan. `generate-blueprint.py` does not apply these (they are live/reactive), but the weekly watchdog script and bot use them.

| Signal | Source | Action |
|---|---|---|
| HRV > 15% below 7-day average | Morning wellness | Flag: suggest Z1 session or rest day |
| RHR > 5 bpm above 7-day average | Morning wellness | Flag: suggest Z1 session or rest day |
| TSB < –30 (very high fatigue) | Intervals.icu fitness | Flag: insert 3-day recovery block |
| Injury pain score ≥ 3/10 | Athlete self-report | Flag: modify sport accordingly; halt affected limb |
| Sleep < 6 hours on 2 consecutive nights | Wellness input | Suggest to reduce next-day session intensity |
| >2 sessions missed in a week | Session log | Mark week as recovery week; do not ramp following week |

---

## 10. Test / Retest Schedule

Performance tests anchor the plan. All zone targets are recalculated from test results.

### 10.1 Cycling FTP

| Phase | Timing | Protocol |
|---|---|---|
| Pre-plan (baseline) | Day 0 | 20-minute FTP test (×0.95) or ramp test |
| Mid-base | End of week 4–6 | Ramp test (lower fatigue cost) |
| End of build | Final recovery week | 20-minute test preferred |
| Post-peak | Not recommended (taper); use race data |

If live FTP rises > 8% from previous: recalculate all zone targets immediately.

### 10.2 Running Threshold / LTHR

| Phase | Timing | Protocol |
|---|---|---|
| Pre-plan | Day 0 | 30-minute time trial; avg HR of final 20 min = LTHR |
| End of base | Final recovery week | Same protocol |
| End of build | Final recovery week | Same protocol |

### 10.3 Swim CSS

| Phase | Timing | Protocol |
|---|---|---|
| Pre-plan | Day 0 | 400m + 200m time trial (CSS calculator) |
| Mid-build | Week 8–10 | Repeat |

### 10.4 Test Week Rules

- Tests are performed in the first 2 days of a recovery week, when fatigue is dropping but not yet fully cleared.
- No other quality sessions in test week.
- If an athlete is injured or unwell, defer test by one week.

---

## 11. Analysis Layers

Two distinct analysis tiers operate in parallel.

### 11.1 Immediate Analysis (Per-Activity)

Triggered by `activity-watcher.py` within 15 minutes of activity completion.

**Structured rides:** interval set summary (count × duration @ avg power, % FTP), completion vs target, nutrition prompt.

**Unstructured rides / long rides:** NP, IF, aerobic decoupling (Pa:HR) for rides > 90 min. Nutrition prompt.

**Runs:** distance, avg GAP pace vs threshold, HR zone adherence, HR cap adherence where applicable, RPE prompt (or injury pain score if active injury).

**Swims:** distance, avg pace per 100m vs CSS, RPE prompt.

**Strength:** duration, RPE and focus prompt.

Post-session inline shortcuts (Telegram inline keyboard) allow rapid capture of:
- RPE (1–10)
- Injury pain score (0–10, if applicable)
- Carb intake (g/hr)
- Bottles consumed

### 11.2 Trend Analysis (Weekly Summary)

Produced by the weekly summary script and stored in `athletes/{slug}/athlete-summary.json`.

Covers: rolling 28-day TSS, CTL/ATL/TSB trend, training load vs plan adherence, per-sport distribution check (actual vs blueprint target), injury trajectory, weight trend, fuelling compliance, test results history.

Flags triggered if:
- Actual distribution drifts > 10% from blueprint target for > 2 consecutive weeks.
- CTL growth rate exceeds ramp rule for > 1 week.
- Injury pain scores trending upward over 7 days.

---

## Appendix A — generate-blueprint.py Parameter Reference

The script reads these fields from `athletes/{slug}/profile.json`:

| Field | Required | Default | Description |
|---|---|---|---|
| `slug` | Yes | — | Athlete identifier |
| `race_date` | Yes | — | ISO date string |
| `race_distance` | Yes | — | One of: `Full Ironman`, `70.3`, `Marathon`, `Half Marathon`, `10k`, `5k`, `Ultra`, `Duathlon`, `Aquathlon`, `Sportive`, `Gravel` |
| `ftp_watts` | Yes | — | Current outdoor FTP |
| `indoor_ftp_watts` | No | `ftp_watts` | If different |
| `swim_css_per_100m` | No | `null` | CSS pace in seconds |
| `run_threshold_pace_per_km` | No | `null` | In seconds per km |
| `max_hours_per_week` | Yes | — | Hard ceiling |
| `race_conditions` | No | `temperate` | `hot`, `cold_water`, `altitude` |
| `altitude_m` | No | `0` | Race venue altitude |
| `course_type` | No | `flat` | `flat`, `rolling`, `hilly`, `mountainous` |
| `a_goal` | No | — | Used in pacing inputs |

Output: `athletes/{slug}/reference/training-blueprint.md`

---

## Appendix B — Example Blueprint Outputs

### Athlete A — Full Ironman, 18.7 weeks out, 15 hr/week

```
Weeks to race: 18.7 → Phase structure: Base (6w) → Build1 (4w) → Build2 (4w) → Peak (2w) → Taper (2w)

TSS ceiling: 634 (base) → 694 (build) → 778 (peak)
Current CTL: 78 → Above build entry target (70–85). No fitness check required.

Phase start dates:
  Base:   2026-05-12 → 2026-06-22
  Build1: 2026-06-23 → 2026-07-20
  Build2: 2026-07-21 → 2026-08-17
  Peak:   2026-08-18 → 2026-08-31
  Taper:  2026-09-01 → 2026-09-19 (race)

Course: Rolling — +5% Z3 bike in Build
Heat: Active (race temp >28°C) — sauna protocol begins 2026-08-25
Fuelling: Base 40–55 g/hr → Build 60–75 g/hr → Peak 75–90 g/hr

Tests:
  FTP baseline:   2026-05-12 (or next recovery day)
  FTP mid-base:   ~2026-06-01
  FTP end-build:  ~2026-08-10
  LTHR baseline:  2026-05-12
  LTHR end-base:  ~2026-06-22
  CSS baseline:   2026-05-12
  CSS mid-build:  ~2026-07-27
```

### Athlete B — 70.3, 18.7 weeks out, 11 hr/week

```
Weeks to race: 18.7 → Phase structure: Base (6w) → Build1 (4w) → Build2 (4w) → Peak (2w) → Taper (2w)

TSS ceiling: 465 (base) → 509 (build) → 570 (peak)
Current CTL: 52 → Within build entry range (55–65). No fitness check required.

Phase start dates:
  Base:   2026-05-12 → 2026-06-22
  Build1: 2026-06-23 → 2026-07-20
  Build2: 2026-07-21 → 2026-08-17
  Peak:   2026-08-18 → 2026-08-31
  Taper:  2026-09-01 → 2026-09-20 (race)

Course: Flat — standard distribution
Heat: Check race forecast; protocol not active
Fuelling: Base 40–55 g/hr → Build 55–65 g/hr → Peak 65–75 g/hr

Tests:
  FTP baseline:   2026-05-12
  FTP mid-base:   ~2026-06-01
  FTP end-build:  ~2026-08-10
  LTHR baseline:  2026-05-12
  CSS baseline:   2026-05-12
  CSS mid-build:  ~2026-07-27
```
