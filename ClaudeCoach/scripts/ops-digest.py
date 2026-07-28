#!/usr/bin/env python3
"""
Evening ops digest. Runs via VM crontab at 21:30 daily.

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

Still log-only for everything EXCEPT one thing (28 Jul 2026): if a named
deliverable has no heartbeat, the coach gets a Telegram. That is condition 1 of
the two Jamie allows to interrupt him; which deliverables qualify is declared in
lib/coach_alert.DELIVERABLES, not here, so the routing decision reads in one
place. Every failure line stays in the log either way.

DAILY deliverables are sent from main() below, keyed on today's date — a fresh
key every day, so a recurring daily miss re-alerts daily, which is correct (each
day is its own miss). WEEKLY deliverables (weekly summary, weekly plan) route
to Telegram too as of 28 Jul 2026, but NOT through that same per-day key: the
weekly gap check reads a 7-day window, so a single Sunday miss would otherwise
stay "missing" and re-Telegram every evening for up to a week. weekly_alerts()
below sends these on a stable per-script key with a cooldown spanning the whole
window, and clears that cooldown the moment the deliverable is seen again — one
miss, one message.

Safe to run manually: python3 ClaudeCoach/scripts/ops-digest.py
  CC_ALERT_DRY_RUN=1 python3 .../ops-digest.py   # never sends; logs what it would

Exercising it by hand is safe on the send side either way — coach_alert's
cooldown means a hand-run cannot double-message.
"""
import json, subprocess, sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
CONFIG      = BASE / "config/athletes.json"
sys.path.insert(0, str(BASE / "lib"))
import ops_log
import coach_alert

MAX_LINES = 40  # cap the logged digest — the raw entries stay in run-status.jsonl
WEEKLY_WINDOW_DAYS = 7
# A weekly miss stays "missing" for the whole 7-day window it's checked over.
# Bank the cooldown for that long so one miss can't re-Telegram on 7 separate
# evenings; weekly_alerts() clears it early the moment the job is seen again.
WEEKLY_ALERT_COOLDOWN_H = WEEKLY_WINDOW_DAYS * 24


def all_entries() -> list[dict]:
    out = []
    try:
        for line in ops_log.RUN_STATUS.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return out


def todays_entries(entries: list[dict] = None) -> list[dict]:
    today = date.today().isoformat()
    return [e for e in (entries if entries is not None else all_entries())
            if str(e.get("ts", "")).startswith(today)]


