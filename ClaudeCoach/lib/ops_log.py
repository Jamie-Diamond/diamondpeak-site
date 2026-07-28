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
one. The single exception is sync_failure()'s stuck-sync escalation; see there.
"""
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR    = Path.home() / "Library/Logs/ClaudeCoach"
ALERT_LOG  = LOG_DIR / "ops-alerts.log"
RUN_STATUS = LOG_DIR / "run-status.jsonl"
SYNC_STATE = LOG_DIR / "git-sync-state"

_MAX_LINES = {ALERT_LOG: 5000, RUN_STATUS: 2000}

# A git sync that fails once and heals itself on the next tick is not news: over
# 23-27 Jul every single instant alert was of that kind. Alert from the SECOND
# consecutive failure, and only escalate to Telegram once the sync is genuinely
# stuck (ESCALATE_AFTER x the every-30-min tick = ~3h of commits piling up).
ALERT_AFTER    = 2
ESCALATE_AFTER = 6


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


def sync_failure(job: str, message: str) -> str:
    """Record a git sync failure and decide how loud it should be.

    Counters are per job and cleared only by that job's own sync_ok, because
    five different jobs share this helper and a busy one succeeding must not
    reset a genuinely stuck one. Returns the action taken: "transient" (first
    failure, logged but not alert-worthy), "alert" (in the evening digest), or
    "escalate" (also Telegrammed, once per stuck episode)."""
    state = _read_sync_state(job)
    n = int(state.get("n", 0)) + 1
    state["n"] = n

    if n < ALERT_AFTER:
        ts = datetime.now().isoformat(timespec="seconds")
        _append(ALERT_LOG, f"[{ts}] [{job}] transient (1st consecutive, usually self-heals "
                           f"next tick): {message}")
        record_run(job, ok=True, detail=f"transient git-sync failure (1st): {message}")
        action = "transient"
    else:
        # The consecutive count goes in the log line but NOT in the run-status
        # detail: the digest folds repeats on that detail, and a count baked into
        # it would make every tick of one stuck sync a distinct digest line again.
        ts = datetime.now().isoformat(timespec="seconds")
        _append(ALERT_LOG, f"[{ts}] [{job}] {message} ({n} consecutive)")
        record_run(job, ok=False, detail=message)
        action = "alert"

    # The ONE thing still allowed to interrupt the coach: the sync is stuck, so
    # commits (including athlete data) are piling up locally and unbacked-up.
    # Once per episode — the next success clears the marker.
    if n >= ESCALATE_AFTER and not state.get("escalated"):
        state["escalated"] = True
        action = "escalate"
        _escalate(job, n, message)

    _write_sync_state(job, state)
    return action


def sync_ok(job: str) -> None:
    """Clean run for this job — reset its counter and escalation marker."""
    try:
        _sync_state_path(job).unlink()
    except Exception:
        pass


def _escalate(job: str, n: int, message: str) -> None:
    # The underlying error is raw git plumbing (git_sync._stderr passes up to 300
    # chars of stderr verbatim). It goes to the log, never into the sent text —
    # tone guide §5: an alert names the consequence, not the mechanism.
    ts = datetime.now().isoformat(timespec="seconds")
    _append(ALERT_LOG, f"[{ts}] [{job}] ESCALATED after {n} consecutive "
                       f"failures; underlying error: {message}")
    text = (f"⚠️ Git sync has been failing for {n} runs in a row ({job}). "
            f"Changes are saved on the VM but are not reaching GitHub, so they are "
            "not backed up.")
    log_outbound(f"{job}-escalation", text, sent=True)
    try:
        notify = Path(__file__).resolve().parent.parent / "telegram" / "notify.py"
        subprocess.run([sys.executable, str(notify), "--no-history", text],
                       capture_output=True, timeout=30)
    except Exception:
        pass
