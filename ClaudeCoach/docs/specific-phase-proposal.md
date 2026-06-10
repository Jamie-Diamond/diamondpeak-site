# Specific Phase — Content Proposal (for sign-off)

**Status: APPROVED + IMPLEMENTED 2026-06-10.** Jamie's decisions: distributions
as proposed; race sims SPLIT (one late Specific, one Peak — amended from the
original both-in-Specific suggestion); IF 0.70. Content rows live in
generate-blueprint.py; sims split reflected in blueprints/blueprint.md.

## Problem

The Specific phase (Jamie: 6 Jul – 2 Aug 2026) currently reuses Build's content
tables wholesale — distribution, IF target, fuelling, bricks (`content_family()`
maps specific → build). Only the CTL *target* differs (105 vs 95). So the phase
raises load but does not shift the *character* of training toward the race,
which is the entire point of a specificity phase.

## Proposal

Give "specific" its own content row in each table in `generate-blueprint.py`.
The theme: **convert fitness into race-day shape at race intensity** — more
race-IF (Z3-boundary) work than Build, less top-end than Peak.

Numbers below are my suggestions from standard long-course periodisation
practice — they are coaching calls for you to confirm or adjust:

### Distribution (Full Ironman, weekly average per sport)

| Sport | Build (current) | **Specific (proposed)** | Peak (current) |
|---|---|---|---|
| Bike | 75% Z1–2 / 15% Z3 / 10% Z4–5 | **72% Z1–2 / 20% Z3 / 8% Z4–5** | 70% Z1–2 / 15% Z3 / 15% Z4–5 |
| Run | 80% Z1–2 / 12% Z3 / 8% Z4–5 | **78% Z1–2 / 15% Z3 / 7% Z4–5** | 75% Z1–2 / 12% Z3 / 13% Z4–5 |
| Swim | 65% Z1–2 / 25% Z3–4 / 10% Z5 | **62% Z1–2 / 30% Z3–4 / 8% Z5** | 60% Z1–2 / 25% Z3–4 / 15% Z5 |

Rationale: the Z3 share grows (race-IF work — Jamie's bike target IF 0.71 sits
at the Z2/Z3 boundary) while Z4–5 *drops* slightly relative to Build — VO2-type
top-end is a Build/Peak stimulus, not a specificity one.

### Other content rows

- **IF target (TSS ceiling)**: 0.70 (between Build 0.68 and Peak 0.72) → Jamie
  15 h ceiling ≈ 735 TSS.
- **Fuelling**: full race rate (75–90 g/hr) on ALL key sessions, not just race
  sims — the gut-training window is exactly this phase.
- **Bricks**: 2–3/week, biased to quality/long bricks at race effort.
- **Key sessions**: both race simulations anchor in Specific (currently "1× late
  build, 1× peak"); long-ride race-IF finishing blocks (already live via the
  durability rule) progress to 60+ min here; at least one open-water swim/week
  at race effort where access allows.

### Implementation (after sign-off, ~2 h)

1. Add `specific` rows to `DISTRIBUTION`, `FUELLING`, `IF_TARGETS`, `BRICK_MIN`,
   `BRICK_TYPE` in `generate-blueprint.py`.
2. Narrow `content_family()` so specific only falls back to build for events
   with no specific row (Kathryn's 70.3 config has no Specific phase — unaffected).
3. Add matching `blueprints/blueprint.md` section; regenerate Jamie's sidecar;
   tests for the new mapping.

## Decision points

1. Approve/adjust the distribution numbers above?
2. Both race sims in Specific, or keep one in Peak?
3. IF target 0.70 OK?
