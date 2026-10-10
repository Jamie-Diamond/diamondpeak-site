#!/usr/bin/env python3
"""Shadow week: Haiku writes the same athlete-facing messages as Sonnet, never sent
(Jamie, 9 Oct 2026, "1. Shadow week").

A one-off side-by-side on 9 Oct showed Haiku 5.5 close on morning cards but changing
numbers in a night-before brief, and the debrief could not be tested on already-logged
runs. So for SHADOW_UNTIL days, after each real morning card, night-before brief and
activity debrief, launch() starts a DETACHED Haiku run of the same prompt:

  - read-only: Read/Grep and lib/icu_fetch.py only, with CC_READ_ONLY=1 (icu_fetch
    refuses every write endpoint), so it cannot send, write a file or touch the calendar
  - never waited on and fail-soft: the real job's message is already out, and nothing
    here may delay or break it
  - saved with Sonnet's output to SHADOW_DIR/<date>/<job>-<slug>-<time>.json

    python3 lib/shadow.py --report [--days 7]     the pairs, for review

How to review (Jamie, 10 Oct 2026: "we need to not say it's Haiku that's the issue, it's
probably our prompting"): a difference is a finding about the PROMPT first. For each one,
find what in the prompt allowed it - an unlabelled data line printed as copy, a sum left to
the model, a step that pulls stale notes, a value with no stated source - and fix that,
so the message comes out right on any model. Only what survives a fixed prompt counts
against the model. The 9-10 Oct night-before fixes (scripts/night-before-brief.py) are
the worked example.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent             # ClaudeCoach/
SHADOW_DIR = Path.home() / "Library/Logs/ClaudeCoach/shadow"
SHADOW_UNTIL = date(2026, 10, 16)                           # 7 days from 10 Oct
TOOLS = "Read,Grep,Bash(python3 ClaudeCoach/lib/icu_fetch.py:*)"


def active(today: date | None = None) -> bool:
    return (today or date.today()) <= SHADOW_UNTIL


def launch(job: str, slug: str, prompt: str, sonnet: str, note: str = "") -> None:
    """Start the Haiku twin of a run that has just finished. Never raises."""
    try:
        if not active() or not prompt:
            return
        now = datetime.now()
        d = SHADOW_DIR / now.date().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{job}-{slug}-{now.strftime('%H%M%S')}.json"
        f.write_text(json.dumps({"job": job, "slug": slug, "ts": now.isoformat(timespec="seconds"),
                                 "prompt": (note + "\n\n" if note else "") + prompt,
                                 "sonnet": sonnet or "", "haiku": None}))
        env = {**os.environ, "CC_ATHLETE_SCOPE": slug, "CC_READ_ONLY": "1"}
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--run", str(f)],
                         env=env, cwd=str(BASE.parent), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _run(path: str) -> None:
    sys.path.insert(0, str(BASE / "lib"))
    import claude_call
    f = Path(path)
    rec = json.loads(f.read_text())
    try:
        r = claude_call.run_claude(rec["prompt"], model=claude_call.HAIKU, fallback=[],
                                   allowed_tools=TOOLS, cwd=str(BASE.parent), timeout=400,
                                   no_session_persistence=True, label=f"shadow:{rec['slug']}",
                                   env=dict(os.environ))
        rec["haiku"], rec["rc"] = r.stdout or "", r.returncode
    except Exception as e:
        rec["haiku"], rec["rc"] = f"[shadow run failed: {e}]", -1
    f.write_text(json.dumps(rec))


def report(days: int = 7) -> str:
    import re
    out = []
    for d in sorted(SHADOW_DIR.glob("*"))[-days:]:
        for f in sorted(d.glob("*.json")):
            r = json.loads(f.read_text())

            def tg(t):
                m = re.search(r"<telegram>(.*?)</telegram>", t or "", re.S)
                return m.group(1).strip() if m else (t or "").strip()
            out.append(f"===== {d.name} {r['job']} {r['slug']} rc={r.get('rc')}\n"
                       f"--- Sonnet:\n{tg(r['sonnet'])}\n--- Haiku:\n{tg(r.get('haiku'))}\n")
    return "\n".join(out) or "no shadow runs yet"


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--run":
        _run(sys.argv[2])
    elif "--report" in sys.argv:
        n = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
        print(report(n))
