# ClaudeCoach — Planning Engine Remediation Plan

_Version 1.0 — 7 June 2026. Updated 8 June 2026._

This plan fixes six issues found in an appraisal of the training-planning engine.
It records the design decisions taken, sequences the work by dependency, and
defines done for each workstream.

## Status — 8 June 2026

| WS | Scope | Status |
|----|-------|--------|
| **A** | Consolidate load maths into `primitives/load.py` | ✅ Done + deployed |
| **B** | Structured `training-blueprint.json` sidecar + validator | ✅ Done + deployed |
| **C** | Wire blueprint guidance into the planner prompt | ✅ Done + deployed |
| **D** | One methodology for all athletes/events (retire `is_triathlete` branch) | ✅ Done + deployed + verified live |
| **E** | Backstop validation | ✅ **Done + deployed.** `validate_week` primitive (wrong-day / TSS-cap / ramp). Planner re-fetches & validates the pushed plan behind `ENFORCE_VALIDATION`. **Day-rule checks active** via `day_rules` in athletes.json — the single source that also renders the prompt's HARD rule lines. **Block mode** (remediation re-prompt + coach-alert + withhold) implemented, default-off (`=block` to enable after warn observation). **Prescription backstop**: daily-prescription computes the modulation engine's result from deterministic inputs (`classify_session_type` + readiness assembly), shadow-logged (`PRESCRIPTION_BACKSTOP=shadow`), authoritative-flip pending observation. Live-verified on the VM. |
| **F** | README refresh + drift guards | ✅ Done (README rewrite, no-duplicate-maths guard, path-existence guard) |

Out-of-plan but completed alongside: fixed a baseline-test scheduling bug that
re-dated FTP/CSS/LTHR baselines to "today" on every blueprint regen and spammed
two live athletes (anchored to plan start instead).

---

## 1. Context

The planning engine is sound in design — deterministic Python computes the hard
numbers (CTL/TSS maths), and an LLM (Sonnet 4.6, via `claude -p`) does session
construction and athlete-facing writing. The appraisal found that the *rigour is
front-loaded into the Python core, then handed to the LLM for execution*, and
several of the most rigorous-looking parts are guarded by prompt-compliance
rather than code. The fixes below close that gap without throwing away the
LLM-for-judgement design, which is correct.

### Issues being fixed

| # | Issue | Severity |
|---|---|---|
| 1 | **Duplicated load maths.** `generate-plan.py` reimplements CTL/TSS maths inline; the tested `ironman-analysis` primitives are a separate copy. Plus a **third** CTL-target source: a hardcoded, Jamie-specific `BUILD_TABLE` in `primitives/load.py` that disagrees with `athletes.json`. | High |
| 2 | **LLM-guarded safety layers.** The R1–R7 modulation engine and the pre-push validation gate are invoked by the LLM via prompt, not by deterministic code. If the model skips them there is no fallback. | High |
| 3 | **Blueprint half-wired.** `training-blueprint.md` is written by `generate-blueprint.py` and read by `weekly-summary.py` (retrospective), but **not** by `generate-plan.py` (prospective). The forward planner uses a hardcoded weekly template. | Medium |
| 4 | **README drift.** Root `README.md` documents a single-athlete layout that no longer exists; sessions are told to read it first. | Medium |
| 5 | **Blueprint roster gap.** Full IM (Jamie) and 70.3 (Kathryn) are fully specified; Calum's event (Sportive) is a stub, handled by a separate hardcoded cycling-only branch. | Medium |
| 6 | **No guard against recurrence** of doc/path/implementation drift. | Low |

### Decisions taken (forks resolved with the owner)

