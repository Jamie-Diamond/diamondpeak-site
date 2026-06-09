# Strength Programme — Content Proposal (for sign-off)

**Status: PROPOSAL — not implemented. 2026-06-09.**
**Decision needed from Jamie before any code changes.**

## Problem

The system caps strength at 2×/week (`strength_max`, validator-enforced) but
prescribes **no content** — no exercises, sets, loads, or phase periodisation.
The planner pushes empty "Strength 40 min" slots. Jamie's KPI says 2×/week;
actual compliance is ~0.3×/week. With a lateral-ankle-sprain history and a
masters age bracket, this is the largest unused PB lever in the methodology:
concurrent heavy strength work is one of the best-evidenced economy and
injury-resilience interventions in the endurance literature (standard
references: Rønnestad & Mujika 2014 review; multiple RCTs on running/cycling
economy — flag: third-party knowledge, confirm it fits how you want to train).

## Proposed programme

Two 35–45 min sessions/week (the existing cap), periodised by phase family.
All loads RPE-based (no 1RM testing — consistent with the no-field-tests policy).

### Session template (both weekly sessions, A/B alternation)

| Block | Content | Notes |
|---|---|---|
| Warm-up (5′) | bike/row easy + leg swings | |
| Main (20′) | A: squat pattern 3×5 @ RPE 8 (goblet/back squat or leg press) + RDL 3×6 | B: split squat / step-up 3×6/side + hip thrust 3×8 |
| Ankle (8′) | eccentric calf raises 3×12/side, single-leg balance with perturbation, banded eversion | permanent — ankle protocol, both sessions |
| Core (7′) | side plank, Pallof press, dead bug — 2 rounds | |

### Phase periodisation

| Phase | Frequency | Emphasis |
|---|---|---|
| Build (now) | 2×/wk | full template, progress load weekly |
| Specific | 2×/wk | maintain load, drop volume to 2 working sets |
| Peak | 1–2×/wk | maintenance singles-of-sets, nothing new |
| Taper | 1×/wk light, none in race week | neuromuscular touch only |

### Scheduling (fits existing day_rules)

- Slot 1: **Wed spare slot** (already designated "strength OR cross-training").
- Slot 2: **Tue or Thu after the swim** (low interference; never the day before
  the Friday long ride, and ≥8 h from any quality bike/run session).
- Travel weeks: hotel-gym/bodyweight variant of the same template (goblet →
  rear-foot-elevated split squat, RDL → single-leg banded RDL) so sessions
  never silently drop.

### Implementation (after sign-off, ~half a day)

1. `blueprints/strength.md` — the programme above as the single content source.
2. Sidecar: `strength` block (frequency by phase, template ref) emitted by
   generate-blueprint; planner Step 5 references it so pushed strength events
   carry real descriptions instead of empty slots.
3. Watchdog: compliance trigger — strength sessions completed < target for
   2 consecutive weeks → flag (same suppression rules as other triggers).
4. Tests: sidecar schema, planner prompt inclusion, validator unchanged
   (cap already enforced).

## Decision points

1. Approve/adjust the template and RPE-based loading?
2. Equipment reality: what do you actually have access to at home / when
   travelling? (Determines the default vs travel variant split.)
3. Slot 2 placement — Tue or Thu after swim, or keep Mon fully as rest?
4. Same programme for Kathryn/Calum with their own day_rules, or Jamie-only
   first?
