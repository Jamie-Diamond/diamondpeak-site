#!/bin/bash
# Shared loud-failure helper for the git sync/push cron jobs
# (sync-private-repo.sh, cc-gitpull.sh).
#
# Before this existed, a failed commit/push was swallowed: cc-gitpull.sh had no
# logging or error check at all, and sync-private-repo.sh only wrote a line to a
# log nobody read. That let the nightly private-repo sync abort every night from
# 5 Jul 2026 with no one noticing (stale athlete-data history off-box).
#
# git_sync_fail "<job>" "<reason>" records a failure two ways:
#   1. appends to a persistent flag file (a human/watchdog sees a standing signal)
#   2. hands it to ops_log.sync_failure, which decides how loud it should be
# git_sync_ok "<job>" clears both on a clean run so they do not nag forever.
#
# LOG-ONLY from 27 Jul 2026 (Jamie's call): this used to fire an instant Telegram
# to the coach on every failure. Across 23-27 Jul that produced a dozen messages
# and not one needed action - every failure healed itself on the next tick. The
# loudness decision now lives in ONE place, lib/ops_log.py's sync_failure, so the
# shell jobs and the Python jobs (lib/git_sync.py) cannot drift apart: first
# failure is logged as transient, the second and beyond reach the evening digest,
# and only a sync stuck for ESCALATE_AFTER runs still breaks through to Telegram.
#
# The job label is what keys the consecutive-failure counter, so pass a STABLE
# one - five jobs share this helper and each counts separately.
CC_BASE="/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach"
GA_LOG_DIR="$HOME/Library/Logs/ClaudeCoach"
GA_FLAG_FILE="$GA_LOG_DIR/git-sync-FAILED.flag"

git_sync_fail() {
  local job="$1" reason="$2" ts
  ts="$(date "+%Y-%m-%d %H:%M:%S")"
  mkdir -p "$GA_LOG_DIR"
  printf "[%s] %s: %s\n" "$ts" "$job" "$reason" >> "$GA_FLAG_FILE"
  echo "[$job] FAIL $ts: $reason" >&2
  python3 - "$job" "$reason" <<"PY" 2>/dev/null || true
import sys
sys.path.insert(0, "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/lib")
import ops_log
ops_log.sync_failure(sys.argv[1], sys.argv[2])
PY
}

git_sync_ok() {
  local job="${1:-}"
  rm -f "$GA_FLAG_FILE" 2>/dev/null || true
  [ -n "$job" ] || return 0
  python3 - "$job" <<"PY" 2>/dev/null || true
import sys
sys.path.insert(0, "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/lib")
import ops_log
ops_log.sync_ok(sys.argv[1])
PY
}

# ---------------------------------------------------------------------------
# Serialisation + one bounded retry for the cron jobs that share the ONE
# diamondpeak-site working tree.
#
# Why (24-27 Jul 2026): four-plus cron jobs each ran their own add/commit/push
# in the same tree and most were scheduled on the hour. Two pushing in the same
# second made GitHub reject the loser - "cannot lock ref 'refs/heads/main': is
# at X but expected Y", 6 occurrences, every one exactly on the hour - and two
# running local git at once produced ".git/index.lock: File exists" (26 Jul).
# Staggering the schedule alone cannot fix the index.lock class, so the git
# block is serialised with flock and a lost push race is absorbed by a single
# retry instead of alerting. lib/git_sync.py gives the Python jobs the same two
# behaviours against the SAME lock path - keep the two in step.
GA_LOCK="${GA_LOCK:-/var/lock/dp-git.lock}"
GA_LOCK_WAIT="${GA_LOCK_WAIT:-120}"

# git_lock <job> - take the repo-wide git lock on fd 9. Returns 1 if another job
# still holds it after GA_LOCK_WAIT seconds, and the caller should then SKIP
# quietly (exit 0): the next tick redoes the work, so waiting out a busy lock is
# not a failure and must not fire git_sync_fail. Wrap ONLY the git block - never
# a long API pull - or a slow job starves the */5 and */30 pushers.
git_lock() {
  local job="$1"
  exec 9>"$GA_LOCK" 2>/dev/null || {
    echo "[$job] WARN: cannot open $GA_LOCK - proceeding unlocked"
    return 0
  }
  if ! flock -w "$GA_LOCK_WAIT" 9; then
    echo "[$job] SKIP: another git job held $GA_LOCK for >${GA_LOCK_WAIT}s - next tick will retry"
    return 1
  fi
  return 0
}

git_unlock() {
  flock -u 9 2>/dev/null || true
  exec 9>&- 2>/dev/null || true
}

# git_push_retry <job> [rebase|merge] - push origin main; on a REJECTED push do
# ONE bounded fetch + integrate + push, then give up. Returns 0 on success. The
# integration mode defaults to rebase and should match the caller's own policy
# (cc-gitpull.sh merges, so it passes "merge").
#
# Safety - the retry cannot commit anything the caller did not already stage:
# it runs no `git add` and no `git commit`, and by the time it is reached the
# caller has already committed. --autostash only shelves and restores
# UNCOMMITTED worktree files (the routine config/*.enc bot churn); it cannot
# promote them into the commit being pushed. If the automatic restore conflicts
# the stash count grows, and we fail loudly rather than leave someone's dirty
# work silently parked in a stash.
# A secret gate runs BEFORE the first push (28 Jul 2026). .gitignore is advisory -
# it does nothing for an already-tracked path and `git add -f` ignores it, so it
# cannot by itself keep a credential out of this PUBLIC repo. Every shell pusher
# reaches origin through this function, so this is the one place that makes the
# deny-list load-bearing. Fail CLOSED: a missing/failing gate script blocks the
# push. The gate never prints a secret value.
# Reported on STDOUT, not stderr: git_lock below does `exec 9>... 2>/dev/null`,
# which silences stderr for the rest of the shell, and callers lock before they
# push - a >&2 message here would never reach the log.
git_push_retry() {
  local job="$1" mode="${2:-rebase}" stash_before stash_after
  local gate="$CC_BASE/scripts/check-no-public-secrets.sh"
  if [ ! -x "$gate" ]; then
    echo "[$job] BLOCKED: secret gate missing or not executable at $gate"
    return 1
  fi
  if ! "$gate" tree; then
    echo "[$job] BLOCKED by secret gate - refusing to push (see [secret-gate] lines above)"
    return 1
  fi
  if git push origin main; then return 0; fi
  echo "[$job] push rejected - one bounded retry (fetch + $mode + push)"
  if ! git fetch origin; then
    echo "[$job] retry fetch failed"
    return 1
  fi
  if [ "$mode" = "merge" ]; then
    if ! git merge origin/main --no-edit; then
      git merge --abort 2>/dev/null || true
      echo "[$job] retry merge failed (conflict) - aborted"
      return 1
    fi
  else
    stash_before="$(git stash list | wc -l)"
    if ! git rebase --autostash origin/main; then
      git rebase --abort 2>/dev/null || true
      echo "[$job] retry rebase failed (conflict) - aborted"
      return 1
    fi
    stash_after="$(git stash list | wc -l)"
    if [ "$stash_after" -gt "$stash_before" ]; then
      echo "[$job] retry autostash could NOT be restored - uncommitted work is parked in git stash"
      return 1
    fi
  fi
  git push origin main
}
