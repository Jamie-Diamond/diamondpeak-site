#!/bin/bash
# Evening capture reminder — W3. Fires PushNotification if a key session has no log entry.
# Runs via launchd at 18:07 daily. Safe to run manually: bash capture-reminder.sh

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT=$(cat <<'PROMPT_END'
You are running the evening session capture reminder for Jamie Diamond's IM Cervia 2026 coaching system. Run silently — only produce output if there is an unlogged key session.

Pull from IcuSync: get_athlete_profile (for today's date), then get_training_history (last 2 days).

Read: /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/session-log.json

Check for completed activities in the last 36 hours that meet ALL of:
1. TSS > 40 OR duration > 45 minutes
2. Sport is Ride, VirtualRide, Run, VirtualRun, or Brick (skip Swim and Strength)
3. No entry in session-log.json with a matching activity_id

If an unlogged key session is found: call PushNotification with message:
"Log [session name] — open Claude and say 'log session'"
Under 200 characters.

If no unlogged key sessions: output nothing. Do not call PushNotification. Silent run.
PROMPT_END
)

TOOLS="Read,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_training_history,PushNotification"

/Users/diamondpeakconsulting/.local/bin/claude -p "$PROMPT" --allowedTools "$TOOLS" 2>>"$HOME/Library/Logs/ClaudeCoach/capture-reminder.log"
