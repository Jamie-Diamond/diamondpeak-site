#!/usr/bin/env python3
"""
ClaudeCoach Telegram bot - two-way interface.
Polls Telegram for messages, passes them to claude CLI, sends responses back.
Run: python3 bot.py
"""

import json, re, subprocess, sys, time, ssl, os
import urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime, date, timedelta

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
ATHLETES_CONFIG = BASE.parent / "config/athletes.json"
LOG_FILE = BASE / "bot.log"

# Fallback paths (used by fast_path / _update_ftp for single-athlete globals)
HISTORY_FILE = BASE.parent / "athletes/jamie/telegram/history.json"
SYSTEM_PROMPT_FILE = BASE.parent / "athletes/jamie/system_prompt.txt"

MAX_HISTORY_PAIRS = 12  # keep last 12 exchanges for context

MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

RACE_DAY = date(2026, 9, 19)

_MODEL_LABEL = {
    MODEL_HAIKU:  "H",
    MODEL_SONNET: "S",
    "claude-opus-4-7": "O4.7",
}

def response_footer(model: str) -> str:
    days = (RACE_DAY - date.today()).days
    label = _MODEL_LABEL.get(model, model.split("-")[1][0].upper())
    return f"\n_{days} days to go · {label}_"

# Messages that only need a simple lookup — Haiku handles these
_SIMPLE_QUERY_RE = re.compile(
    r"^(what'?s?\s+)?(today'?s?\s+)?(session|plan|workout|schedule)\b"
    r"|^show\s+(me\s+)?this\s+week\b"
    r"|^(this|next)\s+week\b"
    r"|^what'?s?\s+(my\s+)?(tsb|ctl|atl|form|fitness)\b"
    r"|^how\s+am\s+i\s+(looking|doing)\b"
    r"|^(am\s+i\s+on\s+track|on\s+track)\b",
    re.IGNORECASE,
)

def select_model(text: str) -> str:
    if _SIMPLE_QUERY_RE.match(text.strip()):
        return MODEL_HAIKU
    return MODEL_SONNET


def build_keyboard():
    now  = datetime.now()
    hour = now.hour
    wday = now.weekday()  # 0=Mon … 6=Sun

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
        buttons = [
            {"text": "Log RPE + feel",    "callback_data": "log session"},
            {"text": "How am I looking?", "callback_data": "how am I looking?"},
        ]
    elif wday == 6 and hour >= 18:  # Sunday evening
        buttons = [
            {"text": "Week review",    "callback_data": "show me this week"},
            {"text": "Next week plan", "callback_data": "what's the plan for next week?"},
        ]
    elif 5 <= hour < 10:  # morning
        buttons = [
            {"text": "Today's session",   "callback_data": "what's today's session?"},
            {"text": "How am I looking?", "callback_data": "how am I looking?"},
        ]
    else:
        buttons = [
            {"text": "Today's plan",      "callback_data": "what's today's session?"},
            {"text": "How am I looking?", "callback_data": "how am I looking?"},
        ]
    return {"inline_keyboard": [buttons]}

TOOLS = ",".join([
    "Read", "Write", "Edit", "Bash",
    # IcuSync MCP — available as fallback; prefer icu_fetch.py for speed
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_fitness",
    "mcp__claude_ai_icusync__get_wellness",
    "mcp__claude_ai_icusync__get_training_history",
    "mcp__claude_ai_icusync__get_events",
    "mcp__claude_ai_icusync__get_activity_detail",
    "mcp__claude_ai_icusync__get_extended_metrics",
    "mcp__claude_ai_icusync__get_best_efforts",
    "mcp__claude_ai_icusync__get_power_curves",
    "mcp__claude_ai_icusync__get_training_summary",
    "mcp__claude_ai_icusync__push_workout",
    "mcp__claude_ai_icusync__edit_workout",
    "mcp__claude_ai_icusync__delete_workout",
])


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_config():
    return json.loads(CONFIG_FILE.read_text())


def load_athletes():
    """Returns {chat_id: athlete_record} from athletes.json."""
    if not ATHLETES_CONFIG.exists():
        return {}
    athletes = json.loads(ATHLETES_CONFIG.read_text())
    return {
        str(a["chat_id"]): {**a, "slug": slug}
        for slug, a in athletes.items()
        if a.get("active") and a.get("chat_id")
    }

def athlete_files(slug):
    """Return per-athlete Path objects for history and system prompt."""
    adir = BASE.parent / "athletes" / slug
    return {
        "history":       adir / "telegram/history.json",
        "system_prompt": adir / "system_prompt.txt",
    }

def load_history(history_file=None):
    f = Path(history_file) if history_file else HISTORY_FILE
    if f.exists():
        return json.loads(f.read_text())
    return []


