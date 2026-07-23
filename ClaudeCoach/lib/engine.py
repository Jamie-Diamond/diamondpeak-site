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
# Commands the chat model must never run mid-reply. Restarting the service (or
# killing the process) drops the in-flight reply — the cause of the 5 self-
# restarts + ~25-min silences on 2026-07-05. Code edits/pushes stay allowed
# (intended self-improvement); they just take effect on the next natural restart.
DISALLOWED_TOOLS = (
    "Bash(systemctl *) Bash(sudo *) Bash(service *) "
    "Bash(reboot *) Bash(reboot) Bash(shutdown *) Bash(halt) "
    "Bash(kill *) Bash(pkill *)"
)
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
        # Image entries render as an explicit marker, never as bare text - a
        # captionless photo used to be stored as the literal string "[image]",
        # indistinguishable from a real text message that happened to be missing
        # (the swim-splits misdiagnosis, where the bot mistook a dropped text
        # reply for an unreadable photo).
        if h.get("kind") == "image":
            caption = h.get("user", "")
            user_line = (f'[sent a photo, caption: "{caption}"]' if caption
                         else "[sent a photo, no caption]")
        else:
            user_line = h["user"]
        lines.append(f"{stamp}{athlete_name}: {user_line}")
        lines.append(f"ClaudeCoach: {h['assistant']}")
    return lines


def load_persistent_rules(sp_file) -> str:
    """Contents of persistent-rules.md adjacent to the system prompt, or ''."""
    rules_file = Path(sp_file).parent / "persistent-rules.md"
    if rules_file.exists():
        text = rules_file.read_text().strip()
        return text if text else ""
    return ""


def load_global_rules(sp_file) -> str:
    """Shared cross-athlete coaching rules from athletes/_shared/persistent-rules.md, or ''.
    sp_file is athletes/<name>/system_prompt.txt, so the shared file is one level up."""
    gf = Path(sp_file).parent.parent / "_shared" / "persistent-rules.md"
    if gf.exists():
        text = gf.read_text().strip()
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
    "CAPTURE: When the athlete logs a rule, correction, constraint, or session data, you MUST "
    "actually write it to the correct file with the Write or Edit tool BEFORE confirming, and "
    "never say it is saved unless that write completed in this reply. Then confirm in one short "
    "line naming what you saved (e.g. 'Logged: no cycling Thu/Fri added to your rules.'). Do not "
    "reply with the bare word 'Logged.' on its own. If the message reports a genuine fault in the "
    "coaching system, record it as a bug in feedback-log.json."
)


# Accuracy hard-rails (Phase 1, 11 Jul 2026). Injected in code for EVERY athlete
# so the rule has one source of truth, not three drifting prompt copies. Targets
# the 11 Jul incidents: "220 TSS" built as 220 minutes; two Load figures for one
# session (183 vs 202); hand-summed totals that did not add up; wrong trip pulled
# from memory. The determinism lives in the tools these lines point at — this is
# only the routing rule the model cannot infer.
_ACCURACY_RULE = (
    "TRAINING-NUMBER ACCURACY — HARD RULES: "
    "(1) UNITS: a value the athlete labels TSS or Load is NEVER minutes. To turn a Load "
    "target into a session, run `python3 ClaudeCoach/lib/plan_tools.py session-for-load "
    "--sport <S> --load-target <N> [--zone <z>|--if <f>]` — it holds the Load fixed and "
    "DERIVES the duration. Never hand-convert a TSS/Load figure into minutes. "
    "(2) SINGLE LOAD: there is exactly one Load per session — ICU's icu_training_load, else "
    "load_target — obtained via `plan_tools.py session-load`. Never state a second, self-"
    "computed Load for the same session, and never derive a Load in free text. "
    "(3) NO MENTAL MATHS: any total or sum of Loads comes from a tool (`plan_tools.py sum` "
    "or `plan_tools.py tss --sessions`), never added by hand; any past trip/block is looked "
    "up by DATE RANGE via icu_fetch (history / training_summary / events), never recalled "
    "from memory."
)


