# ClaudeCoach — the macro (block) planning layer

Status: **design + slice 1 shipped** · 2026-07-28 · read alongside
`docs/planning-architecture.md` (this is the artefact above its Layer 1).

Public repo: this file describes mechanisms only. Athlete numbers below are
illustrative shapes, not records.

---

## 1. The gap

The Sunday generator builds **exactly six days**. `weekCalendar` always ends the
following Sunday and there is no artefact above it. Everything block-level is
therefore improvised, or decided by a mechanical cadence that has never seen the
block:

- Which week of a five-week Peak carries the long race-simulation brick.
- Which week the long-run peak lands in, and at what distance.
- Which weeks unload. This *was* `deload_every_n_weeks` counting from
  `plan_start` — a modulo, with no reference to how much CTL is left to find.
  Partly closed on 28 Jul 2026: `plan_tools.block_deload_weeks` now places the
  cadence's down-weeks by the block (§3a). The remaining improvisation is which
  week carries which key session, not which week unloads.
- How an environmental block (sauna) overlays the load weeks it lands on.
- Whether the weeks that remain can reach the CTL the race needs *at all*.

Three live consequences, all verified on 28 Jul 2026:

1. An athlete's whole Peak block asked for ~5-15% more weekly load than his own
   hours ceiling allows, every week, because the configured phase CTL target is
   unreachable inside that ceiling. The weekly generator has only two moves and
   both are wrong: obey the target and hard-fail the load cap (which is what the
   plan audit reports), or obey the cap and quietly miss the target.
2. Two athletes project to arrive at race week **below** `ctl_targets.race_min`.
   In one case the deciding factor is a single cadence deload landing on one of
   the last loading weeks: with the cadence deload the block misses the target,
   without it the block just reaches it. A placement decision nothing owns is
   deciding whether the block is feasible. **Fixed 28 Jul 2026** for that case by
   §3a: Kathryn's week-16-of-18 deload moves to week 15, and her projected
   race-week CTL goes 74.4 -> 76.3 against `race_min` 76. The other athlete
   (Calum, 2.0 short) is NOT a placement problem — his only remaining deload is
   already the earliest week available, and moving it later would push it into the
   window §3a exists to keep clear. His gap is a target/ramp-cap question.
3. The plan audit reports a `0 TSS vs target` failure for the week after next for
   every athlete, every day — an artefact of the six-day horizon, not a real
   defect (§7).

## 2. The artefact

`athletes/<slug>/reference/macro-plan.json` — beside `training-blueprint.json`,
inside the gitignored `athletes/` tree (it carries dates and distances; the repo
is public).

It is the **skeleton the Sunday generator fills in**. It is an *input*, authored
and edited by the coach, not a generated artefact — this is the single most
important difference from `training-blueprint.json`, which is regenerated from
config and must never carry a hand-edit.

```json
{
  "schema_version": 1,
  "slug": "<slug>",
  "block": {"name": "Peak", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
  "updated": "YYYY-MM-DD",
  "weeks": [
    {"week_start": "YYYY-MM-DD",
     "intent": "load",                       // load | deload | taper | race
     "slots": [
       {"role": "race_sim_brick", "sport": "Brick", "minutes": 300},
       {"role": "long_run_peak",  "sport": "Run",   "km": 35}
     ],
     "overlays": ["heat_block"],
     "note": "coach prose, free text"}
  ]
}
```

`slots` carry a **role and a magnitude, never a session**: no segments, no zones,
no TSS. Structure stays with the session library (Layer 0/2) and load stays with
the engine (§3). A slot is a booking, not a workout.

Validator: `ironman-analysis/primitives/macro_plan.py`, a pure module mirroring
`primitives/blueprint.py` — required keys, ISO dates in order, weeks contiguous
and inside the block, roles from a closed vocabulary, no slot in a `taper`/`race`
week. Malformed fails loudly at edit time, not at planning time.

## 3. Who owns which number

The macro layer must not become a fifth source of truth. Ownership is therefore
split by *kind* of number, and the macro layer owns exactly one column of it.

