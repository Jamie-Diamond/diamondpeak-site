#!/usr/bin/env python3
"""
Poll Telegram for feedback replies and update session-log.json stubs.
Runs every 5 min via cron.

Flow:
  1. getUpdates — fetch new messages since last seen update_id
  2. For each user message, find the most recent unfilled stub
  3. Claude parses the reply into structured fields
  4. Python writes to session-log.json and commits
  5. Send a short confirmation back to Telegram
"""
import json, re, ssl, subprocess, sys, urllib.request, urllib.parse
from datetime import datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
SESSION_LOG     = BASE / "session-log.json"
STATE_FILE      = BASE / "telegram-feedback-state.json"
NOTIFY          = BASE / "telegram/notify.py"
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"

_cfg    = json.loads((BASE / "telegram/config.json").read_text())
TOKEN   = _cfg["bot_token"]
CHAT_ID = str(_cfg["chat_id"])

_cafile = "/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None
SSL     = ssl.create_default_context(cafile=_cafile)

PARSE_PROMPT = """Parse this Telegram reply from an endurance athlete into structured session feedback.

Sport: {sport}
Session name: {name}

Reply text: {reply!r}

Extract ONLY what is clearly stated — do not infer or default anything not mentioned.
Output a JSON object with these fields (omit a field entirely if not mentioned):
  "rpe": integer 1-10
  "feel": string (qualitative description in athlete's own words)
  "ankle_pain_during": integer 1-10  (runs only)
  "ankle_pain_next_morning": integer 1-10  (runs only)
  "nutrition_g_carb": integer grams  (rides only)
  "hydration_ml": integer ml  (rides only)
  "notes": string (anything else worth keeping)

Output ONLY the JSON object. No other text."""


def _api(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10, context=SSL) as r:
        return json.loads(r.read())


def _notify(msg):
    try:
        subprocess.run(["python3", str(NOTIFY), msg], cwd=PROJECT_DIR, timeout=15)
    except Exception:
        pass


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"offset": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def get_updates(offset):
    try:
        result = _api("getUpdates", offset=offset, timeout=0, limit=20)
        return result.get("result", [])
    except Exception as exc:
        return []


def find_unfilled_stub():
    """Most recent stub where rpe is null."""
    if not SESSION_LOG.exists():
        return None, None
    try:
        entries = json.loads(SESSION_LOG.read_text())
    except Exception:
        return None, None
    for i, e in enumerate(entries):
        if e.get("stub") and e.get("rpe") is None:
            return i, e
    return None, None


def parse_feedback(reply_text, stub):
    """Ask Claude to parse natural-language reply into structured fields."""
    prompt = PARSE_PROMPT.format(
        sport=stub.get("sport", "Unknown"),
        name=stub.get("name", ""),
        reply=reply_text,
    )
    try:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", ""],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=30,
        )
        m = re.search(r'\{.*?\}', result.stdout, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return {}


def apply_feedback(entries, idx, parsed, reply_text):
    """Merge parsed fields into the stub entry."""
    stub = entries[idx]
    allowed = {"rpe", "feel", "ankle_pain_during", "ankle_pain_next_morning",
               "nutrition_g_carb", "hydration_ml", "notes"}
    for k, v in parsed.items():
        if k in allowed and v is not None:
            stub[k] = v
    # If Claude returned nothing useful, store raw reply in notes
    if not any(parsed.get(k) for k in ("rpe", "feel", "notes")):
        stub["notes"] = reply_text.strip()
    stub["stub"] = False
    stub["logged_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    entries[idx] = stub
    return stub


def commit_and_push():
    for cmd in [
        ["git", "add", "ClaudeCoach/session-log.json"],
        ["git", "commit", "-m", f"feedback: Telegram reply {datetime.now().strftime('%Y-%m-%d')}"],
        ["git", "fetch", "origin"],
        ["git", "rebase", "--autostash", "origin/main"],
        ["git", "push", "origin", "main"],
    ]:
        r = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            break


def confirmation_msg(stub, parsed):
    sport  = stub.get("sport", "session")
    name   = stub.get("name", "")
    rpe    = parsed.get("rpe")
    feel   = parsed.get("feel", "")
    ankle  = (parsed.get("ankle_pain_during"), parsed.get("ankle_pain_next_morning"))

    lines = [f"Logged for *{name}*"]
    if rpe:
        lines.append(f"RPE {rpe}/10")
    if feel:
        lines.append(f"Feel: {feel}")
    if sport == "Run" and ankle[0] is not None:
        lines.append(f"Ankle during: {ankle[0]}/10, next morning: {ankle[1]}/10")
    if parsed.get("nutrition_g_carb"):
        lines.append(f"Nutrition: {parsed['nutrition_g_carb']}g carbs")
    if parsed.get("notes"):
        lines.append(f"Notes saved")
    return " — ".join(lines)


def main():
    state = load_state()
    offset = state.get("offset", 0)

    updates = get_updates(offset)
    if not updates:
        return

    new_offset = offset
    for update in updates:
        new_offset = max(new_offset, update["update_id"] + 1)

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue

        # Only process messages from the configured chat
        if str(msg.get("chat", {}).get("id")) != CHAT_ID:
            continue

        # Skip bot's own messages
        if msg.get("from", {}).get("is_bot"):
            continue

        text = (msg.get("text") or "").strip()
        if not text:
            continue

        # Skip slash commands — those are for other bots/scripts
        if text.startswith("/"):
            continue

        idx, stub = find_unfilled_stub()
        if stub is None:
            # No unfilled stubs — ignore or send a gentle note
            continue

        parsed = parse_feedback(text, stub)

        # Only write if we got something meaningful
        if not parsed:
            _notify(f"Got your message but couldn't parse it — could you reply with just the RPE (1–10)?")
            continue

        entries = json.loads(SESSION_LOG.read_text())
        updated_stub = apply_feedback(entries, idx, parsed, text)
        SESSION_LOG.write_text(json.dumps(entries, indent=2))

        commit_and_push()
        _notify(confirmation_msg(updated_stub, parsed))

    if new_offset != offset:
        state["offset"] = new_offset
        save_state(state)


if __name__ == "__main__":
    main()