# Authority precedence for planning answers (added after the 22 Jul failure).
# Injected in code for EVERY athlete, alongside _ACCURACY_RULE, so there is one
# source of truth. The 22 Jul incident: the bot improvised Kathryn's forward week
# from prose rules, zeroed her Build-phase Z4–5 run slice while asserting
# compliance, and narrated week-13 Build as "start of Peak" from memory. Root
# cause: no stated ranking between the numeric blueprint and the prose rules, and
# no validation of a stated plan against the distribution.
_AUTHORITY_RULE = (
    "PLANNING AUTHORITY — HARD RULES (apply in this order): "
    "(1) SPEC: the per-sport intensity distribution in the training blueprint "
    "(training-blueprint.json, e.g. 'Run 78% Z1–2 / 12% Z3 / 10% Z4–5') is THE SPEC "
    "for how much of each zone a week must contain. "
    "(2) PROSE REFINES, NEVER OVERRIDES: prose rules (rules.md, standing rules, notes) "
    "may refine HOW a slice is delivered (which day, session shape, cues) but MUST NOT "
    "zero out or reduce a zone slice the blueprint requires. If prose appears to remove "
    "a required slice, the blueprint wins — keep the slice. "
    "(3) ONLY GATE THAT ZEROS A SLICE: the sole thing that may drop a required quality "
    "slice is an injury/illness hard-gate read from structured current-state.json, NEVER "
    "from prose and NEVER from memory. "
    "(4) PHASE FROM CONFIG: state the training phase from the live-context 'Phase:' line "
    "(config-derived), never narrated from memory — do not call a Build week 'Peak'. "
    "(5) FORWARD PLANS FROM THE ENGINE: for any 'what will next week look like / how do we "
    "hit X' question, answer from the deterministic engine's week (the sessions already on "
    "the calendar, or the FORWARD WEEK block in the live context) — do NOT improvise a "
    "session-by-session week from prose. If the asked-about week is not generated yet, say "
    "so and give the blueprint target (phase, weekly Load, the Z1–2/Z3/Z4–5 split); do not "
    "invent specific sessions. Before telling the athlete a stated week is 'on spec', it MUST "
    "pass the distribution check in BOTH directions (enough Z4–5/Z3 AND not too much quality) "
    "— express the week as zoned segments and run `python3 ClaudeCoach/lib/plan_distribution.py "
    "--athlete <slug> --week-start <YYYY-MM-DD> --sessions '<json>'`; a non-zero exit / any "
    "OFF-SPEC finding means do NOT claim compliance — correct the week or state the gap. "
    "(6) DERIVED NUMBERS: any target you cite that is flagged derived/unconfirmed in the "
    "context must be presented as provisional, not as a confirmed figure."
)


