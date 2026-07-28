"""Operational logging — coach-facing alerts plus a run-status heartbeat.

Two append-only files under the existing log dir:
  ops-alerts.log   — human-readable lines for things a coach should see
  run-status.jsonl — one JSON object per script run/outcome; the evening
                     ops digest reads this to flag failures and gaps

Both are fail-soft: observability must never break the workflow it observes.

Ops chatter is LOG-ONLY from 27 Jul 2026. Engineering failures and the evening
digest no longer message the coach's Telegram thread — they land here, and the
full rendered text of anything that would have been sent is written verbatim by
log_outbound() so the log is a real debugging record rather than a summary of
one. From 28 Jul 2026 there is NO exception: sync_failure()'s stuck-sync
escalation lost its notify call too, because a stuck git sync is not one of the
two conditions Jamie allows to interrupt him. Telegram routing lives in exactly
one file now — lib/coach_alert.py. Nothing in here sends.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR    = Path.home() / "Library/Logs/ClaudeCoach"
ALERT_LOG  = LOG_DIR / "ops-alerts.log"
RUN_STATUS = LOG_DIR / "run-status.jsonl"
SYNC_STATE = LOG_DIR / "git-sync-state"

# RUN_STATUS was 2000 lines, which held ~4 weeks at the 12-40 entries/day this
# file was written for. The four newly-instrumented senders add ~30/day
# (session-sync alone is 8 runs x 3 athletes), and the gap check now reads a
# 7-DAY window for the Sunday jobs, so retention became correctness-critical
# rather than cosmetic. 6000 lines is >30 days at the new ~70/day, and still
# >20 days on the worst observed spike day (91 entries, 16 Jul).
_MAX_LINES = {ALERT_LOG: 5000, RUN_STATUS: 6000}

# A git sync that fails once and heals itself on the next tick is not news: over
# 23-27 Jul every single instant alert was of that kind. Alert from the SECOND
# consecutive failure.
ALERT_AFTER = 2

# Escalation used to be ESCALATE_AFTER=6 CONSECUTIVE failures on a counter that
# any success cleared — which cannot see an intermittent fault, and didn't. The
# real episode (ops-alerts.log:380-398) was 7 push failures over 24-27 Jul with
# successful hourly ticks between them: 24T17:00, 24T23:00, 25T11:00, 26T10:00,
# 26T12:00, 26T16:00, 27T00:00. The longest consecutive run was 1, so the
# counter never got past 1 and nothing escalated for four days.
#
# Replaced with failures-in-window: N failures within M hours, successes no
# longer reset the history — entries only age out.
#   N=3, M=24. Against the real pattern that first fires at 25T11:00 (three
#   failures inside the preceding 24h: 24T17, 24T23, 25T11) — day two of a
#   four-day episode — and again on 26 Jul. A single self-healing blip stays
#   quiet, and so does a pair (24T17 + 24T23 alone is 2).
# Hours, not tick counts, because five jobs share this helper on different
# cadences (cc-gitpull twice-hourly, others daily) and "how long has my data
# been unbacked-up" is the question that matters.
ESCALATE_FAILS    = 3
ESCALATE_WINDOW_H = 24


def _append(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(line.rstrip("\n") + "\n")
        _trim(path)
    except Exception:
        pass


def _trim(path: Path) -> None:
    try:
        max_lines = _MAX_LINES.get(path, 5000)
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


def record_run(script: str, athlete: str = "", ok: bool = True, detail: str = "") -> None:
    """One structured heartbeat line per script run (or per-athlete outcome)."""
    _append(RUN_STATUS, json.dumps({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "script": script,
        "athlete": athlete,
        "ok": bool(ok),
        "detail": detail,
    }, separators=(",", ":")))


def alert(script: str, message: str, athlete: str = "") -> None:
    """Something a human should see — lands in the alert log AND as a failed
    run-status entry, so the evening digest picks it up either way."""
    ts = datetime.now().isoformat(timespec="seconds")
    who = f":{athlete}" if athlete else ""
    _append(ALERT_LOG, f"[{ts}] [{script}{who}] {message}")
    record_run(script, athlete=athlete, ok=False, detail=message)


def log_outbound(source: str, text: str, sent: bool = True, athlete: str = "") -> None:
    """Record a message VERBATIM, whether or not it actually went out.

    The point is the debugging record: before this, alert and digest text was
    sent with notify.py --no-history and never written down anywhere, so the
    only trace of what the coach was told was the coach's own Telegram thread.
    Multi-line bodies are indented under a header so the block reads back.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    who = f":{athlete}" if athlete else ""
    head = "SENT" if sent else "NOT SENT (log-only)"
    body = "\n".join("    | " + ln for ln in str(text).splitlines()) or "    | (empty)"
    _append(ALERT_LOG, f"[{ts}] [{source}{who}] {head} — rendered text follows:\n{body}")


def _sync_state_path(job: str) -> Path:
    """Per-job counter file. The job label reaches here from a caller argument
    (cc-git-commit-push.sh passes an arbitrary one), so it is sanitised before
    it becomes a filename."""
    return SYNC_STATE / (re.sub(r"[^A-Za-z0-9._-]", "_", str(job)) or "job")


def _read_sync_state(job: str) -> dict:
    try:
        return json.loads(_sync_state_path(job).read_text())
    except Exception:
        return {}


