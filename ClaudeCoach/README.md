# ClaudeCoach workspace

**For Claude:** read this at session start. It orients you to the workspace and points to where the authoritative plan lives.

**Race:** Ironman Italy Emilia-Romagna, **Saturday 19 September 2026**, Cervia.
**Athlete:** Jamie Diamond. Self-coached. **A-goal 9:30. B-goal 9:45. C-goal sub-10:06.**
**Heat is the binding constraint on this race, not fitness.** Re-evaluate every recommendation against heat impact.

---

## Where authority lives

| Source | What it holds | Read when |
|---|---|---|
| **Project custom instructions** | Athlete profile, race plan, build-CTL targets, heat/cooling/fuelling/sodium protocols, ankle rehab protocol, equipment plan, race-day execution rules. **The single source of truth for the plan.** | Always at session start. |
| `reference/rules.md` | Athlete-specific DOs and DON'Ts in one place, no rationale. | Always before prescribing. |
| `current-state.md` | Subjective layer IcuSync can't see — ankle pain, missed sessions, open actions, heat-acclimation log. Updated weekly by Jamie. | Before any weekly check-in or daily-readiness prompt. |
| **Intervals.icu via IcuSync MCP** | Activities, fitness (CTL/ATL/TSB), planned calendar, wellness (HRV/RHR/sleep). System of record for objective state. | Whenever data is needed. Never paste-pretend. |

---

## Read order at session start

1. **Project custom instructions** — always. Athlete profile, race plan, hard constraints, build targets.
2. `reference/rules.md` — every athlete-specific DO and DON'T in one place. Re-check before any prescription.
3. `reference/race-day-2025.md` — interval-level analysis of last year's IM Cervia (10:06). Foundational for the 9:30 case. Read on first session; re-read when discussing the goal gap.
4. `current-state.md` — subjective layer (sleep, niggles, weight, what got cut). Always before weekly check-in or daily-readiness.
5. Whatever else the question needs — see file map below.

---

## Folder layout

```
ClaudeCoach/
├── README.md                       # this file
├── current-state.md                # subjective state — Jamie updates weekly
│
├── reference/                      # static knowledge (read by Claude)
│   ├── rules.md                    # all athlete-specific rules in one place
│   ├── race-day-2025.md            # interval-level 2025 race analysis
│   ├── course.md                   # Cervia course intelligence
│   ├── decision-points.md          # pre-wired branches at every test/fork
│   ├── risk-register.md            # 14 risks, leading indicators, mitigations
│   ├── run-execution.md            # IM-run pacing, fuelling, blow-up modes
│   ├── run-form-and-strength.md    # form drills, strength programme, ankle-rehab priority
│   └── prompts.md                  # reusable Claude prompt patterns
│
└── templates/                      # operational scaffolding (Claude fills in)
    ├── session-library.md          # concrete workout templates + phase mapping
    ├── weekly-checkin.md           # weekly review output structure
    └── race-week-countdown.md      # D-7 to D-0 day-by-day checklist
```

---

## File map

| File | When to read |
|---|---|
| `reference/rules.md` | Always, before prescribing. Anchor values (FTP 316, threshold pace 4:02/km) live here. |
| `reference/race-day-2025.md` | When discussing the 9:30 goal, last-year diagnosis, where the time hides, or any pacing/fuelling/cooling decision. |
| `reference/course.md` | When discussing race-day specifics — bike profile, Bertinoro climbs, run loop, swim conditions, weather scenarios. |
| `reference/decision-points.md` | When a test result or trigger fires (FTP retest, CSS test, sweat test, ankle clearance, etc.). Pre-wired branches. |
| `reference/risk-register.md` | Weekly check-in scan. 14 risks with H+M probability, leading indicators, mitigations. |
| `reference/run-execution.md` | When discussing IM-run pacing, fuelling cadence, blow-up risks, or post-race debrief structure. |
| `reference/run-form-and-strength.md` | When the question is form, gait, drills, gym prescription, or injury prevention. |
| `reference/prompts.md` | When the user opens a recurring workflow (weekly check-in, niggle triage, session deep-dive, etc.). |
| `templates/session-library.md` | When prescribing sessions. Derives all targets from anchors. Includes phase-method mapping. |
| `templates/weekly-checkin.md` | Claude fills this in for the weekly review. |
| `templates/race-week-countdown.md` | D-7 to D-0 operations manual. Switch to this in race week. |

