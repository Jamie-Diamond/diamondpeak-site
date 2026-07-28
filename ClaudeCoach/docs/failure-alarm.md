# Failure alarm — what interrupts the coach, and what doesn't (28 Jul 2026)

Before this, nothing told anyone when ClaudeCoach stopped working. A weekly
report crashed silently for three weeks and a git push failed for four days;
both went unnoticed because the only failure surface was a log file nobody had
a reason to open.

## The rule

Exactly **two** conditions reach Jamie on Telegram. Everything else is
log-only, in `~/Library/Logs/ClaudeCoach/ops-alerts.log` and
`run-status.jsonl`.

1. **A named deliverable did not happen** — no *successful* heartbeat for the
   morning card, the daily prescription, the night-before brief, the evening
   check-in (which since 28 Jul also carries the capture ask), the nightly config
   backup, the weekly summary or the weekly plan.
   Which ones qualify, and which may Telegram, is declared once in
   `coach_alert.DELIVERABLES`.
2. **The Claude CLI could not authenticate in production** — a
   `CLAUDE_CODE_OAUTH_TOKEN` was present and rejected, in a non-interactive
   run. Every Claude-authored surface shares that auth, so this is a total
   outage.

Both live in `lib/coach_alert.py`. `send()` **refuses** any reason not in
`REASONS` and logs the refusal, so nothing can start messaging him by accident,
and `lib/ops_log.py` has no notify path at all any more.

## Deliberately log-only

| Condition | Why not Telegram |
|---|---|
| git sync stuck / flapping | Not one of the two. Now detected properly (see below) and loud in the digest, but silent on Telegram. **This is the 24-27 Jul incident: it would still not reach him.** One `REASONS` entry flips it. |
| session-sync, watchdog gaps | Internal plumbing and nudges, not deliverables. (`capture-reminder` was retired on 28 Jul and deregistered — its work is now covered by `evening-checkin`, see below.) |
| `plan_audit` hard fails, under-training, everything else | Digest lines. |

## Heartbeats: silence vs absence

Several senders correctly send nothing on many days. That is recorded as a
**success** (`ok=True`, `detail="silent (...)"`), so legitimate silence never
reads as a missed deliverable.

| State | run-status | Digest | Telegram |
|---|---|---|---|
| ran, sent | `ok=True detail="sent"` | nothing | no |
| ran, correctly silent | `ok=True detail="silent (...)"` | nothing | no |
| ran, **found something** | `ok=False`, FINDING | ✗ line | no |
| ran, **failed** | `ok=False`, FAILURE | ✗ line **+ ⚠ gap line** | yes, if `telegram: True` |
| ran, ok=False, unclassified | `ok=False` | ⚠ UNCLASSIFIED line | no |
| no record at all | nothing | ⚠ gap line | yes, if `telegram: True` |

The trap is **empty output from a broken CLI looks exactly like correct
silence**. `capture-reminder.py` spawns the CLI directly and has no auth
detection at all, so a non-zero exit with no `<notify>` block records
`ok=False` — otherwise a dead token would read as "nothing to remind about"
every night, forever.

## The blind spot: `ok=False` meant two opposite things (28 Jul 2026)

`_saw()` used to count **any** heartbeat as "it ran", on the reasoning that a
recorded failure is already a ✗ line and a ⚠ as well is the same fact twice.
That reasoning was wrong where it mattered. The ✗ line is log-only, so a job
that failed and dutifully logged the failure produced no gap, no Telegram and no
consequence. **Only a job that died before reaching `ops_log` could alarm** — the
alarm caught crashes and missed failures, which is most of them.

It is not a one-line fix, because `ok=False` does not mean "failed":

```
2026-07-26T20:00:02  weekly-summary  ok=False  realised TID excess_quality:
                                     realised easy share 46% vs target 72%
```

That is the script **succeeding** at detecting training drift. Eight such lines
sit in `run-status.jsonl` from 12-26 July. Treat `ok=False` as failure naively
and the new alarm fires in week one from a working script — and the 26 Jul pair
is still inside the 7-day weekly window, so it would fire from *history* on day
one. That is how a new alarm gets ignored in week one.