def save_history(history, history_file=None):
    f = Path(history_file) if history_file else HISTORY_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(history[-MAX_HISTORY_PAIRS:], indent=2))


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
STATE_JSON    = BASE.parent / "athletes/jamie/current-state.json"
HEAT_LOG      = BASE.parent / "athletes/jamie/heat-log.json"
SESSION_LOG_F = BASE.parent / "athletes/jamie/session-log.json"
TRAINING_DATA = BASE.parent / "athletes/jamie/training-data.json"
SITE_DATA     = BASE.parent / "site-data.json"

_ANKLE_RE    = re.compile(r'^(ankle|pain|niggle)\s+(\w+[\s\w]*?)\s+(\d+(?:\.\d+)?)\s*$', re.I)
_WEIGHT_RE   = re.compile(r'^(?:weight|kg|weigh(?:ed)?)\s+([\d.]+)\s*(?:kg)?\s*$', re.I)
_HEAT_RE     = re.compile(r'^(?:heat|bath)\s+([\d.]+)\s*(?:min|m)?\s*$', re.I)
_PLAN_RE     = re.compile(r'^(?:generate\s+plan|plan\s+(?:next\s+)?(?:2\s+)?weeks?|plan\s+ahead)\s*$', re.I)
_FTP_RE      = re.compile(r'^(?:ftp\s+(?:retest|result|update|new)|new\s+ftp)\s+([\d.]+)\s*(?:w(?:atts?)?)?\s*$', re.I)
_WEEK_CMD_RE = re.compile(r'^/week\s*$', re.I)
_FORM_CMD_RE = re.compile(r'^/form\s*$', re.I)

GENERATE_PLAN_SCRIPT = BASE.parent.parent / "ClaudeCoach/scripts/generate-plan.sh"

CTL_RACE_TARGET = 90.0


def _week_stats() -> str:
    """Python-only weekly training summary from session-log.json."""
    if not SESSION_LOG_F.exists():
        return "No session log found yet."
    try:
        entries = json.loads(SESSION_LOG_F.read_text())
    except Exception:
        return "Session log unreadable."

    today      = date.today()
    week_start = today - timedelta(days=today.weekday())

    by_sport   = {}
    total_tss  = 0
    total_min  = 0
    n_sessions = 0
    n_logged   = 0

    for e in entries:
        try:
            d = date.fromisoformat(e.get("date", ""))
        except Exception:
            continue
        if d < week_start or d > today:
            continue
        sport = e.get("sport", "Other")
        tss   = e.get("tss") or 0
        dur   = e.get("duration_min") or 0
        total_tss += tss
        total_min += dur
        n_sessions += 1
        if not e.get("stub", True):
            n_logged += 1
        s = by_sport.setdefault(sport, {"n": 0, "tss": 0, "min": 0})
        s["n"] += 1
        s["tss"] += tss
        s["min"] += dur

    if not by_sport:
        return f"No sessions this week ({week_start.strftime('%-d %b')} – today)."

    days_to_race = (RACE_DAY - today).days
    h, m   = divmod(int(total_min), 60)
    lines  = [f"*Week {week_start.strftime('%-d %b')} – {today.strftime('%-d %b')}*"]
    lines.append(f"*{int(total_tss)} TSS* · {h}h {m:02d}m total\n")
    for sport, v in sorted(by_sport.items(), key=lambda x: -x[1]["tss"]):
        sh, sm = divmod(int(v["min"]), 60)
        lines.append(
            f"  {sport}: {v['n']} session{'s' if v['n'] > 1 else ''}"
            f" · {int(v['tss'])} TSS · {sh}h{sm:02d}m"
        )
    if n_sessions and n_sessions > n_logged:
        diff = n_sessions - n_logged
        lines.append(f"\n_{diff} session{'s' if diff > 1 else ''} still awaiting feedback_")
    lines.append(f"\n_{days_to_race} days to Cervia_")
    return "\n".join(lines)


