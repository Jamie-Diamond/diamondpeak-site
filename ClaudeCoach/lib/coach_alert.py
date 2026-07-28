"""The ONLY place ClaudeCoach is allowed to interrupt the coach on Telegram.

Everything else in this codebase is log-only (ops_log.alert / log_outbound), and
that must stay true: ops_log.alert() is called from a dozen places and gains no
notify path, so nothing can start Telegramming by accident. If a new condition
should reach Jamie, it gets an entry in REASONS here and a call to send() —
there is no second route to read.

Jamie's decision, 28 Jul 2026: exactly TWO conditions may interrupt him.

  1. DELIVERABLE_MISSING — a named deliverable (daily OR weekly) did not happen.
  2. CLAUDE_AUTH_FAILED  — the Claude CLI could not authenticate in production.

Deliberately NOT here, and log-only as a result:
  - git sync stuck (commits piling up locally). It was the one pre-existing
    Telegram path; it is removed because it is not one of the two. The 24-27 Jul
    push failures now surface as a digest ✗ line via the new failures-in-window
    rule in ops_log, not as a message. Flipping it back is one REASONS entry.

Changed 28 Jul 2026: the WEEKLY deliverables (weekly summary, weekly plan) are
now routed to Telegram too — the owner approved this after a weekly-summary
build crashed silently for three weeks with nothing but a log line to show for
it. Both now carry telegram=True below. Because the weekly gap check reads a
7-day window, a single Sunday miss stays "missing" for up to a week; routing it
through the same per-day key as the daily check would Telegram every evening
until it is fixed (up to 7 messages). ops-digest.py's weekly_alerts() sends
these separately from the daily path, on a stable per-script key with a cooldown
that spans the whole window, and explicitly clears that cooldown the moment the
deliverable is seen again — so one miss is one message, and the FOLLOWING
week's occurrence isn't silenced by a leftover cooldown.

Environment:
  CC_COACH_CHAT_ID  — where coach alerts go. Unset = notify.py's config default.
  CC_ALERT_DRY_RUN  — set to 1 and nothing is sent; the rendered text is written
                      to ops-alerts.log via log_outbound(sent=False) instead.
                      This is how the alert path is exercised without messaging
                      a real Telegram thread.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import ops_log

# --- the routing table -------------------------------------------------------

DELIVERABLE_MISSING = "deliverable_missing"
CLAUDE_AUTH_FAILED  = "claude_auth_failed"

# reason -> how loud, and how often at most. Cooldown exists because auth
# failures recur on EVERY claude_call (many per hour) and a missing deliverable
# is re-detected by every digest run until it is fixed; the coach needs the fact
# once, not once per occurrence.
REASONS = {
    DELIVERABLE_MISSING: {"cooldown_h": 12},
    CLAUDE_AUTH_FAILED:  {"cooldown_h": 6},
}

# Named deliverables the gap-check watches. `telegram` is the routing decision
# and lives here rather than in ops-digest so one file answers "what can reach
# Jamie". `window`: daily = must appear today; weekly = must appear in 7 days.
# `detail` (optional) narrows what counts as the heartbeat.
DELIVERABLES = [
    {"script": "morning-checkin",    "label": "morning card",       "window": "daily",
     "per_athlete": True,  "telegram": True,  "detail": "card sent"},
    {"script": "daily-prescription", "label": "daily prescription", "window": "daily",
     "per_athlete": True,  "telegram": True},
    {"script": "night-before-brief", "label": "night-before brief", "window": "daily",
     "per_athlete": True,  "telegram": True},
    {"script": "evening-checkin",    "label": "evening check-in",   "window": "daily",
     "per_athlete": True,  "telegram": True},
    # Internal plumbing and nudges — a gap is worth a digest line, not a message.
    {"script": "capture-reminder",   "label": "capture reminder",   "window": "daily",
     "per_athlete": True,  "telegram": False},
    {"script": "session-sync",       "label": "session sync",       "window": "daily",
     "per_athlete": True,  "telegram": False},
    {"script": "watchdog",           "label": "watchdog",           "window": "daily",
     "per_athlete": False, "telegram": False},
    # Sunday jobs. Checked over 7 days. telegram=True since 28 Jul 2026, but NOT
    # via this per-day cooldown — ops-digest.py's weekly_alerts() sends these on
    # its own occurrence-based key so one miss is one message, not one per evening.
    {"script": "weekly-summary",     "label": "weekly summary",     "window": "weekly",
     "per_athlete": True,  "telegram": True},
    {"script": "stage1-plan",        "label": "weekly plan",        "window": "weekly",
     "per_athlete": True,  "telegram": True},
]

STATE = ops_log.LOG_DIR / "coach-alert-state.json"


def dry_run() -> bool:
    return os.environ.get("CC_ALERT_DRY_RUN", "") not in ("", "0", "false", "False")


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    except Exception:
        pass


def _cooling_down(reason: str, key: str, now: datetime, cooldown_h: float = None) -> bool:
    hours = REASONS[reason]["cooldown_h"] if cooldown_h is None else cooldown_h
    last = _read_state().get(f"{reason}|{key}")
    if not last:
        return False
    try:
        return datetime.fromisoformat(last) > now - timedelta(hours=hours)
    except Exception:
        return False


def clear_cooldown(reason: str, key: str) -> None:
    """Forget a past send for this key. Used when an occurrence resolves (the
    deliverable is seen again), so the NEXT genuinely new occurrence of the same
    key alerts immediately instead of waiting out a cooldown banked for a miss
    that has since been fixed."""
    state = _read_state()
    if state.pop(f"{reason}|{key}", None) is not None:
        _write_state(state)


def send(reason: str, text: str, key: str = "", cooldown_h: float = None) -> str:
    """Interrupt the coach — if and only if `reason` is one of the two approved.

    `cooldown_h` overrides REASONS[reason]["cooldown_h"] for this call only —
    used by ops-digest.py's weekly_alerts() to bank a cooldown that spans the
    whole 7-day weekly window, rather than the shorter default tuned for the
    daily/auth-failure cases.

    Returns the action taken: "sent", "dry-run", "cooldown", "send-failed", or
    "refused" (an unlisted reason: logged loudly and NOT sent, so a future caller
    adding a third condition finds out in the log instead of silently messaging him).
    """
    now = datetime.now()
    if reason not in REASONS:
        ops_log.alert("coach-alert", f"REFUSED to Telegram an unapproved reason "
                                     f"{reason!r} — log-only. Text: {text}")
        return "refused"
    if _cooling_down(reason, key, now, cooldown_h):
        ops_log.log_outbound(f"coach-alert:{reason}", text, sent=False)
        return "cooldown"

    if dry_run():
        ops_log.log_outbound(f"coach-alert:{reason}", text, sent=False)
        return "dry-run"

    # Send FIRST, and only bank the cooldown if it actually went. Writing the
    # cooldown before the send would mean a Telegram API error silently burned the
    # next 6-12 hours of alerting — an alarm that fails quietly, which is the exact
    # failure class this whole file exists to kill.
    try:
        notify = Path(__file__).resolve().parent.parent / "telegram" / "notify.py"
        cmd = [sys.executable, str(notify), "--no-history"]
        chat = os.environ.get("CC_COACH_CHAT_ID", "")
        if chat:
            cmd += ["--chat-id", chat]
        cmd.append(text)
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        rc, err = r.returncode, (getattr(r, "stderr", b"") or b"")
    except Exception as exc:
        rc, err = -1, str(exc).encode()

    if rc != 0:
        # No cooldown banked: the next digest/claude_call tick tries again.
        ops_log.log_outbound(f"coach-alert:{reason}", text, sent=False)
        tail = err.decode(errors="replace")[-200:] if isinstance(err, bytes) else str(err)[-200:]
        ops_log.alert("coach-alert", f"notify FAILED (rc={rc}) for {reason}, "
                                     f"coach NOT told, will retry next tick: {tail}")
        return "send-failed"

    state = _read_state()
    state[f"{reason}|{key}"] = now.isoformat(timespec="seconds")
    _write_state(state)
    ops_log.log_outbound(f"coach-alert:{reason}", text, sent=True)
    return "sent"
