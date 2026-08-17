# Training Bot Improvement Plan

Status: proposed  
Scope: weekly planning, session prescription and event-specific methodology

## Objective

Make the training bot reliably prescribe the right sessions, at the right intensity,
for the athlete's event and current readiness. The deterministic layer must own
safety, calendar scope, event requirements and numeric prescription. The LLM may
adapt, explain and select among valid options; it must not fill methodological gaps.

## Current assessment

The current Ironman/70.3 direction is broadly appropriate: substantial low-intensity
volume, long-course specificity, progressive race-pace work, fuelling practice,
bricks and a volume-reducing taper that retains short intensity touches.

The main gaps are implementation gaps rather than a need to replace the philosophy:

1. A weekly proposal is not checked to ensure that every date is in the requested
   Monday-to-Sunday window. A stray date can alter the week being validated and pushed.
2. Several safety checks can be skipped after an ICU data failure while the plan
   remains pushable.
3. A daily modulation-engine failure falls back to an LLM path that may still write
   a workout.
4. Race pace is rendered from generic sport bands rather than an event- and
   athlete-specific target. This can blur Ironman, 70.3 and sportive race work.
5. The planner measures per-sport zone percentages but does not enforce a combined
   hard-session, impact or recovery-spacing budget.
6. Bricks, long swims and race simulations are described in the methodology but are
   not deterministic phase/event requirements.
7. Swim sessions are described in minutes even when the library's intended dose is
   distance, repetitions and rest.
8. 5k, 10k, half-marathon and marathon are recognised by the session library but
   do not have complete blueprint, distribution, load or taper support. The generic
   long-ride rule makes a clean run-only weekly plan impossible.
9. Grand Fondo/Sportive is bike-only and structurally supported, but course type is
   explanatory text only: climbing, altitude, technicality and event duration do
   not yet alter the weekly session requirements.

## Principles

- Safety-critical unknowns fail closed for calendar writes. A useful but incomplete
  draft may be shown to a coach; it must not auto-push.
- Use one physiological intensity model internally: individual thresholds identify
  low, moderate and high domains; device-facing five/seven-zone labels are a render
  format, not the planning model.
- Prescribe session purpose and dose first. Weekly time-in-zone is an audit measure,
  not a target to fill mechanically.
- Count combined stress across disciplines: hard days, long-session recovery,
  run-impact exposure, strength and life/readiness constraints.
- Planned progression must be constrained by completed work, not merely the prior
  calendar entry.
- Unsupported event types must be explicitly blocked rather than silently using a
  triathlon or generic cycling template.

## Phase 0 — calendar and safety invariants

Priority: release blocker

1. Add proposal-schema validation before `plan_builder.build_sessions`:
   - every session has a valid ISO date;
   - every date is within `[week_start, week_start + 6]`;
   - no duplicate sport/session identity unless a planned double-session is allowed;
   - sport and session type are valid for the event;
   - non-strength sessions carry valid, positive segments.
2. Keep `week_start` an explicit input to `build_sessions`; never derive it from the
   earliest model-supplied session date.
3. On `--push`, block a plan when CTL, prior-week load, run-volume history or a
   required TSS ceiling/floor cannot be obtained. Record a draft and alert the coach.
4. If the daily modulation engine fails, do not permit an automatic calendar write.
   Send a conservative “manual review required” card or retain the existing workout.
5. Change the empty-calendar fallback to fail closed for unclassified hard-rule
   codes. New safety rules must be classified before they can be bypassed.

Acceptance criteria:

- An out-of-week session, missing safety input or unclassified hard violation cannot
  reach Intervals.icu.
- Tests cover a date before the week, after the week, invalid dates and an ICU failure
  for each safety input.
- The push result reports `validated_week_start`, validation coverage and every
  blocker clearly.

## Phase 1 — event registry and support gate

Priority: release blocker for run events

Create one event registry used by `primitives.blueprint`, `generate-blueprint.py`,
`session_library.py`, `stage1-plan.py`, validation and race-plan generation. Each
event must declare:

- sports and permitted cross-training;
- phase distributions or a deliberate no-distribution policy;
- long-session type, progression and maximum;
- quality-session menu and maximum weekly frequency;
- taper duration and volume factors;
- race-pace model and required rehearsal sessions;
- required session types by phase.

Mark an event `supported: false` until all of these fields exist. The planner must
refuse auto-push and explain what is missing.

Initial supported profiles:

- Full Ironman
- 70.3
- Road Sportive / Gran Fondo

Initial blocked profiles pending a dedicated blueprint:

- Marathon
- Half marathon
- 10k
- 5k

This removes the current contradiction where run-only events are recognised by the
session library yet inherit a long-ride target.

## Phase 2 — deterministic weekly skeleton

Priority: high

Replace the current “LLM proposes the week shape” step with a deterministic skeleton:

1. Resolve event, phase, availability, injuries, completed load and readiness.
2. Allocate fixed weekly roles: rest, long session, one or two quality sessions,
   easy/recovery sessions, strength and event-specific rehearsal.
3. Allocate load to those roles within caps and completed-work progression limits.
4. Enforce recovery spacing before rendering:
   - no adjacent hard run days;
   - no quality run immediately after a long run or high-impact brick;
   - no more than the athlete's allowed combined hard sessions in a rolling 72 hours;
   - strength placement that does not compromise the key run/bike session;
   - long-session recovery appropriate to athlete age, injury history and readiness.
