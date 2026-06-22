# ClaudeCoach — Nightly Bug-Fixer

**Status:** Live since 2026-06-22. Stage 1 + Stage 2 deployed; midnight cron on.

Each midnight it reads the bug/feedback log, triages and consolidates it, drafts fixes for the
clearly-fixable items on throwaway branches, and sends Jamie a Telegram review card per fix.
**Nothing merges or deploys without Jamie's explicit Yes tap.**

The log is `athletes/<slug>/feedback-log.json` — entries Jamie creates by sending `bug:`,
`feature:` or `feedback:` messages to the Telegram bot.

## Two stages

### Stage 1 — triage / consolidate / plan (read-only)
`scripts/bug-fixer.py` (no flag). An agent with **Read,Bash** only:
- checks git history + the codebase for what's already fixed → marks `already_resolved` with evidence;
- **consolidates** open entries that share a root cause into one work group (not one fix per raw report);
- classifies each group `fixable_now` / `needs_human` / `already_resolved` with a plan.
Outputs a `<plan>` JSON. Never edits anything.

### Stage 2 — fix + review (`--fix`)
For each `fixable_now` group:
1. create a git **worktree** on branch `bugfix/<date>-<n>`;
2. an agent (**Read,Write,Edit,Bash**) implements the plan in that worktree;
3. compile-check changed `.py`; commit on the branch; record the review in `.bug-reviews.json`;
4. remove the worktree (branch persists), post a Telegram card: bug + summary + diff stat + ✅/❌/✏️.

Dedup: a group whose log entries already have a review is skipped (no nightly re-posting; the log is
append-only so entry indices are stable).

## Review flow (`telegram/bot.py` → `_handle_bugfix`, callbacks `bf:yes|no|edit:<id>`)
- **✅ Yes** → merge `bugfix/<id>` → `main` (`--no-ff`; aborts safely on conflict), push, mark the
  feedback entries `resolved`, delete the branch. Restart the bot service **only** if changed files
  are under `telegram/` or `lib/`.
- **❌ No** → delete the branch, mark `dismissed`.
- **✏️ Edit** → the bot captures Jamie's next message as the revision →
  `bug-fixer.py --refix <id> "<instruction>"` re-runs the agent on the branch and re-posts the card.

## Safety model
- The cron only **drafts** + posts cards. Merging to `main` and any deploy need Jamie's Yes —
  validated end-to-end 2026-06-22 (merge commit `d3c7adf`).
- Only a branch recorded `awaiting` in `.bug-reviews.json` can be merged (not an arbitrary ref).
- A fix that doesn't compile is discarded before any card is posted.
- `--dry-run` builds branches then discards them (no post, no merge) — for testing.

## Files
| File | Role |
|---|---|
| `scripts/bug-fixer.py` | planner + fixer (`--fix`) + revise (`--refix`) |
| `.bug-reviews.json` (in `ClaudeCoach/`, gitignored) | review state: awaiting / merged / dismissed |
| `athletes/<slug>/feedback-log.json` | source log; entries gain a `status` field on resolve/dismiss |
| `telegram/bot.py` | `_handle_bugfix` + the Edit follow-up interception |

## Operate
- **Cron (VM root crontab):** `0 0 * * * python3 …/ClaudeCoach/scripts/bug-fixer.py --fix >> ~/Library/Logs/ClaudeCoach/bug-fixer.log 2>&1`. Auth via the crontab `CLAUDE_CODE_OAUTH_TOKEN` line.
- **Pause:** remove that crontab line.
- **Run by hand:** `--fix` (real), `--fix --dry-run` (safe test), no flag (print triage), `--json` (raw plan).
- **Scope:** only `fixable_now` groups get cards; `needs_human` items show in the triage but aren't auto-carded (they need a decision from Jamie). A nightly "N items need your input" summary is a possible future add.
