#!/bin/bash
# Evening capture reminder — W3. Fires PushNotification if a key session has no log entry.
# Runs via launchd at 20:00 daily. Safe to run manually: bash capture-reminder.sh

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT_FILE=$(mktemp /tmp/claudecoach_capture.XXXXXX)
trap "rm -f $PROMPT_FILE" EXIT

cat > "$PROMPT_FILE" <<PROMPT_END
You are running the evening session capture reminder for Jamie Diamond's IM Cervia 2026 coaching system. Run silently — only produce output if there is an unlogged key session.

Pull from IcuSync: get_athlete_profile (for today's date), then get_training_history (last 2 days).

Read: /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/session-log.json

Check for completed activities in the last 36 hours that meet ALL of:
1. TSS > 40 OR duration > 45 minutes
2. Sport is Ride, VirtualRide, Run, VirtualRun, Brick, or Swim (skip Strength only)
3. No entry in session-log.json with a matching activity_id

If an unlogged key session is found:
- Call PushNotification with message: "Log [session name] - say 'log session'" (under 200 chars)
- Also print the same message to stdout so it can be forwarded to Telegram

If no unlogged key sessions: output nothing. Do not call PushNotification. Silent run.
PROMPT_END

TOOLS="Read,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_training_history,PushNotification"

OUTPUT=$(/usr/bin/claude -p "$(cat "$PROMPT_FILE")" --allowedTools "$TOOLS" 2>>"$HOME/Library/Logs/ClaudeCoach/capture-reminder.log")
echo "$OUTPUT"
if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