5. Permit the LLM only to choose from equivalent library variants and write athlete-
   facing notes.

Add deterministic, event-specific required-session checks. For example, a long-course
build week should explicitly require the intended swim, bike, run and brick exposures;
a rest-week may intentionally waive selected requirements.

## Phase 3 — physiological intensity and progression model

Priority: high

1. Plan internally with three threshold-anchored domains:
   - low: below the first physiological threshold;
   - moderate: between thresholds / sustainable race-specific work;
   - high: above the second threshold.
2. Map those domains to each athlete's device zones only at rendering time. Keep bike
   power, run pace/critical speed and swim CSS distinct; do not equate their percentage
   bands mechanically.
3. Replace generic `race` bands with an event/athlete race-pace target object. It must
   distinguish, for example, Ironman bike race intensity from 70.3 bike race intensity.
4. Treat weekly intensity distribution as an audit band, not a quota. The plan must not
   add VO2 work merely to meet a percentage.
5. Promote the existing completed-work progression guard from advisory to a rule for
   material changes in interval duration, intensity, repetitions or total quality time.
   Allow a coach override with recorded rationale.
6. Add sport-specific progression rules for total work time, recovery ratio and exposure
   frequency, not only repetitions.

## Phase 4 — triathlon session requirements

Priority: high

Full Ironman and 70.3 profiles should each encode, by phase:

- minimum sport frequency and the key session(s) required;
- long ride and long run progression with independent recovery cost;
- brick frequency, type and transition rule;
- long swim and CSS/race-pace swim dose;
- race simulation timing, duration, fuelling and pacing objective;
- maximum combined high-intensity dose across swim, bike and run;
- an explicit lower run-intensity allowance for athletes with injury risk.

The first implementation should use conservative defaults and allow athlete-level
overrides. Peak percentages should not be treated as universal targets: their
appropriateness depends on weekly volume, training age, injury history and the
concentration of quality into a few sessions.

## Phase 5 — swim prescription

Priority: high

Render swim work as distance, repetitions, target pace and rest. Convert to time only
when an integration constraint requires it, while preserving the original set in the
description. Validate:

- total distance and main-set distance;
- rep count and rest;
- CSS/race-pace range;
- pool-length rounding;
- no claim that ICU velocity-derived intervals are exact swim rep boundaries.

## Phase 6 — run-event blueprints

Priority: high before enabling auto-push

Implement distinct profiles rather than one generic running plan:

| Event | Primary qualities | Key sessions | Taper |
|---|---|---|---|
| 5k | economy, VO2, speed reserve | short reps, VO2 intervals, threshold support | 5–7 days |
| 10k | critical speed/VO2 and threshold | 10k-pace intervals, threshold, long aerobic run | 7–10 days |
| Half marathon | threshold and race-pace durability | threshold/cruise work, race-pace blocks, progressive long run | 7–14 days |
| Marathon | aerobic volume and race-pace durability | long run, marathon-pace work, threshold support | 2–3 weeks |

For each, add separate distributions, long-run caps and targets, session frequencies,
quality-dose limits, race simulations and test/retest policy. The blueprint generator
must use run-only sports and must never create a bike requirement unless cross-training
is explicitly enabled.

## Phase 7 — Gran Fondo / Sportive specialisation

Priority: medium-high

Extend the sportive profile with structured course inputs:

- event duration, distance, climbing metres and longest climb;
- grade/altitude, technical descending and group-riding demands;
- terrain-specific race-power caps and fuelling plan;
- athlete climbing history and indoor/outdoor availability.

Turn course type into deterministic requirements:

- flat: sustained endurance and race-pace blocks;
- rolling: sweet spot and repeated surges;
- hilly/mountainous: climbing-specific threshold/torque work, longer climbing blocks,
  descending skills and event-duration exposure.

Long-ride ceilings must be event- and athlete-specific. A fixed five-hour ceiling is
not appropriate for every long mountainous Gran Fondo.

## Validation and rollout

1. Add pure tests for every event registry entry, safety invariant and session
   requirement.
2. Add representative dry-run fixtures for Ironman, 70.3, 5k, 10k, half marathon,
   marathon, flat sportive and mountainous Gran Fondo.
3. Compare generated weeks to a coach-approved gold set before enabling a profile.
4. Shadow-run the deterministic skeleton against live plans for at least two complete
   load/recovery cycles per event type.
5. Enable auto-push per event only after the audit is green and a coach signs off the
   dry-run examples.

## Evidence guardrail

Use the evidence base to bound decisions, not to impose one universal split. Most
endurance training should remain low intensity, with event-specific moderate/high work
and an individual taper. Changes to the methodology should cite a primary study or
systematic review and record the athlete population and uncertainty.

Relevant references:

- Oliveira et al. (2024), polarized versus other intensity distributions:
  https://pubmed.ncbi.nlm.nih.gov/38717713/
- Muñoz et al. (2014), intensity distribution during an Ironman season:
  https://pubmed.ncbi.nlm.nih.gov/23921084/
- Bosquet et al. (2007), taper meta-analysis:
  https://pubmed.ncbi.nlm.nih.gov/17762369/
- Haugen et al. (2022), periodisation in elite distance runners:
  https://pubmed.ncbi.nlm.nih.gov/35418513/
- Beattie et al. (2014), strength training in endurance athletes:
  https://pubmed.ncbi.nlm.nih.gov/24532151/
