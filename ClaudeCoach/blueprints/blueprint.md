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

**Specific** — convert fitness to race shape: work slightly above race effort (Z3 muscular endurance), race-IF finishes on long sessions, race-rate fuelling on ALL key sessions, bricks at race effort. First race simulation late in this phase.

**Peak** — quality over quantity. Volume reduces by 15–20% vs build. Intensity maintained or sharpened. Final long race-simulation sessions in first half of peak.

**Taper** — volume steps down across the taper to ~70% → 55% → 40% of peak week (per-event tables in §4 are authoritative). Intensity touches maintained (2–3 short sharp sessions). TSS/day drops, CTL declines, TSB rises toward +5 to +15 by race eve.

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
| Taper | — | steps 70→55→40% of peak week | steps 70→55→40% of peak week |

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

If the athlete's current CTL is **> phase target × 1.10**, they are **fitter than the goal
needs**. What to do with that is **the athlete's call** (Jamie, 29 Sep 2026), never decided
for them. The weekly plan asks, and holds Fitness until they answer:

| Choice | What the plan does |
|---|---|
| `hold_shift` | Hold total Fitness and shift the mix toward the goal sport (e.g. more running, let swimming fade) |
| `drift` | Let total Fitness drift down, never below the athlete's floor (§2.1), and put the freed time into speed |
| `raise_goal` | Hold, while a faster goal and its targets are agreed |

The answer is saved per athlete (`fitness_surplus_choice`, set by the coach bot with
`plan_tools.py fitness-choice`) and applies until they change it. `generate-blueprint.py`
reports the same decision (`AWAITING_DECISION` until one is recorded). Constants:
`lib/race_fitness.py`.

If CTL is **< phase target × 0.85** (significantly below), the script logs a warning but proceeds, noting that phase TSS targets will be moderated to the athlete's actual fitness level.

### 2.1 Run Races — Hybrid Fitness Floors

A run race is judged on **two floors, each on its own**:

- **Running Fitness** (42-day average of run load only) against the race's range below.
- **Total Fitness** against the **athlete's own total floor** (`ctl_targets.total_fitness_floor`,
  else the bottom of their `maintenance_ctl_band`). Athletes without one get only the running floor.

A triathlete can be far above a 5k's needs on total and still under it on running (Jamie,
29 Sep 2026: total 95, running 32), so neither number alone says whether they are ready.

**Cycling counts, a little** (Jamie, 30 Sep 2026). Cycling builds the aerobic engine but not
running economy (cross-training studies: roughly 70–90% of the aerobic effect, no economy
gain), so **a third of cycling Fitness** counts toward running Fitness, and **at least three
quarters of the range's floor must come from running itself**. Example: running 60 + cycling
30 counts as 70, the bottom of a sub-3 marathon's range, because 60 of it is running.

**Run races are planned from running** (Jamie, 1 Oct 2026). Each week's load is built from
the RUNNING target: enough run load to reach the level's running Fitness by the end of the
phase, growing at most 15% a week over the load that holds current running Fitness (the
+10–15% run rule in load terms). Bike and swim only top the week up to keep total Fitness
at the athlete's floor. Total phase targets are a floor here, never a number the week
chases, so a shortfall the run caps will not allow stays a short week, not extra riding.
Triathlons and bike events keep total Fitness as the target.

**How much running Fitness** depends on the athlete's level for that race: see §4.5
(four levels per event, picked from the goal time).

Phase entry = the level's into-taper range × Base 0.70 / Build 0.80 / Specific 0.90 / Peak 0.95 / Taper 1.00.
Running above the range is "fitter than the goal" (§2); below it, the block builds running.

---

## 3. Intensity Distribution

Distribution is expressed as a **weekly average per sport**, not a per-session requirement. Some sessions will be pure Z1–2; others will hit Z4–5. The weekly average across all sessions of that sport should land within the target band.

### 3.1 Zone Definitions

