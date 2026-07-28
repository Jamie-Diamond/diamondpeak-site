# Rule lifecycle — identity, type, enforcement

Status: design + mechanical half implemented (28 Jul 2026). Content migration NOT started;
it needs coach decisions, not code. No rule's prose has been changed by this work.

## The problem, in numbers

148 standing rules across four files (56 shared, 56 + 26 + 10 athlete-specific), against a
ceiling of 90 per file. All 148 are injected as flat prose into every prompt. Three
consequences, all observed live:

1. **No identity.** Rules are cited by LINE POSITION. `lib/progression.py`'s docstring
   cites "rule 96" for a file with 56 rules; a `current-state.md` cites six more numbers
   the same way. Every such citation is already wrong or will be within days, because the
   files gain and lose lines daily. Prose in the files themselves now refers to other rules
   by number and even declares supersession of "the prior philosophy" — cross-references
   the system cannot resolve.
2. **No typing.** A day anchor that could be enforced in code, a product lookup table, a
   data-sourcing method and a coaching stance are the same kind of object: a `[perm]` line.
   Because they are indistinguishable, all four get the only treatment prose can get — hope
   that the model reads them.
3. **No reconciliation.** A `[perm]` rule can be silently unenforced (the config never
   learned it) or silently breached (the build ignored it), and nothing reports either.
   Eight such contradictions exist right now (see *Detector*), including a rule the coach
   reviewed and explicitly ACCEPTED that the same week's plan overrides twice.

## Type taxonomy — the proposed three did not survive the corpus

The review proposed three types: constraint, reference fact, philosophy. Tested against all
148 rules, that split fails in two ways.

**It is missing two large classes.**

| Type | What it does | Where it belongs | n (auto-classified) |
|---|---|---|---|
| `constraint` | binds what may be planned or scheduled | typed config + a check that can fail | 43 |
| `reference` | a fact or stored value to look up | a data table injected as DATA, not instruction | 43 |
| `method` | which source or tool a number must come from, and how to reconcile it | tool routing + hard-rails already injected from code | 17 |
| `format` | the shape of an output: debrief, activity description, address terms, labels | an output contract, checkable after generation | 24 |
| `philosophy` | a stance or judgement priority with no mechanical test | prose in the prompt — the only class that must be | 5 |
| `unclassified` | no cue fires; coach must type it by hand | — | 16 |

`method` alone is a fifth of the shared file and is neither a plan constraint, a lookup, nor
philosophy: it says which of two data sources is authoritative for a computation. `format`
is a quarter of the total. Collapsing either into "philosophy" would put the largest, most
mechanically checkable classes into the one bucket that gets no enforcement at all.

**Over half the rules are more than one type at once: 82 of 148 (55%).** The commonest shape
is a lookup table that also carries a prohibition, with an incident trail appended — one
line that is simultaneously a reference fact, a constraint on output, and provenance. So
`type` cannot be a single field used to route a rule to one home. The registry therefore
stores a LIST of types plus a primary, and the primary is chosen by consequence:
`constraint > reference > method > format > philosophy`. A line that both binds the plan and
states a figure has to be enforced, so it is primarily a constraint; the figure it carries is
separately extractable as reference data.

**Verdict: five types, multi-valued, with the multi-type rate reported.** A 55% multi-type
rate is itself the finding: many rules should be SPLIT during migration, and the honest count
of how many is a coach decision, not a classifier output.

## Constraints must name their enforcing code path

A rule typed `constraint` asserts that certain plans are invalid. If no code can detect the
invalid plan, the rule is a wish. **At least 24 of the 43 auto-classified constraints name no
enforcing code path** - a lower bound, for the reason below. The enforceable surface today is
small and entirely known:

* `day_rules.{swim,bike,run}_days` -> `validate_plan:{sport}_forbidden_day` (hard) or
  `validate_plan:{sport}_directed_day` (soft, when the deviation is in the athlete's
  override register — see *Day rules are guidelines* below)
