# ClaudeCoach — Feature Backlog

## Data & Integrations

### Garmin integration — Phase 1 of app transition (docs/app-transition-plan.md)
`python-garminconnect` (PyPI) gives HRV/sleep/body-battery directly from Garmin servers — no dependency
on Intervals.icu sync. Runs standalone on VM; token stored as garth session JSON per athlete.
- **Phase 1a (read path):** wire into morning-checkin.py for earlier HRV/sleep data (~6h)
- **Phase 1b (write path):** apply to Garmin Connect Developer Programme (2–5 day approval) for structured workout push to device
Previous verdict ("won't solve sync delay") applied only to the poll-via-API approach — reading directly before ICU sync *does* help.

## Coach Web Interface

### App transition — phased plan (docs/app-transition-plan.md)
Full roadmap from Telegram bot + static site → installable PWA with in-app chat and Garmin integration.
Phase 1 (Garmin reads, ~6h, no backend needed). Phase 2 (FastAPI backend, ~4h) enables browser features.
Recommended first sprint: Phase 1 → Phase 2 → Phase 3 (~18h total).

### Drag-and-drop event rescheduling
Allow the coach to drag planned sessions on the athlete dashboard (athlete-*.html) to reschedule them — currently requires going into Intervals.icu directly.
- Requires Phase 2 (FastAPI backend) first
- Add `icu_event_id` to weekCalendar in refresh script (0.5h)
- Refactor `renderLiveCalendar` to DOM nodes + drag events (3h)
- FastAPI reschedule endpoint (covered in Phase 2)
- Full breakdown: docs/app-transition-plan.md Phase 3

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