def _form_stats() -> str:
    """Python-only CTL/ATL/TSB + projected CTL to race day."""
    kpi     = {}
    history = []

    if TRAINING_DATA.exists():
        try:
            td      = json.loads(TRAINING_DATA.read_text())
            kpi     = td.get("kpi", {})
            history = td.get("fitnessThis", [])
        except Exception:
            pass

    if not kpi.get("ctl") and SITE_DATA.exists():
        try:
            sd      = json.loads(SITE_DATA.read_text())
            jamie   = sd.get("athletes", {}).get("jamie", {})
            kpi     = {"ctl": jamie.get("ctl"), "atl": jamie.get("atl"), "tsb": jamie.get("tsb")}
            history = jamie.get("ctl_history", [])
        except Exception:
            pass

    ctl = kpi.get("ctl")
    if ctl is None:
        return "No fitness data yet — refresh pending."

    atl          = kpi.get("atl") or 0
    tsb          = ctl - atl
    today        = date.today()
    days_to_race = (RACE_DAY - today).days
    tsb_zone     = "Fresh" if tsb > 5 else ("Load" if tsb > -20 else "Heavy")

    lines = [f"*CTL {ctl:.1f}* · ATL {atl:.1f} · TSB {tsb:+.1f} ({tsb_zone})"]

    if len(history) >= 28:
        old     = history[-28]
        old_ctl = old[1] if isinstance(old, list) else old.get("ctl", 0)
        weekly_ramp   = (ctl - old_ctl) / 4
        projected_ctl = ctl + weekly_ramp * (days_to_race / 7)
        needed_ramp   = (CTL_RACE_TARGET - ctl) / (days_to_race / 7) if days_to_race > 0 else 0
        lines.append(f"4-wk ramp: *{weekly_ramp:+.1f}/wk*")
        if projected_ctl >= CTL_RACE_TARGET * 0.95:
            lines.append(f"On track: CTL *{projected_ctl:.0f}* by race day ✓")
        else:
            lines.append(
                f"Projected: CTL *{projected_ctl:.0f}* — "
                f"need *{needed_ramp:+.1f}/wk* to hit {int(CTL_RACE_TARGET)}"
            )

    lines.append(f"_{days_to_race} days to Cervia_")
    return "\n".join(lines)


def _git_commit(msg):
    try:
        subprocess.run(["git", "add", "ClaudeCoach/"],
                       cwd=str(PROJECT_DIR), capture_output=True)
        r = subprocess.run(["git", "commit", "-m", msg],
                           cwd=str(PROJECT_DIR), capture_output=True)
        if r.returncode != 0:
            return  # nothing to commit — skip push
        subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                       cwd=str(PROJECT_DIR), capture_output=True)
        subprocess.run(["git", "push", "origin", "main"],
                       cwd=str(PROJECT_DIR), capture_output=True)
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
    txt   = text.strip()

    if _WEEK_CMD_RE.match(txt):
        return _week_stats()

    if _FORM_CMD_RE.match(txt):
        return _form_stats()

    m = _ANKLE_RE.match(txt)
    if m:
        location = m.group(2).strip() if m.group(1).lower() != "ankle" else "ankle"
        score = float(m.group(3))
        state = _load_state_json()
        prev  = state.get("ankle", {}).get("pain_during")
        state.setdefault("ankle", {})["pain_during"] = int(score)

        # Rolling pain history — keep last 20 readings
        hist = state["ankle"].setdefault("history", [])
        hist.append({"date": today, "score": int(score)})
        state["ankle"]["history"] = hist[-20:]

        state["last_updated"] = today
        _save_state_json(state)
        _git_commit(f"auto: ankle pain {score} {today}")

        trend = ""
        if prev is not None:
            if score > prev:
                trend = f" (up from {prev} — monitor)"
            elif score < prev:
                trend = f" (down from {prev} — improving)"

        reply = f"Logged {location} pain {int(score)}/10{trend}."

        # Trend and rebalancing alerts
        recent_scores = [h["score"] for h in state["ankle"]["history"][-3:]]
        if len(recent_scores) >= 3 and recent_scores[-1] > recent_scores[-2] > recent_scores[-3]:
            reply += (
                "\n\n⚠️ *Three readings rising in a row.* "
                "Drop run volume and flag to your physio."
            )
        elif len(recent_scores) >= 2 and recent_scores[-1] > recent_scores[-2] and score >= 4:
            reply += (
                "\n\n⚠️ *Rising and at 4+ — consider swapping today's run for easy bike.* "
                "Say _rebalance plan_ for adjustments."
            )
        elif score >= 4:
            reply += (
                "\n\n⚠️ *Score 4+* — if a run is on the plan today, "
                "say _rebalance plan_ before heading out."
            )

        return reply

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
    rules_path = BASE.parent / "athletes/jamie/reference/rules.md"
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


