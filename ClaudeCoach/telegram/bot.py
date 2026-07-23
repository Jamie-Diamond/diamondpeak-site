#!/usr/bin/env python3
"""
ClaudeCoach Telegram bot - two-way interface.
Polls Telegram for messages, passes them to claude CLI, sends responses back.
Run: python3 bot.py
"""

import json, re, subprocess, sys, time, ssl, os, shutil
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime, date, timedelta

# Force IPv4 for the Telegram API. The IPv6 path to api.telegram.org intermittently
# stalls the FIRST connection after the bot's been idle — the new SYN is lost and TCP
# retransmits for ~10s before it connects, which made button taps (e.g. opening Graphs)
# feel frozen for ~10s. IPv4 (149.154.x.x) connects instantly here. Only telegram.org is
# affected; ICU / QuickChart / GitHub resolution is untouched.
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_telegram_getaddrinfo(host, *args, **kwargs):
    results = _orig_getaddrinfo(host, *args, **kwargs)
    if isinstance(host, str) and "telegram.org" in host:
        ipv4 = [r for r in results if r[0] == socket.AF_INET]
        if ipv4:
            return ipv4
    return results
socket.getaddrinfo = _ipv4_telegram_getaddrinfo


def _resolve_claude_bin() -> str:
    """Find the claude CLI on PATH, fall back to common install locations."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in ("/usr/bin/claude", "/usr/local/bin/claude",
                      os.path.expanduser("~/.local/bin/claude")):
        if os.path.isfile(candidate):
            return candidate
    return "claude"  # last resort — will fail with a clear error at call time


CLAUDE_BIN = _resolve_claude_bin()

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent / "lib"))
import claude_call
import engine
from engine import call_claude, call_claude_with_image, stream_claude
HEARTBEAT_FILE = BASE.parent / ".bot_heartbeat"  # touched each poll loop; watched by bot-watchdog.py
try:
    import charts as _charts
except Exception:
    _charts = None

try:
    from coaching_levels import level_block as _level_block
except Exception:
    def _level_block(level: str) -> str:  # type: ignore[misc]
        return ""

try:
    import menstrual as _menstrual
except Exception:
    _menstrual = None

try:
    import heat as _heat_lib
except Exception:
    _heat_lib = None

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


# --- Text-to-speech (Piper, local, CPU) ------------------------------------
VOICES_DIR = BASE / "voices"
PIPER_VOICE_NAME = "en_GB-cori-high"     # high-quality UK female (trial); revert to "en_GB-alan-medium" if synth too slow
PIPER_LENGTH_SCALE = 0.85                # <1 = faster speech, >1 = slower (model default ~1.0)
_piper_voice = None


def get_piper():
    """Lazily load and cache the Piper voice. The ~63MB ONNX model loads ONCE
    (a fresh subprocess per reply would reload it and add 2-5s every time)."""
    global _piper_voice
    if _piper_voice is None:
        try:
            from piper import PiperVoice
            _piper_voice = PiperVoice.load(str(VOICES_DIR / f"{PIPER_VOICE_NAME}.onnx"))
            log("Piper voice ready")
        except Exception as e:
            log(f"Piper not available: {e}")
    return _piper_voice


# Symbols/units that read badly when spoken verbatim. The MAIN lever is the
# voice-style directive in the prompt; this is a backstop for structured text
# that slips through (e.g. a copied session line like "5x200m @ 1:38/100m").
_SPEECH_SUBS = [
    (re.compile(r'```.*?```', re.S), ' '),            # code fences
    (re.compile(r'`([^`]*)`'), r'\1'),                 # inline code
    (re.compile(r'\[([^\]]+)\]\([^)]+\)'), r'\1'),     # [text](url) -> text
    (re.compile(r'https?://\S+'), ' '),                # bare URLs
    (re.compile(r'<\s*(\d)'), r'under \1'),            # "<144" -> "under 144" (before md strip eats >)
    (re.compile(r'>\s*(\d)'), r'over \1'),             # ">144" -> "over 144"
    (re.compile(r'[*_#>|]+'), ' '),                    # md emphasis / headings / table pipes
    (re.compile(r'(\d)\s*[x×]\s*(\d)'), r'\1 by \2'),  # 5x200 -> 5 by 200
    (re.compile(r'\s*@\s*'), ' at '),                  # @ -> at
    (re.compile(r'/100\s*m\b', re.I), ' per hundred metres'),
    (re.compile(r'/km\b', re.I), ' per kilometre'),
    (re.compile(r'\s*bpm\b', re.I), ' beats per minute'),   # "144bpm" or "144 bpm"
    (re.compile(r'\s*&\s*'), ' and '),
    (re.compile(r'\s*%'), ' percent'),
    (re.compile(r' +([,.;:])'), r'\1'),                # tidy " ," left by a prior sub
]
# Strip emoji / pictographs so the TTS doesn't try to read them aloud.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"   # symbols & pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027BF"    # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"    # regional indicators (flags)
    "←-⇿"            # arrows
    "⬀-⯿"            # misc symbols & arrows
    "️]"                  # variation selector
)


def _clean_for_speech(text: str) -> str:
    """Reduce a markdown/emoji reply to clean prose Piper can read aloud."""
    t = text
    for pat, repl in _SPEECH_SUBS:
        t = pat.sub(repl, t)
    t = _EMOJI_RE.sub('', t)
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r'\n{2,}', '\n', t)
    return t.strip()


def synthesize_voice(text: str):
    """Return OGG/Opus bytes for `text`, or None on any failure (caller falls back to text)."""
    voice = get_piper()
    if not voice or not text.strip():
        return None
    try:
        from piper.config import SynthesisConfig
        syn = SynthesisConfig(length_scale=PIPER_LENGTH_SCALE)
        pcm = b"".join(chunk.audio_int16_bytes for chunk in voice.synthesize(text, syn_config=syn))
        if not pcm:
            return None
        rate = getattr(voice.config, "sample_rate", 22050)
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
             "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
            input=pcm, capture_output=True, timeout=60,
        )
        if proc.returncode != 0 or not proc.stdout:
            log(f"ffmpeg opus encode failed: {proc.stderr.decode('utf-8', 'replace')[:200]}")
            return None
        return proc.stdout
    except Exception as e:
        log(f"synthesize_voice error: {e}")
        return None


def send_voice(token, chat_id, ogg_bytes, reply_markup=None):
    """Upload an OGG/Opus voice note via sendVoice (multipart, mirrors send_photo)."""
    boundary = "CCvoice"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
    ]
    if reply_markup:
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"reply_markup\"\r\n\r\n"
             f"{json.dumps(reply_markup)}\r\n").encode())
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"voice\"; "
         f"filename=\"reply.ogg\"\r\nContent-Type: audio/ogg\r\n\r\n").encode())
    body = b"".join(parts) + ogg_bytes + f"\r\n--{boundary}--\r\n".encode()
    url = f"https://api.telegram.org/bot{token}/sendVoice"
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"send_voice error: {e}")
        return {}


def _spoken_rewrite(reply_text: str) -> str:
    """Render a rich (markdown) coaching reply as a short SPOKEN version for TTS.
    The text shown to the athlete stays in the normal rich style; only the audio
    is voice-friendly, and only when voice mode is on. Cheap Haiku pass; on any
    failure returns the input unchanged (then _clean_for_speech strips markdown)."""
    if not reply_text.strip():
        return reply_text
    prompt = (
        "Rewrite the following coaching reply to be SPOKEN ALOUD by a voice assistant. "
        "Plain conversational sentences only - no markdown, bullets, tables, headings or emoji. "
        "Say numbers, paces and sets the way you'd say them out loud (e.g. 'five by two hundred "
        "metres at threshold', 'around five thirty per kilometre', 'a hundred and forty-four beats "
        "per minute'); never read symbols like @, /, %, x or colons in times. Keep it brief and "
        "natural - 2 to 5 sentences, lead with the answer. Output only the spoken text.\n\nREPLY:\n"
        + reply_text
    )
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--model", MODEL_HAIKU],
            capture_output=True, text=True, timeout=25,
        )
        out = (result.stdout or "").strip()
        if out:
            return out
    except Exception as e:
        log(f"_spoken_rewrite error: {e}")
    return reply_text

CONFIG_FILE = BASE / "config.json"
ATHLETES_CONFIG = BASE.parent / "config/athletes.json"
LOG_FILE = BASE / "bot.log"

# Fallback paths (used by fast_path / _update_ftp for single-athlete globals)
HISTORY_FILE = BASE.parent / "athletes/jamie/telegram/history.json"
SYSTEM_PROMPT_FILE = BASE.parent / "athletes/jamie/system_prompt.txt"

MAX_HISTORY_PAIRS = 30  # keep last 30 exchanges on disk (engine sends only the last few)

# --- Concurrency -------------------------------------------------------------
# The poll loop is single-threaded: a 15-40s coaching reply used to block EVERY
# other athlete until it finished. Slow, blocking work (LLM generation, image
# analysis, race-plan subprocess) is now handed to this pool so the main loop
# keeps polling. A per-chat lock serialises an individual athlete's messages so
# their history.json is never read-modify-written concurrently — different
# athletes still run in parallel.
_REPLY_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="cc-reply")
_CHAT_LOCKS = {}
_CHAT_LOCKS_GUARD = threading.Lock()


def _chat_lock(chat_id):
    with _CHAT_LOCKS_GUARD:
        lk = _CHAT_LOCKS.get(chat_id)
        if lk is None:
            lk = threading.Lock()
            _CHAT_LOCKS[chat_id] = lk
        return lk


def _submit(worker, chat_id, *args):
    """Run worker(*args) on the reply pool under the chat's lock. A failed
    worker must never kill its thread silently — log and move on."""
    def _runner():
        try:
            with _chat_lock(chat_id):
                worker(*args)
        except Exception as e:
            log(f"reply worker error for {chat_id}: {e}")
    _REPLY_EXECUTOR.submit(_runner)


# --- prefetch_context cache --------------------------------------------------
# prefetch_context hits intervals.icu on every message. Within a short window the
# data (CTL/ATL, recent activities, plan) doesn't change, so cache the rendered
# block per athlete. Invalidated immediately when the athlete logs something
# (weight/ankle/session) so the "already answered today" injection never goes
# stale and re-asks a question they just answered.
_PREFETCH_CACHE = {}            # slug -> (epoch, context_str)
_PREFETCH_TTL = 150             # seconds
_PREFETCH_GUARD = threading.Lock()


def _invalidate_prefetch(slug):
    with _PREFETCH_GUARD:
        _PREFETCH_CACHE.pop(slug, None)

MODEL_SONNET = "claude-sonnet-5"
MODEL_OPUS   = "claude-opus-4-8"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"  # retired from selection (kept for label map)

_MODEL_LABEL = {
    MODEL_HAIKU:  "H",
    MODEL_SONNET: "S5",
    MODEL_OPUS:   "O",
    "claude-sonnet-4-6": "S",
    "claude-opus-4-7": "O4.7",
}

def response_footer(model: str, slug: str = "", athlete_cfg: dict | None = None) -> str:
    label = _MODEL_LABEL.get(model, model.split("-")[1][0].upper())
    if athlete_cfg:
        race_date_str = athlete_cfg.get("race_date", "")
        race_name     = athlete_cfg.get("race_name", "race")
        try:
            days = (date.fromisoformat(race_date_str) - date.today()).days
            return f"\n_{days} days to {race_name} · {label}_"
        except (ValueError, TypeError):
            pass
    return f"\n_{label}_"


def _strip_model_countdown(text: str, athlete_cfg: dict | None) -> str:
    """response_footer() is the single, harness-computed source of the race
    countdown appended to every reply. Claude sometimes writes its own
    '_N days to <race>_' line in the reply body (it has the race date in
    context too) - strip that so the countdown never appears twice."""
    if not athlete_cfg or not text:
        return text
    race_name = athlete_cfg.get("race_name", "race")
    pattern = re.compile(
        r'\n?[ \t]*_?\s*\d+\s*days?\s*to\s*' + re.escape(race_name) + r'\s*_?[ \t]*(?=\n|$)',
        re.IGNORECASE,
    )
    return re.sub(r'\n{3,}', '\n\n', pattern.sub('', text)).strip()

# Model selection (Jamie's directive 15 Jun: Sonnet for simple, Opus for hard).
# Reverted 11 Jul (Phase 4): the 2 Jul Sonnet-5 default is rolled back - Opus is
# the safe default again for everyday interactive chat and the ask-anything path.
# The Sonnet-5 trial routed ~70% of chat to the weaker model and quality regressed.
#
# Design: Opus is the SAFE DEFAULT. Mis-routing a substantive question to a
# weaker model is the expensive error (it caused the 14 Jun planning mess), so
# anything not clearly trivial goes to Opus. Two reasons not to key "hard" off a
# narrow regex: (1) trivia is a small, closed set, so match THAT and default
# everything else up; (2) stickiness - once a substantive thread is open, a short
# follow-up ("make Saturday shorter") needs the same brain as the message before
# it, so the thread stays on Opus.

# Clearly trivial: greetings, acknowledgements, short pain logs, bare values.
_TRIVIAL_RE = re.compile(
    r"^(hi|hey|hello|yo|good\s+(morning|evening|afternoon)|"
    r"thanks?|thank\s+you|cheers|ta|"
    r"ok(ay)?|kk|got\s+it|noted|perfect|great|nice|cool|good|awesome|"
    r"yes|yep|yup|yeah|no|nope|nah|sure|done)\b[\s!.]{0,3}$"
    r"|^(ankle|niggle|pain|knee|achilles|calf|hamstring)\s+\d{1,2}\b.{0,30}$"
    r"|^\d{1,3}(\.\d)?\s*(kg|km|k|mi|miles?|min|minutes?|hrs?|hours?|w|watts?|bpm)?\s*$",
    re.IGNORECASE,
)

# Topics that always warrant Opus AND make the surrounding thread sticky to Opus.
_HARD_RE = re.compile(
    r"\b(plan|planning|tss|ctl|atl|tsb|form|fitness|build|taper|phase|periodi[sz]e|"
    r"week|weekly|block|ramp|load|projec|forecast|fuell?ing|nutrition|race|pacing|"
    r"strateg|why|analy|compare|should\s+i|how\s+(do|should|much|many|far)|workout|"
    r"session|brick|threshold|interval|zone|recovery|fatigue|overreach|long\s+run)\b",
    re.IGNORECASE,
)


def select_model(text: str, history=None) -> str:
    """Opus for anything substantive or planning-adjacent, Sonnet for clear trivia.
    Sticky: stays on Opus through a substantive thread. Stickiness keys on the
    ATHLETE's recent messages only - the assistant's coaching replies are full of
    'week/session/load/recovery' and would otherwise pin everything to Opus,
    defeating the Sonnet path for genuine trivia."""
    t = text.strip()
    recent = " ".join(h.get("user", "") for h in (history or [])[-3:])
    if _HARD_RE.search(t) or _HARD_RE.search(recent):
        return MODEL_OPUS
    if _TRIVIAL_RE.match(t):
        return MODEL_SONNET
    return MODEL_OPUS  # safe default - never silently downgrade the unknown


# Persistent reply keyboard (expense-bot style) — always pinned at the bottom of the
# chat. Tapping a button sends its label as a message; the labels are routed by
# fast_path's _MENU_MAP (action buttons) or fall through to Claude (the two questions).
MENU_KEYBOARD = {
    "keyboard": [
        ["Today's session", "How am I looking?"],
        ["📈 Graphs", "🔄 Replan week"],
        ["🔍 Check activity", "🎙 Voice"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def build_keyboard(slug=None):
    """Context-aware markup. Right after a session, or on Sunday evening, return the
    intelligent INLINE action buttons; otherwise return the persistent menu. The
    persistent reply keyboard stays pinned at the bottom regardless (Telegram keeps it
    until replaced), so on contextual replies the inline buttons appear *alongside* it."""
    now = datetime.now()
    hour, wday = now.hour, now.weekday()  # wday 0=Mon … 6=Sun
    last_act_file = (BASE.parent / "athletes" / slug / "last_activity_state.json") if slug \
        else (BASE.parent / "last_activity_state.json")
    post_session = False
    last_activity_id = None
    if last_act_file.exists():
        try:
            st = json.loads(last_act_file.read_text())
            ts = st.get("notified_at")
            if ts:
                post_session = (now - datetime.fromisoformat(ts)).total_seconds() < 10800
                last_activity_id = st.get("last_id")
        except Exception:
            pass

    if post_session:
        # Just finished a session — offer logging/analysis, plus the same
        # drill-down buttons (_handle_drill) shown on the initial notification,
        # so they're still reachable if that message scrolled out of view.
        rows = [[
            {"text": "Log session",     "callback_data": "log session"},
            {"text": "Analyse session", "callback_data": "analyse this session"},
        ]]
        if slug and last_activity_id:
            rows.append([
                {"text": "📊 Intervals", "callback_data": f"drill:intervals:{last_activity_id}:{slug}"},
                {"text": "🍌 Nutrition",  "callback_data": f"drill:nutrition:{last_activity_id}:{slug}"},
                {"text": "💓 HR",         "callback_data": f"drill:hr:{last_activity_id}:{slug}"},
                {"text": "↔️ Compare",    "callback_data": f"drill:compare:{last_activity_id}:{slug}"},
            ])
        return {"inline_keyboard": rows}
    if wday == 6 and hour >= 18:  # Sunday evening — weekly review window
        return {"inline_keyboard": [[
            {"text": "How was this week?", "callback_data": "show me this week"},
            {"text": "What's next week?",  "callback_data": "what's the plan for next week?"},
        ]]}
    return MENU_KEYBOARD


def _reply_inline(slug=None):
    """Inline keyboard for a conversational reply: any contextual buttons from
    build_keyboard PLUS a 🔊 Speak button that re-renders the reply as a voice note
    on demand (works regardless of voice mode). Inline-only, so it is valid on
    editMessageText too (the persistent reply menu stays pinned separately) — this
    also fixes the 'inline keyboard expected' 400 the final edit used to throw."""
    speak_row = [{"text": "🔊 Speak", "callback_data": "__SPEAK_LAST__"}]
    kb = build_keyboard(slug)
    if isinstance(kb, dict) and kb.get("inline_keyboard"):
        return {"inline_keyboard": kb["inline_keyboard"] + [speak_row]}
    return {"inline_keyboard": [speak_row]}

TOOLS = "Read,Write,Edit,Bash"
# IcuSync MCP tools are intentionally excluded — the MCP connector is bound to a single
# athlete's account. All Intervals.icu access must go through icu_fetch.py (Bash) which
# uses per-athlete API keys from athletes.json and cannot cross-contaminate accounts.


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# Route engine logs (incl. per-reply [timing] lines) into bot.log instead of
# stderr so latency data survives in one place.
engine.log = lambda msg: log(f"[engine] {msg}")


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


def _extract_plan_override(slug: str) -> dict | None:
    """Scan recent conversation history for a conversation-agreed JSON session plan.
    Returns the parsed plan dict (with a valid 'sessions' list) if found, else None.
    Used to bypass LLM generation when a plan was already agreed in chat."""
    files = athlete_files(slug)
    history = load_history(files["history"])
    for entry in reversed(history[-10:]):
        text = entry.get("assistant") or ""
        if '"sessions"' not in text:
            continue
        m = re.search(r'\{.*\}', text, re.S)
        if not m:
            continue
        try:
            plan = json.loads(m.group(0))
        except Exception:
            continue
        sessions = plan.get("sessions")
        if (isinstance(sessions, list) and sessions
                and all(isinstance(s, dict) and "date" in s and "sport" in s
                        for s in sessions[:3])):
            return plan
    return None


def _write_plan_override(slug: str, plan: dict) -> str:
    """Serialise a plan dict to a temp file for stage1-plan.py --override-json. Returns path."""
    path = f"/tmp/replan_override_{slug}.json"
    Path(path).write_text(json.dumps(plan))
    return path


def _hist_entry(user, assistant, kind="text"):
    """A history pair stamped with the local send time, so the bot can resolve
    time-referenced questions ("my 8:08 message") instead of claiming it can't see
    them (the 16 Jun misdiagnosis). Old entries without 'ts' render without a stamp.
    'kind' marks whether 'user' is a plain text message or an image caption - the
    two must never be conflated, or a lookup can mistake a real text message for a
    photo (the swim-splits misdiagnosis)."""
    return {"user": user, "assistant": assistant, "kind": kind,
            "ts": datetime.now().isoformat(timespec="seconds")}


def tg_post(token, method, payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as r:
            return json.loads(r.read())
    except Exception as e:
        # Telegram returns HTTP 400 when legacy-Markdown can't be parsed (an unbalanced
        # *, _, [ or ` in the LLM's reply). Without a fallback the whole message is lost -
        # this is the 16 Jun bug where Jamie's 8:08 swim debrief was generated but never
        # delivered. Retry ONCE as plain text so a reply is never silently dropped (worst
        # case the user sees raw markdown chars). Mirrors notify.py's plain-text fallback.
        _body = (e.read().decode("utf-8", "replace")[:300] if hasattr(e, "read") else "")
        _code = getattr(e, "code", None)
        # "message is not modified" - the edit's text+markup already match what is
        # shown (fires on streaming ticks where no new token arrived). Benign: treat as
        # success so we neither fire the bogus plain-text retry nor trip the caller fallback.
        if _code == 400 and "not modified" in _body:
            return {"ok": True, "result": {}}
        # Telegram flood control: HTTP 429 carries parameters.retry_after (seconds).
        # The Phase 3 two-message UX roughly doubles edit traffic (a status line
        # rewritten ~1/sec plus the streamed reply), so honour the back-off ONCE
        # rather than dropping the call. Cap the wait so a worker can never hang on
        # a long back-off.
        if _code == 429:
            retry_after = 1.0
            try:
                _j = json.loads(_body) if _body else {}
                retry_after = float((_j.get("parameters") or {}).get("retry_after", 0)) or 0.0
            except Exception:
                retry_after = 0.0
            if not retry_after:
                _m = re.search(r"retry after (\d+)", _body)
                retry_after = float(_m.group(1)) if _m else 1.0
            retry_after = min(max(retry_after, 0.5), 5.0)
            log(f"tg_post {method} 429 - backing off {retry_after:.1f}s then retrying once")
            time.sleep(retry_after)
            try:
                req429 = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req429, timeout=10, context=SSL_CONTEXT) as r:
                    return json.loads(r.read())
            except Exception as e429:
                _b429 = (e429.read().decode("utf-8", "replace")[:300] if hasattr(e429, "read") else "")
                log(f"tg_post {method} 429 retry failed: {e429}; body: {_b429}")
                return {}
        # Genuine legacy-Markdown parse failure -> retry ONCE as plain text so the reply is
        # never dropped. Skip MESSAGE_TOO_LONG: a plain-text edit cannot shorten it; the
        # caller send() fallback chunks it instead.
        if _code == 400 and payload.get("parse_mode") and "too_long" not in _body.lower():
            log(f"tg_post {method} 400 (Markdown parse) - retrying as plain text")
            retry = {k: v for k, v in payload.items() if k != "parse_mode"}
            try:
                req2 = urllib.request.Request(
                    url, data=json.dumps(retry).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req2, timeout=10, context=SSL_CONTEXT) as r:
                    return json.loads(r.read())
            except Exception as e2:
                _b2 = (e2.read().decode("utf-8", "replace")[:300] if hasattr(e2, "read") else "")
                log(f"tg_post {method} plain-text retry failed: {e2}; body: {_b2}")
                return {}
        log(f"tg_post {method} error: {e}; body: {_body}")
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


def send(token, chat_id, text, parse_mode="Markdown", reply_markup=None,
         disable_notification=False):
    chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
    last_id = None
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
        if disable_notification:
            payload["disable_notification"] = True
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        r = tg_post(token, "sendMessage", payload)
        last_id = ((r or {}).get("result") or {}).get("message_id") or last_id
    return last_id


def typing(token, chat_id):
    tg_post(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})


def answer_callback(token, callback_query_id):
    tg_post(token, "answerCallbackQuery", {"callback_query_id": callback_query_id})


def edit_keyboard_confirm(token, chat_id, message_id, text):
    """Edit a keyboard message to show confirmation text with buttons removed."""
    tg_post(token, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": {"inline_keyboard": []},
    })


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


CHART_RE    = re.compile(r'<<<CHART:(\w+):(.*?)>>>', re.DOTALL)
TELEGRAM_RE = re.compile(r'<telegram>(.*?)</telegram>', re.DOTALL | re.IGNORECASE)




def _profile_coaching_level(slug):
    """Read coaching_level from athlete profile.json, defaulting to 'mid'."""
    try:
        p = BASE.parent / "athletes" / slug / "profile.json"
        return json.loads(p.read_text()).get("coaching_level", "mid")
    except Exception:
        return "mid"


def process_charts(token, chat_id, response, slug=None):
    """Send any [[CHART:TYPE:JSON]] images, return response with markers stripped."""
    if _charts is None:
        return CHART_RE.sub('', response).strip()
    coaching_level = _profile_coaching_level(slug) if slug else "mid"
    sent_types = set()
    for m in CHART_RE.finditer(response):
        chart_type, raw = m.group(1), m.group(2)
        if chart_type in sent_types:
            log(f"skipping duplicate {chart_type} chart marker in response")
            continue
        try:
            data = json.loads(raw)
            png = None
            if chart_type in ("fitness", "form"):
                if isinstance(data, list):
                    data = {"data": data}
                if "today" not in data:
                    data["today"] = date.today().strftime("%m-%d")
                if chart_type == "fitness":
                    png = _charts.fitness_chart(data, coaching_level=coaching_level)
                else:
                    png = _charts.form_chart(data, coaching_level=coaching_level)
            elif chart_type == "week":
                png = _charts.week_chart(
                    data.get("events", []),
                    title=data.get("title", "Training week"),
                    week_start=data.get("week_start"),
                )
            elif chart_type == "load":
                # Always re-fetch live data from training_history and ICU events —
                # never render the model's JSON, which may be reconstructed from
                # conversation context and produce stale data or missing completed sessions.
                if slug:
                    try:
                        data = _build_load_payload(slug)
                    except Exception as _fe:
                        log(f"load chart live re-fetch failed, falling back to model data: {_fe}")
                log(f"load chart: days={len(data.get('days',[]))}, seed_ctl={data.get('seed_ctl')}, seed_atl={data.get('seed_atl')}")
                png = _charts.load_chart(data, coaching_level=coaching_level)
            elif chart_type == "powercurve":
                png = _charts.power_curve_chart(
                    data.get("efforts", []),
                    ftp=data.get("ftp", 316),
                )
            elif chart_type == "heat":
                # Always re-fetch live: acclimation_score() + heat-log.json, never the
                # model's JSON (same reasoning as the load chart).
                if slug:
                    try:
                        data = _build_heat_payload(slug)
                    except Exception as _fe:
                        log(f"heat chart live re-fetch failed, falling back to model data: {_fe}")
                png = _charts.heat_chart(data, coaching_level=coaching_level)
            if png:
                send_photo(token, chat_id, png)
                sent_types.add(chart_type)
        except Exception as e:
            log(f"Chart error ({chart_type}): {e}")
    text = CHART_RE.sub('', response).strip()
    # If model used <telegram> tags, extract only tagged content (discards all reasoning)
    m = TELEGRAM_RE.search(text)
    if m:
        return m.group(1).strip()
    # No tags — strip any leading reasoning before the first substantive line.
    # Heuristic: drop lines that look like internal narration.
    _REASONING_RE = re.compile(
        r'^(I\'ll |I will |Let me |Reading |Fetching |Checking |Now I |Looking |I need |I\'ve |'
        r'First |Step \d|Based on |The athlete |This is |Note:|Here I )',
        re.IGNORECASE
    )
    lines = text.splitlines()
    clean_lines = []
    dropping = True
    for line in lines:
        if dropping and _REASONING_RE.match(line.strip()):
            continue
        dropping = False
        clean_lines.append(line)
    return '\n'.join(clean_lines).strip() or text


PROJECT_DIR = BASE.parent.parent  # diamondpeak-site/
SITE_DATA     = BASE.parent / "site-data.json"

_ANKLE_RE         = re.compile(r'^(ankle|pain|niggle)\s+(?:(\w+[\s\w]*?)\s+)?(\d+(?:\.\d+)?)\s*$', re.I)
_RACE_PLAN_RE     = re.compile(r'^(?:regenerate|update|refresh|regen)\s+race\s+plan\s*$', re.I)
_CSS_RE           = re.compile(r'^css\s+([\d:]+)\s*(?:/100m)?\s*$', re.I)
_LTHR_RE          = re.compile(r'^lthr\s+(\d{2,3})\s*(?:bpm)?\s*$', re.I)
_WEEKLY_SUMMARY_RE = re.compile(
    r'^(?:weekly\s+summary|full\s+week\s+review|week\s+(?:summary|review)|run\s+weekly\s+summary)\s*$',
    re.I,
)
_WEIGHT_RE   = re.compile(r'^(?:weight|kg|weigh(?:ed)?)\s+([\d.]+)\s*(?:kg)?\s*$', re.I)
_HEAT_RE     = re.compile(r'^(?:heat|bath)\s+([\d.]+)\s*(?:min|m)?\s*$', re.I)
_PERIOD_RE    = re.compile(r'^period(?:\s+start(?:ed)?)?(?:\s+(today|yesterday|\d{4}-\d{2}-\d{2}))?\s*$', re.I)
_CYCLE_DAY_RE = re.compile(r'^cycle\s+day\s+(\d{1,2})\s*$', re.I)
_PLAN_RE     = re.compile(r'^(?:generate\s+plan|plan\s+(?:next\s+)?(?:2\s+)?weeks?|plan\s+ahead)\s*$', re.I)
_REPLAN_RE   = re.compile(r'^/?replan(?:\s+week)?\s*$', re.I)
_FTP_RE      = re.compile(r'^(?:ftp\s+(?:retest|result|update|new)|new\s+ftp)\s+([\d.]+)\s*(?:w(?:atts?)?)?\s*$', re.I)
_WEEK_CMD_RE     = re.compile(r'^/week\s*$', re.I)
_FORM_CMD_RE     = re.compile(r'^/form\s*$', re.I)
_RACE_CMD_RE     = re.compile(r'^/race\s*$', re.I)
# (chat_id, callback_data) -> last-handled timestamp, for command-callback debounce
_RECENT_CALLBACKS = {}
# (chat_id) -> expiry timestamp; set when replan confirmation is pending
_PENDING_REPLAN: dict[str, float] = {}

_LOAD_CMD_RE     = re.compile(r'^/load\s*$', re.I)
_FITNESS_CMD_RE  = re.compile(r'^/fitness\s*$', re.I)
_ACTIVITY_CMD_RE = re.compile(r'^/?activity(?:\s+check)?\s*$', re.I)
_GRAPHS_RE       = re.compile(r'^/?graphs\s*$', re.I)
_DURABILITY_RE   = re.compile(r'^/?durability\s*$', re.I)
_RECOVERY_RE     = re.compile(r'^/?recovery\s*$', re.I)
_COMPLIANCE_RE   = re.compile(r'^/?compliance\s*$', re.I)
_POWERCURVE_RE   = re.compile(r'^/?power\s?curve\s*$', re.I)
_FEEDBACK_LOG_RE = re.compile(
    r'^(?:feedback|note|log|correction|reminder|fyi|heads.?up)\s*:[ \t]*.{5,}',
    re.I | re.DOTALL,
)

# Persistent-menu button labels (with emoji) → fast-path sentinels.
_MENU_MAP = {
    "📈 graphs":         "__GRAPHS__",
    "🔄 replan week":    "__REPLAN__",
    "🔍 check activity": "__ACTIVITY_CHECK__",
    # Slash aliases so the same actions work from the Telegram command menu
    # (the always-visible menu button) as well as the reply-keyboard buttons.
    "/graphs":           "__GRAPHS__",
    "/replan":           "__REPLAN__",
    "/check":            "__ACTIVITY_CHECK__",
}

# The command menu shown by Telegram's menu button (setMyCommands). Registered at
# startup so the actions are reachable without clearing the text input / hunting
# the reply keyboard. Each maps to an existing handler (slash command, _MENU_MAP
# alias, or a natural-language question via _SLASH_QUESTION).
BOT_COMMANDS = [
    ("today",   "What's today's session?"),
    ("looking", "How am I looking? (readiness)"),
    ("week",    "This week's sessions + Load"),
    ("form",    "Fitness / Fatigue / Form + race projection"),
    ("race",    "Race predictor (Now / Race day / Target)"),
    ("fitness", "Fitness & Form charts"),
    ("load",    "Training-load chart (±8 days)"),
    ("graphs",  "All charts menu"),
    ("replan",  "Rebuild this week's plan"),
    ("check",   "Check a recent activity"),
    ("voice",   "Toggle spoken replies on/off"),
    ("help",    "Commands & how to use"),
]

# Slash commands that are shortcuts for a natural-language question routed to the
# coach (no dedicated handler — translated to text before routing).
_SLASH_QUESTION = {
    "/today":   "What's today's session?",
    "/looking": "How am I looking?",
}
_STRENGTH_RE     = re.compile(
    r'^(?:strength(?:\s+session)?|gym(?:\s+session)?|lift(?:ing)?|'
    r'what(?:\'s|\s+is)\s+(?:today\'?s?\s+)?(?:strength|gym)(?:\s+session)?)\s*$',
    re.I,
)

STAGE1_PLAN_SCRIPT = BASE.parent.parent / "ClaudeCoach/scripts/stage1-plan.py"

def _week_stats(slug: str, athlete_cfg: dict | None = None) -> str:
    """Python-only weekly training summary from athletes/{slug}/session-log.json."""
    session_log_f = BASE.parent / "athletes" / slug / "session-log.json"
    if not session_log_f.exists():
        return "No session log found yet."
    try:
        entries = json.loads(session_log_f.read_text())
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

    h, m   = divmod(int(total_min), 60)
    lines  = [f"*Week {week_start.strftime('%-d %b')} – {today.strftime('%-d %b')}*"]
    lines.append(f"*{int(total_tss)} Load* · {h}h {m:02d}m total\n")
    for sport, v in sorted(by_sport.items(), key=lambda x: -x[1]["tss"]):
        sh, sm = divmod(int(v["min"]), 60)
        lines.append(
            f"  {sport}: {v['n']} session{'s' if v['n'] > 1 else ''}"
            f" · {int(v['tss'])} Load · {sh}h{sm:02d}m"
        )
    if n_sessions and n_sessions > n_logged:
        diff = n_sessions - n_logged
        lines.append(f"\n_{diff} session{'s' if diff > 1 else ''} still awaiting feedback_")

    # Days-to-race footer
    if athlete_cfg:
        try:
            race_date_str = athlete_cfg.get("race_date", "")
            race_name     = athlete_cfg.get("race_name", "race")
            days_to_race  = (date.fromisoformat(race_date_str) - today).days
            lines.append(f"\n_{days_to_race} days to {race_name}_")
        except (ValueError, TypeError):
            pass

    return "\n".join(lines)


def _form_stats(slug: str, athlete_cfg: dict | None = None) -> str:
    """Python-only CTL/ATL/TSB + projected CTL to race day."""
    kpi     = {}
    history = []

    training_data = BASE.parent / "athletes" / slug / "training-data.json"
    if training_data.exists():
        try:
            td      = json.loads(training_data.read_text())
            kpi     = td.get("kpi", {})
            history = td.get("fitnessThis", [])
        except Exception:
            pass

    if not kpi.get("ctl") and SITE_DATA.exists():
        try:
            sd      = json.loads(SITE_DATA.read_text())
            ad      = sd.get("athletes", {}).get(slug, {})
            kpi     = {"ctl": ad.get("ctl"), "atl": ad.get("atl"), "tsb": ad.get("tsb")}
            history = ad.get("ctl_history", [])
        except Exception:
            pass

    ctl = kpi.get("ctl")
    if ctl is None:
        return "No fitness data yet — refresh pending."

    atl      = kpi.get("atl") or 0
    tsb      = ctl - atl
    today    = date.today()
    tsb_zone = "Fresh" if tsb > 5 else ("Load" if tsb > -20 else "Heavy")

    lines = [f"*Fitness {ctl:.1f}* · Fatigue {atl:.1f} · Form {tsb:+.1f} ({tsb_zone})"]

    # CTL target: prefer athletes.json ctl_targets.race_max, fallback 90
    ctl_race_target = 90.0
    if athlete_cfg:
        ctl_race_target = float(
            athlete_cfg.get("ctl_targets", {}).get("race_max", ctl_race_target)
        )

    if len(history) >= 28:
        old     = history[-28]
        old_ctl = old[1] if isinstance(old, list) else old.get("ctl", 0)
        weekly_ramp   = (ctl - old_ctl) / 4
        lines.append(f"4-wk ramp: *{weekly_ramp:+.1f}/wk*")

        if athlete_cfg:
            race_date_str = athlete_cfg.get("race_date", "")
            race_name     = athlete_cfg.get("race_name", "race")
            try:
                days_to_race  = (date.fromisoformat(race_date_str) - today).days
                projected_ctl = ctl + weekly_ramp * (days_to_race / 7)
                needed_ramp   = (ctl_race_target - ctl) / (days_to_race / 7) if days_to_race > 0 else 0
                if projected_ctl >= ctl_race_target * 0.95:
                    lines.append(f"On track: Fitness *{projected_ctl:.0f}* by race day ✓")
                else:
                    lines.append(
                        f"Projected: Fitness *{projected_ctl:.0f}* — "
                        f"need *{needed_ramp:+.1f}/wk* to hit {int(ctl_race_target)}"
                    )
                lines.append(f"_{days_to_race} days to {race_name}_")
            except (ValueError, TypeError):
                pass

    return "\n".join(lines)


def _race_stats(slug: str) -> str:
    """Python-only IM race prediction — the SHARED model in lib/race_predictor.py
    (IF ∝ √CTL vs previous-race anchor), same numbers as the website overview
    and plan_tools.py race-predict. No LLM call."""
    from race_predictor import race_predictor
    root = BASE.parent
    prof_p = root / "athletes" / slug / "profile.json"
    if not prof_p.exists():
        return "No profile found — the race predictor needs prev_race + race_predictor blocks."
    try:
        profile = json.loads(prof_p.read_text())
    except Exception:
        return "Profile unreadable — race predictor unavailable."
    ctl = None
    td_p = root / "athletes" / slug / "training-data.json"
    if td_p.exists():
        try:
            ctl = json.loads(td_p.read_text()).get("kpi", {}).get("ctl")
        except Exception:
            pass
    if ctl is None:
        return "No fitness data yet — refresh pending."
    rp = race_predictor(profile, ctl)
    if not rp:
        return ("Race predictor not configured for this athlete — profile needs "
                "prev_race (times, bike_if, bike_np_watts) and race_predictor (anchor_ctl).")

    def hm(m):
        m = int(round(m))
        return f"{m // 60}:{m % 60:02d}"

    a = rp["anchor"]
    lines = [f"*Race predictor* — anchored to {a['name']} "
             f"({hm(a['total_min'])} @ CTL {a['ctl']}, IF {a['if']})", ""]
    for r in rp["rows"]:
        lines.append(f"*{r['label']}* (CTL {r['ctl']}): *{hm(r['total_min'])}*")
        lines.append(f"  Swim {hm(r['swim_min'])} · Bike {hm(r['bike_min'])} "
                     f"@ {r['bike_w']}W (IF {r['if']:.2f}) · Run {hm(r['run_min'])} "
                     f"· T1+T2 {r['t12_min']}m")
    lines.append("")
    lines.append("_IF ∝ √CTL, FTP held fixed, IF capped at 0.75. Fitness is the only lever._")
    return "\n".join(lines)


_SPORT_MAP = {
    "VirtualRide": "Ride", "GravelRide": "Ride", "MountainBikeRide": "Ride",
    "EBikeRide": "Ride", "Cycling": "Ride",
    "TrailRun": "Run", "VirtualRun": "Run",
    "OpenWaterSwim": "Swim", "Swim": "Swim",
    "WeightTraining": "Strength", "Workout": "Strength",
    "Elliptical": "Strength",
}

def _bot_norm_sport(s):
    return _SPORT_MAP.get(s, s) if s in ("Ride", "Run", "Swim", "Strength") or s in _SPORT_MAP else s


def _build_load_payload(slug: str) -> dict:
    """Fetch live ICU data and build the load_chart payload.
    Always re-fetches from training_history and ICU events — never uses cached context.
    Called by both _load_chart_quick (fast path) and process_charts (Claude path) so
    the chart data is always authoritative regardless of how the render was triggered."""
    sys.path.insert(0, str(BASE.parent / "lib"))
    from icu_api import IcuClient

    athletes_data = json.loads(ATHLETES_CONFIG.read_text())
    a = athletes_data[slug]
    client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])

    today = date.today()
    end_date = (today + timedelta(days=8)).isoformat()

    wellness, history_acts, events = client.fetch_all(
        ("get_wellness", 10),
        ("get_training_history", 10),
        ("get_events", today.isoformat(), end_date),
    )

    seed_ctl = seed_atl = None
    if wellness:
        w = wellness[-1]
        seed_ctl = round(float(w.get("ctl") or 0), 1)
        seed_atl = round(float(w.get("atl") or 0), 1)

    tsb_by_date = {}
    for w in (wellness or []):
        d = (w.get("id") or "")[:10]
        if d:
            tsb_by_date[d] = round((w.get("ctl") or 0) - (w.get("atl") or 0), 1)

    acts_by_date = {}
    for act in (history_acts or []):
        d = (act.get("start_date_local") or "")[:10]
        if not d:
            continue
        sport = _bot_norm_sport(act.get("type", "Other"))
        tss = round(float(act.get("icu_training_load") or 0), 1)
        dur = round((act.get("moving_time") or 0) / 60)
        acts_by_date.setdefault(d, []).append(
            {"sport": sport, "tss": tss, "dur": dur, "status": "completed"}
        )

    plans_by_date = {}
    for ev in (events or []):
        d = (ev.get("start_date_local") or "")[:10]
        if not d or d < today.isoformat():
            continue
        sport = _bot_norm_sport(ev.get("type") or ev.get("category") or "Other")
        tss = round(float(ev.get("icu_training_load") or ev.get("load_target") or 0), 1)
        dur = round((ev.get("moving_time") or 0) / 60)
        plans_by_date.setdefault(d, []).append(
            {"sport": sport, "tss": tss, "dur": dur, "status": "planned"}
        )

    days = []
    today_str = today.isoformat()
    for i in range(-8, 8):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        if d_str < today_str:
            acts = acts_by_date.get(d_str, [])
            tsb = tsb_by_date.get(d_str)
        else:
            acts = list(acts_by_date.get(d_str, []))
            done = {a["sport"] for a in acts}
            acts += [p for p in plans_by_date.get(d_str, []) if p["sport"] not in done]
            tsb = tsb_by_date.get(d_str) if d_str == today_str else None
        days.append({"date": d_str, "tsb": tsb, "activities": acts})

    return {
        "today": today.strftime("%m-%d"),
        "seed_ctl": seed_ctl,
        "seed_atl": seed_atl,
        "days": days,
    }


def _build_heat_payload(slug: str, window_days: int = 45) -> dict:
    """Build the heat_chart payload from heat.py's acclimation_score() + the
    athlete's heat-log.json. Always re-fetched live, same reasoning as load chart."""
    sys.path.insert(0, str(BASE.parent / "lib"))
    import heat as heat_lib

    today = date.today()
    days = []
    for i in range(window_days, -1, -1):
        d = today - timedelta(days=i)
        days.append({"date": d.isoformat(), "score": round(heat_lib.acclimation_score(slug, d), 1)})

    log_file = BASE.parent / "athletes" / slug / "heat-log.json"
    try:
        entries = json.loads(log_file.read_text())
    except Exception:
        entries = []
    cutoff = (today - timedelta(days=window_days)).isoformat()
    events = [
        {"date": str(e.get("date") or "")[:10], "dose": e.get("dose") or 1.0,
         "method": e.get("method") or ""}
        for e in entries if str(e.get("date") or "")[:10] >= cutoff
    ]

    return {"today": today.strftime("%m-%d"), "days": days, "events": events}


