# ClaudeCoach — model-limit resilience plan

**Created:** 2026-06-17 · **Trigger:** the morning cron jobs went silent and the
interactive bot appeared to "time out".

## Root cause (confirmed)

The bot and every cron script shell out to the same `claude` CLI, as the **same
root user, same OAuth subscription** (verified: no per-script API key, same `cwd`,
same `~/.claude`). Usage on the Max (5×) plan is **metered per tier**:

| Meter | State when this broke |
|---|---|
| Session (5-hr rolling) | 10% — fine |
| Weekly, **all models** | 57% — headroom (this is the pool Opus draws on) |
| Weekly, **Sonnet only** | **100% — maxed** (resets Fri ~13:00) |

The Sonnet-only weekly bucket is small and exhausted long before the shared
all-models pool. So:

- **Interactive chat (Opus)** kept working — draws on the half-full all-models pool.
- **Every Sonnet cron job** (`morning-checkin`, `daily-prescription`,
  `session-sync`, `watchdog`, `night-before-brief`, `weekly-summary`,
  `stage1-plan`, the `activity-watcher` main call) hit the wall and produced
  nothing. The bot relays Claude's stdout, so a capped reply just *looks* like a
  short/odd answer; cron scripts bin it silently.

Secondary, transient, already self-resolved: a Telegram `502`/Anthropic `529
Overloaded` burst the previous evening + a streaming `editMessageText` 400 bug
that made replies look frozen.

## Model inventory (which script uses which model)

| Script | Cadence | Model | On maxed Sonnet bucket |
|---|---|---|---|
| daily-prescription.py | 05:00 | Sonnet | 🔴 |
| watchdog.py | 05:30 | Sonnet | 🔴 |
| morning-checkin.py | every 15m, 06–09 | Sonnet | 🔴 |
| session-sync.py | every 2h, 07–22 | Sonnet | 🔴 |
| night-before-brief.py | 20:30 | Sonnet | 🔴 |
| weekly-summary.py | Sun 20:00 | Sonnet | 🔴 |
| stage1-plan.py | Sun 18:00 | Sonnet (default) | 🔴 |
| activity-watcher.py | every 10m | Haiku ×2 + **Sonnet ×1** | 🟠 |
| evening-checkin.py | 21:00 | Haiku | 🟢 |
| capture-reminder.py | 20:10 | Haiku | 🟢 |
| strava-update-activity.py | event | Haiku | 🟢 |
| refresh-site-data / telegram-feedback / backfill-session-log | — | none → CLI default | ⚠️ pin |
| bot.py chat | interactive | **Opus** | 🟢 |
| bot.py WebSearch helper | interactive | Sonnet | 🔴 |

## Design constraints

- **Do not fall back to Opus for frequent jobs** — Opus draws on the all-models
  pool the interactive bot depends on; draining it would kill chat too.
- Cheap/frequent jobs fall back to **Haiku** (also all-models, but tiny cost).
- Quality-critical, low-frequency jobs (`daily-prescription`, `stage1-plan`,
  morning/night briefs) may fall back to **Opus** — a handful of calls/day is
  negligible against 43% headroom.

## Workstreams

### WS1 — cut the Sonnet burn + on-demand pull
- `activity-watcher.py`: **Python-first gate** — a cheap `icu_fetch.py history`
  check decides whether any activity is missing from `session-log.json`; only
  then is the LLM invoked. Fail-open (run the LLM) on any fetch error so we never
  miss an activity. Turns ~144 LLM runs/day into only-on-new-activity.
- Add `--athlete <slug>` to `activity-watcher.py` for single-athlete on-demand runs.
- Cron: `activity-watcher` **10m → 5m** (cheap now it's Python-gated);
  `morning-checkin` **15m → 30m**.
- Bot: **"Check for activity"** inline button → runs `activity-watcher --athlete`
  for that chat. Lets the user pull instead of relying on polling.

### WS2 — model fallback (`lib/claude_call.py`)
- One `run_claude(prompt, model, fallback=[...])` that detects the
  "hit your limit"/overload output and retries down a configured chain.
- Default chains: `Sonnet→Haiku`, `Haiku→Sonnet`, `Opus→Sonnet→Haiku`.
  Quality-critical callers pass `fallback=[OPUS]` explicitly.
- Returns the model that actually answered + `fell_back`/`limited` flags.
- Migrate the 🔴 cron scripts first (this also **unblocks them today** despite the
  maxed Sonnet bucket), then `bot.py`, then the rest. Pin the ⚠️ "no `--model`"
  scripts to an explicit model.

### WS3 — visibility
- `claude_call` soft-imports `ops_log` and records a fallback / alerts when a tier
  (or all tiers) is capped — so a silent cap can't bite again.
- (Programmatic per-tier usage % isn't exposed by the CLI; the fallback signal is
  the practical substitute for a "Sonnet 100%" warning.)

## Deploy
Edit on Mac → commit → push → `git pull` on VM → update VM crontab →
`systemctl restart claudecoach-bot` → verify. Crontab changes only via the VM
crontab (never CronCreate — see CLAUDE.md).
