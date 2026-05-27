# ClaudeCoach — Feature Backlog

## Data & Integrations

### Garmin Connect API — researched, not worth pursuing
Direct Garmin API exists (OAuth 1.0a, `garminconnect` Python package). HRV/sleep data available,
write-back to device possible. **Verdict: won't solve the morning sync delay.** The bottleneck
is device-to-cloud sync timing, not the API layer — sleep data arrives after the user's morning
sync whether we poll Garmin direct or via Intervals.icu. Stick with Intervals.icu.
Possible future angle: prompt athletes to sync before bed, or investigate Garmin auto-sync scheduling.

## Coach Web Interface

### Drag-and-drop event rescheduling
Allow the coach to drag planned sessions on the athlete dashboard (athlete-*.html) to reschedule them — currently requires going into Intervals.icu directly.
- Would need write-back to Intervals.icu via the edit_workout MCP/API
- Scope: drag within a week view; snap to day; confirm modal before write

## Bot

### Image recognition (done — 2026-05-19)
Bot now handles photo messages (Garmin splits screenshots etc.) via `--image` flag to Claude CLI.

### Strava write-back (done — 2026-05-20)
`strava_client.py` — `update_activity(name, description)` live. Activity descriptions auto-written
after every session; sailing activities auto-renamed from persistent-rules.md.

### Tiered coaching language — 3 levels (done — 2026-05-27)
`coaching_level` in `profile.json` (beginner/mid/pro). Level block injected into all prompts:
activity-watcher, bot, morning/evening checkins, prescription, weekly summary.
Calum=beginner, Kathryn=mid, Jamie=pro.

### Coaching levels — chart label variants (done — 2026-05-27)
`mid`: plain-English labels ("Fitness", "Fatigue", "Form", "Load") on all chart axes and legends.
`pro`: combined labels ("Fitness (CTL)", "Fatigue (ATL)", "Form (TSB)", "TSS").
Threaded through charts.py, bot.py (process_charts + quick chart functions), morning-checkin.py.
