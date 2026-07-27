#!/bin/bash
# Single-command commit+push for the AGENT PROMPTS (watchdog, weekly-summary,
# daily-prescription). Those prompts used to hand the model a raw
# "git add && git fetch && git rebase && git commit && git push" chain, which
# bypassed the repo-wide lock and the push retry that every code pusher now goes
# through - so a model-run git could still collide with the cron jobs writing
# this same tree every few minutes (".git/index.lock: File exists",
# "cannot lock ref 'refs/heads/main'").
#
# lib_git_alert.sh exposes git_lock/git_push_retry as bash FUNCTIONS, which is no
# use in a prompt: you cannot reliably tell a model to source a library and
# compose a sequence. This is the one command a prompt can name.
#
# Usage:  cc-git-commit-push.sh "<commit message>" <path> [<path> ...]
#         paths are repo-relative, e.g. ClaudeCoach/athletes/jamie/current-state.md
#
# Exit 0 = committed and pushed, or nothing to commit, or the repo lock was busy
# (a skip, not a failure - the next run redoes it). Exit 1 = a real failure, and
# it has already been made loud (flag file + ops digest + Telegram).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/lib_git_alert.sh"

JOB="agent-commit"

if [ "$#" -lt 2 ]; then
  echo "usage: $(basename "$0") \"<commit message>\" <path> [<path> ...]" >&2
  exit 2
fi

MSG="$1"; shift

cd "$PROJECT_DIR" || { git_sync_fail "$JOB" "cannot cd to repo"; exit 1; }

git_lock "$JOB" || exit 0
trap git_unlock EXIT

# Stage by EXPLICIT path only - never -A. These prompts run alongside unrelated
# bot churn (config/*.enc) that must not be swept into the commit.
git add -- "$@" || { git_sync_fail "$JOB" "git add failed for: $*"; exit 1; }

if git diff --cached --quiet -- "$@"; then
  echo "[$JOB] no change in $* - nothing to commit"
  exit 0
fi

git commit -q -m "$MSG" || { git_sync_fail "$JOB" "git commit failed"; exit 1; }

if ! git fetch origin; then
  git_sync_fail "$JOB" "git fetch failed - committed locally only"
  exit 1
fi
if ! git rebase --autostash origin/main; then
  git rebase --abort 2>/dev/null || true
  git_sync_fail "$JOB" "rebase onto origin/main conflicted - committed locally only"
  exit 1
fi
if git_push_retry "$JOB"; then
  git_sync_ok "$JOB"
  echo "[$JOB] pushed: $MSG"
else
  git_sync_fail "$JOB" "push failed after one retry - committed locally only"
  exit 1
fi
