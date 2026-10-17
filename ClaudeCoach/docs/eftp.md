# FTP is eFTP, by design

Every athlete's bike targets use **eFTP**, not their last tested FTP. This is a
deliberate decision (Jamie, 15 Oct 2026), **not a bug**. Read this before acting on any
athlete complaint that their FTP or power targets are "wrong".

## The decision

| What | Detail |
|---|---|
| **Working FTP** | Live eFTP from Intervals.icu (`lib/thresholds.py get_thresholds`) |
| **Sync** | Nightly at **04:50**, the Intervals.icu configured FTP is set to eFTP, **up or down** (`lib/thresholds.py --all --sync-ftp --apply --notify`; also run before the Sunday plan by `scripts/weekly-plan.sh`) |
| **Noise guard** | Only moves when the change is at least 2 W **and** at least 1% |
| **Who hears about a move** | Jamie, in the Peak Dev tab. Athletes are not messaged: their zones just follow |
| **Accepted trade-off** | eFTP sags after an easy fortnight, and the zones sag with it |
| **Before 15 Oct** | The sync was raise-only and was never scheduled, so the plan read eFTP while the zones on the watch came from a tested FTP 4-8% higher |

## Why eFTP

| Point | What to tell the athlete |
|---|---|
| **What it is** | eFTP is **estimated FTP** (that's the e). Intervals.icu works it out from their power data |
| **Why not the test number** | A test is a **snapshot of one day**. As fitness rises or falls afterwards, that number can **under- or over-predict** their real FTP |
| **What eFTP does** | It models that drift and **keeps updating without regular retesting** |
| **Where testing fits** | An FTP test is still **the most accurate way to get a good eFTP**. Without one, **every hard effort** they ride is used to update it |

## If sessions feel too easy

| Option | How |
|---|---|
| **Retest** | A hard effort lifts eFTP straight away |
| **Push the last sets** | If they're still strong on the final sets, go a little higher |
| **Zwift** | Use the workout +/- % intensity control |

## What the coach must never do

- Call eFTP a mistake, apologise for it, or say "the app switched it without telling you".
- Log it as a bug, or promise to switch back to a tested number.
- Let a standing rule that names an older tested FTP as the "working FTP" override this.

## Where this lives

| Layer | File | What it does |
|---|---|---|
| **Coach, eFTP** | `lib/coach_manual.md` ("FTP is eFTP, by design", under Live data) | The explanation above and the three "too easy" options, for every athlete. Read every turn, so an edit is live as soon as the VM pulls |
| **Coach, any complaint** | `lib/engine.py` `_FEEDBACK_LOG_RULE` ("NOT EVERY COMPLAINT IS A BUG") | Check design before agreeing something is wrong. Needs an API restart to go live |
| **Bug-fixer** | `scripts/bug-fixer.py` planner step 2a ("BY DESIGN IS NOT A BUG") | Closes by-design reports as `already_resolved`; never drafts a fix or exemption that undoes a design decision |
| **Sync** | `lib/thresholds.py` `sync_ftp_from_eftp`; VM crontab `50 4 * * *` | The behaviour itself |

## Incident: Fred, 17 Oct 2026

| Time | What happened |
|---|---|
| 14:07 | Fred asked what FTP the coach was using. It said **196 W** (eFTP) |
| 14:08 | Fred: "This is wrong, we have been using 212 from my last test." The coach **agreed**, said the app had switched it without telling him, gave him 212-based targets and logged a bug asking Jamie to exempt him from the sync |
| 14:13 | Fred set Intervals.icu back to 212 W himself. The coach said it might switch back overnight, marked it urgent, and logged a second bug |
| 18:44 | Correction sent to Fred in Peak (outbox `20261017184427-79cfd5`, source `coach-correction`, push delivered). It was worded like the tables above. Intervals returns to eFTP (196 W) at the next 04:50 sync |

**Root cause:** Fred's `persistent-rules.md` still said "212 W stays as the working FTP"
(written 4 Oct, when his ramp test was skipped). The coach followed the rule over the
design, and nothing told it eFTP was deliberate.

**Fixed:**

| Fix | Where |
|---|---|
| Rule now reads "FTP follows eFTP by design; 212 W was his last test, not the working FTP" | Fred's `persistent-rules.md` and `current-state.md` (VM; backups `*.bak-eftp-20261017*`) |
| Both bug entries closed as "not a bug: by design" | Fred's `feedback-log.json` |
| eFTP explanation, and the general "not every complaint is a bug" rule | Coach manual, `engine.py`, `bug-fixer.py` (commits d077c1a5, c05480aa, 33ec69b5, c2c228d0) |

## If it comes up again

1. **Check the athlete's `persistent-rules.md` and `current-state.md`** for a tested FTP
   written as the "working FTP". Reword it to "last test", not delete it.
2. **Check `feedback-log.json`** for a bug entry about it. Close it as by design, so the
   midnight bug-fixer doesn't act on it.
3. **If the coach told the athlete it was a mistake**, send a correction to the athlete's
   web outbox (`lib/outbox.py record`, `source="coach-correction"`) using the two "Why
   eFTP" and "too easy" tables above. Get Jamie's yes first.
4. **If the coach explained it badly but with the manual in place**, fix the wording in
   `lib/coach_manual.md`, not with a new per-athlete rule.
