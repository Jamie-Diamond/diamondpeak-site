# ClaudeCoach — Feature Backlog

## Data & Integrations

### Garmin / Strava API investigation
Investigate direct Garmin Connect and Strava APIs as upstream data sources.
- **Strava write access** is the priority: post activities, update descriptions, add coaching notes directly from the bot — currently this data is lost before it reaches ClaudeCoach
- Garmin Connect API would give richer raw data (HRV, sleep stages, body battery) earlier than the Intervals.icu sync cycle
- Key question: does Strava write access cover activity description + segment tagging, or just creation?

## Coach Web Interface

### Drag-and-drop event rescheduling
Allow the coach to drag planned sessions on the athlete dashboard (athlete-*.html) to reschedule them — currently requires going into Intervals.icu directly.
- Would need write-back to Intervals.icu via the edit_workout MCP/API
- Scope: drag within a week view; snap to day; confirm modal before write

## Bot

### Tiered coaching language (3 levels) — plan at docs/coaching-levels-plan.md
Add a `coaching_level` field to `profile.json` with values `beginner`, `mid`, `pro`. The bot and all scripts should adjust vocabulary and data density accordingly:
- **beginner** (Calum): plain English only, no metrics jargon, effort-based descriptions
- **mid** (Kathryn, default): current behaviour — plain-English labels (Fitness/Load/Fatigue/Form) with supporting numbers
- **pro** (Jamie): plain-English labels + acronyms in parentheses on first use ("Fitness (CTL)"), full technical depth

### Coaching levels — chart label variants
Follow-on to tiered coaching language: at `pro` level, chart axis/legend labels should show acronyms alongside plain-English labels (e.g. "Fitness (CTL)"). Requires threading `coaching_level` through to `charts.py` render calls — defer until the prompt-injection work is complete and validated.

### Image recognition (done — 2026-05-19)
Bot now handles photo messages (Garmin splits screenshots etc.) via `--image` flag to Claude CLI.
