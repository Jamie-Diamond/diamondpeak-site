# Risk register — IM Italy 2026 build

**Purpose:** structured risk tracking with leading indicators tied to actual data. Replaces narrative risk ("heat is the binding constraint") with monitorable risk. Reviewed in every weekly check-in.

> Probability: L = <20%, M = 20–50%, H = >50% across the remaining build window.
> Impact: time cost on race day OR fitness gap before race day.
> Trigger: the data signal that escalates the risk to active mitigation.

## Top-tier risks (build-defining)

### R1. Ankle re-injury before quality run unlock

| Field | Value |
|---|---|
| Probability | M (in rehab; 8 wk off recently) |
| Impact | High — kills run-fitness gain; goal drops from 9:30 floor to B-goal 9:45 |
| Leading indicator | Pain >2/10 during run; pain >0/10 next morning; weekly run km ramp >10% |
| Trigger | Any pain breach, 2 consecutive weeks |
| Mitigation | Pain-led, not plan-led ramp. Frequency over volume. Strength priority order: SL calf raise + balance + Copenhagen + lateral band. 4 pain-free weeks before quality. |
| Owner | Jamie + physio |

### R2. Heat protocol slips to start

| Field | Value |
|---|---|
| Probability | M (no sessions banked yet; April 2026) |
| Impact | High — heat tolerance is THE binding constraint on the run leg |
| Leading indicator | No hot-bath log entries in `current-state.md` by mid-May |
| Trigger | Heat session 1 not done by 22 May |
| Mitigation | Calendar block 4× weekly hot baths from 15 May. Pair with B1 long ride (post-ride). Track cumulative count; target 14–20 by race week. |
| Owner | Jamie |

### R3. Bike Lap-2 fade recurrence

| Field | Value |
|---|---|
| Probability | M without specific intervention; L with intervention |
| Impact | High — 2025 cost was ~22% AP fade Lap 1 → Lap 2; recovery means -8 to -10 min |
| Leading indicator | Aerobic decoupling >5% on rides >3 hr; AP between halves drops >10% |
| Trigger | Any long ride in build with >10% decoupling |
| Mitigation | Long Z2 ride volume (B1) is the primary lever. Bertinoro climb cap IF 0.78 hard rule. Long brick race-rehearsal mid-Aug verifies. |
| Owner | Jamie |

### R4. Run aid-station overrun recurrence

| Field | Value |
|---|---|
| Probability | M without drill; L with drill |
| Impact | High — 2025 cost was ~15 min |
| Leading indicator | Long-run aid-station drill stops >30 sec average |
| Trigger | Any long run after June with >5 stops over 30 sec |
| Mitigation | Aid-station discipline drill in every long run from June. Stopwatch every stop. Race-rehearsal in mid-Aug brick. |
| Owner | Jamie |

## Mid-tier risks (monitor closely)

### R5. Sleep debt compounding

| Field | Value |
|---|---|
| Probability | H (current avg ~7.4 hr vs 8 hr target) |
| Impact | Medium — accumulated deficit erodes adaptation; HRV signal early |
| Leading indicator | 7-day rolling sleep avg <7.5 hr for 2 consecutive weeks; HRV trend down |
| Trigger | 7-day avg <7 hr OR HRV 7d-rolling drops below 60d-baseline – 1 SD |
| Mitigation | Sleep extension protocol (priority days before key sessions; caffeine cutoff 8 hr pre-bed; bedtime gate before midnight on bike-quality days) |
| Owner | Jamie |

### R6. Weight not dropping

| Field | Value |
|---|---|
| Probability | H (current 82.1 kg, no downward trend in 4 weeks) |
| Impact | Medium — 3 kg above target = ~3% W/kg loss = compounds bike + run cost |
| Leading indicator | 7-day weight avg flat or rising for 3+ weeks |
| Trigger | Mid-May weigh-in still ≥81 kg |
| Mitigation | Modest deficit (300–500 kcal/day on non-quality days). Protein floor 1.6–1.8 g/kg. Don't deficit on key-session day or recovery day. Rate cap 0.5 kg/wk. |
| Owner | Jamie |

### R7. Strength under-execution

| Field | Value |
|---|---|
| Probability | H (4 sessions in 13 weeks logged = 0.3×/wk) |
| Impact | Medium — direct ankle-protection lever; running economy lever; injury risk if absent |
| Leading indicator | Weekly strength count |
| Trigger | <2 strength sessions in any 2-week period after 1 May |
| Mitigation | Hard-calendar 2×/week minimum. Session A (lower-body power) + Session B (stability + ankle-rehab priority). Push to IcuSync as a session, not optional. |
| Owner | Jamie |

