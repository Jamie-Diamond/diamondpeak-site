#!/usr/bin/env python3
"""Which Claude account pays for a CLI call: Jamie's Max subscription or the business API key.

Why (16 Oct 2026): Jamie has $100/month of Claude API credit, and paying athletes need the
API key (a personal subscription cannot serve customers). Dev work costs ~$1,100/month at
API prices and must stay on Max. In `claude -p` the CLI ALWAYS uses ANTHROPIC_API_KEY over
CLAUDE_CODE_OAUTH_TOKEN (code.claude.com/docs/en/iam, "Authentication precedence"), so the
key must never go in cc.env, a unit's EnvironmentFile or os.environ - that would move every
dev session onto the credit and use it up in ~3 days. It is added per spawn, only to the
env of a call made for an athlete who is billed to it.

One key for the whole business, not one per athlete (Jamie, 16 Oct): a self sign-up needs
no action from him. Per-athlete cost comes from our own transcripts (scripts/usage-report.py).

Routing is EXPLICIT. A call goes on the key only when its caller names the athlete
(run_claude(athlete=...), engine.scoped_env, run(..., athlete=...)) and that athlete is
billed to it in config/claude-billing.json. Anything unnamed - dev work, bug fixes, shadow
runs, rule tidy, hand runs - stays on Max.

If the key is refused (credit used up, spend limit, revoked key), the call is retried once on
the subscription and the breaker trips for an hour, so later calls skip the key. One line
goes to the ops log and, at most once a day, to Jamie's Dev tab. An athlete never sees it.

Which account paid is not in the CLI's own transcripts (checked 16 Oct: a Max run and a key
run write identical rows), so every call that ran on the key is written to LEDGER: its
transcript session (pinned with --session-id where the call starts a session) and the time
window it ran in. lib/usage_ledger.py prices the transcript rows inside those windows as
the key's spend, for Peak's "API key this month" line. Everything else was Max.

    python3 ClaudeCoach/lib/claude_billing.py              # status: switch, key, who is on it
    python3 ClaudeCoach/lib/claude_billing.py --test-key   # one Haiku "Say OK" on the key
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG = BASE / "config" / "claude-billing.json"
KEY_FILE = Path("/etc/claudecoach/anthropic-api-key")
TRIP_FILE = Path.home() / "Library/Logs/ClaudeCoach" / "claude-billing-tripped.json"
LEDGER = Path.home() / "Library/Logs/ClaudeCoach" / "claude-billing.jsonl"
KEY_VAR = "ANTHROPIC_API_KEY"
TRIP_SECONDS = 3600          # skip the key this long after it is refused, then try again
TELL_EVERY = 24 * 3600       # Dev-tab note at most once a day while it keeps failing

# The CLI's own wording for a refused key (code.claude.com/docs/en/errors) plus the raw API
# error for a bad key. Checked BEFORE claude_call's limit and auth patterns: "spend limit
# reached" would otherwise read as a model cap and fall back to another model on the same
# dead key, and "Invalid API key" as an expired subscription token.
_BILLING_RE = re.compile(
    r"credit balance is too low|invalid api key|invalid x-api-key|"
    r"organization has been disabled|disabled api key authentication|"
    r"spend limit|shared budget",
    re.I,
)


def _config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


def monthly_credit() -> float | None:
    """The API credit Jamie gets each month, for Peak's "API key this month" line."""
    v = _config().get("monthly_credit_usd")
    return float(v) if v is not None else None


def _key() -> str:
    try:
        return KEY_FILE.read_text().strip()
    except Exception:
        return ""


def _trip_state() -> dict:
    try:
        return json.loads(TRIP_FILE.read_text())
    except Exception:
        return {}


def tripped() -> bool:
    """True for TRIP_SECONDS after the key was last refused."""
    return time.time() - float(_trip_state().get("at") or 0) < TRIP_SECONDS


