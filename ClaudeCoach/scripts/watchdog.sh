#!/bin/bash
# Daily watchdog - W4. Fires PushNotification only if a trigger trips.
# Runs via launchd at 05:30 daily. Safe to run manually: bash watchdog.sh

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT_FILE=$(mktemp /tmp/claudecoach_watchdog.XXXXXX)
trap "rm -f $PROMPT_FILE" EXIT

cat > "$PROMPT_FILE" <<PROMPT_END
You are running the daily watchdog check for Jamie Diamond's IM Cervia 2026 coaching system. Run silently - only produce output if a trigger fires.

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
T5 (Tier 1): Missed planned sessions >=2 in last rolling 7 days
T6 (Tier 1): Aerobic decoupling >5% on any Z2 ride in last 7 days (check via get_activity_detail for rides with IF < 0.75)
T7 (Tier 1): From 15 May 2026 only - sum of dose in heat-log.json for last 14 days < 3.0
T8 (Tier 2): From 15 May 2026 only - most recent date in heat-log.json is >7 days ago

If NO triggers fire: output nothing. Do not call PushNotification. Silent run.

If ANY trigger fires:
1. Call PushNotification once, under 200 characters: "warning [trigger]: [action]" (Tier 2) or "info [trigger]: [note]" (Tier 1). Multiple triggers: list names, lead with highest tier.
2. Update current-state.md — append to the relevant section (ankle, niggles, off-plan, or add a "## Watchdog flags" section if needed) with today's date and the trigger name + signal value. Do not rewrite sections that don't need updating.
3. Run: git add ClaudeCoach/current-state.md && git pull --rebase origin main && git commit -m "watchdog: [trigger list] [date]" && git push origin main
4. Output one L2 reasoning trail per trigger to stdout (written to log):
   [signal with real number] -> [rule: T1-T8] -> [suggested adjustment] -> [expected effect]
   Example: "ATL 148 vs CTL 121 for 4 days -> T1 (ATL > CTL +25) -> insert recovery day, drop Thursday quality to Z2 -> TSB recovers ~8 pts by weekend"
PROMPT_END

TOOLS="Read,Write,Edit,Bash,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_fitness,mcp__claude_ai_icusync__get_training_history,mcp__claude_ai_icusync__get_wellness,mcp__claude_ai_icusync__get_activity_detail,PushNotification"

OUTPUT=$(/Users/diamondpeakconsulting/.local/bin/claude -p "$(cat "$PROMPT_FILE")" --allowedTools "$TOOLS" 2>>"$HOME/Library/Logs/ClaudeCoach/watchdog.log")
echo "$OUTPUT"
if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