def _write_sync_state(job: str, state: dict) -> None:
    try:
        SYNC_STATE.mkdir(parents=True, exist_ok=True)
        _sync_state_path(job).write_text(json.dumps(state))
    except Exception:
        pass


def _recent_failures(state: dict, now: datetime) -> list[str]:
    """Failure timestamps still inside the escalation window. Ageing out is the
    ONLY thing that shrinks this list — a success no longer wipes it, which is
    the whole point: an intermittent fault has successes in it by definition."""
    cutoff = now - timedelta(hours=ESCALATE_WINDOW_H)
    out = []
    for ts in state.get("fails", []):
        try:
            if datetime.fromisoformat(ts) > cutoff:
                out.append(ts)
        except Exception:
            continue
    return out


def sync_failure(job: str, message: str, now: datetime = None) -> str:
    """Record a git sync failure and decide how loud it should be.

    Two independent counters, both per job (five jobs share this helper and a
    busy one succeeding must not mask a stuck one):
      n     — CONSECUTIVE failures, cleared by sync_ok. Drives the transient/
              alert distinction, which it does well: one blip is not news.
      fails — timestamps of the last ESCALATE_WINDOW_H hours of failures, NOT
              cleared by success. Drives escalation.
    Returns the action taken: "transient" (first failure, logged but not
    alert-worthy), "alert" (in the evening digest), or "escalate" (a louder
    digest line — log-only since 28 Jul 2026; see _escalate)."""
    now = now or datetime.now()
    state = _read_sync_state(job)
    n = int(state.get("n", 0)) + 1
    state["n"] = n
    fails = _recent_failures(state, now)
    fails.append(now.isoformat(timespec="seconds"))
    state["fails"] = fails[-64:]

    # Transient ONLY while both tests say so. The window test matters here as much as
    # at escalation: an intermittent fault resets the consecutive counter on every
    # intervening success, so on the consecutive test alone all seven of the 24-27 Jul
    # failures were "1st consecutive, usually self-heals" — recorded ok=True, invisible
    # to the digest. Once the window is over ESCALATE_FAILS, a failure is a failure.
    if n < ALERT_AFTER and len(state["fails"]) < ESCALATE_FAILS:
        ts = now.isoformat(timespec="seconds")
        _append(ALERT_LOG, f"[{ts}] [{job}] transient (1st consecutive, usually self-heals "
                           f"next tick): {message}")
        record_run(job, ok=True, detail=f"transient git-sync failure (1st): {message}")
        action = "transient"
    else:
        # The consecutive count goes in the log line but NOT in the run-status
        # detail: the digest folds repeats on that detail, and a count baked into
        # it would make every tick of one stuck sync a distinct digest line again.
        ts = now.isoformat(timespec="seconds")
        _append(ALERT_LOG, f"[{ts}] [{job}] {message} ({n} consecutive, "
                           f"{len(state['fails'])} in {ESCALATE_WINDOW_H}h)")
        record_run(job, ok=False, detail=message)
        action = "alert"

    # Genuinely stuck or genuinely flapping: commits (including athlete data) are
    # piling up locally and unbacked-up. Once per window — the marker is dropped
    # again as soon as the window has aged empty, so a fresh episode re-fires.
    if len(state["fails"]) >= ESCALATE_FAILS and not state.get("escalated"):
        state["escalated"] = True
        action = "escalate"
        _escalate(job, len(state["fails"]), message, now)

    _write_sync_state(job, state)
    return action


def sync_ok(job: str, now: datetime = None) -> None:
    """Clean run for this job — reset the CONSECUTIVE counter, and keep the
    windowed failure history, which is what makes an intermittent fault
    visible. Once the window has aged empty the state file goes entirely, which
    also re-arms escalation for the next episode."""
    now = now or datetime.now()
    state = _read_sync_state(job)
    fails = _recent_failures(state, now)
    if not fails:
        try:
            _sync_state_path(job).unlink()
        except Exception:
            pass
        return
    _write_sync_state(job, {"n": 0, "fails": fails,
                            "escalated": bool(state.get("escalated"))})


def _escalate(job: str, n: int, message: str, now: datetime = None) -> None:
    """LOG-ONLY since 28 Jul 2026. A stuck git sync is not one of the two
    conditions allowed to interrupt the coach (lib/coach_alert.py), so the
    notify call is gone. The rendered text is still written verbatim with
    sent=False, and the ✗ run-status entry above still puts it in the evening
    digest — so this is loud on the VM and silent on Telegram."""
    now = now or datetime.now()
    # The underlying error is raw git plumbing (git_sync._stderr passes up to 300
    # chars of stderr verbatim). It goes to the log, never into the rendered text —
    # tone guide §5: an alert names the consequence, not the mechanism.
    ts = now.isoformat(timespec="seconds")
    _append(ALERT_LOG, f"[{ts}] [{job}] ESCALATED — {n} failures in the last "
                       f"{ESCALATE_WINDOW_H}h; underlying error: {message}")
    text = (f"⚠️ Your training data has not been backed up on {n} attempts in the last "
            f"{ESCALATE_WINDOW_H} hours. Nothing you have logged is lost — it is all "
            f"still here — but it is only in one place until this clears.")
    log_outbound(f"{job}-escalation", text, sent=False)
