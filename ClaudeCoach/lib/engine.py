#!/usr/bin/env python3
"""
ClaudeCoach shared engine — the transport-agnostic coaching brain.

Prompt assembly + Claude generation, with NO Telegram or HTTP concerns. Imported
by BOTH the Telegram bot (telegram/bot.py) and the web API (FastAPI; Phase 0 of
the web-app transition, docs/app-transition-plan.md). The streaming entry point
`stream_claude` yields plain ('chunk'|'final', text) events that each transport
renders its own way — Telegram via editMessageText, the web via SSE. Keep this
module free of transport code so the bot and the API can never diverge.

Session resume (2 Jul 2026): instead of a fresh `claude -p` session per message
(full system prompt + 12 history pairs re-ingested uncached every reply), each
athlete gets a persisted CLI session resumed via `--resume <id>`. Follow-up
messages send only the live-context block + the new message; the session carries
the system prompt and conversation, so rapid exchanges hit the server-side
prompt cache. Sessions rotate after SESSION_MAX_TURNS turns or SESSION_MAX_AGE_S,
and are invalidated when the system prompt / persistent rules change
(fingerprint). Any resume failure falls back to a fresh full-prompt session, so
the worst case is exactly the old behaviour. Disable with "session_resume":
false in config.json.
"""
import hashlib, json, subprocess, sys, time, shutil, os
from pathlib import Path
from datetime import datetime, timedelta

BASE = Path(__file__).parent.parent                # ClaudeCoach/
sys.path.insert(0, str(Path(__file__).parent))     # lib/ on path for coaching_levels

try:
    from coaching_levels import level_block as _level_block
except Exception:
    def _level_block(level: str) -> str:  # type: ignore[misc]
        return ""

try:
    from claude_call import is_limit_message as _is_limit_message
except Exception:
    def _is_limit_message(text: str) -> bool:  # type: ignore[misc]
        return False


def _resolve_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in ("/usr/bin/claude", "/usr/local/bin/claude",
                      os.path.expanduser("~/.local/bin/claude")):
        if os.path.isfile(candidate):
            return candidate
    return "claude"


CLAUDE_BIN = _resolve_claude_bin()
TOOLS = "Read,Write,Edit,Bash"
MODEL_SONNET = "claude-sonnet-5"
MODEL_OPUS   = "claude-opus-4-8"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
SYSTEM_PROMPT_FILE = BASE / "athletes/jamie/system_prompt.txt"

# How many recent exchanges to feed the model. History is persisted longer on
# disk (bot's MAX_HISTORY_PAIRS) but only the last few are worth re-sending —
# every extra pair is re-ingested uncached on every reply, inflating latency.
PROMPT_HISTORY_PAIRS = 12

# Session-resume tuning. Rotate before the session transcript grows unwieldy
# (each resume replays the whole session server-side) and daily so a stale
# thread never anchors today's coaching.
SESSION_MAX_TURNS = 30
SESSION_MAX_AGE_S = 24 * 3600
SESSION_CATCHUP_PAIRS = 6


def log(msg):
    """Default logger (stderr). The Telegram bot points this at bot.log; the web
    API can point it at its own sink — engine code calls log() either way."""
    print(f"[engine] {msg}", file=sys.stderr)


def render_history(history, athlete_name):
    lines = []
    for h in history:
        stamp = ""
        ts = h.get("ts", "")
        if ts:
            try:
                stamp = datetime.fromisoformat(ts).strftime("[%a %H:%M] ")
            except Exception:
                stamp = ""
        lines.append(f"{stamp}{athlete_name}: {h['user']}")
        lines.append(f"ClaudeCoach: {h['assistant']}")
    return lines


def load_persistent_rules(sp_file) -> str:
    """Contents of persistent-rules.md adjacent to the system prompt, or ''."""
    rules_file = Path(sp_file).parent / "persistent-rules.md"
    if rules_file.exists():
        text = rules_file.read_text().strip()
        return text if text else ""
    return ""


def system_prompt_with_level(sp_file) -> str:
    """Read system_prompt.txt and append the athlete's coaching-level block."""
    sp_file = Path(sp_file)
    text = sp_file.read_text().strip()
    profile_path = sp_file.parent / "profile.json"
    if profile_path.exists():
        try:
            level = json.loads(profile_path.read_text()).get("coaching_level", "mid")
            block = _level_block(level)
            if block:
                text = text + "\n\n" + block
        except Exception:
            pass
    return text


