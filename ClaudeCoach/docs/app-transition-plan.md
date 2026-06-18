# ClaudeCoach — Transition to Full Web App

**Written:** 2026-05-27 · **Revised:** 2026-06-18
**Status:** Phase 0 in progress — shared engine extracted (`lib/engine.py`, commit 8f1749c, live + verified). Next: FastAPI skeleton + CF Tunnel/Access, then SPA scaffold.

> ### What changed in the 2026-06-18 revision (read first)
> The 27 May v1 is superseded on four points, all decided/validated this session:
> - **Garmin integration is no longer Phase 1 — it is dropped from the critical path.** Research (18 Jun) found the official Garmin Developer Programme is closed to new applicants and enterprise-only; the unofficial `python-garminconnect` route means storing each athlete's Garmin credentials (a privacy/ToS liability for a multi-athlete product) and is fragile. Crucially, **the HRV/sleep data we wanted from Garmin was already in intervals.icu all along** — the daily "HRV null / sleep null" was a 05:00 prescription timing bug (reading before the watch synced), now fixed. The marquee Garmin metrics (Training Readiness/Status, HRV-status, stress) are not in any API anyway. So direct-Garmin adds almost nothing; revisit only if run biomechanics (GCT/vertical oscillation) become a priority, and then via an aggregator (Terra) with proper OAuth — never stored credentials.
> - **Auth = Cloudflare Access** (was "API key → JWT → passkey"). Already in the stack across other hosts; email allow-list; no passwords to own.
> - **Frontend = React + Vite SPA** (was "vanilla JS, no rewrite"). Decision: a real SPA for the rich interactions wanted (drag-and-drop calendar, streaming chat). React+Vite matches the existing CdA calculator app in this repo.
> - **Telegram is demoted to notifications + quick-logging only** (was "parallel channel, full parity indefinitely"). Web becomes the primary surface.
> - **Domain:** `coach.diamondpeak.uk`.

---

## Where we are now

| Component | Current implementation |
|---|---|
| AI coaching | Telegram bot (Python, Claude CLI) on the VM — chat, voice, morning cards, activity analysis, logging |
| Dashboards | Static HTML on GitHub Pages (`diamondpeak.uk`), fed a nightly **public subset** (`training-data.json`) |
| Scheduling | Intervals.icu directly; no write-back from the web |
| Wellness data | In intervals.icu daily (HRV/sleep/RHR/sleepScore/VO2max/weight/bodyFat); morning timing bug fixed 18 Jun |
| Auth | None on the website (only a curated public subset is exposed); Telegram identity for the bot |
| Athletes | Jamie, Kathryn, Calum — same VM |

**Core constraint (unchanged):** the website is fully static (GitHub Pages), so the browser has no server to talk to. Every interactive feature — chat, logging, drag-and-drop rescheduling — requires a backend.

## Decisions (locked 2026-06-18)

| Decision | Choice |
|---|---|
| Coaching "brain" | **Reuse the existing VM Python engine** behind an HTTP API — no rewrite |
| Backend | **FastAPI** on the VM, alongside `claudecoach-bot.service` |
| Auth | **Cloudflare Access** (email allow-list) via a CF Tunnel |
| Frontend | **React + Vite SPA**, mobile-first PWA, rich interactions (drag-and-drop) |
| Domain | **coach.diamondpeak.uk** |
| Telegram | **Notifications + quick-logging only** once web is primary |
| Notifications | **Telegram only for now** (no web push yet) |

## Target architecture

```
Browser (React+Vite SPA / PWA)  ──HTTPS──►  Cloudflare Tunnel + Access (email gate)
                                                   │
                                            FastAPI on the VM
                                                   │  (serves the SPA build + the API)
                          ┌────────────────────────┼────────────────────────┐
                     shared engine             ICU / Strava              athlete data
                   (call_claude, tools,        (icu_api, fetch,          (session-log,
                    plan_tools, prompts)         write-back)              current-state…)
                                                   ▲
                                        Telegram bot (same engine)
                                        push + quick voice/RPE only
```

**Keystone — the shared engine.** Extract the coaching engine out of `bot.py` into an importable module that *both* the Telegram bot and the FastAPI API call. This is what prevents two divergent brains. Everything else depends on it; it is Phase 0 and must land before any browser feature, with the bot kept working off it throughout (no big-bang).