---

## Source material caveats

The reference files were extracted from a Notion template for standalone marathon/ultra runners, then rewritten for this IM build. Several assumptions in the source don't apply here:

| Topic | Override |
|---|---|
| **Gels as default fuel** | Athlete has sensory aversion to gels. Run-leg fuel is liquid-primary (Maurten 320 / PF90 in soft flask, cola from km 10, chews as backup). Clock-driven timing still applies: first fuel within 5 min of T2, 60–75 g CHO/hr. |
| **Marathon pacing numbers** | Don't transfer to IM run (5 hrs of cycling first). Use project IM-run rules: first 5 km ≥10 sec/km slower than goal, RPE/HR-led in heat. |
| **Heat protocol** | No heat protocol in source. Project custom instructions cover the full Zurawlew protocol. |
| **Stryd** | Optional in kit list. Stryd tables in `session-library.md` are a lookup if added, not a current requirement. |
| **Subjective wellness 1–10** | Ignored. Use objective signals only (HRV, RHR, sleep, weight). `current-state.md` defines what IS in scope. |
| **Special-needs bags** | Declined — time cost. All nutrition planned around carry + T1/T2 + course supply. |
| **Ice at aid stations** | Unavailable at Cervia. Athlete-supplied via T1/T2 insulated coolers only. |

---

## How a typical session goes

**Weekly check-in (Sunday/Monday):**
1. Read project instructions + `reference/rules.md` + `current-state.md`.
2. Pull last 7 days from IcuSync (activities, CTL/ATL/TSB, planned vs completed).
3. Optionally run `ironman-analysis/scripts/run_baseline.py` for arithmetic.
4. Fill out `templates/weekly-checkin.md` shape with real numbers.
5. Push next week's sessions to Intervals.icu via IcuSync.
6. Confirm push.

**Daily readiness (morning of a quality day):**
1. Read `reference/rules.md` + `current-state.md`.
2. Pull today's planned session + 7-day load + last night's HRV/RHR/sleep.
3. Apply cross-validation rule (multi-signal corroboration).
4. Go / modify / skip with one-sentence rationale. Push any modification via IcuSync.

**Session deep-dive (after key bike or run):**
1. Pull full activity stream via IcuSync.
2. Use the **Session deep-dive** prompt in `reference/prompts.md`.
3. Compare to plan; flag drift, decoupling, form-power red flags.

**Race-week (from D-7):**
1. Switch to `templates/race-week-countdown.md` as the day-by-day driver.
2. Heat-bath maintenance is non-negotiable (3–4 sessions in final 7 days).
3. No new fuel, kit, shoes, or pacing in last 4 weeks.

---

## Standing rules (non-negotiable)

- **UK English. Concise. Tables when comparing across rows. Flag uncertainty explicitly.**
- **Rationale per recommendation in one sentence.** No rationale → don't include.
- **Heat = binding constraint.** Every recommendation re-evaluated against heat impact.
- **Never recommend gels for run-leg fuel.** Sensory aversion — liquid primary only.
- **Never assume the ankle is healed.** Ask for current pain status before run-load prescriptions.
- **Multi-signal corroboration required for load reductions.** HRV alone is never the trigger.
- **Subjective wellness fields ignored.** Use objective signals only.
- **Pull data via IcuSync, don't fabricate.** If IcuSync is down, say so and ask for a manual paste.

---

## Known content gaps

- No swim form / catch / pull / sighting reference at the level of `run-form-and-strength.md`.
- No bike form / pedalling efficiency / aero diagnostic reference. Project instructions cover position fit and pacing rules.
- These are **flagged**, not filled with fabrication.

---

## Scripts available

`ironman-analysis/` exposes pure Python primitives for training-load arithmetic. Use it whenever conversation would otherwise produce mental-math estimates of CTL/ATL/TSB, ramp, ATL−CTL gap streak, or trajectory-vs-build-targets. Conversation handles judgement; code handles arithmetic. See `ironman-analysis/SKILL.md` for the invocation contract.
