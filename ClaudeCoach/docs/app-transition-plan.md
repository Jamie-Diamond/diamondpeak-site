# ClaudeCoach — Transition to Full App

**Written:** 2026-05-27  
**Status:** Plan — execute incrementally over 6–18 months

---

## Where we are now

| Component | Current implementation |
|---|---|
| AI coaching | Telegram bot (Python, Claude CLI) |
| Dashboards | Static HTML on GitHub Pages, data via training-data.json |
| Scheduling | Intervals.icu directly — no write-back from the dashboard |
| Garmin data | Via Intervals.icu sync (delayed ~morning) |
| Auth | None (website), Telegram identity (bot) |
| Athletes | Jamie, Kathryn, Calum — all on same VM |

**Core constraint:** The website is fully static (GitHub Pages). There is no server the browser can talk to. Every interactive feature — rescheduling, session logging, chat — requires a backend.

---

## Target state

A single product with three surfaces:
1. **Mobile/web app** — dashboard, calendar, session log, in-app chat
2. **Telegram** — retained as an optional parallel channel (low cost to keep)
3. **Coach view** — admin panel: drag-and-drop scheduling, athlete overview

Backed by:
- A **FastAPI server** on the existing VM (single enabling step for everything else)
- **Garmin integration** for direct HRV/sleep reads and workout writes to device
- **Intervals.icu write-back** for calendar changes
- **Proper auth** (JWT or passkey — no passwords if possible)

---

## Phases

### Phase 1 — FastAPI backend on VM (enabler for everything)
**Effort:** ~4 hours  
**Why first:** Every subsequent phase depends on this. Without a backend the website stays static forever.

What to build:
- FastAPI app on VM, port 8080, proxied through nginx (or a new subdomain)
- Endpoints:
  - `GET /athletes/{slug}/data` — returns training-data.json content (replaces direct GitHub Pages file)
  - `POST /athletes/{slug}/events/{id}/reschedule` — calls `IcuClient.edit_workout()`
  - `POST /athletes/{slug}/sessions/{id}` — update session log fields (RPE, notes, nutrition)
- Auth: shared API key in header for now (`X-CC-Key: <secret>`). One key per athlete, stored in athletes.json.
- Deploy as a systemd service alongside claudecoach-bot.service

This phase alone enables drag-and-drop and session log editing from the browser.

---

### Phase 2 — Interactive calendar (drag-and-drop rescheduling)
**Effort:** ~8–9 hours (see backlog for full breakdown)  
**Depends on:** Phase 1

Changes:
- `refresh-site-data.py`: add `icu_event_id` to each planned event in `weekCalendar`
- `renderLiveCalendar()`: refactor from `innerHTML` string-builder to DOM node construction; add `draggable` attribute and `dragstart`/`dragover`/`drop` event listeners
- On drop: `POST /athletes/{slug}/events/{id}/reschedule` to the Phase 1 API; optimistic re-render; trigger background data refresh
- Past-date guard: prevent dragging events to dates in the past

---

### Phase 3 — PWA shell (installable, mobile-friendly)
**Effort:** ~3 hours  
**Depends on:** Phase 1 (for API-backed data)

Changes:
- Add `manifest.json` (name, icons, theme colour, display: standalone)
- Add a service worker for offline caching of the shell and last-fetched data
- Mobile CSS pass: ensure dashboards are usable on a phone (current pages are desktop-oriented)
- This makes the website installable on iOS/Android home screen — no App Store needed

At this point athletes have a "ClaudeCoach app" on their phone that shows dashboards and allows rescheduling. Coaching chat is still Telegram.

---

### Phase 4 — In-app chat (replaces Telegram as primary surface)
**Effort:** ~12 hours  
**Depends on:** Phase 1 (API backend)

What to build:
- Chat UI in the PWA: message thread, send input, markdown rendering
- Backend endpoint: `POST /athletes/{slug}/chat` — receives message, runs through the same Claude pipeline as bot.py, returns response
- Streaming support: use Server-Sent Events so the response streams in like a real chat
- Image upload: allow photo attachments (replaces Telegram photo handling)
- History: share the same `telegram/history.json` so context is preserved across both surfaces
- Push notifications via Web Push API (service worker) — replaces Telegram notifications for morning cards, activity analysis etc.

