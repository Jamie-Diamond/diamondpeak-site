# plan_audit.py — scheduled, alerting suppressed (2026-07-28)

`ClaudeCoach/lib/plan_audit.py` runs daily via VM crontab at 06:25:
```
25 6 * * * /root/.claude/cc-run python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/lib/plan_audit.py --all >> /root/Library/Logs/ClaudeCoach/plan-audit.log 2>&1
```
It is read-only (no side effect beyond `print(json.dumps(...))`) and exits 1 on
hard fail, 0 clean. `cb416dc` wired hard-fail outcomes to `ops_log.alert()` —
this writes `ops-alerts.log` / `run-status.jsonl` only; it does not call
Telegram (`ops_log.alert()` has no notify path, and `ops-digest.py`, the only
consumer of `run-status.jsonl`, has been log-only since 27 Jul 2026 — see its
own docstring). **So plan_audit's alerting is suppressed already, by design,
and required no code change here.**

## Why it's suppressed

A run on 28 Jul 2026 returned `hard_fail: true` for **all three athletes**.
Turning this on for real athletes right now would mean 100% false-positive
noise. Baseline category counts (full JSON at
`/root/Library/Logs/ClaudeCoach/plan-audit-baseline-2026-07-28.json`, root-only
permissions, not committed — contains athlete session detail):

- **jamie** — 3 FUELLING, 1 WEEKLY_LOAD, 4 hard day_rules (ride Thu, swim Wed,
  run Thu, run Fri), 2 soft intensity_distribution
- **kathryn** — 1 STRUCTURE, 3 FUELLING, 2 WEEKLY_LOAD, 2 soft
  intensity_distribution
- **calum** — 1 FUELLING, 1 WEEKLY_LOAD, 1 soft intensity_distribution

All three also fail `WEEKLY_LOAD` on the week of 2026-08-03 with 0 TSS,
because no sessions are loaded past 2 Aug.

## Must fix before alerting is re-enabled

1. The day_rules breaches (4 on jamie's week)
2. The missing structured steps on one of Kathryn's rides (STRUCTURE)
3. Sessions with no FUELLING statement where one is expected (7 across the
   three athletes)
4. The empty week past 2 Aug (WEEKLY_LOAD floor firing on unloaded weeks, all
   three athletes)

**RE-ENABLE ALERTING once these are fixed — until then this check is
invisible by design, which is the same failure mode that hid the
weekly-summary crash for three weeks (see review 2026-07-27).**

## Exit code

Left honest — still `sys.exit(1 if any_hard else 0)`. A future baseline/diff
mechanism can key off it; nothing currently consumes it, since cron output
only reaches `plan-audit.log`.