def prefetch_context(slug: str) -> str:
    """Fetch standard training context in parallel and return as a formatted block.
    Falls back silently to empty string on any error so the bot keeps working."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE.parent / "lib"))
        from icu_api import IcuClient
        from datetime import date, timedelta

        athletes = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])

        today = date.today()
        end_date = (today + timedelta(days=21)).isoformat()

        wellness, events, sport, history_acts = client.fetch_all(
            ("get_wellness", 14),
            ("get_events", today.isoformat(), end_date),
            ("get_sport_settings", "Ride"),
            ("get_training_history", 7),
        )

        lines = [f"=== LIVE TRAINING DATA ({today.isoformat()}) ==="]

        # Fitness snapshot
        if wellness:
            w = wellness[-1]
            ctl = round(w.get("ctl") or 0, 1)
            atl = round(w.get("atl") or 0, 1)
            tsb = round((w.get("ctl") or 0) - (w.get("atl") or 0), 1)
            ftp = sport.get("ftp") if isinstance(sport, dict) else None
            lines.append(
                f"Fitness: CTL {ctl}  ATL {atl}  TSB {tsb}"
                + (f"  FTP {ftp}W" if ftp else "")
            )
            fields = []
            if w.get("weight"):    fields.append(f"Weight {w['weight']:.1f}kg")
            if w.get("hrv"):       fields.append(f"HRV {w['hrv']}")
            if w.get("sleepScore"):fields.append(f"Sleep {w['sleepScore']}%")
            if w.get("restingHR"): fields.append(f"RHR {w['restingHR']}")
            if fields:
                lines.append("Wellness: " + "  ".join(fields))

        # Wellness trend (last 7 days, compact)
        if len(wellness) > 1:
            lines.append("Recent CTL/TSB: " + "  ".join(
                f"{w['id'][5:]}:{round(w.get('ctl') or 0, 0):.0f}/{round((w.get('ctl') or 0)-(w.get('atl') or 0), 0):.0f}"
                for w in wellness[-7:]
            ))

        # Recent activities
        if history_acts:
            lines.append("Recent activities:")
            for a in sorted(history_acts, key=lambda x: x.get("start_date_local",""), reverse=True)[:5]:
                date_str = (a.get("start_date_local") or "")[:10]
                sport_type = a.get("type", "?")
                dur = round((a.get("moving_time") or 0) / 60)
                tss = a.get("icu_training_load") or 0
                dist = a.get("distance") or 0
                dist_str = f"  {dist/1000:.1f}km" if dist else ""
                lines.append(f"  {date_str}  {sport_type:<12} {dur}min{dist_str}  TSS={tss}")

        # Upcoming planned events
        if events:
            lines.append("Upcoming events:")
            for ev in events[:8]:
                ev_date = (ev.get("start_date_local") or "")[:10]
                ev_name = ev.get("name") or ""
                ev_type = ev.get("type") or ev.get("category") or ""
                lines.append(f"  {ev_date}  {ev_type:<12} {ev_name}")

        lines.append(
            f"\nFor more detail call: python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint <endpoint> [options]"
        )
        lines.append("Write endpoints: push_workout (--payload JSON), edit_workout (--event-id ID --payload JSON), delete_workout (--event-id ID)")

        # Inject today's already-answered values from current-state.json so Claude
        # doesn't re-ask questions the athlete has already answered this session.
        state_path = BASE.parent / "athletes" / slug / "current-state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                today_str = today.isoformat()
                state_lines = []

                ankle = state.get("ankle", {})
                ankle_hist = ankle.get("history", [])
                if ankle_hist:
                    today_entry = next((h for h in reversed(ankle_hist) if h.get("date") == today_str), None)
                    if today_entry is not None:
                        state_lines.append(f"Ankle score logged today: {today_entry['score']}/10 — do not ask again")
                    else:
                        last = ankle_hist[-1]
                        state_lines.append(f"Last ankle score: {last['score']}/10 on {last['date']}")

                weights = state.get("weight_readings", [])
                if weights:
                    last_w = weights[-1]
                    if last_w.get("date") == today_str:
                        state_lines.append(f"Weight logged today: {last_w['kg']}kg — do not ask again")
                    else:
                        state_lines.append(f"Last weight: {last_w['kg']}kg ({last_w['date']})")

                if state_lines:
                    lines.append("Already answered today: " + "  |  ".join(state_lines))
            except Exception:
                pass

        return "\n".join(lines)

    except Exception as e:
        log(f"prefetch_context error (non-fatal): {e}")
        return ""


# --- ONBOARDING --------------------------------------------------------------

PENDING_FILE    = BASE.parent / "config/pending.json"
ONBOARDING_FILE = BASE.parent / "config/onboarding_state.json"  # gitignored

# Phase 1: always asked, in order
_OB_PHASE1 = [
    ("name",    "Hi! I'm ClaudeCoach. Just a few questions to get you set up.\n\nWhat's your *full name*?"),
    ("race",    "What's your *target race* -- name and date? (e.g. _IM Frankfurt, 2027-06-29_)"),
    ("icu_id",  "Your *Intervals.icu athlete ID* -- it looks like _i196362_.\n\nFind it at: intervals.icu → top-right menu → *Settings* → scroll to the bottom → *Developer settings*. Your athlete ID is shown there."),
    ("icu_key", "Now your *Intervals.icu API key*.\n\nSame place: intervals.icu → *Settings* → scroll to the bottom → *Developer settings* → click *Show API key*. Copy and paste the full key here."),
]

# Always asked after ICU fetch, regardless of what ICU returned
_OB_QUALITATIVE = [
    ("a_goal",     "What's your *A goal* for {race_name}?"),
    ("experience", "How many full-distance triathlons have you raced? What do you most want to work on?"),
    ("injuries",   "Any current injuries or health constraints? (or _none_)"),
    ("max_hours",  "What's the *maximum hours per week* you can realistically train?"),
    ("slug",       "Last one: choose a short *account handle* for your profile. Lowercase letters and numbers only (e.g. _sarah_). Can't be changed later."),
]

# (icu_data_key, answer_key, question) -- only asked if ICU returned nothing for icu_data_key
_OB_GAPS = [
    ("ftp_watts",                 "ftp",          "I couldn't find your *FTP* in Intervals.icu. What is it in watts?"),
    ("run_threshold_pace_per_km", "run_threshold", "No run threshold pace found. What's yours per km? (e.g. _4:15_)"),
    ("swim_css_per_100m",         "swim_css",      "No swim CSS found. What's yours per 100m? (e.g. _1:45_)"),
    ("weight_kg",                 "weight",        "No weight recorded in Intervals.icu. Current weight in kg?"),
]


def load_pending():
    if PENDING_FILE.exists():
        data = json.loads(PENDING_FILE.read_text())
        return [str(x) for x in data]
    return []


def save_pending(pending):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(pending, indent=2))


def load_onboarding_state():
    if ONBOARDING_FILE.exists():
        return json.loads(ONBOARDING_FILE.read_text())
    return {}


def save_onboarding_state(ob_state):
    ONBOARDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    ONBOARDING_FILE.write_text(json.dumps(ob_state, indent=2))


def _validate_ob_answer(key, answer):
    """Return error string if invalid, else None."""
    if not answer:
        return "Please send a text reply."
    if key == "icu_id":
        if not re.match(r'^i\d+$', answer.strip()):
            return "That doesn't look right -- the athlete ID starts with 'i' followed by numbers (e.g. _i196362_). Check intervals.icu -> Profile."
    if key == "slug":
        if not re.match(r'^[a-z][a-z0-9_]{1,19}$', answer.strip()):
            return "Handle must be 2-20 lowercase letters, numbers, or underscores -- e.g. _sarah_ or _tom2_. Try again."
        existing = json.loads(ATHLETES_CONFIG.read_text()) if ATHLETES_CONFIG.exists() else {}
        if answer.strip() in existing:
            return f"The handle _{answer.strip()}_ is already taken. Please choose a different one."
    if key == "max_hours":
        if not re.search(r'\d', answer):
            return "Please enter a number -- e.g. _12_ for 12 hours/week."
    if key == "ftp":
        if not re.search(r'\d', answer):
            return "Please enter your FTP as a number in watts -- e.g. _280_."
    if key == "weight":
        if not re.search(r'\d', answer):
            return "Please enter your weight as a number in kg -- e.g. _82_."
    return None


def _fetch_icu_data(icu_id, icu_key):
    """Fetch athlete profile + sport settings in parallel. Returns (icu_data dict, summary str)."""
    import sys as _sys
    _sys.path.insert(0, str(BASE.parent / "lib"))
    from icu_api import IcuClient

    client = IcuClient(icu_id.strip(), icu_key.strip())
    profile, ride, run_s, swim, wellness = client.fetch_all(
        "get_athlete_profile",
        ("get_sport_settings", "Ride"),
        ("get_sport_settings", "Run"),
        ("get_sport_settings", "Swim"),
        ("get_wellness", 7),
    )

    weight = None
    if isinstance(profile, dict):
        weight = profile.get("weight")
    if not weight and isinstance(wellness, list) and wellness:
        weight = wellness[-1].get("weight")

    last_w = wellness[-1] if isinstance(wellness, list) and wellness else {}

    icu_data = {
        "ftp_watts":                 (ride or {}).get("ftp"),
        "indoor_ftp_watts":          (ride or {}).get("indoor_ftp"),
        "lthr":                      (ride or {}).get("lthr"),
        "run_threshold_pace_per_km": (run_s or {}).get("threshold_pace"),
        "swim_css_per_100m":         (swim or {}).get("css"),
        "weight_kg":                 weight,
        "icu_name":                  (profile or {}).get("name"),
        "ctl":                       last_w.get("ctl"),
        "tsb":                       (last_w.get("ctl", 0) or 0) - (last_w.get("atl", 0) or 0),
    }

    # Summary message
    lines = ["*Found in Intervals.icu:*"]
    if icu_data["icu_name"]:
        lines.append(f"Name: {icu_data['icu_name']}")
    metrics = []
    if icu_data["ftp_watts"]:        metrics.append(f"Bike FTP {icu_data['ftp_watts']}W")
    if icu_data["run_threshold_pace_per_km"]: metrics.append(f"Run {icu_data['run_threshold_pace_per_km']}/km")
    if icu_data["swim_css_per_100m"]: metrics.append(f"Swim CSS {icu_data['swim_css_per_100m']}/100m")
    if icu_data["weight_kg"]:        metrics.append(f"Weight {icu_data['weight_kg']}kg")
    if icu_data["ctl"]:              metrics.append(f"CTL {round(icu_data['ctl'])}")
    if metrics:
        lines.append("  ".join(metrics))

    missing = []
    for icu_field, _, label in [
        ("ftp_watts", None, "FTP"),
        ("run_threshold_pace_per_km", None, "run threshold"),
        ("swim_css_per_100m", None, "swim CSS"),
        ("weight_kg", None, "weight"),
    ]:
        if not icu_data.get(icu_field):
            missing.append(label)
    if missing:
        lines.append(f"_Not found: {', '.join(missing)} -- I'll ask about those in a moment._")

    return icu_data, "\n".join(lines)


def _build_remaining_queue(answers, icu_data):
    """Build qualitative + gap questions after ICU fetch."""
    race_str  = answers.get("race", "your race")
    race_name = race_str
    dm = re.search(r'(\d{4}-\d{2}-\d{2})', race_str)
    if dm:
        race_name = race_str[:dm.start()].strip().rstrip(", ")

    qual = [(key, q.format(race_name=race_name)) for key, q in _OB_QUALITATIVE]
    gaps = [
        (answer_key, question)
        for icu_field, answer_key, question in _OB_GAPS
        if not icu_data.get(icu_field)
    ]
    return qual + gaps


def _scaffold_athlete(chat_id, answers, icu_data):
    """Create athletes/{slug}/ folder, write seed files, update athletes.json. Returns slug."""
    from string import Template

    slug       = answers["slug"].strip()
    name       = answers["name"].strip()
    first_name = name.split()[0]

    def _int(s):
        try: return int(float(re.sub(r'[^\d.]', '', str(s))))
        except Exception: return None

    def _float(s):
        try: return float(re.sub(r'[^\d.]', '', str(s)))
        except Exception: return None

    ftp      = icu_data.get("ftp_watts")      or _int(answers.get("ftp", ""))
    run_thr  = icu_data.get("run_threshold_pace_per_km") or answers.get("run_threshold") or None
    swim_css = icu_data.get("swim_css_per_100m")         or answers.get("swim_css")      or None
    weight   = icu_data.get("weight_kg")      or _float(answers.get("weight", ""))
    lthr     = icu_data.get("lthr")
    max_hours = _int(answers.get("max_hours", ""))

    race_str  = answers.get("race", "").strip()
    race_date, race_name = None, race_str
    dm = re.search(r'(\d{4}-\d{2}-\d{2})', race_str)
    if dm:
        race_date = dm.group(1)
        race_name = race_str[:dm.start()].strip().rstrip(", ")

    injuries_str = answers.get("injuries", "none").strip()
    injuries = [] if injuries_str.lower() == "none" else [
        {"location": "", "description": injuries_str, "protocol": ""}
    ]

    profile = {
        "slug": slug, "name": name,
        "weight_kg": weight, "race_weight_kg": None,
        "race_name": race_name, "race_date": race_date, "race_distance": "Full Ironman",
        "a_goal": answers.get("a_goal", ""), "b_goal": None, "c_goal": "Finish",
        "ftp_watts": ftp, "indoor_ftp_watts": icu_data.get("indoor_ftp_watts"),
        "swim_css_per_100m": swim_css, "run_threshold_pace_per_km": run_thr, "lthr": lthr,
        "training_days": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],
        "max_hours_per_week": max_hours,
        "icu_athlete_id": answers.get("icu_id", "").strip(),
        "experience": answers.get("experience", ""),
        "injuries": injuries,
    }

    adir = BASE.parent / "athletes" / slug
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "telegram").mkdir(exist_ok=True)
    (adir / "reference").mkdir(exist_ok=True)

    (adir / "profile.json").write_text(json.dumps(profile, indent=2))

    today_str = date.today().isoformat()
    for fname, content in [
        ("session-log.json",    "[]"),
        ("heat-log.json",       "[]"),
        ("swim-log.json",       "[]"),
        ("feedback-log.json",   "[]"),
        ("decoupling-log.json", "[]"),
        ("current-state.json",  json.dumps({"last_updated": today_str}, indent=2)),
    ]:
        if not (adir / fname).exists():
            (adir / fname).write_text(content)

    (adir / "current-state.md").write_text(
        f"# {name} -- Current State\n\nLast updated: {today_str}\n\n"
        f"## Injuries / Niggles\n{injuries_str}\n\n## Open Actions\n- [ ] Set up initial training plan\n"
    )

    template_file = BASE.parent / "onboarding/templates/system_prompt.txt"
    if template_file.exists():
        sp = Template(template_file.read_text()).safe_substitute(
            name=name, first_name=first_name, slug=slug,
            race_name=race_name, race_date=race_date or "TBD",
            a_goal=profile["a_goal"],
            experience=answers.get("experience", ""),
            injuries=injuries_str,
            max_hours=max_hours or "?",
        )
        (adir / "system_prompt.txt").write_text(sp)

    # Riskiest write last
    athletes_data = json.loads(ATHLETES_CONFIG.read_text()) if ATHLETES_CONFIG.exists() else {}
    athletes_data[slug] = {
        "name": name, "chat_id": str(chat_id),
        "icu_athlete_id": answers.get("icu_id", "").strip(),
        "icu_api_key":    answers.get("icu_key", "").strip(),
        "active": False,
        "race_date": race_date or "", "race_name": race_name,
    }
    ATHLETES_CONFIG.write_text(json.dumps(athletes_data, indent=2))

    return slug


def handle_onboarding(token, chat_id, text):
    """Returns True if chat_id is pending and the message was handled."""
    pending = load_pending()
    if chat_id not in pending:
        return False

    ob_state = load_onboarding_state()

    # First contact -- send Q0, initialise queue with remainder of phase 1
    if chat_id not in ob_state:
        ob_state[chat_id] = {
            "current_key": _OB_PHASE1[0][0],
            "queue": [[k, q] for k, q in _OB_PHASE1[1:]],
            "answers": {}, "icu_data": {},
            "started_at": datetime.now().isoformat(),
        }
        save_onboarding_state(ob_state)
        send(token, chat_id, _OB_PHASE1[0][1])
        return True

    session = ob_state[chat_id]
    key     = session["current_key"]
    answer  = text.strip()

    err = _validate_ob_answer(key, answer)
    if err:
        send(token, chat_id, err)
        return True

    session["answers"][key] = answer

    # After ICU key: verify, fetch everything, build rest of queue
    if key == "icu_key":
        save_onboarding_state(ob_state)
        send(token, chat_id, "_Connecting to Intervals.icu..._")
        try:
            icu_data, summary = _fetch_icu_data(session["answers"]["icu_id"], answer)
        except Exception as e:
            log(f"Onboarding ICU fetch failed for {chat_id}: {e}")
            send(token, chat_id,
                 f"Couldn't connect to Intervals.icu -- please check your API key and try again.\n"
                 f"_(Error: {e})_\n\n" + _OB_PHASE1[-1][1])
            session["answers"].pop("icu_key", None)
            save_onboarding_state(ob_state)
            return True

        session["icu_data"] = icu_data
        remaining = _build_remaining_queue(session["answers"], icu_data)
        session["queue"] = [[k, q] for k, q in remaining]
        next_key, next_q = session["queue"].pop(0)
        session["current_key"] = next_key
        save_onboarding_state(ob_state)
        send(token, chat_id, summary + "\n\n" + next_q)
        return True

    # Advance to next question
    if session["queue"]:
        next_key, next_q = session["queue"].pop(0)
        session["current_key"] = next_key
        save_onboarding_state(ob_state)
        send(token, chat_id, next_q)
        return True

    # Queue exhausted -- scaffold
    save_onboarding_state(ob_state)
    send(token, chat_id, "_Setting up your profile..._")
    try:
        slug = _scaffold_athlete(chat_id, session["answers"], session["icu_data"])
    except Exception as e:
        log(f"Onboarding scaffold failed for {chat_id}: {e}")
        send(token, chat_id, f"Something went wrong setting up your profile -- please contact your coach. (Error: {e})")
        return True

    log(f"Onboarding complete: {session['answers'].get('name', '?')} ({slug})")
    _git_commit(f"onboarding: add athlete {slug}")

    pending_list = load_pending()
    if chat_id in pending_list:
        pending_list.remove(chat_id)
    save_pending(pending_list)
    del ob_state[chat_id]
    save_onboarding_state(ob_state)

    first_name = session["answers"]["name"].split()[0]
    send(token, chat_id,
         f"You're all set, *{first_name}*! Your profile has been created.\n\n"
         f"Your account isn't live yet -- your coach will activate it shortly "
         f"and you'll get a message here when you're good to go.")

    config_data = load_config()
    admin_id = str(config_data.get("admin_chat_id", ""))
    if admin_id:
        send(token, admin_id,
             f"*New athlete ready to activate:*\n"
             f"Name: {session['answers']['name']}\n"
             f"Handle: `{slug}`\n"
             f"Race: {session['answers'].get('race', '?')}\n"
             f"Goal: {session['answers'].get('a_goal', '?')}\n\n"
             f"Send `/approve {slug}` to activate.")
    return True


def handle_admin_command(token, chat_id, text, config):
    """Handle /invite and /approve commands from the admin chat_id. Returns True if handled."""
    admin_id = str(config.get("admin_chat_id", ""))
    if not admin_id or chat_id != admin_id:
        return False

    lower = text.lower().strip()

    if lower.startswith("/invite "):
        raw = text.split(None, 1)[1].strip()
        pending = load_pending()
        if raw not in pending:
            pending.append(raw)
            save_pending(pending)
            send(token, chat_id, f"Added `{raw}` to pending list. They can now start onboarding.")
        else:
            send(token, chat_id, f"`{raw}` is already in the pending list.")
        return True

    if lower.startswith("/approve "):
        slug_to_approve = text.split(None, 1)[1].strip()
        athletes_data = json.loads(ATHLETES_CONFIG.read_text()) if ATHLETES_CONFIG.exists() else {}
        if slug_to_approve not in athletes_data:
            send(token, chat_id, f"No athlete found with handle `{slug_to_approve}`.")
            return True
        athletes_data[slug_to_approve]["active"] = True
        ATHLETES_CONFIG.write_text(json.dumps(athletes_data, indent=2))
        approved_cid = str(athletes_data[slug_to_approve].get("chat_id", ""))
        approved_name = athletes_data[slug_to_approve]["name"].split()[0]
        if approved_cid:
            send(token, approved_cid,
                 f"Welcome aboard, *{approved_name}*! ClaudeCoach is now active for you.\n\n"
                 f"Try: _how am I looking?_ or _what's today's session?_")
        send(token, chat_id, f"Athlete `{slug_to_approve}` is now active.")
        log(f"Admin approved athlete: {slug_to_approve}")
        return True

    return False


# --- END ONBOARDING ----------------------------------------------------------


def call_claude(user_message, config, history, model=MODEL_SONNET,
                system_prompt_file=None, athlete_name="Jamie", context=""):
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    system_prompt = sp_file.read_text().strip()

    parts = [system_prompt, ""]

    if context:
        parts.append(context)
        parts.append("")

    if history:
        parts.append("Recent conversation:")
        for h in history:
            parts.append(f"{athlete_name}: {h['user']}")
            parts.append(f"ClaudeCoach: {h['assistant']}")
        parts.append("")

    parts.append(f"{athlete_name}: {user_message}")

    full_prompt = "\n".join(parts)

    try:
        result = subprocess.run(
            [config["claude_binary"], "-p", full_prompt, "--allowedTools", TOOLS, "--model", model],
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
    athletes = load_athletes()
    log(f"ClaudeCoach bot started. Registered athletes: {[a['name'] for a in athletes.values()]}")
    get_whisper()

    offset = 0
    while True:
        # Reload athlete registry on each poll cycle so new athletes are picked up without restart
        athletes = load_athletes()

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
                    if voice and chat_id in athletes:
                        typing(token, chat_id)
                        raw = download_tg_file(token, voice["file_id"])
                        if raw:
                            text = transcribe_voice(raw) or ""
                            if text:
                                send(token, chat_id, f"_Heard: {text}_")

            if not text:
                continue

            # Admin commands (/invite, /approve) — handled before athlete routing
            if handle_admin_command(token, chat_id, text, config):
                continue

            athlete = athletes.get(chat_id)
            if not athlete:
                if handle_onboarding(token, chat_id, text):
                    continue
                log(f"Unregistered message from chat_id {chat_id}: {text[:60]}")
                send(token, chat_id, "This account isn't registered with ClaudeCoach yet.")
                continue

            slug = athlete["slug"]
            files = athlete_files(slug)
            athlete_name = athlete.get("name", slug).split()[0]  # first name only

            if text.lower() in ("/start", "/help"):
                send(token, chat_id,
                     "*ClaudeCoach* — IM Cervia 2026\n\n"
                     "*Quick commands (instant):*\n"
                     "  /week — this week's sessions + TSS\n"
                     "  /form — CTL/ATL/TSB + race projection\n"
                     "  ankle 3 — log pain score\n"
                     "  82.5 kg — log weight\n"
                     "  heat 30 — log heat session\n\n"
                     "*Ask anything:*\n"
                     "  _how am I looking?_\n"
                     "  _what's today's session?_\n"
                     "  _log session_ (after a workout)\n"
                     "  _rebalance plan_ (ankle flare)\n"
                     "  _generate plan_",
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

            history = load_history(files["history"])
            context = prefetch_context(slug)
            model = select_model(text)
            response = call_claude(text, config, history, model=model,
                                   system_prompt_file=files["system_prompt"],
                                   athlete_name=athlete_name, context=context)

            clean = process_charts(token, chat_id, response)
            if clean:
                send(token, chat_id, clean + response_footer(model), reply_markup=build_keyboard())
            log(f"Out: {clean[:80]}")

            history.append({"user": text, "assistant": clean})
            save_history(history, files["history"])

        time.sleep(1)


if __name__ == "__main__":
    main()
