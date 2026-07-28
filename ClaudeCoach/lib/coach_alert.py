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
    # 28 Jul 2026: the only backup of config/athletes.json (intervals.icu API
    # keys), nightly at 23:50 via lib_git_alert.sh's git_sync_ok/git_sync_fail —
    # the job label there IS "backup-config" (checked in backup-config.sh, not
    # assumed). Heartbeat comes from ops_log.sync_ok/sync_failure, not a direct
    # record_run() call in the script itself; sync_ok now also writes the
    # success heartbeat (see its docstring) so a clean run doesn't read as a gap.
    #
    # detail="sync ok" is LOAD-BEARING, not decoration. sync_failure's FIRST
    # consecutive failure records ok=True with detail "transient git-sync failure
    # (1st): ..." on the reasoning that the next tick will heal it. For the five
    # jobs that tick every 15-30 minutes that is sound; for a job that runs ONCE
    # at 23:50 the next tick is 24 hours away, so that ok=True would report a
    # night with NO backup as a clean run and make this whole registration
    # cosmetic. Pinning the detail to the exact string sync_ok writes (and only
    # sync_ok writes) means only a genuine success satisfies the check. Narrowing
    # here rather than changing sync_failure keeps the blast radius to this one
    # job instead of all five that share the helper.
    {"script": "backup-config",      "label": "config backup",      "window": "daily",
     "per_athlete": False, "telegram": True, "detail": "sync ok"},
    # Sunday jobs. Checked over 7 days. telegram=True since 28 Jul 2026, but NOT
    # via this per-day cooldown — ops-digest.py's weekly_alerts() sends these on
    # its own occurrence-based key so one miss is one message, not one per evening.
    {"script": "weekly-summary",     "label": "weekly summary",     "window": "weekly",
     "per_athlete": True,  "telegram": True},
    {"script": "stage1-plan",        "label": "weekly plan",        "window": "weekly",
     "per_athlete": True,  "telegram": True},
]

# --- is an ok=False heartbeat a FAILURE or a FINDING? ------------------------
#
# THE PROBLEM. ops-digest's gap check used to count ANY heartbeat as "it ran", so
# a job that failed and dutifully logged the failure produced no gap and no
# alert. Only a job that died before reaching ops_log tripped the alarm — the
# quiet failures stayed quiet, which is the whole class this alarm exists to kill.
#
# WHY IT IS NOT A ONE-LINE FIX. `ok=False` does not mean "failed". It means
# either "I failed" or "I ran fine and found something": weekly-summary wrote 8
# ok=False lines reading "realised TID excess_quality: realised easy share 46% vs
# target" — that is the script SUCCESSFULLY detecting training drift. Treat
# ok=False as failure naively and the new alarm fires on week one from a working
# script, which is how a new alarm gets ignored on week one.
#
# WHY A TABLE AND NOT A FIX AT THE CALL SITES. Two reasons, both hard:
#   1. The benign call site is ALREADY corrected. weekly-summary.py:250 records
#      ok=True ("training-balance note ready") as of 27 Jul 2026. The 8 ok=False
#      lines in run-status.jsonl are STALE DATA written by code that no longer
#      exists — and the 26 Jul pair is still inside the 7-day weekly window, so
#      history alone would fire this alarm on day one.
#   2. Every remaining benign ok=False producer lives in a file owned by other
#      concurrent work (plan_builder.py, weekly-summary.py, capture-reminder.py).
#      Correcting them here is not available.
# A read-side classification is therefore the only route, and it has the
# advantage of also classifying the six weeks of history correctly.
#
# WHY NOT A POSITIVE DELIVERY CHECK INSTEAD (the obvious alternative — extend the
# existing `"detail": "card sent"` mechanism to every deliverable): impossible for
# weekly-summary, which writes NO delivery heartbeat at all. Its only record_run
# is the drift note, so requiring a delivery detail would report it missing every
# single week. That gap is owned by another ticket; until it is closed, the
# absence of a failure is the only signal available for that script.
#
# THREE STATES, and what each does:
#   FAILURE       -> does NOT count as "it ran". Produces a gap line, and a
#                    Telegram if the deliverable is telegram=True.
#   FINDING       -> DOES count as "it ran". Digest ✗ line only, never alarms.
#   unclassified  -> does NOT alarm, and is NOT swallowed: ops-digest emits a
#                    loud "UNCLASSIFIED" digest line naming the script so the
#                    omission is visible, and test_ops_digest asserts every
#                    DELIVERABLES script is classified, so adding a monitored
#                    deliverable without classifying it fails the build.
#
# A NEW BENIGN CASE ADDED LATER. Two ways it stays safe. If it is added to a
# FINDING-class script it is benign by default. If it is added to a FAILURE-class
# script — the only genuinely dangerous case — the call site passes
# outcome=ops_log.FINDING and that wins over this table. That per-record override
# is why the table is a DEFAULT and not a verdict. A new script nobody classifies
# lands in `unclassified`: silent on Telegram, loud in the digest.
FAILURE = ops_log.FAILURE
FINDING = ops_log.FINDING

OUTCOME_CLASS = {
    # --- FAILURE: an ok=False line from these means the job did not do its job.
    # Every one verified against its call sites: the ok=False paths are Telegram
    # sends that failed after retry, unhandled exceptions, and claude CLI
    # non-zero exits (which is how a dead auth token hides).
    "morning-checkin":    FAILURE,
    "daily-prescription": FAILURE,
    "night-before-brief": FAILURE,   # "no <telegram> block in model output"
    "evening-checkin":    FAILURE,
    "capture-reminder":   FAILURE,   # "claude CLI exited N with no <notify> block"
    "session-sync":       FAILURE,
    "watchdog":           FAILURE,
    "backup-config":      FAILURE,   # via ops_log.sync_failure's non-transient branch
    "stage1-plan":        FAILURE,
    "bot-watchdog":       FAILURE,   # the bot stopped answering
    "claude_call":        FAILURE,   # auth expired in production
    "cc-gitpull":         FAILURE,
    "coach-alert":        FAILURE,   # the alarm itself failing to send
    # --- FINDING: an ok=False line from these is the script working correctly and
    # reporting something about the TRAINING or the CONFIG, not about itself.
    # None of these is a missed deliverable; all of them are already a digest line.
    "weekly-summary":     FINDING,   # realised-TID drift; run-threshold staleness
    "plan_builder":       FINDING,   # 316 of the 411 historical ok=False lines:
                                     # "weekly_tss_cap check SKIPPED — no cap supplied"
    "plan_audit":         FINDING,   # plan invariant broke, already baseline-gated
    "rules-lint":         FINDING,   # "rule may withhold Swim high work"
    "thresholds":         FINDING,   # "zones may be stale-high"
    "selftest":           FINDING,   # "loud-path verification (not a real failure)"
}


def classify(entry: dict) -> str:
    """FAILURE / FINDING / "unclassified" for one run-status entry.

    ok=True is never either — it is a clean run. Precedence: the record's own
    `outcome` field (a call site that knows), then OUTCOME_CLASS (this file's
    default per script), then "unclassified".
    """
    if entry.get("ok"):
        return ""
    stamped = entry.get("outcome")
    if stamped in (FAILURE, FINDING):
        return stamped
    return OUTCOME_CLASS.get(entry.get("script"), "unclassified")


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