* `day_rules.{swim,bike,run}_days_expires` -> the same two checks, per event date
* the override register -> `validate_plan:day_rules_drifted`
* `day_rules.strength_max` -> `validate_plan:strength_over_cap`
* `max_ctl_ramp_per_week` -> `validate_plan:ctl_ramp`
* blueprint `tss_ceiling` / `required_tss` -> `validate_plan:weekly_tss_cap|weekly_tss_floor`
* blueprint `distribution` -> `validate_plan:intensity_distribution`
* plus `monotony`, `run_weekly_volume`, `run_long_volume`, `distance_duration_mismatch`

Everything else asserted as a constraint in prose — per-session duration caps, per-sport
minimum session counts, minimum useful session duration, a cap on two-a-day days per week,
weekday-only training, per-discipline weekly session floors — has nothing that can fail. One
athlete's own rule already says so, carrying an OPEN ACTION asking for a minimum
sessions-per-sport default to be encoded in config.

`enforced_by` names a check that binds a rule's SUBJECT; it does not prove the rule's
specific assertion is checked, and the gap is not academic. The day-level checks are
`day_rules.<sport>_days`, which can express "runs happen on these days" and cannot express a
session KIND: "the LONG run is Wednesday" maps to `run_forbidden_day`, yet nothing fails when
the long run lands on another day `run_days` also permits - which is why the detector has to
find that breach itself. 14 of the 19 constraints counted as enforced rest on a day-level or
cap check strictly weaker than the rule mapped to it, so treat 24 as the floor.

**Rule:** a new `constraint` must name an existing check, or a config key that feeds one. If
it cannot, it is either retyped as philosophy (honest: guidance the model may miss) or it
arrives with the config key and check that would enforce it. No third option, or the count of
unenforceable constraints only grows.

## Day rules are guidelines: overrides and dated exceptions (28 Jul 2026)

The coach's ruling, verbatim: **"I told it this week to swim on wed, so we swim on wed,
rules are guidelines."** `day_rules` describe an athlete's normal weekly pattern and the
coach overrides them conversationally, so a `constraint` typed off `day_rules` is not an
invariant. Two mechanisms make that true in code without giving up the signal — a
generator that drifts off the pattern with nobody asking is a genuine defect, and
`day_rules` are what catch it.

### 1. Per-session override register

`athletes/<slug>/reference/day-rules-overrides.json`, modelled on the reviewed-exception
register `rules-lint-accepted.json` that sits beside it: a flat map of stable id -> prose
recording what was accepted and when. **It is under `athletes/`, which is gitignored, so
it never appears in a diff** — this schema is its documentation.

```json
{
  "swim:2026-07-29": "Coach-directed: the coach instructed a Wednesday swim for the week
                      of 27 Jul 2026 (\"rules are guidelines\"). day_rules.swim_days stays
                      [Tue, Thu] because that is what the athlete actually does — Tue x7,
                      Thu x7, Wed x0 since 1 Jun. Recorded 2026-07-28."
}
```

* Key = `<family>:<YYYY-MM-DD>`; family in `{swim, bike, run}` (the `day_rules` key minus
  `_days`, so Ride/VirtualRide/GravelRide share one). Value = free prose naming who
  directed it and when.
* A matching deviation becomes a SOFT `{sport}_directed_day` carrying the note in its
  detail, in `plan_audit`'s `DIRECTED` category. No match = HARD `{sport}_forbidden_day`,
  exactly as before.
* Fails CLOSED. A missing, corrupt, mistyped or empty-note entry grants nothing, so no
  check can be silenced by a broken file.
* **Granularity is per session, deliberately.** Per WEEK lets a second, undirected move
  hide behind the directed one. A STANDING amendment with no end date is precisely how
  Calum's Saturday exception became permanent — it recreates the problem. A dated key is
  also self-expiring: an entry for a past date can never excuse a future deviation.
