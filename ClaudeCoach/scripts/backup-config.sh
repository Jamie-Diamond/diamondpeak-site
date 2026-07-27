#!/bin/bash
# Encrypt athletes.json and commit to git as a backup.
# Key lives at ~/.claudecoach_key — keep it somewhere safe outside this repo.
# To restore: openssl enc -d -aes-256-cbc -pbkdf2 -in ClaudeCoach/config/athletes.json.enc -out ClaudeCoach/config/athletes.json -pass file:~/.claudecoach_key
#
# Race-hardened 2026-07-27: runs its git block under the shared repo lock and
# gets one bounded push retry (lib_git_alert.sh), and a push that still fails is
# now LOUD instead of killing the script silently via `set -e`. The git steps
# were also reordered to commit BEFORE integrating with origin — the old order
# (add → fetch → merge → commit) tried to merge with a dirty index, which is the
# same bug lib/git_sync.py's docstring calls out.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC="$PROJECT_DIR/ClaudeCoach/config/athletes.json"
DST="$PROJECT_DIR/ClaudeCoach/config/athletes.json.enc"
KEY="$PROJECT_DIR/ClaudeCoach/config/backup.key"

source "$PROJECT_DIR/ClaudeCoach/scripts/lib_git_alert.sh"

if [ ! -f "$KEY" ]; then
  echo "[backup-config] ERROR: key not found at $KEY" >&2
  exit 1
fi

if [ ! -f "$SRC" ]; then
  echo "[backup-config] ERROR: athletes.json not found at $SRC" >&2
  exit 1
fi

openssl enc -aes-256-cbc -pbkdf2 -in "$SRC" -out "$DST" -pass file:"$KEY"

cd "$PROJECT_DIR"

# Serialise everything that touches .git. A busy lock is a skip, not a failure.
git_lock "backup-config" || exit 0
trap git_unlock EXIT

git add -- ClaudeCoach/config/athletes.json.enc
if git diff --cached --quiet -- ClaudeCoach/config/athletes.json.enc; then
  echo "[backup-config] athletes.json.enc unchanged - nothing to commit"
  git_sync_ok
  echo "[backup-config] done $(date)"
  exit 0
fi

git commit -m "backup: athletes.json.enc $(date +%Y-%m-%d)"

# Integrate before the first push so the common case is not a rejected push.
# Fenced against `set -e` so a failure alerts instead of dying quietly.
if ! git fetch origin; then
  git_sync_fail "backup-config" "git fetch failed - .enc backup committed locally only"
  exit 1
fi
if ! git rebase --autostash origin/main; then
  git rebase --abort 2>/dev/null || true
  git_sync_fail "backup-config" "rebase onto origin/main conflicted - .enc backup committed locally only"
  exit 1
fi
if git_push_retry "backup-config"; then
  git_sync_ok
else
  git_sync_fail "backup-config" "push to origin/main failed after one retry - .enc backup committed locally only"
  exit 1
fi

echo "[backup-config] done $(date)"