| Number | Owner | Macro layer |
|---|---|---|
| Weekly TSS target | `plan_tools.required_tss` | reads, never writes |
| CTL ramp cap | `athletes.json max_ctl_ramp_per_week` | reads |
| Weekly load ceiling | `plan_builder._weekly_tss_cap` → profile hours × `primitives.blueprint.tss_ceiling`, else phase `tss_ceiling` | reads |
| Phase dates + CTL milestones | `athletes.json phase_tss` / `ctl_targets`, mirrored into the blueprint | reads |
| Intensity distribution | blueprint `phase.distribution` | reads |
| Heat-block start | `lib/heat.py` (blueprint `env_protocols`) | reads |
| Run volume progression | `lib/progression.py`, `plan_tools.run_caps` | reads |
| Session structure | session library (Layer 0/2) | never touches |
| **Which week carries which key session** | **macro layer** | **writes** |
| **Which weeks unload** | **macro layer**, expressed through the EXISTING knobs `deload_skip_weeks` + `manual_easy_weeks` | **writes** |
| **Long-run peak week + distance** | **macro layer** | **writes** |
| **Which weeks an env block overlays** | **macro layer** (start date still from `heat.py`) | **writes** |

Deload placement deserves a note: the macro layer does **not** get its own deload
mechanism. It writes the two override channels that already exist and that
`required_tss` already honours (`deload_skip_weeks` to move a cadence deload off
a week, `manual_easy_weeks` to declare one). The engine stays the only thing that
turns "this week unloads" into a number.

## 3a. Block-aware deload placement (landed 28 Jul 2026)

The cadence proposes; the block decides. `plan_tools.block_deload_weeks(cfg)` is
pure, depends on `cfg` alone (never on `today`, so a week reads the same in the
Sunday build, the audit, the projection and `required_tss`'s own `today - 7`
lookback), and returns the deload weeks for the WHOLE plan. `required_tss` asks it
instead of testing `week_now % n == 0`, so there is still exactly one source of the
weekly target and one source of a week's type.

The rule, in one line: **a deload must leave more than `LATE_LOADING_WINDOW` (2)
loading weeks between itself and the taper.** The taper *is* the unload; a deload
abutting it unloads twice into race day, and it spends the last week in which
fitness can still be added. A deload that breaks the rule is moved EARLIER:

- destination = the latest earlier week that is free (not another down-week, not
  in `deload_skip_weeks`, not a `manual_easy_weeks` week), not adjacent to another
  down-week, far enough from the taper itself, and **within one cadence period**
  of where it came from — dragged further, a deload stops being that block's
  recovery and starts oscillating load/recover week about;
- the **count of down-weeks is preserved**. Nothing here deletes recovery to chase
  CTL: a small fitness gap is a far cheaper failure than an overtrained athlete.
  Only the position is negotiable;
- if no legal destination exists, the deload **stays put** and is reported in
  `unmoved_late`. An unrepairable block is reported, not silently stripped of its
  recovery — and `macro_projection`'s `deload_placement` flag then still fires.
  That includes the case where the week beside the offending deload is already a
  declared `manual_easy_weeks` down-week, so the block would unload for a
  fortnight. Dropping the cadence deload is very likely right there — it is what
  Jamie's hand-written `deload_skip_weeks` did on 16 Jul 2026 — but that stays a
  per-athlete judgement made through that override, not a rule.

Taper weeks are not deloads and are never touched: the taper branch of
`required_tss` returns before placement is consulted.

The ramp cap needs no special handling — a vacated week's target is still
`min(required, ramp_capped)`, so moving a deload cannot create a week that
breaches `max_ctl_ramp_per_week`. `test_macro_projection` asserts no week becomes
ramp-limited by the move, and `macro_projection` imports `LATE_LOADING_WINDOW`
from `plan_tools` rather than restating 2, so the rule and the flag reporting on
it cannot drift apart.

Live effect on 28 Jul 2026 (all three athletes, `macro_projection --all`): one
week changes. Kathryn 17 Aug -> 10 Aug, `ctl_shortfall` closes (74.4 -> 76.3 vs
`race_min` 76). Jamie's placement `{4, 8, 16}` and Calum's `{4, 8}` are untouched,
and their projections are byte-identical before and after.

## 4. The key question: prescribe, or constrain?

