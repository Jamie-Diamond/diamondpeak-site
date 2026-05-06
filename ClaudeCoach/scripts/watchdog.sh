#!/bin/bash
# Daily watchdog — W4. Fires PushNotification only if a trigger trips.
# Runs via launchd at 07:03 daily. Safe to run manually: bash watchdog.sh

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT=$(cat <<'PROMPT_END'
You are running the daily watchdog check for Jamie Diamond's IM Cervia 2026 coaching system. Run silently — only produce output if a trigger fires.

Read these files:
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/current-state.md
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/reference/rules.md
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/session-log.json
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/heat-log.json

Pull from IcuSync: get_athlete_profile first (for today's date), then get_fitness (14 days), get_training_history (14 days), get_wellness (14 days).

Evaluate these triggers in order:
T1 (Tier 2): ATL > CTL + 25 for 3+ consecutive days
T2 (Tier 2): CTL ramp >4/wk while ankle still in rehab (check current-state.md ankle quality-sessions-resumed field)
T3 (Tier 1): HRV trend down >7% over last 7 days
T4 (Tier 1): Sleep <7h for 3+ days in last 7 (skip if no sleep data available)
T5 (Tier 1): Missed planned sessions ≥2 in last rolling 7 days
T6 (Tier 1): Aerobic decoupling >5% on any Z2 ride in last 7 days (check via get_activity_detail for rides with IF < 0.75)
T7 (Tier 1): From 15 May 2026 only — sum of dose in heat-log.json for last 14 days < 3.0
T8 (Tier 2): From 15 May 2026 only — most recent date in heat-log.json is >7 days ago

If NO triggers fire: output nothing. Do not call PushNotification. Silent run.

If ANY trigger fires: call PushNotification once with a message under 200 characters.
- Tier 2 format: "⚠ [trigger]: [one-line action required]"
- Tier 1 format: "ℹ [trigger]: [one-line note]"
- Multiple triggers: list all names, lead with the highest tier.
PROMPT_END
)

TOOLS="Read,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_fitness,mcp__claude_ai_icusync__get_training_history,mcp__claude_ai_icusync__get_wellness,mcp__claude_ai_icusync__get_activity_detail,PushNotification"

/Users/diamondpeakconsulting/.local/bin/claude -p "$PROMPT" --allowedTools "$TOOLS" 2>>"$HOME/Library/Logs/ClaudeCoach/watchdog.log"