* **Anti-drift.** `DRIFT_THRESHOLD` (3) directed hits on the same sport+weekday inside
  `DRIFT_WINDOW_DAYS` (28) raise a HARD `day_rules_drifted` naming
  `day_rules.<family>_days` as the remedy. So the register cannot be used to move the
  pattern by stealth, and the audit cannot go quiet on the whole category. It ages out on
  its own, so it can never be permanently red.

**Writers.** `lib/day_overrides.record()` is the whole write surface, plus a CLI:

```
python3 lib/day_overrides.py --base-dir ClaudeCoach --slug jamie --list
python3 lib/day_overrides.py --base-dir ClaudeCoach --slug jamie \
    --sport swim --date 2026-07-29 --note "Coach-directed in Telegram, 27 Jul 2026."
```

**FOLLOW-UP OWED — the bot side.** The instructions arrive in Telegram, and today the
register is filled in by hand, so an override that is never typed up still reads as a
breach. `telegram/bot.py` needs: when a coach message re-days a session (the same
intent that already triggers a replan/move), call
`day_overrides.record(slug, BASE, sport, session_date, note)` with the verbatim
instruction and the message date as the note, before pushing the moved event. One call
site, no new format, no schema decision left open. Until then the coach runs the CLI, or
edits the JSON.

### 2. Dated exceptions in config: `<key>_expires`

The `[expires:DATE]` tag already time-boxes a RULE; nothing time-boxed a CONFIG day, so
Calum's `[expires:2026-09-05]` Saturday-long-ride exception was encoded by adding `Sat`
to `bike_days` with nothing to revert it. The dated exception became permanent and the
`[perm]` "all cycling on weekdays only" rule was silently lost.

```json
"bike_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
"bike_days_expires": { "Sat": "2026-09-05" }
```

* `validate_plan` permits the day for sessions **up to and including** that date, judged
  per EVENT date — so the module stays pure and future weeks audit correctly.
* **A sidecar is a dict, and `_normalise_day_rules` skips non-list values, so this
  encoding is a NO-OP for any code path that has not been taught it.** That was the
  deciding constraint: `config/athletes.json` is hand-edited on the live box and is not
  deployed with the code, so an inline `"Sat [expires:...]"` tag would have been parsed by
  the running validator as an unknown day, dropped Saturday entirely, and hard-failed
  Calum's long ride the moment the config was saved. Verified on live data: the audit
  signature for all three athletes is byte-identical before and after the config edit
  under unmodified code.
* It **reverts on its own** (the day goes back to forbidden, with a detail naming
  `<key>_expires` and saying the exception EXPIRED — not a false claim that the day was
  never permitted).
* It is **visible while live** in the config the coach edits, and it **cannot be
  forgotten**: `rule_conflicts` axis A raises `config_undated_exception` (hard) when a
  dated prose rule's day sits in `day_rules` with no sidecar, and
  `config_exception_expired` (soft, "delete it") once a sidecar's date has passed. Both
  are self-clearing, so neither can be permanently red.
* A garbled date or day name is dropped, leaving the day permitted — a typo must never
  invent a new failure.
* **The day must ALSO be listed in `<key>_days`.** The sidecar NARROWS an existing
  permission; it never grants one. That is exactly what makes the encoding inert for code
  that has not been taught about it. A sidecar naming a day the list omits is a silent
  no-op, and axis A cannot see it either.

### Known rough edges (documented, not fixed)

* `_drift_violations` runs once per audited WEEK, and `plan_audit` audits two, so the run
  on which a third directed hit lands emits `day_rules_drifted` TWICE and `RULES` jumps by
  two. The alert is correct; the duplication is not. The natural fix is the `if wk == 0`
  gate `plan_audit` already uses for the streak store — deliberately not applied here
  because `lib/plan_audit.py` was contended at the time of writing and the edit was kept
  to four lines.
