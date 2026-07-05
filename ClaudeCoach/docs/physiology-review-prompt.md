# ClaudeCoach methodology audit: agent prompt

Run this prompt against the ClaudeCoach repo (working directory `ClaudeCoach/`)
with read access to code, docs and `athletes/`. This is a read-only technical
review; no edits.

Scope note: this is a software and methodology audit of an automated
training-planning system. You are reviewing the system's logic, its parameters,
and the sessions it generates, not providing medical care or individual health
advice. The `athletes/` files are the system's configuration and output data;
treat them as inputs to the software under review. In the written report, refer
to the three athletes by role label only (Athlete A, B, C, defined below), not by
name.

---

## Role

You are a sports-science-literate systems reviewer auditing an automated endurance
training planner. Your domain background covers the applied literature on
training-load management, intensity distribution, periodisation, environmental
acclimatisation, sex-based training considerations, and masters and age-group
athletes. The review question is narrow and technical: **does this system's
methodology produce sound, evidence-aligned training, and where is its logic
wrong, risky, or leaving fitness on the table?**

## What you are reviewing

ClaudeCoach is an automated planner that serves three athlete configurations:

- **Athlete A**: long-course triathlon, roughly 11 weeks from race day.
- **Athlete B**: mid-level, half-distance triathlon.
- **Athlete C**: beginner, targeting a one-day alpine sportive finish.

It plans weekly training, prescribes daily sessions, monitors recovery signals,
and answers athlete questions over a chat interface.

Read the ACTUAL implementation, not just the documentation, and where they
disagree, flag the drift. The moving parts:

| Area | Where |
|---|---|
| Planning engine (two-stage: LLM proposal, then deterministic builder) | `scripts/stage1-plan.py`, `lib/plan_builder.py`, `lib/session_library.py` |
| Thresholds and zones (from intervals.icu eFTP etc.) | `lib/thresholds.py` |
| Methodology and per-athlete targets | `blueprints/blueprint.md`, `athletes/*/reference/training-blueprint.*`, `athletes/*/profile.json` (incl. `race_predictor`, peak-CTL/ramp config) |
| Coaching behaviour the athletes actually experience | `athletes/*/system_prompt.txt`, `athletes/*/persistent-rules.md`, `lib/coaching_levels.py` |
| Recovery and readiness | `lib/recovery_score.py`, morning check-in and activity-watcher scripts in `scripts/` |
| Heat protocol | `lib/heat.py`, heat entries in athlete profiles |
| Cycle-aware planning feature | `lib/menstrual.py` and its planner rules |
| Strength programme | strength handling in the planner and session library |
| Plan validation and guardrails | validation logic in the planner scripts (BLOCK mode) |
| What was actually delivered | `athletes/*/session-log.json`, recent plan output, `athletes/*/current-state.md` |

Also sample the delivered product: reconstruct the last 3-4 weeks for each
athlete configuration (planned vs completed, loads, intensity mix, rest days) and
judge the system by what it actually prescribed, not what the code intends.

## Fixed constraints (do not relitigate; critique within them)

1. **No scheduled performance tests.** Thresholds come from intervals.icu
   estimates (eFTP etc.), never from prescribed FTP/LTHR/CSS test sessions.
   You may flag the physiological cost of this and how to mitigate it within
   the constraint, but do not recommend scheduling tests.
2. **One methodology for all athletes**, parameterised per athlete (level,
   targets, availability). Critique the methodology and its parameterisation,
   not the single-blueprint decision itself.
3. Athlete availability rules (e.g. standard-week day rules, weekday-only
   cycling for the beginner) are agreed configuration facts, not system flaws.

## Review dimensions

Assess each; verdict per dimension: SOUND / NEEDS WORK / RISKY.

1. **Load management.** CTL/ATL/TSB usage, ramp-rate caps and whether they are
   actually enforced, acute spikes, monotony and strain, and the deeper
   question: where does TSS-centric planning itself mislead (e.g. TSS-chasing
   vs session quality, swim/strength TSS distortions)?
2. **Intensity distribution.** What distribution does the system actually
   produce (compute it from the session logs), is it appropriate per phase and
   per athlete, and is easy training genuinely easy?
3. **Periodisation.** Base/build/specific/taper structure, block lengths,
   recovery-week logic, taper shape and duration vs evidence, and whether
   Athlete A's remaining ~11 weeks are being sequenced correctly.
4. **Recovery and readiness.** How HRV/RHR/sleep/subjective data gate or
   modify prescriptions, overreach detection, whether the system can hold an
   athlete back from training when the signals say it should, and whether it
   does so.
5. **Specificity and race preparation.** Race-demand modelling (long-course
   triathlon vs alpine sportive), long-session progression, brick work, race
   simulations, race-day pacing, and the race predictor's assumptions (IF as a
   function of CTL: is that defensible?).
6. **Fuelling.** In-session and race nutrition guidance: quantity, timing,
   gut training, and whether it is prescribed or just mentioned.
7. **Heat.** The heat-acclimation protocol (dose crediting from ambient
   temperature, maintenance floors, formal block timing): consistent with the
   acclimatisation literature or a token gesture?
8. **Cycle-aware planning.** The cycle-tracking feature and its luteal-phase
   rule: is the software rule evidence-aligned, individualised, and actually
   influencing the plans it generates? Review it as a system feature against the
   published consensus, not as clinical advice.
9. **Strength.** Programme structure, exercise selection logic, concurrent-
   training interference management, and how strength work is reduced near race
   day.
10. **Injury risk.** Run-load progression, return-from-niggle logic (there is
    ankle-tracking machinery), whether anything caps week-to-week running
    volume growth specifically (not just total TSS).
11. **Threshold integrity.** Consequences of the no-test constraint: how stale
    or biased can eFTP-derived zones get for each sport (swim pace especially),
    and what passive validation exists or should exist.
12. **The chat layer as coach.** Do the system prompts and persistent rules
    push the model toward sound in-the-moment responses (e.g. when an athlete
    reports fatigue, a niggle, or asks to swap a session), or is quality left to
    the model's discretion?

## Method and evidence standard

- Every finding cites its evidence: file and line for code claims, concrete
  dates/sessions from the logs for delivery claims.
- Separate **evidence-based critique** (cite the established literature or
  consensus position you are applying, e.g. intensity-distribution research,
  taper meta-analyses, heat-acclimation dose-response, ACWR and its known
  limitations) from **coaching preference** (label it as such).
- Quantify where possible: compute the actual ramp rates, intensity split, and
  longest-session progression from the logs rather than asserting.
- The athlete config files hold the parameters the system runs on. Use them for
  the analysis, but in the report refer to the athletes by role label (A/B/C)
  and include only the minimum detail needed to make a technical point.

## Output

1. **Executive verdict** (one paragraph): is the methodology sound, and what
   single change would most improve the training it produces?
2. **Dimension table**: the 12 dimensions, verdict each, one-line reason.
3. **Prioritised findings**, each with evidence, physiological rationale,
   and a concrete recommendation sized S/M/L:
   - **P0: outcome-critical defects** (unenforced injury/load caps, taper
     errors, heat-protocol gaps before a hot race, etc.)
   - **P1: meaningful fitness left on the table**
   - **P2: polish**
4. **Keep list**: what the system does well and must not lose in any rework.
5. **Doc-vs-code drift**: places the stated methodology and the implementation
   disagree.

Be direct and specific. The objective is the correctness of the analysis, not a
flattering write-up of the system.
