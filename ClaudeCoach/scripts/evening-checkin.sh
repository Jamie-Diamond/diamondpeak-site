#!/bin/bash
# Evening check-in — runs via VM crontab at 21:00 daily.
# Compares today's completed activities to the plan; asks a specific question if needed.
# Silent if everything is already logged.

CLAUDE=/usr/bin/claude
LOG_DIR="$HOME/Library/Logs/ClaudeCoach"
mkdir -p "$LOG_DIR"

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT=$(cat <<'PROMPT_END'
Evening check for Jamie Diamond's IM Cervia 2026 training log.

Step 1 — Pull data:
- get_athlete_profile (today's date)
- get_training_history (today only — completed activities)
- get_events (today only — planned sessions)

Step 2 — Read ClaudeCoach/athletes/jamie/session-log.json (check which activity_ids are already stubbed).

Step 3 — Decide whether to send a message:

Case A — A completed activity exists that is NOT yet in session-log.json:
  Send one specific question about it. Examples:
  - Run: "Good [X km] run done. Ankle score during and this morning?"
  - Ride (>90 min): "Solid [X km] ride done. Nutrition — roughly g carbs/hr and bottles?"
  - Swim: "Swim done — [X m] at [pace]. RPE and how did it feel?"
  - Strength: "Strength session done. RPE and main focus?"
  Max 2 sentences. No preamble.

Case B — A planned session has NO matching completed activity AND it's after 19:00:
  Send one line: "Did the [session name] happen today?"

Case C — All planned sessions are accounted for in training_history, and all are already stubbed:
  Output nothing. Do not send any message.

Case D — No planned sessions and no activities: output nothing.

Priority: Case A > Case B > silence.
Only ever send ONE message. Never ask multiple questions.
PROMPT_END
)

TOOLS="Read,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_training_history,mcp__claude_ai_icusync__get_events"

OUTPUT=$($CLAUDE -p "$PROMPT" --allowedTools "$TOOLS" 2>>"$LOG_DIR/evening-checkin.log")
trim_log() { local f=$1; tail -n 5000 "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"; }
trim_log "$LOG_DIR/evening-checkin.log"

if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