def billed_to_key(athlete: str | None) -> bool:
    """Is this athlete's coaching meant to be on the API key? The switch and the lists
    only, not whether the key works right now: route() adds that."""
    if not athlete:
        return False
    cfg = _config()
    if not cfg.get("enabled"):
        return False
    if athlete in (cfg.get("subscription_athletes") or []):
        return False
    on_key = cfg.get("api_athletes") or []
    if on_key == "all":
        # A registered athlete only: "all" must never put a stray slug (a temp dir, a
        # dev label) on the key.
        return athlete in _athletes()
    return athlete in on_key


def _athletes() -> dict:
    try:
        return json.loads((BASE / "config" / "athletes.json").read_text())
    except Exception:
        return {}


def route(athlete: str | None) -> str:
    """'api' or 'subscription' for a call made for `athlete` (None = not an athlete's)."""
    if billed_to_key(athlete) and _key() and not tripped():
        return "api"
    return "subscription"


def env_for(athlete: str | None, env: dict | None = None) -> dict | None:
    """The env to spawn `claude` with. Never mutates `env` or os.environ.

    athlete None leaves `env` exactly as given (None still means "inherit"), so a call
    nested inside an athlete's chat turn bills where that turn bills. A named athlete gets
    a copy with the key added (route 'api') or removed (route 'subscription'), so Jamie's
    own coaching can never reach the key by inheritance."""
    if not athlete:
        return env
    out = dict(env if env is not None else os.environ)
    if route(athlete) == "api":
        out[KEY_VAR] = _key()
    else:
        out.pop(KEY_VAR, None)
    return out


def uses_key(env: dict | None) -> bool:
    return bool((env if env is not None else os.environ).get(KEY_VAR))


def failure_text(stdout: str) -> str:
    """The message to test: the `result` of `--output-format json` output, else the raw text."""
    s = (stdout or "").strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return str(d.get("result") or "")
        except Exception:
            pass
    return s


def is_billing_failure(text: str) -> bool:
    """True if `text` is the CLI refusing the key, not a model answer. Short outputs only,
    so a long reply that mentions credit or spend limits is never misread."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 600 and bool(_BILLING_RE.search(t))


def failed(env: dict | None, *texts: str) -> bool:
    """Did a call that ran on the key fail because of the key?"""
    return uses_key(env) and any(is_billing_failure(failure_text(t)) for t in texts if t)


def fall_back(env: dict | None, who: str, text: str) -> dict:
    """Trip the breaker and return `env` without the key, for the one retry on Max."""
    trip(who, text)
    out = dict(env if env is not None else os.environ)
    out.pop(KEY_VAR, None)
    return out


def trip(who: str, text: str) -> None:
    """Record a refused key. While the credit stays out the key is re-tried once an hour and
    refused again; that is one outage, not 24 a day, so it is logged when it starts and
    told to Jamie's Dev tab at most once a day. Never raises."""
    try:
        state = _trip_state()
        now = time.time()
        continuing = now - float(state.get("at") or 0) < 2 * TRIP_SECONDS
        reason = " ".join((failure_text(text) or "").split())[:200]
        state.update({"at": now, "since": state.get("since") if continuing else now,
                      "reason": reason, "who": who or ""})
        tell = now - float(state.get("told") or 0) >= TELL_EVERY
        if tell:
            state["told"] = now
        TRIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRIP_FILE.write_text(json.dumps(state))
        if continuing and not tell:
            return
        msg = (f"API key refused ({reason!r}) on a call for {who or '?'}: retried on the Max "
               f"subscription; every athlete stays on Max for the next "
               f"{TRIP_SECONDS // 60} min, then the key is tried again.")
        try:
            import ops_log
            ops_log.alert("claude_billing", msg, athlete=who or "")
        except Exception:
            pass
        if tell:
            _tell_jamie(reason)
    except Exception:
        pass


def _tell_jamie(reason: str) -> None:
    try:
        import outbox
        cid = json.loads((BASE / "config" / "athletes.json").read_text())["jamie"]["chat_id"]
        outbox.record(cid, f"*Claude API key refused:* {reason}\n\nAthletes are being coached "
                           "on your Max subscription instead, so nothing is missed. Top up "
                           "or check the key in the Claude Console.",
                      source="dev-session", tab="dev")
    except Exception:
        pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# Flags after which the CLI continues an existing session, or keeps none: --session-id