**Cycling (power-based — intervals.icu 7-zone scheme; the athlete's live sportSettings in intervals.icu override this reference table):**

| Zone | % FTP | Description |
|---|---|---|
| Z1 | < 55% | Active recovery |
| Z2 | 55–75% | Aerobic endurance |
| Z3 | 75–90% | Tempo / sweet spot |
| Z4 | 90–105% | Threshold |
| Z5 | 105–120% | VO₂ max |
| Z6 | 120–150% | Anaerobic capacity |
| Z7 | > 150% | Neuromuscular |

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

**Taper carries Peak's proportions unchanged.** These are SHARES of a week's time in
zone, and §2's taper rule is that *volume* steps down (70% → 55% → 40% of peak week)
while *intensity touches are maintained* — so the absolute quality minutes fall with
the volume and the mix does not. A Taper row is stated explicitly rather than left
blank because blank is indistinguishable from "not yet decided", and the taper is the
one week where an all-race-pace calendar would otherwise pass unexamined (added
11 Sep 2026). Treat it as a conservative Z1–2 FLOOR, not a prescription: it is the
number that catches a taper week with no easy work left in it.

**Full Ironman:**

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 70% Z1–2 / 20% Z3–4 / 10% Z5 | 80% Z1–2 / 12% Z3 / 8% Z4–5 | 85% Z1–2 / 10% Z3 / 5% Z4–5 |
| Build | 65% Z1–2 / 25% Z3–4 / 10% Z5 | 75% Z1–2 / 15% Z3 / 10% Z4–5 | 80% Z1–2 / 12% Z3 / 8% Z4–5 |
| Peak | 60% Z1–2 / 25% Z3–4 / 15% Z5 | 70% Z1–2 / 15% Z3 / 15% Z4–5 | 75% Z1–2 / 12% Z3 / 13% Z4–5 |
| Taper | 60% Z1–2 / 25% Z3–4 / 15% Z5 | 70% Z1–2 / 15% Z3 / 15% Z4–5 | 75% Z1–2 / 12% Z3 / 13% Z4–5 |

**70.3 / Half Ironman:**

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 70% Z1–2 / 20% Z3–4 / 10% Z5 | 78% Z1–2 / 14% Z3 / 8% Z4–5 | 83% Z1–2 / 12% Z3 / 5% Z4–5 |
| Build | 65% Z1–2 / 22% Z3–4 / 13% Z5 | 70% Z1–2 / 18% Z3 / 12% Z4–5 | 78% Z1–2 / 12% Z3 / 10% Z4–5 |
| Peak | 58% Z1–2 / 25% Z3–4 / 17% Z5 | 65% Z1–2 / 18% Z3 / 17% Z4–5 | 72% Z1–2 / 14% Z3 / 14% Z4–5 |
| Taper | 58% Z1–2 / 25% Z3–4 / 17% Z5 | 65% Z1–2 / 18% Z3 / 17% Z4–5 | 72% Z1–2 / 14% Z3 / 14% Z4–5 |

**Olympic, Sprint and run races** (added 30 Sep 2026). Shorter events carry more Z4–5; the run races are pyramidal, with the easy share growing with volume (Doherty et al. 2024: faster marathoners add Z1, not Z2–3).

**Olympic:**

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 70% Z1–2 / 20% Z3–4 / 10% Z5 | 77% Z1–2 / 13% Z3 / 10% Z4–5 | 82% Z1–2 / 10% Z3 / 8% Z4–5 |
| Build | 65% Z1–2 / 23% Z3–4 / 12% Z5 | 72% Z1–2 / 15% Z3 / 13% Z4–5 | 78% Z1–2 / 10% Z3 / 12% Z4–5 |
| Peak | 60% Z1–2 / 25% Z3–4 / 15% Z5 | 68% Z1–2 / 15% Z3 / 17% Z4–5 | 74% Z1–2 / 12% Z3 / 14% Z4–5 |
| Taper | 60% Z1–2 / 25% Z3–4 / 15% Z5 | 68% Z1–2 / 15% Z3 / 17% Z4–5 | 74% Z1–2 / 12% Z3 / 14% Z4–5 |

**Sprint:**

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 70% Z1–2 / 20% Z3–4 / 10% Z5 | 75% Z1–2 / 12% Z3 / 13% Z4–5 | 80% Z1–2 / 10% Z3 / 10% Z4–5 |
| Build | 65% Z1–2 / 22% Z3–4 / 13% Z5 | 70% Z1–2 / 13% Z3 / 17% Z4–5 | 75% Z1–2 / 10% Z3 / 15% Z4–5 |
| Peak | 60% Z1–2 / 25% Z3–4 / 15% Z5 | 65% Z1–2 / 15% Z3 / 20% Z4–5 | 72% Z1–2 / 10% Z3 / 18% Z4–5 |
| Taper | 60% Z1–2 / 25% Z3–4 / 15% Z5 | 65% Z1–2 / 15% Z3 / 20% Z4–5 | 72% Z1–2 / 10% Z3 / 18% Z4–5 |

**Marathon:**

Swim and bike are cross-training for a run race (easy aerobic, a little intensity) and apply only to athletes who keep them.

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 85% Z1–2 / 10% Z3 / 5% Z4–5 |
| Build | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 80% Z1–2 / 12% Z3 / 8% Z4–5 |
| Peak | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 78% Z1–2 / 14% Z3 / 8% Z4–5 |
| Taper | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 78% Z1–2 / 14% Z3 / 8% Z4–5 |

**Half Marathon:**

Swim and bike are cross-training for a run race (easy aerobic, a little intensity) and apply only to athletes who keep them.

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 82% Z1–2 / 10% Z3 / 8% Z4–5 |
| Build | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 78% Z1–2 / 12% Z3 / 10% Z4–5 |
| Peak | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 75% Z1–2 / 14% Z3 / 11% Z4–5 |
| Taper | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 75% Z1–2 / 14% Z3 / 11% Z4–5 |

**10k:**

Swim and bike are cross-training for a run race (easy aerobic, a little intensity) and apply only to athletes who keep them.

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 82% Z1–2 / 8% Z3 / 10% Z4–5 |
| Build | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 78% Z1–2 / 10% Z3 / 12% Z4–5 |
| Peak | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 75% Z1–2 / 10% Z3 / 15% Z4–5 |
| Taper | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 75% Z1–2 / 10% Z3 / 15% Z4–5 |

**5k:**

Swim and bike are cross-training for a run race (easy aerobic, a little intensity) and apply only to athletes who keep them.

| Phase | Swim | Bike | Run |
|---|---|---|---|
| Base | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 82% Z1–2 / 8% Z3 / 10% Z4–5 |
| Build | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 77% Z1–2 / 8% Z3 / 15% Z4–5 |
| Peak | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 75% Z1–2 / 7% Z3 / 18% Z4–5 |
| Taper | 85% Z1–2 / 10% Z3–4 / 5% Z5 | 90% Z1–2 / 7% Z3 / 3% Z4–5 | 75% Z1–2 / 7% Z3 / 18% Z4–5 |

---

## 4. Event Profiles

Each event profile defines the race-specific demands that shape the build and peak phases. The base phase methodology is shared across all events; the divergence begins in Build.

### 4.1 Full Ironman

**Race demands:** 3.8km swim / 180km bike / 42.2km run. Total duration 9–17 hours. Dominantly aerobic (Z2). Bike pacing critical — target IF 0.68–0.72. Run is a controlled Z2 effort with final-km surge only.

**Key sessions:**
- Long ride: 4–6 hours at Z2 with fuelling practice. Frequency: weekly in build/peak. Durability: from build onwards the ride finishes WITH work — early build last 2×20 min at race IF, progressing to a continuous 60–90 min race-IF finish by peak (fatigue resistance is trained at intensity on tired legs, not by Z2 hours alone).
- Long run: 2–3.5 hours. Cap at 32km in build; pull back to 26km in peak.
- Swim: 3–4 km sets including 1×1500m continuous sub-race-pace effort.
- Bricks: minimum 1/week in build, 2/week in peak. Standard brick = 90–120 min ride + 30–40 min run.
- Race simulation: 1× late Specific (5hr ride + 60 min run; late build if the plan has no Specific phase); 1× in peak (4hr ride + 45 min run) — the second sim re-tests whatever the first exposed.

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
| Olympic | **Implemented** (§3.2, §4.5) | 1.5 km / 40 km / 10 km. More Z4–5 than long course; short, sharp bricks. Taper 7–10 days. |
| Sprint | **Implemented** (§3.2, §4.5) | 750 m / 20 km / 5 km. The most Z4–5 of the triathlons. Taper 5–7 days. |
| Marathon | **Implemented** (§3.2, §4.5) | Run-dominant; bike and swim as cross-training only in base. Peak = 3×20 min race-pace sessions. Taper 2–3 weeks. |
| Half Marathon | **Implemented** (§3.2, §4.5) | Higher Z4–5 run proportion in peak. Taper 7–10 days. |
| 10k | **Implemented** (§3.2, §4.5) | Significant Z5–7 work in build/peak. Taper 5–7 days. |
| 5k | **Implemented** (§3.2, §4.5) | Speed-dominant; Z5–7 forms 25% of weekly run volume in peak. Taper 5–7 days. |
| Ultramarathon | Stub | Volume-dominant; IF ceiling lower (0.60 base, 0.64 peak). Time-on-feet over pace. |
| Duathlon | Stub | Brick-heavy from base (run–bike–run format). No swim block. |
| Aquathlon | Stub | Swim–run format. Bike as cross-training. Transitions and pacing across disciplines key. |
| Road Sportive / Gran Fondo | **Implemented** (`Sportive`) | Bike-only: bike distribution by phase (Base 80/12/8 → Build 70/18/12 → Peak 65/18/17 → Taper 65/18/17 Z1–2/Z3/Z4–5), FTP tests only (no LTHR/CSS), no bricks. Climbing-weighted via the course modifier on hilly routes. |
| Gravel Race | **Implemented** (maps to `Sportive`; own levels §4.5) | Shares the Sportive bike-only profile; extended Z2–3 with power management. |
| Endurance Swim | Stub | Swim-dominant. Run/bike as active recovery only. |

### 4.4 Race Priority — A / B / C

Every race in the registry (`races` in athletes.json) and every booked off-season attempt
carries a priority. The planner applies it; constants in `lib/race_fitness.py`.

| | A | B | C |
|---|---|---|---|
| Plan built around it | Yes, the whole block | No | No |
| Taper | The event's full taper (§4.1, §4.2, §2.1) | 3 easier days before; race week at **65%** load | None: an easy day before |
| Recovery after | Ironman 3 weeks, 70.3 ~10 days, marathon 1–2 weeks, half ~1 week, 5k/10k 2–3 days | 3 easy days | 1 easy day |
| How many | 1–2 a year | 2–4 | Any |

"Easy" = no hard work (nothing over 10 min at ≥ 0.90 IF). The easy days either side of a
B or C race carry across week boundaries, so a Saturday race's recovery lands in the next
week's plan. A B race in the registry makes its week an easy week automatically; a
hand-declared `manual_easy_weeks` entry for that week still wins.

### 4.5 Event Levels — Volume and Fitness by Goal

Every event has **four levels**, picked by the athlete's **goal time** (else the time their
run threshold predicts, else their current Fitness; sportive and gravel riders say their
level). A 4-hour marathoner is not asked for a sub-3 runner's 90 km a week. Numbers are the
sustained peak weeks of the build and the Fitness to carry into the taper: **running**
Fitness for run races (with the athlete's own total floor, §2.1), **total** Fitness for
triathlons and bike events. Source: `config/event-levels.json`, via `lib/race_fitness.py`.

Evidence: marathon volume by finish time from Doherty et al. 2024 (Sports Medicine,
151,813 marathons by 119,452 runners); taper 2 weeks at −41–60% volume with intensity kept
(Bosquet et al. 2007 meta-analysis), 3 weeks for the marathon; weekly run-distance jumps
over 30% raise distance injuries (Nielsen et al. 2014). Half/10k/5k and triathlon levels are
coaching estimates scaled from those anchors, and Fitness ≈ 0.9 × run km/week (running) or
≈ 6 × hours/week (total), from this system's own athletes.

<!-- event-levels:start -->

**5k** (taper 5–7 days)

| Level | Goal | Run km/week | Long run | Running Fitness into taper |
|---|---|---|---|---|
| 1 | sub 19:00 | 45–65 | 14–18 km | 40–58 |
| 2 | 19–23 min | 30–45 | 11–14 km | 27–40 |
| 3 | 23–27 min | 20–35 | 8–11 km | 18–32 |
| 4 | 27 min+ | 12–25 | 6–9 km | 11–22 |

**10k** (taper 5–7 days)

| Level | Goal | Run km/week | Long run | Running Fitness into taper |
|---|---|---|---|---|
| 1 | sub 40 | 50–70 | 16–20 km | 45–63 |
| 2 | 40–47 min | 35–50 | 13–16 km | 32–45 |
| 3 | 47–55 min | 25–40 | 10–13 km | 22–36 |
| 4 | 55 min+ | 15–30 | 8–11 km | 14–27 |

**Half Marathon** (taper 7–10 days)

| Level | Goal | Run km/week | Long run | Running Fitness into taper |
|---|---|---|---|---|
| 1 | sub 1:25 | 60–80 | 18–22 km | 55–72 |
| 2 | 1:25–1:40 | 45–60 | 18–20 km | 40–55 |
| 3 | 1:40–2:00 | 30–45 | 16–19 km | 27–40 |
| 4 | 2:00+ | 20–35 | 14–18 km | 18–32 |

**Marathon** (taper 14–21 days)

| Level | Goal | Run km/week | Long run | Running Fitness into taper |
|---|---|---|---|---|
| 1 | sub 3:00 | 80–110 | 32–35 km | 70–95 |
| 2 | 3:00–3:30 | 60–80 | 30–32 km | 55–72 |
| 3 | 3:30–4:15 | 45–60 | 28–32 km | 40–55 |
| 4 | 4:15+ | 30–45 | 26–30 km | 27–40 |

**Sprint triathlon** (taper 5–7 days)

| Level | Goal | Hours/week | Long ride | Long run | Total Fitness into taper |
|---|---|---|---|---|---|
| 1 | sub 1:10 | 8–11 | 2–2.5 h | 12–15 km | 50–65 |
| 2 | 1:10–1:20 | 6–8 | 1.5–2 h | 10–12 km | 38–50 |
| 3 | 1:20–1:35 | 4.5–6 | 1.25–1.5 h | 8–10 km | 28–38 |
| 4 | 1:35+ | 3–4.5 | 1–1.25 h | 6–8 km | 18–28 |

**Olympic triathlon** (taper 7–10 days)

| Level | Goal | Hours/week | Long ride | Long run | Total Fitness into taper |
|---|---|---|---|---|---|
| 1 | sub 2:15 | 10–13 | 2.5–3 h | 15–18 km | 60–78 |
| 2 | 2:15–2:35 | 8–10 | 2–2.5 h | 12–15 km | 48–60 |
| 3 | 2:35–3:00 | 6–8 | 1.5–2 h | 10–12 km | 36–48 |
| 4 | 3:00+ | 4–6 | 1.25–1.5 h | 8–10 km | 25–36 |

**70.3 / Half Ironman** (taper 10–14 days)

| Level | Goal | Hours/week | Long ride | Long run | Total Fitness into taper |
|---|---|---|---|---|---|
| 1 | sub 4:45 | 12–15 | 3.5–4 h | 18–21 km | 72–90 |
| 2 | 4:45–5:30 | 10–12 | 3–3.5 h | 16–19 km | 60–72 |
| 3 | 5:30–6:15 | 8–10 | 2.5–3 h | 14–16 km | 48–60 |
| 4 | 6:15+ | 6–8 | 2–2.5 h | 12–14 km | 36–48 |

**Ironman** (taper 14–21 days)

| Level | Goal | Hours/week | Long ride | Long run | Total Fitness into taper |
|---|---|---|---|---|---|
| 1 | sub 10:00 | 15–20 | 5–6 h | 30–35 km | 95–120 |
| 2 | 10:00–11:30 | 12–15 | 4.5–5.5 h | 28–32 km | 80–95 |
| 3 | 11:30–13:00 | 10–12 | 4–5 h | 24–28 km | 65–80 |
| 4 | 13:00+ | 8–10 | 3.5–4.5 h | 20–24 km | 50–65 |

**Sportive / Gran Fondo** (taper 5–7 days)

| Level | Goal | Hours/week | Long ride | Total Fitness into taper |
|---|---|---|---|---|
| 1 | Competitive | 10–14 | 4.5–5.5 h | 70–90 |
| 2 | Strong | 8–10 | 4–4.5 h | 55–70 |
| 3 | Steady | 6–8 | 3–4 h | 40–55 |
| 4 | Finish | 4–6 | 2.5–3 h | 28–40 |

**Gravel race** (taper 5–7 days)

| Level | Goal | Hours/week | Long ride | Total Fitness into taper |
|---|---|---|---|---|
| 1 | Competitive | 10–14 | 5–6 h | 70–90 |
| 2 | Strong | 8–10 | 4–5 h | 55–70 |
| 3 | Steady | 6–8 | 3.5–4.5 h | 40–55 |
| 4 | Finish | 4–6 | 3–3.5 h | 28–40 |

<!-- event-levels:end -->

### 4.6 Bespoke Events — Temporary Blueprints

A race that is not a standard distance gets a **temporary blueprint blended from the two
nearest standard events** (Jamie, 30 Sep 2026), no new table needed:

- **Run race** (e.g. a 30k): blended by distance (log scale) between 5k / 10k / half /
  marathon. A 30k is about half half-marathon, half marathon. Goal bands scale by distance.
- **Multisport race** (e.g. a 4 km swim + 10 km bike): volume, Fitness and taper blended by
  the race's estimated duration between sprint / Olympic / 70.3 / Ironman; each sport's
  intensity split blended by that leg's distance against the same leg of the standard
  races. Sports not in the race drop out; a long swim leg sets an overdistance long swim.
- **Bike-only** uses the sportive levels. **Outside the tables** (an ultra, a sub-5k) the
  nearest table is used and the blueprint says so. Swim-only races are not covered yet.

Set with `plan_tools.py bespoke-event --athlete <slug> --name <race> --run-km/--bike-km/--swim-km`
(the coach bot runs it when an athlete names a non-standard race). It is bound to that
race and stops applying when another race is set. Code: `race_fitness.bespoke_event`.

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
