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
import hashlib, json, signal, subprocess, sys, threading, time, shutil, os, uuid
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
    import illness as _illness
except Exception:
    _illness = None

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
# WebSearch/WebFetch added 2026-08-03: without them a chat-level grant from the
# athlete can never work, and the model falls back to urllib over Bash, which 429s
# on saltstick.com/highfive.co.uk and cannot read JS-rendered nutrition panels.
# They are NOT a fabrication fix - the 300mg SiS and ~600mg sausage-roll figures
# were invented while a working shell fetch route was available.
TOOLS = "Read,Write,Edit,Bash,WebSearch,WebFetch"
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
MODEL_OPUS   = "claude-opus-5"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
SYSTEM_PROMPT_FILE = BASE / "athletes/jamie/system_prompt.txt"

# How many recent exchanges to feed the model. History is persisted longer on
# disk (bot's MAX_HISTORY_PAIRS) but only the last few are worth re-sending —
# every extra pair is re-ingested uncached on every reply, inflating latency.
PROMPT_HISTORY_PAIRS = 12

# Session-resume tuning. Rotate before the session transcript grows unwieldy
# (each resume replays the whole session server-side) and daily so a stale
# thread never anchors today's coaching.
#
# 30 -> 12 (2026-08-03). The resume path sends NO system prompt and NO rules
# (see _resume_prompt): the whole 82KB / 123-rule surface goes in once at turn 1
# and is never re-sent. Jamie's .chat_session.json read turns=9 at the point
# quality collapsed on 3 Aug. Rotation is the only thing that puts the rules back
# in front of the model, and it is not a memory reset - the `new` path re-sends
# the full prompt AND the last PROMPT_HISTORY_PAIRS exchanges. 12 matches that
# window, so a rotation carries roughly what the session was already holding.
# Override per athlete with "session_max_turns" in telegram/config.json - no
# deploy needed, and setting it back to 30 restores the old behaviour exactly.
SESSION_MAX_TURNS = 12
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


def load_illness_block(sp_file, athlete_name: str = "") -> str:
    """Illness/compromised instruction block for this athlete, or ''.

    Read from the structured `illness` block in the current-state.json next to the
    system prompt (lib/illness.py). Injected for EVERY athlete and every surface that
    goes through this engine, because the 26 Jul failure was a prompt faithfully
    executing rules that had no illness gate to check — a per-athlete prose note could
    not have stopped it.
    """
    if _illness is None:
        return ""
    try:
        return _illness.prompt_block_from_dir(Path(sp_file).parent,
                                              first_name=athlete_name)
    except Exception as e:
        log(f"illness block skipped: {e}")
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
                 persistent_rules="", global_rules="", illness_block=""):
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
    # After the standing rules and the hard-rails, deliberately: an active illness
    # flag SUSPENDS the fuelling / compliance criticism those rules would otherwise
    # demand, so it has to be the last word on that. It suspends nothing safety-
    # critical — lib/illness.NOT_SUPPRESSED is spelled out inside the block.
    if illness_block:
        parts.append(illness_block)
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


def _feed_stdin(proc, prompt):
    """Write `prompt` to proc.stdin and close it, on a daemon thread so a prompt
    larger than the pipe buffer cannot block the reader before it drains.

    The bare except is load-bearing since cancellation landed (17 Aug 2026): a
    cancel kills the CLI while this thread may still be pushing an 80KB prompt
    into the pipe, so BrokenPipeError (or ValueError on an already-closed stdin)
    is now an ORDINARY outcome here, not a fault. It must stay silent - the
    cancelling athlete gets their acknowledgement from the transport, and a
    traceback on a daemon thread would only muddy bot.log."""
    def _w():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:
            pass
    threading.Thread(target=_w, daemon=True).start()


