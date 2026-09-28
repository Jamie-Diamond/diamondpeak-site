#!/usr/bin/env python3
"""Offline tests for session-sync's nothing-new gate (28 Sep 2026).
Run: python3 ClaudeCoach/scripts/test_session_sync_gate.py

The gate exists to stop the model re-running 8 times a day on unchanged input ($270 of
a ~$830/month API-priced bill). It fails in two directions and both are guarded:
  - it skips a run that had something new (a rule the athlete stated goes uncaptured)
  - it runs when nothing changed (the saving silently disappears)
No network, no model, no real athlete file.
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
spec = importlib.util.spec_from_file_location("session_sync", _here / "session-sync.py")
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)
import ops_log  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp(prefix="sync-gate-"))
ss.BASE = tmp
ss.LOG_DIR = tmp
ops_log.ALERT_LOG = tmp / "ops-alerts.log"
ops_log.RUN_STATUS = tmp / "run-status.jsonl"
adir = tmp / "athletes/sam"
(adir / "telegram").mkdir(parents=True)
(adir / "persistent-rules.md").write_text("- [perm] Long ride on Saturday\n")
(adir / "current-state.md").write_text("# Sam\n")
hist = adir / "telegram/history.json"

CALLS = []
RC = [0]


class _R:
    def __init__(self, rc):
        self.returncode, self.stdout, self.stderr = rc, "", ""


ss.claude_call.run_claude = lambda *a, **k: CALLS.append(1) or _R(RC[0])


class _D:  # a date whose isoformat we control
    today_value = "2026-09-28"

    @classmethod
    def today(cls):
        return type("X", (), {"isoformat": lambda self: cls.today_value})()


ss.date = _D


def write(history):
    hist.write_text(json.dumps(history))


def run():
    n = len(CALLS)
    ss.run_athlete("sam", {"chat_id": ""})
    return len(CALLS) - n


write([{"user": "no bike on Friday please", "assistant": "Logged."}])
check("first run of the day runs the model", run() == 1)
check("same input again: skipped", run() == 0)

h = json.loads(hist.read_text())
h.append({"user": "", "assistant": "*Morning card* Easy 45 min run today."})
write(h)
check("a coach-only message (morning card) does not re-run it", run() == 0)

h.append({"user": "I've moved my long run to Sunday", "assistant": "Noted."})
write(h)
check("a new athlete message runs it", run() == 1)
check("  ...and then it is quiet again", run() == 0)

(adir / "session-log.json").write_text('[{"activity_id": "i1", "rpe": 6}]')
check("a new logged session runs it (the rolling summary covers sessions)", run() == 1)

_D.today_value = "2026-09-29"
check("a new day always runs it once (date-based pruning)", run() == 1)
check("  ...and only once", run() == 0)

h.append({"user": "swap Tuesday swim for a rest day", "assistant": "Done."})
write(h)
RC[0] = 1
check("a failed model run...", run() == 1)
RC[0] = 0
check("  ...is retried at the next slot, not treated as done", run() == 1)
check("  ...and then quiet", run() == 0)

beats = [json.loads(l) for l in ops_log.RUN_STATUS.read_text().splitlines()]
check("every pass still records its heartbeat (skips included)",
      len(beats) == 11 and all(b["ok"] for b in beats), len(beats))
check("a skip says so in the heartbeat", any("model skipped" in b["detail"] for b in beats))

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