**The fix is a read-side classification**, `coach_alert.OUTCOME_CLASS`, mapping
each script to `FAILURE` or `FINDING`, with a per-record `outcome` field on
`record_run()` that overrides it. Resolution is `coach_alert.classify()`.

- **FAILURE** → does not count as having run. Gap line, and Telegram if eligible.
- **FINDING** → counts as having run. ✗ digest line only, never alarms.
- **unclassified** → cannot alarm, but is *not* swallowed: a loud
  `⚠ UNCLASSIFIED ok=False from '<script>'` digest line names it, and
  `test_every_monitored_deliverable_is_classified` fails the build if a
  monitored deliverable has no classification.

Why classification rather than correcting the call sites — the obvious fix:

1. **The benign call site was already corrected.** `weekly-summary.py:250`
   records `ok=True` ("training-balance note ready") as of 27 Jul. Those eight
   `ok=False` lines are stale data written by code that no longer exists. Fixing
   forward does nothing about six weeks of history the weekly window still reads.
2. **The remaining benign producers are in other people's files.**
   `plan_builder.py` alone wrote 316 of the 411 historical `ok=False` lines
   ("weekly_tss_cap check SKIPPED — no cap supplied").

And why not the tempting alternative of a **positive** delivery check (extend
the existing `"detail": "card sent"` idea to every deliverable): impossible for
`weekly-summary`, which writes **no delivery heartbeat at all** — its only
`record_run` is the drift note. Requiring a delivery detail would report it
missing every single week. Until that gap is closed, the absence of a *failure*
is the only signal available for that script.

**A new benign case added later.** In a FINDING-class script it is benign by
default. In a FAILURE-class script — the one dangerous case — the call site
passes `outcome=ops_log.FINDING`, which beats the table. In a brand-new script
it lands in `unclassified`: silent on Telegram, loud in the digest.

**One definition of "did it run".** The ok-only `_ran()` used by the three
original checks is gone. Now that `_saw()` discounts FAILURE-class heartbeats it
is strictly better — ok-only would treat a FINDING as "did not run" and alarm on
a working script — and two answers to the same question is how the next blind
spot gets in. Verified a no-op before removal: across all 1047 historical
entries those three scripts' only `ok=False` details are `claude CLI exit 1` /
`exit -1` (FAILURE-class either way), and `daily-prescription` has never
recorded `ok=False` at all.

## The nightly config backup

`scripts/backup-config.sh` (23:50) produces the **only** backup of
`config/athletes.json`, which holds the intervals.icu API keys and is excluded
from the private mirror. It is registered with `window: "daily"`,
`per_athlete: False`, `telegram: True`.

Its heartbeat comes from `ops_log.sync_ok` / `sync_failure` via
`lib_git_alert.sh`, not a `record_run()` in the script — the job label there is
literally **`backup-config`** (read out of the script, not assumed; a wrong
string would report a false miss every night).

`detail: "sync ok"` on that entry is **load-bearing**. `sync_failure`'s first
consecutive failure records `ok=True` with `detail="transient git-sync failure
(1st): ..."`, on the reasoning that the next tick will heal it. For the jobs that
tick every 15-30 minutes that is sound; for a job that runs once at 23:50 the
next tick is 24 hours away, so that `ok=True` would report a night with **no
backup** as a clean run and make the registration cosmetic. Pinning the detail
to the exact string only `sync_ok` writes means only a genuine success satisfies
the check. Narrowing here rather than changing `sync_failure` keeps the blast
radius to this one job instead of all five sharing the helper.

## Nothing is judged before it could have happened (28 Jul 2026)

The alarm shipped with two false alarms that would both have fired on its first
night, from jobs that were working perfectly. They are the same fault seen from
two angles: **the gap check asked "is there a heartbeat in this window?" and
never "could there be one yet?"**

| The false alarm | Why it fired |
|---|---|
| `⚠ no successful config backup today` | `backup-config` runs at **23:50**. The digest runs at **21:30** — 2h20m *before* it. Registered as "must appear today", it reported missing every night, in perpetuity. |
| `⚠ no successful weekly plan for jamie/kathryn/calum in 7 days` | `stage1-plan`'s heartbeat was added at 11:44 on 28 Jul. Its 7-day window reached back to the Sunday 26 Jul run — two days before any heartbeat could exist. |