def claude_cmd(model, extra_args=None):
    """Build the CLI argv. The prompt is NOT included: it must be fed on stdin
    by the caller. A single argv element is capped at MAX_ARG_STRLEN (128 KiB
    on Linux) and exec() fails with E2BIG once a built prompt crosses it."""
    cmd = [CLAUDE_BIN, "-p", "--allowedTools", TOOLS,
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
        illness_block=load_illness_block(sp_file, athlete_name),
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
        # The illness block is in the fingerprint for the same reason the rule
        # constants are: it is baked in at session start and the --resume path never
        # re-injects it. Without this, an athlete who falls ill mid-session keeps
        # being coached without the gate for up to SESSION_MAX_TURNS / 24h — i.e.
        # exactly the scolding this flag exists to stop. A state change here rotates
        # the session so the next reply is built with (or without) the gate.
        blob = (system_prompt_with_level(sp_file) + "\n"
                + load_global_rules(sp_file) + "\n"
                + load_persistent_rules(sp_file) + "\n"
                + load_illness_block(sp_file) + "\n"
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


def _session_usable(st, fp, max_turns: int | None = None) -> bool:
    """max_turns None = the module default. Passed explicitly by _plan_session so
    "session_max_turns" in telegram/config.json can be tuned without a deploy."""
    cap = SESSION_MAX_TURNS if max_turns is None else max_turns
    return bool(
        st and fp and st.get("fp") == fp
        and st.get("turns", 0) < cap
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
    try:
        max_turns = int(config.get("session_max_turns", SESSION_MAX_TURNS))
    except (TypeError, ValueError):
        max_turns = SESSION_MAX_TURNS
    if _session_usable(st, _prompt_fingerprint(sp_file), max_turns):
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


def _log_timing(path, model, mode, t0, t_init, t_first,
                turns=None, prompt_bytes=None):
    """One line per reply so latency can be split into CLI boot (spawn→init),
    ingest+thinking (init→first text) and generation (→total).

    turns/prompt_bytes added 2026-08-03: without them the resume and rotation
    populations cannot be separated after the fact, so there is no way to tell
    whether lowering SESSION_MAX_TURNS helped or cost anything. `turns` is the
    1-based index of THIS reply within its session; `prompt_bytes` is what we
    send locally, which on a resume is small by design - the transcript is
    replayed server-side and is not visible here."""
    t_end = time.time()
    boot = f"{t_init - t0:.1f}" if t_init else "?"
    first = f"{t_first - t0:.1f}" if t_first else "?"
    extra = ""
    if turns is not None:
        extra += f" turn={turns}"
    if prompt_bytes is not None:
        extra += f" prompt_bytes={prompt_bytes}"
    log(f"[timing] {path} model={model} session={mode} "
        f"boot={boot}s first_text={first}s total={t_end - t0:.1f}s{extra}")


def _turn_index(st) -> int:
    """1-based index of the reply about to be produced within its session. A new
    or stateless session is turn 1."""
    try:
        return int((st or {}).get("turns", 0)) + 1
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# Cancelling an in-flight run (bug #30, 17 Aug 2026)
# ---------------------------------------------------------------------------
#
# Kathryn said "2.5 hours" meaning ONE DAY. The bot read it as a week-wide
# constraint and set off replanning her whole week, and she had to sit and watch
# it. Coach turns measured 100 to 500s on 17 Aug (one totalled 526s), so "sit and
# watch it" is minutes of a wrong answer being produced with no way out.
#
# The killable thing is the subprocess, and the subprocess lives here, so this is
# where cancellation belongs - the transports only ever get to ASK. Killing the
# CLI closes its stdout, which ends the `for raw_line in proc.stdout` loop in
# _stream_once, so the read loop needs no polling and no timeout: the kill IS the
# signal.
#
# THE RACE THIS IS SHAPED AROUND. The cancel arrives on a different thread from
# the one streaming (bot.py runs replies on a ThreadPoolExecutor under a per-chat
# lock), and it can arrive LATE: the turn the athlete wanted stopped finishes on
# its own, a new turn for the same chat starts, and the cancel lands on the new
# one. Killing the athlete's fresh, CORRECT request silently is worse than the
# bug being fixed here. So the registry is keyed by a run id that is unique per
# run and NEVER reused, and cancel_run only ever acts on an exact key match:
# a cancel naming a run that has already finished finds nothing and is a no-op.
# There is deliberately no "cancel whatever is running for this chat" call, and
# there must never be one - that is precisely the API that kills the wrong run.
#
# WHAT THE FIRST CUT MISSED (17 Aug 2026, same day, second half of the fix). The
# Stop button shipped this morning killed the CLI and ONLY the CLI. proc.terminate()
# and proc.kill() signal one pid, and the coach's work does not all happen in that
# pid: the model holds a Bash tool and its own authority rule tells it to shell out
# to plan_distribution.py for calendar writes. Measured on the day: the parent went
# down with rc -15 and a Bash-spawned grandchild carried on and finished its work
# about five seconds later, unaffected. So a turn stopped mid-replan could still
# land the write, AFTER the athlete had been told "Stopped."
#
# The awareness machinery (snapshot diff, undo) exists because a kill can never be
# a guarantee. But it should have less to report on and less to undo than this. So
# the CLI is now spawned into a process group of its own and the signals go to the
# GROUP, which is the only handle the OS gives us on "that process and everything
# it spawned".

# Terminate first, and only reach for SIGKILL if the CLI is still there after
# this long. Polite first because the CLI flushes and tidies on SIGTERM; short
# because the athlete is waiting and has already told us they want out.
CANCEL_GRACE_S = 2.0

_RUNS = {}                       # run_id -> _Run, guarded by _RUNS_LOCK
_RUNS_LOCK = threading.Lock()


class _Run:
    """One in-flight generation. `cancelled` is sticky for the WHOLE turn, not
    just the current subprocess: stream_claude can spawn up to three processes
    (initial, dead-resume fallback, rate-limit fallback) and a cancelled turn
    must not roll into the next one. `proc` is the process currently running,
    or None between/outside spawns."""
    __slots__ = ("run_id", "owner", "cancelled", "proc")

    def __init__(self, run_id, owner=None):
        self.run_id = run_id
        self.owner = owner
        self.cancelled = False
        self.proc = None


def new_run_id() -> str:
    """A fresh run id. Unique and never reused - that property is the whole
    defence against a late cancel killing the next turn, so do not replace this
    with anything derived from the chat id."""
    return uuid.uuid4().hex


def _register_run(run_id, owner=None) -> _Run:
    run = _Run(run_id, owner)
    with _RUNS_LOCK:
        if run_id in _RUNS:
            # A caller reusing an id is a bug on their side, but the failure mode
            # matters: clobbering would leave the older run unkillable and point
            # the id at the newer one. Give the newcomer a fresh id instead, so
            # the worst case is a cancel that does nothing rather than a cancel
            # that stops the wrong turn.
            log(f"[cancel] run id {run_id} already in flight - reissuing")
            run.run_id = run_id = new_run_id()
        _RUNS[run_id] = run
    return run


def _deregister_run(run) -> None:
    """Remove the entry, but only if it is still OURS. Identity, not key, so a
    duplicate id can never make one run's teardown unregister another's."""
    if run is None:
        return
    with _RUNS_LOCK:
        if _RUNS.get(run.run_id) is run:
            del _RUNS[run.run_id]


def _signalable_group(proc):
    """The process group id it is SAFE to signal on `proc`'s behalf, or None when
    we must fall back to signalling the single process.

    The None cases are the whole reason this is a function rather than an inline
    os.getpgid(), and neither of them is about Windows - this repo is POSIX to the
    bone (systemd units, /usr/bin/claude, /root paths):

      * The group has already gone. getpgid raises ProcessLookupError and there is
        nothing to signal. Falling back is a no-op either way, but it must not be
        an exception out of a cancel path that promises never to raise.

      * THE GROUP IS OUR OWN. This is the dangerous one. If `proc` was spawned
        WITHOUT start_new_session it sits in the bot's own process group, and
        os.killpg on that group SIGTERMs the bot - the cancel would take down the
        service it was called from, and in the test suite it takes down the pytest
        runner rather than failing a test. _stream_once always passes
        start_new_session=True so this should never fire in production, but the
        cost of being wrong is the whole process, so it is checked every time
        rather than assumed. Do not remove this comparison.

    hasattr(os, "killpg") is then free insurance on top, not the motivation."""
    if not hasattr(os, "killpg") or not hasattr(os, "getpgid"):
        return None
    try:
        pgid = os.getpgid(proc.pid)
        if pgid <= 0 or pgid == os.getpgrp():
            return None
        return pgid
    except Exception:
        return None


def _signal_group(pgid, sig) -> None:
    """Signal a whole process group, swallowing the "it is already gone" cases.
    A cancel that raises would take its caller (a Telegram poll loop) down with
    it, and by the time we are here the athlete has already been told we stopped."""
    try:
        os.killpg(pgid, sig)
    except Exception:
        pass


def _group_is_alive(pgid) -> bool:
    """Is there still anything in this process group? Signal 0 asks the kernel
    without delivering anything.

    Deliberately NOT _signal_group with sig=0: that swallows every outcome and
    returns None, which is the right shape for "make this stop" and useless as a
    question. The polarity is worth spelling out because inverting it is silent -
    ProcessLookupError means the group is empty, PermissionError means something
    is in there that we are not allowed to signal (so: alive), and anything else
    we call gone rather than escalate against a group we cannot reason about."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _terminate_then_kill(proc, grace=None) -> None:
    """SIGTERM now, SIGKILL later if it is ignored. The escalation waits on a
    daemon thread because the caller is a Telegram poll loop or a web request:
    it must get its "stopping..." back immediately, not `grace` seconds later.

    Signals the process GROUP where there is one (17 Aug 2026). Signalling the pid
    alone left a Bash-spawned grandchild running: it finished its work five seconds
    after the parent died with rc -15, which for a replan means the calendar write
    lands after the athlete has been told "Stopped." See the section note above.

    The group id is resolved ONCE, here, synchronously, and the escalation thread
    closes over the integer. Resolving it inside the thread instead would be asking
    getpgid about a pid that proc.wait() may already have reaped, and a reaped pid
    can be recycled - the answer would be some unrelated process's group.

    WHY THE ESCALATION ASKS ABOUT THE GROUP AND NOT THE LEADER. Signalling a group
    is not a barrier: a member forked AFTER the killpg never receives it. Measured
    while building this, cancelling six milliseconds after the first line of output
    and watching the pids, 3 runs in 4: the shell died with rc -15 at t+0.05s and a
    child that appeared at t+0.053s, forked in the window between our SIGTERM and
    the shell acting on it, ran happily for its full 30 seconds. proc.wait() had
    returned cleanly, so an escalation keyed on the leader never fired. That window
    is exactly the moment a tool call starts, which is exactly the calendar write
    this is all trying to stop. So once the leader is gone (or the grace runs out),
    anything STILL in the group gets SIGKILL, with no second grace: it never saw
    the SIGTERM, and everything in that group is the cancelled turn's work.

    Residual, stated honestly: proc.wait() reaps the leader, so the pgid could in
    principle be recycled before the killpg. The window is the microseconds between
    wait() returning and the next line, and recycling would need a new process to
    take that pid AND make itself a session leader inside it."""
    if proc is None:
        return
    pgid = _signalable_group(proc)
    if pgid is None:
        try:
            proc.terminate()
        except Exception:
            pass
    else:
        _signal_group(pgid, signal.SIGTERM)

    def _escalate():
        timed_out = False
        try:
            proc.wait(timeout=CANCEL_GRACE_S if grace is None else grace)
        except Exception:
            # TimeoutExpired (still alive) or anything odd - either way we have
            # already asked nicely, so stop asking.
            timed_out = True
        if pgid is None:
            # No group to reason about, so the old rule stands unchanged: SIGKILL
            # only a process that ignored the SIGTERM.
            if timed_out:
                try:
                    proc.kill()
                except Exception:
                    pass
            return
        # Politeness is preserved by the liveness check, not by the return code:
        # a group that emptied itself on the SIGTERM is not there to be killed.
        if _group_is_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)
    threading.Thread(target=_escalate, daemon=True).start()


def cancel_run(run_id, owner=None, grace=None) -> bool:
    """Stop the in-flight run with EXACTLY this id. Returns True if a live run
    matched and has been told to stop, False otherwise.

    Safe to call from any thread, and safe to call late: a run that has already
    finished is gone from the registry, so this returns False and does nothing.
    Calling it twice for the same run is fine - the second call re-signals a
    process that is on its way out.

    `owner` is an optional second key (the chat id, say). When given it must
    equal the owner the run registered with or the cancel is refused; it is a
    belt-and-braces check on top of the unique id, not a substitute for it.
    There is no lookup BY owner on purpose (see the note above this section).

    Never raises - a cancel that fails must not take the caller down with it."""
    if not run_id:
        return False
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return False
        if owner is not None and run.owner != owner:
            log(f"[cancel] refused {run_id}: owner {owner!r} != {run.owner!r}")
            return False
        run.cancelled = True
        proc = run.proc          # may be None: the spawn has not happened yet
    # Outside the lock: terminate() is a syscall and the escalation spawns a
    # thread, neither of which should be holding up another chat's registration.
    _terminate_then_kill(proc, grace)
    log(f"[cancel] run {run_id} cancelled"
        + ("" if proc is not None else " before it spawned"))
    return True


def active_run_ids(owner=None) -> list:
    """DIAGNOSTICS ONLY - which runs are in flight, optionally for one owner.
    Do NOT use this to choose a run to cancel: reading it and then cancelling is
    exactly the two-step that lets a newly started turn be killed in place of the
    one the athlete meant. Cancel by the id you were given when you started the
    run."""
    with _RUNS_LOCK:
        return [rid for rid, run in _RUNS.items()
                if owner is None or run.owner == owner]


# ---------------------------------------------------------------------------
# Generation entry points
# ---------------------------------------------------------------------------

def scoped_env(sp_file):
    """Environment for a spawned claude, carrying CC_ATHLETE_SCOPE so lib/icu_fetch.py
    refuses calendar WRITES to any athlete but this one (reads are unrestricted), and
    CC_TURN_ID so lib/replan_gate.py can tell "a plan_tools load command ran earlier in
    THIS turn" from "ran at some other point entirely" (24 Aug 2026, the ad-hoc-replan
    gate — see replan_gate.py's docstring for why a prose rule was not enough).

    The slug comes from the system-prompt path — athletes/<slug>/system_prompt.txt — which
    is the one thing every spawn site already has and which cannot disagree with the
    athlete whose rules and history were just assembled into the prompt.

    Built PER SPAWN and passed as `env=`; os.environ is never mutated. bot.py serves three
    athletes from a ThreadPoolExecutor, so a process-global scope would race and could
    scope Jamie's turn to Kathryn. The same reasoning fixes CC_TURN_ID's lifetime: called
    ONCE per turn (call_claude/stream_claude), so a resume-fallback or model-fallback retry
    within the same turn reuses this same env and the same id, while the NEXT turn — even
    seconds later, even mid-replan — gets a fresh one and cannot be authorised by evidence
    left over from the turn before it."""
    slug = Path(sp_file).parent.name
    return {**os.environ, "CC_ATHLETE_SCOPE": slug, "CC_TURN_ID": uuid.uuid4().hex}


def _run_once(prompt, model, extra_args, cwd, timeout=300, env=None):
    """One non-streaming claude invocation with JSON output so the session id
    is capturable. Returns (text, session_id, returncode)."""
    r = subprocess.run(
        claude_cmd(model, ["--output-format", "json"] + extra_args),
        input=prompt,
        capture_output=True, text=True, cwd=cwd, timeout=timeout, env=env,
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
    env = scoped_env(sp_file)
    t0 = time.time()
    try:
        text, sid, rc = _run_once(prompt, model, extra, config["project_dir"], env=env)
        if (rc != 0 or not text) and mode == "resume":
            log(f"[session] resume failed rc={rc} — retrying with fresh session")
            _clear_session(sp_file)
            extra, prompt, mode, st = _plan_session(user_message, config, history,
                                                    sp_file, athlete_name, context)
            text, sid, rc = _run_once(prompt, model, extra, config["project_dir"], env=env)
        if _is_limit_message(text) and model != MODEL_SONNET:
            # Opus is primary now: a capped bucket must not surface a rate-limit
            # notice to the athlete while Sonnet 5 still has headroom, so fall
            # DOWN to Sonnet so the bot still answers.
            log(f"[limit] {model} capped - retrying on {MODEL_SONNET}")
            # env= on the fallback too: a rate-limited turn must not run unscoped.
            text, sid, rc = _run_once(prompt, MODEL_SONNET, extra, config["project_dir"],
                                      env=env)
            model = MODEL_SONNET
        # Read the turn index BEFORE _finish_session, which increments st["turns"]
        # IN PLACE - reading it afterwards logs the NEXT turn, not the one just served.
        turn_idx = _turn_index(st)
        if rc == 0 and text:
            _finish_session(sp_file, mode, st, sid)
        _log_timing("call", model, mode, t0, None, None,
                    turns=turn_idx, prompt_bytes=len(prompt or ""))
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


def _stream_once(prompt, model, extra_args, cwd, env=None, run=None):
    """One streaming claude invocation. Yields ('chunk', snapshot) and, when a
    tool_use block appears, ('status', tool_name, input_summary). Returns
    (final, streamed, session_id, returncode, t_init, t_first). Never raises —
    errors are logged and reported through the return tuple.

    `run` is the optional _Run handle for this turn (bug #30, 17 Aug 2026). It is
    how a cancel on another thread reaches THIS process: we publish the Popen
    onto it, and cancel_run terminates what it finds there. Passed explicitly
    rather than stashed on a thread-local because the only failure mode of an
    implicit hand-off is a cancel that silently does nothing, and this ships
    ahead of its own UI - nobody would notice until an athlete pressed stop and
    watched the wrong answer keep coming. The return contract is UNCHANGED:
    cancellation is reported through `run`, not through a seventh tuple slot."""
    streamed = ""
    final = None
    session_id = None
    t_init = t_first = None
    rc = -1
    try:
        # Cancel landed between registering the run and getting here (prompt
        # assembly reads a dozen files and can take a moment). Never spawn: a
        # process started after the athlete said stop is a process nobody is
        # waiting for.
        if run is not None and run.cancelled:
            return (final, streamed, session_id, rc, t_init, t_first)
        proc = subprocess.Popen(
            claude_cmd(model,
                       ["--output-format", "stream-json", "--verbose"] + extra_args),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE, text=True, cwd=cwd, env=env,
            # Its own session, so the CLI and everything it spawns share a process
            # group that is NOT the bot's (17 Aug 2026, second half of bug #30).
            # Without this there is no group to signal, and cancelling a turn kills
            # the CLI while a Bash-spawned grandchild carries on and finishes its
            # calendar write. See the cancellation section note.
            #
            # start_new_session=True and NOT preexec_fn=os.setsid. Same effect, but
            # preexec_fn runs in the child between fork and exec, and bot.py serves
            # three athletes off a ThreadPoolExecutor: forking a threaded process
            # and then taking a lock in the child is the classic way to hang it
            # forever. It is also already how this repo spawns detached processes
            # (telegram/bot.py, scripts/session-sync.py), so it is the local idiom.
            #
            # THE THING THIS CHANGES FOR AN UNCANCELLED RUN: the CLI no longer
            # receives signals sent to the bot's process group. Checked before
            # shipping - system/claudecoach-bot.service sets no KillMode, so
            # systemd's default control-group applies and systemctl stop/restart
            # (which is also all bot-watchdog.py does) still reaps the CLI through
            # the cgroup, which a process group does not escape. What it does
            # change is Ctrl-C on a bot run by hand in a terminal: that used to
            # reach the CLI through the foreground group and now will not.
            start_new_session=True,
        )
        if run is not None:
            # Publish and re-check under the SAME lock cancel_run takes. Without
            # the re-check there is a window: a cancel that read run.proc as None
            # a microsecond before this line would signal nothing and leave the
            # process it was meant to stop running to completion.
            with _RUNS_LOCK:
                run.proc = proc
                missed = run.cancelled
            if missed:
                _terminate_then_kill(proc)
        _feed_stdin(proc, prompt)
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
    finally:
        # Nothing in here may yield or raise: it runs on the ordinary path, on
        # the error path, and on GeneratorExit if the consumer walks away. A
        # stale proc reference would have a later cancel signalling a dead pid.
        if run is not None:
            with _RUNS_LOCK:
                run.proc = None
    return (final, streamed, session_id, rc, t_init, t_first)


def stream_claude(user_message, config, history, model=MODEL_OPUS,
                  system_prompt_file=None, athlete_name="Jamie", context="",
                  run_id=None, run_owner=None):
    """Generator over a streaming Claude run. Yields:
      ('chunk', snapshot)                 — growing reply to display live (transport throttles edits)
      ('status', tool_name, input_hint)   — a tool_use block appeared; transport may show a status line
      ('final', full)                     — authoritative full reply, emitted exactly once at the end
      ('cancelled', partial)              — the athlete stopped this run; emitted INSTEAD of ('final',...)
    Only ('chunk',...) and ('final',...) carry reply text; ('status',...) is purely
    for the live UI and must never be logged or treated as the reply.
    assistant events are full snapshots (replace); content_block_delta events are
    incremental (append); result replaces all. Transport-agnostic by design.

    CANCELLATION (bug #30, 17 Aug 2026). Pass `run_id` (from new_run_id(), or any
    id you know to be unique and never reused) and optionally `run_owner` (the
    chat id) to make this turn cancellable: another thread calls
    cancel_run(run_id, owner=run_owner) and the CLI is killed under us. The turn
    then ends with ('cancelled', partial) and NOT with ('final', ...). That
    distinction is the point of the event: a cancelled turn must not be posted to
    the athlete and must not be written to history, whereas a crash still ends in
    ('final', ...) and keeps its existing error handling. `partial` is whatever
    text had been generated when the kill landed - it is there for logging what
    was thrown away, never for display. A cancelled turn also skips
    _finish_session: the CLI session was killed part-way and its server-side
    state is not something to resume from.

    Pass no run_id and NOTHING changes: the run is still registered (so ops can
    see it) but no one can name it, and the event stream is exactly what it was."""
    run = _register_run(run_id or new_run_id(), run_owner)
    try:
        sp_file = Path(system_prompt_file) if system_prompt_file else SYSTEM_PROMPT_FILE
        extra, prompt, mode, st = _plan_session(user_message, config, history,
                                                sp_file, athlete_name, context)
        env = scoped_env(sp_file)
        t0 = time.time()
        final, streamed, sid, rc, t_init, t_first = yield from _stream_once(
            prompt, model, extra, config["project_dir"], env=env, run=run)

        # A dead resume fails before any text streams — fall back to a fresh session.
        # `not run.cancelled` first: a killed process looks EXACTLY like a dead
        # resume (non-zero rc, no text), so without this guard stopping a turn
        # would immediately spawn a second one and the athlete would watch the
        # wrong answer start over.
        if not run.cancelled and mode == "resume" and rc != 0 and not (final or streamed.strip()):
            log(f"[session] resume failed rc={rc} — falling back to fresh session")
            _clear_session(sp_file)
            extra, prompt, mode, st = _plan_session(user_message, config, history,
                                                    sp_file, athlete_name, context)
            final, streamed, sid, rc, t_init, t_first = yield from _stream_once(
                prompt, model, extra, config["project_dir"], env=env, run=run)

        text = (final if final is not None else streamed).strip()
        if not run.cancelled and _is_limit_message(text) and model != MODEL_SONNET:
            # Opus is primary now: fall DOWN to Sonnet 5 on a cap so the athlete
            # still gets an answer rather than a rate-limit notice. Same guard as
            # above - a cancelled turn does not get retried on another model.
            log(f"[limit] {model} capped - retrying on {MODEL_SONNET}")
            # env= on the fallback too: a rate-limited turn must not run unscoped.
            final, streamed, sid, rc, t_init, t_first = yield from _stream_once(
                prompt, MODEL_SONNET, extra, config["project_dir"], env=env, run=run)
            text = (final if final is not None else streamed).strip()
            model = MODEL_SONNET

        # Before _finish_session: it increments st["turns"] in place (see call_claude).
        turn_idx = _turn_index(st)
        # `not run.cancelled` is explicit rather than leaning on rc: a kill that
        # lands in the last moments of a turn can still find rc==0 and a complete
        # answer, and the athlete asked for that answer to be dropped. The cost is
        # a session whose turn counter did not advance, which only means it
        # rotates a turn early.
        if rc == 0 and text and not run.cancelled:
            _finish_session(sp_file, mode, st, sid)
        _log_timing("stream", model, mode, t0, t_init, t_first,
                    turns=turn_idx, prompt_bytes=len(prompt or ""))
        if run.cancelled:
            log(f"[cancel] run {run.run_id} stopped, {len(text)} chars discarded")
            yield ("cancelled", text)
            return
        yield ("final", text or "(no response)")
    finally:
        # finally, not a trailing call: the consumer breaks out of its loop on
        # ('final',...) rather than draining us, so the generator is CLOSED at a
        # yield and this is the only teardown that runs. It also covers the path
        # where _plan_session throws - an entry left behind would be a run nobody
        # can ever cancel and a slow leak in a process that runs for weeks.
        _deregister_run(run)


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
    illness_block = load_illness_block(sp_file, athlete_name)
    if illness_block:
        parts.append(illness_block)
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
            claude_cmd(model, ["--no-session-persistence"]),
            input=full_prompt,
            capture_output=True, text=True,
            cwd=config["project_dir"], timeout=300,
            env=scoped_env(sp_file),
        )
        _log_timing("image", model, "stateless", t0, None, None)
        return result.stdout.strip() or result.stderr.strip() or "(no response)"
    except subprocess.TimeoutExpired:
        return "Sorry, that took too long. Try a simpler question or break it into steps."
    except Exception as e:
        log(f"Claude image error: {e}")
        return f"Error calling claude: {e}"
