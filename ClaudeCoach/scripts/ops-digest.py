#!/usr/bin/env python3
"""
Evening ops digest — coach-only. Runs via VM crontab at 21:30 daily.

Reads run-status.jsonl (written by the cron scripts via lib/ops_log.py) for
today's entries and messages the coach ONLY if something failed or a daily
deliverable is missing — a missed morning card, a missed prescription, or a
watchdog that never ran. Silent when everything ran clean.

Sends to the default chat in telegram/config.json (the coach), never athletes.
Safe to run manually: python3 ClaudeCoach/scripts/ops-digest.py
"""
import json, subprocess, sys
from datetime import date, datetime
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)
NOTIFY      = BASE / "telegram/notify.py"
CONFIG      = BASE / "config/athletes.json"
sys.path.insert(0, str(BASE / "lib"))
import ops_log

MAX_LINES = 15  # cap the Telegram message — full detail stays in the logs


def todays_entries() -> list[dict]:
    today = date.today().isoformat()
    entries = []
    try:
        for line in ops_log.RUN_STATUS.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if str(e.get("ts", "")).startswith(today):
                entries.append(e)
    except FileNotFoundError:
        pass
    return entries


def build_digest(entries: list[dict], athletes: dict) -> list[str]:
    """Failure + gap lines for today; empty list = all clean."""
    lines = []

    for e in entries:
        if not e.get("ok"):
            who = f" ({e['athlete']})" if e.get("athlete") else ""
            ts = str(e.get("ts", ""))[11:16]
            lines.append(f"✗ {ts} {e.get('script', '?')}{who}: {e.get('detail', '')}")

    def _ran(script, athlete=None, detail=None):
        return any(
            e.get("ok") and e.get("script") == script
            and (athlete is None or e.get("athlete") == athlete)
            and (detail is None or e.get("detail") == detail)
            for e in entries
        )

    # Gap lines report heartbeat absence — "we have no record it happened",
    # which is distinct from "we recorded a failure" (the ✗ lines above).
    active = {s: c for s, c in athletes.items() if c.get("active")}
    for slug in active:
        if not _ran("morning-checkin", athlete=slug, detail="card sent"):
            lines.append(f"⚠ no morning-card heartbeat for {slug}")
    for slug, cfg in active.items():
        if cfg.get("daily_prescription", True) and not _ran("daily-prescription", athlete=slug):
            lines.append(f"⚠ no prescription heartbeat for {slug}")
    if not _ran("watchdog"):
        lines.append("⚠ no watchdog heartbeat today")

    return lines


def plan_sanity(athletes: dict) -> list[str]:
    """UNDER-TRAINING check on the week of tomorrow (Sunday evening = the freshly
    planned next week; midweek = the live week). Compares the calendar's
    planned+completed total against the required-tss floor for that week —
    min(phase requirement, 7 x CTL maintenance); deload/taper floors are 0.
    Added 5 Jul 2026 after a 581-TSS week was planned into a specific-phase
    week needing 816 and no report flagged it."""
    from datetime import timedelta
    pt = str(BASE / "lib" / "plan_tools.py")
    target_day = date.today() + timedelta(days=1)
    monday = target_day - timedelta(days=target_day.weekday())
    lines = []
    for slug, cfg in athletes.items():
        if not cfg.get("active"):
            continue
        try:
            wk = json.loads(subprocess.run(
                [sys.executable, pt, "week-tss", "--athlete", slug,
                 "--week-start", monday.isoformat()],
                capture_output=True, text=True, timeout=90).stdout)
            req = json.loads(subprocess.run(
                [sys.executable, pt, "required-tss", "--athlete", slug,
                 "--date", monday.isoformat()],
                capture_output=True, text=True, timeout=90).stdout)
        except Exception:
            continue
        total, floor = wk.get("total_tss"), req.get("weekly_tss_floor")
        if total is None or not floor:
            continue
        if total < floor * 0.95:
            lines.append(
                f"🔥 {slug}: week of {monday} totals {total} TSS vs floor {floor} "
                f"(required ~{req.get('recommended_weekly_tss')}, "
                f"{req.get('phase')}) — UNDER-TRAINING, plan needs volume")
    return lines


def main():
    try:
        athletes = json.loads(CONFIG.read_text())
    except Exception as e:
        athletes = {}
        print(f"ops-digest: failed to load athletes config: {e}", file=sys.stderr)

    lines = build_digest(todays_entries(), athletes)
    lines += plan_sanity(athletes)
    if not lines:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ops-digest: all clean", file=sys.stderr)
        return

    shown = lines[:MAX_LINES]
    if len(lines) > MAX_LINES:
        shown.append(f"…and {len(lines) - MAX_LINES} more — see ops-alerts.log")
    msg = "🛠 *ClaudeCoach ops digest*\n" + "\n".join(shown)
    # --no-history: ops chatter must not pollute the coach's athlete history
    r = subprocess.run(
        ["python3", str(NOTIFY), "--no-history", msg],
        cwd=PROJECT_DIR, timeout=20,
    )
    if r.returncode != 0:
        print("ops-digest: Telegram send failed", file=sys.stderr)


if __name__ == "__main__":
    main()
