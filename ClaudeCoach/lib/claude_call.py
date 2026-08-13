#!/usr/bin/env python3
"""
Shared Claude CLI invocation with automatic model fallback.

All ClaudeCoach scripts should call run_claude() instead of spawning the
`claude` CLI directly. When a model's usage limit is hit (the CLI prints
"You've hit your limit · resets ..."), run_claude transparently retries the
next model in the fallback chain, so a single capped tier can't silently kill
the automation.

Why: the Max subscription meters usage PER TIER. The Sonnet-only weekly bucket
is small and exhausts well before the shared all-models pool, so Sonnet
automation dies while the Opus interactive bot keeps working. Fallback
Sonnet -> Haiku keeps cheap/frequent jobs alive without draining the all-models
pool the interactive bot relies on; quality-critical, low-frequency jobs pass
fallback=[OPUS] and fall Sonnet -> Opus instead.
"""
import os
import re
import subprocess
import sys
import time

CLAUDE = "/usr/bin/claude"

# Model ids (keep in step with telegram/bot.py)
OPUS   = "claude-opus-5"
SONNET = "claude-sonnet-5"
HAIKU  = "claude-haiku-4-5-20251001"

# Default fallback chains keyed by the starting model. Cheap/frequent automation
# falls to Haiku (preserves the shared all-models pool the Opus chat needs);
# quality-critical callers override with fallback=[OPUS].
DEFAULT_FALLBACK = {
    SONNET: [HAIKU],
    HAIKU:  [SONNET],
    OPUS:   [SONNET, HAIKU],
}

# Substrings that mean "this tier is rate-limited / unavailable", not real output.
_LIMIT_RE = re.compile(
    r"hit your (usage )?limit|usage limit|limit reached|"
    r"resets (in |at |on |\w{3} )|rate.?limit|"
    r"\boverloaded\b|too many requests|\b429\b|\b529\b",
    re.I,
)

# Substrings that mean the CLI could not AUTHENTICATE (dead/expired/absent
# CLAUDE_CODE_OAUTH_TOKEN). NOT a rate limit, so is_limit_message misses it
# and no fallback helps - every model shares this auth. Phrases only, never a
# bare "401": a plan JSON with "load": 401 (a legit TSS value) must not be
# read as an auth failure. The CLI always prefixes real auth errors with
# "Failed to authenticate", so phrases catch expired + invalid-credential cases.
_AUTH_RE = re.compile(
    r"failed to authenticate|authentication_error|"
    r"invalid authentication credentials|oauth access token has expired|"
    r"re-authenticate|not authenticated",
    re.I,
)

# Soft dependency: ops_log lives in the same lib/ dir. Importable whenever a
# caller has already put lib/ on sys.path (every script does). Optional so the
# helper has zero hard deps.
try:
    import ops_log as _ops_log
except Exception:
    _ops_log = None


def is_limit_message(text: str) -> bool:
    """True if `text` looks like a rate-limit / overload notice rather than a
    real model answer. Only short outputs are considered, so a long genuine
    answer that happens to mention 'rate limit' is never misclassified."""
    if not text:
        return False
    t = text.strip()
    if len(t) > 400:
        return False
    return bool(_LIMIT_RE.search(t))


def is_auth_failure(text: str) -> bool:
    """True if `text` looks like an authentication failure (dead/expired/
    absent CLAUDE_CODE_OAUTH_TOKEN) rather than a real answer. Short-output
    guard (< 600 chars) so a long genuine answer mentioning these words is
    never misclassified."""
    if not text:
        return False
    t = text.strip()
    if len(t) > 600:
        return False
    return bool(_AUTH_RE.search(t))