def build_prompt(user_message, history, system_prompt, athlete_name, context,
                 persistent_rules="", global_rules=""):
    parts = [system_prompt, ""]
    if global_rules:
        parts.append("## Global coaching rules - apply to every athlete")
        parts.append(global_rules)
        parts.append("")
    if persistent_rules:
        parts.append("## Standing rules — always apply (athlete-agreed, session-derived)")
        parts.append(persistent_rules)
        parts.append("")
    parts.append(_FEEDBACK_LOG_RULE)
    parts.append("")
    parts.append(_ACCURACY_RULE)
    parts.append("")
    parts.append(_AUTHORITY_RULE)
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
    cmd = [CLAUDE_BIN, "-p", prompt, "--allowedTools", TOOLS,
           "--disallowedTools", DISALLOWED_TOOLS, "--model", model]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _assemble(user_message, history, system_prompt_file, athlete_name, context):
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    return build_prompt(
        user_message, history,
        system_prompt_with_level(sp_file), athlete_name, context,
        persistent_rules=load_persistent_rules(sp_file),
        global_rules=load_global_rules(sp_file),
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
        # Include the build_prompt rule constants: they are baked into a session
        # at start but are NOT files, so a change to them (e.g. _AUTHORITY_RULE,
        # added 22 Jul) must also rotate running sessions — otherwise a chat that
        # started under the old prompt keeps coaching without the new rule until
        # it expires (the resume path never re-injects them).
        blob = (system_prompt_with_level(sp_file) + "\n"
                + load_global_rules(sp_file) + "\n"
                + load_persistent_rules(sp_file) + "\n"
                + _FEEDBACK_LOG_RULE + "\n" + _ACCURACY_RULE + "\n" + _AUTHORITY_RULE)
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


def call_claude(user_message, config, history, model=MODEL_OPUS,
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
        if _is_limit_message(text) and model != MODEL_SONNET:
            # Opus is primary now: a capped bucket must not surface a rate-limit
            # notice to the athlete while Sonnet 5 still has headroom, so fall
            # DOWN to Sonnet so the bot still answers.
            log(f"[limit] {model} capped - retrying on {MODEL_SONNET}")
            text, sid, rc = _run_once(prompt, MODEL_SONNET, extra, config["project_dir"])
            model = MODEL_SONNET
        if rc == 0 and text:
            _finish_session(sp_file, mode, st, sid)
        _log_timing("call", model, mode, t0, None, None)
        return text or "(no response)"
    except subprocess.TimeoutExpired:
        return "Sorry, that took too long. Try a simpler question or break it into steps."
    except Exception as e:
        log(f"Claude error: {e}")
        return f"Error calling claude: {e}"


def _tool_input_summary(inp):
    """Short, plain-text hint at what a tool_use block is doing, for the live
    status line (Phase 3). Prefers the fields that identify the action - a Bash
    command, a file path - and truncates hard. Never raises; returns ''."""
    try:
        if not isinstance(inp, dict):
            return ""
        for key in ("command", "file_path", "path", "file", "query", "pattern"):
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:80]
        for val in inp.values():
            if isinstance(val, str) and val.strip():
                return val.strip()[:80]
    except Exception:
        pass
    return ""


def _stream_once(prompt, model, extra_args, cwd):
    """One streaming claude invocation. Yields ('chunk', snapshot) and, when a
    tool_use block appears, ('status', tool_name, input_summary). Returns
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
                    btype = block.get("type")
                    if btype == "text":
                        snapshot = block.get("text", "")
                    elif btype == "tool_use":
                        # Surface tool activity so the transport can show a live
                        # status line (Phase 3). Text blocks still drive the reply;
                        # this only ADDS status events - the ('chunk'|'final')
                        # contract stays intact, so downstream consumers that only
                        # know 'chunk'/'final' keep working.
                        yield ("status", block.get("name", ""),
                               _tool_input_summary(block.get("input") or {}))
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


def stream_claude(user_message, config, history, model=MODEL_OPUS,
                  system_prompt_file=None, athlete_name="Jamie", context=""):
    """Generator over a streaming Claude run. Yields:
      ('chunk', snapshot)                 — growing reply to display live (transport throttles edits)
      ('status', tool_name, input_hint)   — a tool_use block appeared; transport may show a status line
      ('final', full)                     — authoritative full reply, emitted exactly once at the end
    Only ('chunk',...) and ('final',...) carry reply text; ('status',...) is purely
    for the live UI and must never be logged or treated as the reply.
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
    if _is_limit_message(text) and model != MODEL_SONNET:
        # Opus is primary now: fall DOWN to Sonnet 5 on a cap so the athlete
        # still gets an answer rather than a rate-limit notice.
        log(f"[limit] {model} capped - retrying on {MODEL_SONNET}")
        final, streamed, sid, rc, t_init, t_first = yield from _stream_once(
            prompt, MODEL_SONNET, extra, config["project_dir"])
        text = (final if final is not None else streamed).strip()
        model = MODEL_SONNET

    if rc == 0 and text:
        _finish_session(sp_file, mode, st, sid)
    _log_timing("stream", model, mode, t0, t_init, t_first)
    yield ("final", text or "(no response)")


def call_claude_with_image(img_path, caption, config, history, model=MODEL_OPUS,
                           system_prompt_file=None, athlete_name="Jamie", context=""):
    # Image analysis stays on throwaway sessions: it needs the file-read tools
    # anyway and the exchange reaches the athlete's session via the history
    # catch-up block on the next resume.
    sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
    system_prompt = system_prompt_with_level(sp_file)
    parts = [system_prompt, ""]
    global_rules = load_global_rules(sp_file)
    if global_rules:
        parts.append("## Global coaching rules - apply to every athlete")
        parts.append(global_rules)
        parts.append("")
    persistent_rules = load_persistent_rules(sp_file)
    if persistent_rules:
        parts.append("## Standing rules — always apply (athlete-agreed, session-derived)")
        parts.append(persistent_rules)
        parts.append("")
    parts.append(_ACCURACY_RULE)
    parts.append("")
    parts.append(_AUTHORITY_RULE)
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
