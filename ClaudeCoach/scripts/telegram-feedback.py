#!/usr/bin/env python3
"""
Poll Telegram for feedback replies and update session-log.json stubs.
Runs every 1 min via cron.

Flow:
  1. getUpdates — fetch new messages since last seen update_id
  2. Immediately send a Python-only ack ("Got it — parsing...")
  3. Claude parses the reply into structured fields (90s timeout)
  4. On success: Python writes to session-log.json, commits, sends confirmation
  5. On Claude failure/timeout: Python sends a diagnostic error — no Claude in error path
"""
import json, re, ssl, subprocess, sys, time, urllib.request, urllib.parse
from datetime import datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
SESSION_LOG     = BASE / "session-log.json"
STATE_FILE      = BASE / "telegram-feedback-state.json"
NOTIFY          = BASE / "telegram/notify.py"
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
CLAUDE_TIMEOUT  = 90   # seconds — must respond before Python sends the error fallback

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


# ── Telegram API ──────────────────────────────────────────────────────────────

def _api(method, **params):
    url  = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(params).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10, context=SSL) as r:
        return json.loads(r.read())


def _send(msg: str):
    """Send a plain-text Telegram message — pure Python, no Claude."""
    try:
        _api("sendMessage", chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as exc:
        pass  # if Telegram itself is down there's nothing to do


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"offset": 0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


# ── Telegram polling ──────────────────────────────────────────────────────────

def get_updates(offset: int) -> list:
    try:
        result = _api("getUpdates", offset=offset, timeout=0, limit=20)
        return result.get("result", [])
    except Exception:
        return []


# ── Session log ───────────────────────────────────────────────────────────────

def find_unfilled_stub():
    """Return (index, entry) for the most recent stub with rpe=null, or (None, None)."""
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


# ── Claude parse (with timeout) ───────────────────────────────────────────────

def parse_feedback(reply_text: str, stub: dict) -> tuple[dict, str | None]:
    """
    Returns (parsed_fields, error_message).
    error_message is None on success, a Python-generated string on failure.
    Claude is only involved in success path.
    """
    prompt = PARSE_PROMPT.format(
        sport=stub.get("sport", "Unknown"),
        name=stub.get("name", ""),
        reply=reply_text,
    )
    t_start = time.time()
    try:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", ""],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        elapsed = int(time.time() - t_start)
        return {}, (
            f"Parsing timed out after {elapsed}s — Claude may be overloaded or the API is down. "
            f"Your message has been saved as a note. Try again in a minute."
        )
    except FileNotFoundError:
        return {}, "Claude not found at /usr/bin/claude — check VM setup."
    except Exception as exc:
        return {}, f"Unexpected error launching Claude: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        return {}, (
            f"Claude exited with error {result.returncode}. "
            f"{'Stderr: ' + stderr if stderr else 'No error detail available.'} "
            f"Your message was saved as a note."
        )

    m = re.search(r'\{.*?\}', result.stdout, re.DOTALL)
    if not m:
        return {}, (
            f"Claude responded but returned no parseable fields. "
            f"Raw reply saved as a note. You can manually update the log."
        )

    try:
        return json.loads(m.group(0)), None
    except json.JSONDecodeError as exc:
        return {}, f"JSON parse error: {exc}. Raw reply saved as a note."


# ── Apply and commit ──────────────────────────────────────────────────────────

def apply_feedback(entries: list, idx: int, parsed: dict, raw_text: str) -> dict:
    stub = entries[idx]
    allowed = {"rpe", "feel", "ankle_pain_during", "ankle_pain_next_morning",
               "nutrition_g_carb", "hydration_ml", "notes"}
    for k, v in parsed.items():
        if k in allowed and v is not None:
            stub[k] = v
    # If Claude found nothing useful, at least store the raw text
    if not any(parsed.get(k) for k in ("rpe", "feel", "notes")):
        stub["notes"] = (stub.get("notes") or "") + f" [raw: {raw_text.strip()}]"
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


def confirmation_msg(stub: dict, parsed: dict) -> str:
    sport = stub.get("sport", "session")
    name  = stub.get("name", "")
    lines = [f"Logged for *{name}*"]
    if parsed.get("rpe"):
        lines.append(f"RPE {parsed['rpe']}/10")
    if parsed.get("feel"):
        lines.append(f"Feel: {parsed['feel']}")
    if sport == "Run" and parsed.get("ankle_pain_during") is not None:
        lines.append(f"Ankle during: {parsed['ankle_pain_during']}/10, next morning: {parsed.get('ankle_pain_next_morning', '?')}/10")
    if parsed.get("nutrition_g_carb"):
        lines.append(f"Nutrition: {parsed['nutrition_g_carb']}g carbs")
    if parsed.get("notes"):
        lines.append("Notes saved")
    return " — ".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    state  = load_state()
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
        if str(msg.get("chat", {}).get("id")) != CHAT_ID:
            continue
        if msg.get("from", {}).get("is_bot"):
            continue

        text = (msg.get("text") or "").strip()
        if not text or text.startswith("/"):
            continue

        idx, stub = find_unfilled_stub()
        if stub is None:
            continue  # no stub waiting — ignore message

        # ── Step 1: immediate Python-only ack (fires within the poll window) ──
        sport = stub.get("sport", "session")
        name  = stub.get("name", "")
        _send(f"Got it — logging your {sport.lower()} feedback for _{name}_...")

        # ── Step 2: Claude parse (90s timeout) ───────────────────────────────
        parsed, error = parse_feedback(text, stub)

        # ── Step 3a: Claude failed — Python-only diagnostic reply ─────────────
        if error:
            _send(f"Couldn't auto-parse: {error}")
            # Fall back: store raw text as note so nothing is lost
            parsed = {"notes": text.strip()}

        # ── Step 3b: Write, commit, confirm ──────────────────────────────────
        try:
            entries = json.loads(SESSION_LOG.read_text())
            updated = apply_feedback(entries, idx, parsed, text)
            SESSION_LOG.write_text(json.dumps(entries, indent=2))
            commit_and_push()
            if not error:
                _send(confirmation_msg(updated, parsed))
            else:
                _send(f"Raw reply saved as a note for _{name}_. You can tidy it up later.")
        except Exception as exc:
            _send(f"Saved to session log failed: {exc}. Message was: \"{text[:80]}\"")

    if new_offset != offset:
        state["offset"] = new_offset
        save_state(state)


if __name__ == "__main__":
    main()
