# Why conversational planning is failing (root cause) — 2026-06-15

Diagnosis from Jamie's Telegram conversation of **14 Jun 2026** (30 turns,
`athletes/jamie/telegram/history.json`). Two symptom-level bugs were already
logged to `athletes/jamie/feedback-log.json` on 14 Jun (both `resolved:false`).
This note records the **architectural root cause** behind both, which the log
does not.

## One sentence

There is no conversational planning brain at all: the tested engine is a
Sunday batch job that can't take incremental edits, so every "tweak my week"
request falls through to a freeform LLM doing TSS/CTL maths by hand — and a
slice of those even get downgraded to Haiku.

## The two brains

1. **The real engine — `scripts/generate-plan.py`** (runs Sunday 21:00 via VM
   cron). Builds from principles using the tested `ironman-analysis/primitives`:
   - `compute_required_tss`, `compute_projected_ctl`, `derive_phase_ctl_targets`,
     `compute_race_min_ctl` — load targets derived from the CTL goal + phase.
   - `primitives/planned_tss.py` — **deterministic** TSS, `duration × IF² × 100`
     with an IF-by-session-type table. Its docstring literally says
     *"Planned-session TSS — deterministic, never LLM arithmetic."* It was built
     on 11 Jun *specifically because* LLM TSS estimation was already a known bug.
   - `validate_week` for sanity.

2. **The freeform chat completion** (`telegram/bot.py`, fallthrough path). When
   a message isn't caught by a command router it returns `None` and the raw
   message goes to the `claude` CLI with the athlete system prompt. The model
   then does TSS estimation, CTL projection and weekly totals **by hand, in the
   LLM**, with no access to any of the primitives above.

## The trigger gap (the actual bug)

The chat→engine bridge only fires on near-exact phrases. From `bot.py`:

```
_PLAN_RE   = ^(?:generate plan | plan (next )?(2 )?weeks? | plan ahead)$
_REPLAN_RE = ^/?replan( week)?$
```

Both are anchored exact matches. **None** of Jamie's natural-language planning
requests on 14 Jun matched them, so every one fell through to freeform mode:

- "Make a plan which builds my fitness. That's your role here"
- "I'm planning a 60-90 min z2 ride"
- "What will my fitness be next Sunday based on that?"
- "extend the Wednesday run", "push this to ICU"

So all the remediation work (WS A–F, the tested primitives) is real and correct
— it is simply **bypassed the moment Jamie phrases a planning request in natural
language**, which is essentially always. And note `generate-plan.py` is a
*detached batch regenerator* (fills 2 weeks, launched with `timeout 1500`); it
cannot service an incremental edit like "extend the Wednesday run to 50 min."
The whole 14 Jun transcript is incremental refinement, so even a perfect intent
match couldn't have routed it to the engine. There is no incremental planning
path — only freeform.

## Second cause: planning-ish queries get downgraded to Haiku

`bot.py` `select_model()` (L117–120) sends any message matching
`_SIMPLE_QUERY_RE` to **Haiku**, else Sonnet. That regex classifies as "simple":
`this/next week`, `what's my tsb/ctl/atl/form/fitness`, and bare durations
(`^\d+\s*(km|min|hrs?…)`). Several of those **need computation** — "what's my
fitness" implies a CTL/TSB read or projection; "this week" implies a weekly TSS
roll-up. The picker keys on the *wording of the message*, not on whether the
answer requires arithmetic, so it routes exactly the compute-heavy lookups to
the weakest model. Haiku doing multi-row TSS tables is a plausible contributor
to "rows don't sum" and "took 3 goes to count 7 days." It also switches model
turn-by-turn within one planning conversation, ignoring context.

## Why each symptom follows directly

- **"All it can do is copy ICU or copy last week"** (turn 14): the freeform path
  has no generative load model. It can only read the ICU calendar and reflect it
  back, or mirror recent actuals. The build-from-target logic lives in
  `generate-plan.py` Step 4/6, which the chat never calls.
- **TSS estimates wrong** (turns 18–23): the chat reinvented a flat TSS/min rate
  from a single session. `planned_tss.py` already does it correctly
  (`IF² × hrs × 100`) — Jamie reverse-engineered the bot *toward the formula that
  already exists in the codebase but isn't wired into chat*.
- **Totals don't sum** (turns 12, 13, 23): LLM mental arithmetic on the table.
- **"Took 3 goes to count 7 days"** (turn 0): same pattern — the deterministic
  `/week` aggregator (`bot.py` ~L454–489) exists, but the freeform answer didn't
  use it.

## Secondary issues in the same transcript

- **Message duplication** (turns 3, 15): full reply text emitted twice — a
  send/render bug in the chat path, independent of planning.
- **Malformed plan-overview push** (turn 6): "BEHIND" status, CTL projection,
  "reply to upgrade" upsell, gym-gear question buried in a plan summary —
  violates the standing rule that plan messages show only
  day · session · duration · TSS.

## Fix direction (not yet implemented)

1. Give the **chat path** the primitives as callable tools — `planned_tss`,
   `compute_projected_ctl`, the `/week` aggregator — and forbid the model from
   computing TSS / CTL / weekly totals itself. Broadening the intent regex to
   route to `generate-plan.py` does **not** solve this: that engine is a 2-week
   batch regenerator and can't do incremental edits, which is what the
   conversation actually is. The fix is tools-in-chat, not re-routing.
2. Reconsider `select_model`: don't downgrade compute-bearing queries
   (`what's my fitness/ctl`, `this week`) to Haiku, or keep a conversation on
   one model once planning starts.
3. Investigate the duplicate-send bug (turns 3, 15) separately.
