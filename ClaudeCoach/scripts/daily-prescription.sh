#!/bin/bash
# Daily session prescription — W2. Runs via launchd at 05:00 daily.
# Safe to run manually: bash daily-prescription.sh

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT_FILE=$(mktemp /tmp/claudecoach_prescription.XXXXXX)
trap "rm -f $PROMPT_FILE" EXIT

cat > "$PROMPT_FILE" <<PROMPT_END
You are running the W2 daily session prescription for Jamie Diamond's IM Cervia 2026 coaching system.

Step 1 — Pull live data from IcuSync:
- get_athlete_profile (anchors today's date — use this date for all calculations)
- get_fitness (7 days back to today)
- get_training_history (7 days)
- get_wellness (14 days back)
- get_events (today only — for today's planned session)

Step 2 — Read these files:
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/current-state.md
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/session-log.json (most recent entry = last RPE)

Step 3 — Assemble the readiness dict:
  atl: from get_fitness most recent row
  ctl: from get_fitness most recent row
  hrv_trend_pct: (today HRV - 7d avg HRV) / 7d avg HRV x 100  [if no HRV data, use 0.0]
  sleep_h_last_night: from get_wellness (most recent night)
  last_session_rpe: most recent rpe field in session-log.json (null if empty)
  ankle_pain_score: from current-state.md
  ankle_quality_cleared: from current-state.md (True once 4 consecutive pain-free quality sessions confirmed)
  temp_c: today's forecast ambient temp — use 18.0 as fallback if unavailable
  dew_point_c: today's forecast dew point — use 10.0 as fallback if unavailable

Step 4 — Identify today's planned session from get_events. Map to session_type:
  Threshold/FTP intervals -> bike_threshold
  Z2 / long ride -> bike_z2
  VO2max -> bike_vo2
  Race-pace bike -> bike_race_pace
  Run intervals / tempo -> run_quality
  Easy run / walk-run -> run_easy
  Long run -> run_long
  Brick -> brick
  Swim -> swim
  Gym -> strength
  No session planned -> output "Rest day — no session planned." and stop.

Also extract from the planned session event:
  target_intensity (if not explicit, derive from session type: threshold=1.0, race_pace=0.72, z2=0.65, vo2=1.10)
  interval_count (null if not an interval session)
  interval_duration_min (null if not an interval session)
  recovery_min (null if not an interval session)
  total_duration_min

Step 5 — Call the modulation engine:
  python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/ironman-analysis/scripts/modulate.py '<json with planned and readiness keys>'

Step 6 — If modified or swapped_to_z2: push the adjusted session to Intervals.icu via push_workout (replacing today's planned session).
         If go == false: push a recovery note workout instead with description "BLOCKED: [R1 reason from reasoning trail]".
         If no rules fired: no push needed.

Step 7 — Output the prescription card in exactly this format:

---
**Today: [session name] — [GO / MODIFIED / SWAPPED / BLOCKED]**

| Field | Planned | Prescribed |
|---|---|---|
| Intensity | X% FTP | Y% FTP |
| Intervals | N x M min | N' x M min |
| Recovery | X min | X min |
| Duration | X min | X min |

**Reasoning trail(s):**
- [L2 trail for each fired rule — format: (signal with real number) -> (rule) -> (adjustment) -> (expected effect)]

*[One-sentence summary]*

---

If no rules fired: output "Today: [session name] — execute as planned." and the planned targets only (no reasoning trails section).

Step 8 — Call PushNotification if session was modified, swapped, or blocked. Message under 200 characters:
  "[session name]: [one-line summary of change]"
  Do not call PushNotification if session is unchanged.
PROMPT_END

TOOLS="Read,Bash,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_fitness,mcp__claude_ai_icusync__get_training_history,mcp__claude_ai_icusync__get_wellness,mcp__claude_ai_icusync__get_events,mcp__claude_ai_icusync__push_workout,PushNotification"

OUTPUT=$(/Users/diamondpeakconsulting/.local/bin/claude -p "$(cat "$PROMPT_FILE")" --allowedTools "$TOOLS" 2>>"$HOME/Library/Logs/ClaudeCoach/prescription.log")
echo "$OUTPUT"
if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
