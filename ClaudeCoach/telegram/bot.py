#!/usr/bin/env python3
"""
ClaudeCoach Telegram bot - two-way interface.
Polls Telegram for messages, passes them to claude CLI, sends responses back.
Run: python3 bot.py
"""

import json, re, subprocess, sys, time, ssl, os
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

def build_keyboard():
    now  = datetime.now()
    hour = now.hour
    wday = now.weekday()  # 0=Mon … 6=Sun

    # Post-session: within 3 hours of last notified activity
    last_act_file = BASE.parent / "last_activity_state.json"
    post_session = False
    if last_act_file.exists():
        try:
            st = json.loads(last_act_file.read_text())
            ts = st.get("notified_at")
            if ts:
                notified = datetime.fromisoformat(ts)
                post_session = (now - notified).total_seconds() < 10800
        except Exception:
            pass

    if post_session:
        rows = [
            [{"text": "Log RPE + feel", "callback_data": "log session"},
             {"text": "Ankle score?",   "callback_data": "ankle score for last run?"}],
            [{"text": "How am I looking?", "callback_data": "how am I looking?"},
             {"text": "This week",         "callback_data": "show me this week"}],
        ]
    elif wday == 6 and hour >= 18:  # Sunday evening
        rows = [
            [{"text": "Week review",    "callback_data": "show me this week"},
             {"text": "Next week plan", "callback_data": "what's the plan for next week?"}],
            [{"text": "How am I looking?", "callback_data": "how am I looking?"},
             {"text": "Log session",       "callback_data": "log session"}],
        ]
    elif 5 <= hour < 10:  # morning
        rows = [
            [{"text": "Today's session", "callback_data": "what's today's session?"},
             {"text": "Log weight",      "callback_data": "log weight"}],
            [{"text": "How am I looking?", "callback_data": "how am I looking?"},
             {"text": "This week",         "callback_data": "show me this week"}],
        ]
    else:
        rows = [
            [{"text": "Today's plan",      "callback_data": "what's today's session?"},
             {"text": "How am I looking?", "callback_data": "how am I looking?"}],
            [{"text": "This week",   "callback_data": "show me this week"},
             {"text": "Log session", "callback_data": "log session"}],
        ]
    return {"inline_keyboard": rows}

