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

Changed 28 Jul 2026 — THE BLIND SPOT. The gap check counted any heartbeat as "it
ran", including one recorded ok=False, so a job that failed and logged the
failure produced no gap and no alert; only a job that died before reaching
ops_log tripped the alarm. It caught crashes and missed failures. _saw() now
discounts FAILURE-class heartbeats. It is not a one-line change because ok=False
means both "I failed" and "I found something" — the classification, and the full
reasoning, live in lib/coach_alert.py above OUTCOME_CLASS.

Changed 28 Jul 2026 — NOTHING IS JUDGED BEFORE IT COULD HAVE HAPPENED. The gap
check asked "is there a heartbeat in this window" and never "could there be one
yet", so it produced two false alarms on its first night from jobs that were
working: backup-config runs at 23:50 and was checked at 21:30, and stage1-plan's
7-day window reached back before its own heartbeat existed. Each deliverable now
declares its crontab schedule and the moment its instrumentation went live, and
coach_alert.due_status() gates the check on both. A deliverable scheduled after
this digest is judged on its PREVIOUS cycle rather than skipped. Full reasoning:
docs/failure-alarm.md, "Nothing is judged before it could have happened".

Changed 28 Jul 2026 — THE REGISTRY IS DERIVED, NOT TRUSTED. coach_alert.DELIVERABLES
was hand-maintained and drifted twice in one day: backup-config was registered with
a time the digest could never see satisfied, and capture-reminder stayed registered
after its cron entry was deleted. Every run now diffs the registry against
`crontab -l` in BOTH directions — a registration with no cron entry, and (the one
that matters) a cron entry nobody watches — derives every due time from the live
schedule, and reports any mismatch as a loud CRON AUDIT line at the TOP of the
digest plus an ops_log.alert. Registry drift is an engineering fault, so it is
log-only: it never reaches Telegram. If the crontab cannot be read the gap check
carries on off the static registry and says so. See coach_alert.cron_audit().

Safe to run manually: python3 ClaudeCoach/scripts/ops-digest.py
  CC_ALERT_DRY_RUN=1 python3 .../ops-digest.py   # never sends; logs what it would