# cannot be added to those.
_NO_PIN = {"--resume", "-r", "--continue", "-c", "--session-id", "--no-session-persistence",
           "--fork-session"}


def pin(cmd: list) -> tuple[list, str | None]:
    """(cmd with a fresh --session-id, that id), so the ledger can name the transcript this
    call writes. Unchanged and None when the call resumes a session or keeps none."""
    if any(a in _NO_PIN for a in cmd):
        return cmd, None
    sid = str(uuid.uuid4())
    return list(cmd) + ["--session-id", sid], sid


def record(session: str | None, start: str, athlete: str = "", label: str = "",
           usd: float | None = None) -> None:
    """One call that ran on the key: its transcript session and the window it ran in
    (usage_ledger prices those rows). `usd` instead, for a call that keeps no transcript.
    Never raises."""
    if not session and usd is None:
        return
    entry = {"session": session, "start": start, "end": now(),
             "athlete": athlete or "", "label": label or ""}
    if usd is not None:
        entry["usd"] = round(float(usd or 0), 6)
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def ledger() -> list[dict]:
    try:
        lines = LEDGER.read_text().splitlines()
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def run(cmd: list, *, athlete: str | None = None, env: dict | None = None, label: str = "",
        **kwargs):
    """subprocess.run for a `claude -p` call, billed to `athlete`'s account, with the one
    retry on Max if the key is refused. Same return value as subprocess.run."""
    use = env_for(athlete, env)
    keyed = uses_key(use)
    run_cmd, sid = pin(cmd) if keyed else (cmd, None)
    t0 = now()
    try:
        r = subprocess.run(run_cmd, env=use, **kwargs)
    finally:
        if keyed:
            record(sid, t0, athlete or "", label)
    out = r.stdout if isinstance(r.stdout, str) else ""
    err = r.stderr if isinstance(r.stderr, str) else ""
    if failed(use, out, err):
        r = subprocess.run(cmd, env=fall_back(use, athlete or "", out or err), **kwargs)
    return r


def status() -> dict:
    cfg = _config()
    slugs = list(_athletes())
    on_key = cfg.get("api_athletes") or []
    return {
        "enabled": bool(cfg.get("enabled")),
        "key_file": str(KEY_FILE),
        "key_present": bool(_key()),
        "tripped": tripped(),
        "trip": _trip_state() or None,
        "routes": {s: route(s) for s in slugs},
        "on_key_once_enabled": [s for s in slugs
                                if s not in (cfg.get("subscription_athletes") or [])
                                and (on_key == "all" or s in on_key)],
    }


def test_key() -> int:
    """One tiny Haiku call on the key, whatever the switch says. Proves the key and the
    CLI's use of it before anyone is moved. Costs a fraction of a cent."""
    key = _key()
    if not key:
        print(f"no key in {KEY_FILE}")
        return 1
    env = dict(os.environ)
    env[KEY_VAR] = key
    r = subprocess.run(["/usr/bin/claude", "-p", "--model", "haiku", "--output-format", "json",
                        "--no-session-persistence"],
                       input="Reply with exactly: OK", capture_output=True, text=True,
                       timeout=120, env=env)
    try:
        d = json.loads(r.stdout)
    except Exception:
        print(f"rc={r.returncode} stdout={r.stdout[:300]!r} stderr={r.stderr[:300]!r}")
        return 1
    print(json.dumps({"rc": r.returncode, "result": d.get("result"),
                      "is_error": d.get("is_error"), "cost_usd": d.get("total_cost_usd")}))
    return 0 if r.returncode == 0 and not d.get("is_error") else 1


if __name__ == "__main__":
    sys.path.insert(0, str(BASE / "lib"))
    if "--test-key" in sys.argv:
        sys.exit(test_key())
    print(json.dumps(status(), indent=1, default=str))