_FEEDBACK_LOG_RULE = (
    "HARD RULE — feedback acknowledgement: When the athlete logs feedback, corrections, "
    "or session data in a single message, reply with exactly one word: Logged. "
    "Do not echo, summarise, or acknowledge the content."
)


def build_prompt(user_message, history, system_prompt, athlete_name, context,
                 persistent_rules=""):
    parts = [system_prompt, ""]
    if persistent_rules:
        parts.append("## Standing rules — always apply (athlete-agreed, session-derived)")
        parts.append(persistent_rules)
        parts.append("")
    parts.append(_FEEDBACK_LOG_RULE)
    parts.append("")
    if context:
        parts.append(context)
        parts.append("")
    if history:
        parts.append("Recent conversation:")
        parts.extend(render_history(history[-PROMPT_HISTORY_PAIRS:], athlete_name))
        parts.append("")
    parts.append(f"{athlete_name}: {user_message}")
    return "\n".join(parts)


def claude_cmd(prompt, model, extra_args=None):
    cmd = [CLAUDE_BIN, "-p", prompt, "--allowedTools", TOOLS, "--model", model]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _assemble(user_message, history, system_prompt_file, athlete_name, context):
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    return build_prompt(
        user_message, history,
        system_prompt_with_level(sp_file), athlete_name, context,
        persistent_rules=load_persistent_rules(sp_file),
    )


# ---------------------------------------------------------------------------
# Session-resume state (one .chat_session.json per athlete, next to the
# system prompt; athletes/ is gitignored so this never lands in the repo)
# ---------------------------------------------------------------------------

def _session_path(sp_file) -> Path:
    return Path(sp_file).parent / ".chat_session.json"


def _prompt_fingerprint(sp_file) -> str:
    """Hash of everything baked into a session at start. If the system prompt,
    coaching level or persistent rules change, running sessions are stale —
    rotate rather than coach on the old rules."""
    sp_file = Path(sp_file)
    try:
        blob = system_prompt_with_level(sp_file) + "\n" + load_persistent_rules(sp_file)
    except Exception:
        return ""
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _load_session(sp_file):
    try:
        st = json.loads(_session_path(sp_file).read_text())
        if st.get("session_id"):
            return st
    except Exception:
        pass
    return None


def _save_session(sp_file, st):
    try:
        _session_path(sp_file).write_text(json.dumps(st))
    except Exception as e:
        log(f"session state save failed: {e}")


def _clear_session(sp_file):
    try:
        _session_path(sp_file).unlink(missing_ok=True)
    except Exception:
        pass


def _session_usable(st, fp) -> bool:
    return bool(
        st and fp and st.get("fp") == fp
        and st.get("turns", 0) < SESSION_MAX_TURNS
        and time.time() - st.get("started", 0) < SESSION_MAX_AGE_S
    )


def _resume_prompt(user_message, history, athlete_name, context, last_seen):
    """Prompt for a resumed session: live-context block + any exchanges the
    session missed (voice notes, fast-path buttons — they append to history
    without passing through the session) + the new message. No system prompt,
    no rolling history — the session already carries both."""
    parts = []
    if context:
        parts.append(context)
        parts.append("")
    missed = [h for h in history if h.get("ts", "") > (last_seen or "")]
    missed = missed[-SESSION_CATCHUP_PAIRS:]
    if missed:
        parts.append("(For context — exchanges logged outside this thread since your last reply:)")
        parts.extend(render_history(missed, athlete_name))
        parts.append("")
    parts.append(f"{athlete_name}: {user_message}")
    return "\n".join(parts)


def _plan_session(user_message, config, history, sp_file, athlete_name, context):
    """Decide how this call runs. Returns (extra_args, prompt, mode, state):
    mode 'stateless' — old behaviour, fresh throwaway session (config opt-out)
    mode 'resume'    — continue the athlete's persisted session
    mode 'new'       — start a persisted session with the full prompt"""
    if not config.get("session_resume", True):
        return (["--no-session-persistence"],
                _assemble(user_message, history, sp_file, athlete_name, context),
                "stateless", None)
    st = _load_session(sp_file)
    if _session_usable(st, _prompt_fingerprint(sp_file)):
        return (["--resume", st["session_id"]],
                _resume_prompt(user_message, history, athlete_name, context,
                               st.get("last_seen", "")),
                "resume", st)
    return ([], _assemble(user_message, history, sp_file, athlete_name, context),
            "new", None)