- **Blueprint (#3):** **Wire it into the planner.** The blueprint's phase
  distribution, brick frequency and test schedule will drive the pushed plan.
- **Determinism (#2):** **Backstop pattern.** Keep the LLM building and calling;
  add a deterministic post-step that *blocks any push violating a hard
  constraint* and re-prompts. Not a full orchestrator rewrite.
- **This turn (superseded):** plan document only; no code changes.

### Decision log

**2026-06-07 — Phase-model reconciliation (WS B→C boundary).** Discovered three
distinct periodisation shapes in play: the blueprint generator invents its own
(`Base/Build1/Build2/Peak`, anchored to `date.today()`), while `athletes.json`
carries per-athlete phase config that itself differs by athlete — jamie:
`base/build/specific/peak` (tuned `phase_ctl` incl. specific=105); kathryn:
`base/build/peak` (no specific, no `phase_ctl`); calum: none. All three disagree
on anchor and structure.

Decision: **`athletes.json` per-athlete phase config is canonical** (anchored to
`plan_start`); `generate-blueprint.py` is changed to *adopt* those boundaries
rather than invent its own, so the sidecar windows agree with what the planner
already prescribes. Rationale: this unifies anchor + windows + source onto one
place **without rewriting any athlete's live, tuned plan** — the non-destructive
correct option, chosen over making the blueprint canonical (which would drop
jamie's specific phase and shift his boundaries mid-build). Athletes with no
config (calum) fall back to the existing `phase_structure()` auto-derivation.

Baked-in assumption (flagged): the blueprint's content tables (distribution,
fuelling, IF) have no `specific` column, so a **specific phase reuses build-family
content** for those, while keeping its own `phase_ctl` target. Reasonable
(specific = late-build race-specific work) but a coaching call — revisit if a
distinct specific-phase distribution is wanted.

---

## 2. Guiding principles

1. **One source of truth per fact.** CTL targets, phase windows and the weekly
   template each come from exactly one place after this work.
2. **Python owns hard constraints; the LLM owns judgement and prose.** Anything
   that can break the athlete (constraint breach, over-ramp, ankle load) becomes
   a Python assertion. Session *selection* and *wording* stay with the LLM.
3. **Structured data over prose for machine consumers.** The blueprint stays
   human-readable, but a structured sidecar feeds the planner and validator.
4. **Every new primitive ships with tests** (the package's existing ~1:1
   test:source ratio is the bar).
5. **No silent failure.** If a deterministic step can't run, it logs loudly and
   degrades to a flagged-but-safe state, never to "looked fine".

---

## 3. The linchpin — structured blueprint sidecar

`generate-blueprint.py` already computes every methodology value in Python
(phase windows, per-phase TSS ceiling, intensity distribution, brick frequency,
key sessions, test dates, env protocols) and renders them to **prose markdown**.
It also already emits a structured `test-schedule.json`. The single highest-leverage
change is to make it **also emit `athletes/{slug}/reference/training-blueprint.json`**
— the same values it already has in memory, serialised. This one artefact unblocks
both #3 (planner reads it) and #2 (validator checks against it), and lets
`weekly-summary.py` stop LLM-parsing prose for the phase-transition flag.

This is built first (Workstream B) because two other workstreams depend on it.

**Proposed schema** (`training-blueprint.json`):

```json
{
  "slug": "jamie",
  "generated": "2026-05-12",
  "event_type": "Full Ironman",
  "phases": [
    {"name": "Base", "start": "2026-05-12", "end": "2026-06-22",
     "tss_ceiling": 634, "if_target": 0.65,
     "distribution": {"swim": {"z1_2": 70, "z3_4": 20, "z5": 10},
                      "bike": {"z1_2": 80, "z3": 12, "z4_5": 8},
                      "run":  {"z1_2": 85, "z3": 10, "z4_5": 5}},
     "brick_min_per_phase": 1,
     "key_sessions": ["long_ride", "long_run", "swim_css"]}
  ],
  "tests": [{"type": "ftp", "date": "2026-05-12", "protocol": "20-min"}],
  "env_protocols": {"heat": {"active": true, "starts": "2026-08-25"}},
  "week_template_rules": {
    "swim_days": ["Tue", "Thu"], "bike_days": ["Fri", "Sat", "Sun"],
    "no_cycle_days": ["Mon", "Tue", "Wed", "Thu"], "strength_min_per_week": 1
  }
}
```

The `week_template_rules` block replaces the hardcoded weekly template currently
baked into `generate-plan.py`'s prompt string, and becomes the validator's
rule source.

---

## 4. Workstreams

Ordered by dependency, not by issue number.

### Workstream A — Consolidate the load maths (Issue #1)

**Problem.** `generate-plan.py` defines `compute_required_tss`,
`compute_projected_ctl`, `derive_phase_ctl_targets` and `compute_race_min_ctl`
inline. The tested library lacks them. A third CTL-target source
(`BUILD_TABLE` in `load.py`) is hardcoded to Jamie's dates and disagrees with
`athletes.json` `phase_ctl`. Three implementations, no shared tests, guaranteed
to drift.

**Design.**
1. Lift the four functions into `primitives/load.py` (they are pure maths —
   they belong there). Keep signatures identical so the move is mechanical.
2. Add unit tests in `tests/test_load.py` covering: EMA round-trip
   (`required → projected` is self-consistent), the documented calibration
   anchors (IM 9:30 → ~96 CTL; 70.3 5:30 → ~63 CTL), ramp-cap clamping, and the
   zero/edge cases.
3. **Parameterise `BUILD_TABLE`.** `trajectory_check` currently takes the
   Jamie-hardcoded default. Change the default to *derive* from `athletes.json`
   `phase_ctl` + `phase_tss` end-weeks + `plan_start`, so every athlete gets a
   correct trajectory and there is one CTL-target source. The hardcoded table
   becomes a test fixture only.
4. `generate-plan.py` imports from `primitives.load` and deletes its inline copies.

**Files.** `primitives/load.py`, `tests/test_load.py`, `scripts/generate-plan.py`,
`primitives/__init__.py` (export new names).

**Tests / validation.** `pytest ironman-analysis/tests/test_load.py`; then a
golden-output check — run `generate-plan.py --athlete jamie` against a frozen
fixture and confirm the TSS target and trajectory are byte-identical to the
pre-change output (this is a pure refactor; output must not move).

**Effort.** ~3–4 h. **Risk.** Low (pure refactor + tests). **Rollback.** Revert
the commit; inline copies return.

---

### Workstream B — Structured blueprint sidecar (enabler for #3, #2)

**Problem.** The blueprint exists only as prose; no machine consumer can read it.

**Design.**
1. In `generate-blueprint.py`, build the structured dict alongside the markdown
   (the values already exist in locals) and write
   `athletes/{slug}/reference/training-blueprint.json`.
2. Add a JSON-schema doc to `ironman-analysis/schemas/` and a tiny validator so a
   malformed sidecar fails loudly at generation time, not at planning time.
3. Regenerate sidecars for all three athletes and commit.

**Files.** `scripts/generate-blueprint.py`, `ironman-analysis/schemas/`,
`athletes/*/reference/training-blueprint.json` (generated).

**Tests / validation.** Schema-validate each emitted sidecar; assert the
markdown and JSON agree on phase windows (cross-check test in the suite).

**Effort.** ~2–3 h. **Risk.** Low. **Rollback.** Stop writing the sidecar;
nothing else has consumed it yet if A/C not yet merged.

---

### Workstream C — Wire the blueprint into the planner (Issue #3)

**Depends on B.**

**Problem.** `generate-plan.py` ignores the blueprint and uses a hardcoded,
Jamie-specific weekly template and phase context.

**Design.** (revised per the 2026-06-07 phase-model decision — `athletes.json`
is canonical; the sidecar's windows are *generated from* it, so reading the
sidecar and reading `athletes.json` now agree by construction.)
1. Precursor (lands in this workstream's first step): `generate-blueprint.py`
   builds phases from `athletes.json` (`plan_start` + `phase_tss` end-weeks) via a
   new pure `primitives/blueprint.canonical_phases()`, falling back to
   `phase_structure()` only when an athlete has no config. This makes the sidecar
   windows match the planner before anything consumes them. Handle the `specific`
   family (content reuses `build`; CTL target from `athletes.json` `phase_ctl`).
2. `generate-plan.py` then reads `training-blueprint.json` for per-phase
   **content** — intensity distribution, brick frequency, fuelling, tests due in
   the window — keyed by the phase it already resolves from `plan_start`. It does
   NOT take phase boundaries from a second source; `athletes.json` stays the
   boundary authority.
3. `week_template` sport-day rules (swim/bike days) have no blueprint source and
   stay in the planner for now (or move to a profile field) — out of WS C scope.
3. Inject the phase distribution and brick target into the prompt as **explicit
   build targets** the LLM must hit, not just context.
4. If the sidecar is missing (athlete never had a blueprint generated), fall back
   to the current hardcoded template **and log a warning** — no silent change of
   behaviour.

**Files.** `scripts/generate-plan.py`; optionally a small
`primitives/blueprint.py` helper for phase resolution (with tests).

**Tests / validation.** Phase-resolution unit tests (date in each window → right
phase; boundary days). Manual: run `--athlete jamie` and confirm the prompt now
carries blueprint-derived distribution/brick/test lines; confirm the pushed week
still respects the same hard day-rules.

**Effort.** ~5–7 h. **Risk.** Medium — this changes planner *output*. Mitigate
with a side-by-side diff of a generated week before/after, reviewed before the
first live Sunday run. **Rollback.** Feature-flag the sidecar read
(`USE_BLUEPRINT=0` env) so a bad week reverts to the hardcoded template without a
redeploy.

---

### Workstream D — Flesh out Sportive/Gravel profile (Issue #5)

**Depends on B/C** (so Calum is covered by the same wired path).

**Problem.** Calum's event (Sportive) is a blueprint stub; he's served by a
separate hardcoded cycling-only branch in `generate-plan.py`. Wiring the
blueprint into the planner leaves 1/3 of the roster on the old path unless the
Sportive profile is specified.

**Design.**
1. Promote the Sportive (and Gravel — same family) rows in `blueprints/blueprint.md`
   from stub to full event profile: phase distribution, key sessions
   (long ride, climbing repeats if `course_type` hilly), taper ratios, no
   swim/run blocks after base.
2. Teach `generate-blueprint.py` to emit a sidecar for cycling-only events.
3. Retire the hardcoded cycling-only branch in `generate-plan.py` once the wired
   path covers Calum (keep it behind the same `USE_BLUEPRINT` flag as a fallback).

**Files.** `blueprints/blueprint.md`, `scripts/generate-blueprint.py`,
`scripts/generate-plan.py`.

**Tests / validation.** Generate Calum's sidecar; dry-run his plan; confirm it
matches the intent of the old cycling-only template (no swim/run injected, bike
days respected).

**Effort.** ~3–4 h. **Risk.** Low–medium. **Rollback.** `USE_BLUEPRINT=0` keeps
Calum on the cycling-only branch.

---

### Workstream E — Backstop validation (Issue #2)

**Depends on B** (uses `week_template_rules` from the sidecar as its rule source).

**Problem.** The pre-push validation gate and the R1–R7 modulation call are
prompt instructions the LLM grades itself on. No deterministic guarantee a
pushed session respects hard constraints, and no fallback if `modulate.py` isn't
called.

**Design — two deterministic backstops, no orchestrator rewrite.**

*Planner backstop (`generate-plan.py`):*
1. After the LLM run, the Python wrapper **re-fetches** the events it created for
   the window via `icu_fetch.py` and runs a new
   `primitives/validate_plan.py:validate_week(events, rules, ctl, ankle_state)`.
2. `validate_week` asserts hard constraints from the sidecar
   `week_template_rules` + `rules.md`: swim only on permitted days, no cycling on
   forbidden days, no quality run while ankle uncleared, weekly duration/TSS cap,
   ramp ≤ cap.
3. On violation: **block the Telegram send**, log the breach, and re-invoke the
   LLM once with a correction prompt naming the specific breach. If it still
   breaches, send a coach-facing alert (not the athlete) and leave the week
   un-pushed/flagged. A breach that reaches the athlete is a failed run.

*Prescription backstop (`daily-prescription.py`):*
4. The wrapper itself calls `modulate.py` with the constructed `planned`/`readiness`
   JSON and injects the `SessionPrescription` into the prompt as authoritative,
   rather than asking the LLM to run it. The LLM narrates the result; it no longer
   owns whether the engine runs. (This is the one place we move a call from
   prompt to wrapper — cheap, and it's the highest-value safety layer.)
5. If `modulate.py` errors, log loudly and fall back to "execute as planned" only
   when no rule *could* have fired (e.g. no readiness signals available);
   otherwise hold and flag.

**Files.** `primitives/validate_plan.py` (new, + tests),
`scripts/generate-plan.py`, `scripts/daily-prescription.py`, `primitives/__init__.py`.

**Tests / validation.** Unit tests for `validate_week` (each hard constraint, a
clean week passes, each breach type caught). Integration: feed a deliberately
bad LLM output fixture (swim on Monday, quality run with ankle uncleared) and
confirm the send is blocked and the correction loop fires.

**Effort.** ~6–8 h. **Risk.** Medium — the re-prompt loop adds latency and a
failure mode (loops, partial pushes). Mitigate: single re-prompt only, then
coach-alert; idempotent push (check date+sport before pushing, already a rule).
**Rollback.** Env flag `ENFORCE_VALIDATION=0` downgrades block→warn.

---

### Workstream F — README refresh + drift guards (Issues #4, #6)

**Problem.** Root `README.md` points new sessions at a defunct single-athlete
layout. Nothing prevents this class of drift recurring.

**Design.**
1. Rewrite `README.md` to the multi-athlete reality: per-athlete
   `athletes/{slug}/` holding `reference/`, `current-state.*`, logs; corrected
   read-order and file map; note the structured sidecar.
2. Add a cheap **path-existence test** (`tests/test_docs_paths.py`) that parses
   the file-map tables in `README.md` and asserts every referenced path resolves
   (per athlete where templated). This catches doc drift in CI.
3. Add a **no-inline-maths guard** (`tests/test_no_duplicate_maths.py`) that
   asserts `generate-plan.py` imports the load functions from `primitives` and
   does not redefine them — prevents Workstream A regressing.

**Files.** `README.md`, `ironman-analysis/tests/test_docs_paths.py`,
`tests/test_no_duplicate_maths.py`.

**Effort.** ~2–3 h. **Risk.** Very low. **Rollback.** Trivial.

---

## 5. Sequencing

```
A (load consolidation)  ─┐
B (blueprint sidecar)   ─┼─► C (wire planner) ─► D (Sportive) ─► E (backstop)
                         │                                        ▲
F (README + guards)  ────┴────────────────────────────────────────┘ (F any time)
```

- **A and B are independent** and can be done first, in parallel.
- **C depends on B.** **D depends on C.** **E depends on B** (rules source) and is
  best done after C so it validates the new output.
- **F** can land any time; the no-inline-maths guard should land *with or right
  after* A.

Suggested order: **A → B → C → D → E → F**, with F's guards merged alongside their
target workstream. Total estimated effort **~21–29 h**.

If time-boxed, the **must-do core** is A + the no-inline-maths guard (kills the
silent drift landmine) and E's prescription backstop (restores the tested safety
engine). C/D/E-planner are the larger value but lower acute risk.

---

## 6. Deployment & verification

Per the workspace's deployment rules:

- **Scripts run via VM crontab** (`generate-plan.py` Sunday 21:00,
  `daily-prescription.py` daily). They are **not** the bot service. Deploy by
  pushing to `main` and pulling on the VM (`cc-gitpull.sh`) — verify the VM is on
  the new commit before the next cron fire. **Do not use `CronCreate`.**
- **Restart `claudecoach-bot`** only if the running bot imports changed
  `ironman-analysis` primitives (Workstreams A/B/E touch them). Check whether
  `bot.py` imports the changed modules; if so,
  `systemctl restart claudecoach-bot` on the VM after pull, and verify it came
  back up.
- **Run a one-line diagnostic before issuing command sequences on the VM**
  (confirm commit, confirm cron entries unchanged).
- **Tests:** `cd ironman-analysis && pytest` must be green before any push. New
  tests live under `ironman-analysis/tests/` (`testpaths = ["tests"]`).
- **First live planner run after C/D/E** should be watched: trigger
  `generate-plan.py --athlete jamie` manually, inspect the `<telegram>` output and
  the pushed events, before trusting the Sunday cron.

---

## 7. Definition of done

- [ ] `generate-plan.py` imports all load maths from `primitives.load`; no inline
      EMA functions remain; guard test enforces this.
- [ ] Exactly one CTL-target source (`athletes.json` `phase_ctl`, consumed by
      `trajectory_check`); `BUILD_TABLE` is fixture-only.
- [ ] `training-blueprint.json` emitted for all active athletes and
      schema-validated.
- [ ] `generate-plan.py` builds the week from the sidecar (distribution, bricks,
      tests, permitted days); falls back with a logged warning if absent.
- [ ] Sportive/Gravel event profile specified; Calum served by the wired path.
- [ ] Deterministic `validate_week` blocks any hard-constraint breach before the
      athlete is messaged; `daily-prescription.py` wrapper invokes `modulate.py`
      directly.
- [ ] `README.md` matches the multi-athlete layout; path-existence test green.
- [ ] Full `pytest` suite green; golden-output diff confirms Workstream A is a
      no-op on numbers.
- [ ] VM on the new commit; bot restarted if it imports changed primitives;
      first live runs watched.

---

## 8. Open questions / risks to watch

- **Re-prompt loop latency (E).** The correction loop adds an extra `claude -p`
  call. Acceptable for a Sunday batch; confirm it stays within the cron window.
- **Blueprint staleness.** The sidecar is generated once (e.g. 12 May). If phase
  windows shift mid-build, the planner reads stale phases. Consider a freshness
  check (sidecar `generated` date vs `plan_start`) and a regen trigger — flag for
  a follow-up, out of scope here.
- **`rules.md` vs `week_template_rules` overlap.** Two rule sources for the
  validator (sidecar + `rules.md`). Define precedence explicitly in
  `validate_week` (sidecar = structural day-rules; `rules.md` = athlete hard
  overrides; `rules.md` wins on conflict).
