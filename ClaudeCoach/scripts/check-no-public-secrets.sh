#!/bin/bash
# Fail-closed secret gate for the PUBLIC repo (github.com/Jamie-Diamond/diamondpeak-site).
#
# WHY THIS EXISTS: .gitignore is advisory - it does nothing once a path is already
# tracked, and `git add -f` walks straight past it, so an ignore rule on its own
# has never been able to keep a credential path out of this repo. This script is
# the load-bearing half: the push path refuses to run at all if a deny-listed
# path is in the tree being pushed.
#
# Two layers:
#   1. PATH layer - deny-listed paths must not be tracked. Zero false positives,
#      cheap (git ls-files / ls-tree), and it catches the failure mode directly.
#   2. CONTENT layer - high-confidence credential SHAPES (a key name AND a
#      value-shaped literal) in the files a push would add/change. Deliberately
#      narrower than a bare word-match on "token"/"secret": sync-private-repo.sh
#      lines 74-78 record a broad pattern aborting a legitimate push every night
#      for weeks. Prose like `"icu_api_key": "..."` in docs does NOT match.
#
# NEVER PRINTS A SECRET VALUE. Detection is `grep -q`; the report is path +
# pattern NAME only. sync-private-repo.sh's scan echoes the matching line into
# sync-private.log, which is a credential-to-logfile pipe; do not copy that half.
#
# EVERYTHING IS REPORTED ON STDOUT, NOT STDERR, on purpose: lib_git_alert.sh's
# git_lock runs `exec 9>"$GA_LOCK" 2>/dev/null`, which permanently redirects
# stderr for the rest of the calling shell, and callers take that lock BEFORE
# they push. Anything written to stderr from here would vanish, leaving a blocked
# push with no stated reason. Cron logs stdout, so stdout is visible and safe.
#
# Usage:
#   check-no-public-secrets.sh tree     # default - scan the committed HEAD tree
#   check-no-public-secrets.sh staged   # scan the index (pre-commit hook)
# Exit 0 = clean (or target repo is the known-private one). Exit 1 = BLOCK.
set -uo pipefail

MODE="${1:-tree}"

if ! REPO="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "[secret-gate] BLOCK: not inside a git repo"
  exit 1
fi

# Polarity is fail-closed: enforce everywhere EXCEPT the repo we have positively
# confirmed is private. An unknown/missing origin is enforced, not skipped.
REMOTE="$(git remote get-url origin 2>/dev/null || echo none)"
case "$REMOTE" in
  *dpc_private*)
    echo "[secret-gate] target is dpc_private (private) - path deny-list not applied"
    exit 0
    ;;
esac

# --- layer 1: paths that must never be tracked in the public repo -------------
# Exact paths. Keep in step with the matching block in .gitignore.
DENY_EXACT=(
  "ClaudeCoach/config/athletes.json"
  "ClaudeCoach/config/athletes.json.enc"
  "ClaudeCoach/config/strava_app.json"
  "ClaudeCoach/config/backup.key"
  "ClaudeCoach/telegram/config.json"
  "ClaudeCoach/scripts/athletes.json"
)
# Glob patterns, matched against the whole path.
DENY_GLOB=(
  "*/strava_tokens.json"
  "*.pem"
  "*.env"
  "*.p12"
  "*id_rsa*"
  "*id_ed25519*"
)

if [ "$MODE" = "staged" ]; then
  LISTING="$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null)"
else
  LISTING="$(git ls-tree -r HEAD --name-only 2>/dev/null)"
fi

FAIL=0
while IFS= read -r path; do
  [ -n "$path" ] || continue
  for d in "${DENY_EXACT[@]}"; do
    if [ "$path" = "$d" ]; then
      echo "[secret-gate] BLOCK: deny-listed path present ($MODE): $path"
      FAIL=1
    fi
  done
  for g in "${DENY_GLOB[@]}"; do
    # shellcheck disable=SC2254
    case "$path" in
      $g)
        echo "[secret-gate] BLOCK: deny-listed pattern '$g' matched ($MODE): $path"
        FAIL=1
        ;;
    esac
  done
done <<< "$LISTING"

# --- layer 2: credential SHAPES in the files this push would add/change --------
# Pattern name|ERE. Every pattern requires a key name AND a value-shaped literal,
# so documentation placeholders ("icu_api_key": "...") cannot trip it.
PATTERNS=(
  'anthropic-api-key|sk-ant-[A-Za-z0-9_-]{20,}'
  'icu-api-key|"icu_api_key"[[:space:]]*:[[:space:]]*"[A-Za-z0-9]{12,}"'
  'oauth-client-secret|"client_secret"[[:space:]]*:[[:space:]]*"[A-Za-z0-9]{20,}"'
  'telegram-bot-token|"bot_token"[[:space:]]*:[[:space:]]*"[0-9]{6,}:[A-Za-z0-9_-]{20,}"'
  'strava-oauth-token|"(access|refresh)_token"[[:space:]]*:[[:space:]]*"[A-Za-z0-9]{20,}"'
  'private-key-block|BEGIN [A-Z ]*PRIVATE KEY'
)

scan_paths() {
  if [ "$MODE" = "staged" ]; then
    git diff --cached --name-only --diff-filter=ACMR 2>/dev/null
  elif git rev-parse --verify --quiet origin/main >/dev/null; then
    git diff --name-only --diff-filter=ACMR origin/main..HEAD 2>/dev/null
  else
    # No origin/main to compare against (fresh clone, unfetched). Fall back to
    # the whole tree rather than skipping - the gate must not go quiet.
    git ls-tree -r HEAD --name-only 2>/dev/null
  fi
}

while IFS= read -r path; do
  [ -n "$path" ] || continue
  if [ "$MODE" = "staged" ]; then
    content="$(git show ":$path" 2>/dev/null)" || continue
  else
    content="$(git show "HEAD:$path" 2>/dev/null)" || continue
  fi
  for entry in "${PATTERNS[@]}"; do
    name="${entry%%|*}"
    re="${entry#*|}"
    if printf '%s' "$content" | grep -Eq "$re"; then
      echo "[secret-gate] BLOCK: '$name' shape found in $path (value not printed)"
      FAIL=1
    fi
  done
done <<< "$(scan_paths)"

if [ "$FAIL" -ne 0 ]; then
  echo "[secret-gate] REFUSING to proceed against $REMOTE."
  echo "[secret-gate] Untrack the file (git rm --cached <path>), keep it on disk,"
  echo "[secret-gate] and add a matching .gitignore rule. Never 'git add -f' it."
  exit 1
fi

echo "[secret-gate] clean ($MODE)"
exit 0