def _load_chart_quick(token, chat_id, slug):
    """Fetch live data and send the load chart directly — no Claude round-trip."""
    if _charts is None:
        send(token, chat_id, "Chart library not available.", reply_markup=build_keyboard(slug))
        return
    try:
        payload = _build_load_payload(slug)
        seed_ctl = payload.get("seed_ctl")
        seed_atl = payload.get("seed_atl")
        log(f"load chart (quick): days={len(payload.get('days',[]))}, seed_ctl={seed_ctl}, seed_atl={seed_atl}")
        png = _charts.load_chart(payload, coaching_level=_profile_coaching_level(slug))
        if png:
            send_photo(token, chat_id, png)
            if seed_ctl is not None:
                tsb = round(seed_ctl - seed_atl, 1)
                send(token, chat_id,
                     f"Fitness *{seed_ctl}* · Fatigue {seed_atl} · Form *{tsb:+.1f}*",
                     reply_markup=build_keyboard(slug))
        else:
            send(token, chat_id, "Could not generate chart.", reply_markup=build_keyboard(slug))
    except Exception as e:
        log(f"load chart quick error: {e}")
        send(token, chat_id, f"Chart error: {e}", reply_markup=build_keyboard(slug))


def _fitness_charts_quick(token, chat_id, slug):
    """Fetch 42-day wellness and send fitness (CTL/ATL) + form (TSB) charts — no Claude round-trip."""
    if _charts is None:
        send(token, chat_id, "Chart library not available.", reply_markup=build_keyboard(slug))
        return
    try:
        sys.path.insert(0, str(BASE.parent / "lib"))
        from icu_api import IcuClient

        athletes_data = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes_data[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])

        today = date.today()
        wellness = client.get_wellness(42)
        if not wellness:
            send(token, chat_id, "No fitness data available.", reply_markup=build_keyboard(slug))
            return

        data = []
        for w in wellness:
            d = (w.get("id") or "")[:10]
            ctl = w.get("ctl") or 0
            atl = w.get("atl") or 0
            if d:
                data.append({"date": d, "ctl": round(float(ctl), 1),
                              "atl": round(float(atl), 1),
                              "tsb": round(float(ctl) - float(atl), 1)})

        # Project the next 14 days of CTL/ATL/TSB from planned sessions, so the
        # fitness + form charts show where training is heading, not just history.
        try:
            sys.path.insert(0, str(BASE.parent / "ironman-analysis"))
            from primitives.load import project_pmc_daily
            end14 = (today + timedelta(days=14)).isoformat()
            planned = {}
            for ev in (client.get_events(today.isoformat(), end14) or []):
                d = (ev.get("start_date_local") or "")[:10]
                if d and d > today.isoformat():
                    planned[d] = planned.get(d, 0) + float(ev.get("icu_training_load") or ev.get("load_target") or 0)
            if data:
                fdates = [(today + timedelta(days=i)).isoformat() for i in range(1, 15)]
                ftss = [planned.get(d, 0) for d in fdates]
                for d, p in zip(fdates, project_pmc_daily(data[-1]["ctl"], data[-1]["atl"], ftss)):
                    data.append({"date": d, "ctl": round(p["ctl"], 1),
                                 "atl": round(p["atl"], 1), "tsb": round(p["tsb"], 1),
                                 "projected": True})
        except Exception as exc:
            log(f"fitness projection skipped: {exc}")

        # Phase bands (Base/Build/Specific/Peak/Taper) clamped to the chart window,
        # so the chart shows where in the plan each date sits. x0/x1 are MM-DD labels
        # that exist in the data (daily series), so box annotations align.
        phases = []
        try:
            if data and a.get("plan_start") and a.get("race_date"):
                ps = date.fromisoformat(a["plan_start"]); rd = date.fromisoformat(a["race_date"])
                pt = a.get("phase_tss", {})
                spans, prev = [], ps
                for nm, wk, col in (("Base", pt.get("base_end_week", 6), "rgba(41,128,185,0.07)"),
                                    ("Build", pt.get("build_end_week", 10), "rgba(29,104,64,0.07)"),
                                    ("Specific", pt.get("specific_end_week", 14), "rgba(39,174,96,0.07)"),
                                    ("Peak", pt.get("peak_end_week", 17), "rgba(192,57,43,0.07)")):
                    spans.append((nm, prev, ps + timedelta(weeks=wk), col)); prev = ps + timedelta(weeks=wk)
                spans.append(("Taper", prev, rd, "rgba(124,77,255,0.07)"))
                lo = date.fromisoformat(data[0]["date"]); hi = date.fromisoformat(data[-1]["date"])
                for nm, s, e, col in spans:
                    s2, e2 = max(s, lo), min(e, hi)
                    if s2 < e2:
                        phases.append({"name": nm, "x0": s2.strftime("%m-%d"),
                                       "x1": e2.strftime("%m-%d"), "color": col})
        except Exception as exc:
            log(f"phase bands skipped: {exc}")

        payload = {"today": today.strftime("%m-%d"), "data": data, "phases": phases}
        log(f"fitness charts (quick): {len(data)} days, {len(phases)} phases")
        cl = _profile_coaching_level(slug)
        png_fit = _charts.fitness_chart(payload, coaching_level=cl)
        png_form = _charts.form_chart(payload, coaching_level=cl)
        if png_fit:
            send_photo(token, chat_id, png_fit)
        if png_form:
            send_photo(token, chat_id, png_form)
        if png_fit or png_form:
            w = wellness[-1]
            ctl = round(float(w.get("ctl") or 0), 1)
            atl = round(float(w.get("atl") or 0), 1)
            tsb = round(ctl - atl, 1)
            send(token, chat_id,
                 f"Fitness *{ctl}* · Fatigue {atl} · Form *{tsb:+.1f}*",
                 reply_markup=build_keyboard(slug))
        else:
            send(token, chat_id, "Could not generate charts.", reply_markup=build_keyboard(slug))
    except Exception as e:
        log(f"fitness charts quick error: {e}")
        send(token, chat_id, f"Chart error: {e}", reply_markup=build_keyboard(slug))


