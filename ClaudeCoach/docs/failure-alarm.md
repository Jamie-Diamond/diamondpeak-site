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
   check-in, the nightly config backup, the weekly summary or the weekly plan.
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
| session-sync, watchdog, capture-reminder gaps | Internal plumbing and nudges, not deliverables. |
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
