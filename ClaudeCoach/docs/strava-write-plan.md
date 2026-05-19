# Strava Write-Back Plan

## Goal
After a completed activity syncs, update the Strava activity description with a concise coaching note — so the record in Strava (and by extension Intervals.icu) carries useful context.

## API
`PUT https://www.strava.com/api/v3/activities/{id}`
- Field: `description` (string)
- OAuth scope: `activity:write`
- Rate limit: 600 req/15min per athlete — no concern
- Library: `stravalib` or direct `requests`

## Auth flow (one-time per athlete)
1. Generate an OAuth URL (scope: `read,activity:read_all,activity:write`)
2. Athlete visits URL in browser, approves
3. Exchange code for `access_token` + `refresh_token`
4. Store tokens in `athletes/{slug}/strava_tokens.json` (gitignored, VM-only)
5. Auto-refresh: Strava tokens expire in 6h — refresh using `refresh_token` before each call

## Where to trigger
**`activity-watcher.py`** — already fires when a new activity appears in Intervals.icu.
Currently it sends a Telegram notification. Add a second step: call Strava API to append
a coaching note to the same activity.

The activity-watcher already has the Intervals.icu activity ID. Need to cross-reference to
the Strava activity ID — available in the Intervals.icu activity detail as `external_id`
or `strava_id` field.

## What to write to the description
Keep it short — Strava descriptions are visible publicly if the activity is public.

Option A — metrics summary (safe, always useful):
```
[ClaudeCoach] TSS 87 · NP 241W · Decoupling 4.2% · Form −12 → −18
```

Option B — coaching note from Claude (richer, requires a short Claude call):
```
[ClaudeCoach] Solid Z2 ride. Decoupling within target. HRV down 8% this week — 
keep tomorrow easy. 122 days to Cervia.
```

Option B is better value but adds ~10s latency to the activity-watcher run. 
Could run async (Popen) so it doesn't block the Telegram notification.

## Implementation steps
1. Add `strava_client_id` + `strava_client_secret` to `config/athletes.json` (or a separate `strava_config.json`)
2. Write `lib/strava_client.py` — token refresh, `get_activity(id)`, `update_description(id, text)`
3. Add a one-time auth helper script: `scripts/strava-auth.py --athlete <slug>`
4. Wire into `activity-watcher.py`: after Telegram send, call `strava_client.update_description()`
5. Gitignore `athletes/*/strava_tokens.json`

## Per-athlete tokens
Each athlete has their own Strava account. Store per athlete:
```json
// athletes/{slug}/strava_tokens.json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1234567890
}
```

## Risks / constraints
- Activity must have already synced to Strava before we can update it — the Intervals.icu 
  activity-watcher fires after Intervals.icu has it, but Intervals.icu syncs FROM Strava, 
  so the Strava activity always exists first. No timing issue.
- If athlete's activity is set to "Everyone" on Strava, the description is public. 
  Keep the note factual, no medical detail.
- Strava requires the OAuth app to be registered at developers.strava.com (free). 
  One app registration covers all athletes.

## Effort estimate
~3–4 hours: auth flow, token refresh, lib, wiring into activity-watcher, testing.
