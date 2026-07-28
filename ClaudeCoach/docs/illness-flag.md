# Illness flag, and the heat-silence flag

Two structured state flags that used to exist only as prose, where no code could act on
them. Both are **surfacing gates**: they change what the coach says, not what any model
computes.

## Why the illness flag exists

26 Jul 2026. Kathryn, on antibiotics recovering from tonsillitis, rode 76 minutes and
reported her fuelling. The reply opened *"You rode 76min at zero carbs"* and closed
*"works against you"*, with no acknowledgement that she had trained at all.

Nothing malfunctioned. `athletes/kathryn/system_prompt.txt:67` says to log session
feedback and confirm in one line; `lib/coaching_levels.py` (mid) says "matter-of-fact,
never gushing"; her front-load-carbs standing rule makes fuelling a thing to flag. Her
tonsillitis existed only as prose in `current-state.md`, which no prompt reads. There
was no illness gate anywhere in code.

`README.md` §Planning authority point 3 has promised an "injury/illness hard-gate read
from structured `current-state.json`" since 22 Jul. The injury half existed
(`lib/injury.py`). The illness half did not. This is it.

## Schema — `athletes/<slug>/current-state.json`

```json
"illness": {
  "status":         "active",              // active | recovering | resolved
  "condition":      "tonsillitis",         // short label, optional
  "started":        "2026-07-24",          // ISO date, REQUIRED
  "expected_until": "2026-08-02",          // ISO date, optional
  "note":           "on antibiotics",      // free text, optional
  "training_gate":  "none",                // none | no_quality | no_training
  "logged":         "2026-07-28T09:14:03"  // written by the setter, audit only
}
```

Module: `lib/illness.py`. Nothing else in the repo owns this key.

* `status` — `active` and `recovering` both suppress; `resolved` (or no block)
  suppresses nothing.
* `started` — required. A block without a parseable start date is **ignored**: a
  suppression window of unknown length is worse than no flag, because nothing would
  ever lapse it.
* `expected_until` — optional, and deliberately **not** a hard stop. Recovery slips, and
  nobody should be scolded on the day a guess expires, so the flag keeps suppressing
  past that date and only lapses `STALE_GRACE_DAYS` (7) later. After that it stops
  suppressing — a forgotten flag must not soften the coaching indefinitely.
* An open-ended flag never auto-lapses, but `needs_review` goes True after
  `REVIEW_AFTER_DAYS` (10) and the prompt block then asks the athlete once how they are.
* `training_gate` — the **only** field here that may reduce a plan. See below.

## What an active flag suppresses

Enumerated in code as `illness.SUPPRESSES`, so the prompt block and the tests read one
list:

* fuelling / carb-intake flags and any nutrition criticism
* plan-adherence and compliance criticism (missed, shortened or easier sessions)
* progression nagging (volume, ramp rate, weekly Load shortfall)
* body-composition and weight nudges

It also **requires** an acknowledgement: if the athlete trained at all, the first clause
of the reply credits that they got out and did it while unwell.

A standing rule on any suppressed topic is **suspended, not deleted** — it returns when
the flag clears. Data is still recorded exactly as normal; suppression is about what the
coach *says*, never about what it writes. And a direct question is still answered
straight: this gates unprompted criticism, not a question the athlete asked.

## What it deliberately does NOT suppress

`illness.NOT_SUPPRESSED`. Warmth does not buy softness on safety:

* injury hard-gates — a physio clearance of 0 still blocks that zone (`lib/injury.py`)
* load ceilings, deloads and taper maths
* the acute pain gate (modulation R1)
* medical escalation — say plainly when something needs a doctor
* recording facts: session-log / heat-log writes are untouched
* safety-critical corrections (heat stacking, hydration in real heat, over-reaching)

**An illness flag on its own does not reduce the plan.** With `training_gate: "none"`
(the default) the blueprint still governs and no zone slice may be zeroed on the basis
of the flag. Reducing the plan needs an explicit gate:

| `training_gate` | Effect |
|---|---|
| `none` (default) | suppress criticism only; blueprint unchanged |
| `no_quality` | Z1–2 only — overrides the blueprint's quality slice for the duration |
| `no_training` | rest is the prescription |

Keeping these separate matters: conflating "be kind" with "cut the plan" would hand the
model a lever against the blueprint-wins rule.

## Where it is read

| Surface | How |
|---|---|
| Chat (`lib/engine.py`) | `load_illness_block()` → `build_prompt`, the image path, **and `_prompt_fingerprint`** |
| Session debrief (`scripts/activity-watcher.py`) | injected under the coaching-level block |
| Daily prescription, morning check-in, evening check-in | same |
| Weekly summary | same, plus `illness.weekly_card_line()` — the week's card carries it **once** as context for the numbers |