def _recovery_chart_quick(token, chat_id, slug):
    """HRV (vs rolling baseline) + RHR + sleep — no Claude round-trip."""
    if _charts is None:
        send(token, chat_id, "Chart library not available.", reply_markup=build_keyboard(slug)); return
    try:
        sys.path.insert(0, str(BASE.parent / "lib"))
        from icu_api import IcuClient
        a = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        today = date.today()
        days = []
        for w in (client.get_wellness(42) or []):
            d = (w.get("id") or "")[:10]
            if not d:
                continue
            ss = w.get("sleepSecs")
            days.append({"date": d, "hrv": w.get("hrv"), "rhr": w.get("restingHR"),
                         "sleep_h": round(ss / 3600, 1) if ss else None})
        if not any(x.get("hrv") for x in days) and not any(x.get("rhr") for x in days):
            send(token, chat_id, "No recovery data (HRV/RHR/sleep) available yet.", reply_markup=build_keyboard(slug)); return
        payload = {"today": today.strftime("%m-%d"), "days": days}
        log(f"recovery chart (quick): {len(days)} days")
        png = _charts.recovery_chart(payload, coaching_level=_profile_coaching_level(slug))
        if png:
            send_photo(token, chat_id, png)
            last = days[-1]
            send(token, chat_id,
                 f"HRV *{last.get('hrv') or '—'}* · RHR {last.get('rhr') or '—'} · sleep {last.get('sleep_h') or '—'}h",
                 reply_markup=build_keyboard(slug))
        else:
            send(token, chat_id, "Could not generate chart.", reply_markup=build_keyboard(slug))
    except Exception as e:
        log(f"recovery chart error: {e}")
        send(token, chat_id, f"Chart error: {e}", reply_markup=build_keyboard(slug))


def _durability_chart_quick(token, chat_id, slug):
    """Aerobic decoupling (Pa:HR) per long session — bike + run, lower = better."""
    if _charts is None:
        send(token, chat_id, "Chart library not available.", reply_markup=build_keyboard(slug)); return
    try:
        adir = BASE.parent / "athletes" / slug
        sessions = []
        for fn, sport in (("decoupling-log.json", "Ride"), ("run-durability-log.json", "Run")):
            f = adir / fn
            if not f.exists():
                continue
            try:
                for e in json.loads(f.read_text()):
                    if e.get("decoupling_pct") is not None and e.get("date"):
                        sessions.append({"date": e["date"][:10],
                                         "decoupling_pct": round(float(e["decoupling_pct"]), 1),
                                         "sport": sport, "if": e.get("if"),
                                         "duration_min": e.get("duration_min")})
            except Exception:
                pass
        sessions.sort(key=lambda s: s["date"])
        sessions = sessions[-12:]
        if not sessions:
            send(token, chat_id, "No durability data yet (needs long rides/runs with decoupling logged).",
                 reply_markup=build_keyboard(slug)); return
        payload = {"today": date.today().strftime("%m-%d"), "sessions": sessions}
        log(f"durability chart (quick): {len(sessions)} sessions")
        png = _charts.durability_chart(payload, coaching_level=_profile_coaching_level(slug))
        if png:
            send_photo(token, chat_id, png)
            send(token, chat_id, "Lower decoupling = better durability (holding power/pace late).",
                 reply_markup=build_keyboard(slug))
        else:
            send(token, chat_id, "Could not generate chart.", reply_markup=build_keyboard(slug))
    except Exception as e:
        log(f"durability chart error: {e}")
        send(token, chat_id, f"Chart error: {e}", reply_markup=build_keyboard(slug))


def _compliance_chart_quick(token, chat_id, slug):
    """Per-week planned vs actual TSS for the last ~8 weeks."""
    if _charts is None:
        send(token, chat_id, "Chart library not available.", reply_markup=build_keyboard(slug)); return
    try:
        from collections import defaultdict
        sys.path.insert(0, str(BASE.parent / "lib"))
        from icu_api import IcuClient
        a = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        today = date.today()

        def _wk(dstr):
            dd = date.fromisoformat(dstr[:10])
            return (dd - timedelta(days=dd.weekday())).isoformat()

        actual, planned = defaultdict(float), defaultdict(float)
        for act in (client.get_training_history(63) or []):
            d = (act.get("start_date_local") or "")[:10]
            if d:
                actual[_wk(d)] += float(act.get("icu_training_load") or 0)
        for ev in (client.get_events((today - timedelta(days=63)).isoformat(),
                                      (today + timedelta(days=7)).isoformat()) or []):
            d = (ev.get("start_date_local") or "")[:10]
            if d:
                planned[_wk(d)] += float(ev.get("icu_training_load") or ev.get("load_target") or 0)
        this_wk = _wk(today.isoformat())
        wks = sorted(w for w in set(list(actual) + list(planned)) if w <= this_wk)[-8:]
        if not wks:
            send(token, chat_id, "No weekly data available.", reply_markup=build_keyboard(slug)); return
        weeks = [{"label": date.fromisoformat(w).strftime("%m-%d"),
                  "planned": round(planned.get(w, 0)), "actual": round(actual.get(w, 0))} for w in wks]
        payload = {"today": today.strftime("%m-%d"), "weeks": weeks}
        log(f"compliance chart (quick): {len(weeks)} weeks")
        png = _charts.compliance_chart(payload, coaching_level=_profile_coaching_level(slug))
        if png:
            send_photo(token, chat_id, png)
            send(token, chat_id, "Planned vs actual weekly Load — are you hitting the plan?",
                 reply_markup=build_keyboard(slug))
        else:
            send(token, chat_id, "Could not generate chart.", reply_markup=build_keyboard(slug))
    except Exception as e:
        log(f"compliance chart error: {e}")
        send(token, chat_id, f"Chart error: {e}", reply_markup=build_keyboard(slug))


# Standard power-curve durations (mirrors scripts/refresh-site-data.py _POWER_DURATIONS).
_PC_DURATIONS = [
    (5, "5s"), (15, "15s"), (30, "30s"), (60, "1m"), (120, "2m"), (300, "5m"),
    (600, "10m"), (1200, "20m"), (1800, "30m"), (3600, "60m"), (5400, "90m"),
]


def _power_curve_quick(token, chat_id, slug):
    """90-day cycling power curve, pulled straight from intervals.icu (no Claude).

    Replaces the old Claude-emitted [[CHART:powercurve:JSON]] path, which broke
    whenever the model hand-wrote malformed JSON for the ~11-point efforts array.
    """
    if _charts is None:
        send(token, chat_id, "Chart library not available.", reply_markup=build_keyboard(slug)); return
    try:
        sys.path.insert(0, str(BASE.parent / "lib"))
        from icu_api import IcuClient
        a = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])

        pc_raw = client.get_power_curves(sport="Ride", curves="90d")
        curve  = (pc_raw.get("list") or [None])[0]
        if not curve or not curve.get("secs"):
            send(token, chat_id, "No cycling power-curve data in the last 90 days.",
                 reply_markup=build_keyboard(slug)); return
        secs_to_w = dict(zip(curve.get("secs", []), curve.get("values", [])))
        efforts = [{"label": lbl, "power": round(secs_to_w[t])}
                   for t, lbl in _PC_DURATIONS if secs_to_w.get(t)]
        if not efforts:
            send(token, chat_id, "No cycling power-curve data in the last 90 days.",
                 reply_markup=build_keyboard(slug)); return

        # Resolve FTP: profile.json first, then the athlete's cycling FTP, else 316.
        ftp = _load_profile(slug).get("ftp_watts")
        if not ftp:
            try:
                prof = client.get_athlete_profile()
                for sp in (prof.get("sportSettings") or prof.get("sport_settings") or []):
                    if (sp.get("type") in ("Ride", "VirtualRide", "Cycling")) and sp.get("ftp"):
                        ftp = sp["ftp"]; break
            except Exception:
                pass
        ftp = int(ftp or 316)

        log(f"power curve (quick): {len(efforts)} efforts, ftp={ftp}")
        png = _charts.power_curve_chart(efforts, ftp=ftp)
        if png:
            send_photo(token, chat_id, png)
            send(token, chat_id, f"90-day best power by duration · FTP {ftp}W.",
                 reply_markup=build_keyboard(slug))
        else:
            send(token, chat_id, "Could not generate chart.", reply_markup=build_keyboard(slug))
    except Exception as e:
        log(f"power curve chart error: {e}")
        send(token, chat_id, f"Chart error: {e}", reply_markup=build_keyboard(slug))


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

def _athlete_dir(slug: str) -> Path:
    return BASE.parent / "athletes" / slug

def _load_state_json(slug: str):
    f = _athlete_dir(slug) / "current-state.json"
    if f.exists():
        return json.loads(f.read_text())
    return {}

def _save_state_json(slug: str, state):
    f = _athlete_dir(slug) / "current-state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2))
    _invalidate_prefetch(slug)  # freshly-logged values must show in the next reply's context


# Files a capture/log could plausibly write to. Deliberately EXCLUDES the
# cron-churned artefacts (fitness-*-cache.json, current-state.md, system_prompt.txt,
# *_state.json) so an unrelated refresh landing mid-reply cannot be mistaken for a
# successful capture write. current-state.json carries pain/weight/menstrual state.
_CAPTURE_TARGET_FILES = (
    "feedback-log.json", "session-log.json", "persistent-rules.md",
    "current-state.json", "heat-log.json", "swim-log.json",
    "decoupling-log.json", "run-durability-log.json", "pending-captures.json",
)

# A reply that ASSERTS something was persisted. Kept tight (log/save verbs only,
# short reply) so plan-edit confirmations such as "Updated your Sunday ride" — which
# write to intervals.icu, not a local file — are not mistaken for a local capture.
_CAPTURE_CONFIRM_RE = re.compile(
    r"^\W*(logged|saved|noted|recorded|captured)\b", re.IGNORECASE)