### R8. Sweat test not booked

| Field | Value |
|---|---|
| Probability | M (open action, no booking yet) |
| Impact | Medium — sodium prescription stays a guess; race-day sodium under-/over-dosing risk |
| Leading indicator | No booking confirmation in `current-state.md` open actions |
| Trigger | Mid-May without a booked test |
| Mitigation | Book PH test for May/June. Test in heat + at race intensity if possible. Update sodium protocol on result. |
| Owner | Jamie |

### R9. CTL ramp too hot while ankle in rehab

| Field | Value |
|---|---|
| Probability | M (already breached on 25 Apr — ramp hit +4.4 vs cap +4.0) |
| Impact | Medium — re-injury risk; lost training days |
| Leading indicator | 7-day ramp >+4 CTL/wk |
| Trigger | 2 consecutive weeks at >+4 |
| Mitigation | Bike-driven ramp only (low risk); cap run volume increases at 10%/wk. Use `ironman-analysis/` flags weekly. |
| Owner | Jamie |

## Lower-tier risks (note + monitor)

### R10. Wetsuit ruling — non-wetsuit on race day

| Field | Value |
|---|---|
| Probability | M (sea ~24°C edge case) |
| Impact | Low–medium — slower swim if non-wetsuit untrained |
| Leading indicator | Pre-race local sea temperature reports |
| Trigger | Race week confirmed non-wetsuit |
| Mitigation | Equal training balance — ≥6 wetsuit sessions AND non-wetsuit volume from May. |
| Owner | Jamie |

### R11. Travel/logistics disruption

| Field | Value |
|---|---|
| Probability | L–M |
| Impact | Low–medium |
| Leading indicator | Flight schedule; bike-bag insurance; arrival-day plan |
| Trigger | Any flight cancellation; bike-bag damage at airport |
| Mitigation | Arrive D-4 minimum. Confirm bike-bag insurance pre-flight. Carry essentials (helmet, shoes, race kit) hand-luggage. Pre-book post-arrival bike build slot at hotel/local shop as a backup. |
| Owner | Jamie |

### R12. New equipment not bedded in

| Field | Value |
|---|---|
| Probability | M (TT bike fit, ISM saddle, aero helmet all open) |
| Impact | Medium — race-day surprise = chafing, position discomfort, cooling failure |
| Leading indicator | Any equipment introduced in last 4 weeks |
| Trigger | New saddle, helmet, or position change after mid-Aug |
| Mitigation | Equipment locked by **mid-July**. Saddle 6–8 weeks bedded. Helmet trialled in heat. TT fit done before June. |
| Owner | Jamie |

### R13. Race-day weather extreme

| Field | Value |
|---|---|
| Probability | L (>30°C); H (warm conditions ~24–28°C is the median) |
| Impact | Low if heat-acclimated; high if not |
| Leading indicator | 10-day forecast pre-race |
| Trigger | Forecast ≥30°C |
| Mitigation | Heat acclimation is the primary mitigation. HR-floor decision rule on the run for in-race response. Pre-race ice slurry + T2 ice ready regardless of forecast. |
| Owner | Jamie |

### R14. Bike gearing too tall for Bertinoro

| Field | Value |
|---|---|
| Probability | H if not addressed (current 52T × 10–33 forces above-cap power on ≥6% gradients) |
| Impact | Medium–high — recurs the 2025 match-burning failure on climbs (305 W × 90 sec × 4 climbs = ~22 kJ above-FTP floor) |
| Leading indicator | Cadence on training-ride climbs >5% gradient — if dropping below 70 rpm in lowest gear |
| Trigger | Gearing not changed by 1 June, OR a hilly training ride confirms the gear is unworkable |
| Mitigation | Swap chainring to 46T or 48T sub-compact (recommended; £80–250 + fitting). OR larger cassette (11-36 / 11-40) if rear derailleur cage compatible. OR plan around it: cap NP at 220 W not 225 W, accept above-cap on climbs, soften first 90 min. **Decision needed by mid-May to bed in over June–July.** |
| Owner | Jamie |

---

## Review cadence

- **Weekly check-in:** scan all H + M probability rows for trigger breaches. Flag any new entries.
- **Monthly:** full review — re-rate probabilities, retire mitigated risks, add new ones if context changes.
- **Race week:** R10, R11, R12, R13 are the live risks. R1–R9 are locked or irrelevant by then.
