# ClaudeCoach MCP Server — Design Plan

## Problem

The IcuSync MCP connector (`mcp__claude_ai_icusync__*`) is a claude.ai account-level integration tied to a single athlete (Jamie). It cannot be used for Kathryn or any future athletes. The current workaround — calling `icu_fetch.py` via Bash subprocess in every prompt — is fragile and produces noisy, boilerplate-heavy prompts.

## Solution

Build a local MCP server on the VM that wraps `ClaudeCoach/lib/icu_api.py`. Each tool accepts an `athlete_slug` parameter and looks up credentials from `config/athletes.json`. Any number of athletes. No per-athlete MCP instances.

---

## Architecture

**Transport:** stdio (standard MCP pattern — Claude CLI starts the server process per-invocation, no persistent daemon needed)

**Language:** Python, using the `mcp` package (`pip install mcp`)

**Location on VM:** `ClaudeCoach/mcp/server.py`

**Claude CLI config:** add to `/root/.claude.json` (VM global):
```json
{
  "mcpServers": {
    "claudecoach": {
      "command": "python3",
      "args": ["/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/mcp/server.py"]
    }
  }
}
```

All cron scripts already use `CLAUDE = "/usr/bin/claude"` — they'll pick up the MCP server automatically once it's in the global config. No per-script changes to the subprocess call.

---

## Tools to expose

Each tool mirrors an `IcuClient` method and adds `athlete_slug: str` as the first parameter.

| Tool name | Maps to | Key params |
|---|---|---|
| `get_athlete_profile` | `client.get_athlete_profile()` | `athlete_slug` |
| `get_wellness` | `client.get_wellness(days)` | `athlete_slug`, `days` |
| `get_training_history` | `client.get_training_history(days)` | `athlete_slug`, `days`, `sport?` |
| `get_activity_detail` | `client.get_activity_detail(id)` | `athlete_slug`, `activity_id` |
| `get_extended_metrics` | `client.get_extended_metrics(id)` | `athlete_slug`, `activity_id` |
| `get_events` | `client.get_events(start, end)` | `athlete_slug`, `start`, `end` |
| `get_fitness` | `client.get_fitness(days)` | `athlete_slug`, `days`, `newest?` |
| `get_best_efforts` | `client.get_best_efforts(sport)` | `athlete_slug`, `sport`, `period?` |
| `push_workout` | `client.push_workout(...)` | `athlete_slug`, workout fields |
| `edit_workout` | `client.edit_workout(id, ...)` | `athlete_slug`, `event_id`, fields |
| `delete_workout` | `client.delete_workout(id)` | `athlete_slug`, `event_id` |

---

## Prompt migration

Once the server is live, strip all Bash fetch boilerplate from script prompts and replace with native tool calls.

**Before (activity-watcher):**
```
Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 3
```

**After:**
```
Step 1 — Fetch data:
  Call get_athlete_profile(athlete_slug="{slug}")
  Call get_training_history(athlete_slug="{slug}", days=3)
```

Scripts affected: `activity-watcher.py`, `morning-checkin.py`, `evening-checkin.py`,
`night-before-brief.py`, `capture-reminder.py`, `weekly-summary.py`.

The `--allowedTools` arg in each script changes from `"Read,Bash"` to
`"Read,Write,Bash,mcp__claudecoach__get_athlete_profile,mcp__claudecoach__get_wellness,..."`.
Or use a wildcard if the CLI supports it.

---

## Work breakdown

| Task | Effort |
|---|---|
| Write `ClaudeCoach/mcp/server.py` (~200 lines) | 2–3 hr |
| Install `mcp` package on VM, configure `~/.claude.json` | 30 min |
| Migrate prompts in all 6 scripts | 2 hr |
| End-to-end test (run activity-watcher manually on VM) | 1 hr |
| **Total** | **~half a day** |

---

## Notes / decisions to make before starting

1. **`--allowedTools` wildcard:** check if `claude -p` supports `mcp__claudecoach__*` glob or if every tool name must be listed explicitly. If explicit, build a shared `TOOLS` constant to import.
2. **Interactive session:** once the VM MCP server exists, consider whether to also wire it into the Mac-side interactive claude.ai session (replacing the IcuSync connector). This would unify the tool interface across both runtimes. Not required for Phase 1.
3. **`icu_fetch.py`:** can be kept as-is for manual/debugging use. The MCP server is a parallel path, not a replacement.

---

*Saved 2026-05-13. Pick up when coaching system is stable and the Bash-fetch friction becomes the bottleneck.*