def auth_failure_kind() -> str:
    """WHY the CLI could not authenticate — and it matters, because the two cases
    need opposite responses and used to produce an identical alert.

      "token-expired"  : CLAUDE_CODE_OAUTH_TOKEN IS set and was rejected, in a
                         non-interactive run. This is the production outage —
                         every Claude-authored surface (morning card, brief,
                         check-in, debrief, weekly card) is down at once.
      "no-token"       : the variable is absent/empty. The run simply wasn't
                         wrapped in /root/.claude/cc-run. Cron always is, so this
                         is a hand-run, not an outage.
      "interactive"    : a token exists but stdin is a tty — someone is sitting
                         at a terminal and can read the error themselves.

    Provenance, not wording, is what separates them: cc-run only execs the
    command (no tty allocated), so cron runs are always non-interactive and
    always carry the token.
    """
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        return "no-token"
    try:
        if sys.stdin.isatty() or sys.stderr.isatty():
            return "interactive"
    except Exception:
        pass
    return "token-expired"


def _report_auth_failure(model: str, label: str = "") -> str:
    """Log the auth failure at a severity matching its kind, and route ONLY the
    production case to Telegram (via the one approved path). Returns the kind."""
    kind = auth_failure_kind()
    if kind == "token-expired":
        msg = (f"PRODUCTION AUTH OUTAGE: CLI authentication FAILED on {model} with a"
               f" CLAUDE_CODE_OAUTH_TOKEN present, non-interactively — the token has"
               f" expired or been revoked. No fallback possible; every model shares"
               f" this auth, so every Claude-authored message is down. Refresh"
               f" /root/.claude/cc.env.")
    elif kind == "no-token":
        msg = (f"auth failed on {model} with NO CLAUDE_CODE_OAUTH_TOKEN in the"
               f" environment — this is a hand-run outside /root/.claude/cc-run, not"
               f" a production outage. Re-run via cc-run. Not escalated.")
    else:
        msg = (f"auth failed on {model} in an INTERACTIVE session — you are at a"
               f" terminal and can see this; production cron is unaffected by"
               f" definition. Not escalated.")
    print(f"[{label or '?'}] AUTH-FAIL ({kind}): {msg}", file=sys.stderr, flush=True)
    if _ops_log:
        try:
            if kind == "token-expired":
                _ops_log.alert("claude_call", msg, athlete=label)
            else:
                # Not a failed run: nothing in production broke. Recorded so the
                # digest can still show it happened, without a ✗ or a gap.
                _ops_log.record_run("claude_call", athlete=label, ok=True, detail=msg)
        except Exception:
            pass
    if kind == "token-expired":
        try:
            import coach_alert
            coach_alert.send(
                coach_alert.CLAUDE_AUTH_FAILED,
                "⚠️ ClaudeCoach cannot reach Claude — the access token has expired, so "
                "no coaching messages can be written until it is refreshed. "
                "Nothing is lost; everything resumes once it is back.",
                key="token-expired")
        except Exception:
            pass
    return kind