TOOLS = ",".join([
    "Read", "Write", "Edit", "Bash",
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
            elif chart_type == "load":
                png = _charts.load_chart(data)
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


PROJECT_DIR = BASE.parent.parent  # diamondpeak-site/
STATE_JSON  = BASE.parent / "current-state.json"
HEAT_LOG    = BASE.parent / "heat-log.json"

_ANKLE_RE    = re.compile(r'^(ankle|pain|niggle)\s+(\w+[\s\w]*?)\s+(\d+(?:\.\d+)?)\s*$', re.I)
_WEIGHT_RE   = re.compile(r'^(?:weight|kg|weigh(?:ed)?)\s+([\d.]+)\s*(?:kg)?\s*$', re.I)
_HEAT_RE     = re.compile(r'^(?:heat|bath)\s+([\d.]+)\s*(?:min|m)?\s*$', re.I)
_PLAN_RE     = re.compile(r'^(?:generate\s+plan|plan\s+(?:next\s+)?(?:2\s+)?weeks?|plan\s+ahead)\s*$', re.I)
_FTP_RE      = re.compile(r'^(?:ftp\s+(?:retest|result|update|new)|new\s+ftp)\s+([\d.]+)\s*(?:w(?:atts?)?)?\s*$', re.I)

GENERATE_PLAN_SCRIPT = BASE.parent.parent / "ClaudeCoach/scripts/generate-plan.sh"

def _git_commit(msg):
    try:
        subprocess.run(
            ["git", "add", "ClaudeCoach/"],
            cwd=str(PROJECT_DIR), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(PROJECT_DIR), capture_output=True
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(PROJECT_DIR), capture_output=True
        )
    except Exception as e:
        log(f"git commit error: {e}")

def _load_state_json():
    if STATE_JSON.exists():
        return json.loads(STATE_JSON.read_text())
    return {}

def _save_state_json(state):
    STATE_JSON.write_text(json.dumps(state, indent=2))

def fast_path(text):
    """
    Returns a reply string if the message can be handled without calling Claude,
    or None if Claude should handle it.
    """
    today = date.today().isoformat()

    m = _ANKLE_RE.match(text.strip())
    if m:
        location = m.group(2).strip() if m.group(1).lower() != "ankle" else "ankle"
        score = float(m.group(3))
        state = _load_state_json()
        prev = state.get("ankle", {}).get("pain_during")
        state.setdefault("ankle", {})["pain_during"] = int(score)
        state["last_updated"] = today
        _save_state_json(state)
        _git_commit(f"auto: ankle pain {score} {today}")
        trend = ""
        if prev is not None:
            if score > prev:
                trend = f" (up from {prev} — monitor)"
            elif score < prev:
                trend = f" (down from {prev} — improving)"
        return f"Logged {location} pain {int(score)}/10{trend}."

    m = _WEIGHT_RE.match(text.strip())
    if m:
        kg = float(m.group(1))
        state = _load_state_json()
        state.setdefault("weight_readings", []).append({"date": today, "kg": kg})
        state["last_updated"] = today
        _save_state_json(state)
        _git_commit(f"auto: weight {kg} kg {today}")
        target = 79.0
        diff = round(kg - target, 1)
        return f"Logged {kg} kg. {diff:+.1f} kg to race-day target (79 kg)."

    m = _HEAT_RE.match(text.strip())
    if m:
        mins = int(float(m.group(1)))
        entries = json.loads(HEAT_LOG.read_text()) if HEAT_LOG.exists() else []
        entries.append({"date": today, "duration_min": mins, "temp_c": 40, "hr_peak": None, "notes": ""})
        HEAT_LOG.write_text(json.dumps(entries, indent=2))
        state = _load_state_json()
        state.setdefault("heat", {})
        state["heat"]["sessions_cumulative"] = state["heat"].get("sessions_cumulative", 0) + 1
        state["heat"]["last_session_date"] = today
        state["last_updated"] = today
        _save_state_json(state)
        _git_commit(f"auto: heat session {mins}min {today}")
        total = state["heat"]["sessions_cumulative"]
        remaining = max(0, 14 - total)
        return f"Logged heat session {mins} min. {total} done" + (f" — {remaining} still needed to hit 14-session floor." if remaining else " — above minimum, keep banking.")

    if _PLAN_RE.match(text.strip()):
        return "__GENERATE_PLAN__"

    m = _FTP_RE.match(text.strip())
    if m:
        return f"__FTP_RETEST__:{m.group(1)}"

    return None


def _update_ftp(new_ftp: int) -> str:
    """Update FTP across local files and current-state.json. Returns reply string."""
    today = date.today().isoformat()
    updated = []
    errors = []

    # system_prompt.txt — "FTP 316 W" style
    try:
        sp = SYSTEM_PROMPT_FILE.read_text()
        sp_new = re.sub(r'FTP \d+ W', f'FTP {new_ftp} W', sp)
        if sp_new != sp:
            SYSTEM_PROMPT_FILE.write_text(sp_new)
            updated.append("system_prompt.txt")
    except Exception as e:
        errors.append(f"system_prompt: {e}")

    # reference/rules.md — "Bike FTP: 316 W" style
    rules_path = BASE.parent / "reference/rules.md"
    try:
        rules = rules_path.read_text()
        rules_new = re.sub(r'Bike FTP: \d+ W', f'Bike FTP: {new_ftp} W', rules)
        if rules_new != rules:
            rules_path.write_text(rules_new)
            updated.append("rules.md")
    except Exception as e:
        errors.append(f"rules.md: {e}")

    # current-state.json
    try:
        state = _load_state_json()
        prev_ftp = state.get("bike_ftp")
        state["bike_ftp"] = new_ftp
        state["last_updated"] = today
        action = {"id": "ftp-retest", "action": f"FTP updated {prev_ftp}→{new_ftp} W", "due": today, "status": "done"}
        acts = state.setdefault("open_actions", [])
        existing = next((a for a in acts if a.get("id") == "ftp-retest"), None)
        if existing:
            existing.update(action)
        else:
            acts.append(action)
        _save_state_json(state)
        updated.append("current-state.json")
    except Exception as e:
        errors.append(f"current-state.json: {e}")

    _git_commit(f"ftp: updated to {new_ftp} W {today}")

    if errors:
        return f"FTP updated to *{new_ftp} W* in {', '.join(updated)}.\n⚠️ Errors: {'; '.join(errors)}\n\nZones recalculated. Update in Intervals.icu if not already done."

    zones_note = (
        f"Z2 bike: {round(new_ftp*0.55)}–{round(new_ftp*0.75)} W · "
        f"Threshold: {round(new_ftp*0.90)}–{round(new_ftp*1.05)} W"
    )
    return (
        f"FTP updated to *{new_ftp} W* in {', '.join(updated)}.\n\n"
        f"{zones_note}\n\n"
        f"Remember to update in Intervals.icu too — Settings → Athlete → FTP. "
        f"Say _generate plan_ to push updated sessions."
    )


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
                     reply_markup=build_keyboard())
                continue

            log(f"In: {text[:80]}")

            fast = fast_path(text)
            if fast == "__GENERATE_PLAN__":
                send(token, chat_id, "_Generating plan — this takes a few minutes…_")
                try:
                    result = subprocess.run(
                        ["bash", str(GENERATE_PLAN_SCRIPT)],
                        capture_output=True, text=True,
                        cwd=str(PROJECT_DIR), timeout=600,
                    )
                    out = (result.stdout or result.stderr or "Done.").strip()
                    send(token, chat_id, out[:4096], reply_markup=build_keyboard())
                except Exception as e:
                    send(token, chat_id, f"Plan generation failed: {e}", reply_markup=build_keyboard())
                log("Out (fast): plan generated")
                continue
            elif fast and fast.startswith("__FTP_RETEST__:"):
                new_ftp = int(float(fast.split(":", 1)[1]))
                reply = _update_ftp(new_ftp)
                send(token, chat_id, reply, reply_markup=build_keyboard())
                log(f"Out (FTP update): {new_ftp} W")
                continue
            elif fast:
                send(token, chat_id, fast, reply_markup=build_keyboard())
                log(f"Out (fast): {fast[:80]}")
                continue

            typing(token, chat_id)
            send(token, chat_id, "_On it..._")

            history = load_history()
            response = call_claude(text, config, history)

            clean = process_charts(token, chat_id, response)
            if clean:
                send(token, chat_id, clean, reply_markup=build_keyboard())
            log(f"Out: {clean[:80]}")

            history.append({"user": text, "assistant": clean})
            save_history(history)

        time.sleep(1)


if __name__ == "__main__":
    main()
