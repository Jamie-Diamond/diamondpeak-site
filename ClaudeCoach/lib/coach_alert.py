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
from datetime import datetime, time, timedelta
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
#
# Added 28 Jul 2026 — `cron` and `since`, the two fields that stop this table
# alarming about something that could not possibly have happened yet.
#
# `cron` — the deliverable's REAL crontab schedule. The gap check now asks "when
#   was this last DUE?" and judges the deliverable only once that moment has
#   passed. Without it, backup-config (23:50) was registered as "must appear
#   today" and checked by a digest that runs at 21:30 — 2h20m too early — so it
#   reported missing every night, in perpetuity. Declaring the schedule fixes
#   that for every deliverable at once instead of special-casing this one, and a
#   deliverable added later with an awkward time gets the same treatment free.
#   `cron_cmd` identifies its line in the live crontab: a test cross-checks the
#   declared schedule against the real one, so a WRONG declaration fails the
#   build rather than quietly mis-timing the check for ever.
#
# `since` — when this deliverable's heartbeat instrumentation went live (the
#   moment it landed on prod `main`, which IS the runtime here — NOT the moment
#   it was committed on a branch. The distinction is load-bearing: 83bb541 was
#   committed 11:37 and merged 11:44, and the session-sync cron launched at 11:40
#   in between, so it ran the OLD uninstrumented code and left no heartbeat. A
#   `since` of 11:37 called that a genuine gap; 11:44 correctly calls it
#   pre-instrumentation. For the older deliverables it is the first heartbeat on
#   disk). An
#   occurrence scheduled BEFORE that moment cannot have left a heartbeat, so it
#   is not judged. Without it stage1-plan — instrumented 28 Jul 11:44 — was
#   measured over a 7-day window reaching back to its Sunday 26 Jul run, two days
#   before any heartbeat could exist, and alarmed about a job that never had the
#   chance to report. FIVE of the ten deliverables below were instrumented on
#   28 Jul and every one of them had this fault (night-before-brief,
#   capture-reminder, session-sync, backup-config, stage1-plan — zero entries
#   between them in 1075 lines of run-status history), not just the one spotted.
#   The alternative fix — seeding heartbeats into run-status.jsonl — was rejected:
#   that file is the audit trail, and writing runs into it that never happened
#   corrupts the only record of what the system actually did.
DELIVERABLES = [
    {"script": "morning-checkin",    "label": "morning card",       "window": "daily",
     "per_athlete": True,  "telegram": True,  "detail": "card sent",
     "cron": "*/30 6-9 * * *",   "cron_cmd": "morning-checkin.py",
     "since": "2026-06-10T00:00:00"},
    {"script": "daily-prescription", "label": "daily prescription", "window": "daily",
     "per_athlete": True,  "telegram": True,
     "cron": "0 5 * * *",        "cron_cmd": "daily-prescription.py",
     "since": "2026-06-10T00:00:00"},
    {"script": "night-before-brief", "label": "night-before brief", "window": "daily",
     "per_athlete": True,  "telegram": True,
     "cron": "30 20 * * *",      "cron_cmd": "night-before-brief.py",
     "since": "2026-07-28T11:44:37"},   # ff6fba3
    {"script": "evening-checkin",    "label": "evening check-in",   "window": "daily",
     "per_athlete": True,  "telegram": True,
     "cron": "0 21 * * *",       "cron_cmd": "evening-checkin.py",
     "since": "2026-06-10T00:00:00"},
    # Internal plumbing and nudges — a gap is worth a digest line, not a message.
    # capture-reminder was DEREGISTERED here on 28 Jul 2026 (commit 2ba93c0, "one
    # evening message"). Its 20:10 cron entry is gone, capture-reminder.py's main()
    # is now a deliberate no-op, and the capture ask is Case A2 of the 21:00
    # evening-checkin. Left registered it would have produced "⚠ no successful
    # capture reminder for <athlete> today" every night for ever — a permanent
    # false gap line, which is the same defect this change exists to remove.
    #
    # Nothing is lost by removing it rather than re-pointing it at
    # evening-checkin's 21:00 slot: evening-checkin is ALREADY a deliverable on
    # that exact schedule, and it is telegram=True where capture-reminder was
    # telegram=False — so the work is monitored more strictly than before, not
    # less. Re-pointing would instead create two deliverables that can only ever
    # succeed or fail together: one root cause, two digest lines, which is the
    # anti-pattern argued against on stage1-plan's per_athlete note above.
    #
    # evening-checkin's _record() still writes a vestigial "capture-reminder"
    # heartbeat (it was added so this registry entry would not gap before it could
    # be removed). Nothing reads it now, and it is harmless: capture-reminder stays
    # in OUTCOME_CLASS below, so if one ever arrives ok=False it is classified as a
    # FAILURE rather than surfacing as an UNCLASSIFIED digest line.
    # session-sync's LAST occurrence of the day (21:40) is after the digest, which
    # looks like backup-config's fault and is not: earlier occurrences the same day
    # (07:40 onwards) are already behind us at 21:30, so last_due() finds 19:40 and
    # the day's heartbeat is genuinely expected. Only a job whose FIRST occurrence
    # of the cycle is still ahead is un-judgeable.
    {"script": "session-sync",       "label": "session sync",       "window": "daily",
     "per_athlete": True,  "telegram": False,
     "cron": "40 7-22/2 * * *",   "cron_cmd": "session-sync.py",
     "since": "2026-07-28T11:44:37"},   # ff6fba3
    {"script": "watchdog",           "label": "watchdog",           "window": "daily",
     "per_athlete": False, "telegram": False,
     "cron": "30 5 * * *",       "cron_cmd": "watchdog.py",
     "since": "2026-06-10T00:00:00"},
    # 28 Jul 2026: the only backup of config/athletes.json (intervals.icu API
    # keys), nightly at 23:50 via lib_git_alert.sh's git_sync_ok/git_sync_fail —
    # the job label there IS "backup-config" (checked in backup-config.sh, not
    # assumed). Heartbeat comes from ops_log.sync_ok/sync_failure, not a direct
    # record_run() call in the script itself; sync_ok now also writes the
    # success heartbeat (see its docstring) so a clean run doesn't read as a gap.
    #
    # 23:50 is AFTER the 21:30 digest, so this is the deliverable that proved the
    # gap check needed a due time at all. It is now judged on its PREVIOUS cycle:
    # at 21:30 the last due moment is last night's 23:50, so a genuinely failed
    # backup is caught within 24 hours rather than never. Not rescheduled: moving
    # the cron entry is the special case, would edit a script owned by other work,
    # and the next late deliverable would reintroduce the bug.
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
     "per_athlete": False, "telegram": True, "detail": "sync ok",
     "cron": "50 23 * * *",      "cron_cmd": "backup-config.sh",
     "since": "2026-07-28T13:00:37"},   # 5eaae85
    # Sunday jobs. Checked over 7 days. telegram=True since 28 Jul 2026, but NOT
    # via this per-day cooldown — ops-digest.py's weekly_alerts() sends these on
    # its own occurrence-based key so one miss is one message, not one per evening.
    {"script": "weekly-summary",     "label": "weekly summary",     "window": "weekly",
     "per_athlete": True,  "telegram": True,
     "cron": "0 20 * * 0",       "cron_cmd": "weekly-summary.sh",
     "since": "2026-07-12T00:00:00"},
    # per_athlete stays True even though one crashed Sunday build is one root
    # cause: weekly_alerts() already collapses it to ONE message that NAMES the
    # affected athletes, so the cost is a more useful message body, not three
    # messages. weekly-plan.sh invokes stage1-plan.py once per athlete, so a
    # single-athlete failure is a real shape that per_athlete=False would hide.
    {"script": "stage1-plan",        "label": "weekly plan",        "window": "weekly",
     "per_athlete": True,  "telegram": True,
     "cron": "0 18 * * 0",       "cron_cmd": "weekly-plan.sh",
     "since": "2026-07-28T11:44:37"},   # ff6fba3
]

