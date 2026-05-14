#!/bin/bash
# Rolling plan generator — runs via VM crontab at 21:00 every Sunday (after weekly-summary.sh).
# Fills the next 2 weeks in Intervals.icu if fewer than 3 events exist in that window.
# Safe to run manually: bash generate-plan.sh

CLAUDE=/usr/bin/claude
LOG_DIR="$HOME/Library/Logs/ClaudeCoach"
mkdir -p "$LOG_DIR"

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT_FILE=$(mktemp /tmp/claudecoach_plan.XXXXXX)
trap "rm -f $PROMPT_FILE" EXIT

cat > "$PROMPT_FILE" <<'PROMPT_END'
You are generating the rolling 2-week training plan for Jamie Diamond's IM Cervia 2026 coaching system.

Step 1 — Pull live data:
- get_athlete_profile (today's date, FTP, athlete timezone)
- get_fitness (oldest=today-14, newest=today+35) — 14 days history + 35 days forward projection
- get_wellness (14 days — sleep, HRV)
- get_training_history (14 days — what was actually done)
- get_events (start_date=today, end_date=<today+35 days>) — full 5-week window: existing plan + upcoming races/constraints

Step 2 — Read:
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/athletes/jamie/current-state.md (ankle, niggles, open actions)
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/athletes/jamie/current-state.json (ankle pain scores, weight)
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/athletes/jamie/reference/rules.md (HARD CONSTRAINTS — read fully)
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/athletes/jamie/reference/decision-points.md (upcoming forks)
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/athletes/jamie/session-log.json — extract all Ride/GravelRide/Brick entries with duration_min ≥ 90 and nutrition_g_carb set. Compute g_per_hr = nutrition_g_carb / duration_min * 60 for each. Store as nutrition_history list (most recent first).

From nutrition_history compute:
  nutrition_avg_g_hr = mean of all g_per_hr values (null if no entries)
  nutrition_target_g_hr = min(round(nutrition_avg_g_hr + 10, -1), 90) if avg exists, else 60
  (This sets a progressive target 10g/hr above current avg, capped at 90g/hr race target)

Step 3 — Determine the planning window:
- Target: the 2 weeks starting NEXT Monday (not today).
- Check get_events for that window. If there are already 7+ events planned: output "Plan already populated for [date range] — skipping." and stop.
- If <7 events: generate enough sessions to fill the week appropriately.

Step 3b — Trajectory check (use get_fitness forward projection):
- ctl_today = today's CTL value from get_fitness
- ctl_end_wk2 = projected CTL on the last day of the 2-week planning window (from get_fitness forward data — this is the passive decay baseline assuming no new training is added)
- Phase-end CTL blueprint milestones:
    End of Base     (week 6):  ≥75 CTL
    End of Build    (week 10): ≥85 CTL
    End of Specific (week 14): ≥95 CTL
    End of Peak     (week 17): ≥100 CTL
- weeks_to_phase_end = phase_end_week − current_week_number (compute once week number is known in Step 4; run Step 3b logic after Step 4 week-number calc if needed)
- required_weekly_gain = (target_ctl_phase_end − ctl_today) / max(weeks_to_phase_end, 1)
- Set trajectory_status:
    BEHIND   if required_weekly_gain > 3.0  → use TOP 20% of phase TSS range
    ON_TRACK if 1.5 ≤ required_weekly_gain ≤ 3.0 → use MIDDLE of phase TSS range
    AHEAD    if required_weekly_gain < 1.5  → use LOWER 20% of phase TSS range
- Race / key-event check: scan get_events results for days 15–28 from next Monday (weeks 3–4 of the 5-week window). If any event has type "Race" or priority "A" or "B":
    → set pre_event_taper = true: cap WEEK 2 TSS at BOTTOM of phase range regardless of trajectory_status
    → store event_name and event_date for use in Step 8 output

Step 4 — Determine phase and TSS target:
- Week number = ceil((Monday date − 2026-04-27) / 7)
- Phase and TSS ranges:
  Week 1–6   (Base):     350–500 TSS/wk, focus Z2 bike volume + aerobic swim + easy run
  Week 7–10  (Build):    450–600 TSS/wk, add threshold bike work, extend long run
  Week 11–14 (Specific): 550–720 TSS/wk, race-pace intervals, brick sessions
  Week 15–17 (Peak):     650–800 TSS/wk, race simulation, consolidate fitness
  Week 18–21 (Taper):    200–350 TSS/wk, sharpen, no new stimuli
- Apply trajectory_status from Step 3b to select the TSS target within the range:
    BEHIND   → target TOP 20% of range (e.g. Base: ~480 TSS)
    ON_TRACK → target MIDDLE of range  (e.g. Base: ~425 TSS)
    AHEAD    → target LOWER 20% of range (e.g. Base: ~370 TSS)
- If pre_event_taper = true: week 1 stays at trajectory_status target; week 2 is overridden to BOTTOM of range.
- CTL ramp cap (see Step 5) is a hard ceiling: if ankle rehab active, apply whichever limit is lower — trajectory target or ramp cap.

Step 5 — Apply mandatory constraints (from rules.md — these are HARD overrides):
- Ankle: no quality run sessions (intervals/tempo/race-pace) until current-state.json ankle.four_pain_free_weeks_reached = true. Use 5:30 run-walk format only (Z2 HR cap 150). Weekly run km increase ≤10%.
- CTL ramp: ≤+4 CTL/wk while ankle in rehab. Calculate expected weekly TSS that would produce this; cap the week total accordingly.
- Pre-event fatigue management: if pre_event_taper = true (race or A/B event in days 15–28): in week 2 avoid all intensity sessions, prioritise swim + short Z2 rides only, no new stimulus. State "[event_name] on [event_date] — week 2 is a lead-in week" in each week-2 session description.
- Strength: minimum 1 session/week (target 2). Jamie is currently at 0 sessions/week — make strength a priority.
- Never prescribe new fuel/kit/shoes in the last 4 weeks.
- Always state day-of-week alongside date in session names.
- Travel / access constraints: scan current-state.md "Travel & training blocks" table for any dates within the planning window where bike is unavailable. For those dates: substitute rides with swims or ankle-protocol runs of equivalent TSS. Flag in Step 8 output.
- Upcoming constraints (next 28 days beyond the planning window): note in session descriptions if a key test, race, or travel block is approaching — e.g. "Last long ride before travel block (18 May — no bike for 5 days)".

Step 6 — Build the 2-week session structure:
Standard week template (adapt to phase):
- Monday: Rest or recovery swim
- Tuesday: Run (Z2, 5:30 walk-run) + optional swim
- Wednesday: Bike Z2 (60–90 min) or strength
- Thursday: Swim (CSS-based) + optional short run
- Friday: Long ride (Z2 NP target) — key session
- Saturday: Brick (ride + run) or long run
- Sunday: Rest or short active recovery

Session description consistency rules (HARD — check before every push_workout):
- Never combine a fixed-distance label (e.g. "5k") with a fixed-duration label (e.g. "50 min") unless they are provably equivalent. At 5:30/km walk-run pace: 5k = 27.5 min, NOT 50 min. If they conflict, use duration only.
- Walk-run interval counts must match the stated duration: for 9:1 format (10 min/cycle), 50 min = 5 cycles. Always verify N × cycle_length ≈ total_min before writing the name or description.
- State distance OR duration in the session name, not both, unless both are internally consistent and you have verified the arithmetic.

For each session create a push_workout call with:
  sport: "Ride" | "Run" | "Swim" | "WeightTraining"
  date: YYYY-MM-DD (must be a specific date in the planning window)
  name: "[Day date] — [session description]" e.g. "Tue 12 May — Z2 run 50 min (5x 9:1)"
  description: full coaching notes including:
    - Target zones, paces, or power ranges
    - Duration and structure
    - Nutrition instructions for ALL sessions >90 min: state the specific nutrition_target_g_hr computed above (e.g. "Target: 75g CHO/hr — up from your recent avg of 65g/hr. Eat at 15 min then every 25 min."). If nutrition_avg_g_hr is null: "Target: 60g CHO/hr — start building gut training. Eat at 20 min then every 30 min."
    - Ankle protocol reminder on all run sessions
    - One sentence rationale (physiological adaptation or risk being mitigated)
  planned_training_load: estimated TSS integer

Step 7 — Call push_workout for each planned session. Do NOT overwrite sessions that already exist in get_events for those dates — check date+sport before pushing.

Step 8 — Output summary:
"Plan generated: [date range]
Week [N] ([phase]): [N sessions] · [total Load] planned
Week [N+1] ([phase]): [N sessions] · [total Load] planned
Fitness: [X] today → target [Y] by end of [phase] (wk [Z]) · status: [BEHIND / ON_TRACK / AHEAD] · Load target: [position in range]
[If pre_event_taper: 📌 Pre-event taper — [event_name] on [event_date]: week 2 capped at [Load]]
Key constraints applied: [list any ankle/ramp/strength rules that shaped the plan]"

Step 9 — Notify via Telegram:
Run: python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py "Plan generated [date range]: W[N] [X Load] + W[N+1] [Y Load]. [Any key constraint note]"

Step 10 — Update current-state.md "Open actions" section: mark "Plan generated through [date]" with today's date.
Run: git add ClaudeCoach/athletes/jamie/current-state.md && git fetch origin && git rebase --autostash origin/main && git commit -m "plan: generated W[N]-W[N+1] [date]" && git push origin main
PROMPT_END

TOOLS="Read,Write,Edit,Bash,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_fitness,mcp__claude_ai_icusync__get_wellness,mcp__claude_ai_icusync__get_training_history,mcp__claude_ai_icusync__get_events,mcp__claude_ai_icusync__push_workout,mcp__claude_ai_icusync__edit_workout"

OUTPUT=$($CLAUDE -p "$(cat "$PROMPT_FILE")" --allowedTools "$TOOLS" --model claude-sonnet-4-6 2>>"$LOG_DIR/generate-plan.log")
trim_log() { local f=$1; tail -n 5000 "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"; }
trim_log "$LOG_DIR/generate-plan.log"
echo "$OUTPUT"
if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
