#!/bin/bash
# Rolling plan generator — runs via VM crontab at 21:00 every Sunday (after weekly-summary.sh).
# Fills the next 2 weeks in Intervals.icu if fewer than 3 events exist in that window.
# Safe to run manually: bash generate-plan.sh

CLAUDE=$(command -v claude 2>/dev/null); [ -x "$CLAUDE" ] || CLAUDE="/usr/bin/claude"
LOG_DIR="$HOME/Library/Logs/ClaudeCoach"
mkdir -p "$LOG_DIR"

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT_FILE=$(mktemp /tmp/claudecoach_plan.XXXXXX)
trap "rm -f $PROMPT_FILE" EXIT

cat > "$PROMPT_FILE" <<'PROMPT_END'
You are generating the rolling 2-week training plan for Jamie Diamond's IM Cervia 2026 coaching system.

Step 1 — Pull live data:
- get_athlete_profile (today's date, FTP, athlete timezone)
- get_fitness (14 days — CTL, ATL, TSB)
- get_wellness (14 days — sleep, HRV)
- get_training_history (14 days — what was actually done)
- get_events (start_date=today, end_date=<today+21 days>) — what's already planned

Step 2 — Read:
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/current-state.md (ankle, niggles, open actions)
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/current-state.json (ankle pain scores, weight)
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/reference/rules.md (HARD CONSTRAINTS — read fully)
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/reference/decision-points.md (upcoming forks)

Step 3 — Determine the planning window:
- Target: the 2 weeks starting NEXT Monday (not today).
- Check get_events for that window. If there are already 7+ events planned: output "Plan already populated for [date range] — skipping." and stop.
- If <7 events: generate enough sessions to fill the week appropriately.

Step 4 — Determine phase and TSS target:
- Week number = ceil((Monday date - 2026-04-27) / 7)
- Phase and TSS targets:
  Week 1–6   (Base):     350–500 TSS/wk, focus Z2 bike volume + aerobic swim + easy run
  Week 7–10  (Build):    450–600 TSS/wk, add threshold bike work, extend long run
  Week 11–14 (Specific): 550–720 TSS/wk, race-pace intervals, brick sessions
  Week 15–17 (Peak):     650–800 TSS/wk, race simulation, consolidate fitness
  Week 18–21 (Taper):    200–350 TSS/wk, sharpen, no new stimuli

Step 5 — Apply mandatory constraints (from rules.md — these are HARD overrides):
- Ankle: no quality run sessions (intervals/tempo/race-pace) until current-state.json ankle.four_pain_free_weeks_reached = true. Use 5:30 run-walk format only (Z2 HR cap 150). Weekly run km increase ≤10%.
- CTL ramp: ≤+4 CTL/wk while ankle in rehab. Calculate expected weekly TSS that would produce this; cap the week total accordingly.
- Strength: minimum 1 session/week (target 2). Jamie is currently at 0 sessions/week — make strength a priority.
- Never prescribe new fuel/kit/shoes in the last 4 weeks.
- Always state day-of-week alongside date in session names.

Step 6 — Build the 2-week session structure:
Standard week template (adapt to phase):
- Monday: Rest or recovery swim
- Tuesday: Run (Z2, 5:30 walk-run) + optional swim
- Wednesday: Bike Z2 (60–90 min) or strength
- Thursday: Swim (CSS-based) + optional short run
- Friday: Long ride (Z2 NP target) — key session
- Saturday: Brick (ride + run) or long run
- Sunday: Rest or short active recovery

For each session create a push_workout call with:
  sport: "Ride" | "Run" | "Swim" | "WeightTraining"
  date: YYYY-MM-DD (must be a specific date in the planning window)
  name: "[Day date] — [session description]" e.g. "Tue 12 May — Z2 run 50 min (5:30)"
  description: full coaching notes including:
    - Target zones, paces, or power ranges
    - Duration and structure
    - Nutrition instructions for sessions >90 min
    - Ankle protocol reminder on all run sessions
    - One sentence rationale (physiological adaptation or risk being mitigated)
  planned_training_load: estimated TSS integer

Step 7 — Call push_workout for each planned session. Do NOT overwrite sessions that already exist in get_events for those dates — check date+sport before pushing.

Step 8 — Output summary:
"Plan generated: [date range]
Week [N] ([phase]): [N sessions] · [total TSS] TSS planned
Week [N+1] ([phase]): [N sessions] · [total TSS] TSS planned
Key constraints applied: [list any ankle/ramp/strength rules that shaped the plan]"

Step 9 — Notify via Telegram:
Run: python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py "Plan generated [date range]: W[N] [X TSS] + W[N+1] [Y TSS]. [Any key constraint note]"

Step 10 — Update current-state.md "Open actions" section: mark "Plan generated through [date]" with today's date.
Run: git add ClaudeCoach/current-state.md && git fetch origin && git rebase --autostash origin/main && git commit -m "plan: generated W[N]-W[N+1] [date]" && git push origin main
PROMPT_END

TOOLS="Read,Write,Edit,Bash,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_fitness,mcp__claude_ai_icusync__get_wellness,mcp__claude_ai_icusync__get_training_history,mcp__claude_ai_icusync__get_events,mcp__claude_ai_icusync__push_workout,mcp__claude_ai_icusync__edit_workout"

OUTPUT=$($CLAUDE -p "$(cat "$PROMPT_FILE")" --allowedTools "$TOOLS" 2>>"$LOG_DIR/generate-plan.log")
echo "$OUTPUT"
if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
