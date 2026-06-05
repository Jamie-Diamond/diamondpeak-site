#!/bin/bash
# Encrypt athletes.json and commit to git as a backup.
# Key lives at ~/.claudecoach_key — keep it somewhere safe outside this repo.
# To restore: openssl enc -d -aes-256-cbc -pbkdf2 -in ClaudeCoach/config/athletes.json.enc -out ClaudeCoach/config/athletes.json -pass file:~/.claudecoach_key

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC="$PROJECT_DIR/ClaudeCoach/config/athletes.json"
DST="$PROJECT_DIR/ClaudeCoach/config/athletes.json.enc"
KEY="$PROJECT_DIR/ClaudeCoach/config/backup.key"

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
git add ClaudeCoach/config/athletes.json.enc
git fetch origin
git merge origin/main --no-edit
git commit -m "backup: athletes.json.enc $(date +%Y-%m-%d)" || echo "[backup-config] nothing to commit"
git push origin main

echo "[backup-config] done $(date)"
