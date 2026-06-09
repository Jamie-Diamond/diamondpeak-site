"""Operational logging — coach-facing alerts plus a run-status heartbeat.

Two append-only files under the existing log dir:
  ops-alerts.log   — human-readable lines for things a coach should see
  run-status.jsonl — one JSON object per script run/outcome; the evening
                     ops digest reads this to flag failures and gaps

Both are fail-soft: observability must never break the workflow it observes.
"""
import json
from datetime import datetime
from pathlib import Path

LOG_DIR    = Path.home() / "Library/Logs/ClaudeCoach"
ALERT_LOG  = LOG_DIR / "ops-alerts.log"
RUN_STATUS = LOG_DIR / "run-status.jsonl"

_MAX_LINES = {ALERT_LOG: 5000, RUN_STATUS: 2000}


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
