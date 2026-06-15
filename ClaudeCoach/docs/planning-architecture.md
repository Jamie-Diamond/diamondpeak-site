# ClaudeCoach — Planning Architecture (design)

Status: **proposed** · Author: Claude (with Jamie) · 2026-06-15
Supersedes the ad-hoc LLM generator. Read alongside `docs/planning-chat-bypass-diagnosis.md`.

---

## 1. Why this exists

Two failures forced a rethink:

1. **Inconsistency.** When an LLM is left in charge of *mechanical* work — TSS, fuelling,
   durations, structured steps, weekly totals — it freelances. The chat path only became
   reliable where we made it deterministic; the Sunday generator stayed unreliable because it
   still relies on the LLM following instructions in a 23k-char prompt (the 15 Jun replan pushed
   prose with hardcoded "30 g/hr" and "4 hr", zero structured steps).
2. **It's a scaler, not a coach.** The deterministic tools we have handle *volume, load, and
   checking*. The actual intelligence — prescribing **varied, progressive, well-designed
   intensity** — was never built. "Can we write a *good* session?" Today: only as good as the
   LLM improvising the intervals, inconsistently.

The fix is architectural, not another feature: **methodology is encoded data, the plan is
assembled deterministically, the LLM only adapts and explains.**

## 2. Principles

- **Determinism owns correctness.** TSS, load, fuelling, structure, validation, the *shape* of
  a good session — all computed in tested code, never by LLM arithmetic or instruction-following.
- **The LLM owns judgment.** Adaptation to the individual (fatigue, life, preference), variety,
  and explanation — where intelligence genuinely helps and inconsistency is tolerable.
- **Grounded.** Every number the model states comes from a tool; it never invents one.
- **Athlete-in-control.** Propose options with trade-offs and explanation; don't silently
  auto-change. (Market leaders that auto-modify silently get criticised as "too passive" —
  Athletica; the conversational-with-options pattern is the sweet spot.)
- **Self-verifying.** An audit asserts invariants on every generation; nothing goes live until
  green. The *system* catches drift, not the athlete in a session.

## 3. Competitive positioning (why this direction is right)

Verified competitive scan (TrainerRoad, Athletica, Humango, Runna; see research note). The field
splits into two schools:

- **Domain-specific simulation engine** (TrainerRoad): non-LLM, re-simulates the whole plan in
  seconds, per-energy-system "Progression Levels", auto FTP detection/projection.
- **Grounded conversational AI coach** (Athletica multi-agent RAG; Humango "Hugo"/ChatGPT):
  chat coach grounded in the athlete's own data + a sports-science library.

**ClaudeCoach is the second school, and the frontier is converging there.** Our deterministic-
grounded chat is the right move. The architecture below keeps that strength and closes the gaps.

### What we're missing vs the field
| Dimension | Field | Us | Action |
|---|---|---|---|
| Auto threshold detection | Table-stakes (TR AI-FTP, Athletica CP/CS) | Use ICU eFTP/CSS/threshold | ~OK; make explicit in Layer 0b |
| Structured workouts → device | Table-stakes | **Built (15 Jun)** | ✓ |
| Grounded conversational coach | Frontier differentiator | **Our strength** | ✓ keep |
| Event-triggered adaptive re-planning | Table-stakes | Partial (watcher + `reoptimise`, no re-plan) | **Layer 5** |
| **Physiology beyond CTL/ATL/TSB** | Differentiator (CP/W′, durability, per-zone) | **PMC only** | **Layer 0b** |
| **Readiness → daily session adjust** | Differentiator (HRV traffic-light) | Data present, not wired to reshape | **Readiness signal → Layer 5** |
| Session-design *quality* | Implicit in their engines/libraries | **LLM-improvised, inconsistent** | **Layer 0 (core)** |
| Predictive injury/load-risk | Nobody ships it (open frontier) | Ramp caps only | **Future feature** |

### What we already have that much of the field lacks (don't lose)
Heat-acclimation protocol · menstrual-cycle-aware planning · race pacing + fuelling with course
splits · gut-training fuelling ramp. These are genuine differentiators already in the product.

## 4. The architecture

Two modes — **Generation** (build the week) and **Adaptation** (re-optimise in response to
anything) — on **one deterministic spine**.