def _finish_session(sp_file, mode, st, session_id):
    """Persist session state after a successful turn. last_seen is stamped 10s
    in the future: the bot appends this exchange to history.json moments after
    we return, and without the skew that entry would look "missed" and be
    re-injected on the next resume."""
    if mode == "stateless":
        return
    last_seen = (datetime.now() + timedelta(seconds=10)).isoformat()
    if mode == "resume" and st:
        st["turns"] = st.get("turns", 0) + 1
        st["last_seen"] = last_seen
        _save_session(sp_file, st)
    elif mode == "new" and session_id:
        _save_session(sp_file, {
            "session_id": session_id,
            "fp": _prompt_fingerprint(sp_file),
            "turns": 1,
            "started": time.time(),
            "last_seen": last_seen,
        })


def _log_timing(path, model, mode, t0, t_init, t_first):
    """One line per reply so latency can be split into CLI boot (spawn→init),
    ingest+thinking (init→first text) and generation (→total)."""
    t_end = time.time()
    boot = f"{t_init - t0:.1f}" if t_init else "?"
    first = f"{t_first - t0:.1f}" if t_first else "?"
    log(f"[timing] {path} model={model} session={mode} "
        f"boot={boot}s first_text={first}s total={t_end - t0:.1f}s")


# ---------------------------------------------------------------------------
# Generation entry points
# ---------------------------------------------------------------------------

