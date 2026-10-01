"""The coach's link to Claude, for Peak's Settings (Jamie, 1 Oct 2026: "ideally this
isn't an AI function, this is code"). Every Claude call records its outcome here in
plain code; api/server.py /api/claude/status reads it. One small JSON file, written
atomically; a lost update between two processes only delays the status by one call.

    {"last_ok": iso, "last_ok_where": label,
     "last_fail": iso, "last_fail_where": label, "last_fail_detail": short reason}
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

PATH = Path(os.environ.get("CC_CLAUDE_HEALTH") or
            Path(__file__).resolve().parent.parent / ".claude_health.json")


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def record(ok: bool, where: str = "", detail: str = "") -> None:
    try:
        st = load()
        now = datetime.now().isoformat(timespec="seconds")
        if ok:
            st.update(last_ok=now, last_ok_where=where or "")
        else:
            st.update(last_fail=now, last_fail_where=where or "", last_fail_detail=(detail or "")[:200])
        tmp = PATH.with_name(PATH.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(PATH)
    except Exception:
        pass


def state(st: dict | None = None) -> dict:
    """{"state": "ok" | "failing" | "unknown", ...}: failing when the most recent
    outcome was a failure."""
    st = load() if st is None else st
    ok, fail = st.get("last_ok") or "", st.get("last_fail") or ""
    s = "unknown" if not (ok or fail) else ("failing" if fail > ok else "ok")
    return {"state": s, **st}
