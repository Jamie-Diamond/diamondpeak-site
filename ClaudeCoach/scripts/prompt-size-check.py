#!/usr/bin/env python3
"""Daily: flag any scheduled job whose prompt passed the Haiku 5.5 price step (Jamie, 12 Oct
2026: "we want to keep prompts below 100k as that's when the haiku cost goes up").

Reads yesterday's CLI transcripts (the same ones scripts/usage-report.py prices). One API
call's prompt = input + cache reads + cache writes, and it grows as a job opens files, so
the largest call per run is what counts. A run over WARN_AT (90k, room before the 100k
step) goes to the ops-alerts log, once per job per day, with its athlete and size. Chat,
Dev and the bug fixer are exempt: they are expected to run long and are not moving to Haiku.

Cron (VM): 06:10 daily.  python3 scripts/prompt-size-check.py [--days 1] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))
import ops_log  # noqa: E402

WARN_AT = 90_000
EXEMPT = {"chat", "dev work", "bug fixer", "smoke test"}


def _ur():
    spec = importlib.util.spec_from_file_location("usage_report", BASE / "scripts" / "usage-report.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def scan(days: int = 1) -> dict:
    """{job: [(max prompt tokens, athlete, file), ...]} for runs over WARN_AT."""
    ur = _ur()
    athletes = json.loads((BASE / "config" / "athletes.json").read_text())
    since = datetime.now(timezone.utc) - timedelta(days=days)
    over = defaultdict(list)
    for pat in ur.PROJECT_GLOBS:
        for f in glob.glob(str(ur.PROJECTS / pat / "*.jsonl")):
            try:
                if datetime.fromtimestamp(Path(f).stat().st_mtime, timezone.utc) < since:
                    continue
                rows = [json.loads(l) for l in open(f, errors="replace") if l.strip()]
            except (OSError, ValueError):
                continue
            ath, job = ur.attribute(f, ur.first_prompt(rows), athletes, set())
            if job in EXEMPT:
                continue
            mx = 0
            for r in rows:
                if r.get("type") != "assistant" or not r.get("timestamp"):
                    continue
                if datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) < since:
                    continue
                u = (r.get("message") or {}).get("usage") or {}
                mx = max(mx, (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                         + (u.get("cache_creation_input_tokens") or 0))
            if mx > WARN_AT:
                over[job].append((mx, ath, Path(f).name))
    return over


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    over = scan(a.days)
    for job, runs in sorted(over.items()):
        runs.sort(reverse=True)
        top = runs[0]
        msg = (f"{job}: {len(runs)} run(s) passed {WARN_AT // 1000}k prompt tokens "
               f"(largest {top[0] // 1000}k, {top[1]}, {top[2]}). Haiku bills over 100k at 5x: "
               "trim what this job pastes or reads (lib/prompt_trim.py).")
        print(msg)
        if not a.dry_run:
            ops_log.alert("prompt-size-check", msg)
    if not over:
        print("all scheduled-job prompts under", WARN_AT)


if __name__ == "__main__":
    main()