* `escalate_repeats` sees `{sport}_directed_day` (it is soft) and will eventually append
  `{sport}_directed_day_persistent`, which `plan_audit`'s routing catches by neither
  suffix nor severity, so it is silently dropped. Harmless — `day_rules_drifted` carries
  the real signal on a tighter window — but a counter accumulates in
  `config/plan-audit-streaks.json` for a code nobody reads.

## Identity: immutable IDs in a sidecar, not in the rule line

Every rule gets a permanent ID (`<slug>-NNN`), assigned at capture, never reused, never
deleted — a rule removed from the file keeps its ID and is marked `missing`.

**IDs are held in a sidecar registry, not written into the rule text.** Two live mechanisms
key off the rule LINE, and an inline identifier breaks both:

* the reviewed-exception register is keyed by a hash of the rule line, so injecting an ID
  changes every hash and silently re-fires every accepted exception as a new finding;
* the capture guard requires every run of digits in a rule to survive a fold, so a
  digit-bearing ID becomes a figure the guard must preserve, and can abort folds that pass
  today.

A sidecar keyed by a content hash has neither hazard, changes zero bytes of the coach's
prose, and needs no approval to build. It lives beside the rules it describes and stores a
token fingerprint rather than a copy of the prose.

**Identity through rewording.** The capture guard exists to let a refinement be FOLDED into
the rule it extends, which changes the content hash. So an unmatched line rebinds to its own
registry entry when the old rule's content words are still >=80% present in the new line (the
guard's own rewording budget) AND every figure survives (the guard's own
numbers-must-not-drift rule). Below that, a new ID is minted: a duplicate costs the coach one
merge decision, whereas a wrong rebind silently transfers one rule's review history onto
another.

## Reference facts belong in a data table, injected as DATA

Reference rules are the 43 that state a stored value. Three failures show why prose is the
wrong home: repeating the same fact in two prose places for three days did not stop the model
answering a direct factual question wrongly; a preference stayed unreliable while it was
prose and became reliable the day it became a structured flag; and a structured flag for the
same preference on another athlete had already been working.

Target: a per-athlete `reference/facts.json` of typed values (equipment capacities, pool
length, threshold values, product carbohydrate content), injected into the prompt as a
labelled DATA block — a table the model looks things up in, not an instruction it must
remember to obey. Migration is per-fact extraction from prose, so it is coach work; the
mechanical half is only identifying which rules carry facts.

## Philosophy: review date, last-relevant stamp, and a cap

Philosophy is the only class that must be prose, and it is the smallest (5 auto-classified,
plus whatever the 16 unclassified turn out to be). Registry fields, unset until the coach
sets them:

* `review_by` — a date. Past it, the rule is reported for review. This deliberately mirrors
  the `[expires:DATE]` tag that already works, and does not replace it: `[expires:]` deletes
  a rule automatically, `review_by` only asks a question.
* `last_relevant` — the date the rule last changed an output. A philosophy rule that has not
  mattered for months is a candidate for retirement rather than injection.

**Cap on injected philosophy: 15 rules per athlete.** When the cap is hit, capture does not
silently drop the newest and does not silently truncate the oldest: the new rule is written,
and the athlete's registry is flagged `philosophy_over_cap`, which surfaces the rules with
the oldest `last_relevant` (unset last, i.e. never-yet-relevant first) as a review card
asking the coach to retire or promote one. Both silent options are worse: dropping the newest
loses a decision the athlete just made, and truncating the oldest deletes reasoning nobody
reviewed. The existing standing-rule ceiling (90) stays as the hard backstop.

## Detector — contradictions found mechanically

`lib/rule_conflicts.py`, read-only, five axes:

