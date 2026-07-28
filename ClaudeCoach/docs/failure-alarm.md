# Failure alarm — what interrupts the coach, and what doesn't (28 Jul 2026)

Before this, nothing told anyone when ClaudeCoach stopped working. A weekly
report crashed silently for three weeks and a git push failed for four days;
both went unnoticed because the only failure surface was a log file nobody had
a reason to open.

## The rule

Exactly **two** conditions reach Jamie on Telegram. Everything else is
log-only, in `~/Library/Logs/ClaudeCoach/ops-alerts.log` and
`run-status.jsonl`.

1. **A named daily deliverable did not happen** — no heartbeat today for the
   morning card, the daily prescription, the night-before brief or the evening
   check-in.
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
| weekly summary / weekly plan missing | Jamie said *daily*. Registered with `telegram: False` and a 7-day window. **This is the three-week silent crash: it is caught now, in the log only.** One boolean flips it. |
| session-sync, watchdog, capture-reminder gaps | Internal plumbing and nudges, not deliverables. |
| `plan_audit` hard fails, under-training, everything else | Digest lines. |

## Heartbeats: silence vs absence

Several senders correctly send nothing on many days. That is recorded as a
**success** (`ok=True`, `detail="silent (...)"`) and the gap check keys on the
**presence** of an entry, not its detail — so legitimate silence never reads as
a missed deliverable. Three distinct states:

| State | run-status | Digest |
|---|---|---|
| ran, sent | `ok=True detail="sent"` | nothing |
| ran, correctly silent | `ok=True detail="silent (...)"` | nothing |
| ran, failed | `ok=False` | ✗ line |
| no record at all | nothing | ⚠ gap line (+ Telegram if a daily deliverable) |

A ✗ is never also a ⚠: `_saw()` in `ops-digest.py` counts a recorded failure as
a heartbeat. The three original checks keep the older `ok`-only `_ran()`
semantics so their behaviour is unchanged.

The trap is the fourth state: **empty output from a broken CLI looks exactly
like correct silence**. `capture-reminder.py` spawns the CLI directly and has no
auth detection at all, so a non-zero exit with no `<notify>` block records
`ok=False` — otherwise a dead token would read as "nothing to remind about"
every night, forever.

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