**The case for PRESCRIBE** (the skeleton dictates next week; the generator fills
detail). The stated lesson of this repo is *take the mechanical parts out of the
LLM's hands* — the 15 Jun failure was an LLM handed a 23k-char prompt and trusted
to follow it. Block sequencing is mechanical in exactly that sense: which week
holds the race sim is a decision with one right answer given the block, and it is
not a judgment call the weekly path should be re-making from six days of context.
A prescriptive skeleton is inspectable, diffable and testable; a generator that
merely *proposes* will keep improvising, because improvising is all it can do
without an artefact.

**The case for CONSTRAIN** (the generator proposes; the macro layer vetoes). A
prescribed *number* written weeks in advance is stale the moment execution
diverges from projection — and it always does. A stale prescribed weekly TSS is
worse than no number, because it competes with `required_tss`, which recomputes
from today's actual CTL. Two writers for one number is precisely the drift class
`lib/athlete_targets.py` exists to prevent. And `engine.py:195-198` shows what
this system does when two authorities disagree: it resolves rule-vs-blueprint in
the blueprint's favour **silently**. Add a second prescriptive authority and you
have added a second silent-resolution site.

**The case against CONSTRAIN, though**, is decisive on its own: a veto arrives too
late. Vetoing week three of a five-week block does not recover the block. The
failures in §1 are not "this week is wrong", they are "the shape of the remaining
weeks cannot work" — unreachable at the moment the block starts, and knowable
then.

**Decision — split by kind of number:**

- **LOAD: neither prescribe nor veto. PROJECT.** The macro layer computes what the
  existing engine will ask for in every remaining week, at the ceiling and ramp
  cap that already exist, and reports infeasibility *to the coach*. The remedy is a
  config change (`phase_ctl`, `race_min`, `max_hours_per_week`, deload placement)
  which the engine then reads on its own. This adds **zero** new authority over
  any load number, so `required_tss` stays the single source of truth.
- **PLACEMENT: PRESCRIBE.** The skeleton names the week and the role; the generator
  fills in the session. Nothing owns these today, they cannot be decided from six
  days of context, and they do not go stale when CTL diverges — "the race sim is
  in the second-to-last Peak week" is still true if the athlete gets ill; only its
  *content* is renegotiated.

**Conflict rule — loud, never silent.** When a prescribed slot cannot be honoured
(illness gate, `day_rules`, a slot that would breach the ramp), the builder emits a
**hard violation naming the slot and the reason**, and the week does not push. It
must not silently drop the slot, and it must not silently override the gate. This
is the deliberate opposite of the `engine.py:195-198` precedence pattern, and it is
why placement is safe to prescribe: an unhonoured booking is visible.

## 5. Slice 1 — shipped: the read-only macro projection

`lib/macro_projection.py` — `python3 lib/macro_projection.py --all`. Writes
nothing, pushes nothing, touches no calendar. Not wired to cron.

It walks every remaining week to race day, calling **the same
`plan_tools.required_tss`** the generator, brief, audit and dashboard call, seeded
with the CTL projected from the previous week via **the same
`primitives.load.compute_projected_ctl`** that `compute_required_tss` inverts. It
introduces no arithmetic of its own; it is the existing single source, iterated.
`project_block()` is pure — CTL, last week's actual load, the per-week ceiling
resolver and the heat-block start are all injected; the CLI does the IO.

The buildable load for a week is `min(engine target, ceiling × (1 + tolerance))`,
with the tolerance **read off `validate_week`'s own signature** rather than
restated, so the macro layer can never flag a week the builder would accept. A
projected shortfall is therefore a *lower bound* on the problem: it already assumes
every week is built to the maximum the validator tolerates.

Which is exactly why it reports **two trajectories**. The headline CTL spends that
tolerance every week; on a ceiling-infeasible block that means it is reached only
by building weeks the plan audit hard-fails. So the projection also reports the
same block held **strictly at the ceiling** (`ctl_at_*_at_ceiling`). Without this,
an athlete can show `ceiling_infeasible` *and* an apparently healthy projected CTL
and the two read as contradictory, when in fact the second is conditional on the
first. A week landing exactly on ceiling × (1 + tolerance) is deliberately **not**
flagged infeasible — that is what the validator tolerates — but the strict line
shows the cost of living there.

Flags:

