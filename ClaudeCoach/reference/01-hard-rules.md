# Hard rules — Claude self-check before any prescription

Athlete-specific overrides and standing constraints, no rationale. Re-read before issuing training, pacing, fuelling, recovery, or equipment recommendations. If a recommendation contradicts a rule here, the rule wins.

Source: project custom instructions + reconciled extract from this folder. Last reviewed 25 April 2026.

## Anchor values (Intervals.icu = source of truth)

- **Bike FTP: 316 W.** (Project doc said 320 — superseded by Intervals as of 2026-04-25; retest end-May.)
- **Run threshold pace: 4:02/km.** Run zones derive from this.
- **LTHR: 170. MaxHR: 190. RHR: 50–56.**
- **HR zones (run/ride):** Z1<133, Z2 133–146, Z3 146–159, Z4 159–173, Z5 173–190.
- **Bike power zones (% FTP):** Z1<55%, Z2 55–75%, Z3 75–90%, Z4 90–105%, Z5 105–120%, Z6 120–150%, Z7 150%+.
- **Run pace zones (% threshold pace):** Z1<77.5%, Z2 77.5–87.7%, Z3 87.7–94.3%, Z4 94.3–100%, Z5 100–103.4%, Z6 103.4–111.5%, Z7 >111.5%.
- **Race-day weight target: 79 kg** (current ~82, drift down ~0.15 kg/wk required, max 0.5 kg/wk to protect performance).
- **Bike gearing (current): Speedmax 52T × 10–33.** Lowest gear at 80 rpm = 15.9 km/h. **At gradients ≥6%, the IF 0.78 climb cap and 80 rpm cadence are mutually incompatible** — physics forces either low-cadence grind or above-cap power. 2025 race climb 3 at 56 rpm × 305 W is the data signature. **Resolve via chainring change (recommended: 46T or 48T sub-compact) or accept above-cap power on steep pitches and budget for it elsewhere.** See `cervia-course.md` Bertinoro section + `risk-register.md` R14.

## NEVER

