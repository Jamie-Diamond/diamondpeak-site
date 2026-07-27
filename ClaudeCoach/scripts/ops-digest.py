#!/usr/bin/env python3
"""
Evening ops digest — LOG-ONLY. Runs via VM crontab at 21:30 daily.

Reads run-status.jsonl (written by the cron scripts via lib/ops_log.py) for
today's entries and records a digest ONLY if something failed or a daily
deliverable is missing — a missed morning card, a missed prescription, or a
watchdog that never ran. Silent when everything ran clean.

Changed 27 Jul 2026 (Jamie's call): this used to Telegram the coach's own
coaching thread, which mixed engineering chatter into a personal thread and put
up to 24 raw log lines in it at a time. The digest now writes its full rendered
text to ops-alerts.log via ops_log.log_outbound — the same words, on the VM,
where he or an agent can go and read them when there is a reason to. Repeated
(script, athlete, detail) lines are folded into one line with a count, because
the same warning eight times is one fact, not eight.

Safe to run manually: python3 ClaudeCoach/scripts/ops-digest.py
"""
import json, subprocess, sys
from datetime import date, datetime
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
CONFIG      = BASE / "config/athletes.json"
sys.path.insert(0, str(BASE / "lib"))
import ops_log

MAX_LINES = 40  # cap the logged digest — the raw entries stay in run-status.jsonl


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
    """Failure + gap lines for today, PLUS informational auto-apply lines; empty
    list = all clean. Auto-applied bug-fixer rule merges/prunes (guard-validated,
    loss-free, no human review needed) are not a failure, but Jamie should still see
    what changed unattended overnight — one line each, from lib/ops_log's
    'bug-fixer-automerge' run-status entries."""
    lines = []

    # Fold repeats: plan_builder logged the SAME weekly_tss_cap warning 8 times in
    # 90 seconds on 26 Jul and all 8 went into the digest. Key on what makes the
    # line unique, keep the earliest timestamp, count the rest. Deduplication
    # happens HERE and never at write time — run-status.jsonl is also the
    # heartbeat source for the _ran() gap checks below, so dropping entries there
    # would break "did the morning card go out for kathryn".
    folded: dict = {}
    for e in entries:
        ok = bool(e.get("ok"))
        if ok and e.get("script") != "bug-fixer-automerge":
            continue
        key = (ok, e.get("script", "?"), e.get("athlete", ""), e.get("detail", ""))
        if key in folded:
            folded[key] += 1
        else:
            folded[key] = 1
            who = f" ({e['athlete']})" if e.get("athlete") else ""
            ts = str(e.get("ts", ""))[11:16]
            mark = "✓" if ok else "✗"
            what = "auto-applied" if ok else e.get("script", "?")
            lines.append((key, f"{mark} {ts} {what}{who}: {e.get('detail', '')}"))

    lines = [text + (f" (x{folded[key]})" if folded[key] > 1 else "")
             for key, text in lines]

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
        shown.append(f"…and {len(lines) - MAX_LINES} more — see run-status.jsonl")
    msg = "🛠 ClaudeCoach ops digest\n" + "\n".join(shown)
    ops_log.log_outbound("ops-digest", msg, sent=False)
    print(msg, file=sys.stderr)


if __name__ == "__main__":
    main()