def _capture_written_since(slug: str, before_ts: float) -> bool:
    """True if any athlete capture-target file was written since before_ts."""
    adir = _athlete_dir(slug)
    for fname in _CAPTURE_TARGET_FILES:
        f = adir / fname
        try:
            if f.exists() and f.stat().st_mtime >= before_ts:
                return True
        except Exception:
            pass
    return False


def _stash_pending_capture(slug: str, text: str, reply: str) -> None:
    """No-loss deterministic fallback: when the model claimed a save but wrote
    nothing (even after a retry), persist the athlete's raw message so it is never
    lost and stays visible/recoverable in git."""
    if not text:
        return
    try:
        pf = _athlete_dir(slug) / "pending-captures.json"
        try:
            entries = json.loads(pf.read_text()) if pf.exists() else []
        except Exception:
            entries = []
        entries.append({
            "logged_at": datetime.now().isoformat(timespec="seconds"),
            "message": text,
            "model_reply": (reply or "")[:200],
            "note": "auto-stashed after a silent write failure — needs filing",
        })
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(json.dumps(entries, indent=2))
        _git_commit(f"auto: stash unfiled capture {slug} {date.today().isoformat()}")
    except Exception as e:
        log(f"[{slug}] pending-capture stash failed: {e}")


def _make_capture_retry(text, config, history, model, files, athlete_name, context):
    """Build a no-arg callable that re-asks the model to actually perform a write
    it claimed to have done. Only invoked by _verify_logged_reply on a detected
    silent write failure, so it adds no latency on the happy path."""
    def _retry():
        retry_msg = (
            "SYSTEM CHECK: your previous reply said something was logged or saved, but no "
            "file was actually written. Perform the pending file write NOW using the Write "
            "or Edit tool for my most recent message, then reply with one short line naming "
            f"what you saved. My message was: {text!r}"
        )
        return call_claude(retry_msg, config, history, model=model,
                           system_prompt_file=files["system_prompt"],
                           athlete_name=athlete_name, context=context)
    return _retry


def _verify_logged_reply(slug: str, before_ts: float, clean: str,
                         text: str = None, retry=None) -> str:
    """Guard against silent write failures: never tell the athlete something was
    saved unless a capture file was actually written.

    If the reply asserts a save but nothing was written, retry the write once (via
    `retry`, a no-arg callable returning the new reply text); if that still writes
    nothing, deterministically stash the raw message so it is never lost and return
    an honest confirmation rather than a false success."""
    stripped = clean.strip()
    if not _CAPTURE_CONFIRM_RE.match(stripped) or len(stripped) > 140:
        return clean
    if _capture_written_since(slug, before_ts):
        return clean  # write confirmed — genuine success
    log(f"[{slug}] WARN: reply claimed a save but no capture file was updated — retrying write")
    if retry is not None:
        # Small backward grace so the retry's own write is never missed to mtime/
        # float precision. Safe: the retry model call takes seconds, far longer than
        # this margin, so it cannot re-count a pre-retry write.
        retry_ts = time.time() - 2
        try:
            retry_reply = retry()
        except Exception as e:
            log(f"[{slug}] capture retry errored: {e}")
            retry_reply = ""
        if _capture_written_since(slug, retry_ts):
            log(f"[{slug}] capture retry succeeded")
            rr = (retry_reply or "").strip()
            if (rr and _CAPTURE_CONFIRM_RE.match(rr)
                    and len(rr) <= 200 and rr.lower() != "logged."):
                return rr
            return "Saved that to your file."
    # Retry failed or unavailable — never lose the data, never fake success.
    _stash_pending_capture(slug, text, clean)
    log(f"[{slug}] WARN: silent write failure — stashed raw message to pending-captures.json")
    return ("I couldn't file that automatically, so I've saved your note so nothing is lost "
            "and flagged it for a fix. No need to resend.")


_SESSION_REF_RE = re.compile(
    r"\b(today'?s|tomorrow'?s|this (morning|afternoon|evening)'?s)?\s*"
    r"(session|ride|run|swim|workout|brick|race|set|interval)\b", re.IGNORECASE)
# Target language now includes Load/TSS (Phase 1, 11 Jul): a session prescribed
# purely by Load with no duration/distance ("Sunday's ride: 220 Load, Z2") is the
# exact "lead with duration/distance" miss this backstop exists to catch, but the
# old pattern only matched pace/power/HR language.
_TARGET_LANG_RE = re.compile(
    r"\b(pace|power|watts?|w/kg|threshold|ftp|zone\s?\d|heart rate|hr|bpm|target|effort|load|tss)\b",
    re.IGNORECASE)
_DURATION_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(min|mins|minute|minutes|hour|hours|hr|hrs)\b", re.IGNORECASE)
_DISTANCE_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(km|kilometre|kilometer|kilometres|kilometers|mile|miles|mi)\b",
    re.IGNORECASE)


# Retrospective markers — a reply reviewing a COMPLETED session should not have an
# upcoming session's duration appended. Guards the broadened Load/TSS trigger below
# against firing on backward-looking comments ("nice run yesterday, load's climbing").
_RETRO_RE = re.compile(
    r"\b(yesterday|last (night|week|session)|earlier|this morning'?s? (was|felt)|"
    r"completed|logged|you (did|ran|rode|swam|nailed|smashed)|that was|well done|great (work|session))\b",
    re.IGNORECASE)


def _preview_missing_duration(clean: str) -> bool:
    """True if the reply PRESCRIBES an upcoming/in-progress session in pace/power/
    HR/Load/target language but states no duration or distance figure anywhere.
    Skipped for clearly retrospective replies (a completed-session review)."""
    return bool(
        _SESSION_REF_RE.search(clean)
        and _TARGET_LANG_RE.search(clean)
        and not _DURATION_RE.search(clean)
        and not _DISTANCE_RE.search(clean)
        and not _RETRO_RE.search(clean)
    )


_SPORT_FAMILIES = {
    "Run":   ("run", "runs", "running", "jog", "jogging", "tempo run", "long run"),
    "Ride":  ("ride", "rides", "bike", "biking", "cycle", "cycling", "turbo", "spin"),
    "Swim":  ("swim", "swims", "swimming", "pool", "open water"),
}
# ICU event types that count as each family.
_SPORT_TYPES = {
    "Run":  {"Run"},
    "Ride": {"Ride", "VirtualRide"},
    "Swim": {"Swim", "OpenWaterSwim"},
}
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def _preview_sport_types(clean: str):
    """The set of ICU event types the reply is about, ONLY if it references exactly
    one sport family (run / ride / swim). Returns None if zero or more-than-one family
    is mentioned (ambiguous — e.g. race-strategy chat naming both bike and run), or if
    a multi-sport 'brick' is mentioned. None means: do not try to append a duration."""
    low = clean.lower()
    if re.search(r"\bbrick\b", low):
        return None
    fams = [fam for fam, words in _SPORT_FAMILIES.items()
            if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in words)]
    if len(fams) != 1:
        return None
    return _SPORT_TYPES[fams[0]]


def _preview_target_date(clean: str):
    """The single calendar date the reply prescribes, ONLY if it names exactly one
    (today / tomorrow / tonight / this morning|afternoon|evening, or one weekday).
    Returns None if zero or more-than-one distinct day is referenced — we only append
    a duration when we can tie it to one specific session."""
    low = clean.lower()
    today = date.today()
    cands = set()
    if re.search(r"\b(today'?s?|this (morning|afternoon|evening)|tonight)\b", low):
        cands.add(today)
    if re.search(r"\btomorrow'?s?\b", low):
        cands.add(today + timedelta(days=1))
    for name, wd in _WEEKDAYS.items():
        if re.search(r"\b" + name + r"'?s?\b", low):
            ahead = (wd - today.weekday()) % 7  # weekday name == today => today
            cands.add(today + timedelta(days=ahead))
    return next(iter(cands)) if len(cands) == 1 else None


def _verify_session_preview(slug: str, clean: str) -> str:
    """Deterministic backstop for the 'always state duration/distance in session
    previews' rule (persistent-rules.md). Only fires when the reply PROVABLY prescribes
    one specific near-term session: it must name a single day (today / tomorrow / a
    weekday) AND a single sport, and that (day, sport) must resolve to exactly ONE
    upcoming ICU event. Only then is THAT event's own duration/distance appended.

    If the reference is ambiguous or cannot be matched to a unique event, nothing is
    appended and the miss is logged for a human. The bot never appends a duration it
    cannot tie to the session in the reply, and never shows the athlete an internal
    'not confirmed' warning — both previously leaked arbitrary/irrelevant durations
    (e.g. the 4h long ride's 240min tacked onto a 10k pace discussion) and scary
    internal text into athlete-visible replies (misfires 2026-07-22/23)."""
    if not _preview_missing_duration(clean):
        return clean

    target = _preview_target_date(clean)
    sport_types = _preview_sport_types(clean)
    if target is None or sport_types is None:
        log(f"[{slug}] session preview: no single day+sport in reply "
            "(ambiguous or strategy chat) — no append")
        return clean

    try:
        sys.path.insert(0, str(BASE.parent / "lib"))
        from icu_api import IcuClient
        athletes = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        today = date.today()
        events = client.get_events(today.isoformat(), (today + timedelta(days=8)).isoformat())
        # A session PREVIEW is one not yet done. If the target sport is already
        # completed on the target date, the reply is a retrospective review
        # (e.g. analysing this morning's finished ride) — never append a preview
        # duration to it (misfires 2026-07-23: the completed 4h ride's duration
        # was tacked onto post-ride analysis replies).
        hist = client.get_training_history(days=3)
        if any((h.get("start_date_local") or "")[:10] == target.isoformat()
               and (h.get("type") or "") in sport_types for h in (hist or [])):
            log(f"[{slug}] session preview: {target.isoformat()} {sorted(sport_types)} "
                "already completed — retrospective, no append")
            return clean
        matches = [e for e in (events or [])
                   if (e.get("start_date_local") or "")[:10] == target.isoformat()
                   and (e.get("type") or "") in sport_types
                   and (e.get("moving_time") or e.get("distance"))]
        if len(matches) != 1:
            log(f"[{slug}] session preview: {len(matches)} events match "
                f"{target.isoformat()}/{sorted(sport_types)} — no append (need exactly 1)")
            return clean
        ev = matches[0]
        bits = []
        dur = round((ev.get("moving_time") or 0) / 60)
        if dur:
            bits.append(f"{dur}min")
        dist = ev.get("distance") or 0
        if dist:
            bits.append(f"{dist / 1000:.1f}km")
        if bits:
            return clean.rstrip() + f"\n\n({' / '.join(bits)})"
    except Exception as e:
        log(f"[{slug}] session preview duration lookup failed: {e}")
    return clean



def _load_profile(slug: str) -> dict:
    f = _athlete_dir(slug) / "profile.json"
    try:
        return json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        return {}


def _voice_mode_on(slug: str) -> bool:
    """Sticky per-athlete voice-reply toggle (persists in profile.json, gitignored)."""
    return bool(_load_profile(slug).get("voice_reply"))


def _set_voice_mode(slug: str, on: bool) -> None:
    f = _athlete_dir(slug) / "profile.json"
    try:
        prof = json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        prof = {}
    prof["voice_reply"] = bool(on)
    f.write_text(json.dumps(prof, indent=2, ensure_ascii=False))


def _strength_session(slug: str) -> str:
    """Return today's prescribed strength session based on day of week."""
    dow = date.today().weekday()  # Mon=0, Sun=6

    # Session A: Tue/Sat | Session B: Thu/Sun | Off: Mon/Wed/Fri
    if dow in (1, 5):  # Tuesday or Saturday
        session = "A"
        exercises = (
            "1. Bulgarian split squat — 3 × 8 each\n"
            "2. Romanian deadlift — 3 × 8\n"
            "3. Step-ups (high box) — 3 × 10 each\n"
            "4. *Single-leg calf raise — 3 × 15 each* ⬅ ankle priority\n"
            "5. Hip thrust — 3 × 12"
        )
        focus = "Lower-body power"
    elif dow in (3, 6):  # Thursday or Sunday
        session = "B"
        exercises = (
            "1. Single-leg deadlift — 3 × 8 each\n"
            "2. Nordic hamstring curl — 3 × 6\n"
            "3. Lateral band walk — 3 × 20 steps\n"
            "4. Dead bug — 3 × 10 each\n"
            "5. *Copenhagen plank — 3 × 20 sec each* ⬅ ankle priority"
        )
        focus = "Stability + injury prevention"
    else:
        return "No strength session scheduled today. Next: Session A (Tue) or Session B (Thu)."

    state = _load_state_json(slug)
    pain = state.get("ankle", {}).get("pain_during", 0) or 0
    ankle_note = ""
    if pain and pain > 0:
        ankle_note = f"\n\n⚠️ Ankle pain {pain}/10 logged — reduce calf raise load if needed. Pain-led, not plan-led."

    return (
        f"*Session {session} — {focus}*\n\n"
        f"{exercises}\n\n"
        f"Finish with 10 min mobility: hip flexor + calf + Achilles stretch.{ankle_note}"
    )


def fast_path(text, slug: str = "", athlete_cfg: dict | None = None):
    """
    Returns a reply string if the message can be handled without calling Claude,
    or None if Claude should handle it.
    """
    today = date.today().isoformat()
    txt   = text.strip()

    if txt.lower() in _MENU_MAP:           # persistent-menu button taps
        return _MENU_MAP[txt.lower()]

    if _WEEK_CMD_RE.match(txt):
        return _week_stats(slug, athlete_cfg)

    if _FORM_CMD_RE.match(txt):
        return _form_stats(slug, athlete_cfg)

    if _RACE_CMD_RE.match(txt):
        return _race_stats(slug)

    if _LOAD_CMD_RE.match(txt):
        return "__LOAD_CHART__"

    if _FITNESS_CMD_RE.match(txt):
        return "__FITNESS_CHARTS__"

    if _ACTIVITY_CMD_RE.match(txt):
        return "__ACTIVITY_CHECK__"

    if _GRAPHS_RE.match(txt):
        return "__GRAPHS__"
    if _DURABILITY_RE.match(txt):
        return "__DURABILITY__"
    if _RECOVERY_RE.match(txt):
        return "__RECOVERY__"
    if _COMPLIANCE_RE.match(txt):
        return "__COMPLIANCE__"
    if _POWERCURVE_RE.match(txt):
        return "__POWERCURVE__"

    m = _ANKLE_RE.match(txt)
    if m and slug:
        kw = m.group(1).lower()
        location = "ankle" if kw == "ankle" else (m.group(2) or "general").strip().lower()
        score = float(m.group(3))
        state = _load_state_json(slug)
        # The ankle keeps its long-standing block (R1 modulation, watchdog and the
        # morning card read it); every other location tracks independently under
        # state["pain"][location] so trends never mix across body parts.
        if location == "ankle":
            block = state.setdefault("ankle", {})
            prev  = block.get("pain_during")
            block["pain_during"] = int(score)
        else:
            block = state.setdefault("pain", {}).setdefault(location, {})
            prev  = block.get("current")
            block["current"] = int(score)
            block["last_logged"] = today

        # Rolling pain history — per location, keep last 20 readings
        hist = block.setdefault("history", [])
        hist.append({"date": today, "score": int(score)})
        block["history"] = hist[-20:]

        state["last_updated"] = today
        _save_state_json(slug, state)
        _git_commit(f"auto: {location} pain {score} {slug} {today}")

        trend = ""
        if prev is not None:
            if score > prev:
                trend = f" (up from {prev} — monitor)"
            elif score < prev:
                trend = f" (down from {prev} — improving)"

        reply = f"Logged {location} pain {int(score)}/10{trend}."

        # Trend and rebalancing alerts — within this location's history only
        recent_scores = [h["score"] for h in block["history"][-3:]]
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
    if m and slug:
        kg = float(m.group(1))
        state = _load_state_json(slug)
        state.setdefault("weight_readings", []).append({"date": today, "kg": kg})
        state["last_updated"] = today
        _save_state_json(slug, state)
        _git_commit(f"auto: weight {kg} kg {slug} {today}")
        target = _load_profile(slug).get("race_weight_kg")
        if target:
            diff = round(kg - float(target), 1)
            return f"Logged {kg} kg. {diff:+.1f} kg to race-day target ({target:g} kg)."
        return f"Logged {kg} kg."

    m = _HEAT_RE.match(text.strip())
    if m and slug:
        mins = int(float(m.group(1)))
        heat_log = _athlete_dir(slug) / "heat-log.json"
        try:
            entries = json.loads(heat_log.read_text()) if heat_log.exists() else []
        except Exception:
            entries = []
        entries.append({"date": today, "duration_min": mins, "temp_c": 40, "hr_peak": None, "notes": ""})
        heat_log.parent.mkdir(parents=True, exist_ok=True)
        heat_log.write_text(json.dumps(entries, indent=2))
        state = _load_state_json(slug)
        state.setdefault("heat", {})
        state["heat"]["sessions_cumulative"] = state["heat"].get("sessions_cumulative", 0) + 1
        state["heat"]["last_session_date"] = today
        state["last_updated"] = today
        _save_state_json(slug, state)
        _git_commit(f"auto: heat session {mins}min {slug} {today}")
        reply = f"Logged heat session {mins} min."
        # 14-day dose vs the athlete's protocol floor (entries without a dose
        # field count 1.0 — same convention as lib/heat.py).
        if _heat_lib is not None:
            cutoff = (date.today() - timedelta(days=14)).isoformat()
            dose14 = sum(float(e.get("dose", 1.0) or 1.0)
                         for e in entries if str(e.get("date", "")) >= cutoff)
            hs = _heat_lib.state(slug, _load_profile(slug))
            if hs["active"]:
                floor = (_heat_lib.PROTOCOL_DOSE_14D if hs["in_protocol_window"]
                         else _heat_lib.MAINTENANCE_DOSE_14D)
                status = ("on target" if dose14 >= floor
                          else f"{floor - dose14:g} more needed in the window")
                reply += f" 14-day dose {dose14:g}/{floor:g} — {status}."
            else:
                reply += f" 14-day dose {dose14:g}."
        return reply

    m = _PERIOD_RE.match(txt)
    if m and _menstrual is not None and slug:
        when = (m.group(1) or "today").lower()
        if when == "today":
            d = date.today()
        elif when == "yesterday":
            d = date.today() - timedelta(days=1)
        else:
            try:
                d = date.fromisoformat(when)
            except ValueError:
                return ("Couldn't read that date — try `period started`, "
                        "`period started yesterday`, or `period 2026-06-10`.")
        mc = _menstrual.log_period_start(slug, d)
        _git_commit(f"auto: cycle anchor update {slug} {today}")
        cycle_len = int(mc.get("cycle_length_days", 28))
        day_now = (date.today() - d).days + 1
        nxt = (d + timedelta(days=cycle_len)).strftime("%-d %b")
        tail = (" Phases are factored into your plan and daily sessions."
                if _menstrual.enabled(slug) else "")
        return (f"Logged period start {d.strftime('%-d %b')} — cycle day {day_now} today "
                f"({_menstrual.phase_from_day(day_now, cycle_len)}). "
                f"Next expected ~{nxt}.{tail}")

    m = _CYCLE_DAY_RE.match(txt)
    if m and _menstrual is not None and slug:
        n = int(m.group(1))
        if not 1 <= n <= 35:
            return "Cycle day must be between 1 and 35."
        mc = _menstrual.set_cycle_day(slug, n)
        _git_commit(f"auto: cycle anchor update {slug} {today}")
        phase = _menstrual.phase_from_day(n, int(mc.get("cycle_length_days", 28)))
        return f"Set cycle day {n} ({phase}). Say `period started` on day 1 to keep this accurate."

    if _REPLAN_RE.match(text.strip()):
        return "__REPLAN__"

    if _PLAN_RE.match(text.strip()):
        return "__GENERATE_PLAN__"

    m = _FTP_RE.match(text.strip())
    if m:
        return f"__FTP_RETEST__:{m.group(1)}"

    m = _CSS_RE.match(text.strip())
    if m:
        return f"__CSS__:{m.group(1)}"

    m = _LTHR_RE.match(text.strip())
    if m:
        return f"__LTHR__:{m.group(1)}"

    if _WEEKLY_SUMMARY_RE.match(text.strip()):
        return "__WEEKLY_SUMMARY__"

    if _STRENGTH_RE.match(text.strip()):
        return _strength_session(slug)

    if _FEEDBACK_LOG_RE.match(txt):
        if slug:
            _fm = re.match(r'^([\w-]+)\s*:[ \t]*(.*)', txt, re.I | re.DOTALL)
            entry_type = _fm.group(1).lower() if _fm else "note"
            content    = _fm.group(2).strip()  if _fm else txt
            flog = _athlete_dir(slug) / "feedback-log.json"
            try:
                flog_entries = json.loads(flog.read_text()) if flog.exists() else []
            except Exception:
                flog_entries = []
            flog_entries.append({
                "date": today,
                "type": entry_type,
                "message": content,
                "status": "open",
                "resolution_commit": None,
            })
            flog.parent.mkdir(parents=True, exist_ok=True)
            flog.write_text(json.dumps(flog_entries, indent=2))
            _git_commit(f"auto: feedback-log {entry_type} {slug} {today}")
        return "Logged."

    return None