class ClaudeResult:
    """Result of run_claude(). Mimics the bits of subprocess.CompletedProcess
    callers use (.stdout/.stderr/.returncode) plus fallback metadata."""

    __slots__ = ("stdout", "stderr", "returncode", "model", "fell_back", "limited", "auth_failed")

    def __init__(self, stdout, stderr, returncode, model, fell_back, limited,
                 auth_failed=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.model = model          # model that actually produced this result
        self.fell_back = fell_back  # True if a non-primary model was used
        self.limited = limited      # True if EVERY model in the chain was capped
        self.auth_failed = auth_failed  # True if the CLI could not authenticate


def run_claude(prompt, model=SONNET, *, fallback=None, allowed_tools=None,
               extra_args=None, cwd=None, timeout=300,
               no_session_persistence=True, stderr=None, label="", env=None):
    """Run `claude -p`, retrying down the fallback chain on a limit/overload.

    prompt              : the prompt, piped to the CLI on stdin (not argv)
    model               : starting model id
    fallback            : list of model ids to try after `model`; None => DEFAULT_FALLBACK
    allowed_tools       : value for --allowedTools (omit flag if None)
    extra_args          : extra CLI args (list), e.g. ["--output-format", "stream-json"]
    cwd, timeout        : passed to subprocess.run
    no_session_persistence : append --no-session-persistence (default True)
    stderr              : optional file object for stderr (else captured)
    label               : athlete/script label for ops_log alerts
    env                 : environment for the spawned CLI; None inherits this process's
                          (never pass {}). Callers serving one athlete should pass
                          {**os.environ, "CC_ATHLETE_SCOPE": slug} so lib/icu_fetch.py
                          refuses calendar writes to any other athlete. Per-spawn by
                          design — os.environ must not be mutated for this.

    Returns a ClaudeResult. On a fully-capped chain, returns the last (limited)
    result so callers can still inspect/relay it.
    """
    chain = [model] + (fallback if fallback is not None else list(DEFAULT_FALLBACK.get(model, [])))
    last = None

    for i, m in enumerate(chain):
        cmd = [CLAUDE, "-p", "--model", m]
        if no_session_persistence:
            cmd.append("--no-session-persistence")
        if allowed_tools is not None:
            cmd += ["--allowedTools", allowed_tools]
        if extra_args:
            cmd += list(extra_args)

        _t0 = time.monotonic()
        try:
            # The prompt goes on stdin, never argv: a single argv element is
            # capped at MAX_ARG_STRLEN (128 KiB on Linux) and exec() fails
            # outright with E2BIG once a generated prompt crosses it. stdin has
            # no such limit.
            r = subprocess.run(
                cmd,
                input=prompt,
                stdout=subprocess.PIPE,
                stderr=(stderr if stderr is not None else subprocess.PIPE),
                text=True, cwd=cwd, timeout=timeout, env=env,
            )
            out = r.stdout or ""
            err = r.stderr or ""
            rc = r.returncode
            # PER-ATTEMPT ELAPSED (timeout diagnosis): stage1 & co. previously logged nothing,
            # so per-attempt gen times were invisible. Emit to THIS process's stderr (captured
            # by every caller's cron/log redirect); size plan-gen timeouts from this, not guesses.
            print(f"[{label or '?'}] {m} finished in {time.monotonic() - _t0:.0f}s (rc={rc})",
                  file=sys.stderr, flush=True)
        except subprocess.TimeoutExpired:
            # A timeout is not a limit — surface it immediately, don't burn the chain.
            print(f"[{label or '?'}] {m} timed out at {time.monotonic() - _t0:.0f}s (limit {timeout}s)",
                  file=sys.stderr, flush=True)
            return ClaudeResult("", "timeout", -1, m, i > 0, False)
        except Exception as exc:
            last = ClaudeResult("", str(exc), 1, m, i > 0, False)
            continue

        limited = is_limit_message(out) or (rc != 0 and is_limit_message(err))
        res = ClaudeResult(out, err, rc, m, i > 0, limited)

        # AUTH FAILURE takes precedence over the limit/fallback logic: an
        # expired/invalid token fails identically on EVERY model, so falling
        # back is pointless and returning empty is dangerous (the caller reads
        # it as "model produced nothing" and silently skips the work). How loud
        # depends on WHY - see _report_auth_failure / auth_failure_kind.
        if is_auth_failure(out) or is_auth_failure(err):
            res.auth_failed = True
            _report_auth_failure(m, label)
            return res

        if limited and i < len(chain) - 1:
            nxt = chain[i + 1]
            if _ops_log:
                try:
                    _ops_log.record_run("claude_call", athlete=label, ok=True,
                                        detail=f"{m} capped -> falling back to {nxt}")
                except Exception:
                    pass
            last = res
            continue

        if limited and _ops_log:  # exhausted the whole chain
            try:
                _ops_log.alert("claude_call",
                               f"all models capped: {chain}", athlete=label)
            except Exception:
                pass
        return res

    return last
