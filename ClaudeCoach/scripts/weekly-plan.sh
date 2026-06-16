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
for A in jamie kathryn calum; do
  echo "--- $A $(date) ---" >> "$LOG"
  timeout 1800 python3 "$R/scripts/stage1-plan.py" --athlete "$A" --push --notify --max-attempts 3 >> "$LOG" 2>&1
  echo "--- $A rc=$? ---" >> "$LOG"
done
echo "=== done $(date) ===" >> "$LOG"
