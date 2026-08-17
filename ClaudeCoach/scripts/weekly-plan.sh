#!/bin/bash
# Weekly plan generation — two-stage engine (stage1-plan.py), gated --push.
# Runs each CONFIGURED athlete (ctl_targets/phase_tss + event set). Replaces the old
# generate-plan.py Sunday cron. Gated: only pushes a week that passes the protocol
# audit; a non-clean week is NOT pushed (athlete's existing plan stays intact).
# Calum configured 16 Jun 2026 (finish-oriented Marmotte targets) — now included.
set -u
R=/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach
LOG="$HOME/Library/Logs/ClaudeCoach/weekly-plan.log"
mkdir -p "$(dirname "$LOG")"
echo "=== weekly-plan $(date) ===" >> "$LOG"
# Keep each athlete's ICU configured FTP tracking eFTP (raise-only) BEFORE planning, so
# the week is built on current power zones. Messages the athlete on any change.
echo "--- FTP sync $(date) ---" >> "$LOG"
timeout 300 python3 "$R/lib/thresholds.py" --all --sync-ftp --apply --notify >> "$LOG" 2>&1
# rc was captured and thrown away, which is how 9 Aug 2026 passed unnoticed: all THREE
# athletes got no week (every attempt hard-blocked on name_intensity_mismatch), the loop
# logged "rc=0" three times, and the first anyone knew was Jamie asking "What's the plan
# this week?" the next morning. stage1-plan now exits 3 when it pushed nothing, 1 when it
# could not build at all, and a non-zero rc here raises a real alert.
#
# 17 Aug 2026, the other half of that: the alert has to be TRUE as well as loud. On
# Sunday 16 Aug calum's build took the empty-week fallback, pushed three events onto his
# calendar (push_result listed the ids, and the next day's plan audit read the week back
# as 380 TSS), and still exited 3, so this loop reported "NO WEEK PUSHED for calum" for
# a week that was there. stage1-plan now derives its code from whether it actually pushed:
# the fallback exits 0 like any other delivered week, and the fact that it is off target
# reaches Jamie as a FINDING heartbeat in the evening digest instead of as a missing
# deliverable. Every code the script can emit is now named in the lookup below, which 4
# was not: 1 built nothing, 3 built a week and would not push it, 4 stood down because
# another build held this athlete's lock (plan_lock.BUSY_EXIT), 124 the `timeout` above
# killed it. 0 never reaches the lookup.
FAILED=""
for A in jamie kathryn calum; do
  echo "--- $A $(date) ---" >> "$LOG"
  timeout 2700 python3 "$R/scripts/stage1-plan.py" --athlete "$A" --push --notify --max-attempts 3 >> "$LOG" 2>&1
  RC=$?
  echo "--- $A rc=$RC ---" >> "$LOG"
  if [ "$RC" -ne 0 ]; then
    FAILED="$FAILED $A(rc=$RC)"
    python3 - "$A" "$RC" <<'PY' >> "$LOG" 2>&1
import sys
sys.path.insert(0, "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/lib")
import ops_log
athlete, rc = sys.argv[1], sys.argv[2]
what = {"3": "built a week but it failed the gate, so nothing was pushed",
        "1": "could not build a week at all",
        "4": "stood down because another build for this athlete was already running",
        "124": "TIMED OUT after 45 min"}.get(rc, f"exited {rc}")
# 4 had no entry until 17 Aug 2026 and fell through to "exited 4". It is also the one
# code here that is not a missed deliverable: the build holding the lock pushes the week
# and sends the athlete's message, which is why stage1-plan writes no heartbeat for it.
# So it keeps the alert (this run did push nothing, and a Sunday collision is worth
# knowing about) and loses the closing sentence, which would be false.
tail = ("" if rc == "4" else
        " Their calendar has no plan for the coming week until someone builds one.")
ops_log.alert("weekly-plan", f"NO WEEK PUSHED for {athlete}: {what}." + tail,
              athlete=athlete)
PY
  fi
done
# One line naming everyone who got nothing, so the failure is legible at a glance rather
# than reconstructed from three separate entries.
if [ -n "$FAILED" ]; then
  echo "=== NO WEEK PUSHED for:$FAILED ===" >> "$LOG"
  python3 - "$FAILED" <<'PY' >> "$LOG" 2>&1
import sys
sys.path.insert(0, "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/lib")
import ops_log
ops_log.alert("weekly-plan", f"weekly generation delivered NO plan for:{sys.argv[1]}")
PY
fi
echo "=== done $(date) ===" >> "$LOG"