It was not two deliverables but **five**: `night-before-brief`,
`capture-reminder`, `session-sync`, `backup-config` and `stage1-plan` were all
instrumented on 28 Jul and had zero entries between them in 1075 lines of
`run-status.jsonl` history.

### The fix: a declared schedule and a declared instrumentation date

Every entry in `coach_alert.DELIVERABLES` now carries two more fields:

- **`cron`** — its real crontab schedule (plus `cron_cmd`, the command that
  identifies its crontab line).
- **`since`** — when its heartbeat instrumentation went **live on prod `main`**,
  which is the runtime here. Not the commit time: `83bb541` was committed at
  11:37 and merged at 11:44, and the `session-sync` cron launched at 11:40 in
  between on the old uninstrumented code. A `since` of 11:37 would have called
  that a genuine gap.

`coach_alert.due_status()` then answers *may this be judged right now?*

| State | Meaning | Gap line | Telegram |
|---|---|---|---|
| `due` | the last scheduled occurrence has passed, and is after `since` | ⚠ if missing | if `telegram: True` |
| `pre-instrumentation` | the last occurrence predates `since` | ℹ, naming when it starts being judged | never |
| `not-scheduled-yet` | no occurrence lies behind us at all | ℹ | never |

Two properties matter more than the mechanism:

**A job scheduled after the digest is judged on its PREVIOUS cycle, not
skipped.** At 21:30, `backup-config`'s last due moment is *last night's* 23:50,
so the window checked is "since the 23:50 run on <date>" and a genuinely failed
backup is caught within 24 hours. Skipping it would have made the whole
registration cosmetic — and this is the only backup of the intervals.icu keys.

**Not judged is never silent.** Every non-`due` state still prints an
`ℹ … not judged` digest line naming the reason, so a deliverable cannot sit
unchecked with nothing to show for it.

### Why a deliverable added later with a bad time cannot repeat this

Three defences, in increasing strength:

1. **Expectation is derived, not assumed.** Any schedule — 23:50, 03:00,
   Sunday-only — goes through the same code path. There is no "correct" time to
   get wrong.
2. **`parse_cron` raises rather than guesses.** A spec it cannot read fails
   `test_every_deliverable_declares_a_parseable_schedule_and_a_since`.
3. **The schedule is read from the live crontab at run time.** As of the
   cron-derived registry (below) the declared `cron` is only a fallback: every
   due time comes from the line cron will actually run, and a declaration that
   disagrees with reality is reported loudly while reality is used. The
   build-time check (`test_declared_schedules_match_the_live_crontab`) stays as
   a second net, but it is no longer the only one — it `pytest.skip`s where
   there is no crontab, and a skipped test defends nothing at 21:30.

