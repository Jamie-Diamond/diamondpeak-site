# Weekly available hours — asked, not configured

**Status:** mechanism landed on `feat/weekly-hours-capture` (28 Jul 2026); the reply
capture landed on `feat/bot-capture-handlers` (29 Jul 2026). The ask is appended to the
Sunday morning card, the reply is parsed automatically, and the ceiling derives from the
answer. See [The bot half](#the-bot-half).

## Why

`profile.max_hours_per_week` is a static description of a typical week, and it was the
only input to the weekly Load ceiling (`hours x 100 x IF²`). Two consequences, both
observed:

- **Jamie.** 15 h on file gives a Peak ceiling of 778 TSS. His engine target for late
  August Peak weeks is up to 918. Last season he reached a CTL peak of 117.5 and raced
  at 103.7, so he demonstrably trains above 15 h. The generator could only breach the
  ceiling or miss the target — every week.
- **Kathryn.** Her `persistent-rules.md` has required the weekly ask since 10 Jul 2026:
  *"The fixed 11h/week training-hours cap was REMOVED permanently — max_hours_per_week
  is null … Do NOT reinstate a fixed hours cap. Confirm each week's available hours +
  any time caps via the Sunday check-in and build to that (fall back to full
  phase-required load if she doesn't specify)."* That check-in did not exist.

And because there was no weekly figure to point at, `stage1-plan.py` resolved the
conflict by quoting a config key at a human — *"raise max_hours_per_week to close the
gap"* — reworded on 26 Jul, root cause unaddressed until now.

## The record

`ClaudeCoach/athletes/<slug>/this-week-availability.json` — the file `stage1-plan.py`
already read for per-week day shape (Phase 5a), extended rather than duplicated.
`ClaudeCoach/athletes/` is gitignored wholesale, so nothing here reaches the PUBLIC
repo (`git check-ignore -v` confirms the rule at `.gitignore:26`).

```json
{"declarations": [
  {"week_start": "2026-08-03",
   "hours": 17.5,
   "constraints": "away Thu-Fri, nothing long Mon-Thu",
   "unavailable_days": ["Thu", "Fri"],
   "declared_at": "2026-08-02T07:14:03",
   "source": "telegram-reply"}
]}
```

`hours` is the athlete's total training time for that week. It is **not** a per-day cap
— day-level limits go in `constraints`, free prose that reaches the Stage-1 planner
verbatim. `swim_days` / `bike_days` / `run_days` / `unavailable_days` are the existing
Phase 5a keys and behave exactly as before.

### Expiry

A declaration names one Monday and applies to that week **only**. There is deliberately
no "carry last week's figure forward" — silent persistence is precisely the defect
being removed.

Expiry is by **named week, not by deletion**. `lib/plan_audit.py` audits the current
*and* next week every morning at 06:25 and must resolve the same ceiling the generator
used; deleting the record on Monday would make the audit retro-flag a week that was
built correctly. So the file keeps the last `_KEEP = 6` declarations and the resolver
hands back only the one whose `week_start` matches the week being asked about. Anything
else — no record, a different week, an undated legacy file, a figure outside
`1.0–40.0 h` — resolves to `None`, and the caller falls back to config.

### Legacy flat files

A pre-existing Phase 5a file (day-shape keys, no dates) is honoured for day shape
exactly as today and can **never** supply hours: an undated number is the standing
constant this replaces. On the first declaration it is carried forward whole under
`legacy_day_shape` rather than discarded.

## The ask

**Sunday morning, appended to the morning check-in** (`morning-checkin.py`, the
`*/30 6-9` poll), asking about the week that starts tomorrow. The Sunday build is
`0 18 * * 0`, so the athlete has at least nine hours to answer. Every other Sunday push
lands after the build and is therefore useless for this: `weekly-summary` 20:00,
`night-before-brief` 20:30, `evening-checkin` 21:00.

**It piggybacks rather than being its own cron.** The athletes already receive ~35–45
pushes a week, and three evening messages were merged into two on 28 Jul because
Kathryn stopped answering three consecutive asks. A new Sunday push would spend the one
unit of attention this question needs on a notification instead. Appended to a card
that was going out anyway, it costs **zero extra pushes**, and the existing per-athlete
daily sentinel already guarantees once-per-day.

It is **deterministic text**, appended after the LLM card is extracted and before the
send — not a prompt instruction the model may quietly drop. For the one question the
whole mechanism depends on, that is not a risk worth taking.

Gates (all in `weekly_availability.sunday_hours_ask`, so no caller can forget them):

| Gate | Behaviour |
|---|---|
| Already declared for that week | silent — the answer is in |
| Illness flag active (`lib/illness`) | silent — the reduced week is driven by the flag, and asking burns attention on an answer we would override |
| Not Sunday | not asked (gate at the call site, which owns `today`) |

`coaching_levels.level_block()` only shapes **LLM prompts**, so a hardcoded string
bypasses it entirely. The three level variants are therefore written by hand in
`lib/weekly_availability.py`. Calum is `beginner`: his variant carries no
Load/TSS/IF/zone framing at all, and a test asserts that.

## No reply

The ask itself states the consequence of silence *before* the athlete is silent, and
the plan message states it again afterwards. Three cap states, three sentences
(`stage1-plan._week_message`, driven by `plan_builder.cap_source`):

| `cap_source` | Meaning | What the athlete reads |
|---|---|---|
| `declared` | the athlete answered | *"…only {cap} fits in the {n} hours you told me you have…"* |
| `hours` | **no answer** — standing config figure used | *"…only {cap} fits in the hours I have on file for you… You didn't tell me this week's hours, so I used your usual week. Tell me the real figure and I'll rebuild it."* |
| `phase` | blueprint phase load ceiling; more hours would not move it | *"…{cap} is as much as I will safely put in front of you at this stage. Nothing for you to do."* |

Never silent, and never an assumption of unlimited or minimal time.

## Cap precedence, after this change

`lib/plan_builder._weekly_tss_cap(slug, phase, week_start=None)` — extended, not
forked:

1. **hours the athlete declared for `week_start`** → `tss_ceiling(hours, phase)`
2. `profile.max_hours_per_week` → same formula — now a documented **fallback**
3. blueprint phase `tss_ceiling` (Kathryn's arming path, 10 Jul 2026)
4. `None` — a taper carries no ceiling in any source, and still reports the check as
   skipped

The formula itself stays in exactly one place, `primitives.blueprint.tss_ceiling`.
`plan_builder.cap_source()` returns which branch bit, so no caller re-derives the
precedence (`stage1-plan` used to re-read `max_hours_per_week` to label the message and
would now have been wrong).

**`week_start` defaults to `None`, meaning "no declaration applies".** That default is
load-bearing: `lib/macro_projection.py:338` passes `lambda ws, phase:
_weekly_tss_cap(slug, phase)`, discarding the week, and one real week's declaration
must not bound every projected week. Callers that know which single week they are
bounding pass it: `plan_builder.build_sessions` (`week_start=ws`),
`plan_audit.audit_athlete` (`week_start=ws`), `stage1-plan` (`week_start=week_start`).

### Does this reinstate Kathryn's removed cap?

No. Her rule forbids a *fixed* hours cap and, in the same sentence, requires building
to the hours she confirms each week. A figure valid for one named week is the second,
not the first. Absent a declaration her cap resolves to the phase ceiling exactly as
today.

## Nothing changed absent a declaration

Proven read-only, not asserted. The pre-change `_weekly_tss_cap` was loaded from
`git show main:ClaudeCoach/lib/plan_builder.py` and run beside the new one across all
13 athlete × blueprint-phase combinations on the live athlete files:

```
athlete   phase          old  new(+wk)  new(no wk)  verdict
jamie     Base         634.0     634.0       634.0  SAME
jamie     Build        694.0     694.0       694.0  SAME
jamie     Specific     735.0     735.0       735.0  SAME
jamie     Peak         778.0     778.0       778.0  SAME
jamie     Taper         None      None        None  SAME
kathryn   Base         883.0     883.0       883.0  SAME
kathryn   Build        966.0     966.0       966.0  SAME
kathryn   Peak        1083.0    1083.0      1083.0  SAME
kathryn   Taper         None      None        None  SAME
calum     Base         338.0     338.0       338.0  SAME
calum     Build        370.0     370.0       370.0  SAME
calum     Peak         415.0     415.0       415.0  SAME
calum     Taper         None      None        None  SAME
13 combinations, 0 differences
```

No athlete has a `this-week-availability.json` on production, so today's live
behaviour is byte-identical. With a 17.5 h declaration for Jamie (written into a
throwaway `/tmp` tree, never production):

```
cap BEFORE (15h config) : 778.0   source=hours     918 <= 856  -> DOES NOT FIT
cap AFTER  (17.5h)      : 907.0   source=declared  918 <= 998  -> FITS
cap for a DIFFERENT week: 778.0   <- no leak
cap with week_start=None: 778.0   <- macro_projection unchanged
```

`+10%` is `validate_week`'s `tss_tolerance`, read off the signature by
`macro_projection._cap_tolerance()`.

## The bot half

**Landed 29 Jul 2026** (`feat/bot-capture-handlers`). The reply is parsed in
`telegram/bot.py` `_handle_hours_capture` / `_handle_hours_confirm`, on the detector and
parser in `lib/weekly_availability.py`. A declaration can still be written by hand:

```python
python3 -c "import sys; sys.path.insert(0,'lib'); import weekly_availability as wa; \
  print(wa.record('jamie', '2026-08-03', hours=17.5, \
       constraints='away Thu-Fri, nothing long Mon-Thu', source='coach'))"
```

### Two tiers, because a bare number on that card is ambiguous

The morning check-in the ask rides on also asks *"Ankle score this morning? (0-10)"*,
*"Injury pain score before heading out? (0-10)"* and *"Weight this morning?"*
(`morning-checkin.py:60-72`). A single-tier "bare number in 1-40 on a Sunday" detector
would read a reply of `7` to the ankle question as a seven-hour week and cap that
athlete's ceiling at under half its real value - invisibly, since the plan message would
then say *"the 7 hours you told me you have"* about a figure never said. So:

| Tier | Trigger | Behaviour |
|---|---|---|
| 1 | `looks_like_hours_declaration` - a figure **and** weekly framing (`14h next week`, `20, big week`) | writes immediately, reads the record back |
| 2 | `looks_like_hours_reply` - a figure with no framing (`12 max, nothing long midweek`, a bare `14`) | only while `ask_outstanding`; **never writes on sight** - confirm keyboard, write on the tap |

Unframed bare figures are held to `BARE_MIN_HOURS` (5.0) rather than `MIN_HOURS` (1.0),
because a bare `3` is overwhelmingly a pain score for athletes whose standing figures are
8 and 15. Between 5 and 10 the two questions genuinely overlap, which is why that tier
requires a tap.

Both tiers refuse a **question** (the lesson `races.looks_like_race_statement` learned), a
**report of hours already done** (`I did 14 hours last week`, `slept 7 hours` - capping the
coming week off the previous one is the silent persistence this design removes), and **one
session's duration** (`a 2 hour ride tomorrow`).

### `ask_outstanding` rests on a recorded send

`morning-checkin.py` calls `note_ask_sent` when it appends the ask. *"It is Sunday and
nothing is declared"* is a different statement: `sunday_hours_ask` returns `""` and sends
nothing while the illness flag is up, and in that state an unexplained number is answering
one of the card's other questions.

Two traps found while building it, both now covered by tests:

- **`note_ask_sent` must not look like a legacy flat file.** It writes
  `{"asks": {...}, "declarations": []}`, and an *empty* declarations list is falsy - so
  without the `_MANAGED_KEYS` check in `_is_legacy_flat`, `day_shape` would hand that
  bookkeeping to `session_library.reconcile_day_rules` as the athlete's day shape, for
  every athlete, every Sunday.
- **Which week a reply is about must be read, not recomputed.** `target_week` resolves an
  outstanding ask first. "The Monday after today" only equals the asked-about week on a
  Sunday: a Monday-morning reply - well inside `_ASK_WINDOW_HOURS`, which exists precisely
  so it still counts - would otherwise land on the *following* week, leaving the week the
  athlete was asked about on the config fallback.

### What the follow-up had to do

1. **Recognise the reply.** A bare number in the ~1–40 range, on a Sunday, within a few
   hours of the ask, or any message containing an hours figure ("about 14", "14h",
   "maybe 12-13" → take the lower bound). Mirror the shape of
   `illness.looks_like_illness_statement` / `parse_illness_message`: a narrow detector
   plus a parser, both unit-tested, both refusing rather than guessing.
2. **Call `weekly_availability.record(slug, next_monday, hours=…, constraints=…,
   source="telegram-reply")`.** It is atomic, replaces same-week declarations, prunes
   history and raises on an out-of-band figure. Do not write the file directly.
3. **Capture the prose.** Anything in the reply that is not the number goes into
   `constraints` verbatim — it reaches the Stage-1 planner.
4. **Confirm back in one line**, including the derived consequence in the athlete's
   own coaching-level language, so a mis-parse is visible immediately.
5. **Allow a mid-week correction.** `record` already replaces, but re-running the build
   for a week already pushed is a separate decision — do not do it silently.

~~Also outstanding: `lib/plan_tools.py:663-667` (`cmd_validate`) still holds an inline
copy of the hours-ceiling maths.~~ **Fixed 28 Jul 2026 by `68b5ea3`** ("plan_tools
cmd_validate: resolve the weekly cap through the shared precedence"). Note that
`plan_builder._weekly_tss_cap`'s own docstring still describes that inline copy as
outstanding and owned by a concurrent ticket - that comment is now stale.