# The digest's own schedule. Declared so the crontab cross-check can prove what
# this whole mechanism assumes: that "now" when the check runs is 21:30.
DIGEST_CRON = "30 21 * * *"


# --- when was a deliverable last DUE? ----------------------------------------
#
# THE FAULT THIS KILLS. The gap check asked "is there a heartbeat in this
# window" and never "could there be one yet". A deliverable whose scheduled time
# falls after the digest's answers no structurally, for ever, so a brand-new
# alarm cried wolf on night one about a job that was working fine.
#
# HOW A BAD TIME ADDED LATER CANNOT REPEAT IT. Three things, in order of
# strength. (1) Expectation is DERIVED from the declared schedule, so any time —
# 23:50, 03:00, Sunday-only — is handled by the same code path; there is no
# "correct" time to get wrong. (2) parse_cron RAISES on a spec it cannot read
# rather than guessing, and a test parses every entry, so an unreadable schedule
# fails the build. (3) A test cross-checks each declared `cron` against the LIVE
# crontab, so declaring 05:00 for a job that actually runs at 23:50 also fails
# the build. The remaining hole — registering a deliverable with no crontab entry
# at all — is closed by (3) too: no matching crontab line is a failure.

GRACE_MIN = 10   # an occurrence younger than this may still be running


