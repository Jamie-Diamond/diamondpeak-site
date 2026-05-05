# Reusable coaching prompts

**TL;DR:** Standing prompt patterns for recurring workflows. Fill bracketed fields and paste into Claude. All prompts assume IcuSync MCP is wired up — Claude pulls Intervals.icu data directly and pushes planned sessions back. If IcuSync is down, Claude should say so before working from stale data.

## Index

| Prompt | Use when |
|---|---|
| [Map a full block](#map-a-full-block) | Starting a new build phase or after a major schedule change. |
| [Weekly check-in](#weekly-check-in) | Every Sunday/Monday — review last week, plan next. |
| [Daily readiness](#daily-readiness) | Morning gate before any quality session. |
| [Session deep-dive](#session-deep-dive) | After a key bike or run session — power/HR/decoupling/drift. |
| [Compare last N sessions](#compare-last-n-sessions) | Tracking adaptation across recurring sessions. |
| [Missed training](#missed-training) | After illness, injury, life event — reassess goal honestly. |
| [Niggle triage](#niggle-triage) | Pain or new injury — diagnose / modify / refer. |
| [Race or session blow-up](#race-or-session-blow-up-analysis) | Bad race or DNF / shocker session — what went wrong. |
| [Form + strength prescription](#form--strength-prescription) | Post-Ochy report — drills, gym work, what to track. |
| [Build a fuelling plan](#build-a-fuelling-plan) | New race or new conditions — total carbs/sodium/fluid + minute-by-minute plan. |
| [Pre-race week countdown](#pre-race-week-countdown) | 7 days out from race — day-by-day checklist. |
| [Push next week's sessions](#push-next-weeks-sessions-via-icusync) | Standalone IcuSync push (if it didn't happen in the weekly check-in). |
| [Ad-hoc data pull](#ad-hoc-data-pull-and-analysis) | One-off questions about training data. |
| [Rationale-required wrapper](#rationale-required-wrapper) | Standing prefix for any deep-coaching prompt. |

> **Cross-validation rule (apply across all prompts):** before recommending a hard session or load increase, cross-check against multi-signal state (HRV trend, RHR, sleep, body weight vs 7-day avg, niggle pain score). Calendar-says-hard is overridden by tanked HRV + poor sleep + elevated yesterday RPE. See `templates/current-state.md` for subjective layer.

---

## Map a full block

```
Use IcuSync to pull my athlete profile, recent training history, and current fitness (CTL/ATL/TSB) from Intervals.icu.
Read my project instructions, the files in /reference/, and /templates/current-state.md.

Race: [NAME] | Distance: [DIST] | Date: [DATE] | Weeks out: [N]
Goal: [TIME] | Method preference: [CHOICE or "you decide"]
Notes: [injuries, life events, recent breaks]

Please:
1. Use real Intervals.icu data to assess current fitness baseline.
2. Map every phase from today to race day with dates and weekly hours/km by discipline.
3. Flag any risks or tight timelines (load ramp, ankle, work travel, etc.).
4. Write Week 1 sessions and push them to my Intervals.icu calendar via IcuSync on the correct dates.
5. Confirm the push succeeded and list the sessions written.
```

## Weekly check-in

```
Use IcuSync to pull my last 7 days of training data from Intervals.icu — all activities, planned vs completed, CTL/ATL/TSB trajectory.
Read my project instructions and /templates/current-state.md.

Subjective inputs (only those listed in current-state.md scope):
- Sleep avg: [hr]
- Body weight: [kg vs 7-day avg]
- Ankle pain 1–10: [N]
- Other niggle: [none / location / pain 1–10]
- Anything off-plan: [missed sessions / life / illness]
- Heat-acclimation sessions this week: [N]

Please:
1. Review the week vs the plan, by discipline.
2. Analyse key session data — power, pace, HR, RPE, drift.
3. Flag anything in metrics or load trajectory (CTL ramp vs cap, ATL > CTL gap, TSB).
4. Write next week's sessions and push them to my Intervals.icu calendar via IcuSync on the correct dates.
5. Confirm the push succeeded and end with a one-line week summary.
```

## Daily readiness

Morning gate before any quality session.

```
Use IcuSync to pull today's planned session from my Intervals.icu calendar, plus my last 7 days of completed activities and CTL/ATL/TSB.

This morning's signals:
- HRV (lnRMSSD or app score): [N]
- Resting HR: [bpm]
- Sleep last night: [hr / quality if logged]
- Body weight: [kg vs 7-day avg]
- Ankle pain 1–10: [N]
- Other niggle: [none / location / pain 1–10]
- Yesterday's session RPE: [N]

Tell me:
1. Go / modify / skip — with the rationale in one sentence.
2. If modify: what specifically changes (intensity, duration, swap). Push the modification to my Intervals.icu calendar via IcuSync.
3. If go: any execution caveats (warm-up extended, hydration emphasis, cap RPE).
```

## Session deep-dive

```
Use IcuSync to pull yesterday's [bike / run / swim] activity from Intervals.icu — full activity stream including power, HR, pace, cadence, and any Stryd metrics.
Planned target: [power / pace / HR zones, duration, structure].

Please:
1. Power / pace / HR zone distribution vs prescribed.
2. Drift across the session — 1st third vs last third on power, HR, cadence, GCT.
3. Decoupling (HR vs power or pace).
4. Form-power %, vertical oscillation, ground contact time — any red flags.
5. Was the prescribed adaptation achieved — yes / partially / no, with rationale.
6. One thing to change in the next session of this type.
```

## Compare last N sessions

```
Use IcuSync to pull my last [N] [type] sessions from Intervals.icu — most recent first. Filter by activity type / workout name / tag if helpful.

For each, give:
- Power / pace / HR averages.
- Drift profile.
- RPE (from notes if logged).
- Where I am on the trend — improving, stable, regressing.
- What this implies for the next session of this type.
```

## Missed training

```
I missed [X days/weeks] due to [reason].
I'm [X] weeks from [RACE], was in [PHASE], running [KM]/week.
I feel [fine / rusty / tired].

Please: reassess my goal honestly, redesign the remaining weeks, tell me what to cut and what to protect.
```

## Niggle triage

```
I have [pain] in my [location].
Started [when]. Feel: [sharp / dull / aching]. Scale 1–10: [N].
Worse: [when — uphill / downhill / morning / mid-run].
Weeks to race: [N].

Please:
1. Most likely diagnosis (with confidence level).
2. Should I run today.
3. Modified training for the next 7 days.
4. Threshold for seeing physio.
```

## Race or session blow-up analysis

```
Bad [race / session] on [DATE]: [what happened — bonk / stomach / pacing / heat].
Data: [paste Stryd / Garmin / power-meter summary]
Fuelling: [what I took, when]
Conditions: [temp / humidity / elevation]

Please:
1. Diagnose what went wrong.
2. What to change going forward.
3. Does this change my next block.
```

## Form + strength prescription

```
Ochy report: [attach PDF or paste results — score / style / weak points]
Stryd (if available): cadence [spm], GCT [ms], VO [cm], form power [%], CP [W]

Gym availability: [days/week — which days]
Current phase: [base / build / specific / taper]
Ankle status: [in rehab / cleared, with current pain 1–10]

Please:
1. Cross-reference Ochy weak points with Stryd metrics (or note Stryd absence).
2. Quantify what each issue costs in running economy.
3. Prescribe specific drills, cues, and gym exercises for each issue.
4. Prioritise — highest-value thing to fix first. Apply ankle-rehab override from /reference/run-form-and-strength.md if relevant.
5. Tell me which Stryd metrics (if Stryd added) to track week-to-week.
6. Schedule a follow-up Ochy test in [N] weeks.
```

## Build a fuelling plan

```
Goal time: [TIME] for a [DISTANCE].
Carb sources I tolerate: [list]
Sources I do NOT tolerate: [include "gels — sensory aversion" if not already in project instructions]
Conditions expected: [temp / humidity]
Aid station spacing: [km]

Please build:
1. Total carbs/hour target.
2. Total fluid target /hour.
3. Sodium target /hour (flag that a Precision Hydration sweat test would replace assumption with data).
4. Minute-by-minute timetable for each fuel source.
5. What to carry from start vs refill on course (note: athlete declines special-needs bags).
6. Bail-out plan if I can't take in fuel after [N] minutes.
```

## Pre-race week countdown

```
Race: [NAME] on [DATE]. Today: [DATE]. Days out: [N].

Please give me a day-by-day countdown checklist covering:
- Training (taper specifics).
- Carb load (timing and grams/kg, anchored to current weight).
- Sleep priority days.
- Equipment prep and bag pack.
- Travel logistics.
- Heat protocol maintenance (3–4 hot baths in final 7 days — adaptation decays within 7–10 days).
- T1/T2 ice strategy and 5+ hr ice retention test in a hot car.
- Pre-race-day rituals.
- Race-morning sequence (timeline in /reference/run-execution.md).
```

(See also `templates/race-week-countdown.md` for the structured fill-in version.)

## Push next week's sessions via IcuSync

Use after the weekly check-in if the push didn't happen there, or if you want to redo a week.

```
Use IcuSync to push next week's planned sessions to my Intervals.icu calendar.
Read my project instructions and current state.

For each session, write:
- Type and discipline.
- Warm-up.
- Main set with intervals and targets (power / pace / HR by zone).
- Cool-down.
- Total time and distance.
- Rationale in one sentence — what adaptation it's targeting or what risk it's mitigating.

Include any brick if the week calls for one. Flag any session that depends on weather (heat acclimation, indoor vs outdoor).

After the push, confirm:
1. Each session written, on the correct date.
2. Total hours by discipline for the week.
3. Primary stimulus of the week.
4. What's at risk if I miss the single most important session.
```

## Ad-hoc data pull and analysis

```
Use IcuSync to pull [what you want from Intervals.icu — e.g. "all bike activities longer than 3 hours in the last 12 weeks", "my run cadence trend by week since 1 March", "all sessions where TSS > 200"].

Question: [what you actually want to know].

Analyse and answer. State assumptions explicitly. If the data doesn't support a confident answer, say so.
```

## Rationale-required wrapper

Paste at the top of any deep-coaching prompt, or rely on the standing rule already in project instructions.

```
For every training, pacing, fuelling, or recovery recommendation in this conversation, state the rationale in one sentence — what physiological adaptation it's targeting, what risk it's mitigating, or what data point it's responding to. If you can't justify it in one sentence, don't include it.
```
