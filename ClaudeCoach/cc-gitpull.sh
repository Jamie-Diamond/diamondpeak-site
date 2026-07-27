#!/bin/bash
# Every-30-min auto-commit + pull/merge/push of the public diamondpeak-site repo
# (cron: 5,35 * * * * /usr/local/bin/cc-gitpull.sh).
#
# Hardened 2026-07-23: was previously silent - no logging, no error check, so a
# failed push (e.g. a merge conflict or auth lapse) let commits pile up locally
# with nobody the wiser. Now it logs every run and fires the shared loud-failure
# path (flag file + ops digest + Telegram to the coach) on any fetch/merge/push
# failure. NOTE: this lives in TWO places - the repo copy (versioned) and
# /usr/local/bin/cc-gitpull.sh (what cron runs). Keep them in sync.
#
# Race-hardened 2026-07-27: the git block now runs under the shared repo lock
# (git_lock) so it cannot collide with another job's index, and a push rejected
# by a concurrent pusher gets one bounded retry (git_push_retry, merge mode to
# match this script's merge-not-rebase policy) before it counts as a failure.
# Moved off :00 to :05/:35 so it no longer starts in the same second as the
# hourly refresh jobs.
set -uo pipefail
source "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/scripts/lib_git_alert.sh"

LOG="$HOME/Library/Logs/ClaudeCoach/cc-gitpull.log"
mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1
echo "=== cc-gitpull $(date '+%Y-%m-%d %H:%M:%S') ==="

cd /Users/diamondpeakconsulting/diamondpeak-site || { git_sync_fail "cc-gitpull" "cannot cd to repo"; exit 1; }

# Serialise everything that touches .git. A busy lock is a skip, not a failure.
git_lock "cc-gitpull" || exit 0
trap git_unlock EXIT

# Auto-commit VM-side state changes (data files only - not config or scripts).
# ClaudeCoach/athletes/ is gitignored here; the tracked state is training-data*.json.
if ! git diff --quiet -- ClaudeCoach/training-data*.json || \
   ! git diff --staged --quiet -- ClaudeCoach/training-data*.json; then
  git add ClaudeCoach/athletes/ ClaudeCoach/training-data*.json
  git commit -m "auto-save: pre-pull state $(date +%Y-%m-%dT%H:%M)" || echo "[cc-gitpull] nothing to commit"
fi

# Merge (not rebase) - avoids conflicts during active development sessions
git fetch origin || { git_sync_fail "cc-gitpull" "git fetch failed"; exit 1; }
if ! git merge origin/main --no-edit; then
  git merge --abort 2>/dev/null || true
  git_sync_fail "cc-gitpull" "merge of origin/main failed (conflict) - manual resolve needed"
  exit 1
fi
if git_push_retry "cc-gitpull" merge; then
  git_sync_ok "cc-gitpull"
else
  git_sync_fail "cc-gitpull" "push to origin/main failed after one retry - commits accumulating locally"
  exit 1
fi
