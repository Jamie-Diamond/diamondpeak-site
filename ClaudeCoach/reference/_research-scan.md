# Research scan: other AI coaching systems + gaps in current setup

> **Status:** historical research scan, pre-restructure. Many of the Part 4 recommendations have been actioned: `current-state.md` created in `templates/`; daily-readiness prompt added; IcuSync discipline encoded in `prompts.md`; `01-hard-rules.md` and `02-conflicts.md` added. Cross-references below use pre-restructure filenames (`coaching-prompts.md` → now `prompts.md`; `running-form-and-strength.md` → now `run-form-and-strength.md`). Read for *toolchain context* and *open gaps* (notably swim/bike form companion files, .fit/.zwo structured-artefact fallback), not as current-state instructions.

Quick scan completed 25 April 2026. Three angles: existing templates and MCPs to steal from, prompting/coaching-design techniques others have published, and a critique of where the current setup has holes.

---

## Part 1 — Existing systems to steal from

### A. Open-source: Felix Rieseberg's Claude Coach

[GitHub repo](https://github.com/felixrieseberg/claude-coach) · [demo site](https://felixrieseberg.github.io/claude-coach/) · MIT licensed.

Built by an Anthropic engineer (independent project). Triathlon / marathon / Ironman support. Architecturally interesting:

- Distributed as a **Claude Skill** (`.zip` you upload via Skills settings on Claude.ai or `/install-skill` in Claude Code), not as a system prompt or Notion template.
- Pulls Strava activities directly via your own Strava API credentials (Client ID + Secret) — data stays in your browser, not on a server.
- Generates a structured plan with editable workouts, marks complete, updates HR zones / LTHR / threshold pace / FTP as you go.
- **Exports workouts as `.ics` (calendar), `.zwo` (Zwift), `.fit` (Garmin), `.mrc` (TrainerRoad/ERG)** — that's the standout feature. Closes the loop from "Claude wrote a session" to "session is on the device" without copy-paste.
- Suggested launch prompt: *"Help me create a training plan for the Ironman 70.3 Oceanside on March 29th 2026 using the 'coach' skill."*

**Worth borrowing:** the export-formats list as a fallback. IcuSync is the primary watch loop here, but if the connector ever fails or you need a one-off `.zwo` for an indoor session that isn't in the calendar, having Claude generate the structured file directly is a useful escape hatch.

### B. Hosted MCP: AI Endurance

[Docs](https://aiendurance.com/docs/mcp). Custom Claude.ai connector at `https://aiendurance.com/mcp`. Subscription required (separate account). Capabilities exposed via MCP:

- Training plan management — create/modify structured workouts (intervals, zones) for swim, bike, run.
- Activity analysis — power, HR, HRV, pace, running power.
- ML-based race-time predictions and fitness forecasting.
- Recovery — HRV, resting HR, sport-specific recovery scores.
- Daily nutrition — calorie + macronutrient targets tied to training load.
- Workout scheduling with automatic sync out to Garmin / TrainingPeaks / Intervals.icu.
- Plan-adherence tracking by zone (actual vs prescribed).

**Worth borrowing:** the *conversation patterns* in the docs are gold for designing prompts. Examples: *"Move tomorrow's threshold workout to Friday"*, *"Am I recovered enough for today's hard workout?"*, *"Compare my last 3 long runs"*, *"Build me a 60min tempo ride at 85% of Threshold for Sunday"*. Added to `coaching-prompts.md`.

### C. The MCP / connector landscape

| Tool | What it does | Status here |
|---|---|---|
| **[IcuSync](https://icusync.icu)** | Hosted Claude → Intervals.icu connector. Pulls activities, fitness, plan from Intervals.icu; pushes planned workouts back to the calendar; Intervals.icu auto-syncs to Garmin. | **In use.** Active connector. All prompts assume this is wired up. |
| [AI Endurance MCP](https://aiendurance.com/mcp) | Hosted MCP server. Multi-discipline. Includes ML race-time predictions, nutrition, recovery model. | Not in use. Would replace the Intervals.icu+IcuSync stack with their own platform. Worth knowing about as the closest "all-in-one" alternative. |
| [AthleteData / multi-source MCP](https://forum.slowtwitch.com/t/hosted-mcp-server-for-connecting-claude-with-all-your-data-garmin-intervals-icu-strava-etc/1297701) | Hosted MCP combining Garmin, Strava, Whoop, Hevy, Oura, Intervals.icu in one connector. | Not in use. Useful only if Oura/Whoop/Strava get added as separate inputs alongside Intervals.icu. |
| [TrainingPeaks MCP (jamsusmaximus)](https://www.pulsemcp.com/servers/jamsusmaximus-trainingpeaks) | 52 tools. Workouts, performance analysis, PRs, equipment, templates. | Not in use. TrainingPeaks isn't part of the stack. |
| [Intervals.icu MCP (vsidhart)](https://lobehub.com/mcp/vsidhart-intervals-mcp) | 48 tools. Open-source equivalent of IcuSync. | Not in use. Self-host alternative; only relevant if IcuSync ever gets dropped. |
| [Strava MCP servers (multiple)](https://forum.intervals.icu/t/mcp-server-for-connecting-claude-with-intervals-icu-api/95999) | Activity queries, training analysis. | Not needed — IcuSync covers the same activities via Intervals.icu's Strava sync. |

### D. Custom GPT: ELEO (Trail Runner Substack)

[Part I article](https://trailrunner.substack.com/p/how-to-create-your-own-ai-triathlon). The most useful single source on prompt design. Built by a 56-year-old triathlete-ultrarunner doing both IM and ultras in 2026. Mid-level technical — no code required. Three-section prompt structure analysed in Part 2 below.

### E. Other templates noted but not deep-fetched

- [3Mate Sprint Notion template](https://tomasvysny.gumroad.com/l/3matesprint) — sprint tri only, less relevant for IM.
- [DocsBot Ironman Triathlon Trainer prompt](https://docsbot.ai/prompts/personal/ironman-triathlon-trainer) — short prompt template, light on substance.
- [Building a Modern Training Assistant With Claude and Garmin (Medium / DZone)](https://medium.com/the-architects-mind/ai-powered-triathlon-coaching-building-a-modern-training-assistant-with-claude-and-garmin-27f44dc35377) — architecture write-up (Head Coach / Lead Coach / sub-coach pattern).

---

## Part 2 — Prompting and coaching-design techniques

Distilled from ELEO (Trail Runner Substack) plus the Claude Coach Skill and AI Endurance docs.

### 1. Role specificity changes the output meaningfully

ELEO's author found that *"endurance training coach"* gave a plan-heavy short-term output, whereas *"endurance sports and health optimisation coach"* generated more strength, mobility, and recovery work. The phrasing of the role is a lever, not decoration.

For an IM athlete with a –20 min run goal hinging on heat tolerance and fuelling, a role like *"endurance sports performance coach with heat-physiology specialism"* would bias toward the relevant levers.

### 2. Three-section prompt structure that holds up

ELEO's prompt has a clear shape worth copying:

1. **Role + context + interaction model** — who you are, who I am, what to do when I share data, what to do when context is missing.
2. **Prioritisation areas** — explicit, numbered. Each one names what it covers and what trade-offs it makes.
3. **Tone and response style** — how to write, what to include, what to skip.

The current project instructions cover sections 1 and 2 implicitly (heavy on substance, light on interaction model). Section 3 isn't there at all. Adding a tone section would standardise output style.

### 3. "Every item needs rationale"

ELEO prompt phrase: *"Every item needs a rationale — if you cannot explain why something is in the training plan, it does not belong there."* This is a single sentence that does heavy lifting. It forces Claude to surface its reasoning rather than pattern-match the internet's median view.

The current instructions push back on flawed reasoning but don't *require* a stated rationale per recommendation. Adding this would tighten output quality.

### 4. Prescribe failure modes upfront

ELEO names the author's three known failure modes: overtrains in good weather, drifts off training-phase discipline, and 2026 is the year of competing IM + ultra goals. The GPT is told to police these specifically.

Equivalent for this project: overshooting CTL ramp while ankle is still in rehab, racing the bike on Bertinoro, drifting off run-fuelling discipline in heat. These already exist in the `Risk and priority assessment` section of the project instructions — good. The reinforcement is to make them an *active rule for every recommendation*, not just a static note.

### 5. Cross-validate across data inputs

ELEO: *"Decisions must be cross-validated: don't recommend something if it's not aligned across inputs."* Practical: don't recommend a hard session because the calendar says so if HRV is tanked, sleep was 5 hr, and yesterday's RPE was elevated.

Current setup has the data sources (Intervals.icu, sleep target, body weight) but no explicit cross-validation rule.

### 6. Race-specificity is a prompt driver, not a plan driver

ELEO's prompt section *"Create training plans for target races with specificity"* tells the GPT to know course features (hills, temperature, climate, wetsuit/non-wetsuit) and modify training accordingly. Course recon becomes part of the prompt's job, not just yours.

This project already has race-specific notes (Bertinoro hills, salt pans, ice-availability constraint, sea-temperature uncertainty). The prompt-level instruction would be: *"For every weekly recommendation, check whether anything race-specific in the project notes should change today's session."*

### 7. Memory / state strategy

ELEO's flagged limitation: GPTs lose context across long conversations. Their fix: store the training plan as a CSV in the Knowledge section, and instruct the GPT *"look up today's date from system resources, and know where we are in the current training plan"* on every turn.

In this stack the equivalent is: **make IcuSync the source of truth for state.** Intervals.icu already holds CTL/ATL/TSB, completed activities with metrics, and the planned calendar. Every standing prompt in `coaching-prompts.md` now starts with *"Use IcuSync to pull..."* — that's the discipline. The remaining gap is the *subjective* layer (sleep, niggles, weight-on-day, what got cut and why) which IcuSync doesn't see and the project instructions can't update mid-block. A short `current-state.md` file in the workspace, updated weekly with the subjective bits, plugs that hole.

### 8. The Skills + structured-output pattern (Felix's Claude Coach)

Rather than asking Claude to write training in Markdown for the user to copy, Felix's tool has Claude produce `.ics`, `.zwo`, `.fit`, `.mrc` files directly. This is a different pattern entirely: Claude as a generator of structured artefacts, with the user just downloading them.

For triathlon: a Claude session that outputs a `.fit` workout for tomorrow's bike interval set is significantly better than copy-pasting steps into Garmin Connect. Worth considering as a future-build target.

### 9. Multi-coach / sub-coach pattern

The Architect's Mind Medium article describes a *Head Coach* (one persona, big picture, season goals) and a *Lead Coach* (rolling 14-day plan). Some setups go further with sport-specific sub-coaches.

Whether this matters depends on whether the single project gets confused juggling swim + bike + run constraints. For Cervia, three disciplines plus heat plus rehab is already a lot of variables. A swim-only / bike-only / run-only sub-conversation triggered by a tag in the prompt might produce sharper output than mixed prompts.

---

## Part 3 — Gaps in current setup

Critique of the existing project instructions + Notion extract, in priority order.

### High priority

**1. Run-bias in the imported material.** The Notion extract is run-only. The IM has three legs and the bike represents –11 min and the swim –2 min of the goal split. There's nothing equivalent for swim form (catch / pull / kick / sighting) or bike form (aerodynamics / pedalling efficiency / position) at the level of detail in `running-form-and-strength.md`. Recommend: add equivalent files for swim and bike, structured the same way (issue → diagnostic → fix → measure), drawing on the project instructions plus a dedicated session.

**2. Subjective state isn't captured anywhere Claude can see.** IcuSync gives Claude everything quantified — CTL/ATL/TSB, power files, HR, planned vs actual. But sleep quality, ankle pain score, body weight on the day, niggles, what got missed and why — none of that lives in Intervals.icu. Recommend a `current-state.md` in the workspace, updated each Sunday in 60 seconds: subjective freshness, sleep average, niggles 1–10, weight, what got cut and why, open actions. Tell Claude in the project instructions to read it before any weekly check-in or daily-readiness prompt.

**3. IcuSync prompt discipline.** The connector is set up, but the value compounds only if every prompt actually uses it. Concretely:
   - Every weekly check-in starts with *"Use IcuSync to pull..."* — never paste data manually if Claude can pull it.
   - Every plan revision pushes back to the calendar in the same turn (single source of truth — Intervals.icu).
   - Every session deep-dive pulls the full activity stream, not a summary screenshot.
   - If IcuSync fails on a given day, Claude says so explicitly rather than working from stale or partial data.

   The prompts in `coaching-prompts.md` now enforce this — keep the discipline.

**4. Race-day cooling protocol relies on T1/T2 ice retention assumption that isn't tested.** Project instructions specify "test 5+ hr ice retention in a hot car pre-race" — make sure this is on a calendar somewhere, not just in instructions. Goes in `current-state.md` as an open action when that file is created.

### Medium priority

**5. No "rationale required" guardrail on Claude's output.** Add to project instructions: *"For every training or pacing recommendation, state the rationale in one sentence — what physiological adaptation, what risk, what data point. If you can't, don't make the recommendation."*

**6. No daily-readiness loop.** The prompt library has weekly check-in but not a daily one. Daily readiness should be its own pattern: *given last night's HRV/sleep/RPE, is today's planned session right, modified, or skipped?* Added to `coaching-prompts.md`.

**7. Tone section missing from project instructions.** No explicit guidance on output format (length, when to use tables, when to use bullets). Recommend a short tone clause: *"Default to prose. Use tables when comparing across rows. No motivational filler. Flag uncertainty explicitly."*

### Low priority

**8. Sport-specific sub-prompts.** The single-project setup is fine for now. If output starts mixing concerns (run advice tangled into a bike session review), consider triggering a tag-based sub-coach pattern (`#swim`, `#bike`, `#run`).

**9. Structured artefact output as a fallback.** IcuSync handles the watch loop, so this isn't a primary gap. But Claude generating `.fit` / `.zwo` files directly into the workspace folder remains a useful fallback for the day IcuSync is down or for one-off indoor sessions that aren't on the calendar. Felix's Claude Coach is the precedent.

**10. File naming consistency in this folder.** Minor: numeric prefixes only on the index. Not worth changing now.

---

## Part 4 — Recommended next actions

In approximate order:

1. Create a `current-state.md` template — the subjective-layer state file. Add a clause to the project instructions telling Claude to read it before any weekly check-in or daily-readiness prompt.
2. Add a "rationale required" sentence and a short tone section to the project's custom instructions.
3. Build swim and bike companion files to `running-form-and-strength.md` — the IM has three legs and the run is only the largest opportunity, not the only one.
4. **Test IcuSync end-to-end on a real weekly check-in** before the build phase starts (end May). Confirm: it pulls 7 days of activities cleanly, it pushes a full week back, and Intervals.icu syncs the planned workouts to Garmin. If any link in that chain doesn't work, find out now, not in July.
5. Optional: try Felix's Claude Coach skill once for the structured-artefact pattern, even if not adopted long-term. Useful as a fallback if IcuSync ever fails (Claude generates `.fit` directly into the workspace folder).

## Sources

- [Claude Coach (felixrieseberg) — GitHub](https://github.com/felixrieseberg/claude-coach)
- [Claude Coach demo site](https://felixrieseberg.github.io/claude-coach/)
- [AI Endurance MCP docs](https://aiendurance.com/docs/mcp)
- [Trail Runner Substack — Building a custom GPT triathlon coach (ELEO Part I)](https://trailrunner.substack.com/p/how-to-create-your-own-ai-triathlon)
- [IcuSync](https://icusync.icu)
- [Hosted multi-source MCP — Slowtwitch forum thread](https://forum.slowtwitch.com/t/hosted-mcp-server-for-connecting-claude-with-all-your-data-garmin-intervals-icu-strava-etc/1297701)
- [TrainingPeaks MCP server (jamsusmaximus)](https://www.pulsemcp.com/servers/jamsusmaximus-trainingpeaks)
- [Intervals.icu MCP (vsidhart)](https://lobehub.com/mcp/vsidhart-intervals-mcp)
- [The Architect's Mind — Building a Modern Training Assistant With Claude and Garmin](https://medium.com/the-architects-mind/ai-powered-triathlon-coaching-building-a-modern-training-assistant-with-claude-and-garmin-27f44dc35377)
- [MCP and Sports/Fitness landscape — ChatForest](https://chatforest.com/guides/mcp-sports-fitness/)
