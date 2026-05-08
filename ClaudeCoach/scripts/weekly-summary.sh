#!/bin/bash
# Weekly summary — W8. Runs via launchd at 20:00 every Sunday.
# Pre-prepares the week summary card before Monday's check-in.
# Safe to run manually: bash weekly-summary.sh

cd /Users/diamondpeakconsulting/diamondpeak-site

PROMPT_FILE=$(mktemp /tmp/claudecoach_weekly.XXXXXX)
trap "rm -f $PROMPT_FILE" EXIT

cat > "$PROMPT_FILE" <<PROMPT_END
You are running the W8 weekly training summary for Jamie Diamond's IM Cervia 2026 coaching system. Generate a concise week-end card and send a PushNotification.

Step 1 — Pull from IcuSync:
- get_athlete_profile (today's date and FTP)
- get_fitness (14 days — to show week-on-week CTL change)
- get_training_history (7 days — all this week's activities)
- get_events (Mon-Sun this week — planned sessions)
- get_wellness (7 days — HRV/sleep)

Step 2 — Read:
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/current-state.md
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/session-log.json (this week's RPE entries)
- /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/heat-log.json (this week's heat sessions)

Step 3 — Run the compliance + re-optimiser analysis:
  python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/ironman-analysis/scripts/reoptimise.py '<json>'
  JSON: planned_sessions (from get_events, with planned_tss), actual_sessions (from get_training_history, with tss), today, current_ctl, ankle_in_rehab.

Step 4 — Compute week summary:
  - Total actual TSS vs planned TSS
  - Compliance rate (actual / planned)
  - CTL change this week (end vs start of week)
  - ATL at end of week
  - Disciplines completed (bike / run / swim / strength) with session count
  - Sessions missed (if any) with names
  - Heat sessions this week (from heat-log.json)
  - Average sleep this week (from get_wellness)
  - Any watchdog triggers that would have fired (T1-T8, evaluate quickly)

Step 5 — Output the summary card:

---
**Week ending [date] — [STRONG / SOLID / LIGHT / MIXED]**
*(STRONG: >=95% compliance, no flags | SOLID: 80-95%, no major flags | LIGHT: <80% compliance | MIXED: compliance ok but flags fired)*

| Metric | This week | Target/trend |
|---|---|---|
| TSS | X (planned Y) | — |
| Compliance | X% | >=90% |
| CTL change | +X / -X | [build target if set] |
| ATL | X | — |
| Sleep avg | Xh | >=7h |
| Heat sessions | N | — |

**Completed:** [discipline summaries — e.g. "3 rides, 2 runs, 1 swim"]
**Missed:** [session names, or "none"]

**Key finding:** [one sentence — most important thing from this week, with L2 trail if a flag fired]

**Monday focus:** [one sentence — the single most important thing for next week's first session]

---

Step 6 — Send PushNotification with this exact format (under 200 chars):
"Week [N of ~21]: [X TSS / Y%] | CTL [+/-Z] | [headline flag or 'all clear']"

Example: "Week 3/21: 485 TSS / 91% | CTL +4.2 | all clear"
Example with flag: "Week 3/21: 310 TSS / 63% | CTL -1.1 | 2 sessions missed"

Do not send more than one PushNotification.
PROMPT_END

TOOLS="Read,Bash,mcp__claude_ai_icusync__get_athlete_profile,mcp__claude_ai_icusync__get_fitness,mcp__claude_ai_icusync__get_training_history,mcp__claude_ai_icusync__get_events,mcp__claude_ai_icusync__get_wellness,PushNotification"

OUTPUT=$(/Users/diamondpeakconsulting/.local/bin/claude -p "$(cat "$PROMPT_FILE")" --allowedTools "$TOOLS" 2>>"$HOME/Library/Logs/ClaudeCoach/weekly-summary.log")
echo "$OUTPUT"
if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT" | python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/telegram/notify.py
fi
