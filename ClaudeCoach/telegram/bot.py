#!/usr/bin/env python3
"""
ClaudeCoach Telegram bot - two-way interface.
Polls Telegram for messages, passes them to claude CLI, sends responses back.
Run: python3 bot.py
"""

import json, re, subprocess, sys, time, ssl
import urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime, date

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
try:
    import charts as _charts
except Exception:
    _charts = None

_whisper_model = None


def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
            log("Whisper model ready")
        except Exception as e:
            log(f"Whisper not available: {e}")
    return _whisper_model

CONFIG_FILE = BASE / "config.json"
HISTORY_FILE = BASE / "history.json"
SYSTEM_PROMPT_FILE = BASE / "system_prompt.txt"
LOG_FILE = BASE / "bot.log"

MAX_HISTORY_PAIRS = 6  # keep last 6 exchanges for context

RACE_DAY = date(2026, 9, 19)


def days_to_race():
    return (RACE_DAY - date.today()).days

QUICK_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "Today's plan",      "callback_data": "what's today's session?"},
            {"text": "How am I looking?", "callback_data": "how am I looking?"},
        ],
        [
            {"text": "This week",   "callback_data": "show me this week"},
            {"text": "Log session", "callback_data": "log session"},
        ],
    ]
}

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
    "mcp__claude_ai_icusync__get_power_curves",
    "mcp__claude_ai_icusync__get_extended_metrics",
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


def send(token, chat_id, text, parse_mode="Markdown", reply_markup=None):
    chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        tg_post(token, "sendMessage", payload)


def typing(token, chat_id):
    tg_post(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})


def answer_callback(token, callback_query_id):
    tg_post(token, "answerCallbackQuery", {"callback_query_id": callback_query_id})


def download_tg_file(token, file_id):
    info = tg_post(token, "getFile", {"file_id": file_id})
    file_path = info.get("result", {}).get("file_path")
    if not file_path:
        return None
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as r:
            return r.read()
    except Exception as e:
        log(f"File download error: {e}")
        return None


def transcribe_voice(audio_bytes):
    import os, tempfile
    model = get_whisper()
    if not model:
        return None
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_bytes)
        tmp = f.name
    try:
        segments, _ = model.transcribe(tmp, language="en")
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:
        log(f"Transcription error: {e}")
        return None
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def send_photo(token, chat_id, photo_bytes):
    boundary = "CCbound"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"chart.png\"\r\nContent-Type: image/png\r\n\r\n"
    ).encode() + photo_bytes + f"\r\n--{boundary}--\r\n".encode()
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"send_photo error: {e}")
        return {}


CHART_RE = re.compile(r'<<<CHART:(\w+):(.*?)>>>', re.DOTALL)


def process_charts(token, chat_id, response):
    """Send any [[CHART:TYPE:JSON]] images, return response with markers stripped."""
    if _charts is None:
        return CHART_RE.sub('', response).strip()
    for m in CHART_RE.finditer(response):
        chart_type, raw = m.group(1), m.group(2)
        try:
            data = json.loads(raw)
            png = None
            if chart_type in ("fitness", "form"):
                if isinstance(data, list):
                    data = {"data": data}
                if "today" not in data:
                    data["today"] = date.today().strftime("%m-%d")
                if chart_type == "fitness":
                    png = _charts.fitness_chart(data)
                else:
                    png = _charts.form_chart(data)
            elif chart_type == "session":
                png = _charts.session_chart(
                    data.get("name", "Session"),
                    data.get("intervals", []),
                    data.get("ftp", 316),
                )
            elif chart_type == "week":
                png = _charts.week_chart(
                    data.get("events", []),
                    title=data.get("title", "Training week"),
                    week_start=data.get("week_start"),
                )
            elif chart_type == "powercurve":
                png = _charts.power_curve_chart(
                    data.get("efforts", []),
                    ftp=data.get("ftp", 316),
                )
            if png:
                send_photo(token, chat_id, png)
        except Exception as e:
            log(f"Chart error ({chart_type}): {e}")
    return CHART_RE.sub('', response).strip()


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
    get_whisper()

    offset = 0
    while True:
        data = get_updates(token, offset)
        for update in data.get("result", []):
            offset = update["update_id"] + 1

            if "callback_query" in update:
                cq = update["callback_query"]
                chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                text = cq.get("data", "").strip()
                answer_callback(token, cq["id"])
            else:
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                if not text:
                    voice = msg.get("voice") or msg.get("audio")
                    if voice and chat_id == allowed_chat_id:
                        typing(token, chat_id)
                        raw = download_tg_file(token, voice["file_id"])
                        if raw:
                            text = transcribe_voice(raw) or ""
                            if text:
                                send(token, chat_id, f"_Heard: {text}_")

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
                     "- _what's my CTL?_",
                     reply_markup=QUICK_KEYBOARD)
                continue

            log(f"In: {text[:80]}")
            typing(token, chat_id)
            send(token, chat_id, "_On it..._")

            history = load_history()
            response = call_claude(text, config, history)

            clean = process_charts(token, chat_id, response)
            if clean:
                clean += f"\n\n— {days_to_race()} days to Cervia"
                send(token, chat_id, clean, reply_markup=QUICK_KEYBOARD)
            log(f"Out: {clean[:80]}")

            history.append({"user": text, "assistant": clean})
            save_history(history)

        time.sleep(1)


if __name__ == "__main__":
    main()
