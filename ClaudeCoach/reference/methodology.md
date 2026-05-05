# Run training methodology + Stryd reference

**TL;DR:** Six methods (Canova / Daniels VDOT / 80/20 / Pfitzinger / Norwegian Double Threshold / Blended). For this build use **Blended**, with Canova-style race-pace progressions dominant from July onward. Stryd CP zones and race-power targets at the bottom — Stryd not currently in kit, included as lookup if added.

> Conflicts: marathon-specific pacing/power numbers do **not** transfer verbatim to the IM run leg (~5 hrs of cycling first). See `02-conflicts.md` §2.

## Universal agreement across methods

1. Easy runs must be genuinely easy — most amateurs run them too fast.
2. Consistency beats heroics.
3. Periodisation works — phase-based build to race day.
4. The long run is the cornerstone for marathon and ultra distances.
5. Specificity — the body adapts to what it is trained to do.

## Method comparison

| Method | Best for | Core idea | Signature session | Intensity split |
|---|---|---|---|---|
| **Canova** | Experienced runners chasing time goals | Train *closer to race pace* using "adjacent speeds" — at and around goal pace, not just easy or flat-out hard. Phases: Transition → General → Fundamental → Specific. | Canova alternating: 1 km @ 105% MP / 1 km @ MP, 8–12 reps. | ~65% easy / ~25% MP±5% / ~10% above threshold |
| **Daniels VDOT** | All levels; runners wanting clean formula-based targets | One race result → VDOT → all paces calculated. Five zones: E / M / T / I / R. Calculator: vdoto2.com. | Cruise intervals: 4–6 × 1.6 km @ T-pace, 60 sec rest. | Standard distribution by phase |
| **80/20 Polarised (Seiler)** | High-mileage runners, ultras, anyone chronically tired or plateauing | Elites spend ~80% genuinely easy, ~20% genuinely hard, almost nothing moderate. Eliminate the grey zone. | Long Z1 + true Z3+ intervals. | 80% Z1 / 20% Z3+ |
| **Pfitzinger** | Experienced marathoners at 90–110+ km/wk | High consistent mileage, pyramid intensity. Signature: medium-long run mid-week (18–22 km) on top of the weekly long run — double endurance stimulus. | Plans 18/55, 18/70, 18/85+ (88 / 112 / 137+ km peak weeks). | Pyramid |
| **Norwegian Double Threshold** ⚠ advanced | Experienced runners 80+ km/wk who can control intensity precisely | 3–4 threshold sessions per week as AM+PM doubles, kept *below* lactate accumulation. Volume *at* threshold, not above. Lactate meter recommended. | 4×4: 4 × 4 min @ 90–95% HRmax / 3 min jog. Drop in once a week alongside any other method. | Precise threshold control |
| **Blended** | Most runners, including this build | Use the right method for each phase. | See phase table below. | See phase table |

## Blended phase mapping (this build)

| Phase | Dates | Method dominant | Why |
|---|---|---|---|
| Base | now → end May | 80/20 (high easy bike volume; ankle-led run) | Ankle return-to-run; aerobic capacity before quality |
| Build | June | Daniels VDOT zones (bike threshold + over-FTP) | Clean zone-based stimulus while ramping CTL |
| Specific | July | Canova — race-pace progressions | Race-specific pacing under fatigue, paired with race-pace bricks |
| Peak | mid-Aug | Pfitzinger-style (medium-long mid-week run) | Consolidates run endurance during longest week |
| Taper | Sept (3 wk) | All methods agree — cut volume, preserve intensity | |

For the IM run-specific block from July: Canova alternating-pace and race-pace progressions, paired with brick race-pace runs off the bike. **Numbers don't transfer from a marathon Canova prescription** — derate for fatigued legs and heat.

## Which method when (lookup)

| Situation | Recommendation |
|---|---|
| New to structured training | Daniels VDOT |
| Chronically tired / injured | 80/20 |
| Experienced, chasing a time goal | Canova or Blended |
| High mileage, marathon focus | Pfitzinger |
| Plateauing, very experienced | Norwegian Double Threshold |
| Not sure | Blended |
| **This athlete (IM build, ankle in rehab)** | **Blended — see phase table above** |

---

## Stryd quick reference

> Stryd is **not currently in the kit list**. This section is a *what it would tell you* lookup. Adding a foot pod activates this table.

### Zones from Critical Power (CP)

| Zone | % of CP | Purpose |
|---|---|---|
| Z1 Recovery | <76% | Post-race, very easy |
| Z2 Endurance | 76–86% | Daily runs, long runs |
| Z3 Marathon | 86–96% | Race zone (standalone marathon) |
| Z4 Threshold | 96–106% | Hard reps |
| Z5 VO2max | 106–120% | Peak efforts |

### Race power targets by distance

| Distance | Power (% CP) |
|---|---|
| Half marathon | 96–102% |
| Marathon (standalone) | 88–96% |
| **IM run leg** | **80–88% CP working range; adjust by RPE/HR in heat** |
| 50 km ultra | 72–80% |
| 80 km+ ultra | 65–75% |

The IM target is below standalone marathon — similar effort sensation, but on already-fatigued legs after ~5 hrs cycling.

### Running economy — green and red flags

| Metric | Green | Red flag |
|---|---|---|
| GCT | Trending down across weeks | Rising *within* a session = fatigue |
| Form Power % | Trending down | Higher than usual = poor recovery |
| Cadence | Stable or rising | Dropping on easy runs = tired legs |
| Same power, lower HR | ✅ Fitness improving | Same power, higher HR = fatigue/illness |

### Sharing data with Claude

- Stryd FIT file: Stryd app → activity → Export → Export FIT File. Then ask for power-zone, cadence, GCT, VO, economy analysis.
- Intervals.icu: share profile link or activity URL — fitness trend, training load, fatigue, flags. Already wired via IcuSync.
- Strava: auto-syncs from Garmin but strips some metrics — use raw Stryd FIT for detailed power analysis.