Exercising it by hand is safe on the send side either way — coach_alert's
cooldown means a hand-run cannot double-message.
"""
import json, subprocess, sys
from datetime import date, datetime, time, timedelta
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
    """Did this job DO ITS JOB — i.e. is there a heartbeat that is not a
    recorded failure?

    This is the fix for the alarm's central blind spot. It used to return True
    for ANY heartbeat, on the reasoning that a recorded failure is already a ✗
    digest line and calling it a gap too is the same fact twice. That reasoning
    is wrong where it matters: the ✗ line is log-only, so a job that failed and
    dutifully logged the failure produced no gap, no Telegram, and no
    consequence. Only a job that died before reaching ops_log alarmed — the
    alarm caught crashes and missed failures.

    A FAILURE-class heartbeat no longer counts as having run. A FINDING-class one
    still does, because the script ran fine and found something. Which is which
    is coach_alert.classify()'s call, not ours — see the long note above
    coach_alert.OUTCOME_CLASS for why that distinction cannot be a one-liner.

    An `unclassified` ok=False is treated as "it ran" — deliberately: silence on
    Telegram is the safe default for something nobody has classified, and
    unclassified_lines() below makes sure it is not silent in the digest.
    """
    return any(
        e.get("script") == script
        and (athlete is None or e.get("athlete") == athlete)
        and (detail is None or e.get("detail") == detail)
        and coach_alert.classify(e) != coach_alert.FAILURE
        for e in entries
    )


def unclassified_lines(entries) -> list[str]:
    """Loud digest lines for ok=False entries whose script is in neither class of
    coach_alert.OUTCOME_CLASS.

    This is the half of the safety property that stops an unclassified failure
    being SILENTLY swallowed. Such an entry cannot Telegram (a script nobody has
    classified is not trusted to interrupt the coach), so without this line it
    would vanish into "it ran" and the omission would never surface. Named here
    so whoever reads the digest can go and classify it.
    """
    seen, out = set(), []
    for e in entries:
        if coach_alert.classify(e) != "unclassified":
            continue
        script = e.get("script", "?")
        if script in seen:
            continue
        seen.add(script)
        out.append(f"⚠ UNCLASSIFIED ok=False from {script!r} — it can neither alarm "
                   f"nor be trusted as a success; classify it in "
                   f"coach_alert.OUTCOME_CLASS")
    return out


def not_due_line(d: dict, due, state: str) -> str:
    """The digest line for a deliverable that is NOT judged this run.

    This exists so "not judged" can never be confused with "checked and fine".
    A deliverable in either non-DUE state produces no gap and no Telegram, which
    is the fix for the false alarm — but silence about it would mean a mis-timed
    or never-instrumented deliverable could sit unchecked for ever with nothing
    to show. The line names the reason and the moment it starts being judged.
    """
    if state == coach_alert.NO_CRON_ENTRY:
        # The capture-reminder case, now detected instead of eyeballed. Not judged,
        # because a job with no crontab entry cannot run and reporting it missing
        # would be a false alarm about the REGISTRY. cron_audit() has already put a
        # loud CRON AUDIT line at the top of this digest naming the fix.
        return (f"\u2139 {d['label']} not judged \u2014 {d['cron_cmd']} has no live crontab "
                f"entry, so it cannot run (see the CRON AUDIT line above)")
    if state == coach_alert.PRE_INSTRUMENTATION:
        return (f"ℹ {d['label']} not judged yet — its last scheduled run "
                f"({due:%a %d %b %H:%M}) predates the heartbeat instrumentation "
                f"added {d['since'][:10]}; judged from its next run onwards")
    return (f"ℹ {d['label']} not judged yet — no scheduled run has passed "
            f"(cron {d['cron']})")


def gap_lines(today_entries, week_entries, athletes, now=None, audit=None) -> tuple[list[str], list[str]]:
    """(all gap lines, the subset of DAILY misses that may interrupt the coach).

    Gap lines report the absence of a SUCCESSFUL heartbeat — either nothing was
    recorded at all, or what was recorded was a failure. Those were separate
    concepts until 28 Jul 2026 and the split was the bug: a recorded failure
    produced no gap, so the only quiet consequence of a failed deliverable was a
    log line. _saw() now merges them; see its docstring.

    What counts as a deliverable, its window, and whether it may Telegram all
    come from coach_alert.DELIVERABLES. WEEKLY telegram routing is deliberately
    NOT done here — see weekly_alerts() below for why it needs a different
    cooldown key.

    Changed 28 Jul 2026 — NOTHING IS JUDGED BEFORE IT IS DUE. A deliverable is
    only checked once coach_alert.due_status() says its scheduled time has passed
    AND that occurrence is after its instrumentation date. backup-config runs at
    23:50 and this digest runs at 21:30, so registering it as "must appear today"
    made it report missing every single night for ever; stage1-plan's 7-day window
    reached back to a Sunday two days before its heartbeat existed. Both were
    false alarms from working jobs on the alarm's first night. The gate is
    derived, not special-cased, so any future deliverable at any hour gets it.

    A deliverable whose due moment falls BEFORE today (i.e. it is scheduled after
    this digest runs) is judged over the window since that moment rather than
    "today" — so last night's 23:50 backup is checked tonight, giving real 24-hour
    detection instead of the alternative of never checking it at all.

    Changed 28 Jul 2026 — the due moment is DERIVED from the live crontab. `audit`
    is coach_alert.cron_audit(); with it, the schedule each deliverable is judged
    against is the line cron will actually run, and a deliverable with no cron line
    at all is not judged (NO_CRON_ENTRY) because it cannot run. Without it, the
    declared schedules are used — the hermetic test path and the fail-safe path.
    """
    now = now or datetime.now()
    day_start = datetime.combine(now.date(), time.min)
    lines, telegram = [], []
    active = {s: c for s, c in athletes.items() if c.get("active")}

    # ONE definition of "did it run", for every deliverable. There used to be two:
    # _saw() for new checks and an ok-only _ran() for the three original ones
    # (morning-checkin, daily-prescription, watchdog), kept to avoid disturbing
    # long-standing behaviour. Now that _saw() discounts FAILURE-class heartbeats
    # it is strictly the better of the two — ok-only would count a FINDING-class
    # ok=False as "did not run" and alarm on a working script — and keeping a
    # second answer to the same question is how the next blind spot gets in.
    # Verified a no-op for those three before removing it: across all 1047
    # historical entries their only ok=False details are "claude CLI exit 1" /
    # "exit -1" (FAILURE-class, so both functions agree), and daily-prescription
    # has never recorded ok=False at all.
    for d in coach_alert.DELIVERABLES:
        due, state = coach_alert.due_status(d, now, audit)
        if state != coach_alert.DUE:
            lines.append(not_due_line(d, due, state))
            continue
        if d["window"] == "weekly":
            entries, when = week_entries, f"in {WEEKLY_WINDOW_DAYS} days"
        elif due >= day_start:
            entries, when = today_entries, "today"
        else:
            # Scheduled after the digest: judge the cycle that HAS happened.
            entries = [e for e in week_entries
                       if str(e.get("ts", "")) >= due.isoformat(timespec="seconds")]
            when = f"since the {due:%H:%M} run on {due:%a %d %b}"
        targets = list(active) if d["per_athlete"] else [None]
        for slug in targets:
            # morning-checkin's own opt-out flag, unchanged.
            if d["script"] == "daily-prescription" and not active[slug].get("daily_prescription", True):
                continue
            if _saw(entries, d["script"], athlete=slug, detail=d.get("detail")):
                continue
            # "no successful X", not "no X heartbeat": a failed run DID leave a
            # heartbeat, and the old wording alongside its own ✗ line read as a bug.
            who = f" for {slug}" if slug else ""
            line = f"⚠ no successful {d['label']}{who} {when}"
            lines.append(line)
            if d["telegram"] and d["window"] == "daily":
                telegram.append(f"{d['label']}{who}")
    return lines, telegram


def weekly_alerts(week_entries, athletes, now=None, audit=None) -> list[str]:
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
    now = now or datetime.now()
    active = {s: c for s, c in athletes.items() if c.get("active")}
    alerted = []
    for d in coach_alert.DELIVERABLES:
        if d["window"] != "weekly" or not d["telegram"]:
            continue
        # Same due gate as gap_lines. stage1-plan was instrumented on 28 Jul and
        # its 7-day window reached back to the Sunday 26 Jul run — two days before
        # any heartbeat could exist — so it alarmed on night one about a job that
        # had never had the chance to report. NOT expected yet means neither send
        # NOR clear_cooldown: clearing on a cycle we did not actually check would
        # discard a cooldown banked for a real, still-unfixed miss. The first cycle
        # that is genuinely checked does the clearing.
        # NO_CRON_ENTRY lands here too, and correctly: a weekly deliverable whose
        # cron entry has been deleted must not Telegram, and must not clear a
        # cooldown banked for a real earlier miss either.
        if coach_alert.due_status(d, now, audit)[1] != coach_alert.DUE:
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

    now     = datetime.now()
    entries = all_entries()
    today   = todays_entries(entries)
    week    = since(entries, WEEKLY_WINDOW_DAYS)

    # Diff the deliverable registry against the live crontab, BOTH directions, and
    # derive every due time from what cron will actually do. Problems go first in
    # the digest so MAX_LINES truncation can never hide them, and to ops-alerts.log
    # via ops_log.alert under its own script name (not "coach-alert", which means
    # "the alarm could not reach Jamie"). Deliberately NOT a Telegram condition:
    # only the two Jamie approved may interrupt him, and registry drift is neither.
    audit = coach_alert.cron_audit()
    for problem in audit.problems:
        ops_log.alert("cron-audit", problem)

    lines = list(audit.problems)
    lines += build_digest(today, athletes)
    # One `now` for the whole run: gap_lines and weekly_alerts must agree on what
    # is due, and two datetime.now() calls either side of midnight would not.
    gaps, missing_deliverables = gap_lines(today, week, athletes, now=now, audit=audit)
    lines += gaps
    lines += unclassified_lines(today)
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

    weekly_alerts(week, athletes, now=now, audit=audit)

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