---

## Phases

### Phase 0 — Foundations (the enabler)
- **[DONE 2026-06-18, commit 8f1749c]** Extract the engine from `bot.py` into `lib/engine.py`: prompt assembly (`build_prompt`, `system_prompt_with_level`, `load_persistent_rules`, `render_history`), `call_claude`, `call_claude_with_image`, and a transport-agnostic `stream_claude` generator yielding `('chunk'|'final', text)`. Bot repointed; `call_claude_streaming` is now a thin Telegram wrapper over `stream_claude`. Behaviour-neutral, verified live. (Follow-up: DRY the duplicated `CLAUDE_BIN`/`TOOLS`/`MODEL_*` constants once the API exists; extract session-logging + ICU read/write into the engine too.)
- Stand up FastAPI on the VM (systemd service beside the bot), behind a **Cloudflare Tunnel + Access** at `coach.diamondpeak.uk`. Map CF Access email → athlete slug.
- Scaffold the React + Vite SPA; FastAPI serves the built static assets (one origin, no CORS).

### Phase 1 — Live, auth'd dashboards
- SPA renders each athlete's **full private** dashboards from the API (fitness/form/load/recovery/durability/compliance, plan, current state) — live, not the nightly public subset.
- Replaces the read-only GitHub Pages dashboards. Mobile-first layout from day one.

### Phase 2 — Web chat (headline: the main interface moves)
- Streaming chat UI (SSE) → `/chat` → shared engine, full tool + plan_tools capability. Markdown rendering, image upload, shared history with Telegram.
- This is the point where "the main interface" is on the web.

### Phase 3 — Interactive calendar + actions
- Drag-and-drop session rescheduling on a calendar view → Intervals.icu `edit_workout` write-back; optimistic re-render; past-date guard.
- Session logging/actions (RPE, pain, weight, nutrition, debrief, replan, push-workout) as web flows — the Telegram buttons, on the web.

### Phase 4 — Demote Telegram + PWA polish
- Telegram becomes push (morning card, activity alerts, PRs) + on-the-go quick-log/voice, off the shared engine.
- PWA shell: `manifest.json` + service worker, installable on the phone home screen, offline shell + last-fetched data. Mobile-first throughout (web is replacing the phone touchpoint).
- Notifications stay on Telegram (no web push yet, per decision).

### Phase 5 — Later / optional
- **Coach admin panel:** all athletes on one screen (today's sessions, completion, RPE, alert triage), cross-athlete drag-and-drop scheduling.
- **Native app:** only if the PWA hits a real ceiling (e.g. iOS push limits). React Native shares JS with the SPA; the FastAPI API is already the right target.

---

## Technology summary

| Layer | Choice | Rationale |
|---|---|---|
| Backend | FastAPI (Python) on the VM | Reuses the proven engine; team knows Python |
| Shared engine | `lib/engine.py` imported by both bot + API | One brain, no divergence |
| Auth | Cloudflare Access (email) via CF Tunnel | Already in stack; no password ownership |
| Frontend | React + Vite SPA, PWA | Rich interactions (drag-and-drop, streaming); matches existing CdA app |
| Chat | SSE streaming from FastAPI | Mirrors bot streaming; no new infra |
| Calendar write-back | `IcuClient.edit_workout` | Already implemented in the engine |
| Garmin | **Deferred / dropped** | Data already in ICU; official API closed; unofficial = liability. Aggregator (Terra) only if run dynamics needed later |
| Native (future) | React Native | Defer until PWA ceiling |

## Risks / watch-items
- **4GB VM:** FastAPI + uvicorn + bot + crons + Whisper + Piper. Should fit; keep worker count low and memory-watch.
- **Engine refactor is make-or-break** — do it first, keep the bot working off it the whole time.
- **Mobile-first is mandatory** — the SPA replaces the phone touchpoint, not a desktop dashboard.
- **CF Access email → slug** mapping must be solid (wrong mapping = wrong athlete's data).

## Next step
Phase 0, starting with the **engine extraction** (`bot.py` → `lib/engine.py`, repoint the bot, verify behaviour-neutral). It is the foundation everything else builds on and carries no behaviour change. Then FastAPI skeleton + CF Tunnel/Access + SPA scaffold.
