# Reference index — read this first

Claude-facing index. The authoritative race plan lives in **project custom instructions** (athlete profile, race details, heat/cooling/fuelling protocols, build-CTL targets, equipment, ankle protocol, race-day execution rules). The files in this folder are *reference* material the project instructions assume but don't reproduce.

## Read order at session start

1. **Project custom instructions** — always. Athlete profile, race plan, hard constraints, build targets.
2. `01-hard-rules.md` — every athlete-specific DO and DON'T pulled from project + this folder, in one place. Re-check before any prescription.
3. `race-day-2025.md` — **interval-level analysis of last year's IM Cervia (10:06)**. Foundational for understanding the 9:30 goal. Read on first session and re-read whenever the gap to 9:30 is being discussed.
4. `02-conflicts.md` — where the source material in this folder contradicts the live project plan. Trust the project plan.
5. `../templates/current-state.md` — subjective layer (sleep, niggles, weight, what got cut). Updated weekly. Always read before weekly check-in or daily-readiness.
6. Whatever else is needed for the question at hand (table below).

## File map

| File | When to read |
|---|---|
| `01-hard-rules.md` | Always, before prescribing. Rules-only summary, no rationale. Anchor values (FTP 316, threshold pace 4:02/km) live here. |
| `race-day-2025.md` | When discussing the 9:30 goal, last-year diagnosis, where the time hides, or any pacing/fuelling/cooling decision. Foundational. |
| `cervia-course.md` | When discussing race-day specifics — bike profile, Bertinoro climbs, run loop, swim conditions, weather scenarios. |
| `decision-points.md` | When a test result or trigger fires (FTP retest, CSS test, sweat test, ankle clearance, etc.). Pre-wired branches. |
| `risk-register.md` | Weekly check-in scan. Tracks H+M probability risks, leading indicators, mitigations. |
| `02-conflicts.md` | When citing anything from `methodology.md`, `run-execution.md`, or `run-form-and-strength.md` — check for athlete-specific overrides first. |
| `methodology.md` | When designing or justifying session shape: Canova / Daniels VDOT / 80/20 / Pfitz / Norwegian DT / Blended. Includes Stryd CP-zones lookup. |
| `run-execution.md` | When discussing IM-run pacing, fuelling cadence, blow-up risks, or post-race debrief structure. |
| `run-form-and-strength.md` | When the question is form, gait, drills, gym prescription, or injury prevention. The –20 min on the run leg lives here. |
| `prompts.md` | When the user opens a recurring workflow (weekly check-in, niggle triage, blow-up analysis, etc.). Lift the template, fill, run. |
| `_research-scan.md` | Background only: how this setup compares to other AI coaching systems. Not lookup material. Read only if asked about toolchain choices. |

## What's NOT in this folder (known gaps)

- **Swim form / catch / pull / sighting** — no equivalent of `run-form-and-strength.md`. Project instructions cover the swim *plan* (CSS test, weekly OWS from May, wetsuit conditioning) but not diagnostic-level form. Flag this if asked.
- **Bike form / aero / pedalling efficiency** — same. Project covers position fit, equipment plan, pacing rules. No drill library.
- **Heat acclimation, cooling, hydration, sodium, bike fuelling, ankle rehab, equipment plan** — all in project custom instructions, not duplicated here.
- **Templates and operational scaffolding** — see `../templates/`.
- **Analysis primitives (CTL/ATL/TSB code, dedup, ramp flags)** — in `../ironman-analysis/`. Code answers arithmetic; conversation answers judgement.

## Toolchain assumed

Claude Project + Notion + Intervals.icu (system of record) + IcuSync MCP (Claude ↔ Intervals connector — pulls activities/fitness/plan, pushes planned workouts; Intervals auto-syncs to Garmin). All standing prompts start with "Use IcuSync to pull..." — never paste data manually if it can be pulled.

## Output style

UK English. Concise. Tables when comparing across rows. Flag uncertainty explicitly. No motivational filler. **State rationale for every training, pacing, fuelling, or recovery recommendation in one sentence** — what adaptation, what risk, what data point. If it can't be justified in one sentence, don't include it.

## Provenance

Source material here is extracted from a Notion template (*AI Running Coach by a Real Runner*), fetched 25 April 2026, then rewritten for Claude consumption April 2026. Athlete-specific overrides documented in `02-conflicts.md`.
