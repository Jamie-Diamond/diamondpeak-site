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
import re
import subprocess

CLAUDE = "/usr/bin/claude"

# Model ids (keep in step with telegram/bot.py)
OPUS   = "claude-opus-4-8"
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


class ClaudeResult:
    """Result of run_claude(). Mimics the bits of subprocess.CompletedProcess
    callers use (.stdout/.stderr/.returncode) plus fallback metadata."""

    __slots__ = ("stdout", "stderr", "returncode", "model", "fell_back", "limited")

    def __init__(self, stdout, stderr, returncode, model, fell_back, limited):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.model = model          # model that actually produced this result
        self.fell_back = fell_back  # True if a non-primary model was used
        self.limited = limited      # True if EVERY model in the chain was capped


def run_claude(prompt, model=SONNET, *, fallback=None, allowed_tools=None,
               extra_args=None, cwd=None, timeout=300,
               no_session_persistence=True, stderr=None, label=""):
    """Run `claude -p`, retrying down the fallback chain on a limit/overload.

    prompt              : the -p prompt string
    model               : starting model id
    fallback            : list of model ids to try after `model`; None => DEFAULT_FALLBACK
    allowed_tools       : value for --allowedTools (omit flag if None)
    extra_args          : extra CLI args (list), e.g. ["--output-format", "stream-json"]
    cwd, timeout        : passed to subprocess.run
    no_session_persistence : append --no-session-persistence (default True)
    stderr              : optional file object for stderr (else captured)
    label               : athlete/script label for ops_log alerts

    Returns a ClaudeResult. On a fully-capped chain, returns the last (limited)
    result so callers can still inspect/relay it.
    """
    chain = [model] + (fallback if fallback is not None else list(DEFAULT_FALLBACK.get(model, [])))
    last = None

    for i, m in enumerate(chain):
        cmd = [CLAUDE, "-p", prompt, "--model", m]
        if no_session_persistence:
            cmd.append("--no-session-persistence")
        if allowed_tools is not None:
            cmd += ["--allowedTools", allowed_tools]
        if extra_args:
            cmd += list(extra_args)

        try:
            r = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=(stderr if stderr is not None else subprocess.PIPE),
                stdin=subprocess.DEVNULL,  # `claude -p` reads piped stdin as prompt input
                text=True, cwd=cwd, timeout=timeout,
            )
            out = r.stdout or ""
            err = r.stderr or ""
            rc = r.returncode
        except subprocess.TimeoutExpired:
            # A timeout is not a limit — surface it immediately, don't burn the chain.
            return ClaudeResult("", "timeout", -1, m, i > 0, False)
        except Exception as exc:
            last = ClaudeResult("", str(exc), 1, m, i > 0, False)
            continue

        limited = is_limit_message(out) or (rc != 0 and is_limit_message(err))
        res = ClaudeResult(out, err, rc, m, i > 0, limited)

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