def _run_once(prompt, model, extra_args, cwd, timeout=300):
    """One non-streaming claude invocation with JSON output so the session id
    is capturable. Returns (text, session_id, returncode)."""
    # stdin=DEVNULL: `claude -p` treats piped stdin as extra prompt input, so an
    # inherited descriptor can leak parent-process content into the conversation.
    r = subprocess.run(
        claude_cmd(prompt, model, ["--output-format", "json"] + extra_args),
        capture_output=True, text=True, cwd=cwd, timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    text, session_id = "", None
    try:
        d = json.loads(r.stdout or "")
        text = (d.get("result") or "").strip()
        session_id = d.get("session_id")
    except Exception:
        text = (r.stdout or "").strip()
    return text or (r.stderr or "").strip(), session_id, r.returncode


def call_claude(user_message, config, history, model=MODEL_SONNET,
                system_prompt_file=None, athlete_name="Jamie", context=""):
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    extra, prompt, mode, st = _plan_session(user_message, config, history,
                                            sp_file, athlete_name, context)
    t0 = time.time()
    try:
        text, sid, rc = _run_once(prompt, model, extra, config["project_dir"])
        if (rc != 0 or not text) and mode == "resume":
            log(f"[session] resume failed rc={rc} — retrying with fresh session")
            _clear_session(sp_file)
            extra, prompt, mode, st = _plan_session(user_message, config, history,
                                                    sp_file, athlete_name, context)
            text, sid, rc = _run_once(prompt, model, extra, config["project_dir"])
        if _is_limit_message(text) and model != MODEL_OPUS:
            # Chat quality first: a capped Sonnet bucket must not surface a
            # rate-limit notice to the athlete while Opus still has headroom.
            log(f"[limit] {model} capped — retrying on {MODEL_OPUS}")
            text, sid, rc = _run_once(prompt, MODEL_OPUS, extra, config["project_dir"])
            model = MODEL_OPUS
        if rc == 0 and text:
            _finish_session(sp_file, mode, st, sid)
        _log_timing("call", model, mode, t0, None, None)
        return text or "(no response)"
    except subprocess.TimeoutExpired:
        return "Sorry, that took too long. Try a simpler question or break it into steps."
    except Exception as e:
        log(f"Claude error: {e}")
        return f"Error calling claude: {e}"


def _stream_once(prompt, model, extra_args, cwd):
    """One streaming claude invocation. Yields ('chunk', snapshot). Returns
    (final, streamed, session_id, returncode, t_init, t_first). Never raises —
    errors are logged and reported through the return tuple."""
    streamed = ""
    final = None
    session_id = None
    t_init = t_first = None
    rc = -1
    try:
        proc = subprocess.Popen(
            claude_cmd(prompt, model,
                       ["--output-format", "stream-json", "--verbose"] + extra_args),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, text=True, cwd=cwd,
        )
        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                ev = json.loads(raw_line)
            except Exception:
                continue
            if t_init is None:
                t_init = time.time()
            ev_type = ev.get("type", "")
            if ev_type == "system":
                session_id = ev.get("session_id") or session_id
                continue
            if ev_type == "result":
                final = ev.get("result", "") or final
                session_id = ev.get("session_id") or session_id
                continue
            elif ev_type == "assistant":
                snapshot = ""
                for block in (ev.get("message") or {}).get("content", []):
                    if block.get("type") == "text":
                        snapshot = block.get("text", "")
                if snapshot:
                    streamed = snapshot
            elif ev_type == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    streamed += delta.get("text", "")
            else:
                continue
            if streamed.strip():
                if t_first is None:
                    t_first = time.time()
                yield ("chunk", streamed.strip())
        proc.wait(timeout=10)
        rc = proc.returncode
    except Exception as e:
        log(f"Claude stream error: {e}")
    return (final, streamed, session_id, rc, t_init, t_first)


def stream_claude(user_message, config, history, model=MODEL_SONNET,
                  system_prompt_file=None, athlete_name="Jamie", context=""):
    """Generator over a streaming Claude run. Yields (kind, text):
      ('chunk', snapshot) — growing reply to display live (transport throttles edits)
      ('final', full)     — authoritative full reply, emitted exactly once at the end
    assistant events are full snapshots (replace); content_block_delta events are
    incremental (append); result replaces all. Transport-agnostic by design."""
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    extra, prompt, mode, st = _plan_session(user_message, config, history,
                                            sp_file, athlete_name, context)
    t0 = time.time()
    final, streamed, sid, rc, t_init, t_first = yield from _stream_once(
        prompt, model, extra, config["project_dir"])

    # A dead resume fails before any text streams — fall back to a fresh session.
    if mode == "resume" and rc != 0 and not (final or streamed.strip()):
        log(f"[session] resume failed rc={rc} — falling back to fresh session")
        _clear_session(sp_file)
        extra, prompt, mode, st = _plan_session(user_message, config, history,
                                                sp_file, athlete_name, context)
        final, streamed, sid, rc, t_init, t_first = yield from _stream_once(
            prompt, model, extra, config["project_dir"])

    text = (final if final is not None else streamed).strip()
    if _is_limit_message(text) and model != MODEL_OPUS:
        log(f"[limit] {model} capped — retrying on {MODEL_OPUS}")
        final, streamed, sid, rc, t_init, t_first = yield from _stream_once(
            prompt, MODEL_OPUS, extra, config["project_dir"])
        text = (final if final is not None else streamed).strip()
        model = MODEL_OPUS

    if rc == 0 and text:
        _finish_session(sp_file, mode, st, sid)
    _log_timing("stream", model, mode, t0, t_init, t_first)
    yield ("final", text or "(no response)")


def call_claude_with_image(img_path, caption, config, history, model=MODEL_SONNET,
                           system_prompt_file=None, athlete_name="Jamie", context=""):
    # Image analysis stays on throwaway sessions: it needs the file-read tools
    # anyway and the exchange reaches the athlete's session via the history
    # catch-up block on the next resume.
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    system_prompt = system_prompt_with_level(sp_file)
    parts = [system_prompt, ""]
    persistent_rules = load_persistent_rules(sp_file)
    if persistent_rules:
        parts.append("## Standing rules — always apply (athlete-agreed, session-derived)")
        parts.append(persistent_rules)
        parts.append("")
    if context:
        parts.append(context)
        parts.append("")
    if history:
        parts.append("Recent conversation:")
        parts.extend(render_history(history[-PROMPT_HISTORY_PAIRS:], athlete_name))
        parts.append("")
    question = caption if caption else "analyse this"
    user_msg = f"{athlete_name} sent an image. Read it from {img_path} then {question}."
    parts.append(user_msg)
    full_prompt = "\n".join(parts)
    t0 = time.time()
    try:
        result = subprocess.run(
            claude_cmd(full_prompt, model, ["--no-session-persistence"]),
            capture_output=True, text=True,
            cwd=config["project_dir"], timeout=300,
            stdin=subprocess.DEVNULL,
        )
        _log_timing("image", model, "stateless", t0, None, None)
        return result.stdout.strip() or result.stderr.strip() or "(no response)"
    except subprocess.TimeoutExpired:
        return "Sorry, that took too long. Try a simpler question or break it into steps."
    except Exception as e:
        log(f"Claude image error: {e}")
        return f"Error calling claude: {e}"
