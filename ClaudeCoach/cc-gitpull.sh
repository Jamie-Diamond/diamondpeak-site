#!/bin/bash
cd /Users/diamondpeakconsulting/diamondpeak-site

# Auto-commit VM-side state changes (data files only — not config or scripts)
if ! git diff --quiet -- ClaudeCoach/athletes/ ClaudeCoach/training-data*.json || \
   ! git diff --staged --quiet -- ClaudeCoach/athletes/ ClaudeCoach/training-data*.json; then
  git add ClaudeCoach/athletes/ ClaudeCoach/training-data*.json
  git commit -m "auto-save: pre-pull state $(date +%Y-%m-%dT%H:%M)"
fi

# Merge (not rebase) — avoids conflicts during active development sessions
git fetch origin
git merge origin/main --no-edit
git push origin main
