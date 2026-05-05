# ClaudeCoach workspace

**For Claude:** read this at session start. It orients you to the workspace and points to where the authoritative plan lives.

**Race:** Ironman Italy Emilia-Romagna, **Saturday 19 September 2026**, Cervia.
**Athlete:** Jamie Diamond. Self-coached. **A-goal 9:30. B-goal 9:45. C-goal sub-10:06.**
**Heat is the binding constraint on this race, not fitness.** Re-evaluate every recommendation against heat impact.

## Where authority lives

| Source | What it holds | Read when |
|---|---|---|
| **Project custom instructions** | Athlete profile, race plan, build-CTL targets, heat/cooling/fuelling/sodium protocols, ankle rehab protocol, equipment plan, race-day execution rules. **The single source of truth for the plan.** | Always at session start. |
| `reference/01-hard-rules.md` | Athlete-specific DOs and DON'Ts in one place, no rationale. | Always before prescribing. |
| `reference/02-conflicts.md` | Where extracted source material in this folder contradicts the live plan. | Before quoting anything from `methodology.md`, `run-execution.md`, `run-form-and-strength.md`. |
| `templates/current-state.md` | Subjective layer IcuSync can't see — ankle pain, missed sessions, open actions, heat-acclimation log. Updated weekly by Jamie. | Before any weekly check-in or daily-readiness prompt. |
| **Intervals.icu via IcuSync MCP** | Activities, fitness (CTL/ATL/TSB), planned calendar, wellness (HRV/RHR/sleep). System of record for objective state. | Whenever data is needed. Never paste-pretend. |

## Folder layout

```
ClaudeCoach/
├── README.md                       # this file
├── reference/                      # static knowledge (rewritten for Claude consumption)
│   ├── 00-index.md                 # read order + file map for the reference folder
│   ├── 01-hard-rules.md            # all athlete-specific rules in one place
│   ├── 02-conflicts.md             # source-vs-plan reconciliations
│   ├── methodology.md              # training methods + Stryd reference
│   ├── run-execution.md            # IM-run pacing, fuelling, blow-up modes, debrief
│   ├── run-form-and-strength.md    # form drills, strength programme, ankle-rehab priority
│   ├── prompts.md                  # reusable Claude prompt patterns
│   └── _research-scan.md           # background — other AI coaching systems (rarely needed)
├── templates/                      # operational scaffolding
│   ├── current-state.md            # subjective state — Jamie updates weekly
│   ├── weekly-checkin.md           # Claude's weekly review output structure
│   ├── race-week-countdown.md      # day-by-day from D-7 to D-0
│   └── session-library.md          # concrete workout templates by discipline
└── ironman-analysis/               # Python primitives (CTL/ATL/TSB, dedup, ramp flags)
    ├── README.md                   # repo overview
    ├── SKILL.md                    # invocation contract for Claude
    └── ...                         # pyproject, primitives/, tests/, fixtures/, runs/, schemas/
```

## How a typical session goes

**Weekly check-in (Sunday/Monday):**
1. Read project instructions + `reference/01-hard-rules.md` + `templates/current-state.md`.
2. Pull last 7 days from IcuSync (activities, CTL/ATL/TSB, planned vs completed).
3. Optionally run `ironman-analysis/scripts/run_baseline.py` or its successors for arithmetic.
4. Fill out `templates/weekly-checkin.md` shape with real numbers.
5. Push next week's sessions to Intervals.icu via IcuSync.
6. Confirm push.

**Daily readiness (morning of a quality day):**
1. Read `01-hard-rules.md` + `current-state.md`.
2. Pull today's planned session + 7-day load + last night's HRV/RHR/sleep.
3. Apply cross-validation rule (multi-signal corroboration).
4. Go / modify / skip with one-sentence rationale. Push any modification via IcuSync.

**Session deep-dive (after key bike or run):**
1. Pull full activity stream via IcuSync.
2. Use the **Session deep-dive** prompt in `prompts.md`.
3. Compare to plan; flag drift, decoupling, form-power red flags.

**Race-week (from D-7):**
1. Switch to `templates/race-week-countdown.md` as the day-by-day driver.
2. Heat-bath maintenance is non-negotiable (3–4 sessions in final 7 days).
3. No new fuel, kit, shoes, or pacing in last 4 weeks.

## Standing rules (non-negotiable)

- **UK English. Concise. Tables when comparing across rows. Flag uncertainty explicitly.**
- **Rationale per recommendation in one sentence.** No rationale → don't include.
- **Heat = binding constraint.** Every recommendation re-evaluated against heat impact.
- **Never recommend gels for run-leg fuel.** Sensory aversion. See `reference/02-conflicts.md` §1.
- **Never assume the ankle is healed.** Ask for current pain status before run-load prescriptions.
- **Multi-signal corroboration required for load reductions.** HRV alone is never the trigger.
- **Subjective wellness fields ignored.** Athlete decision. Use objective signals only.
- **Pull data via IcuSync, don't fabricate.** If IcuSync is down, say so and ask for a manual paste.

## Known content gaps in this folder

- No swim form / catch / pull / sighting reference at the level of `run-form-and-strength.md`.
- No bike form / pedalling efficiency / aero diagnostic reference. Project instructions cover position fit and pacing rules.
- These are **flagged**, not filled with fabrication.

## Scripts available

`ironman-analysis/` exposes pure Python primitives for training-load arithmetic. Use it whenever conversation would otherwise produce mental-math estimates of CTL/ATL/TSB, ramp, ATL−CTL gap streak, or trajectory-vs-build-targets. Conversation handles judgement; code handles arithmetic. See `ironman-analysis/SKILL.md` for the invocation contract.
