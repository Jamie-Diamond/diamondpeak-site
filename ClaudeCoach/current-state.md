# Current state — subjective layer

**Purpose:** plug the gap between what IcuSync sees (CTL/ATL/TSB, planned vs completed activities, power, HR, HRV, RHR, sleep duration) and what it doesn't (ankle pain on the day, missed-and-why, open actions, what got cut). Update weekly — 60 seconds. Claude reads this before any weekly check-in or daily-readiness prompt.

> **In-scope subjective fields** (athlete-decision):
> - Ankle pain score 1–10
> - Other niggle (location + score)
> - What got missed and why
> - Open actions (test bookings, kit, life logistics that affect training)
>
> **Out-of-scope** (athlete does not log, do not request): mood, motivation, soreness 1–10, energy 1–10, subjective freshness 1–10. Decisions use objective signals only — HRV, RHR, sleep duration, body weight.

---

## Last updated: [YYYY-MM-DD]

## Ankle (high ankle sprain, onset ~1 March 2026)

- Current pain 1–10 (during run): [N]
- Current pain 1–10 (next morning): [N]
- This week's run km: [N]
- Last week's run km: [N]
- 4 consecutive pain-free weeks reached: [yes / no — if no, weeks done so far]
- Quality sessions resumed: [yes / no]
- Physio engagement status: [active / on pause / pending booking]

> Hard rule: increase load only if pain ≤2/10 during and 0/10 next morning. Cap weekly run-km increase at 10% while in rehab. Cap CTL ramp at +4/wk while ankle in rehab. (See `reference/rules.md`.)

## Other niggles

- [Location]: [pain 1–10] / [trend: stable / improving / worsening] / [action]

## Off-plan in last 7 days

- Missed sessions: [list — date, planned, reason]
- Cut sessions: [list — date, planned, what was done instead, why]
- Illness / life events: [details]
- Sleep deficit days (>1 hr below 8 hr target): [count]

## Body weight

- Current 7-day avg: [kg]
- Trend toward 79 kg race-day target: [on track / drifting up / drifting down]
- Race-day target: 79 kg by 19 Sep 2026

## Heat acclimation log

- Sessions completed this week: [N]
- Sessions completed cumulative (target 14–20 across late May → early Sept): [N]
- Days since last hot-bath session: [N] (decay window: 7–10 days)
- Method mix: [hot bath ×N / indoor trainer in heat ×N / overdressed easy run ×N]

## Open actions

| Action | Owner | Due | Status |
|---|---|---|---|
| Book Precision Hydration sweat-sodium test | Jamie | end May | [pending / booked / done] |
| FTP retest (end-May target) | Jamie | end May | [pending / scheduled / done — value W] |
| FTP retest (mid-July target) | Jamie | mid Jul | [pending / scheduled / done — value W] |
| CSS swim test #1 | Jamie | next 2 weeks | done 2026-04-27 — **CSS 1:39/100m** |
| CSS swim test #2 | Jamie | mid Jul | [pending / done — pace] |
| TT bike fit appointment | Jamie | before June | [pending / booked / done] |
| ISM saddle order | Jamie | now (need 6–8 wks fit time) | [pending / ordered / fitted] |
| Aero helmet (vented — POC Procen Air / Giro Aerohead / Spec TT5) | Jamie | June | [pending / ordered / fitted] |
| 5+ hr ice retention test in hot car (T1/T2 cooler) | Jamie | pre-race | [pending / done — N hours retained] |
| OWS race or full-distance simulation | Jamie | build phase | [pending / scheduled / done] |
| Tested run-fuelling protocol on a long run in heat | Jamie | recurring | [last done: date / outcome] |

## Race-day-relevant tested data

| Item | Value | Date tested |
|---|---|---|
| Sweat rate L/hr (heat) | [self-reported >1 L/hr] | old data — re-test pending |
| Sweat sodium mg/L | unknown — assumed salty | pending PH test |
| FTP (W) | 320 (Nov 2025 test, project) / 316 (Intervals as of 2026-04-25) | end-May retest due |
| Race-day weight (kg) | target 79; current 83 | 2026-04-25 |

## Notes for Claude

Lines 1–4 of this file are the in-scope/out-of-scope rule. Honour it. If a prompt asks for a wellness-1–10 type input that isn't in scope, don't request it; substitute objective signals from IcuSync.