The fingerprint entry is load-bearing, not tidiness. Chat sessions are resumed with
`--resume`, and the resume path re-sends only the live context + the new message — it
never re-injects the rule blocks. Without the illness block in the fingerprint hash, an
athlete who fell ill mid-session would keep being coached with no gate for up to
`SESSION_MAX_TURNS` turns / 24 h: exactly the failure this flag exists to stop. Putting
it in the hash rotates the session the moment the state changes.

Not wired (owned by other work): `scripts/night-before-brief.py`.

## Setting it

CLI, as the coach. Same precedent as `lib/plan_tools.py`, which the engine's accuracy
rule already has the model shell out to (`Bash` is in the engine's allowed tools):

```bash
python3 ClaudeCoach/lib/illness.py set   --athlete kathryn \
        --condition tonsillitis --started 2026-07-24 --expected-until 2026-08-02 \
        --note "on antibiotics"
python3 ClaudeCoach/lib/illness.py show  --athlete kathryn
python3 ClaudeCoach/lib/illness.py clear --athlete kathryn
```

`set` validates rather than coerces — a bad status, gate or date raises, so a mistyped
model-issued command fails loudly instead of writing a flag that never lapses. It writes
atomically and touches only its own key plus `last_updated`. `clear` marks the flag
`resolved` (keeping the record) rather than deleting it.

`illness.looks_like_illness_statement()` / `parse_illness_message()` mirror
`lib/races.py` but are **latent — nothing calls them yet, and the term list and
first-person test want more tightening before they are wired**, because a false
positive writes a flag that silently softens the coaching. With them, a Telegram
ask-and-confirm hook is a small addition when wanted:
mirror `_handle_race_capture` (parse → inline keyboard → write on the callback), asking
the one field a message rarely states, `training_gate`. No bot change is needed for the
flag to *work* — only for that UX.

## Heat silence

`athletes/kathryn/persistent-rules.md` carries `[perm] Heat: … Do not proactively
surface heat-training reminders`. Her blueprint has `env_protocols.heat {active: true,
starts: 2026-08-23}`. On 23 Aug the window opens, and until now the only thing stopping
the prompts she filed as a bug was a model obeying one sentence in a 24-line rules file.

`profile.json heat_silent: true` — mirrors Calum's `heat_protocol: false` pattern:

```json
{ "heat_silent": true }
```

`lib/heat.state()` gains two keys: `silent` (the flag) and `surface` (`active` AND
`in_protocol_window` AND NOT `silent`) — plus `heat.surfacing_allowed(slug, profile)`.
**`surface` is the key a proactive card or trigger should test.**

Gated: `scripts/morning-checkin.py` (the morning heat nudge and the heat-log read),
`scripts/watchdog.py` (the T7/T8 dose triggers and the heat-log read).

Not gated, by design: the dose model itself — the curve, the 21-day decay, the dewpoint
multipliers, the 30 min/25 °C eligibility rule, the indoor exclusion; the ambient-exposure
auto-crediting in `scripts/activity-watcher.py`, which keys off `active`; and replies to
things the athlete did themselves (logging a heat session, opening the heat chart). None
of those is *proactive surfacing*.

`silent` is deliberately **not** folded into `active`: `activity-watcher` credits
exposure off `active`, so silencing that way would starve the dose log and change the
model by data starvation.

Setting it is a runtime action (edit the athlete's `profile.json`), not a code change:

```bash
python3 -c "import json,pathlib; p=pathlib.Path('ClaudeCoach/athletes/kathryn/profile.json'); d=json.loads(p.read_text()); d['heat_silent']=True; p.write_text(json.dumps(d,indent=2))"

# then confirm - must print False:
python3 -c "import sys,json; sys.path.insert(0,'ClaudeCoach/lib'); import heat; print(heat.state('kathryn', json.load(open('ClaudeCoach/athletes/kathryn/profile.json')))['surface'])"
```

`heat.state()` prints a loud stderr warning if it finds a near-miss key
(`heat-silent`, `heat_silence`, `heatsilent`, `heat_quiet`), because a misspelled key
would otherwise be a silent no-op whose only symptom is the returning bug.

## Tests

`ironman-analysis/tests/test_illness.py` — schema and lifecycle, suppression content,
what is *not* suppressed, the conversational parse, the engine integration (including
that an unset flag leaves the prompt byte-identical and that the fingerprint rotates),
the heat surfacing gate, and a before/after comparison against `main`'s `lib/heat.py`
proving `base_dose`, `dose_multipliers`, every module constant and `acclimation_score`
are unchanged.
