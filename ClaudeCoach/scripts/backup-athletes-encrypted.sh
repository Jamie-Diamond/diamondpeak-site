#!/bin/bash
# Nightly ENCRYPTED off-server backup of athlete data (Jamie, 15 Oct 2026, GDPR item 9).
#
# Why: the plaintext athletes/ mirror (sync-private-repo.sh) has failed every night since
# 13 Aug 2026, so nothing but athletes.json left the server for two months - and when it
# did work it put plaintext health data on GitHub, whose free plan has no data processing
# agreement. This replaces it: GitHub only ever holds ciphertext.
#
# What: ClaudeCoach/athletes/, ClaudeCoach/config/ (runtime state: athletes.json,
# onboarding, sign-ups, Strava app) and /etc/claudecoach (push keys, Intervals OAuth app).
# backup.key itself is left out. tar + gzip, then openssl AES-256-CBC with pbkdf2 and
# config/backup.key - the same key and cipher as backup-config.sh.
#
# Where: /root/backup-athletes-local (last 14 nights) and the PRIVATE repo dpc_private,
# branch `encrypted-backups`: ONE orphan commit holding the last 7 archives, force-pushed
# each night, so the branch never grows a history. Each archive is checked to decrypt and
# list before anything is pushed.
#
# Restore one night:
#   git -C /root/dpc_backups fetch origin encrypted-backups
#   git -C /root/dpc_backups show origin/encrypted-backups:athletes-YYYY-MM-DD.tar.gz.enc > a.enc
#   openssl enc -d -aes-256-cbc -pbkdf2 -in a.enc -pass file:<backup.key> | tar -xz -C /tmp/restore
#
# The key lives ONLY on this VM. Keep a copy off the server (Jamie's password manager), or a
# lost VM means unreadable backups.
#
# Cron (VM crontab): 25 23 * * * /root/.claude/cc-run bash .../backup-athletes-encrypted.sh
set -uo pipefail
source "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/scripts/lib_git_alert.sh"

NAME="backup-athletes"
LIVE_REL="Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach"
KEY="/$LIVE_REL/config/backup.key"
LOCAL="/root/backup-athletes-local"
KEEP_LOCAL=14
KEEP_REMOTE=7
REPO="/root/dpc_backups"
REMOTE_URL="git@github.com:Jamie-Diamond/dpc_private.git"
BRANCH="encrypted-backups"
TODAY="$(date +%F)"
OUT="$LOCAL/athletes-$TODAY.tar.gz.enc"

fail() { git_sync_fail "$NAME" "$1"; exit 1; }

[ -f "$KEY" ] || fail "backup key missing at $KEY"
mkdir -p "$LOCAL" && chmod 700 "$LOCAL"

# 1. Archive and encrypt.
tar -C / -czf - --exclude='__pycache__' --exclude="$LIVE_REL/config/backup.key" \
    "$LIVE_REL/athletes" "$LIVE_REL/config" etc/claudecoach \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass file:"$KEY" -out "$OUT.tmp" \
  || fail "archive or encryption failed"

# 2. Prove it decrypts and lists before trusting it.
FILES="$(openssl enc -d -aes-256-cbc -pbkdf2 -in "$OUT.tmp" -pass file:"$KEY" | tar -tzf - | wc -l)"
[ "${FILES:-0}" -gt 50 ] || fail "archive check failed: only ${FILES:-0} entries after decrypting"
mv "$OUT.tmp" "$OUT"
ls -1t "$LOCAL"/athletes-*.tar.gz.enc | tail -n +$((KEEP_LOCAL + 1)) | xargs -r rm -f

# 3. One orphan commit of the newest KEEP_REMOTE archives, force-pushed to the private repo.
if [ ! -d "$REPO/.git" ]; then
  git init -q "$REPO" && git -C "$REPO" remote add origin "$REMOTE_URL" || fail "cannot set up $REPO"
fi
cd "$REPO" || fail "cannot cd to $REPO"
git fetch -q origin "$BRANCH" 2>/dev/null || true     # first run: the branch doesn't exist yet
git checkout -q --orphan "nightly-$TODAY" 2>/dev/null || git checkout -q "nightly-$TODAY" || fail "orphan checkout failed"
git rm -rq --cached . 2>/dev/null || true
find . -maxdepth 1 -name 'athletes-*.tar.gz.enc' -delete
for f in $(ls -1t "$LOCAL"/athletes-*.tar.gz.enc | head -n "$KEEP_REMOTE"); do cp "$f" .; done
printf 'Encrypted nightly athlete backups (AES-256, key on the VM only).\nSee ClaudeCoach/scripts/backup-athletes-encrypted.sh in diamondpeak-site.\n' > README.md
git add -A . && git -c user.name="ClaudeCoach ops" -c user.email="noreply@anthropic.com" \
  commit -qm "encrypted athlete backups to $TODAY (last $KEEP_REMOTE nights)" || fail "commit failed"
git push -q --force origin "HEAD:refs/heads/$BRANCH" || fail "push to dpc_private $BRANCH failed"
# Keep only tonight's branch locally; the remote branch is the record.
git for-each-ref --format='%(refname:short)' refs/heads/ | grep -v "^nightly-$TODAY$" | xargs -r git branch -qD
git reflog expire --expire=now --all && git gc -q --prune=now     # drop archives older than the branch

SIZE="$(du -h "$OUT" | cut -f1)"
echo "[$NAME] $(date '+%F %T') ok: $OUT ($SIZE, $FILES entries), pushed $BRANCH"
git_sync_ok "$NAME"