def _update_ftp(slug: str, new_ftp: int) -> str:
    """Update FTP across the athlete's local files and current-state.json. Returns reply string."""
    today = date.today().isoformat()
    updated = []
    errors = []

    # system_prompt.txt — "FTP 316 W" style (skip silently if the athlete has none)
    sp_path = _athlete_dir(slug) / "system_prompt.txt"
    try:
        if sp_path.exists():
            sp = sp_path.read_text()
            sp_new = re.sub(r'FTP \d+ W', f'FTP {new_ftp} W', sp)
            if sp_new != sp:
                sp_path.write_text(sp_new)
                updated.append("system_prompt.txt")
    except Exception as e:
        errors.append(f"system_prompt: {e}")

    # reference/rules.md — "Bike FTP: 316 W" style (skip silently if absent)
    rules_path = _athlete_dir(slug) / "reference/rules.md"
    try:
        if rules_path.exists():
            rules = rules_path.read_text()
            rules_new = re.sub(r'Bike FTP: \d+ W', f'Bike FTP: {new_ftp} W', rules)
            if rules_new != rules:
                rules_path.write_text(rules_new)
                updated.append("rules.md")
    except Exception as e:
        errors.append(f"rules.md: {e}")

    # profile.json — ftp_watts is what the planner reads
    try:
        prof_path = _athlete_dir(slug) / "profile.json"
        if prof_path.exists():
            prof = json.loads(prof_path.read_text())
            if prof.get("ftp_watts") != new_ftp:
                prof["ftp_watts"] = new_ftp
                prof_path.write_text(json.dumps(prof, indent=2, ensure_ascii=False))
                updated.append("profile.json")
    except Exception as e:
        errors.append(f"profile.json: {e}")

    # current-state.json
    try:
        state = _load_state_json(slug)
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
        _save_state_json(slug, state)
        updated.append("current-state.json")
    except Exception as e:
        errors.append(f"current-state.json: {e}")

    _git_commit(f"ftp: updated to {new_ftp} W {slug} {today}")

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


def _mark_test_completed(slug: str, test_type: str):
    """Mark the earliest uncompleted test of the given type as done in test-schedule.json."""
    test_f = BASE.parent / "athletes" / slug / "test-schedule.json"
    if not test_f.exists():
        return
    try:
        tests = json.loads(test_f.read_text())
        for t in tests:
            if t.get("type") == test_type and not t.get("completed"):
                t["completed"] = True
                t["completed_date"] = date.today().isoformat()
                break
        test_f.write_text(json.dumps(tests, indent=2))
    except Exception as e:
        log(f"_mark_test_completed error: {e}")


def _update_css(slug: str, css_str: str) -> str:
    """Update swim CSS in profile.json, mark test completed, regenerate race plan."""
    today = date.today().isoformat()
    profile_path = BASE.parent / "athletes" / slug / "profile.json"
    try:
        profile = json.loads(profile_path.read_text())
        prev = profile.get("swim_css_per_100m")
        profile["swim_css_per_100m"] = css_str.strip()
        profile_path.write_text(json.dumps(profile, indent=2))
    except Exception as e:
        return f"Failed to update CSS: {e}"

    # Keep the stated CSS in the coaching files in sync (like _update_ftp does for FTP).
    # Swim pace zones in those files are RELATIVE to CSS, so updating this one anchor
    # value keeps every derived pace correct. Replace the old value only on CSS lines.
    if prev and prev != css_str.strip():
        adir = BASE.parent / "athletes" / slug
        for fp in (adir / "system_prompt.txt", adir / "reference" / "rules.md"):
            try:
                if not fp.exists():
                    continue
                lines = fp.read_text().splitlines(keepends=True)
                out, changed = [], False
                for ln in lines:
                    if "css" in ln.lower() and prev in ln:
                        new_ln = ln.replace(prev, css_str.strip())
                        changed = changed or (new_ln != ln)
                        ln = new_ln
                    out.append(ln)
                if changed:
                    fp.write_text("".join(out))
            except Exception:
                pass

    _mark_test_completed(slug, "css")

    try:
        subprocess.run(
            ["python3", str(BASE.parent / "scripts/generate-race-plan.py"), "--athlete", slug],
            capture_output=True, text=True, cwd=str(PROJECT_DIR), timeout=60,
        )
    except Exception:
        pass

    _git_commit(f"css: {slug} updated to {css_str} {today}")
    prev_str = f" (was {prev}/100m)" if prev else ""
    return (
        f"CSS updated to *{css_str}/100m*{prev_str}. "
        f"Race plan swim targets recalculated."
    )


def _update_lthr(slug: str, lthr_bpm: int) -> str:
    """Update LTHR in profile.json, mark test completed, regenerate race plan."""
    today = date.today().isoformat()
    profile_path = BASE.parent / "athletes" / slug / "profile.json"
    try:
        profile = json.loads(profile_path.read_text())
        prev = profile.get("lthr")
        profile["lthr"] = lthr_bpm
        profile_path.write_text(json.dumps(profile, indent=2))
    except Exception as e:
        return f"Failed to update LTHR: {e}"

    _mark_test_completed(slug, "lthr")

    try:
        subprocess.run(
            ["python3", str(BASE.parent / "scripts/generate-race-plan.py"), "--athlete", slug],
            capture_output=True, text=True, cwd=str(PROJECT_DIR), timeout=60,
        )
    except Exception:
        pass

    _git_commit(f"lthr: {slug} updated to {lthr_bpm} bpm {today}")
    prev_str = f" (was {prev} bpm)" if prev else ""
    return (
        f"LTHR updated to *{lthr_bpm} bpm*{prev_str}. "
        f"HR bands in race plan updated."
    )


#  Bug-fixer Stage 2 — review approval (✅Yes / ❌No / ✏️Edit on drafted fixes)
BUG_REVIEWS_FILE  = BASE.parent / ".bug-reviews.json"      # written by scripts/bug-fixer.py --fix
_BUGFIXER         = BASE.parent / "scripts/bug-fixer.py"
_PENDING_BUG_EDIT = {}                                     # chat_id -> review id awaiting a revision msg
_BOT_PATH_PREFIXES = ("ClaudeCoach/telegram/", "ClaudeCoach/lib/")  # changes here need a bot restart


def _bug_reviews():
    try:
        return json.loads(BUG_REVIEWS_FILE.read_text())
    except Exception:
        return {}


def _bug_reviews_save(r):
    BUG_REVIEWS_FILE.write_text(json.dumps(r, indent=2))


def _bug_mark_feedback(slug, entries, status, commit_hash=None):
    f = BASE.parent / f"athletes/{slug}/feedback-log.json"
    try:
        d = json.loads(f.read_text())
        for i in entries:
            if isinstance(i, int) and 0 <= i < len(d):
                entry = d[i]
                for old in ("resolution", "resolved", "fix"):
                    entry.pop(old, None)
                entry["status"] = status
                entry["resolution_commit"] = commit_hash
        f.write_text(json.dumps(d, indent=2))
    except Exception as e:
        log(f"bug-fixer mark feedback failed: {e}")


def _handle_bugfix(token, chat_id, data, athletes, config):
    """Yes = merge branch->main (+push, +restart if bot files changed, +mark resolved);
    No = discard branch + dismiss; Edit = capture the next message as a revision.
    Only merges a branch recorded 'awaiting' in .bug-reviews.json — never an arbitrary ref."""
    if not data.startswith("bf:"):
        return False
    parts = data.split(":", 2)
    if len(parts) != 3:
        return True
    _, action, rid = parts
    reviews = _bug_reviews()
    rv = reviews.get(rid)
    if not rv or rv.get("status") != "awaiting":
        send(token, chat_id, "That fix is no longer pending.")
        return True
    if chat_id not in athletes:
        return True
    proj   = config.get("project_dir", str(BASE.parent.parent))
    branch = rv["branch"]
    slug   = athletes[chat_id]["slug"]
    title  = rv.get("title", "")

    def _g(args):
        return subprocess.run(["git", "-C", proj] + args, capture_output=True, text=True)

    if action == "no":
        _g(["branch", "-D", branch])
        rv["status"] = "dismissed"; _bug_reviews_save(reviews)
        _bug_mark_feedback(slug, rv.get("entries", []), "dismissed")
        send(token, chat_id, f"❌ Dismissed: {title}")
        return True

    if action == "edit":
        _PENDING_BUG_EDIT[chat_id] = rid
        send(token, chat_id, f"✏️ Send your change for *{title}* as your next message and I'll revise it.")
        return True

    if action == "yes":
        # Rule-pile consolidations (prune / merge / add_rule) live outside git -
        # the bug-fixer rewrites the gitignored persistent-rules.md directly (with
        # a backup + stale-SHA guard), so they are applied via --apply-prune, not a
        # git merge. Code fixes fall through to the merge path below unchanged.
        if rv.get("kind") == "prune":
            pslug = rv.get("slug", slug)
            r = subprocess.run(
                ["python3", str(_BUGFIXER), "--apply-prune", rid],
                cwd=config.get("project_dir"), capture_output=True, text=True)
            note = (((r.stderr or "") + (r.stdout or "")).strip().splitlines() or [""])[-1]
            # apply_prune flips the review status to "applied" only on success; every
            # failure path (stale SHA, rules file missing, already applied, no such
            # proposal) leaves the status untouched and explains itself on stderr, so
            # re-read the review to tell a real apply from a graceful no-op.
            if _bug_reviews().get(rid, {}).get("status") == "applied":
                # The live rules changed; drop the athlete's cached chat session so
                # the consolidated rules take effect on their next message.
                engine._clear_session(str(athlete_files(pslug)["system_prompt"]))
                send(token, chat_id, f"✅ Rules consolidated: {title}\n_{rv.get('stat', '')}_")
            else:
                send(token, chat_id,
                     f"⚠️ Couldn't apply *{title}*: {note or 'no change made'}")
            return True
        if _g(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() != "main":
            _g(["checkout", "main"])
        m = _g(["merge", "--no-ff", "-m", f"bugfix {rid}: {title}", branch])
        if m.returncode != 0:
            _g(["merge", "--abort"])
            send(token, chat_id, f"⚠️ Couldn't merge *{title}* cleanly (conflict). Branch kept: `{branch}` — needs a manual look.")
            return True
        _g(["push", "origin", "main"])
        commit_hash = _g(["rev-parse", "--short", "HEAD"]).stdout.strip()
        rv["status"] = "merged"; _bug_reviews_save(reviews)
        _bug_mark_feedback(slug, rv.get("entries", []), "resolved", commit_hash=commit_hash)
        _g(["branch", "-D", branch])
        needs_restart = any(str(p).startswith(_BOT_PATH_PREFIXES) for p in rv.get("files", []))
        send(token, chat_id, f"✅ Merged & deployed: {title}"
             + ("\n_Restarting the bot to load it…_" if needs_restart else ""))
        if needs_restart:
            subprocess.Popen(["systemctl", "restart", "claudecoach-bot.service"], start_new_session=True)
        return True
    return True


def _handle_quick_log(token, chat_id, data, message_id, athletes):
    """Handle quick-log callback from post-session inline keyboard. Returns True if handled."""
    parts = data.split(":", 3)
    if len(parts) != 4 or parts[0] not in ("r", "p", "c"):
        return False

    field_code, activity_id, slug, value_str = parts

    athlete = athletes.get(chat_id)
    if not athlete or athlete["slug"] != slug:
        return False

    field_map = {"r": "rpe", "p": "injury_pain_during", "c": "nutrition_g_carb"}
    field = field_map[field_code]

    try:
        value = int(value_str)
    except ValueError:
        return False

    session_log_f = BASE.parent / "athletes" / slug / "session-log.json"
    if not session_log_f.exists():
        return False

    try:
        entries = json.loads(session_log_f.read_text())
    except Exception:
        return False

    updated = False
    needs_carb_followup = False
    entry_sport = ""
    entry_duration = 0

    for e in entries:
        if str(e.get("activity_id", "")) != activity_id:
            continue
        e[field] = value
        if field == "rpe":
            e["stub"] = False
            entry_sport = e.get("sport", "")
            entry_duration = e.get("duration_min", 0) or 0
            if entry_sport == "Ride" and entry_duration >= 90 and e.get("nutrition_g_carb") is None:
                needs_carb_followup = True
        updated = True
        break

    if not updated:
        return False

    try:
        session_log_f.write_text(json.dumps(entries, indent=2))
        _invalidate_prefetch(slug)  # session-log feeds prefetch context
    except Exception:
        return False

    label_map = {"r": f"RPE {value}", "p": f"Pain {value}/10", "c": f"{value}g/hr carbs"}
    conf = f"✓ {label_map[field_code]} logged"
    # Keep drill buttons visible after logging — replace RPE/pain/carb rows but preserve drill row
    drill_kb = {"inline_keyboard": [[
        {"text": "📊 Intervals", "callback_data": f"drill:intervals:{activity_id}:{slug}"},
        {"text": "🍌 Nutrition",  "callback_data": f"drill:nutrition:{activity_id}:{slug}"},
        {"text": "💓 HR",         "callback_data": f"drill:hr:{activity_id}:{slug}"},
        {"text": "↔️ Compare",    "callback_data": f"drill:compare:{activity_id}:{slug}"},
    ]]}
    if message_id:
        tg_post(token, "editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": conf,
            "reply_markup": drill_kb,
        })
    else:
        send(token, chat_id, conf)

    if needs_carb_followup:
        carb_kb = {"inline_keyboard": [[
            {"text": f"{g}g/hr", "callback_data": f"c:{activity_id}:{slug}:{g}"}
            for g in (40, 50, 60, 70, 80, 90)
        ]]}
        send(token, chat_id, "Carbs per hour?", reply_markup=carb_kb)

    return True


def _handle_drill(token, chat_id, data, message_id, athletes, config):
    """Handle drill-down analysis buttons (Intervals/Nutrition/HR/Compare). Returns True if handled."""
    if not data.startswith("drill:"):
        return False
    parts = data.split(":", 3)
    if len(parts) != 4:
        return False
    _, drill_type, activity_id, slug = parts

    athlete = athletes.get(chat_id)
    if not athlete or athlete["slug"] != slug:
        return False

    drill_prompts = {
        "intervals": (
            f"Analyse the interval structure of activity {activity_id}. "
            f"Fetch extended metrics and show each interval: power or pace, vs FTP/threshold, "
            f"whether the session was executed as prescribed. Keep it to 5 lines max."
        ),
        "nutrition": (
            f"Review the nutrition for activity {activity_id}. "
            f"Find the session-log entry and compute g/hr consumed. "
            f"Compare to my target and my 4-ride recent average. "
            f"Tell me if I'm on track. Keep it to 4 lines max."
        ),
        "hr": (
            f"Analyse the heart rate data for activity {activity_id}. "
            f"Use extended metrics: avg HR, max HR, time in zones, cardiac decoupling vs power or pace. "
            f"Flag anything worth noting. Keep it to 5 lines max."
        ),
        "compare": (
            f"Find the 3 most similar past sessions to activity {activity_id} in session-log.json "
            f"— same sport, closest duration and TSS. Compare key metrics (power/pace, HR, decoupling, nutrition). "
            f"Am I improving? Keep it to 6 lines max."
        ),
    }

    question = drill_prompts.get(drill_type)
    if not question:
        return False

    typing(token, chat_id)
    files = athlete_files(slug)
    history = load_history(files["history"])
    context = prefetch_context(slug)
    athlete_name = athlete.get("name", slug).split()[0]
    response = call_claude(question, config, history, model=MODEL_OPUS,
                           system_prompt_file=files["system_prompt"],
                           athlete_name=athlete_name, context=context)
    clean = process_charts(token, chat_id, response, slug=slug)
    if clean:
        send(token, chat_id, clean + response_footer(MODEL_OPUS, slug=slug, athlete_cfg=athlete))
    history.append(_hist_entry(question, clean))
    save_history(history, files["history"])
    return True


def _handle_test_confirm(token, chat_id, data, message_id, athletes):
    """Handle test confirmation/dismiss callbacks from zone-spotting. Returns True if handled."""
    if not data.startswith("test:"):
        return False
    parts = data.split(":", 3)
    if len(parts) != 4:
        return False
    _, t_type, slug, value = parts

    athlete = athletes.get(chat_id)
    if not athlete or athlete["slug"] != slug:
        return False

    if t_type == "dismiss":
        if message_id:
            edit_keyboard_confirm(token, chat_id, message_id, "❌ Dismissed")
        return True

    if t_type == "ftp":
        try:
            new_ftp = int(float(value))
        except ValueError:
            return False
        reply = _update_ftp(slug, new_ftp)
        _mark_test_completed(slug, "ftp")
    elif t_type == "css":
        reply = _update_css(slug, value)
    elif t_type == "lthr":
        try:
            new_lthr = int(value)
        except ValueError:
            return False
        reply = _update_lthr(slug, new_lthr)
    else:
        return False

    if message_id:
        edit_keyboard_confirm(token, chat_id, message_id, f"✅ Confirmed")
    send(token, chat_id, reply)
    return True