def since(entries: list[dict], days: int) -> list[dict]:
    """Entries from the last `days` days — the window the Sunday jobs are checked
    over. run-status.jsonl is trimmed by line count, so ops_log._MAX_LINES was
    raised alongside this to guarantee the window is actually there to read."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return [e for e in entries if str(e.get("ts", "")) >= cutoff]


def build_digest(entries: list[dict], athletes: dict) -> list[str]:
    """Failure lines for today, PLUS informational auto-apply lines; empty
    list = all clean. Gap lines are gap_lines()'s job. Auto-applied bug-fixer
    rule merges/prunes (guard-validated,
    loss-free, no human review needed) are not a failure, but Jamie should still see
    what changed unattended overnight — one line each, from lib/ops_log's
    'bug-fixer-automerge' run-status entries."""
    lines = []

    # Fold repeats: plan_builder logged the SAME weekly_tss_cap warning 8 times in
    # 90 seconds on 26 Jul and all 8 went into the digest. Key on what makes the
    # line unique, keep the earliest timestamp, count the rest. Deduplication
    # happens HERE and never at write time — run-status.jsonl is also the
    # heartbeat source for the gap checks, so dropping entries there
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

    return lines


def _saw(entries, script, athlete=None, detail=None) -> bool:
    """Is there ANY heartbeat for this job — success or failure?

    Deliberately different from _ran() below. A recorded FAILURE is not a gap: it
    is already a ✗ line above, and reporting it again as "no heartbeat" is the
    same fact twice. _ran() (ok-only) is kept for the three original checks so
    their long-standing behaviour is unchanged; every new check uses _saw().
    """
    return any(
        e.get("script") == script
        and (athlete is None or e.get("athlete") == athlete)
        and (detail is None or e.get("detail") == detail)
        for e in entries
    )


def gap_lines(today_entries, week_entries, athletes) -> tuple[list[str], list[str]]:
    """(all gap lines, the subset of DAILY misses that may interrupt the coach).

    Gap lines report heartbeat absence — "we have no record it happened", which is
    distinct from "we recorded a failure" (the ✗ lines). What counts as a
    deliverable, its window, and whether it may Telegram all come from
    coach_alert.DELIVERABLES. WEEKLY telegram routing is deliberately NOT done
    here — see weekly_alerts() below for why it needs a different cooldown key.
    """
    lines, telegram = [], []
    active = {s: c for s, c in athletes.items() if c.get("active")}

    def _ran(entries, script, athlete=None, detail=None):
        # Original ok-only semantics, kept verbatim for the three pre-existing checks.
        return any(
            e.get("ok") and e.get("script") == script
            and (athlete is None or e.get("athlete") == athlete)
            and (detail is None or e.get("detail") == detail)
            for e in entries
        )

    _ORIGINAL = {"morning-checkin", "daily-prescription", "watchdog"}

    for d in coach_alert.DELIVERABLES:
        entries = week_entries if d["window"] == "weekly" else today_entries
        when = (f"in {WEEKLY_WINDOW_DAYS} days" if d["window"] == "weekly" else "today")
        seen = _ran if d["script"] in _ORIGINAL else _saw
        targets = list(active) if d["per_athlete"] else [None]
        for slug in targets:
            # morning-checkin's own opt-out flag, unchanged.
            if d["script"] == "daily-prescription" and not active[slug].get("daily_prescription", True):
                continue
            if seen(entries, d["script"], athlete=slug, detail=d.get("detail")):
                continue
            who = f" for {slug}" if slug else ""
            line = f"⚠ no {d['label']} heartbeat{who} {when}"
            lines.append(line)
            if d["telegram"] and d["window"] == "daily":
                telegram.append(f"{d['label']}{who}")
    return lines, telegram


def weekly_alerts(week_entries, athletes) -> list[str]:
    """Telegram condition 1 for WEEKLY deliverables — split out from gap_lines()
    because it needs occurrence-based alerting, not the daily per-day key.

    Two differences from the daily path:
      - ONE message per SCRIPT, not one per athlete. A Sunday job failing is a
        single root cause; three copies of the same fact (one per athlete) is
        noise, not three incidents.
      - ONE message per OCCURRENCE, not one per evening it stays missing. The
        cooldown key is stable (not date-based) and banked for
        WEEKLY_ALERT_COOLDOWN_H (the whole window), then cleared the moment the
        deliverable is seen again so the following week's miss isn't silenced
        by a leftover cooldown.

    Returns the labels actually alerted this run (sent or dry-run), for logging.
    """
    active = {s: c for s, c in athletes.items() if c.get("active")}
    alerted = []
    for d in coach_alert.DELIVERABLES:
        if d["window"] != "weekly" or not d["telegram"]:
            continue
        targets = list(active) if d["per_athlete"] else [None]
        missing = [slug for slug in targets
                   if not _saw(week_entries, d["script"], athlete=slug, detail=d.get("detail"))]
        key = f"weekly:{d['script']}"
        if not missing:
            coach_alert.clear_cooldown(coach_alert.DELIVERABLE_MISSING, key)
            continue
        who = " (" + ", ".join(missing) + ")" if d["per_athlete"] else ""
        action = coach_alert.send(
            coach_alert.DELIVERABLE_MISSING,
            f"⚠️ ClaudeCoach did not deliver: {d['label']}{who}. "
            f"Nothing is lost — the detail is in the ops log on the VM.",
            key=key, cooldown_h=WEEKLY_ALERT_COOLDOWN_H)
        print(f"coach-alert deliverable_missing weekly:{d['script']}: {action}", file=sys.stderr)
        if action in ("sent", "dry-run"):
            alerted.append(f"{d['label']}{who}")
    return alerted


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

    entries = all_entries()
    today   = todays_entries(entries)
    week    = since(entries, WEEKLY_WINDOW_DAYS)

    lines = build_digest(today, athletes)
    gaps, missing_deliverables = gap_lines(today, week, athletes)
    lines += gaps
    lines += plan_sanity(athletes)

    # CONDITION 1 of the two approved Telegram interruptions: a named deliverable
    # did not happen. The list of what qualifies is coach_alert's, not this
    # file's, and coach_alert refuses anything not on it. Daily misses are sent
    # here on today's date as the key; weekly misses go through weekly_alerts()
    # below, which uses an occurrence-based key instead (see its docstring).
    if missing_deliverables:
        what = ", ".join(missing_deliverables)
        action = coach_alert.send(
            coach_alert.DELIVERABLE_MISSING,
            f"⚠️ ClaudeCoach did not deliver today: {what}. "
            f"Nothing is lost — the detail is in the ops log on the VM.",
            key=date.today().isoformat())
        print(f"coach-alert deliverable_missing: {action} ({what})", file=sys.stderr)

    weekly_alerts(week, athletes)

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