Defence 3 earned itself immediately. It caught that `capture-reminder`'s cron
line (`10 20 * * *`) had been **removed from the live crontab** while this was
being written — present at 12:00, gone by 13:20 — by commit `2ba93c0` ("one
evening message"), which retired the 20:10 push and folded its ask into the
21:00 check-in as Case A2.

`capture-reminder` is therefore **deregistered**, not kept and disabled. Its
script's `main()` is now a deliberate no-op and it has no cron entry, so left
registered it would have produced `⚠ no successful capture reminder for
<athlete> today` every night for ever.

Nothing is lost by removing it rather than re-pointing it at the 21:00 slot:
`evening-checkin` is *already* a deliverable on that exact schedule, and it is
`telegram: True` where `capture-reminder` was `telegram: False` — so the capture
chase is monitored **more** strictly than before, not less. Re-pointing would
instead create two deliverables that can only ever succeed or fail together: one
root cause, two digest lines, the anti-pattern argued against under
`per_athlete` below. `evening-checkin._record()` still writes a vestigial
`capture-reminder` heartbeat; nothing reads it, and `capture-reminder` stays in
`OUTCOME_CLASS` so an `ok=False` one is classified rather than surfacing as an
`UNCLASSIFIED` digest line.

The general rule this settles: **a retired job is removed from `DELIVERABLES`,
never left registered and unscheduled.** A deliverable with no cron entry can
only ever produce a false gap line, and
`test_no_deliverable_is_registered_without_a_live_cron_entry` enforces it.

### What was NOT done

- **The crontab was not changed.** Moving `backup-config` before 21:30 is the
  special case, not the fix: it edits a script owned by other work and the next
  late deliverable reintroduces the bug.
- **No heartbeats were seeded into `run-status.jsonl`.** That file is the audit
  trail. Writing runs into it that never happened corrupts the only record of
  what the system actually did, to silence an alarm about a period that should
  simply not be judged. `since` ages out by itself on the next occurrence.

### The deliverable/cron/digest table

Every entry verified against the live crontab, digest at 21:30:

| Deliverable | cron | last due at 21:30 | judgeable |
|---|---|---|---|
| morning card | `*/30 6-9 * * *` | 09:30 today | yes |
| daily prescription | `0 5 * * *` | 05:00 today | yes |
| night-before brief | `30 20 * * *` | 20:30 today | yes |
| evening check-in | `0 21 * * *` | 21:00 today | yes — tightest margin (30 min; historically finishes in ~2 min, and `GRACE_MIN` keeps a still-running job from counting) |
| ~~capture reminder~~ | *(retired 28 Jul, `2ba93c0`)* | — | **deregistered** — folded into the 21:00 check-in, which is itself monitored |
| session sync | `40 7-22/2 * * *` | 19:40 today | yes — its *last* daily occurrence (21:40) is after the digest, which looks like the `backup-config` fault and is not: earlier occurrences the same day are already behind us |
| watchdog | `30 5 * * *` | 05:30 today | yes |
| config backup | `50 23 * * *` | **23:50 yesterday** | yes, on the previous cycle |
| weekly summary | `0 20 * * 0` | Sunday 20:00 | yes |
| weekly plan | `0 18 * * 0` | Sunday 18:00 | yes, from Sunday 2 Aug (instrumented 28 Jul) |

### `per_athlete` on a single-root-cause job

`stage1-plan` keeps `per_athlete: True`. `weekly_alerts()` already collapses a
whole-job failure into **one** message that *names* the affected athletes, so
the cost is a more useful message body rather than three messages — and
`weekly-plan.sh` invokes `stage1-plan.py` once per athlete, so a single-athlete
failure is a real shape that `per_athlete: False` would hide.

## The registry is derived from the crontab, not trusted (28 Jul 2026)

`DELIVERABLES` was hand-maintained, and it drifted **twice in one day** — once
in each possible direction:

1. `backup-config` was registered with a hand-declared time (23:50) that the
   21:30 digest could never see satisfied. It would have alerted every night in
   perpetuity about a job that was working.
2. `capture-reminder` stayed registered after its cron entry was deleted. It
   would have printed a false gap line every night in perpetuity.

Both were caught by eye, hours apart. A third would not be. The registry is now
**diffed against the live crontab on every run**, keyed on the script filename,
and any mismatch is reported loudly into `ops-alerts.log` and the digest.

### The direction that matters most

The two incidents above are both *registry → cron* drift, which is **noisy**: it
produces a false alarm that somebody eventually investigates. The dangerous
direction is the mirror image nobody had thought of — **a cron entry with no
registry entry** — which is **silent**. That is a scheduled job nobody watches,
and it is exactly how the weekly report vanished for three weeks with nothing
but a log line to show for it. `coach_alert._registry_problems()` checks both.

### Authoritative source

`crontab -l` for root — the actual thing cron executes.

**Not** `ClaudeCoach/system/crontab.template`. That file is a sanitised rebuild
reference and is itself demonstrably stale: as of 28 Jul 2026 it still lists
`capture-reminder`, has five wrong times (`refresh-site-data` 06:00 vs 06:20,
`cc-gitpull` `*/30` vs `5,35`, `refresh-public-data` `0 *` vs `9 *`,
`backup-config` 23:00 vs 23:50) and is missing `plan_audit.py` entirely.
Diffing against it would have verified one hand-maintained file against another
— the same bug, one layer down. **Fixing the template is a separate job.**

### What the audit reports

| Condition | Reported | Judged? |
|---|---|---|
| Registered, no live cron entry, no `no_cron` | loud `⚠ CRON AUDIT` | **no** — `NO_CRON_ENTRY`. A job that is not scheduled cannot run, so a gap line about it is a false alarm about the *registry*. This is capture-reminder, detected instead of eyeballed. |
| Registered, no cron entry, `no_cron: "reason"` | nothing | yes, on the declared schedule |
| Annotated `no_cron` but a live entry exists | loud — stale annotation | no (the annotation is used) |
| Declared `cron` disagrees with the live line | loud | yes, **on the live line**. Reality wins; the declaration is a fallback. |
| Live cron entry, no registration, no exemption | loud | n/a — nothing watches it |
| Live cron entry, listed in `CRON_EXEMPT` | nothing | n/a — by reviewed decision |
| `CRON_EXEMPT` name with no live entry | loud — stale exemption | n/a |
| Two cron entries for one job | loud | yes, on the **first** — approximate, and says so |
| Live schedule this parser cannot read | loud | yes, on the declared schedule (a parser limit must not un-check a job) |
| `crontab -l` unreadable | loud "NOT verified" | yes, whole registry on declared schedules |
| `crontab -l` readable but with no ClaudeCoach jobs in it | loud "NOT verified" | yes — a crontab with none of our jobs is one read from the wrong account, not one where every deliverable was deregistered; trusting it would silence all ten at once |

Mismatches are **log-only**. Registry drift is an engineering fault, not one of
the two conditions Jamie approved for interrupting him, and this deliberately
does not widen that surface. Lines go to the top of the digest so `MAX_LINES`
truncation cannot hide them, and to `ops-alerts.log` via
`ops_log.alert("cron-audit", …)` — its own script name, because `"coach-alert"`
means *the alarm could not reach Jamie* and overloading it would make a config
fault indistinguishable from a delivery fault in the same log. `cron-audit` is
`FAILURE`-class in `OUTCOME_CLASS`, so it never surfaces as an `UNCLASSIFIED`
line.

### Which scheduled jobs legitimately need no deliverable

`coach_alert.CRON_EXEMPT` — a table of script filename → **why**, so the
decision is reviewable in a diff instead of being an implicit rule in somebody's
head. Every reason was checked against the script's `ops_log` usage, not
assumed. Currently exempt: `ops-digest.py` (this alarm itself — a deliverable
would ask it to detect its own absence), `cc-gitpull.sh` and `bot-watchdog.py`
(plumbing that only speaks when something is wrong, no success heartbeat to look
for), `refresh-public-data.py` and `refresh-site-data.py` (site data
regeneration, no `ops_log` instrumentation, nothing delivered to an athlete or
the coach), `bug-fixer.py` (everything it does is already a digest line by
construction) and `plan_audit.py` (`FINDING`-class, baseline-gated, its whole
output *is* digest lines).

**Open finding, 28 Jul 2026.** Two live jobs are neither registered nor exempt,
and the audit reports both every night until somebody decides:

- **`sync-private-repo.sh` (23:20 daily)** — the sharp one. It calls
  `lib_git_alert.sh`'s `git_sync_ok "sync-private"`, so it *does* write a
  heartbeat, and **nothing reads it**. That is the weekly-report shape exactly:
  a job with a heartbeat and no watcher. It also sits twenty minutes before
  `backup-config.sh`, which is registered precisely because it is the only
  off-box copy of credentials and health data.
- **`activity-watcher.py` (every 5 min)** — writes per-athlete `record_run`
  heartbeats. It is the ingest everything downstream depends on.

Neither was registered here on purpose: adding a deliverable needs a `since`
instrumentation date and would change the alarm's blast radius, which is a
decision for the owner, not a side effect of wiring up the audit.

### Fail safe

If `crontab -l` cannot be read at all — non-zero exit, empty output, no
`crontab` binary, a timeout — `cron_audit()` returns `verified=False`, the gap
check carries on **off the static registry exactly as before**, and the digest
carries a loud line saying the registry was not verified this run. The two
failure modes that matter are opposite, and both are avoided: the alarm is never
silently disabled, and it never starts alerting on everything. An unreadable
crontab is deliberately **not** the same as an absent entry: absent means "this
cannot run, do not judge it", unreadable means "we cannot tell, keep judging".

The audit also never raises. `parse_crontab` turns anything it cannot
understand — cron nicknames, short lines, unparseable specs — into a problem
line, and `cron_audit()` has a belt-and-braces `except` around the whole thing,
because an exception at 21:30 would take the digest down and the alarm would go
quiet: the exact failure mode this file exists to prevent.

## Weekly deliverables: one miss, one message

The weekly summary and weekly plan now Telegram too (Jamie approved it after the
weekly-summary build crashed silently for three weeks). They cannot use the daily
path's per-date cooldown key: the weekly gap check reads a **7-day window**, so a
single Sunday miss still reads as missing on all seven following evenings — that
would be up to seven Telegrams for one incident.

`weekly_alerts()` in `ops-digest.py` sends them separately:

- **One message per SCRIPT, not per athlete.** A Sunday job failing is one root
  cause; three copies of it is noise.
- **One message per OCCURRENCE.** A stable key (`weekly:<script>`) with the
  cooldown banked for the whole 168h window.
- **The cooldown is cleared the moment the deliverable is seen again**, so the
  *following* week's genuine miss is not silenced by a leftover cooldown.
  `test_without_recovery_the_cooldown_would_silence_next_week` is the negative
  control for that.

## Escalation tuning

`ESCALATE_AFTER = 6` counted *consecutive* failures on a counter any success
cleared, so it could not see an intermittent fault, and didn't: the seven push
failures over 24-27 Jul were interleaved with successful ticks, the consecutive
count never got past 1, and nothing escalated for four days.

Replaced with **3 failures in 24 hours**, successes only age entries out.
Replayed against the real episode it first fires on **25 Jul 11:00** — day two
of four — and a single self-healing blip, or a pair 6h apart, still stays quiet.
Hours rather than tick counts, because five jobs share the helper on different
cadences.

## Retention

The weekly window reads `run-status.jsonl`, which is trimmed by line count.
`_MAX_LINES[RUN_STATUS]` was raised 2000 → 6000: the four new heartbeats add
~30 entries/day (session-sync alone is 8 runs × 3 athletes) on top of the
observed 12-40, so 6000 is >30 days at the new rate and >20 days on the worst
observed spike day.

## plan_audit baseline gating

`lib/plan_audit.py` hard-fails for all three athletes by design (see
`plan-audit-status.md`) and called `ops_log.alert()` unconditionally, writing
three `ok=False` entries into `run-status.jsonl` every day — poisoning the store
this alarm reads and training the reader to ignore ✗ lines.

Now baseline-gated on `config/plan-audit-baseline.json`: failure **counts per
category**, per athlete. At or below the accepted counts → `ok=True, "known
baseline fail"`. A new category, or a higher count, still alerts. Counts rather
than an exact signature so that partly fixing a defect does not page you.
Regenerate with `python3 lib/plan_audit.py --all --write-baseline`, or shrink
the numbers by hand as defects are fixed. `sys.exit(1 if any_hard else 0)` is
unchanged and still honest.

## Testing it without messaging anyone

```bash
CC_ALERT_DRY_RUN=1 python3 ClaudeCoach/scripts/ops-digest.py
```
`CC_ALERT_DRY_RUN=1` short-circuits `coach_alert.send()` to
`log_outbound(sent=False)` — the exact text it would have sent goes to
`ops-alerts.log` and nothing leaves the box. It also does not consume the
cooldown, so a dry-run cannot mask a real alert later.

`CC_COACH_CHAT_ID` overrides the recipient; unset, alerts go to `notify.py`'s
configured default, which is Jamie's own thread.

**Dry-run cannot evidence a cooldown, and do not "fix" that.** `send()` returns
`"dry-run"` *before* it banks the cooldown, deliberately — a dry run must not
silence a later real alert. The consequence is that a dry-run replay of a weekly
miss re-alerts on all seven evenings and proves nothing about the once-per-
occurrence behaviour. To test cooldowns, replace `coach_alert.subprocess` with a
stub that reports success (see `TestWeeklyAlerts._stub_sender`); there is exactly
one `subprocess.run` in `coach_alert.py`, so no process is spawned and nothing
leaves the box. A cooldown assertion made under `CC_ALERT_DRY_RUN=1` is vacuous
and will pass whether the mechanism works or not.

## The suite cannot Telegram — two independent guards (28 Jul 2026)

**What happened.** `tests/test_ops_digest.py::TestCronDerivedRegistry::
test_a_weekly_deliverable_with_no_cron_entry_neither_alerts_nor_clears` called
`digest.weekly_alerts([], ...)`. The empty entries list starved *both* weekly
deliverables, not just the one the test had unscheduled, so `stage1-plan` — still
in the crontab, still `telegram=True` — was correctly judged missing and
`coach_alert.send()` ran with no `CC_ALERT_DRY_RUN` and no stubbed `subprocess`.
It executed `telegram/notify.py` for real and delivered *"⚠️ ClaudeCoach did not
deliver: weekly plan (jamie, kathryn)"* to the coach's live thread. The suite ran
twice, and the test's own `STATE` monkeypatch meant no cooldown was banked
between runs, so it went out twice. The only verbatim record was written to a
`tmp_path` `ALERT_LOG` that pytest then deleted, which is why the real
`ops-alerts.log` shows no `SENT` line for it.

**Guard 1 — the default (belt).** `ironman-analysis/conftest.py` has a
session-scoped autouse fixture that sets `CC_ALERT_DRY_RUN=1` for the whole run
and restores the previous value afterwards. Session scope because the variable is
a process-wide default, not per-test state, and because `monkeypatch.setenv` in
the tests that legitimately opt out restores *this* value rather than fighting a
function-scoped fixture. Autouse because the test that sent for real requested
`logs` and `monkeypatch`, not a safety fixture — protection that has to be asked
for is protection that gets forgotten.

**Guard 2 — the refusal (braces).** `coach_alert.send()` raises
`TelegramSendBlocked` if pytest is in the process (`PYTEST_CURRENT_TEST` in the
environment **or** `pytest` in `sys.modules` — the second catches collection and
session/module fixtures, where the first is unset) *and* the module's
`subprocess` is still the real stdlib one. A test that has stubbed
`coach_alert.subprocess` is provably unable to reach the network and is allowed
its real send path, which is what `TestWeeklyAlerts._stub_sender` and
`TestSendFailureDoesNotEatTheCooldown` need. It **raises** rather than returning
`"dry-run"`: a suppressed send and a send that was never warranted must stay
distinguishable, or a real routing bug passes the suite. Before raising it writes
the withheld text with `log_outbound(sent=False)` and prints the same line to
stderr — stderr survives a test that has redirected `ALERT_LOG` to a temporary
directory, which is exactly what hid this incident.

Both exist because one was enough for this to happen once. A fixture only
protects tests that inherit it; the guard sits on the single line that can reach
Telegram, so it holds for a new test file under a different conftest, a direct
`import coach_alert`, or a helper called outside a test function. Locked by
`TestCoachAlertRouting::test_a_test_can_never_execute_the_real_notify` (must
raise) and `::test_a_stubbed_subprocess_is_still_allowed_the_real_path` (must
not).

**Every send is logged, including the successful ones.** `send()` writes
`log_outbound()` on all five exits — `refused` (via `ops_log.alert`), `cooldown`,
`dry-run`, `send-failed`, and `sent` (`sent=True`, present since 83bb541).
`::test_a_successful_send_is_recorded_verbatim` locks the success path, because
it is what lets anyone establish after the fact whether a message really went
out — and the 28 Jul incident is the proof that this matters: the record existed
in code and was still lost, to a monkeypatched log path.

**A weekly deliverable with no crontab entry stays silent.** Decided 28 Jul 2026
and already what the code does: a job that is not scheduled cannot run, so a
missing heartbeat says nothing about the job and everything about the registry.
Alarming would name the wrong fault — *"did not deliver the weekly plan"* when
the truth is *"nobody scheduled it"*. The direction is not silent: the loud
`⚠ CRON AUDIT` line repeats every run until the entry is restored, the
deliverable is deregistered, or it is annotated `no_cron="why"`. Same call as the
daily case.