| Code | Severity | Means |
|---|---|---|
| `ctl_shortfall` | hard | projected CTL at race-week start is below `ctl_targets.race_min` |
| `ceiling_infeasible` | hard | the engine target exceeds the phase load ceiling — the CTL target is unreachable inside the athlete's hours |
| `no_slack` | warn | every remaining loading week is pinned to the ramp cap; any missed week is unrecoverable |
| `deload_placement` | info | where the down-weeks fall, and whether one still sits inside the last `LATE_LOADING_WINDOW` loading weeks after `block_deload_weeks` has placed them (i.e. a block it could not repair) |
| `heat_overlay` | warn | the sauna block overlays weeks already at ≥90% of the load ceiling |
| `no_macro_plan` | info | no skeleton exists, so placement is still improvised |

Exit 0 clean / 1 on any hard flag. Deliberately **not** added to cron and
deliberately **not** routed through `ops_log.alert` in this slice: three athletes
flag hard today, and a job that fails on day one trains the reader to ignore it
(the same lesson the audit's baseline mechanism encodes). Wire it up after the
config corrections it surfaces have been made — or behind the same baseline
mechanism.

**Why this slice and not the skeleton first.** The skeleton is the bigger prize,
but it needs coach input (which week, what distance) and it writes to
`athletes/`. The projection needs no input, writes nothing, cannot break a build,
and it answers the question that decides *what the skeleton should say*: two of
the three athletes' blocks do not currently close, so sequencing key sessions
inside them would be arranging furniture in a house that is 2 CTL short. Fix
feasibility, then place.

## 6. Slices 2-4 (not built)

2. `primitives/macro_plan.py` validator + a hand-authored skeleton for one
   athlete, read by nothing. Zero risk, proves the schema.
3. `plan_audit` reads the skeleton: an unhonoured slot in a *past* week is a
   violation. Still no writes to any plan — the audit only observes.
4. The generator consumes the skeleton: Stage 1 receives the week's slots as
   required bookings; Stage 2 hard-fails an unhonoured slot per §4. Only after 2
   and 3 have run clean for a full block.

Not in scope at any slice: the macro layer emitting a weekly TSS number.

## 7. The `0 TSS vs target` audit artefact

The macro layer does **not** legitimately make it disappear, and the skeleton must
not be allowed to. The audit compares *calendar events* to a target; a week that
has not been generated has no events. A skeleton is a booking, not a built week —
counting it as load would mean the audit passes a week that does not exist.

The correct fix is horizon-awareness in `plan_audit.py`, and it is a **separate,
small change**: a week starting beyond the last generated week reports as
`SKIPPED`, which is already a first-class non-failure category in that file, with
exactly this rationale in its own comment ("not checked" must never read as
"checked and passed"). That removes one `WEEKLY_LOAD` count per athlete from the
baseline, honestly.

`plan_audit.py` is owned by concurrent work (the audit-baseline and plan-audit
tickets) and is **not touched by this ticket**. The change is described here so
whoever holds that file can make it; the macro layer's only contribution is that
once a skeleton exists, "the last generated week" and "the intended horizon" are
both knowable, so the audit can distinguish *not built yet* from *built empty* —
which today it cannot.

## 8. Known limitations of slice 1

- **Two CTL projectors exist.** `compute_projected_ctl` steps `ctl += (d-ctl)/42`;
  `project_pmc_daily` uses `1-e^(-1/42)`. This module uses the former because it
  is the exact inverse of the `compute_required_tss` the engine targets with.
  Sub-0.1-CTL divergence over a block, but they should be unified.
- **The projection assumes full compliance** from today forward. It applies last
  week's actual load to the first week only (as the audit does); a future miss is
  unknowable, so a shortfall figure is optimistic.
- **Derived phase targets move.** An athlete with `race_min` but no `phase_ctl`
  has milestones re-derived from current CTL every week, so the projected target
  drifts with the projection. Worth flagging separately: the derived peak target
  can sit *below* `race_min`, i.e. the engine chases a number that does not reach
  the race requirement.
- **Week granularity.** Race-day CTL is reported as CTL at the start of race week
  (an overstatement by the race week's own taper decay) rather than interpolated
  to the day, to avoid inventing a third projector.