def _handle_replan_confirm(token, chat_id, data, message_id, athletes):
    """Handle replan confirmation/cancel callbacks. Returns True if handled."""
    if data not in ("__REPLAN_CONFIRM__", "__REPLAN_CANCEL__"):
        return False
    athlete = athletes.get(chat_id)
    if not athlete:
        return False
    slug = athlete["slug"]
    expiry = _PENDING_REPLAN.pop(chat_id, None)
    if data == "__REPLAN_CANCEL__" or expiry is None or time.time() > expiry:
        if message_id:
            edit_keyboard_confirm(token, chat_id, message_id, "❌ Replan cancelled")
        return True
    if message_id:
        edit_keyboard_confirm(token, chat_id, message_id, "✅ Replan confirmed — rebuilding week…")
    try:
        cmd = ["timeout", "2700", "python3", str(STAGE1_PLAN_SCRIPT),
               "--athlete", slug, "--push", "--notify"]
        override = _extract_plan_override(slug)
        if override:
            override_path = _write_plan_override(slug, override)
            cmd += ["--override-json", override_path]
            log(f"[{slug}] replan using conversation-agreed plan override")
        subprocess.Popen(cmd, cwd=str(PROJECT_DIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"[{slug}] replan launched in background (confirmed)")
    except Exception as e:
        send(token, chat_id, f"Couldn't start replan: {e}", reply_markup=build_keyboard(slug))
    return True


def prefetch_context(slug: str) -> str:
    """Fetch standard training context in parallel and return as a formatted block.
    Falls back silently to empty string on any error so the bot keeps working.
    Cached per athlete for _PREFETCH_TTL seconds (invalidated on any log write)."""
    now_epoch = time.time()
    with _PREFETCH_GUARD:
        hit = _PREFETCH_CACHE.get(slug)
    if hit and now_epoch - hit[0] < _PREFETCH_TTL:
        return hit[1]
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

        lines = [f"=== LIVE TRAINING DATA ({today.strftime('%A')} {today.isoformat()}) ==="]

        prof = _load_profile(slug)
        bike = prof.get("bike_model")
        lines.append(f"Bike: {bike if bike else 'bike model not recorded'}")

        # Fitness snapshot
        if wellness:
            w = wellness[-1]
            ctl = round(w.get("ctl") or 0, 1)
            atl = round(w.get("atl") or 0, 1)
            tsb = round((w.get("ctl") or 0) - (w.get("atl") or 0), 1)
            ftp = sport.get("ftp") if isinstance(sport, dict) else None
            sport_info = w.get("sportInfo") or []
            eftp = next((si.get("eftp") for si in sport_info if si.get("type") in ("Ride", "VirtualRide", "Cycling")), None)
            if eftp is None and sport_info:
                eftp = sport_info[0].get("eftp")
            # Intervals.icu keeps recomputing the latest day's CTL/ATL/Form until the
            # row settles (its `updated` date moves past the row's own date). Live
            # pulls quoted as final have been off ~10 points — mark it provisional.
            _prov = not w.get("wellness_finalized", False)
            lines.append(
                f"Fitness {ctl}  Fatigue {atl}  Form {tsb}"
                + (f"  FTP {ftp}W" if ftp else "")
                + (f"  eFTP {round(eftp)}W" if eftp else "")
                + ("  [PROVISIONAL: this is the latest day's CTL/ATL/Form, still settling on "
                   "Intervals.icu — it can shift several points as activities finish syncing. "
                   "Call it provisional, don't state it as final.]" if _prov else "")
            )
            # Authoritative live thresholds (eFTP-first, m/s-correct) — the model must
            # use THESE, never a hardcoded number from the system prompt.
            try:
                import thresholds as _th
                _t = _th.get_thresholds(slug, athletes[slug], client)
                _bits = [f"FTP {_t['ftp_watts']}W ({_t['ftp_source']})"]
                if _t["run_threshold_per_km"]: _bits.append(f"run threshold {_t['run_threshold_per_km']}")
                if _t["swim_css_per_100m"]:    _bits.append(f"swim CSS {_t['swim_css_per_100m']}")
                lines.append("CURRENT THRESHOLDS (live, AUTHORITATIVE — use these, not any number "
                             "in the prompt): " + " · ".join(_bits)
                             + ("  [" + "; ".join(_t["notes"]) + "]" if _t["notes"] else ""))
            except Exception as _e:
                log(f"prefetch thresholds (non-fatal): {_e}")
            fields = []
            if w.get("weight"):    fields.append(f"Weight {w['weight']:.1f}kg")
            if w.get("hrv"):       fields.append(f"HRV {w['hrv']}")
            if w.get("sleepScore"):fields.append(f"Sleep score {w['sleepScore']:.0f}")
            if w.get("restingHR"): fields.append(f"RHR {w['restingHR']}")
            if w.get("steps"):     fields.append(f"Steps {int(w['steps']):,}")
            if fields:
                lines.append("Wellness: " + "  ".join(fields))

        # Wellness trend (last 7 days, compact)
        if len(wellness) > 1:
            lines.append("Recent Fitness/Form: " + "  ".join(
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
                lines.append(f"  {date_str}  {sport_type:<12} {dur}min{dist_str}  Load={tss}")

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

        # Same "already answered" treatment for today's RPE/nutrition (from
        # session-log.json), and for whether the most recent activity has already
        # been debriefed (from last_activity_state.json) — without these, a
        # follow-up message that just adds a data point (e.g. "carbs were 60g/hr")
        # gets treated as new information to build a full debrief around, instead
        # of being appended to the debrief that already went out.
        try:
            sl_path = BASE.parent / "athletes" / slug / "session-log.json"
            if sl_path.exists():
                sl = json.loads(sl_path.read_text())
                today_str = today.isoformat()
                today_answered = []
                for s in sl:
                    if s.get("date") != today_str:
                        continue
                    sport = s.get("sport", "session")
                    if s.get("rpe") is not None:
                        today_answered.append(f"RPE for today's {sport} logged: {s['rpe']} — do not ask again")
                    if s.get("nutrition_g_carb") is not None:
                        today_answered.append(
                            f"Nutrition for today's {sport} logged: {s['nutrition_g_carb']}g/hr carbs — do not ask again")
                if today_answered:
                    lines.append("Already answered today: " + "  |  ".join(today_answered))
        except Exception:
            pass

        try:
            last_act_state_path = BASE.parent / "athletes" / slug / "last_activity_state.json"
            if last_act_state_path.exists():
                last_state = json.loads(last_act_state_path.read_text())
                notified_at = last_state.get("notified_at")
                last_id = last_state.get("last_id")
                if notified_at and last_id:
                    age_s = (datetime.now() - datetime.fromisoformat(notified_at)).total_seconds()
                    if age_s < 10800:  # 3h — matches build_keyboard's post-session window
                        lines.append(
                            f"Activity {last_id} was already debriefed {int(age_s // 60)}min ago. If the athlete's "
                            "next message just adds a data point for it (RPE, nutrition, notes), acknowledge that "
                            "point only — do NOT regenerate the full debrief.")
        except Exception:
            pass

        # Deterministic planning numbers — computed here so the model never does
        # TSS/CTL arithmetic by hand (the 14 Jun planning failure). Reuses data
        # already fetched above; no extra network. See lib/plan_tools.py.
        try:
            import plan_tools as _pt
            from datetime import timedelta as _td
            wk_start = today - _td(days=today.weekday())
            roll = _pt.week_rollup_summary(history_acts, events, wk_start, today)
            lines.append(
                f"\n=== DETERMINISTIC PLANNING NUMBERS (use these verbatim; do NOT recompute) ===")
            lines.append(
                f"This week ({roll['week_start']}): completed-to-date {roll['completed_to_date_tss']} Load "
                f"+ planned-remaining {roll['planned_remaining_tss']} Load "
                f"= projected {roll['projected_week_tss']} Load")
            ctl_now = round(float((wellness[-1].get("ctl") or 0)), 1) if wellness else None
            if ctl_now:
                req = _pt.required_tss(athletes[slug], ctl_now)
                if "error" not in req and req.get("recommended_weekly_tss"):
                    lines.append(
                        f"Phase: {req['phase']} (training week {req.get('training_week')}). "
                        f"Target weekly Load to stay on the Fitness plan: "
                        f"~{req['recommended_weekly_tss']} (needs {req.get('required_weekly_tss')}, "
                        f"ramp-cap {req.get('ramp_capped_weekly_tss','n/a')}). {req.get('note','')}")
                elif "error" in req:
                    lines.append(f"Weekly Load target: {req['error']}")
            # Sport-balance guardrails — so the model plans across sports to the
            # athlete's rules, not "all running". day_rules are HARD (a sport on a
            # forbidden day is a validation breach); distribution is the phase's
            # easy-vs-quality mix.
            dr = athletes[slug].get("day_rules")
            if dr:
                lines.append(f"Day rules (HARD — which sports go on which days): {json.dumps(dr)}")
            try:
                bp = _pt._load_blueprint(slug)
                ph = _pt.current_phase(bp, wk_start) or {}
                if ph.get("distribution"):
                    lines.append(f"Phase intensity distribution (easy vs quality per sport): {json.dumps(ph['distribution'])}")
                # ── FORWARD WEEK ──────────────────────────────────────────────
                # Forward-planning questions ("what does next week look like",
                # "how do we hit X next week") must be answered from the
                # deterministic engine's week, NEVER improvised from prose (the
                # 22 Jul failure). Phase + distribution here are read from config
                # for NEXT week specifically — do not narrate the phase from
                # memory, and do not carry THIS week's phase across a boundary.
                # If next week is on the calendar, answer from those sessions; if
                # not, it has not been generated yet — give the blueprint target,
                # do not invent sessions.
                next_wk = wk_start + _td(days=7)
                nph = _pt.current_phase(bp, next_wk) or {}
                nx = [e for e in events
                      if (e.get("category") or "WORKOUT") == "WORKOUT"
                      and next_wk.isoformat() <= (e.get("start_date_local") or "")[:10]
                      <= (next_wk + _td(days=6)).isoformat()]
                fwd = [f"\n=== FORWARD WEEK (Mon {next_wk.isoformat()}) — answer forward-planning "
                       "questions from THIS, never improvise from prose ==="]
                if nph.get("name"):
                    fwd.append(f"Phase (from config): {nph['name']}")
                if nph.get("distribution"):
                    fwd.append(f"Blueprint distribution = THE SPEC (per sport): {json.dumps(nph['distribution'])}")
                if nx:
                    fwd.append("Engine-built sessions already on the calendar (answer from these):")
                    from datetime import date as _date
                    for e in sorted(nx, key=lambda x: (x.get("start_date_local") or "")):
                        d = (e.get("start_date_local") or "")[:10]
                        wd = _date.fromisoformat(d).strftime("%a") if d else "?"
                        dur = round((e.get("moving_time") or 0) / 60)
                        fwd.append(f"  {wd} {d}  {e.get('type','?'):<10} {e.get('name','')}  {dur}min")
                else:
                    fwd.append("NOT GENERATED YET — next week has no sessions on the calendar. Do "
                               "NOT invent a session-by-session week. State the phase + blueprint "
                               "split above and the weekly-load target, and offer to build it "
                               "(the athlete can say 'generate plan'). The weekly engine builds it "
                               "Sunday; it can also be built on request.")
                fwd.append("Before telling the athlete a stated week is 'on spec', check it BOTH "
                           "ways (enough Z4–5/Z3, not too much quality): express the week as zoned "
                           "segments and run  python3 ClaudeCoach/lib/plan_distribution.py "
                           f"--athlete {slug} --week-start {next_wk.isoformat()} --sessions '<json>'. "
                           "A non-zero exit / any OFF-SPEC finding means do NOT claim compliance — "
                           "correct the week or state the gap.")
                lines.append("\n".join(fwd))
            except Exception as _e:
                log(f"prefetch forward-week (non-fatal): {_e}")
            lines.append(
                "For per-session Load or a projection of a proposed week, call: "
                f"plan_tools.py tss --sport <s> --segments '<time-at-intensity json>'  |  "
                f"plan_tools.py project --athlete {slug} --daily '<json>'  |  "
                f"plan_tools.py required-tss --athlete {slug}  |  "
                f"plan_tools.py validate --athlete {slug} --week '<json>' (hard-check sport-day rules + ramp before proposing a week)")
            lines.append(
                "For race-day FUELLING (carbs, fluid, sodium, caffeine, gut/transporter limits) NEVER work out the "
                "numbers yourself — call the shared fuelling engine (same one behind the Diamond Peak web planner):  "
                f"plan_tools.py race-fuelling --athlete {slug} [--gut-trained] "
                "-> evidence-based bike/run carb g/hr, fluid ml/hr, sodium mg/hr and total caffeine from race duration, "
                "body weight and sweat data.  |  "
                f"plan_tools.py fuel-check --athlete {slug} --carb <g/hr> [--glucose <g/hr> --fructose <g/hr>] "
                "--fluid <ml/hr> --sodium <mg/hr> --caffeine <mg total> "
                "-> red-flags a plan the athlete describes (glucose over the ~60 g/hr SGLT1 cap, wrong glucose:fructose "
                "ratio, dehydration, low sodium, caffeine outside 3-6 mg/kg). For the interactive per-leg schedule with "
                "the gut-backlog graph, point them to https://diamondpeak.uk/cycling/fuelling-calculator.html")
            lines.append(
                "For WETSUIT / water-temperature questions about the Cervia race, NEVER guess — call the shared "
                "prediction engine (same one behind the Diamond Peak web predictor):  "
                "plan_tools.py wetsuit  "
                "-> predicted official race-morning water temp, wetsuit-legal probability (AG limit 24.5C, pro 21.9C), "
                "live Adriatic SST from Open-Meteo, and a live-anomaly method that activates inside 30 days of the race. "
                "Web version: https://diamondpeak.uk/cycling/cervia-wetsuit.html")
            lines.append(
                "WEEK-EDIT FLOOR RULE (HARD — Jamie, 6 Jul 2026): if you create, edit, move or delete "
                "planned events (icu_fetch push_workout/edit_workout/delete_workout or plan_tools "
                "render-workout pushes) for the current or next week, you MUST then run  "
                f"plan_tools.py week-tss --athlete {slug} --week-start <monday>  and  "
                f"plan_tools.py required-tss --athlete {slug} --date <monday>  and compare total_tss "
                "against weekly_tss_floor. If the week lands below the floor and week_type is not "
                "deload/taper, you MUST say so in the SAME reply with a 🔥 UNDER-TRAINING line stating "
                "the shortfall and CTL cost, and propose exactly where to add the volume (bike first). "
                "NEVER present an under-floor week as acceptable, NEVER defend a fitness drop in a "
                "build/specific week as fine — the athlete's structural constraints (travel, no bricks, "
                "session placement) are to be honoured by ADDING duration to allowed sessions, not by "
                "shrinking the week.")
            lines.append(
                "For RACE TIME / split predictions, NEVER invent numbers — call:  "
                f"plan_tools.py race-predict --athlete {slug}  "
                "-> Now / Race-day / Target scenarios from the shared IF ∝ √CTL model (same as /race and the website). "
                "If the athlete reports a PRE/POST-SESSION WEIGH-IN (sweat test), call:  "
                f"plan_tools.py sweat-rate --athlete {slug} --pre <kg> --post <kg> --fluid <ml> --minutes <n> --save  "
                "-> computes sweat rate and (with --save) updates sweat_ml_hr so race-fuelling uses it automatically. "
                "Confirm the numbers with the athlete before saving.")
            lines.append(
                "NEVER restart the bot, run systemctl/service, reboot or kill the process while replying — it drops the "
                "reply mid-send (this caused repeated ~25-min silences). Committed code changes apply on the next "
                "natural restart; say 'live on next restart' and move on. To show a chart, emit a <<<CHART:TYPE:JSON>>> "
                "marker (types include heat) — never write code to build a chart.")
        except Exception as _e:
            log(f"prefetch planning numbers (non-fatal): {_e}")

        # Recent session-log entries — so "log session" / "analyse" / "how did I
        # do" don't burn a tool round-trip reading session-log.json to find the
        # stub. Stubs (stub=true) still need RPE/notes; filled ones are context.
        try:
            sl_path = BASE.parent / "athletes" / slug / "session-log.json"
            if sl_path.exists():
                sl = json.loads(sl_path.read_text())
                recent_cut = (today - timedelta(days=4)).isoformat()
                recent = [s for s in sl if (s.get("date") or "") >= recent_cut]
                if recent:
                    lines.append("\nRecent session-log (last 4 days — fill stubs on 'log session', "
                                 "don't re-ask filled ones):")
                    for s in sorted(recent, key=lambda x: x.get("date", ""), reverse=True)[:8]:
                        flag = "STUB — needs RPE/notes" if s.get("stub") else f"RPE {s.get('rpe')}"
                        lines.append(
                            f"  {s.get('date')}  {s.get('sport','?'):<10} "
                            f"id={s.get('activity_id','?')}  [{flag}]")
        except Exception as _e:
            log(f"prefetch session-log (non-fatal): {_e}")

        # Subjective layer (top of current-state.md) — travel blocks, current
        # ankle/niggle status, open actions. The full file is large (~65KB of
        # historical logs); inject only the head and point the model at the rest.
        try:
            md_path = BASE.parent / "athletes" / slug / "current-state.md"
            if md_path.exists():
                head = md_path.read_text()[:2500].strip()
                if head:
                    lines.append(
                        f"\n=== SUBJECTIVE LAYER (current-state.md, top section — "
                        f"read the full file for older detail) ===\n{head}")
        except Exception as _e:
            log(f"prefetch current-state.md (non-fatal): {_e}")

        out = "\n".join(lines)
        with _PREFETCH_GUARD:
            _PREFETCH_CACHE[slug] = (now_epoch, out)
        return out

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

    # ICU threshold_pace is METRES/SECOND — convert to a displayable pace, never show raw.
    def _mps_pace(v, dist_m):
        if not v:
            return None
        s = dist_m / v
        return f"{int(s // 60)}:{s % 60:04.1f}"

    icu_data = {
        "ftp_watts":                 (ride or {}).get("ftp"),
        "indoor_ftp_watts":          (ride or {}).get("indoor_ftp"),
        "lthr":                      (ride or {}).get("lthr"),
        "run_threshold_pace_per_km": _mps_pace((run_s or {}).get("threshold_pace"), 1000),
        "swim_css_per_100m":         _mps_pace((swim or {}).get("threshold_pace"), 100),
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
    if icu_data["ctl"]:              metrics.append(f"Fitness {round(icu_data['ctl'])}")
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


def _lookup_race(race_name: str, race_date: str) -> dict:
    """Use Claude + web search to fetch race-specific details. Returns dict or {} on failure."""
    prompt = (
        f'Search the web for the race "{race_name}" on {race_date}. '
        f'Return ONLY a valid JSON object — no preamble, no explanation:\n'
        f'{{\n'
        f'  "race_type": "e.g. Ironman 140.6 / 70.3 Triathlon / Cycling Gran Fondo / Running Marathon",\n'
        f'  "swim_km": null,\n'
        f'  "bike_km": null,\n'
        f'  "run_km": null,\n'
        f'  "total_km": null,\n'
        f'  "elevation_m": null,\n'
        f'  "expected_hours_fast": null,\n'
        f'  "expected_hours_mid": null,\n'
        f'  "terrain": "flat / hilly / mountainous / alpine",\n'
        f'  "notes": "one sentence — key demands, conditions, or course character"\n'
        f'}}\n'
        f'Use null for any field you cannot find. Return ONLY the JSON.'
    )
    try:
        result = claude_call.run_claude(
            prompt, model=claude_call.SONNET, allowed_tools="WebSearch",
            cwd=str(PROJECT_DIR), timeout=90, label="race-prefill",
        )
        raw = (result.stdout or "").strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        log(f"Race lookup failed for '{race_name}': {e}")
    return {}


def _scaffold_athlete(chat_id, answers, icu_data, race_data=None):
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

    rd = race_data or {}
    race_type = rd.get("race_type") or "triathlon"

    dist_parts = []
    if rd.get("swim_km"):  dist_parts.append(f"{rd['swim_km']}km swim")
    if rd.get("bike_km"):  dist_parts.append(f"{rd['bike_km']}km bike")
    if rd.get("run_km"):   dist_parts.append(f"{rd['run_km']}km run")
    if not dist_parts and rd.get("total_km"): dist_parts.append(f"{rd['total_km']}km")
    race_distance_detail = " / ".join(dist_parts) if dist_parts else race_type

    exp_hrs = rd.get("expected_hours_mid") or rd.get("expected_hours_fast")
    expected_duration = f"~{exp_hrs}h" if exp_hrs else "TBD"

    profile = {
        "slug": slug, "name": name,
        "weight_kg": weight, "race_weight_kg": None,
        "race_name": race_name, "race_date": race_date,
        "race_distance": race_type,
        "race_info": rd,
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

    template_vars = dict(
        name=name, first_name=first_name, slug=slug,
        race_name=race_name, race_date=race_date or "TBD",
        race_distance=race_type,
        race_distance_detail=race_distance_detail,
        race_elevation_m=rd.get("elevation_m") or "unknown",
        race_terrain=rd.get("terrain") or "",
        race_notes=rd.get("notes") or "",
        expected_race_duration=expected_duration,
        a_goal=profile["a_goal"],
        b_goal=profile.get("b_goal", "Finish"),
        experience=answers.get("experience", ""),
        injuries=injuries_str,
        max_hours=max_hours or "?",
        ftp_watts=profile.get("ftp_watts", "TBD"),
        swim_css=profile.get("swim_css_per_100m", "TBD"),
        run_threshold=profile.get("run_threshold_pace_per_km", "TBD"),
        lthr=profile.get("lthr", "TBD"),
    )

    template_file = BASE.parent / "onboarding/templates/system_prompt.txt"
    if template_file.exists():
        sp = Template(template_file.read_text()).safe_substitute(**template_vars)
        (adir / "system_prompt.txt").write_text(sp)

    rules_template = BASE.parent / "onboarding/templates/rules.md"
    if rules_template.exists():
        rules = Template(rules_template.read_text()).safe_substitute(**template_vars)
        (adir / "reference" / "rules.md").write_text(rules)

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

        # Look up race details in the background while we send the ICU summary
        race_str = session["answers"].get("race", "")
        race_date_m = re.search(r'(\d{4}-\d{2}-\d{2})', race_str)
        race_date_str = race_date_m.group(1) if race_date_m else ""
        send(token, chat_id, summary)
        send(token, chat_id, "_Looking up your race..._")
        race_data = _lookup_race(race_str, race_date_str)
        session["race_data"] = race_data

        if race_data:
            parts = []
            if race_data.get("total_km") or race_data.get("bike_km"):
                dist = []
                if race_data.get("swim_km"):  dist.append(f"{race_data['swim_km']}km swim")
                if race_data.get("bike_km"):  dist.append(f"{race_data['bike_km']}km bike")
                if race_data.get("run_km"):   dist.append(f"{race_data['run_km']}km run")
                if not dist and race_data.get("total_km"): dist.append(f"{race_data['total_km']}km")
                if dist: parts.append(" / ".join(dist))
            if race_data.get("elevation_m"): parts.append(f"{race_data['elevation_m']}m elevation")
            mid = race_data.get("expected_hours_mid") or race_data.get("expected_hours_fast")
            if mid: parts.append(f"~{mid}h typical finish")
            if race_data.get("notes"): parts.append(race_data["notes"])
            race_summary = f"*{race_data.get('race_type', race_str)}*"
            if parts:
                race_summary += "\n" + " · ".join(parts)
            send(token, chat_id, race_summary)
        else:
            send(token, chat_id, "_Race details not found — I'll use what you've told me._")

        remaining = _build_remaining_queue(session["answers"], icu_data)
        session["queue"] = [[k, q] for k, q in remaining]
        next_key, next_q = session["queue"].pop(0)
        session["current_key"] = next_key
        save_onboarding_state(ob_state)
        send(token, chat_id, next_q)
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
        slug = _scaffold_athlete(chat_id, session["answers"], session["icu_data"], session.get("race_data"))
    except Exception as e:
        log(f"Onboarding scaffold failed for {chat_id}: {e}")
        send(token, chat_id, f"Something went wrong setting up your profile -- please contact your coach. (Error: {e})")
        return True

    log(f"Onboarding complete: {session['answers'].get('name', '?')} ({slug})")

    # Generate training blueprint and rules.md in background
    blueprint_script = BASE.parent / "scripts/generate-blueprint.py"
    if blueprint_script.exists():
        try:
            subprocess.Popen(
                ["python3", str(blueprint_script), "--athlete", slug],
                cwd=str(PROJECT_DIR),
            )
            log(f"[{slug}] generate-blueprint.py launched in background")
        except Exception as e:
            log(f"[{slug}] generate-blueprint.py launch failed: {e}")

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
        send(token, chat_id,
             f"Athlete `{slug_to_approve}` is now active.\n\n"
             f"_Strava write-back: run this on the VM to enable activity descriptions:_\n"
             f"`python3 ClaudeCoach/scripts/strava-auth.py --athlete {slug_to_approve}`")
        log(f"Admin approved athlete: {slug_to_approve}")
        return True

    return False


# --- END ONBOARDING ----------------------------------------------------------


def _telegram_visible(text):
    """During streaming, show ONLY what's inside <telegram> — suppress the model's
    pre-reply preamble / tool narration so internal thoughts never flash in the
    placeholder. Returns '' until the opening tag appears (the system prompt makes
    <telegram> the first output, so this is normally immediate). The final reply is
    already extracted by process_charts, so this only fixes the live stream."""
    i = text.find("<telegram>")
    if i == -1:
        return ""
    return text[i + len("<telegram>"):].split("</telegram>")[0]


# --- Phase 3: live two-message status UX -------------------------------------
# Deterministic map from a tool_use event (name + a short input hint from the
# engine) to a friendly present-tense status line (shown live in msg1) and a
# past-tense fragment (used to collapse msg1 to one line once the reply lands).
# Pure string matching on the tool metadata - no second model call.

def _classify_tool(name, hint):
    """Return (key, live, past) for a tool_use event. `key` dedupes the collapse
    summary; `live` is the present-tense line shown in msg1 while the tool runs;
    `past` is the past-tense fragment used to collapse msg1 to one line once the
    reply lands. Pure string matching on the tool name + a short input hint (a
    Bash command, a file path) - no second model call.

    The hint lets each line name WHAT is happening (which script/subcommand, which
    intervals.icu endpoint, which of the athlete's files) rather than just the
    tool category. Note the input hint carries the command / file path only - it
    does NOT carry the activity date or sport (those live in the response), so the
    data-pull lines name the endpoint, not "Sunday's ride". Lines stay short
    (<~45 chars), plain-English and athlete-facing - no paths, IDs or raw shell
    leak. An unmapped tool falls through to a safe generic line - never raw JSON,
    never a crash."""
    n = (name or "").lower()
    h = (hint or "").lower()
    blob = n + " " + h

    def has(*subs):
        return any(s in blob for s in subs)

    # --- Plan / session maths (plan_tools.py subcommands, run via Bash) ---------
    # Checked first: these commands don't carry "icu" in the hint, and they are
    # the load/plan calculations, not a raw data pull or a file read.
    if "plan_tools" in blob:
        if "session-for-load" in blob:
            return ("session", "Building the session to your Load target",
                    "built the session")
        if "session-load" in blob:
            return ("load", "Reading the session's Load from intervals.icu",
                    "read the session Load")
        if "week-tss" in blob or "plan_tools.py sum" in blob:
            return ("weekload", "Adding up your week's load",
                    "added up your week")
        if "required-tss" in blob:
            return ("target", "Working out your weekly Load target",
                    "worked out your Load target")
        if "project" in blob:
            return ("project", "Projecting your fitness & form",
                    "projected your fitness")
        if "validate" in blob:
            return ("validate", "Sense-checking the week against your rules",
                    "sense-checked the week")
        if "render-workout" in blob:
            return ("render", "Writing the workout to intervals.icu",
                    "wrote the workout")
        if "race-predict" in blob:
            return ("racepred", "Predicting your race day", "predicted your race")
        if has("race-fuelling", "fuel-target", "fuel-check"):
            return ("fuel", "Working out your fuelling", "worked out your fuelling")
        if "sweat-rate" in blob:
            return ("sweat", "Working out your sweat rate",
                    "worked out your sweat rate")
        if "wetsuit" in blob:
            return ("wetsuit", "Checking the water temp & wetsuit call",
                    "checked the wetsuit call")
        if "log-strength" in blob:
            return ("logstrength", "Logging your strength session",
                    "logged your strength work")
        if "tss" in blob:
            return ("tss", "Working out the session Load", "worked out the Load")
        return ("session", "Crunching your plan numbers", "crunched your plan")

    # --- Named scripts (run via Bash) ------------------------------------------
    if "heat_accl" in blob:
        return ("heat", "Checking today's heat dose", "checked your heat dose")
    if "generate-race-plan" in blob:
        return ("raceplan", "Building your race plan", "built your race plan")
    if has("stage1-plan", "generate-plan", "generate-blueprint"):
        return ("plan", "Rebuilding your plan", "rebuilt your plan")
    if "session-sync" in blob:
        return ("sync", "Syncing your training log", "synced your log")
    if has("strava-update", "strava_update"):
        return ("strava", "Updating the activity on Strava",
                "updated it on Strava")

    # --- Live data pulls from intervals.icu (icu_fetch.py --endpoint …, or the
    # icusync MCP tools / a workout push) ---------------------------------------
    if has("icu_fetch", "icusync", "intervals"):
        if has("wellness", "get_wellness"):
            return ("icu-wellness", "Checking your fitness & wellness",
                    "checked your wellness")
        if has("get_fitness", "endpoint fitness"):
            return ("icu-fitness", "Reading your fitness (CTL & form)",
                    "read your fitness")
        if has("activity_detail", "extended_metrics", "streams",
               "get_activity", "get_extended", "power_curve", "best_effort"):
            return ("icu-activity", "Reading the session in detail",
                    "read the session detail")
        if has("history", "events", "get_events", "training_history",
               "get_training"):
            return ("icu-history", "Looking up your recent activities",
                    "checked your activities")
        if has("push_workout", "edit_workout", "update_activity", "delete_workout"):
            return ("icu-write", "Updating your workout on intervals.icu",
                    "updated intervals.icu")
        return ("icu", "Checking intervals.icu", "checked intervals.icu")
    if has("push_workout", "edit_workout", "update_activity", "delete_workout"):
        return ("icu-write", "Updating your workout on intervals.icu",
                "updated intervals.icu")
    if has("wellness", "get_fitness"):
        return ("icu-wellness", "Checking your fitness & wellness",
                "checked your wellness")

    # --- Reading the athlete's own files (name-gated: every Bash command also
    # carries "--athlete jamie", so match the tool name, not the hint) ----------
    if n in ("read", "grep", "glob"):
        if "current-state" in blob:
            return ("state", "Checking your recent training & wellness",
                    "checked your recent training")
        if has("session-log", "swim-log", "run-durability", "decoupling"):
            return ("log", "Checking your session history",
                    "checked your session log")
        if "blueprint" in blob:
            return ("bp", "Reading your season blueprint", "read your blueprint")
        if "heat-log" in blob:
            return ("heat", "Checking your heat history", "checked your heat log")
        if has("persistent-rules", "feedback-log", "system_prompt", "rules",
               "reference"):
            return ("rules", "Checking your coaching notes", "checked your notes")
        if "plan" in blob:
            return ("planread", "Reading your current plan", "read your plan")
        return ("state", "Checking your training data", "checked your data")

    # --- Writing the athlete's own files ---------------------------------------
    if n in ("write", "edit", "multiedit"):
        if has("session-log", "current-state", "swim-log", "run-durability",
               "decoupling"):
            return ("logwrite", "Updating your session log",
                    "updated your session log")
        if has("feedback-log", "persistent-rules", "system_prompt", "rules"):
            return ("save", "Saving that to your coaching notes",
                    "saved your preference")
        if has("blueprint", "plan"):
            return ("planwrite", "Updating your plan", "updated your plan")
        if "heat-log" in blob:
            return ("heat", "Logging your heat dose", "logged your heat dose")
        return ("save", "Saving your data", "saved your data")

    return ("work", "Working on it", "crunched the numbers")

class _StatusTicker:
    """Owns msg1 (the status message). A single background thread performs ALL
    edits of msg1, so the main stream loop and the elapsed-time counter never
    race. The first edit is deliberately delayed until `not_before` (~1.5s after
    the stream starts): if the reply arrives before then, `shown` stays False and
    the caller reuses the placeholder as the reply target (pre-Phase-3 single-
    message behaviour) - no pointless status flash on an instant/resumed answer.
    While one tool runs with no new events, the thread appends an elapsed-time
    suffix so a long wait (e.g. a race-plan subprocess) does not look frozen."""

    def __init__(self, token, chat_id, msg_id, not_before):
        self.token = token
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.not_before = not_before
        self._text = ""
        self._text_since = time.time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_sent = None
        self.shown = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_status(self, text):
        with self._lock:
            if text and text != self._text:
                self._text = text
                self._text_since = time.time()

    def _render(self):
        with self._lock:
            text, since = self._text, self._text_since
        if not text:
            return None
        elapsed = time.time() - since
        # Surface a live running counter after ~1s so "Thinking... (Ns)" and long
        # tool steps read as live; sub-second flashes stay clean.
        return f"{text} ({int(elapsed)}s)" if elapsed >= 1 else text

    def _edit(self, text):
        if text is None or text == self._last_sent:
            return
        self._last_sent = text
        self.shown = True
        # Plain text: status lines never carry Markdown, so no parse_mode.
        tg_post(self.token, "editMessageText", {
            "chat_id": self.chat_id, "message_id": self.msg_id, "text": text,
        })

    def _run(self):
        # Wait out the grace period; bail immediately if the reply already started.
        while time.time() < self.not_before and not self._stop.is_set():
            self._stop.wait(0.1)
        while not self._stop.is_set():
            self._edit(self._render())
            self._stop.wait(1.0)

    def stop(self):
        """Halt msg1 edits and wait for any in-flight edit to finish, so the
        caller can send msg2 without Telegram interleaving the two messages."""
        self._stop.set()
        self._thread.join(timeout=3)


def call_claude_streaming(token, chat_id, placeholder_id,
                          user_message, config, history, model=MODEL_OPUS,
                          system_prompt_file=None, athlete_name="Jamie", context=""):
    """Telegram wrapper over engine.stream_claude driving ONLY msg1, the silent
    live-status message (Phase 3c). All generation lives in lib/engine.py; this
    only does transport.

    msg1 (the placeholder) is SILENT (disable_notification set by the caller) and
    becomes a LIVE STATUS line rewritten ~1/sec by a background ticker. It ALWAYS
    shows activity (Phase 3d): "Thinking... (Ns)" from the off, replaced by tool
    lines when tools run ("Checking intervals.icu...", "Building the session...").
    It reports what the bot is doing; it is NEVER the answer.

    This function DOES NOT send or stream the reply. It consumes the stream purely
    to (a) drive the status ticker and (b) accumulate the final response text. The
    caller sends the fully-processed reply ONCE, as a NEW notifying message, only
    after generation finishes - so the single Telegram push lands on the complete
    answer and its preview reads as the real reply, never a placeholder. Phase 3c
    removes the old born-as-placeholder streamed reply that pushed early as "...".

    Returns (response, summary):
      response - the final assistant text (post-processing happens in the caller);
      summary  - the one-line collapse text the caller ALWAYS writes onto msg1
                 (Phase 3d: a tool summary like "Checked intervals.icu, built the
                 session" if any tool ran, else "Thought for Ns"). msg1 is never
                 deleted, so a thinking message is a consistent part of every turn.
    No reply message is sent here: the caller owns reply delivery."""
    t_start = time.time()
    not_before = t_start + 1.5
    tools_seen = []          # ordered, deduped (key, past) for the collapse line
    final = None

    # msg1 ALWAYS shows a live status (Phase 3d): start the ticker up front on
    # "Thinking..." so a tool-free turn still gets a live "Thinking... (Ns)" line,
    # not a bare "…". Tool events replace the text with the specific step below.
    ticker = _StatusTicker(token, chat_id, placeholder_id, not_before)
    ticker.set_status("Thinking...")

    # try/finally guarantees the background ticker is always torn down - an
    # orphaned ticker thread would keep editing msg1 every second forever.
    try:
        for event in stream_claude(
            user_message, config, history, model=model,
            system_prompt_file=system_prompt_file, athlete_name=athlete_name, context=context,
        ):
            kind = event[0]
            if kind == "final":
                final = event[1]
                break

            if kind == "status":
                # A tool_use block appeared - update msg1 (never logged, never a reply).
                name, hint = event[1], event[2]
                key, live, past = _classify_tool(name, hint)
                if all(k != key for k, _ in tools_seen):
                    tools_seen.append((key, past))
                ticker.set_status(live)
                continue

            # kind == "chunk": partial reply snapshots. Consumed only to keep the
            # stream draining; we NO LONGER render partial reply text to Telegram.
            # The complete reply is sent once, by the caller, after generation ends.
            continue
    finally:
        ticker.stop()

    # Build the one-line collapse summary the caller ALWAYS writes onto msg1
    # (Phase 3d: never deleted). If tools ran, summarise them; otherwise report the
    # thinking time so the collapsed message still says something useful.
    if tools_seen:
        pasts = [p for _, p in tools_seen]
        summary = ", ".join(pasts)
        summary = summary[0].upper() + summary[1:]
    else:
        summary = f"Thought for {max(1, int(round(time.time() - t_start)))}s"

    return (final if final is not None else "(no response)", summary)


def _chat_reply_worker(token, chat_id, config, athlete, files, athlete_name, slug, text):
    """The slow text-chat generation path, run off the poll loop on the reply
    pool (under the chat lock). Mirrors the old inline try-block exactly."""
    try:
        history = load_history(files["history"])
        context = prefetch_context(slug)
        model = select_model(text, history)
        before_ts = time.time()

        if _voice_mode_on(slug):
            # Sticky voice mode: the TEXT stays the normal rich style (as it used
            # to be) - only the AUDIO is the voice-friendly version. Generate the
            # rich reply, show it, then speak a short conversational rewrite of it.
            # Charts still go out as images. Any TTS failure falls through to the
            # rich text send - a reply is never dropped.
            tg_post(token, "sendChatAction", {"chat_id": chat_id, "action": "record_voice"})
            response = call_claude(
                text, config, history, model=model,
                system_prompt_file=files["system_prompt"], athlete_name=athlete_name,
                context=context,
            )
            clean = process_charts(token, chat_id, response, slug=slug)
            clean = _verify_logged_reply(
                slug, before_ts, clean, text=text,
                retry=_make_capture_retry(text, config, history, model, files,
                                          athlete_name, context))
            clean = _verify_session_preview(slug, clean)
            clean = _strip_model_countdown(clean, athlete)
            final = (clean + response_footer(model, slug=slug, athlete_cfg=athlete)).strip()
            # Send the text reply FIRST so the athlete sees the answer immediately —
            # then do the (slower) spoken rewrite + TTS and send the voice note after.
            # Previously the text waited on the rewrite/TTS, adding up to ~25s+ and,
            # when the rewrite hung, blocking the reply entirely.
            if clean:
                send(token, chat_id, final, reply_markup=_reply_inline(slug))
                try:
                    spoken = _clean_for_speech(_spoken_rewrite(clean))
                    ogg = synthesize_voice(spoken) if spoken else None
                    if ogg:
                        send_voice(token, chat_id, ogg)
                except Exception as _ve:
                    log(f"[{slug}] voice synth failed (text already sent): {_ve}")
            log(f"[{slug}] Out (voice): {clean[:80]}")
            history.append(_hist_entry(text, clean))
            save_history(history, files["history"])
            return

        typing(token, chat_id)
        # msg1: the SILENT (disable_notification) live-status / "thinking" placeholder.
        # It reports what the bot is doing and must NEVER fire a push - the single push
        # per turn lands on the complete reply (msg2) sent below, once generation has
        # finished. call_claude_streaming drives this message and returns the final
        # text; it no longer sends or streams any reply (Phase 3c).
        placeholder = tg_post(token, "sendMessage", {
            "chat_id": chat_id, "text": "…", "parse_mode": "Markdown",
            "disable_notification": True,
        })
        placeholder_id = (placeholder.get("result") or {}).get("message_id")

        summary = None
        if placeholder_id:
            response, summary = call_claude_streaming(
                token, chat_id, placeholder_id,
                text, config, history, model=model,
                system_prompt_file=files["system_prompt"],
                athlete_name=athlete_name, context=context,
            )
        else:
            # msg1 send failed (rare API error): fall back to a non-streaming call so
            # the reply is still produced and delivered below.
            response = call_claude(text, config, history, model=model,
                                   system_prompt_file=files["system_prompt"],
                                   athlete_name=athlete_name, context=context)

        clean = process_charts(token, chat_id, response, slug=slug)
        clean = _verify_logged_reply(
            slug, before_ts, clean, text=text,
            retry=_make_capture_retry(text, config, history, model, files,
                                      athlete_name, context))
        clean = _verify_session_preview(slug, clean)
        clean = _strip_model_countdown(clean, athlete)
        final = (clean + response_footer(model, slug=slug, athlete_cfg=athlete)).strip()

        # msg2: the FINAL, COMPLETE reply, sent ONCE as a NEW NOTIFYING message only
        # now that generation is done. send() notifies by default and handles >4096
        # chunking + Markdown->plain fallback + the reply keyboard. Because it is sent
        # complete, the Telegram push preview shows the real answer and the push fires
        # when the answer is ready - the whole point of Phase 3c. A charts-only reply
        # (clean == "") sends no msg2; the charts already went out as their own messages.
        msg2_id = None
        if clean:
            msg2_id = send(token, chat_id, final, reply_markup=_reply_inline(slug))

        # Tidy msg1 AFTER the reply is sent: ALWAYS collapse it to the one-line summary
        # (tool summary if tools ran, else "Thought for Ns"); NEVER delete it, so a
        # thinking message is a consistent part of every turn (Phase 3d). It stays
        # silent - editMessageText never pings - so the reply is still the only push.
        if placeholder_id:
            tg_post(token, "editMessageText",
                    {"chat_id": chat_id, "message_id": placeholder_id,
                     "text": summary or "Done"})

        log(f"[{slug}] reply delivered: msg1_summary={summary!r} "
            f"msg2_id={msg2_id} chars={len(clean)}")
        log(f"[{slug}] Out: {clean[:80]}")

        history.append(_hist_entry(text, clean))
        save_history(history, files["history"])
    except Exception as _reply_err:
        log(f"[{slug}] reply handling error for {chat_id}: {_reply_err}")
        try:
            send(token, chat_id, "Sorry - I hit a snag answering that. "
                 "Give it another go in a moment.")
            # Still logged as "Out" even though it's an apology, not the real
            # answer - the delivery watchdog only cares that SOMETHING was sent
            # back, so a handled exception here must never look like a dropped reply.
            log(f"[{slug}] Out (error-fallback): sent apology after {_reply_err}")
        except Exception:
            pass


def _image_reply_worker(token, chat_id, file_id, caption, athlete_entry, config):
    """Image analysis path, run off the poll loop on the reply pool."""
    import tempfile as _tempfile
    typing(token, chat_id)
    raw = download_tg_file(token, file_id)
    if not raw:
        return
    with _tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(raw)
        img_path = tf.name
    try:
        # Silent status (Phase 3c): the "On it" note must not ping; the single push
        # lands on the final image reply below (sent notifying by default).
        send(token, chat_id, "_On it..._", disable_notification=True)
        slug = athlete_entry["slug"]
        files = athlete_files(slug)
        athlete_name = athlete_entry.get("name", slug).split()[0]
        history = load_history(files["history"])
        context = prefetch_context(slug)
        response = call_claude_with_image(
            img_path, caption, config, history,
            system_prompt_file=files["system_prompt"],
            athlete_name=athlete_name, context=context,
        )
        clean = process_charts(token, chat_id, response, slug=slug)
        clean = _strip_model_countdown(clean, athlete_entry)
        if clean:
            send(token, chat_id,
                 clean + response_footer(MODEL_OPUS, slug=slug, athlete_cfg=athlete_entry),
                 reply_markup=build_keyboard(slug))
        log(f"[{slug}] Out (image): {clean[:80]}")
        # Caption stored bare (never the "[image]" placeholder) and tagged kind=image,
        # so a later lookup can't mistake this for a real text message with that literal
        # content, or a real text message for a photo.
        history.append(_hist_entry(caption or "", clean, kind="image"))
        save_history(history, files["history"])
    finally:
        try:
            os.unlink(img_path)
        except Exception:
            pass


def _raceplan_worker(token, chat_id, slug):
    """Race-plan generation (blocking ~60s subprocess), run off the poll loop."""
    try:
        r = subprocess.run(
            ["python3", str(BASE.parent / "scripts/generate-race-plan.py"),
             "--athlete", slug],
            capture_output=True, text=True,
            cwd=str(PROJECT_DIR), timeout=60,
        )
        if r.returncode == 0:
            out = r.stdout.strip() or "Race plan updated."
            send(token, chat_id,
                 f"{out}\n\nAsk me to _summarise my race plan_ for the targets.",
                 reply_markup=build_keyboard(slug))
        else:
            send(token, chat_id,
                 f"Race plan generation failed:\n{r.stderr.strip()[:300]}",
                 reply_markup=build_keyboard(slug))
    except Exception as e:
        send(token, chat_id, f"Error generating race plan: {e}", reply_markup=build_keyboard(slug))
    log(f"[{slug}] Out (fast): race plan generated")


def _route_text(token, chat_id, text, athletes, config):
    """Route a typed-or-transcribed text message: admin/onboarding, fast paths,
    then the chat-reply worker. Called synchronously from the poll loop for typed
    messages, and from _voice_reply_worker after transcription."""
    # Admin commands (/invite, /approve) — handled before athlete routing
    if handle_admin_command(token, chat_id, text, config):
        return

    athlete = athletes.get(chat_id)
    if not athlete:
        if handle_onboarding(token, chat_id, text):
            return
        log(f"Unregistered message from chat_id {chat_id}: {text[:60]}")
        send(token, chat_id, "This account isn't registered with ClaudeCoach yet.")
        # Alert admin so missing chat_ids are caught immediately
        admin_id = str(config.get("admin_chat_id") or config.get("chat_id", ""))
        if admin_id and admin_id != chat_id:
            send(token, admin_id,
                 f"⚠️ Unregistered message\nchat\\_id: `{chat_id}`\n_{text[:120]}_")
        return

    slug = athlete["slug"]
    files = athlete_files(slug)
    athlete_name = athlete.get("name", slug).split()[0]  # first name only

    # Bug-fix Edit follow-up: this message is the revision for a pending review.
    if chat_id in _PENDING_BUG_EDIT:
        _rid = _PENDING_BUG_EDIT.pop(chat_id)
        send(token, chat_id, "On it — revising that fix; I'll re-post when it's ready.")
        subprocess.Popen(["python3", str(_BUGFIXER), "--refix", _rid, "--feedback", text],
                         cwd=config.get("project_dir"), start_new_session=True)
        return

    # Sticky voice-reply toggle: /voice [on|off] or the 🎙 menu button.
    _vt = text.strip().lower()
    if _vt == "/voice" or _vt.startswith("/voice ") or _vt == "🎙 voice":
        arg = _vt.replace("🎙 voice", "").replace("/voice", "").strip()
        if arg in ("on", "start", "enable"):
            new_state = True
        elif arg in ("off", "stop", "disable"):
            new_state = False
        else:
            new_state = not _voice_mode_on(slug)   # bare /voice or button = flip
        _set_voice_mode(slug, new_state)
        if new_state:
            vb = synthesize_voice(
                "Voice replies are on. I'll talk back to you from now on. "
                "Say or type voice off any time to switch back to text.")
            note = ("🎙 Voice replies *on* - I'll talk back to you. "
                    "Send /voice off (or tap 🎙 Voice again) to return to text.")
            if vb:
                send_voice(token, chat_id, vb)
                send(token, chat_id, note, reply_markup=build_keyboard(slug))
            else:
                send(token, chat_id,
                     note + "\n\n_Voice engine is unavailable right now, so replies stay text until it's back._",
                     reply_markup=build_keyboard(slug))
        else:
            send(token, chat_id, "🔇 Voice replies *off* - back to text.",
                 reply_markup=build_keyboard(slug))
        log(f"[{slug}] Out (fast): voice mode {'on' if new_state else 'off'}")
        return

    if text.lower() in ("/start", "/help"):
        race_name  = athlete.get("race_name", "your race")
        cycle_help = ("  period started — log cycle start (phases feed your plan)\n"
                      if _menstrual is not None and _menstrual.enabled(slug) else "")
        send(token, chat_id,
             f"*ClaudeCoach* — {race_name}\n\n"
             "*Quick commands (instant):*\n"
             "  /week — this week's sessions + Load\n"
             "  /form — Fitness/Fatigue/Form + race projection\n"
             "  /load — training load chart (±8 days)\n"
             "  /fitness — Fitness & Fatigue + Form charts\n"
             "  strength — today's gym session\n"
             "  ankle 3 — log pain score\n"
             "  82.5 kg — log weight\n"
             "  heat 30 — log heat session\n"
             f"{cycle_help}\n"
             "*Ask anything:*\n"
             "  _how am I looking?_\n"
             "  _what's today's session?_\n"
             "  _log session_ (after a workout)\n"
             "  _rebalance plan_\n"
             "  _generate plan_",
             reply_markup=build_keyboard(slug))
        return

    # Command-menu shortcuts that map to a natural-language question.
    if text.strip().lower() in _SLASH_QUESTION:
        text = _SLASH_QUESTION[text.strip().lower()]

    log(f"[{slug}] In: {text[:80]}")

    fast = fast_path(text, slug=slug, athlete_cfg=athlete)
    if fast == "__REPLAN__":
        _PENDING_REPLAN[chat_id] = time.time() + 60
        send(token, chat_id,
             "⚠️ Replan will rebuild this week from scratch — confirm?",
             reply_markup={"inline_keyboard": [[
                 {"text": "✅ Yes, replan",  "callback_data": "__REPLAN_CONFIRM__"},
                 {"text": "❌ Cancel",        "callback_data": "__REPLAN_CANCEL__"},
             ]]})
        return
    if fast in ("__GENERATE_PLAN__", "__REPLAN__"):
        is_replan = fast == "__REPLAN__"
        send(token, chat_id,
             "_Rebuilding your week to target — this takes a few minutes…_" if is_replan
             else "_Generating plan — this takes a few minutes…_")
        try:
            # Launch generate-plan DETACHED and return immediately. The script
            # sends the athlete-facing message itself via notify.py when it
            # finishes, so the bot does NOT need to wait for it. A *blocking*
            # subprocess.run froze the whole single-threaded bot for every
            # athlete while a plan generated, and timed out at 600s on a
            # populated replan (a fortnight of sessions = many sequential ICU
            # edits). `timeout` gives a generous hard cap so a stuck run can't
            # linger forever. Do NOT echo stdout (caused duplicate messages).
            # Two-stage engine (gated --push, --notify messages the athlete on
            # completion). Replaces the old generate-plan for replan/generate.
            cmd = ["timeout", "2700", "python3", str(STAGE1_PLAN_SCRIPT),
                   "--athlete", slug, "--push", "--notify"]
            override = _extract_plan_override(slug)
            if override:
                override_path = _write_plan_override(slug, override)
                cmd += ["--override-json", override_path]
                log(f"[{slug}] plan using conversation-agreed plan override")
            subprocess.Popen(
                cmd, cwd=str(PROJECT_DIR),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log(f"[{slug}] {'replan' if is_replan else 'plan'} launched in background (detached)")
        except Exception as e:
            send(token, chat_id, f"Couldn't start plan generation: {e}",
                 reply_markup=build_keyboard(slug))
        return
    elif fast and fast.startswith("__FTP_RETEST__:"):
        new_ftp = int(float(fast.split(":", 1)[1]))
        reply = _update_ftp(slug, new_ftp)
        _mark_test_completed(slug, "ftp")
        send(token, chat_id, reply, reply_markup=build_keyboard(slug))
        log(f"[{slug}] Out (FTP update): {new_ftp} W")
        return
    elif fast and fast.startswith("__CSS__:"):
        reply = _update_css(slug, fast.split(":", 1)[1])
        send(token, chat_id, reply, reply_markup=build_keyboard(slug))
        log(f"[{slug}] Out (CSS update): {fast.split(':', 1)[1]}")
        return
    elif fast and fast.startswith("__LTHR__:"):
        reply = _update_lthr(slug, int(fast.split(":", 1)[1]))
        send(token, chat_id, reply, reply_markup=build_keyboard(slug))
        log(f"[{slug}] Out (LTHR update): {fast.split(':', 1)[1]}")
        return
    elif fast == "__LOAD_CHART__":
        typing(token, chat_id)
        log(f"[{slug}] Out (fast): load chart")
        _load_chart_quick(token, chat_id, slug)
        return
    elif fast == "__FITNESS_CHARTS__":
        typing(token, chat_id)
        log(f"[{slug}] Out (fast): fitness charts")
        _fitness_charts_quick(token, chat_id, slug)
        return
    elif fast == "__GRAPHS__":
        send(token, chat_id, "*Graphs* — pick one:", reply_markup={"inline_keyboard": [
            [{"text": "📈 Fitness & Form", "callback_data": "/fitness"}],
            [{"text": "🔋 Load",            "callback_data": "/load"}],
            [{"text": "💪 Durability",       "callback_data": "/durability"}],
            [{"text": "😴 Recovery",         "callback_data": "/recovery"}],
            [{"text": "✅ Compliance",       "callback_data": "/compliance"}],
            [{"text": "⚡ Power curve",      "callback_data": "/powercurve"}],
        ]})
        log(f"[{slug}] Out (fast): graphs menu")
        return
    elif fast == "__DURABILITY__":
        typing(token, chat_id); log(f"[{slug}] Out (fast): durability")
        _durability_chart_quick(token, chat_id, slug)
        return
    elif fast == "__RECOVERY__":
        typing(token, chat_id); log(f"[{slug}] Out (fast): recovery")
        _recovery_chart_quick(token, chat_id, slug)
        return
    elif fast == "__COMPLIANCE__":
        typing(token, chat_id); log(f"[{slug}] Out (fast): compliance")
        _compliance_chart_quick(token, chat_id, slug)
        return
    elif fast == "__POWERCURVE__":
        typing(token, chat_id); log(f"[{slug}] Out (fast): power curve")
        _power_curve_quick(token, chat_id, slug)
        return
    elif fast == "__ACTIVITY_CHECK__":
        send(token, chat_id, "_Checking for new activity…_")
        # Detached single-athlete run; messages the athlete itself via
        # notify.py (new activity, or a 'nothing new' confirmation).
        subprocess.Popen(
            ["python3", str(BASE.parent / "scripts/activity-watcher.py"),
             "--athlete", slug],
            cwd=str(PROJECT_DIR),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log(f"[{slug}] on-demand activity check launched")
        return
    elif fast == "__WEEKLY_SUMMARY__":
        send(token, chat_id,
             "_Weekly summary running — Telegram message incoming in ~3 minutes._")
        subprocess.Popen(
            ["python3",
             str(BASE.parent / "scripts/weekly-summary.py"),
             "--athlete", slug],
            cwd=str(PROJECT_DIR),
        )
        log(f"[{slug}] Out (fast): weekly summary triggered")
        return
    elif fast:
        send(token, chat_id, fast, reply_markup=build_keyboard(slug))
        log(f"[{slug}] Out (fast): {fast[:80]}")
        return

    if _RACE_PLAN_RE.match(text.strip()):
        send(token, chat_id, "_Updating race plan..._")
        _submit(_raceplan_worker, chat_id, token, chat_id, slug)
        return

    # Slow generation runs off the poll loop so one athlete's 15-40s reply
    # never blocks the others. The per-chat lock (in _submit) serialises
    # this athlete's own messages so history.json can't be corrupted.
    _submit(_chat_reply_worker, chat_id,
            token, chat_id, config, athlete, files, athlete_name, slug, text)


def _voice_reply_worker(token, chat_id, file_id, athletes, config):
    """Transcribe a voice note off the poll loop, then route the text. Whisper is
    CPU-heavy (several seconds) and used to block every other athlete inline."""
    typing(token, chat_id)
    raw = download_tg_file(token, file_id)
    if not raw:
        return
    text = transcribe_voice(raw) or ""
    if not text:
        return
    # Silent transcription echo (Phase 3c): confirming what was heard must not ping;
    # the single push lands on the answer produced by _route_text below.
    send(token, chat_id, f"_Heard: {text}_", disable_notification=True)
    _route_text(token, chat_id, text, athletes, config)


def get_updates(token, offset):
    return tg_get(token, "getUpdates", {"offset": offset, "timeout": 30})


def main():
    config = load_config()

    if config["bot_token"] == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("Edit ClaudeCoach/telegram/config.json with your bot token and chat ID first.")
        sys.exit(1)

    token = config["bot_token"]
    athletes = load_athletes()
    log(f"ClaudeCoach bot started. claude={CLAUDE_BIN}. Registered athletes: {[a['name'] for a in athletes.values()]}")

    # Populate Telegram's command menu (the always-visible menu button next to the
    # text input) so actions are reachable without clearing typed text / hunting
    # the reply keyboard. Best-effort; a failure here must never stop the bot.
    try:
        tg_post(token, "setMyCommands", {"commands": [
            {"command": c, "description": d} for c, d in BOT_COMMANDS]})
        tg_post(token, "setChatMenuButton", {"menu_button": {"type": "commands"}})
    except Exception as _e:
        log(f"setMyCommands (non-fatal): {_e}")
    if not shutil.which(CLAUDE_BIN) and not os.path.isfile(CLAUDE_BIN):
        log(f"CRITICAL: claude binary not found at '{CLAUDE_BIN}' — all AI responses will fail")
        send(token, config.get("chat_id", ""), f"⚠️ Bot started but claude binary not found at `{CLAUDE_BIN}` — fix config.json")

    # Integrity check: flag any athlete directory that has no chat_id in athletes.json
    athletes_raw = json.loads(ATHLETES_CONFIG.read_text()) if ATHLETES_CONFIG.exists() else {}
    athletes_dir = BASE.parent / "athletes"
    for slug_dir in (athletes_dir.iterdir() if athletes_dir.exists() else []):
        if not slug_dir.is_dir():
            continue
        slug = slug_dir.name
        if slug.startswith("_"):
            continue  # not an athlete (e.g. _shared global-rules store)
        entry = athletes_raw.get(slug, {})
        if not entry.get("chat_id") and entry.get("active", True):
            log(f"CONFIG WARNING: athlete '{slug}' has no chat_id in athletes.json — they cannot receive messages")

    get_whisper()

    offset = 0
    while True:
        # Liveness heartbeat — bot-watchdog.py restarts the service if this goes stale,
        # so a wedged poll loop can't sit dead silently (no systemd watchdog needed).
        try:
            HEARTBEAT_FILE.write_text(datetime.now().isoformat())
        except Exception:
            pass
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
                msg_id = cq.get("message", {}).get("message_id")
                # 🔊 Speak: re-render the last reply as a voice note on demand,
                # regardless of voice mode (feature req 2026-06-21).
                if text == "__SPEAK_LAST__":
                    _ath = athletes.get(chat_id)
                    if _ath:
                        _hist = load_history(athlete_files(_ath["slug"])["history"])
                        _last = next((h.get("assistant") for h in reversed(_hist)
                                      if (h.get("assistant") or "").strip()), "")
                        if _last:
                            tg_post(token, "sendChatAction", {"chat_id": chat_id, "action": "record_voice"})
                            _ogg = synthesize_voice(_clean_for_speech(_spoken_rewrite(_last)))
                            if _ogg:
                                send_voice(token, chat_id, _ogg)
                            else:
                                send(token, chat_id, "_Couldn't generate the voice note just now._")
                    continue
                if text.startswith("bf:") and _handle_bugfix(token, chat_id, text, athletes, config):
                    continue
                if _handle_quick_log(token, chat_id, text, msg_id, athletes):
                    continue
                if _handle_test_confirm(token, chat_id, text, msg_id, athletes):
                    continue
                if _handle_replan_confirm(token, chat_id, text, msg_id, athletes):
                    continue
                if _handle_drill(token, chat_id, text, msg_id, athletes, config):
                    continue
                # Debounce duplicate command callbacks (e.g. /load): a failed
                # answerCallbackQuery leaves the button spinning, so users re-tap.
                # Quick-log/drill taps above are exempt — repeat taps there are legit.
                _cb_key = (chat_id, text)
                _cb_now = time.time()
                if _cb_now - _RECENT_CALLBACKS.get(_cb_key, 0) < 15:
                    log(f"debounced duplicate callback: {text}")
                    continue
                _RECENT_CALLBACKS[_cb_key] = _cb_now
            else:
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                if not text:
                    voice = msg.get("voice") or msg.get("audio")
                    if voice and chat_id in athletes:
                        # Offload transcription (slow Whisper) + routing so a voice
                        # note doesn't freeze the poll loop for other athletes.
                        _submit(_voice_reply_worker, chat_id, token, chat_id,
                                voice["file_id"], athletes, config)
                    photo = msg.get("photo")
                    if photo and chat_id in athletes:
                        caption = (msg.get("caption") or "").strip()
                        # Offload the blocking download + image generation so it
                        # doesn't freeze the poll loop for other athletes.
                        _submit(_image_reply_worker, chat_id, token, chat_id,
                                photo[-1]["file_id"], caption, athletes[chat_id], config)
                    continue

            if not text:
                continue

            _route_text(token, chat_id, text, athletes, config)

        time.sleep(1)


if __name__ == "__main__":
    main()