Keep Telegram running in parallel indefinitely — low cost and some athletes may prefer it.

---

### Phase 5 — Garmin integration
**Effort:** ~6 hours (unofficial read path) + ~developer programme approval wait (official write path)  
**Depends on:** Phase 1 (API backend to store tokens)

**Two sub-paths:**

#### 5a — Reading health data (unofficial, can do now)
Use `python-garminconnect` (PyPI, cyberjunky/python-garminconnect):
```python
from garminconnect import Garmin
client = Garmin(email, password)
client.login()
hrv = client.get_heart_rate_variability("2026-05-27")   # returns RMSSD
sleep = client.get_sleep_data("2026-05-27")             # stages, score, secs
body_battery = client.get_body_battery("2026-05-27")
```
Store tokens after login (client exports `garth` session tokens — persist to avoid re-auth on every run).  
Use this to give the morning briefing HRV/sleep data *immediately* at boot, before Garmin → ICU sync completes.  
**Risk:** unofficial — Garmin can break it with a site update. Acceptable for 3 athletes; not for a commercial product.

#### 5b — Writing workouts to Garmin device (official, apply when ready)
Apply to Garmin Connect Developer Programme (developer.garmin.com).  
Approval: 2–5 business days. Credentials: consumer key + consumer secret.  
Use Training API to push a structured workout (name, steps, targets) to the athlete's Garmin device so it appears in their watch's workout list.  
This is distinct from Intervals.icu — it pushes directly to the device, not the plan calendar.  
**Required for production app** — unofficial path has no write capability.

---

### Phase 6 — Coach admin panel
**Effort:** ~6 hours  
**Depends on:** Phase 1, Phase 2

A separate view (not athlete-facing) that shows all three athletes on one screen:
- Today's sessions, completion status, RPE across all athletes
- Drag-and-drop scheduling across athletes
- Alert triage (watchdog flags, test reminders)
- Link to individual athlete dashboards

Simple authenticated HTML page served by the FastAPI backend. No new framework needed.

---

### Phase 7 — Native app (defer until PWA is limiting)
**Effort:** ~40+ hours  
**Depends on:** All previous phases

Only worth doing if:
- Push notifications via Web Push are unreliable on iOS (Apple restricts these for PWAs)
- Need deep Garmin SDK access (not HTTP API)
- Want App Store distribution

Technology choice when the time comes: React Native (shares JS logic with the PWA) or Flutter (better cross-platform consistency). The FastAPI backend from Phase 1 is already the right API target — nothing changes on the server.

---

## Technology summary

| Layer | Choice | Rationale |
|---|---|---|
| Backend | FastAPI (Python) | Already on VM, team knows Python, fits existing codebase |
| Auth | API key → JWT → passkey over time | Start simple, upgrade without rearchitecting |
| Frontend | Vanilla JS → add fetch/SSE calls | No rewrite needed; add features incrementally |
| PWA | Web standard (manifest + service worker) | Zero cost, works today |
| Chat | SSE streaming from FastAPI | Mirrors bot.py behaviour; no new infrastructure |
| Garmin reads | python-garminconnect (unofficial) | Works today; replace with official SDK if commercialising |
| Garmin writes | Official Developer Programme | Apply when Phase 5b is scheduled |
| Native (future) | React Native or Flutter | Defer until PWA hits a real ceiling |

---

## What to do next

Phases 1 and 2 together unlock the most value for the least effort (~12–13 hours total) and have no dependencies on external approvals. Phase 3 (PWA, ~3 hours) costs little and gives athletes an installable app immediately.

Suggested first sprint: **Phase 1 → Phase 2 → Phase 3** in sequence. That delivers: rescheduling from the dashboard, an installable mobile app, and session editing from the browser — without touching Telegram, without an App Store, and without any new third-party dependencies.
