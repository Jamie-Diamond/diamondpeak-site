#!/usr/bin/env python3
"""
ClaudeCoach Telegram bot - two-way interface.
Polls Telegram for messages, passes them to claude CLI, sends responses back.
Run: python3 bot.py
"""

import json, subprocess, sys, time, ssl
import urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
HISTORY_FILE = BASE / "history.json"
SYSTEM_PROMPT_FILE = BASE / "system_prompt.txt"
LOG_FILE = BASE / "bot.log"

MAX_HISTORY_PAIRS = 6  # keep last 6 exchanges for context

TOOLS = ",".join([
    "Read", "Write", "Edit",
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_fitness",
    "mcp__claude_ai_icusync__get_training_history",
    "mcp__claude_ai_icusync__get_wellness",
    "mcp__claude_ai_icusync__get_activity_detail",
    "mcp__claude_ai_icusync__get_events",
    "mcp__claude_ai_icusync__push_workout",
    "mcp__claude_ai_icusync__edit_workout",
    "mcp__claude_ai_icusync__delete_workout",
    "mcp__claude_ai_icusync__get_best_efforts",
    "mcp__claude_ai_icusync__get_training_summary",
])


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_config():
    return json.loads(CONFIG_FILE.read_text())


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history[-MAX_HISTORY_PAIRS:], indent=2))


def tg_post(token, method, payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"tg_post {method} error: {e}")
        return {}


def tg_get(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=40, context=SSL_CONTEXT) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"tg_get {method} error: {e}")
        return {"result": []}


def send(token, chat_id, text, parse_mode="Markdown"):
    for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
        tg_post(token, "sendMessage", {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode})


def typing(token, chat_id):
    tg_post(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})


def call_claude(user_message, config, history):
    system_prompt = SYSTEM_PROMPT_FILE.read_text().strip()

    parts = [system_prompt, ""]

    if history:
        parts.append("Recent conversation:")
        for h in history:
            parts.append(f"Jamie: {h['user']}")
            parts.append(f"ClaudeCoach: {h['assistant']}")
        parts.append("")

    parts.append(f"Jamie: {user_message}")

    full_prompt = "\n".join(parts)

    try:
        result = subprocess.run(
            [config["claude_binary"], "-p", full_prompt, "--allowedTools", TOOLS],
            capture_output=True,
            text=True,
            cwd=config["project_dir"],
            timeout=300
        )
        return result.stdout.strip() or result.stderr.strip() or "(no response)"
    except subprocess.TimeoutExpired:
        return "Sorry, that took too long. Try a simpler question or break it into steps."
    except Exception as e:
        log(f"Claude error: {e}")
        return f"Error calling claude: {e}"


def get_updates(token, offset):
    return tg_get(token, "getUpdates", {"offset": offset, "timeout": 30})


def main():
    config = load_config()

    if config["bot_token"] == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("Edit ClaudeCoach/telegram/config.json with your bot token and chat ID first.")
        sys.exit(1)

    token = config["bot_token"]
    allowed_chat_id = str(config["chat_id"])

    log(f"ClaudeCoach bot started. Listening for messages from chat {allowed_chat_id}.")
    send(token, allowed_chat_id, "ClaudeCoach online. What do you need?")

    offset = 0
    while True:
        data = get_updates(token, offset)
        for update in data.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()

            if chat_id != allowed_chat_id or not text:
                continue

            if text.lower() in ("/start", "/help"):
                send(token, chat_id,
                     "*ClaudeCoach* - IM Cervia 2026\n\n"
                     "Ask me anything about training. Examples:\n"
                     "- _how am I looking this week?_\n"
                     "- _log session_ (after a key workout)\n"
                     "- _what's today's session?_\n"
                     "- _log heat session 30 min_\n"
                     "- _what's my CTL?_")
                continue

            log(f"In: {text[:80]}")
            typing(token, chat_id)

            history = load_history()
            response = call_claude(text, config, history)

            send(token, chat_id, response)
            log(f"Out: {response[:80]}")

            history.append({"user": text, "assistant": response})
            save_history(history)

        time.sleep(1)


if __name__ == "__main__":
    main()