```
        LAYER 0   Methodology (DATA, versioned, Jamie-owned)
                  ├─ blueprint: per-phase distribution, TSS ceiling, IF target, CTL targets   [exists]
                  ├─ quality-session library + progressions  ← THE "GOOD SESSION" ENGINE       [NEW]
                  └─ day_rules: which sports / which days                                       [exists]
        LAYER 0b  Physiology model (CODE)                                                       [NEW]
                  └─ per-sport CP/CS + W′ from maximal-mean curve; durability/fatigue-resistance;
                     drives zones, readiness, and prescription. (ICU power curves already pulled.)
        LAYER 1   Weekly skeleton (CODE, deterministic)                                         [NEW]
                  └─ phase + day_rules + required weekly TSS → which day = sport = ROLE
                     (endurance/quality/long/brick), each with a target TSS that SUMS to the
                     weekly target AND satisfies the phase distribution. Quality slots reserved —
                     a Build week cannot collapse to all-Z2.
        LAYER 2   Session instantiation (CODE + narrow LLM)
                  ├─ quality slots → pull phase template (Layer 0) → parameterise to thresholds
                  │                  (Layer 0b) → segments. Deterministic, energy-system-correct.
                  ├─ endurance/long → Z2 to target. Deterministic.
                  └─ LLM (narrow): adapt to the individual (fatigue/HRV/soreness/travel), pick
                     among equivalent options, vary warm-ups/drills, write coaching notes.
                     NEVER sets load, intensity, or core structure.
        LAYER 3   Render + validate + push (CODE — plan_builder.py)                             [built]
                  └─ segments → structured Garmin steps; compute load; inject fuel-target;
                     validate_week (day_rules, ramp, distribution, load-on-target); push.
        LAYER 4   Audit (CODE, self-checking — the trust mechanism)                             [NEW]
                  └─ on every generation + standalone: assert every session structured;
                     fuelling = target; long ride ≤ event anchor; weekly load on target;
                     distribution matches phase (quality actually present in Build/Peak);
                     no un-flagged forbidden-day sessions; CTL ramp within cap. Fail → alert + block.
        LAYER 5   Adaptation (conversational — the intelligent coach)                           [NEW]
                  └─ fires on preference / missed session / readiness / life change:
                     1. Impact (tools): TSS delta, week vs target, distribution effect, CTL/form,
                        purpose lost.
                     2. Option space (reoptimise envelope + option-evaluator): enumerate concrete
                        rebalances, score each for validity (target/distribution/ramp/spacing/
                        long-ride placement/recovery).
                     3. Reason + propose (LLM): recommend proactively with trade-offs
                        ("shorten tomorrow → move the long ride to Friday, keep Saturday's
                        threshold — week stays on target"). Athlete chooses.
                     4. Commit (deterministic): re-instantiate affected sessions → validate →
                        push structured → audit.

        READINESS SIGNAL (CODE) — HRV/RHR/sleep → traffic-light → feeds Layer 5's daily mode     [NEW]
```

## 5. The "good session" problem (Layer 0 in detail)

A session is *good* when its structure trains the intended system: work duration × intensity ×
**rest ratio** matched to the energy system, phase-appropriate, progressive, sport-correct.
`render_workout` can express any of these — but only if the **segments are well-designed**. Today
the LLM designs them, so quality is a coin-flip (e.g. a CSS swim with 1:2 work:rest instead of
short rest).

**Layer 0 fixes this with an encoded quality-session library**: per phase × sport, a small set of
vetted session templates (energy-system-correct work/rest/structure) with a week-to-week
progression, parameterised to the athlete's thresholds (Layer 0b). The LLM selects/varies within
the library; it does not invent intervals from scratch. This is **the coaching IP, as data** —
inspectable, testable, versioned — and it is the input only Jamie can author (or red-line a
drafted default).

**Known sub-gap — swim:** ICU parses `400m` as 400 *minutes*, so we currently render swims in
time. Real swim sets are distance/reps. Layer 0b must convert prescribed distance × CSS pace →
time (or resolve ICU's swim-distance syntax) so swim sessions read naturally.

## 6. Where the LLM lives (and doesn't)
- **Does:** adaptation to the individual; selecting/varying quality sessions within the library;
  conversational planning and explanation; coaching notes.
- **Does NOT:** TSS, load, fuelling numbers, the core quality progression, structure rendering,
  validation, or arithmetic of any kind.

This shrinks the LLM surface from a 23k-char "do everything" prompt to narrow, judgment-only
tasks — where it's reliable.

## 7. Reused vs new
- **Reused:** blueprint + `generate-blueprint`; tested primitives (`load`, `planned_tss`,
  `nutrition`, `validate_plan`, `blueprint`, `modulation`, `reoptimise`); `plan_builder` (Layer 3);
  ICU integration; activity-watcher (Layer 5 trigger); heat/menstrual/race-plan modules.
- **New:** Layer 0 quality-session library + progressions; Layer 0b physiology model; Layer 1
  skeleton; Layer 4 audit; Layer 5 option-evaluator + adaptation flow; readiness signal.

## 8. Roadmap (verification-gated; no big-bang)
1. **Audit first (Layer 4).** Deterministic; immediately shows where current plans fail invariants.
   Reusable, zero risk. Instant visibility.
2. **Layer 0 (with Jamie) + Layer 1.** Encode the session library/progressions + deterministic
   skeleton. Dry-run prints the prescribed week; verify against invariants before any push.
3. **Layer 0b physiology.** CP/CS + W′ + durability from ICU curves → feeds zones/prescription.
4. **Layer 2 + 3 live** behind the audit gate. Replace the old generator only once a dry-run for
   all athletes is green.
5. **Layer 5 adaptation + readiness signal.** Conversational re-optimisation, then proactive
   (watcher-triggered) once trusted.

Each phase ends with a dry-run artifact Jamie approves. Nothing live until the audit is green.

## 9. Future features (not now)
- **Predictive injury/load-risk model** (ACWR / ramp-risk alerting). Nobody in the field ships
  this as a headline feature — a differentiation opportunity for later. We already have CTL-ramp
  caps + rehab logic as a foundation.
- **Continuous background auto-adaptation** (re-optimise without being asked, beyond
  watcher-triggered proposals).
- **Deeper physiology** (VLaMax-style glycolytic/aerobic trade-off prescription; lab-grade
  metabolic profiling).

## 10. Open decisions / what Jamie provides
- **Layer 0 methodology** — the quality-session library + progressions per phase/sport. Author
  with Claude, or red-line a drafted evidence-based default. *This is the input that determines
  session quality; the system cannot reliably invent it.*
- Phase ordering of Layer 0b (physiology) — first build or after the core generator is solid.
