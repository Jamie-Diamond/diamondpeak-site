#!/bin/bash
# Encrypt athletes.json and commit the ciphertext to git as a backup.
# Key: ClaudeCoach/config/backup.key (gitignored, VM-only).
# To restore: openssl enc -d -aes-256-cbc -pbkdf2 -in config/athletes.json.enc \
#             -out athletes.json -pass file:ClaudeCoach/config/backup.key
#
# TARGET CHANGED 2026-07-28: the ciphertext now goes to the PRIVATE repo
# (dpc_private), NOT to diamondpeak-site, which is public. Encrypted athlete
# config is not public-repo material at any strength of cipher. dpc_private is
# confirmed private and already tracks config/athletes.json.enc, so the backup
# keeps its history - just not on a public remote. The public repo no longer
# tracks the .enc at all (see .gitignore).
#
# Race-hardened 2026-07-27: runs its git block under the shared repo lock and
# gets one bounded push retry, and a push that still fails is LOUD instead of
# killing the script silently via `set -e`. Commit happens BEFORE integrating
# with origin - the old order (add -> fetch -> merge -> commit) tried to merge
# with a dirty index.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC="$PROJECT_DIR/ClaudeCoach/config/athletes.json"
DST="$PROJECT_DIR/ClaudeCoach/config/athletes.json.enc"
KEY="$PROJECT_DIR/ClaudeCoach/config/backup.key"

PRIV="/root/dpc_private_repo"
# dpc_private's remote HEAD, confirmed 2026-07-28 with
#   git -C /root/dpc_private_repo ls-remote --symref origin HEAD
#   -> ref: refs/heads/main   HEAD
# The LOCAL branch there is 'master', which is why the push below is an explicit
# HEAD:$PRIV_BRANCH refspec (same reason sync-private-repo.sh does it). Do not
# use lib_git_alert.sh's git_push_retry here - it hardcodes `git push origin
# main`, which has no local 'main' to push in this repo.
PRIV_BRANCH="main"
PRIV_ENC="$PRIV/config/athletes.json.enc"   # dpc_private is ROOT-layout (config/,
                                            # athletes/, lib/ ...), NOT ClaudeCoach/*.

# Off-git snapshots. sync-private-repo.sh runs at 23:20 - 30 min before this job -
# and its first act is `git reset --hard origin/$PRIV_BRANCH`, which DISCARDS any
# commit this script made but failed to push. It does not take git_lock, so the
# lock cannot protect us from it. These snapshots are the actual mitigation: even
# if a commit is thrown away, the ciphertext for that night still exists on disk.
SNAP_DIR="/root/backup-config-local"
SNAP_KEEP=14

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

# Snapshot first, so the backup survives even if every git step below fails.
mkdir -p "$SNAP_DIR"
chmod 700 "$SNAP_DIR"
cp "$DST" "$SNAP_DIR/athletes.json.enc.$(date +%Y-%m-%d)"
chmod 600 "$SNAP_DIR"/athletes.json.enc.* 2>/dev/null || true
# shellcheck disable=SC2012
ls -1t "$SNAP_DIR"/athletes.json.enc.* 2>/dev/null | tail -n +$((SNAP_KEEP + 1)) | while read -r old; do
  rm -f -- "$old"
done

# Safety: refuse to push unless origin really is the private repo. Same guard
# sync-private-repo.sh uses (its lines 30-36) - never let this fall back to the
# public site, which is what it used to do by default.
if [ ! -d "$PRIV/.git" ]; then
  git_sync_fail "backup-config" "private repo checkout missing at $PRIV - .enc written to $SNAP_DIR only"
  exit 1
fi
REMOTE="$(git -C "$PRIV" remote get-url origin 2>/dev/null || echo none)"
case "$REMOTE" in
  *dpc_private*) : ;;
  *)
    git_sync_fail "backup-config" "origin of $PRIV is not dpc_private ($REMOTE) - refusing to push, .enc written to $SNAP_DIR only"
    exit 1
    ;;
esac

cp "$DST" "$PRIV_ENC"
cd "$PRIV"

# Serialise everything that touches .git. A busy lock is a skip, not a failure.
git_lock "backup-config" || exit 0
trap git_unlock EXIT

git add -- config/athletes.json.enc
if git diff --cached --quiet -- config/athletes.json.enc; then
  echo "[backup-config] athletes.json.enc unchanged - nothing to commit"
  git_sync_ok "backup-config"
  echo "[backup-config] done $(date)"
  exit 0
fi

git commit -m "backup: athletes.json.enc $(date +%Y-%m-%d)"

# Integrate before the first push so the common case is not a rejected push.
# Fenced against `set -e` so a failure alerts instead of dying quietly.
if ! git fetch origin; then
  git_sync_fail "backup-config" "git fetch failed (dpc_private) - .enc committed locally only, snapshot in $SNAP_DIR"
  exit 1
fi
if ! git rev-parse --verify --quiet "origin/$PRIV_BRANCH" >/dev/null; then
  git_sync_fail "backup-config" "dpc_private has no origin/$PRIV_BRANCH - branch name changed? re-check with ls-remote --symref"
  exit 1
fi
if ! git rebase --autostash "origin/$PRIV_BRANCH"; then
  git rebase --abort 2>/dev/null || true
  git_sync_fail "backup-config" "rebase onto dpc_private/$PRIV_BRANCH conflicted - .enc committed locally only, snapshot in $SNAP_DIR"
  exit 1
fi

# One bounded push retry, inline (see the git_push_retry note above).
push_priv() {
  if git push origin "HEAD:$PRIV_BRANCH"; then return 0; fi
  echo "[backup-config] push rejected - one bounded retry (fetch + rebase + push)"
  git fetch origin || return 1
  if ! git rebase --autostash "origin/$PRIV_BRANCH"; then
    git rebase --abort 2>/dev/null || true
    return 1
  fi
  git push origin "HEAD:$PRIV_BRANCH"
}

if push_priv; then
  git_sync_ok "backup-config"
else
  git_sync_fail "backup-config" "push to dpc_private/$PRIV_BRANCH failed after one retry - .enc committed locally only (sync-private-repo.sh's 23:20 reset --hard will DROP it; snapshot kept in $SNAP_DIR)"
  exit 1
fi

echo "[backup-config] done $(date)"
