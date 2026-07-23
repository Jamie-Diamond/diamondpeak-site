#!/bin/bash
# Shared loud-failure helper for the git sync/push cron jobs
# (sync-private-repo.sh, cc-gitpull.sh).
#
# Before this existed, a failed commit/push was swallowed: cc-gitpull.sh had no
# logging or error check at all, and sync-private-repo.sh only wrote a line to a
# log nobody read. That let the nightly private-repo sync abort every night from
# 5 Jul 2026 with no one noticing (stale athlete-data history off-box).
#
# git_sync_fail "<job>" "<reason>" makes a failure LOUD three ways:
#   1. appends to a persistent flag file (a human/watchdog sees a standing signal)
#   2. records an ops_log failure entry -> surfaces in the 21:30 coach ops digest
#   3. sends an immediate Telegram alert to the coach (default chat)
# git_sync_ok clears the flag on a clean run so it does not nag forever.
CC_BASE="/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach"
GA_LOG_DIR="$HOME/Library/Logs/ClaudeCoach"
GA_FLAG_FILE="$GA_LOG_DIR/git-sync-FAILED.flag"

git_sync_fail() {
  local job="$1" reason="$2" ts
  ts="$(date "+%Y-%m-%d %H:%M:%S")"
  mkdir -p "$GA_LOG_DIR"
  printf "[%s] %s: %s\n" "$ts" "$job" "$reason" >> "$GA_FLAG_FILE"
  echo "[$job] LOUD-FAIL $ts: $reason" >&2
  python3 - "$job" "$reason" <<"PY" 2>/dev/null || true
import sys
sys.path.insert(0, "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/lib")
import ops_log
ops_log.alert(sys.argv[1], sys.argv[2])
PY
  python3 "$CC_BASE/telegram/notify.py" --no-history \
    "git-sync FAILED - $job: $reason (see $GA_FLAG_FILE)" >/dev/null 2>&1 || true
}

git_sync_ok() {
  rm -f "$GA_FLAG_FILE" 2>/dev/null || true
}
