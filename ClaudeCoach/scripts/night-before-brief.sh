#!/bin/bash
# Night-before brief — runs via VM crontab at 20:30 daily.
# If tomorrow has a key session, sends a tailored brief: targets, nutrition, sleep goal.
# Silent on rest days.

CLAUDE=/usr/bin/claude
LOG_DIR="$HOME/Library/Logs/ClaudeCoach"
mkdir -p "$LOG_DIR"

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT=$(cat <<'PROMPT_END'
You are generating the night-before session brief for Jamie Diamond's IM Cervia 2026 coaching.

Step 1 — Pull data:
- get_athlete_profile (establishes today's date; tomorrow = today + 1 day)
- get_events (tomorrow's date only)
- get_fitness (last 3 days — for current TSB)
- get_wellness (last 1 day — latest HRV)

Step 2 — Read ClaudeCoach/athletes/jamie/current-state.json (ankle pain score).

Step 3 — Decide whether to send a message:
- If no events tomorrow, OR only events with TSS < 30 AND duration < 40 min: output nothing. Silent.
- Otherwise: proceed to Step 4.

Step 4 — Output the night-before brief in Telegram Markdown (no preamble, no sign-off):

*Tomorrow — [session name]*

[Specific targets — 2-4 bullets based on sport:]
Ride: • NP target [X W] (IF [X.XX]) • HR cap [X bpm] • [interval structure if structured]
Run: • Target GAP [X:XX/km] • HR cap [X bpm] (9:1 walk-run — ankle protocol)
Swim: • Target pace [X:XX/100m] vs CSS 1:39 • Main set structure
Strength: • Main focus • Key movements

*Nutrition:* [g carbs/hr] g/hr + [ml] ml/hr fluid — calibrated to session length and intensity. Zero if easy/recovery.
*Sleep:* ≥8h tonight
*Form:* TSB [value] ([Fresh / Load / Heavy])

[If ankle.pain_during >= 3 in current-state.json AND a run is planned: add one line "⚠️ Ankle check before starting — note score after."]

Rules:
- Jamie: FTP 316 W, run threshold 4:02/km, swim CSS 1:39/100m.
- Ankle still in rehab — runs are 9:1 walk-run only. No quality run sessions yet.
- Keep the entire brief under 120 words.
- Never ask questions. Brief only.
PROMPT_END
)

TOOLS="Read,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_events,mcp__claude_ai_icusync__get_fitness,mcp__claude_ai_icusync__get_wellness"

OUTPUT=$($CLAUDE -p "$PROMPT" --allowedTools "$TOOLS" 2>>"$LOG_DIR/night-before-brief.log")
trim_log() { local f=$1; tail -n 5000 "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"; }
trim_log "$LOG_DIR/night-before-brief.log"

if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
