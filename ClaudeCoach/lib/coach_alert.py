"""The ONLY place ClaudeCoach is allowed to interrupt the coach on Telegram.

Everything else in this codebase is log-only (ops_log.alert / log_outbound), and
that must stay true: ops_log.alert() is called from a dozen places and gains no
notify path, so nothing can start Telegramming by accident. If a new condition
should reach Jamie, it gets an entry in REASONS here and a call to send() —
there is no second route to read.

Jamie's decision, 28 Jul 2026: exactly TWO conditions may interrupt him.

  1. DELIVERABLE_MISSING — a named deliverable (daily OR weekly) did not happen.
  2. CLAUDE_AUTH_FAILED  — the Claude CLI could not authenticate in production.

Deliberately NOT here, and log-only as a result:
  - git sync stuck (commits piling up locally). It was the one pre-existing
    Telegram path; it is removed because it is not one of the two. The 24-27 Jul
    push failures now surface as a digest ✗ line via the new failures-in-window
    rule in ops_log, not as a message. Flipping it back is one REASONS entry.

Changed 28 Jul 2026: the WEEKLY deliverables (weekly summary, weekly plan) are
now routed to Telegram too — the owner approved this after a weekly-summary
build crashed silently for three weeks with nothing but a log line to show for
it. Both now carry telegram=True below. Because the weekly gap check reads a
7-day window, a single Sunday miss stays "missing" for up to a week; routing it
through the same per-day key as the daily check would Telegram every evening
until it is fixed (up to 7 messages). ops-digest.py's weekly_alerts() sends
these separately from the daily path, on a stable per-script key with a cooldown
that spans the whole window, and explicitly clears that cooldown the moment the
deliverable is seen again — so one miss is one message, and the FOLLOWING
week's occurrence isn't silenced by a leftover cooldown.

Environment:
  CC_COACH_CHAT_ID  — where coach alerts go. Unset = notify.py's config default.
  CC_ALERT_DRY_RUN  — set to 1 and nothing is sent; the rendered text is written
                      to ops-alerts.log via log_outbound(sent=False) instead.
                      This is how the alert path is exercised without messaging
                      a real Telegram thread. The test suite forces it on for the
                      whole session (ironman-analysis/conftest.py) AND send() has an
                      independent under-test guard that raises rather than shelling
                      out to notify.py — belt and braces, because on 28 Jul 2026 a
                      single mechanism let a unit test message the coach for real.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import ops_log

# The real stdlib module, captured before any test can rebind the `subprocess`
# global on this module. The under-test guard below compares the two: a test that
# has substituted its own stub is PROVABLY unable to reach Telegram and is allowed
# through; an unstubbed test is not.
_REAL_SUBPROCESS = subprocess

# --- the routing table -------------------------------------------------------

DELIVERABLE_MISSING = "deliverable_missing"
CLAUDE_AUTH_FAILED  = "claude_auth_failed"

# reason -> how loud, and how often at most. Cooldown exists because auth
# failures recur on EVERY claude_call (many per hour) and a missing deliverable
# is re-detected by every digest run until it is fixed; the coach needs the fact
# once, not once per occurrence.
REASONS = {
    DELIVERABLE_MISSING: {"cooldown_h": 12},
    CLAUDE_AUTH_FAILED:  {"cooldown_h": 6},
}

# Named deliverables the gap-check watches. `telegram` is the routing decision
# and lives here rather than in ops-digest so one file answers "what can reach
# Jamie". `window`: daily = must appear today; weekly = must appear in 7 days.
# `detail` (optional) narrows what counts as the heartbeat.
#
# Added 28 Jul 2026 — `cron` and `since`, the two fields that stop this table
# alarming about something that could not possibly have happened yet.
#
# `cron` — the deliverable's REAL crontab schedule. The gap check now asks "when
#   was this last DUE?" and judges the deliverable only once that moment has
#   passed. Without it, backup-config (23:50) was registered as "must appear
#   today" and checked by a digest that runs at 21:30 — 2h20m too early — so it
#   reported missing every night, in perpetuity. Declaring the schedule fixes
#   that for every deliverable at once instead of special-casing this one, and a
#   deliverable added later with an awkward time gets the same treatment free.
#   `cron_cmd` identifies its line in the live crontab: a test cross-checks the
#   declared schedule against the real one, so a WRONG declaration fails the
#   build rather than quietly mis-timing the check for ever.
#
#   As of the cron-derived audit below, `cron` is only the FALLBACK. At run time
#   the due time is derived from the live `crontab -l` line matched on `cron_cmd`,
#   and a declaration that disagrees with reality is reported loudly while
#   reality is used. The declaration survives for one reason: if the crontab
#   cannot be read at all, the alarm keeps working off it rather than going quiet.
#
# `no_cron` (optional) — "this deliverable legitimately has no crontab entry, and
#   here is why". The ONLY way to register something unscheduled without the
#   audit reporting it, so the exemption is a reviewed decision in the diff rather
#   than a silent gap. Nothing uses it today; it exists so that a deliverable
#   triggered by some other mechanism does not have to be smuggled past the audit.
#
# `since` — when this deliverable's heartbeat instrumentation went live (the
#   moment it landed on prod `main`, which IS the runtime here — NOT the moment
#   it was committed on a branch. The distinction is load-bearing: 83bb541 was
#   committed 11:37 and merged 11:44, and the session-sync cron launched at 11:40
#   in between, so it ran the OLD uninstrumented code and left no heartbeat. A
#   `since` of 11:37 called that a genuine gap; 11:44 correctly calls it
#   pre-instrumentation. For the older deliverables it is the first heartbeat on
#   disk). An
#   occurrence scheduled BEFORE that moment cannot have left a heartbeat, so it
#   is not judged. Without it stage1-plan — instrumented 28 Jul 11:44 — was
#   measured over a 7-day window reaching back to its Sunday 26 Jul run, two days
#   before any heartbeat could exist, and alarmed about a job that never had the
#   chance to report. FIVE of the ten deliverables below were instrumented on
#   28 Jul and every one of them had this fault (night-before-brief,
#   capture-reminder, session-sync, backup-config, stage1-plan — zero entries
#   between them in 1075 lines of run-status history), not just the one spotted.
#   The alternative fix — seeding heartbeats into run-status.jsonl — was rejected:
#   that file is the audit trail, and writing runs into it that never happened
#   corrupts the only record of what the system actually did.
DELIVERABLES = [
    {"script": "morning-checkin",    "label": "morning card",       "window": "daily",
     "per_athlete": True,  "telegram": True,  "detail": "card sent",
     "cron": "*/30 6-9 * * *",   "cron_cmd": "morning-checkin.py",
     "since": "2026-06-10T00:00:00"},
    {"script": "daily-prescription", "label": "daily prescription", "window": "daily",
     "per_athlete": True,  "telegram": True,
     "cron": "0 5 * * *",        "cron_cmd": "daily-prescription.py",
     "since": "2026-06-10T00:00:00"},
    {"script": "night-before-brief", "label": "night-before brief", "window": "daily",
     "per_athlete": True,  "telegram": True,
     "cron": "30 20 * * *",      "cron_cmd": "night-before-brief.py",
     "since": "2026-07-28T11:44:37"},   # ff6fba3
    {"script": "evening-checkin",    "label": "evening check-in",   "window": "daily",
     "per_athlete": True,  "telegram": True,
     "cron": "0 21 * * *",       "cron_cmd": "evening-checkin.py",
     "since": "2026-06-10T00:00:00"},
    # Internal plumbing and nudges — a gap is worth a digest line, not a message.
    # capture-reminder was DEREGISTERED here on 28 Jul 2026 (commit 2ba93c0, "one
    # evening message"). Its 20:10 cron entry is gone, capture-reminder.py's main()
    # is now a deliberate no-op, and the capture ask is Case A2 of the 21:00
    # evening-checkin. Left registered it would have produced "⚠ no successful
    # capture reminder for <athlete> today" every night for ever — a permanent
    # false gap line, which is the same defect this change exists to remove.
    #
    # Nothing is lost by removing it rather than re-pointing it at
    # evening-checkin's 21:00 slot: evening-checkin is ALREADY a deliverable on
    # that exact schedule, and it is telegram=True where capture-reminder was
    # telegram=False — so the work is monitored more strictly than before, not
    # less. Re-pointing would instead create two deliverables that can only ever
    # succeed or fail together: one root cause, two digest lines, which is the
    # anti-pattern argued against on stage1-plan's per_athlete note above.
    #
    # evening-checkin's _record() still writes a vestigial "capture-reminder"
    # heartbeat (it was added so this registry entry would not gap before it could
    # be removed). Nothing reads it now, and it is harmless: capture-reminder stays
    # in OUTCOME_CLASS below, so if one ever arrives ok=False it is classified as a
    # FAILURE rather than surfacing as an UNCLASSIFIED digest line.
    # session-sync's LAST occurrence of the day (21:40) is after the digest, which
    # looks like backup-config's fault and is not: earlier occurrences the same day
    # (07:40 onwards) are already behind us at 21:30, so last_due() finds 19:40 and
    # the day's heartbeat is genuinely expected. Only a job whose FIRST occurrence
    # of the cycle is still ahead is un-judgeable.
    {"script": "session-sync",       "label": "session sync",       "window": "daily",
     "per_athlete": True,  "telegram": False,
     "cron": "40 7-22/2 * * *",   "cron_cmd": "session-sync.py",
     "since": "2026-07-28T11:44:37"},   # ff6fba3
    {"script": "watchdog",           "label": "watchdog",           "window": "daily",
     "per_athlete": False, "telegram": False,
     "cron": "30 5 * * *",       "cron_cmd": "watchdog.py",
     "since": "2026-06-10T00:00:00"},
    # 28 Jul 2026: the only backup of config/athletes.json (intervals.icu API
    # keys), nightly at 23:50 via lib_git_alert.sh's git_sync_ok/git_sync_fail —
    # the job label there IS "backup-config" (checked in backup-config.sh, not
    # assumed). Heartbeat comes from ops_log.sync_ok/sync_failure, not a direct
    # record_run() call in the script itself; sync_ok now also writes the
    # success heartbeat (see its docstring) so a clean run doesn't read as a gap.
    #
    # 23:50 is AFTER the 21:30 digest, so this is the deliverable that proved the
    # gap check needed a due time at all. It is now judged on its PREVIOUS cycle:
    # at 21:30 the last due moment is last night's 23:50, so a genuinely failed
    # backup is caught within 24 hours rather than never. Not rescheduled: moving
    # the cron entry is the special case, would edit a script owned by other work,
    # and the next late deliverable would reintroduce the bug.
    #
    # detail="sync ok" is LOAD-BEARING, not decoration. sync_failure's FIRST
    # consecutive failure records ok=True with detail "transient git-sync failure
    # (1st): ..." on the reasoning that the next tick will heal it. For the five
    # jobs that tick every 15-30 minutes that is sound; for a job that runs ONCE
    # at 23:50 the next tick is 24 hours away, so that ok=True would report a
    # night with NO backup as a clean run and make this whole registration
    # cosmetic. Pinning the detail to the exact string sync_ok writes (and only
    # sync_ok writes) means only a genuine success satisfies the check. Narrowing
    # here rather than changing sync_failure keeps the blast radius to this one
    # job instead of all five that share the helper.
    {"script": "backup-config",      "label": "config backup",      "window": "daily",
     "per_athlete": False, "telegram": True, "detail": "sync ok",
     "cron": "50 23 * * *",      "cron_cmd": "backup-config.sh",
     "since": "2026-07-28T13:00:37"},   # 5eaae85
    # 28 Jul 2026 — the two live jobs the cron-derived audit itself found on its
    # first run, closing the CRON AUDIT finding logged in docs/failure-alarm.md
    # ("Open finding" section). Both use `detail: "sync ok"` for the same reason
    # as backup-config above: sync_failure's first consecutive failure records
    # ok=True ("transient ... usually self-heals"), which is sound for a job
    # ticking every few minutes but would mask a genuinely missed once-nightly
    # run if the check accepted ANY heartbeat as success.
    #
    # sync-private-repo.sh (23:20 nightly) is the only versioned backup of
    # athletes/ now that the public repo was cleaned (2 Jul 2026) — the same
    # class of "only copy of something" as backup-config, twenty minutes
    # earlier in the crontab. Registered exactly like backup-config: telegram
    # eligible (a dead nightly mirror is condition 1, a missed named
    # deliverable), judged on the PREVIOUS cycle because 23:20 is after the
    # 21:30 digest. `script` here is "sync-private" — the job label
    # lib_git_alert.sh's git_sync_ok/git_sync_fail actually pass to
    # ops_log — NOT the script filename; get that wrong and this is a false
    # registration that can never see a heartbeat.
    {"script": "sync-private",        "label": "private repo sync",  "window": "daily",
     "per_athlete": False, "telegram": True, "detail": "sync ok",
     "cron": "20 23 * * *",      "cron_cmd": "sync-private-repo.sh",
     "since": "2026-07-28T16:21:45"},   # merged to main (was 5eaae85 commit time)
                                        # its heartbeat write, for every job
                                        # that calls it, not just backup-config.
    # activity-watcher.py (every 5 min, "2-57/5 * * * *") is the busiest
    # athlete-facing sender in the system — every activity debrief, chart
    # photo, segment PB, fuelling check and test-due nudge goes through it. If
    # it dies, athletes simply hear nothing; that silence is invisible without
    # this entry.
    #
    # Its heartbeats do NOT fit the daily/weekly shape the other entries use.
    # Every existing ops_log call in the script is CONDITIONAL — a heat-credit
    # success, a Telegram send failure, a stuck-timeout escalation — so the
    # common case (12 ticks/hour, no new activity for anyone) wrote NOTHING to
    # run-status.jsonl at all. There was no positive "it ran" signal to check,
    # only failure signals for "it tried to send", and a dead process leaves no
    # failure either. Fixed at the source (28 Jul 2026): one unconditional
    # `ops_log.record_run("activity-watcher", ok=True, detail="cycle
    # complete")` per cron invocation, added at the end of main() in
    # activity-watcher.py, AFTER the shared lock is released — see that
    # script's own comment. One per invocation, not per athlete: 12/hour is
    # already the right order of magnitude against run-status.jsonl's 6000-
    # line cap over the 7-day weekly window (see docs/failure-alarm.md
    # "Retention"); per-athlete would multiply it for no gain, since a shared
    # cron process either reaches that line or it does not.
    #
    # `window: "rolling"` + `window_minutes` (not "daily") because "did it run
    # at least once TODAY" is far too lax for a 5-min job — a watcher dead
    # since 00:05 would still pass a check phrased that way at 21:30. Instead
    # gap_lines() checks for a "cycle complete" heartbeat within the last
    # `window_minutes`. 60 minutes tolerates the LOCK_FILE staleness margin
    # (20 min — another cycle can legitimately still be running) plus a slow
    # cycle or two (multiple athletes each up to a 300s Claude timeout), while
    # still catching a genuinely dead watcher within the hour rather than
    # within a whole day.
    #
    # telegram: False — this is plumbing, not a named deliverable to an
    # athlete or the coach; Jamie's two approved conditions are a missed named
    # deliverable and dead Claude auth, and "the watcher had a rough hour" is
    # neither. A stalled watcher already escalates its OWN loud failure
    # separately (2 consecutive Claude timeouts -> ops_log.alert), which IS
    # classified below; this entry exists only to catch the death that
    # produces no failure at all.
    {"script": "activity-watcher",    "label": "activity watcher heartbeat",
     "window": "rolling", "window_minutes": 60,
     "per_athlete": False, "telegram": False, "detail": "cycle complete",
     "cron": "2-57/5 * * * *",  "cron_cmd": "activity-watcher.py",
     # Placeholder — this is the moment the heartbeat write landed on THIS
     # BRANCH (fix/register-watched-jobs), not yet on prod main. Per the
     # convention above (83bb541's lesson: since = merge time, not commit
     # time), correct this to the actual merge-to-main commit's timestamp when
     # this is promoted, or every occurrence between now and the real merge is
     # wrongly judged PRE_INSTRUMENTATION-only-until-then instead of DUE.
     "since": "2026-07-28T16:21:45"},
    # Sunday jobs. Checked over 7 days. telegram=True since 28 Jul 2026, but NOT
    # via this per-day cooldown — ops-digest.py's weekly_alerts() sends these on
    # its own occurrence-based key so one miss is one message, not one per evening.
    {"script": "weekly-summary",     "label": "weekly summary",     "window": "weekly",
     "per_athlete": True,  "telegram": True,
     "cron": "0 20 * * 0",       "cron_cmd": "weekly-summary.sh",
     "since": "2026-07-12T00:00:00"},
    # per_athlete stays True even though one crashed Sunday build is one root
    # cause: weekly_alerts() already collapses it to ONE message that NAMES the
    # affected athletes, so the cost is a more useful message body, not three
    # messages. weekly-plan.sh invokes stage1-plan.py once per athlete, so a
    # single-athlete failure is a real shape that per_athlete=False would hide.
    {"script": "stage1-plan",        "label": "weekly plan",        "window": "weekly",
     "per_athlete": True,  "telegram": True,
     "cron": "0 18 * * 0",       "cron_cmd": "weekly-plan.sh",
     "since": "2026-07-28T11:44:37"},   # ff6fba3
]

# The digest's own schedule. Declared so the crontab cross-check can prove what
# this whole mechanism assumes: that "now" when the check runs is 21:30.
DIGEST_CRON = "30 21 * * *"


# --- the registry is DERIVED from the crontab, not trusted --------------------
#
# WHY THIS EXISTS. DELIVERABLES above was hand-maintained and drifted TWICE in one
# day, in both possible directions:
#
#   1. backup-config was registered as a daily deliverable with a hand-declared
#      time, checked by a digest that runs 2h20m earlier than the job — it could
#      never have run when checked, and would have alarmed every night for ever.
#   2. capture-reminder stayed registered after its cron entry was deleted, so it
#      would have printed a false gap line every night for ever.
#
# Both were caught by eye. A third would not be. From 28 Jul 2026 the crontab is
# read at ALARM-RUN time and diffed against this registry, keyed on the script
# filename, and any mismatch is reported loudly into ops-alerts.log.
#
# THE DIRECTION THAT MATTERS MOST is the one nobody thought of: a cron entry with
# NO registry entry. That is a scheduled job nobody watches, which is how the
# weekly report vanished for three weeks with nothing but a log line to show for
# it. Registry->cron drift produces a noisy false alarm; cron->registry drift
# produces SILENCE, which is worse. Both are checked here.
#
# AUTHORITATIVE SOURCE: `crontab -l` for root, the actual thing cron executes.
# NOT ClaudeCoach/system/crontab.template — that is a sanitised rebuild reference
# and is itself demonstrably stale (28 Jul 2026: it still lists capture-reminder,
# has five wrong times and is missing plan_audit.py entirely). Diffing against
# the template would have "verified" the registry against another hand-maintained
# file, i.e. reproduced the bug one layer down.

CRON_SOURCE = ["crontab", "-l"]

# A crontab line is ClaudeCoach's if the script it runs lives under one of these.
# Path-based rather than name-based on purpose: it catches lib/plan_audit.py
# (which a `/scripts/` rule would miss), catches cc-gitpull.sh (which lives in
# /usr/local/bin, not the repo), and excludes the expense-bot entry — a different
# product sharing the same crontab — without naming it.
CC_PATH_MARKERS = ("/ClaudeCoach/", "/usr/local/bin/cc-")

# Tokens to step over when working out WHICH script a cron line runs. `cc-run` is
# the token-injecting wrapper every entry goes through (/root/.claude/cc-run).
_CRON_WRAPPERS = {"cc-run"}
_CRON_INTERPRETERS = {"bash", "sh", "python", "python3", "env"}

# --- WHICH SCHEDULED JOBS LEGITIMATELY NEED NO DELIVERABLE -------------------
#
# This table is the whole point of requirement 3: the alternative to it is an
# implicit rule in someone's head about which jobs are "just plumbing", which is
# how a job stops being watched without anyone deciding that it should. A cron
# entry that is neither registered in DELIVERABLES nor listed here is reported
# loudly every night until somebody makes a decision about it.
#
# The reason strings are checkable against the code, not vibes — each says why
# there is nothing for the gap check to watch, and each was verified by grepping
# the script for ops_log usage on 28 Jul 2026.
#
# A name here that has NO live cron entry is also reported: a stale exemption is
# the same class of drift as a stale registration.
CRON_EXEMPT = {
    "ops-digest.py":
        "this alarm itself. Its schedule is declared as DIGEST_CRON and "
        "cross-checked against the live crontab by cron_audit() — a deliverable "
        "entry would ask the digest to detect its own absence, which it cannot.",
    "cc-gitpull.sh":
        "git plumbing, every 30 min. Its failures already reach the digest via "
        "ops_log.sync_failure's escalation counter (OUTCOME_CLASS: cc-gitpull), "
        "and a single missed pull is self-healing on the next tick.",
    "bot-watchdog.py":
        "the watchdog OF the Telegram bots, every 5 min. It only speaks when "
        "something is wrong (ops_log.alert, no record_run heartbeat at all), so "
        "there is no success heartbeat for a gap check to look for.",
    "refresh-public-data.py":
        "regenerates the public site JSON hourly. No ops_log instrumentation and "
        "no coach-facing output: a failure degrades the public dashboard, not the "
        "coaching, and shows up as stale data on the page itself.",
    "refresh-site-data.py":
        "same class as refresh-public-data — site data regeneration, no ops_log "
        "instrumentation, nothing delivered to an athlete or the coach.",
    "bug-fixer.py":
        "unattended overnight rule maintenance. Everything it does is ALREADY a "
        "digest line by construction: bug-fixer-automerge run-status entries are "
        "printed by build_digest even when ok=True, and its findings go out as "
        "rules-lint alerts (FINDING-class).",
    "plan_audit.py":
        "plan invariant checking, FINDING-class and baseline-gated. Its whole "
        "output is digest lines; a missed run means one day without an audit, "
        "not a missed deliverable to an athlete.",
}

UNVERIFIED = ("\u26a0 CRON AUDIT: could not read `crontab -l` \u2014 the deliverable "
              "registry was NOT verified against the live schedule this run. The gap "
              "check is still running off the static registry in coach_alert.py, so "
              "nothing is silently switched off, but a drifted registry would not be "
              "caught until this is fixed.")


class CronAudit:
    """The result of diffing DELIVERABLES against the live crontab.

    `verified`  — False only when the crontab could not be read at all. False
                  means "judge off the static registry and say so loudly",
                  NEVER "disable the alarm" and never "alarm on everything".
    `schedules` — script filename -> live cron spec, for ClaudeCoach entries with
                  a spec this parser can read. This is what due times are DERIVED
                  from, so a hand-declared time cannot mis-time the check.
    `present`   — every ClaudeCoach script filename seen in the crontab, including
                  ones whose spec was unreadable or duplicated. Kept separate from
                  `schedules` so "scheduled, spec unusable" (fall back to the
                  declared time and keep checking) is distinguishable from "not
                  scheduled at all" (do not check; it cannot run).
    `problems`  — loud lines for ops-alerts.log and the digest. NEVER Telegram:
                  registry drift is an engineering fault, not one of the two
                  conditions Jamie approved for interrupting him.
    """

    def __init__(self, verified: bool, schedules: dict, present: set, problems: list):
        self.verified = verified
        self.schedules = schedules
        self.present = present
        self.problems = problems


def _cron_command_tokens(command: str) -> list:
    """Command tokens up to the first shell redirection or pipe.

    Stops at `>>`, `>`, `2>&1` and `|` so the log path in `... >> ~/Library/Logs/
    ClaudeCoach/backup.log 2>&1` is never mistaken for the script being run.
    """
    out = []
    for tok in command.split():
        if ">" in tok or tok.startswith(("|", "&", "<")):
            break
        out.append(tok)
    return out


def cron_job_name(command: str):
    """(script path, script filename) for one cron command, or (None, None).

    Steps over the cc-run wrapper, known interpreters, `VAR=value` prefixes and
    `-flags` to find the script. Returns the filename because that is the only
    stable key shared by the crontab and the registry — and because keying on the
    basename means `watchdog.py` and `bot-watchdog.py` cannot be confused, which a
    substring match over the raw line can.
    """
    for tok in _cron_command_tokens(command):
        base = tok.rsplit("/", 1)[-1]
        if base in _CRON_WRAPPERS or base in _CRON_INTERPRETERS:
            continue
        if tok.startswith("-") or "=" in base:
            continue
        return tok, base
    return None, None


def _cc_owned(path: str) -> bool:
    return bool(path) and any(m in path for m in CC_PATH_MARKERS)


def parse_crontab(text: str) -> tuple:
    """(schedules, present, problems) for the ClaudeCoach entries in `text`.

    Robust by requirement, because this crontab contains all of it: comment
    banners, a commented-OUT job (`# 0 5 * * 1 tar -zcf ...`), a foreign
    entry (expense-bot), the cc-run wrapper, `bash script.sh` and
    `python3 script.py --flag` forms, and shell redirections. Nothing here raises;
    anything it cannot understand becomes a loud problem line instead, because an
    exception in the audit would take the whole 21:30 digest down with it and the
    alarm would go quiet — the failure mode this file exists to prevent.
    """
    seen, present, problems = {}, set(), []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue                      # comments AND commented-out jobs
        fields = line.split()
        # crontab furniture: MAILTO="", PATH=/usr/bin, SHELL=/bin/bash
        if "=" in fields[0] and not fields[0].startswith("*"):
            continue
        if line.startswith("@"):          # @daily / @reboot nicknames
            path, name = cron_job_name(" ".join(fields[1:]))
            if _cc_owned(path):
                present.add(name)
                problems.append(
                    f"\u26a0 CRON AUDIT: {name} is scheduled with the cron nickname "
                    f"{fields[0]!r}, which this parser cannot turn into a due time. "
                    f"Give it an explicit 5-field schedule.")
            continue
        if len(fields) < 6:
            if any(m in line for m in CC_PATH_MARKERS):
                problems.append(f"\u26a0 CRON AUDIT: unreadable ClaudeCoach crontab "
                                f"line: {line[:120]!r}")
            continue
        path, name = cron_job_name(" ".join(fields[5:]))
        if not _cc_owned(path):
            continue                      # expense-bot, system jobs: not ours
        present.add(name)
        spec = " ".join(fields[:5])
        try:
            parse_cron(spec)
        except ValueError as exc:
            problems.append(f"\u26a0 CRON AUDIT: cannot read {name}'s live schedule "
                            f"{spec!r}: {exc}. Falling back to its declared time.")
            continue
        seen.setdefault(name, []).append(spec)

    schedules = {}
    for name, specs in sorted(seen.items()):
        schedules[name] = specs[0]
        if len(specs) > 1:
            # Keep the first so the check keeps working rather than going quiet,
            # and say so: the union of two schedules is the real answer and this
            # is only an approximation of it.
            problems.append(
                f"\u26a0 CRON AUDIT: {name} has {len(specs)} live crontab entries "
                f"({'; '.join(specs)}). Due times are derived from the first only, "
                f"so the check may be mis-timed — give the job one schedule.")
    return schedules, present, problems


def _registry_problems(schedules: dict, present: set) -> list:
    """Both directions of the DELIVERABLES <-> crontab diff."""
    problems = []
    registered = {}
    for d in DELIVERABLES:
        registered[d["cron_cmd"]] = d
        live, why = schedules.get(d["cron_cmd"]), d.get("no_cron")
        scheduled = d["cron_cmd"] in present
        if not scheduled:
            if not why:
                # Direction 1: capture-reminder's fault. The deliverable is NOT
                # judged (see due_status) because a job with no cron entry cannot
                # run, and alarming that it did not is a false alarm about a
                # registry bug.
                problems.append(
                    f"\u26a0 CRON AUDIT: {d['label']} ({d['script']}) is registered as a "
                    f"deliverable but {d['cron_cmd']} has NO live crontab entry. It "
                    f"cannot run, so it is not being checked. Restore the cron entry, "
                    f"deregister it, or annotate it no_cron=\"why\".")
            continue
        if why:
            problems.append(
                f"\u26a0 CRON AUDIT: {d['script']} is annotated no_cron ({why!r}) but "
                f"{d['cron_cmd']} IS in the live crontab ({live or 'schedule unreadable'}). "
                f"The annotation is stale — remove it so the real schedule is used.")
            continue
        if live:
            try:
                same = parse_cron(live) == parse_cron(d["cron"])
            except ValueError:
                same = False
            if not same:
                problems.append(
                    f"\u26a0 CRON AUDIT: {d['script']} declares cron {d['cron']!r} but the "
                    f"crontab says {live!r}. The LIVE schedule is being used for the due "
                    f"time; fix the declaration.")

    # Direction 2 — the one that matters. A scheduled job with no registry entry
    # and no exemption is a job nobody watches.
    for name, spec in sorted(schedules.items()):
        if name in registered or name in CRON_EXEMPT:
            continue
        problems.append(
            f"\u26a0 CRON AUDIT: {name} runs on the live crontab ({spec}) but NOTHING "
            f"watches it \u2014 no coach_alert.DELIVERABLES entry and no CRON_EXEMPT "
            f"reason. Either register it as a deliverable or record why it needs no "
            f"deliverable.")

    for name, reason in sorted(CRON_EXEMPT.items()):
        if name not in present:
            problems.append(
                f"\u26a0 CRON AUDIT: {name} is listed in CRON_EXEMPT ({reason[:60]}...) "
                f"but has no live crontab entry. Remove the stale exemption.")

    # The mechanism's own timing assumption, verified rather than assumed.
    digest_live = schedules.get("ops-digest.py")
    if digest_live:
        try:
            if parse_cron(digest_live) != parse_cron(DIGEST_CRON):
                problems.append(
                    f"\u26a0 CRON AUDIT: this digest declares DIGEST_CRON {DIGEST_CRON!r} "
                    f"but runs at {digest_live!r} on the live crontab. Every due-time "
                    f"margin in this file is reasoned against the declared value.")
        except ValueError:
            pass
    return problems


def read_crontab():
    """Raw `crontab -l` text, or None if it cannot be read.

    None is the fail-SAFE signal, not an error: cron_audit() turns it into a loud
    "not verified" line and the gap check carries on off the static registry.
    """
    try:
        r = subprocess.run(CRON_SOURCE, capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout


def cron_audit(text=None) -> CronAudit:
    """Diff DELIVERABLES against the live crontab. Never raises, never Telegrams.

    Pass `text` to audit a crontab you already have (the tests do; it keeps them
    hermetic). Omit it and the live crontab is read. The read is deliberately NOT
    a function default anywhere else in this file: due_status() with no audit
    means "use the declared schedule", which is both the test path and the
    fail-safe path.
    """
    if text is None:
        text = read_crontab()
    if text is None:
        return CronAudit(False, {}, set(), [UNVERIFIED])
    try:
        schedules, present, problems = parse_crontab(text)
        if not present:
            # A readable crontab with not one ClaudeCoach job in it is not a
            # crontab where every deliverable has been deregistered — it is a
            # crontab we are looking at from the wrong account. Trusting it would
            # mark all ten deliverables NO_CRON_ENTRY and silence the alarm
            # completely for that run. Treated as unverified instead, which keeps
            # every deliverable judged on its declared schedule.
            return CronAudit(False, {}, set(),
                             [UNVERIFIED + " (`crontab -l` was readable but "
                              "contained no ClaudeCoach jobs at all.)"])
        problems = problems + _registry_problems(schedules, present)
    except Exception as exc:              # belt and braces: never take the digest down
        return CronAudit(False, {}, set(),
                         [f"\u26a0 CRON AUDIT: the audit itself failed ({exc!r}) \u2014 the "
                          f"registry was NOT verified; running off the static registry."])
    return CronAudit(True, schedules, present, problems)


def effective_cron(d: dict, audit: CronAudit = None) -> str:
    """The schedule a deliverable is judged against.

    The LIVE crontab line when there is one, the declared `cron` otherwise. This
    is requirement 4: the expected time is derived from what cron will actually
    do, so the backup-config class of bug (a hand-declared time the digest can
    never see satisfied) cannot recur \u2014 and if the declaration and reality
    disagree, reality wins and the declaration is reported.
    """
    if audit is not None:
        live = audit.schedules.get(d["cron_cmd"])
        if live:
            return live
    return d["cron"]


# --- when was a deliverable last DUE? ----------------------------------------
#
# THE FAULT THIS KILLS. The gap check asked "is there a heartbeat in this
# window" and never "could there be one yet". A deliverable whose scheduled time
# falls after the digest's answers no structurally, for ever, so a brand-new
# alarm cried wolf on night one about a job that was working fine.
#
# HOW A BAD TIME ADDED LATER CANNOT REPEAT IT. Three things, in order of
# strength. (1) Expectation is DERIVED from the declared schedule, so any time —
# 23:50, 03:00, Sunday-only — is handled by the same code path; there is no
# "correct" time to get wrong. (2) parse_cron RAISES on a spec it cannot read
# rather than guessing, and a test parses every entry, so an unreadable schedule
# fails the build. (3) A test cross-checks each declared `cron` against the LIVE
# crontab, so declaring 05:00 for a job that actually runs at 23:50 also fails
# the build. The remaining hole — registering a deliverable with no crontab entry
# at all — is closed by (3) too: no matching crontab line is a failure.

GRACE_MIN = 10   # an occurrence younger than this may still be running


def _cron_field(spec: str, lo: int, hi: int) -> set:
    """One cron field -> the set of values it matches.

    Supports `*`, `n`, `a-b`, `*/n`, `a-b/n` and comma lists of those — which
    covers every entry in this crontab. Anything else RAISES rather than
    guessing: a schedule this cannot read must fail the test suite, not silently
    produce a wrong due time and a nightly false alarm.
    """
    out = set()
    for part in str(spec).split(","):
        body, step = part, 1
        if "/" in part:
            body, _, s = part.partition("/")
            if not s.isdigit() or int(s) < 1:
                raise ValueError(f"unsupported cron step {part!r}")
            step = int(s)
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            a, _, b = body.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"unsupported cron range {part!r}")
            start, end = int(a), int(b)
        elif body.isdigit():
            start = int(body)
            end = hi if step > 1 else start   # vixie: `5/2` means `5-max/2`
        else:
            raise ValueError(f"unsupported cron field {part!r}")
        if not (lo <= start <= end <= hi):
            raise ValueError(f"cron field {part!r} outside {lo}-{hi}")
        out.update(range(start, end + 1, step))
    if not out:
        raise ValueError(f"cron field {spec!r} matches nothing")
    return out


def parse_cron(spec: str) -> dict:
    """{"minute", "hour", "dow"} as value sets.

    Raises on day-of-month / month restrictions: nothing here uses them and they
    would need a different last-occurrence walk, so accepting them silently would
    be the same class of bug this file is fixing.
    """
    fields = str(spec).split()
    if len(fields) != 5:
        raise ValueError(f"cron spec needs 5 fields, got {spec!r}")
    minute, hour, dom, month, dow = fields
    if (dom, month) != ("*", "*"):
        raise ValueError(f"day-of-month/month restrictions unsupported: {spec!r}")
    return {"minute": _cron_field(minute, 0, 59),
            "hour": _cron_field(hour, 0, 23),
            # cron allows both 0 and 7 for Sunday; normalise to 0.
            "dow": {0 if d == 7 else d for d in _cron_field(dow, 0, 7)}}


def last_due(spec: str, now: datetime):
    """The most recent scheduled occurrence at or before `now - GRACE_MIN`.

    The grace margin means a job scheduled a few minutes before the digest and
    still running is not reported missing — evening-checkin at 21:00 has the
    tightest real margin (30 min) and historically finishes in ~2 min, but the
    margin should not depend on that. None if nothing is scheduled in the
    preceding 8 days, which covers the weekly jobs.
    """
    cal = parse_cron(spec)
    cutoff = now - timedelta(minutes=GRACE_MIN)
    hours = sorted(cal["hour"], reverse=True)
    minutes = sorted(cal["minute"], reverse=True)
    for back in range(8):
        day = (cutoff - timedelta(days=back)).date()
        if (day.weekday() + 1) % 7 not in cal["dow"]:   # python Mon=0, cron Sun=0
            continue
        for h in hours:
            for m in minutes:
                moment = datetime.combine(day, time(h, m))
                if moment <= cutoff:
                    return moment
    return None


DUE                 = "due"
NOT_SCHEDULED_YET   = "not-scheduled-yet"
PRE_INSTRUMENTATION = "pre-instrumentation"
NO_CRON_ENTRY       = "no-cron-entry"


def due_status(d: dict, now: datetime = None, audit: CronAudit = None) -> tuple:
    """(due_moment, state) for one deliverable — may it be judged right now?

    Only DUE may produce a gap line or a Telegram. The other two states mean the
    absence of a heartbeat is EXPECTED, not a failure:

      NOT_SCHEDULED_YET   — no occurrence of this schedule lies behind us at all.
      PRE_INSTRUMENTATION — the last occurrence predates `since`, so no heartbeat
                            could exist for it. This ages out by itself: the
                            first occurrence after `since` is judged normally,
                            with no seeded heartbeats and no edit to the audit
                            trail. It is visible while it lasts — ops-digest
                            prints an "ℹ not judged yet" line naming the reason,
                            so a deliverable is never silently unchecked.
      NO_CRON_ENTRY       — we READ the crontab and this deliverable has no entry
                            in it. A job that is not scheduled cannot run, so
                            "no heartbeat" says nothing about the job and
                            everything about the registry: that is exactly the
                            capture-reminder false gap line. Reported loudly by
                            cron_audit() as a registry fault instead. Note the
                            asymmetry with an UNREADABLE crontab, which is not
                            this state — there we cannot tell, so we keep judging
                            off the declared schedule (fail safe) rather than
                            silently un-checking every deliverable at once.

    `audit` — the CronAudit from cron_audit(). Supplied, the due time is DERIVED
    from the live crontab. Omitted, the declared `cron` is used: that is both the
    hermetic test path and the fail-safe path when `crontab -l` is unreadable.
    """
    now = now or datetime.now()
    if (audit is not None and audit.verified
            and d["cron_cmd"] not in audit.present and not d.get("no_cron")):
        return None, NO_CRON_ENTRY
    due = last_due(effective_cron(d, audit), now)
    if due is None:
        return None, NOT_SCHEDULED_YET
    if due < datetime.fromisoformat(d["since"]):
        return due, PRE_INSTRUMENTATION
    return due, DUE

# --- is an ok=False heartbeat a FAILURE or a FINDING? ------------------------
#
# THE PROBLEM. ops-digest's gap check used to count ANY heartbeat as "it ran", so
# a job that failed and dutifully logged the failure produced no gap and no
# alert. Only a job that died before reaching ops_log tripped the alarm — the
# quiet failures stayed quiet, which is the whole class this alarm exists to kill.
#
# WHY IT IS NOT A ONE-LINE FIX. `ok=False` does not mean "failed". It means
# either "I failed" or "I ran fine and found something": weekly-summary wrote 8
# ok=False lines reading "realised TID excess_quality: realised easy share 46% vs
# target" — that is the script SUCCESSFULLY detecting training drift. Treat
# ok=False as failure naively and the new alarm fires on week one from a working
# script, which is how a new alarm gets ignored on week one.
#
# WHY A TABLE AND NOT A FIX AT THE CALL SITES. Two reasons, both hard:
#   1. The benign call site is ALREADY corrected. weekly-summary.py:250 records
#      ok=True ("training-balance note ready") as of 27 Jul 2026. The 8 ok=False
#      lines in run-status.jsonl are STALE DATA written by code that no longer
#      exists — and the 26 Jul pair is still inside the 7-day weekly window, so
#      history alone would fire this alarm on day one.
#   2. Every remaining benign ok=False producer lives in a file owned by other
#      concurrent work (plan_builder.py, weekly-summary.py, capture-reminder.py).
#      Correcting them here is not available.
# A read-side classification is therefore the only route, and it has the
# advantage of also classifying the six weeks of history correctly.
#
# WHY NOT A POSITIVE DELIVERY CHECK INSTEAD (the obvious alternative — extend the
# existing `"detail": "card sent"` mechanism to every deliverable): impossible for
# weekly-summary, which writes NO delivery heartbeat at all. Its only record_run
# is the drift note, so requiring a delivery detail would report it missing every
# single week. That gap is owned by another ticket; until it is closed, the
# absence of a failure is the only signal available for that script.
#
# THREE STATES, and what each does:
#   FAILURE       -> does NOT count as "it ran". Produces a gap line, and a
#                    Telegram if the deliverable is telegram=True.
#   FINDING       -> DOES count as "it ran". Digest ✗ line only, never alarms.
#   unclassified  -> does NOT alarm, and is NOT swallowed: ops-digest emits a
#                    loud "UNCLASSIFIED" digest line naming the script so the
#                    omission is visible, and test_ops_digest asserts every
#                    DELIVERABLES script is classified, so adding a monitored
#                    deliverable without classifying it fails the build.
#
# A NEW BENIGN CASE ADDED LATER. Two ways it stays safe. If it is added to a
# FINDING-class script it is benign by default. If it is added to a FAILURE-class
# script — the only genuinely dangerous case — the call site passes
# outcome=ops_log.FINDING and that wins over this table. That per-record override
# is why the table is a DEFAULT and not a verdict. A new script nobody classifies
# lands in `unclassified`: silent on Telegram, loud in the digest.
FAILURE = ops_log.FAILURE
FINDING = ops_log.FINDING

OUTCOME_CLASS = {
    # --- FAILURE: an ok=False line from these means the job did not do its job.
    # Every one verified against its call sites: the ok=False paths are Telegram
    # sends that failed after retry, unhandled exceptions, and claude CLI
    # non-zero exits (which is how a dead auth token hides).
    "morning-checkin":    FAILURE,
    "daily-prescription": FAILURE,
    "night-before-brief": FAILURE,   # "no <telegram> block in model output"
    "evening-checkin":    FAILURE,
    "capture-reminder":   FAILURE,   # "claude CLI exited N with no <notify> block"
    "session-sync":       FAILURE,
    "watchdog":           FAILURE,
    "backup-config":      FAILURE,   # via ops_log.sync_failure's non-transient branch
    "sync-private":       FAILURE,   # same helper, non-transient branch
    "activity-watcher":   FAILURE,   # "Telegram send failed after retry" /
                                     # "activity analysis timed out Nx in a row"
    "stage1-plan":        FAILURE,
    "bot-watchdog":       FAILURE,   # the bot stopped answering
    "claude_call":        FAILURE,   # auth expired in production
    "cc-gitpull":         FAILURE,
    "coach-alert":        FAILURE,   # the alarm itself failing to send
    # Registry drift is deliberately NOT filed under "coach-alert": that name
    # means "the alarm could not reach Jamie", and overloading it would make a
    # config fault indistinguishable from a delivery fault in the same log.
    "cron-audit":         FAILURE,   # DELIVERABLES has drifted from the crontab
    # --- FINDING: an ok=False line from these is the script working correctly and
    # reporting something about the TRAINING or the CONFIG, not about itself.
    # None of these is a missed deliverable; all of them are already a digest line.
    "weekly-summary":     FINDING,   # realised-TID drift; run-threshold staleness
    "plan_builder":       FINDING,   # 316 of the 411 historical ok=False lines:
                                     # "weekly_tss_cap check SKIPPED — no cap supplied"
    "plan_audit":         FINDING,   # plan invariant broke, already baseline-gated
    "rules-lint":         FINDING,   # "rule may withhold Swim high work"
    "thresholds":         FINDING,   # "zones may be stale-high"
    "selftest":           FINDING,   # "loud-path verification (not a real failure)"
}


def classify(entry: dict) -> str:
    """FAILURE / FINDING / "unclassified" for one run-status entry.

    ok=True is never either — it is a clean run. Precedence: the record's own
    `outcome` field (a call site that knows), then OUTCOME_CLASS (this file's
    default per script), then "unclassified".
    """
    if entry.get("ok"):
        return ""
    stamped = entry.get("outcome")
    if stamped in (FAILURE, FINDING):
        return stamped
    return OUTCOME_CLASS.get(entry.get("script"), "unclassified")


STATE = ops_log.LOG_DIR / "coach-alert-state.json"


def dry_run() -> bool:
    return os.environ.get("CC_ALERT_DRY_RUN", "") not in ("", "0", "false", "False")


class TelegramSendBlocked(RuntimeError):
    """A test reached the real send path. Raised, never swallowed — see under_test."""


def under_test() -> bool:
    """Is pytest in this process?

    Two independent signals because each covers a hole in the other:
      PYTEST_CURRENT_TEST — set by pytest per test item, so it is true even if
        coach_alert was imported long before pytest was (a plugin, a conftest).
      "pytest" in sys.modules — true during collection and inside session/module
        fixtures, where PYTEST_CURRENT_TEST is NOT set. A module-scope fixture that
        called send() would slip past the env var alone.
    Either is enough. Both are cheap.
    """
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    except Exception:
        pass


def _cooling_down(reason: str, key: str, now: datetime, cooldown_h: float = None) -> bool:
    hours = REASONS[reason]["cooldown_h"] if cooldown_h is None else cooldown_h
    last = _read_state().get(f"{reason}|{key}")
    if not last:
        return False
    try:
        return datetime.fromisoformat(last) > now - timedelta(hours=hours)
    except Exception:
        return False


def clear_cooldown(reason: str, key: str) -> None:
    """Forget a past send for this key. Used when an occurrence resolves (the
    deliverable is seen again), so the NEXT genuinely new occurrence of the same
    key alerts immediately instead of waiting out a cooldown banked for a miss
    that has since been fixed."""
    state = _read_state()
    if state.pop(f"{reason}|{key}", None) is not None:
        _write_state(state)


def send(reason: str, text: str, key: str = "", cooldown_h: float = None) -> str:
    """Interrupt the coach — if and only if `reason` is one of the two approved.

    `cooldown_h` overrides REASONS[reason]["cooldown_h"] for this call only —
    used by ops-digest.py's weekly_alerts() to bank a cooldown that spans the
    whole 7-day weekly window, rather than the shorter default tuned for the
    daily/auth-failure cases.

    Returns the action taken: "sent", "dry-run", "cooldown", "send-failed", or
    "refused" (an unlisted reason: logged loudly and NOT sent, so a future caller
    adding a third condition finds out in the log instead of silently messaging him).
    """
    now = datetime.now()
    if reason not in REASONS:
        ops_log.alert("coach-alert", f"REFUSED to Telegram an unapproved reason "
                                     f"{reason!r} — log-only. Text: {text}")
        return "refused"
    if _cooling_down(reason, key, now, cooldown_h):
        ops_log.log_outbound(f"coach-alert:{reason}", text, sent=False)
        return "cooldown"

    if dry_run():
        ops_log.log_outbound(f"coach-alert:{reason}", text, sent=False)
        return "dry-run"

    # --- THE GUARD: a test may never execute the real notify.py ---------------
    #
    # WHY IT EXISTS ALONGSIDE the CC_ALERT_DRY_RUN fixture in ironman-analysis/
    # conftest.py: on 28 Jul 2026 test_a_weekly_deliverable_with_no_cron_entry_
    # neither_alerts_nor_clears got here with no dry-run env and no subprocess stub
    # and delivered a real "ClaudeCoach did not deliver: weekly plan" message to the
    # coach, twice. A fixture only protects tests that inherit it; it cannot protect
    # a test that deliberately sets CC_ALERT_DRY_RUN=0 (several do, correctly), a
    # helper called from a session fixture, or a file added later under a different
    # conftest. This guard sits on the one line that can actually reach Telegram, so
    # it holds regardless of how the caller got here.
    #
    # WHY IT IS NOT A NO-OP. Returning "dry-run" here would make a suppressed send
    # indistinguishable from a send that was never warranted — the exact confusion
    # that would let a real routing bug pass the suite. It raises instead: the test
    # that would have messaged the coach FAILS, loudly, naming the reason. The
    # rendered text is written to the alert log first so the audit trail records what
    # would have gone out, and the same line goes to stderr, which survives a test
    # that has monkeypatched ALERT_LOG to a tmp_path (as that one had).
    #
    # WHY A STUBBED subprocess IS ALLOWED THROUGH. The cooldown-banking tests must
    # see send() take its real path and return "sent"/"send-failed"; they do it by
    # replacing this module's `subprocess` with a fake whose run() returns a chosen
    # returncode. That shape cannot reach the network by construction, so the
    # discriminator is "is the subprocess module still the real one", not "are we in
    # a test".
    if under_test() and subprocess is _REAL_SUBPROCESS:
        why = (f"coach_alert.send({reason!r}) was called under pytest with the real "
               f"subprocess module and CC_ALERT_DRY_RUN unset — this would have sent "
               f"a Telegram message to the coach. BLOCKED. Fix the test: set "
               f"CC_ALERT_DRY_RUN=1, or stub coach_alert.subprocess if it needs the "
               f"real send path. Text withheld: {text!r}")
        ops_log.log_outbound(f"coach-alert:{reason}", text, sent=False)
        print(f"BLOCKED SEND UNDER TEST: {why}", file=sys.stderr)
        raise TelegramSendBlocked(why)

    # Send FIRST, and only bank the cooldown if it actually went. Writing the
    # cooldown before the send would mean a Telegram API error silently burned the
    # next 6-12 hours of alerting — an alarm that fails quietly, which is the exact
    # failure class this whole file exists to kill.
    try:
        notify = Path(__file__).resolve().parent.parent / "telegram" / "notify.py"
        cmd = [sys.executable, str(notify), "--no-history"]
        chat = os.environ.get("CC_COACH_CHAT_ID", "")
        if chat:
            cmd += ["--chat-id", chat]
        cmd.append(text)
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        rc, err = r.returncode, (getattr(r, "stderr", b"") or b"")
    except Exception as exc:
        rc, err = -1, str(exc).encode()

    if rc != 0:
        # No cooldown banked: the next digest/claude_call tick tries again.
        ops_log.log_outbound(f"coach-alert:{reason}", text, sent=False)
        tail = err.decode(errors="replace")[-200:] if isinstance(err, bytes) else str(err)[-200:]
        ops_log.alert("coach-alert", f"notify FAILED (rc={rc}) for {reason}, "
                                     f"coach NOT told, will retry next tick: {tail}")
        return "send-failed"

    state = _read_state()
    state[f"{reason}|{key}"] = now.isoformat(timespec="seconds")
    _write_state(state)
    ops_log.log_outbound(f"coach-alert:{reason}", text, sent=True)
    return "sent"
