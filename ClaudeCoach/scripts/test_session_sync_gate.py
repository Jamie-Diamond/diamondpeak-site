#!/usr/bin/env python3
"""Offline tests for session sync: Python every slot, Claude once a night (28 Sep 2026).
Run: python3 ClaudeCoach/scripts/test_session_sync_gate.py

Jamie's call: session sync keeps its data work in Python every two hours and runs Claude
only at the last slot of the day, only when the athlete wrote something new. Guarded in
both directions:
  - the saving: no model run in the day slots, none at night without new athlete chat,
    none for coach-only messages
  - the safety net: a night with new athlete chat does run it, a failed run is retried
    the next night, and the Python pass really does prune and refresh
No network, no model, no real athlete file.
"""
import importlib.util
import json
import sys
import tempfile
from datetime import date
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
(adir / "current-state.md").write_text(
    "# Sam\n\n## Last updated: 2026-09-28\n\n## Recent context (auto-summary)\n\n- raced Sunday\n\n"
    "## Travel & training blocks\n\n| Dates | Location |\n|---|---|\n"
    "| 2026-08-01 to 2026-08-10 | Alps |\n| 2026-09-25 to 2026-10-05 | Girona |\n| summer | TBC |\n")
(adir / "session-log.json").write_text(json.dumps([
    {"date": "2026-09-27", "sport": "Ride", "name": "Long ride", "duration_min": 180, "tss": 190, "rpe": 6},
    {"date": "2026-09-26", "sport": "Run", "name": "Easy run", "duration_min": 40, "tss": 35},
]))
(adir / "current-state.json").write_text(json.dumps({"weight_readings": [{"date": "2026-09-28", "kg": 70.2}]}))
hist = adir / "telegram/history.json"

CALLS = []
RC = [0]


class _R:
    def __init__(self, rc):
        self.returncode, self.stdout, self.stderr = rc, "", ""


ss.claude_call.run_claude = lambda *a, **k: CALLS.append(1) or _R(RC[0])
ss.date = type("D", (), {"today": staticmethod(lambda: date(2026, 9, 28)),
                         "fromisoformat": staticmethod(date.fromisoformat)})


def write(history):
    hist.write_text(json.dumps(history))


def run(night):
    n = len(CALLS)
    ss.run_athlete("sam", {"chat_id": ""}, claude_pass=night)
    return len(CALLS) - n


write([{"user": "no bike on Friday please", "assistant": "Logged."}])
check("a day slot never runs the model, even with new chat", run(False) == 0)
md = (adir / "current-state.md").read_text()
check("the day slot writes the Latest data section", "## Latest data (auto)" in md, md[:300])
check("  ...above the auto-summary", md.index("## Latest data (auto)") < md.index("## Recent context"))
check("  ...with the last sessions, RPE and a missing RPE called out",
      "Load 190" in md and "RPE 6" in md and "RPE not given" in md, md[:600])
check("  ...and the latest weight", "70.2 kg" in md)
check("a travel row that ended over a week ago is pruned", "Alps" not in md)
check("a current travel row stays", "Girona" in md)
check("a row with no parseable date stays", "summer" in md)
run(False)
check("a second day slot rewrites the data section in place, not twice",
      (adir / "current-state.md").read_text().count("## Latest data (auto)") == 1)

check("the night slot with new athlete chat runs the model", run(True) == 1)
check("the same night again: skipped", run(True) == 0)

h = json.loads(hist.read_text())
h.append({"user": "", "assistant": "*Morning card* Easy 45 min run today."})
write(h)
check("a coach-only message (morning card) does not re-arm it", run(True) == 0)

h.append({"user": "I've moved my long run to Sunday", "assistant": "Noted."})
write(h)
RC[0] = 1
check("new athlete chat at night runs it...", run(True) == 1)
RC[0] = 0
check("  ...a failed run is retried the next night", run(True) == 1)
check("  ...and then quiet", run(True) == 0)

beats = [json.loads(l) for l in ops_log.RUN_STATUS.read_text().splitlines()]
check("every pass records a heartbeat", len(beats) == 8 and all(b["ok"] for b in beats), len(beats))
check("a python-only pass says so", any("model not run" in b["detail"] for b in beats))

prompt = ss._build_prompt("sam", "Sam", [], "2026-09-28", 1, [], {})
check("the model is told not to touch the Latest data section", "never edit it" in prompt)
check("the model is no longer asked to prune (code does it)", "PRUNE" not in prompt)

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
