#!/bin/bash
# Morning briefing — runs via VM crontab at 06:00 daily.
# Pulls live data, sends a personalised card. Never asks for subjective 1-10 inputs.

CLAUDE=$(command -v claude 2>/dev/null); [ -x "$CLAUDE" ] || CLAUDE="/usr/bin/claude"
LOG_DIR="$HOME/Library/Logs/ClaudeCoach"
mkdir -p "$LOG_DIR"

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT=$(cat <<'PROMPT_END'
You are generating the morning briefing for Jamie Diamond's IM Cervia 2026 training day.

Step 1 — Pull data:
- get_athlete_profile (today's date)
- get_wellness (yesterday — sleep_duration, hrv, rhr)
- get_events (today — planned sessions)
- get_fitness (3 days — for current CTL/ATL/TSB)

Step 2 — Read:
- ClaudeCoach/current-state.md (ankle status, watchdog flags, open actions)
- ClaudeCoach/current-state.json (weight_readings — check if any in last 3 days)

Step 3 — Determine the single question to ask (one only, or none):
- If a run is planned today AND ankle.pain_during in current-state.json was >0 last time: ask "Ankle score before heading out?"
- Else if no weight reading in the last 3 days: ask "Weight this morning?"
- Else: no question

Step 4 — Output the morning card in this exact format (Telegram Markdown):

*Good morning — [Day date, e.g. Sat 9 May]*

*Today:* [session name] · [planned TSS] TSS · [duration] min
*TSB:* [value] ([zone: Fresh / Load / Heavy])
*Sleep:* [hours]h · *HRV:* [value] · *RHR:* [value]

[If any watchdog flag is active from current-state.md: ⚠️ [flag name]: [one-line note]]
[If a decision-point action is due within 7 days from current-state.json open_actions: 📌 [action] due [date]]

[Question if applicable — one line]

Rules:
- TSB zone: >+5 = Fresh, 0 to -20 = Load, <-20 = Heavy
- If no planned session: write "Rest day — recovery only"
- If wellness data unavailable: omit that field silently
- No preamble, no sign-off, no rationale. Card only.
- Never ask for subjective 1-10 mood/fatigue/motivation inputs
PROMPT_END
)

TOOLS="Read,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_wellness,mcp__claude_ai_icusync__get_events,mcp__claude_ai_icusync__get_fitness"

OUTPUT=$($CLAUDE -p "$PROMPT" --allowedTools "$TOOLS" 2>>"$LOG_DIR/morning-checkin.log")
trim_log() { local f=$1; tail -n 5000 "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"; }
trim_log "$LOG_DIR/morning-checkin.log"

if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