| Axis | Detects |
|---|---|
| A `config_gap` / `config_absent` | prose names a training day the athlete's `day_rules` does not permit, or constrains a sport for an athlete who has no `day_rules` at all |
| B `*_prose_day_breach` | a planned session of the anchored KIND on a day the prose rules out |
| C `*_intensity_breach` (advisory) | that session advertises an intensity the prose reserves against |
| D `accepted_rule_overridden` | a withholding rule the coach reviewed and ACCEPTED, served anyway in the same week |
| E `expiry_conflicts_perm` | a live dated exception and a `[perm]` rule that disagree |

Precision matters more than recall here, because a detector that reports a breach for every
session buries the real ones. Two properties earn that precision: an anchor binds a session
KIND, not a whole sport ("the long ride is Friday" does not forbid a Tuesday spin), and a
weekday inside a provenance trail ("confirmed 27 Jul 2026 ... Sunday ...") is history, not a
rule. An unexpired `[expires:]` exception suppresses the breach it explicitly permits, so
correct use of the tag is never punished.

Axis C reads session prose and prints the cue that fired: advisory, never a gate.

## Blueprint vs prose (`lib/engine.py`, planning-authority block)

The engine resolves prose-vs-blueprint conflicts in the blueprint's favour, silently. That is
a deliberate fix for a live failure in which prose overrode the numeric spec and shipped a
week with a required quality slice zeroed while asserting compliance. The authority block
states the only thing that may zero a required slice is a structured injury/illness gate —
never prose.

**Recommendation: keep blueprint-wins, remove the silence, add a promotion path.**

* Keep the precedence. Letting a `[perm]` prose line block a build reverses an
  incident-driven fix and hands veto power to the least reviewed, least structured surface in
  the system — the one that has accumulated 148 lines with no typing and provable internal
  contradictions. Risk of keeping it: a genuine athlete constraint keeps being overridden.
* Report every override. When the blueprint overrides a prose rule, emit the rule's ID and
  the slice to the ops log and a coach card. The cost of the current silence is exactly the
  observed case: a rule accepted as deliberate, overridden twice in the same week, with
  nobody told. Risk: card volume — bounded by reporting once per rule ID per week, not per
  session.
* Add promotion. A prose constraint that legitimately should bind gets promoted to typed
  config, where a check that can fail enforces it — the route the athlete's own OPEN ACTION
  rule already asks for. That is the only honest way for a coach's constraint to win, and it
  makes "a constraint must name its enforcing code path" achievable rather than aspirational.
* A `[perm]` rule may block a build in exactly one case, unchanged from today: when it is
  backed by a structured gate. One rule, one mechanism.

## Migration proposal (needs coach decisions — not started)

Mechanical, no content change, already implemented:

1. Assign all 148 IDs and auto-fill `type` / `types` / `enforced_by` in the sidecar
   (`--write`). Idempotent, and survives reordering and folds.
2. Report the three review queues: 16 unclassified, 82 multi-type, 24 constraints with no
   enforcing code path.
3. Run the detector; today it reports 8 contradictions, 6 of them hard.

Coach decisions, per rule, in this order — highest value first:

1. **24 unenforceable constraints.** For each: encode it as config plus a check, or retype it
   as philosophy. Highest-value queue: these are the rules the system claims to obey and
   cannot.
2. **8 contradictions.** Each needs one answer: which side is right — prose, config, or the
   calendar. Two are pure config drift (prose and config disagree about which days a sport
   may fall on) and are a one-line config edit once the coach confirms the correct days.
3. **82 multi-type rules.** Split or leave. Splitting is what lets the facts move to the data
   table and the constraints move to config; leaving them is legitimate but keeps them in
   prose. Best return: the reference-heavy lines, where prose repetition has already been
   shown not to fix a factual error.
4. **16 unclassified.** Type by hand, or delete.
5. **Philosophy over cap.** After typing, any athlete over 15 philosophy rules picks which to
   retire.

Mechanical share: identity, typing pre-fill, enforcement mapping and contradiction detection
— everything except the decisions, which is roughly 130 line-items of coach review across 148
rules, concentrated in the four queues above. Nothing in this pipeline edits a rule; every
content change is the coach's.