- Never recommend gels as primary run-leg fuel. Sensory aversion (not GI). Use as backup chews only if at all.
- Never prescribe new fuel, kit, shoes, or pacing on race day or in the final 4 weeks of build.
- Never assume the ankle is fully healed. Ask for current pain status before adding *quality* (tempo/intervals/race-pace). Day-to-day Z1–Z2 prescription does not need a check unless something has changed.
- Never let weekly run-km increase >10% week-on-week as a general progression rule. (Was previously framed as "in rehab" — reframed 27 Apr 2026 since the rehab framing didn't match actual practice.)
- Never let CTL ramp exceed +4 CTL/wk while ankle is in rehab. (Cap is +3.5 in some weeks; check `ironman-analysis/` flags.)
- Never recommend dilution of Maurten beyond 500 ml or co-mixing electrolyte tabs into the same bottle.
- Never prescribe in-session CHO fuelling on runs <60 min or rides <90 min. Sessions below those thresholds are too short to test gut tolerance or absorption — fuel before/after only. Reserve CHO rehearsal for sessions long enough to actually train the gut.
- Never plan in-race nutrition or cooling around special-needs bags. Athlete declines them.
- Never plan in-race cooling around aid-station ice. Cervia course has effectively zero ice.
- Never use HRV alone as a load-reduction trigger. Multi-signal corroboration required (HRV + RHR + sleep).
- Never use subjective wellness fields (mood, motivation, soreness 1–10) as a decision input. Athlete does not log them.
- Never use system date for daily-TSS bucketing. Use athlete-local date (Europe/London) parsed from the activity's ISO datetime.
- Never trust a single date source when scheduling. Cross-check the application env date, the IcuSync `current_date_local`, and the day-of-week the user has implied or stated. If any two disagree, ask before pushing a workout. (The API's `current_date_local` lagged by ~24 hr on 27 Apr 2026 and led to a workout being pushed to the wrong day.)
- Always state the day-of-week alongside any date in scheduling, fitness reads, or fuelling commentary (e.g. "Mon 27 Apr", not "27 Apr"). Day-of-week is the unambiguous sanity check humans verify instantly.
- Never assume the source material in this folder applies verbatim. Check `02-conflicts.md` for athlete-specific overrides.
- Never frame the 2025 run failure as "blow-up at km 25". Actual pattern: aid-station walk-break overrun starting km 13, ~15 min cumulative cost. See `race-day-2025.md`.
- Never frame the 2025 bike failure as "heat collapse on Bertinoro lap 2". Actual pattern: Lap 1 paced near plan, Lap 2 aerobic durability fade (decoupling 14.5%, AP 202→157 W). See `race-day-2025.md`.

## ALWAYS

- Always state rationale in one sentence per recommendation: what physiological adaptation, what risk being mitigated, or what data point it responds to. No rationale → don't include.
- Always re-evaluate every recommendation against heat impact. Heat is the binding constraint on this race, not fitness.
- Always pull data via IcuSync before commenting on training. Never work from memory or paste-pretend. If IcuSync is down, say so.
- Always cross-validate across data inputs before prescribing a hard session. Calendar-says-hard is overridden by tanked HRV + poor sleep + elevated yesterday-RPE.
- Always report TSB absolute *and* percentage, e.g. "TSB –16 / –22%".
- Always label future-dated `get_fitness` rows as zero-training projections, not forecasts of fitness.
- Always flag speculation explicitly. Search the web before stating race-day facts (course details, partner products, qualifying conditions).
- Always reference caffeine doses to current body weight (target race-day 79 kg). Total race-day cap ~3 mg/kg given low habituation.
- Always flag that a Precision Hydration sweat test would replace assumption with data when discussing sodium.
- Always read `templates/current-state.md` before a weekly check-in or daily-readiness prompt.
- Always push planned workouts back to Intervals.icu via IcuSync in the same turn — single source of truth.
- Always include "Data provided by Garmin®" attribution at the foot of any output that includes activity-detail data sourced from Garmin.

## KPIs to track (race-grounded, from `race-day-2025.md`)

- **Bike aerobic decoupling** on every long ride >3 hr. Trend target 14% (race-day 2025) → <5% by August.
- **Bike VI** on every long ride. Target <1.05 in race rehearsal (race-day 2025: 1.14).
- **Aid-station discipline timer** in every long run from June. Target ≤30 sec/stop. Race-day 2025 leaked ~15 min to walk-break overrun.
- **HR-vs-pace decoupling on hot long runs (>22°C)**. Heat-tolerance KPI — measures the second-half HR suppression that cost run pace in 2025.
- **Long-ride NP at race wattage (210–225 W).** Track that AP holds across 4–5 hr without fade.
- **Sleep avg 7-day rolling.** Target 8 hr; current ~7.4 (deficit ~0.6 hr/night).
- **Strength sessions/week.** Target 2× minimum; current 0.3× — under-executed and protective for ankle.

## CURRENT RUN PROTOCOL (live, supersedes any stale text in project instructions)

Reviewed 27 Apr 2026 against last 4 weeks of actual run data. The previously-documented "30/30/60 run-walk 4:1 / Z1 / no progression / rehab" framing was stale. The protocol below reflects what the athlete is actually doing — and tolerating — and is the authoritative reference going forward.

- **Frequency:** 3 runs/week (Tue / Wed/Thu / Sat-or-Sun typical).
- **Duration:** mixed — 25–60 min per session. No fixed 30/30/60 split.
- **Format:** **run-walk 5:30 ratio** (5 min run / 30 sec walk). 10:1, ~91% running. Lap-recorded so structure shows on activity files.
- **Pace:** rolling-average ~4:50–5:05/km on easy days. Equates to mid Z2 (HR ~140–146). HR cap **150** — soft top of Z2.
- **Volume:** 18–25 km/week is normal; not a stretch. Apply the ≤10% w/w rule for anything above this.
- **Ankle status:** mild niggle present, not flaring. Tolerating current load with no day-after pain. Not in active rehab.
- **Veto trigger:** pain >2/10 during or after the run, OR limp, OR next-morning soreness. Trigger fires → drop the run, replace with Z1 spin or aqua jog, notify the coach. **This rule is unchanged.**
- **Progression to quality:** tempo / intervals / race-pace work still requires **4 consecutive weeks pain-free** before adding. Volume progression at Z1–Z2 doesn't need that gate.
- **Long run:** the prescribed 60-min "long run" is a single Z2 session at 5:30 ratio, not a distance target. Distance falls out of pace.

When prescribing runs: don't reflexively write run-walk 4:1 — write 5:30. Don't cap volume at the 30/30/60 number — work to the 18–25 km/wk band. Flag deviations from *this* protocol, not the stale one.

## CURRENT SWIM PROTOCOL (live)

Reviewed 27 Apr 2026. CSS test logged today at **1:39/100m** (athlete-reported; needs manual update in Intervals.icu Settings → Swim → CSS Pace).

- **Frequency:** 2 swims/week typical (Tue + Thu).
- **Session length:** 35–60 min on the clock (including rest), 2.0–3.0 km total distance.
- **Rep length preference:** longer sustained reps (300m–800m) preferred over fragmented short reps. Short reps (50–100m) used **sparingly** — not as the dominant structure of a session.
- **CSS reference:** 1:39/100m (set 27 Apr 2026; retest mid-July per `decision-points.md` B6).
- **Race target swim pace:** 1:42/100m (3 sec/100m above CSS).
- **Aerobic comfort pace:** ~1:43–1:48/100m at HR 110–130.
- **First-dose discipline for new pace zones:** when introducing a new pace (e.g. updated CSS), volume at that pace should be **~70% of full-dose** for the first session. Full-dose = 2,000m at CSS sustained. First-dose = 1,500m. Build from there.
- **Always state whether session time totals include rest or pure swim time.** Don't quote a "55 min session" that only fills 40 min on the clock.
- **CSS test cadence:** project decision-points B1 + B6 — twice a year (early build + mid-build).

When prescribing swims: lead with sustained reps (300m+), use 50–100m only for drills, descending sets, or as a small accessory block. Always sanity-check total time on the clock against the user's stated session length.

## ANALYSIS PRINCIPLES — ride/run intensity reads

Lessons from 26 Apr 2026 review (overstated "tempo-dominated" on a ride that was Z2 by NP and HR). Apply on every ride/run review.

- **Lead with NP/IF** for the "what zone was this" question. Average power and weighted IF anchor the read; time-in-zone is a secondary view.
- **Cross-check power time-in-zone against HR time-in-zone** before concluding intensity. Big mismatches = athlete highly aerobic, OR zones mis-calibrated, OR surges too short for HR to catch up. Never draw a "tempo too hot" conclusion from power TiZ alone.
- **Note Variability Index before reading TiZ.** VI > 1.10 = high natural surge from outdoor terrain; expect significant time outside NP-zone without the ride being over-cooked.
- **Aerobic decoupling is a durability / fuelling / fatigue signal, not a pacing signal.** A ride with race-pace NP and high decoupling points at fuelling, freshness, or aerobic ceiling — not at "you went too hard early".
- **Bike HR zones are NOT currently calibrated independently.** Profile uses run-derived LTHR 170 for both sports. Bike LTHR is typically 5–10 bpm lower (less muscle mass, lower cardiac drift). Until a bike-specific 20-min test is logged, treat bike HR zones as a rough guide, not gospel. **Action: book bike LTHR test in W1/W2 of current block.**
- **Distinguish race-usable CHO from gross CHO** when reviewing fuelling. Slow-release foods (oat bars, real food) deliver calories but not fast glucose — exclude from race-pace fuelling totals. Gross g/hr can look fine while race-usable is well below target.
- **Session time math: always include rest in stated session length.** When proposing a "55 min session", verify the structure actually fills 55 min on the pool clock or watch — wu + main set work + rest + cd. Don't propose schedules whose components add to 40 min and call them 55.
- **First-dose discipline for new pace zones.** When prescribing at a newly-set pace (CSS, FTP, threshold pace), start at ~70% of full-dose volume in the first session. Full-dose only after the pace is confirmed sustainable.

## FORCE-RECOVERY TRIGGERS

If any of these are true for >5 consecutive days, recommend a recovery intervention before any further build:

- ATL > CTL by more than 25.
- 7-day ramp > +4 CTL/wk while ankle is in rehab.
- HRV 7d-rolling < 60d-baseline – 1 SD AND RHR elevated AND sleep <7 hr average.

## A-GOAL DISCIPLINE

Race-day target splits (from project plan):

| Leg | Last year | A-target | Source of gain |
|---|---|---|---|
| Swim | 66 min | 64 min | Technique + OWS practice |
| T1 | 5 min | 4 min | Rehearsal |
| Bike | 4:55 | 4:44 | Pacing up + aero (NP ~225 W, IF ≤0.78 on Bertinoro, capped 0.68 first 90 min) |
| T2 | 5 min | 4 min | Rehearsal |
| Run | 3:50 | 3:30 | **Heat tolerance + run-leg fuelling under appetite suppression** |
| Total | 10:06 | **9:30** | |

B-goal 9:45. C-goal sub-10:06.

The largest opportunity is the run leg. Most run-leg gain comes from heat acclimation + fuelling discipline, not run-leg fitness. Re-anchor every plan-design decision to this priority order:

**Heat acclimation > cooling protocol > run fuelling rehearsal > bike pacing/aero > swim work.**

## RAMP DISCIPLINE

| Phase | Date | Target CTL | Target form |
|---|---|---|---|
| End base | end May | ~85 | –10 to –20% |
| End build | end June | ~95 | –10 to –25% |
| End specific | end July | ~105 | –10 to –25% |
| Peak | mid-Aug | 110–115 | –20 to –30% |
| Pre-taper | end Aug | ~110 | –15% |
| Race day | 19 Sep | 95–100 | **+5 to +15%** |

Cap ramp ~3.5–4 CTL/wk until ankle cleared. Bike-driven ramp = low-risk, run-driven = high-risk in current phase.
