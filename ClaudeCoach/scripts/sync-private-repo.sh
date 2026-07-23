#!/bin/bash
# Mirror the live (gitignored) athlete data into the PRIVATE dpc_private repo so
# it gains real version history. Runs nightly from cron (see crontab).
#
# This is the lightweight form of the dpc_private cutover stage 2
# (PRIVATE-REPO.md): "re-sync this repo from the live tree (rsync + commit)",
# run on a schedule. It does NOT do the full cutover (history scrub, force-push,
# repointing systemd) - the live system still runs from the diamondpeak-site
# clone. This job only keeps dpc_private continuously in sync so athlete state
# has a version history.
#
# SCOPE: this mirrors the athletes/ tree ONLY - the files that currently have no
# version history (system prompts, blueprints, heat logs, intensity sidecars,
# reference docs). It deliberately does NOT commit config/athletes.json, which
# holds plaintext intervals.icu API keys: keys never get pushed to GitHub, even
# a private repo. config/athletes.json keeps its own history via the existing
# nightly backup-config.sh, which commits an ENCRYPTED .enc blob.
#
# Secrets under athletes/ (strava_tokens.json) are excluded by dpc_private's
# .gitignore AND the rsync excludes below, so none reach git.
#
# Restore any athlete file:  git -C /root/dpc_private_repo show <sha>:<path>
set -uo pipefail
source "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/scripts/lib_git_alert.sh"

LIVE="/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach"
PRIV="/root/dpc_private_repo"
TS="$(date '+%Y-%m-%d %H:%M:%S')"

# Safety: refuse to push unless origin really is the private repo (never the
# public site, which would leak API keys into a public GitHub repo).
REMOTE="$(git -C "$PRIV" remote get-url origin 2>/dev/null || echo none)"
case "$REMOTE" in
  *dpc_private*) : ;;
  *) git_sync_fail "sync-private" "origin is not dpc_private ($REMOTE)"; exit 1 ;;
esac

if [ ! -d "$LIVE/athletes" ]; then
  git_sync_fail "sync-private" "live athlete data not found under $LIVE"; exit 1
fi

# Start from last night's pushed state so the working tree is deterministic.
git -C "$PRIV" fetch origin --quiet || echo "[sync-private] WARN $TS: fetch failed, using local"
git -C "$PRIV" reset --hard origin/main --quiet 2>/dev/null || true

# Mirror the live athlete tree + coaching config into the private repo.
# --delete keeps the mirror faithful (removes files no longer live).
# Excludes match dpc_private/.gitignore secrets + runtime noise so we never even
# copy them into the private working tree.
rsync -a --delete \
  --exclude 'strava_tokens.json' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.bak' \
  --exclude '*.bak-*' \
  "$LIVE/athletes/" "$PRIV/athletes/"

# Keep plaintext config/athletes.json out of the repo TIP on every run (its
# 5-Jul copy stays in history - accepted risk, not scrubbed). This self-heals
# the untrack after each reset --hard, with no interactive push needed.
grep -qxF 'config/athletes.json' "$PRIV/.gitignore" 2>/dev/null || printf '\n# plaintext ICU API keys - stop re-committing (history left as-is)\nconfig/athletes.json\n' >> "$PRIV/.gitignore"
git -C "$PRIV" rm --cached --quiet config/athletes.json 2>/dev/null || true

git -C "$PRIV" add -A athletes .gitignore
if git -C "$PRIV" diff --cached --quiet; then
  echo "[sync-private] no changes $TS"
  git_sync_ok
  exit 0
fi

# Safety net: refuse to push if any ADDED line smells like a credential
# (intervals.icu API keys, tokens, bearer strings). Nothing under athletes/
# should contain these; if one ever does, stop rather than leak it.
# NOTE: we scan ADDED lines only (^+ content, excluding the +++ file header).
# The old form piped through `grep -nEi ... | grep -v '^-'`, but `-n` prefixes a
# line number so deletion lines read `123:-...` and slipped past the `^-`
# filter - which made the untracking of config/athletes.json (a deletion of
# its icu_api_key lines) trip a FALSE credential abort every single night.
if git -C "$PRIV" diff --cached -U0 | grep -E '^\+[^+]' | grep -Ei '(api[_-]?key|bearer|secret|"?token"?[[:space:]]*[:=]|icu_[a-z0-9]{20,})'; then
  git_sync_fail "sync-private" "possible credential in ADDED athlete lines (see match above) - not pushing"
  git -C "$PRIV" reset -q
  exit 1
fi

git -C "$PRIV" commit -q -m "athlete-data sync $(date +%Y-%m-%d)"
# local branch is 'master', remote deploy branch is 'main' - push HEAD explicitly.
if git -C "$PRIV" push -q origin HEAD:main; then
  echo "[sync-private] pushed $TS ($(git -C "$PRIV" rev-parse --short HEAD))"
  git_sync_ok
else
  git_sync_fail "sync-private" "push to dpc_private failed - committed locally, will retry next run"
fi