def _cron_field(spec: str, lo: int, hi: int) -> set:
    """One cron field -> the set of values it matches.

    Supports `*`, `n`, `a-b`, `*/n`, `a-b/n` and comma lists of those — which
    covers every entry in this crontab. Anything else RAISES rather than
    guessing: a schedule this cannot read must fail the test suite, not silently
    produce a wrong due time and a nightly false alarm.
    """
    out = set()
    for part in str(spec).split(","):
        body, step = part, 1
        if "/" in part:
            body, _, s = part.partition("/")
            if not s.isdigit() or int(s) < 1:
                raise ValueError(f"unsupported cron step {part!r}")
            step = int(s)
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            a, _, b = body.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"unsupported cron range {part!r}")
            start, end = int(a), int(b)
        elif body.isdigit():
            start = int(body)
            end = hi if step > 1 else start   # vixie: `5/2` means `5-max/2`
        else:
            raise ValueError(f"unsupported cron field {part!r}")
        if not (lo <= start <= end <= hi):
            raise ValueError(f"cron field {part!r} outside {lo}-{hi}")
        out.update(range(start, end + 1, step))
    if not out:
        raise ValueError(f"cron field {spec!r} matches nothing")
    return out


def parse_cron(spec: str) -> dict:
    """{"minute", "hour", "dow"} as value sets.

    Raises on day-of-month / month restrictions: nothing here uses them and they
    would need a different last-occurrence walk, so accepting them silently would
    be the same class of bug this file is fixing.
    """
    fields = str(spec).split()
    if len(fields) != 5:
        raise ValueError(f"cron spec needs 5 fields, got {spec!r}")
    minute, hour, dom, month, dow = fields
    if (dom, month) != ("*", "*"):
        raise ValueError(f"day-of-month/month restrictions unsupported: {spec!r}")
    return {"minute": _cron_field(minute, 0, 59),
            "hour": _cron_field(hour, 0, 23),
            # cron allows both 0 and 7 for Sunday; normalise to 0.
            "dow": {0 if d == 7 else d for d in _cron_field(dow, 0, 7)}}


def last_due(spec: str, now: datetime):
    """The most recent scheduled occurrence at or before `now - GRACE_MIN`.

    The grace margin means a job scheduled a few minutes before the digest and
    still running is not reported missing — evening-checkin at 21:00 has the
    tightest real margin (30 min) and historically finishes in ~2 min, but the
    margin should not depend on that. None if nothing is scheduled in the
    preceding 8 days, which covers the weekly jobs.
    """
    cal = parse_cron(spec)
    cutoff = now - timedelta(minutes=GRACE_MIN)
    hours = sorted(cal["hour"], reverse=True)
    minutes = sorted(cal["minute"], reverse=True)
    for back in range(8):
        day = (cutoff - timedelta(days=back)).date()
        if (day.weekday() + 1) % 7 not in cal["dow"]:   # python Mon=0, cron Sun=0
            continue
        for h in hours:
            for m in minutes:
                moment = datetime.combine(day, time(h, m))
                if moment <= cutoff:
                    return moment
    return None


DUE                 = "due"
NOT_SCHEDULED_YET   = "not-scheduled-yet"
PRE_INSTRUMENTATION = "pre-instrumentation"


def due_status(d: dict, now: datetime = None) -> tuple:
    """(due_moment, state) for one deliverable — may it be judged right now?

    Only DUE may produce a gap line or a Telegram. The other two states mean the
    absence of a heartbeat is EXPECTED, not a failure:

      NOT_SCHEDULED_YET   — no occurrence of this schedule lies behind us at all.
      PRE_INSTRUMENTATION — the last occurrence predates `since`, so no heartbeat
                            could exist for it. This ages out by itself: the
                            first occurrence after `since` is judged normally,
                            with no seeded heartbeats and no edit to the audit
                            trail. It is visible while it lasts — ops-digest
                            prints an "ℹ not judged yet" line naming the reason,
                            so a deliverable is never silently unchecked.
    """
    now = now or datetime.now()
    due = last_due(d["cron"], now)
    if due is None:
        return None, NOT_SCHEDULED_YET
    if due < datetime.fromisoformat(d["since"]):
        return due, PRE_INSTRUMENTATION
    return due, DUE

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
