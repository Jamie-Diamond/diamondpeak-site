# plan_audit.py — scheduled, alerting BASELINE-GATED (2026-07-28)

> **Three hard checks ARMED, 2026-07-28 (later same day).** `plan_audit` called
> `validate_week(..., day_rules, ctl_today)` and nothing else, so
> `weekly_tss_cap`, `weekly_tss_floor` and `run_weekly_volume` were reported
> SKIPPED on every run for every athlete: Layer 4 had **never once** checked a
> week's total load or its run volume. All three now take their input from the
> same function the generation path uses — `plan_builder._weekly_tss_cap`,
> `plan_tools.required_tss()['weekly_tss_floor']`, and
> `plan_tools.run_caps()['weekly_min_cap']` — so the audit cannot disagree with
> the generator about where the limit sits. `SKIPPED` went 5→0 (jamie), 6→1
> (kathryn), 4→0 (calum), and three REAL failures surfaced for the first time:
>
> 1. **jamie `weekly_tss_cap`** — 847 TSS planned in the week of 27 Jul against a
>    735 cap (`max_hours_per_week` x 100 x IF², Specific phase). Note the deeper
>    contradiction: `required_tss` *recommends* 856 for the same week, i.e. the
>    engine's own target is 16% above the athlete's hours ceiling and no plan can
>    satisfy both. One of the two is wrong — fix the config, not the check.
> 2. **jamie `run_weekly_volume`** — 235 min planned against a 206 min cap
>    (recent-4-week max x the run_protocol ramp). Same class of defect as his
>    day_rules breaches: the plan was pushed past a rule that was not running.
> 3. **`weekly_tss_floor`** — fires on the week of 3 Aug for all three athletes
>    (0 TSS planned) and on kathryn's current week (425 vs a 474 floor, a Build
>    week — genuinely under-trained, and already visible as her WEEKLY_LOAD 400
>    vs 652).
>
> **Deload protection verified, not assumed:** calum's current week resolves to
> `week_type=deload` (scheduled, every 4th training week), `required_tss` returns
> `weekly_tss_floor: 0`, and the floor check correctly scores nothing against it.
> An intentional down-week does not read as under-training. Only his *empty* week
> of 3 Aug fails. This does flip calum from `hard_fail: false` to `true`.
>
> **The 3 Aug failures are the Sunday-generation-cadence artefact already listed
> as defect 4 below**, now hard instead of warn: the audit's 2-week window looks
> at a week that has not been generated yet, so it will fire six days in seven
> until defect 4 is fixed (audit only generated weeks, or treat a zero-event week
> as not-yet-planned). It was deliberately NOT special-cased here — that would
> change what counts as a hard failure.
>
> **A pre-existing baseline hole was closed in the same commit.** `SKIPPED` was
> absent from every athlete's baseline entry, and `within_baseline` compares
> `n <= accepted.get(cat, -1)` — so from the moment the SKIPPED category was
> added, every run failed the gate and jamie + kathryn alerted on **every**
> invocation (confirmed in `ops-alerts.log`, 28 Jul 14:08–14:15). The claim below
> that "alerting is suppressed and required no code change" was true of the
> Telegram route only. `SKIPPED` is now listed explicitly, at 0 where it should
> be 0, so a new skip is still news.
>
> `run_long_min_cap` remains unpassed, so `run_long_volume` is still unarmed —
> deliberately out of scope, and note `validate_week` emits **no** skip line for
> it, so it is invisible rather than merely unchecked. Arming it is a one-line
> change (`run_long_min_cap=` from the same `run_caps` dict) and should be its
> own commit with its own before/after.

> **Superseded in part, same day.** This page originally said plan_audit needed
> no code change because `ops_log.alert()` cannot Telegram. That is still true of
> the Telegram route, but it was the wrong conclusion: the unconditional
> `ops_log.alert()` wrote three `ok=False` entries into `run-status.jsonl` EVERY
> DAY, and `run-status.jsonl` is the heartbeat store the failure alarm reads. A
> permanently-failing job in there trains the reader to ignore ✗ lines, which
> defeats the alarm.
>
> `plan_audit.main()` is now gated on `ClaudeCoach/config/plan-audit-baseline.json`
> — failure **counts per category**, per athlete, committed. A run at or below its
> athlete's accepted counts records `ok=True, "known baseline fail [...]"`. A new
> category or a higher count still alerts. Counts, not an exact signature, so
> partly fixing a defect does not start alerting. Verified against real data on
> 28 Jul 2026: first run alerted all three, second recorded all three as known
> baseline. Regenerate with `python3 lib/plan_audit.py --all --write-baseline`, or
> just shrink the numbers as you fix things. See `docs/failure-alarm.md`.
>
> The rest of this page stands: the four defects below are still the work, and
> "re-enable alerting" now means *shrink the baseline to zero*, not flip a switch.

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

Left honest — still `sys.exit(1 if any_hard else 0)`, unchanged by the baseline
gating: the gate decides how loud the log entry is, not whether the audit failed.
Nothing currently consumes the exit code, since cron output only reaches
`plan-audit.log`.
