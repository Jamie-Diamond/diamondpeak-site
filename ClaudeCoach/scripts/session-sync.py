#!/usr/bin/env python3
"""
Session sync — runs hourly (07:00-22:00) via VM crontab.

Reads the last N message pairs from history.json, extracts any new coaching rules
that weren't written during the session, prunes expired/stale entries, and alerts
Jamie if ClaudeCoach made promises it hasn't confirmed completing.
"""
import json, re, subprocess, sys
from datetime import date, datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MODEL   = "claude-sonnet-4-6"
TOOLS   = "Read,Write,Edit"


def _build_prompt(slug: str, first_name: str, history: list, today: str) -> str:
    # Format recent messages
    msg_lines = []
    for pair in history:
        u = pair.get("user", "").strip()
        a = pair.get("assistant", "").strip()
        if u:
            msg_lines.append(f"{first_name}: {u}")
        if a:
            msg_lines.append(f"ClaudeCoach: {a}")
    messages = "\n".join(msg_lines) if msg_lines else "(no messages)"

    rules_file    = BASE / f"athletes/{slug}/persistent-rules.md"
    state_file    = BASE / f"athletes/{slug}/current-state.md"

    return f"""\
Session sync — {today}

You are the ClaudeCoach session sync. Review the recent conversation and maintain two persistent files.

== RECENT MESSAGES ==
{messages}

== TASKS ==

1. SCAN for new rules or preferences {first_name} stated or ClaudeCoach agreed to.
   Read {rules_file} first to avoid duplicates.
   For each genuinely new rule: append one line to {rules_file} using the Edit tool.
   Format: [perm] <rule text>                    — permanent, no expiry
       OR: [expires:YYYY-MM-DD] <rule text>      — event/block specific; use event end date
   Append only — never rewrite or remove existing lines.
   Do NOT add rules already captured in the file, even in slightly different wording.

2. PRUNE expired entries from {rules_file}.
   Remove any line where [expires:YYYY-MM-DD] date is strictly before today ({today}).
   Use the Edit tool to remove those lines only. Leave all [perm] lines untouched.

3. PRUNE stale entries from {state_file}:
   - Travel/training block table rows where the block end date + 7 days < {today} → remove the row
   - Open actions where status = done AND the completion date > 7 days ago → remove the entry
   Use the Edit tool for surgical removals — never rewrite whole sections.
   If nothing qualifies for pruning, skip this task entirely.

4. SCAN for unverified promises: look for ClaudeCoach responses containing "Done.", "Updated.",
   "Renamed.", "Noted and saved.", "Got it —" or similar that imply a file write or external API
   action, where no subsequent {first_name} message confirms it actually happened.
   List any such items in <sync-alert>...</sync-alert> tags.
   If none found, produce no text output — absolute silence.

OUTPUT FORMAT:
- Use tools to write/edit files for tasks 1-3.
- Task 4 only: if unverified promises exist, output <sync-alert>Outstanding: [brief list]</sync-alert>
- Otherwise: no text output. Absolute silence.
"""


def notify(msg: str, chat_id: str) -> None:
    try:
        subprocess.run(
            ["python3", str(NOTIFY), "--chat-id", str(chat_id), msg],
            cwd=PROJECT_DIR, timeout=15,
        )
    except Exception:
        pass


def run_athlete(slug: str, athlete_cfg: dict) -> None:
    adir     = BASE / f"athletes/{slug}"
    chat_id  = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "session-sync.log"

    history_file = adir / "telegram/history.json"
    if not history_file.exists():
        return

    try:
        history = json.loads(history_file.read_text())
    except Exception as e:
        print(f"[{slug}] Failed to read history: {e}", file=sys.stderr)
        return

    if not history:
        return

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass
    first_name = profile.get("name", slug).split()[0]

    today  = date.today().isoformat()
    prompt = _build_prompt(slug, first_name, history, today)

    with open(log_file, "a") as lf:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lf.write(f"[{ts}] [{slug}] running sync\n")
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS,
             "--model", MODEL, "--no-session-persistence"],
            stdout=subprocess.PIPE, stderr=lf, text=True,
            cwd=PROJECT_DIR, timeout=300,
        )

    output = (result.stdout or "").strip()
    if not output:
        return

    m = re.search(r'<sync-alert>(.*?)</sync-alert>', output, re.DOTALL | re.IGNORECASE)
    if m and chat_id:
        alert = m.group(1).strip()
        notify(f"_Sync check:_ {alert}", chat_id)

    with open(log_file, "a") as lf:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lf.write(f"[{ts}] [{slug}] output: {output[:200]}\n")


def main() -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] session-sync starting", file=sys.stderr)

    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
    except Exception as e:
        print(f"[{ts}] Failed to load athletes config: {e}", file=sys.stderr)
        sys.exit(1)

    for slug, cfg in athletes.items():
        if not cfg.get("active", True):
            continue
        try:
            run_athlete(slug, cfg)
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] session-sync error: {exc}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
