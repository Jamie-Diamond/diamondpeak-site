# Methodology Audit - Action Plan

Companion to `docs/methodology-audit-2026-07-02.md`. Drafted 2026-07-03.

**Verification status.** All four P0 findings were independently re-verified against source before this plan was written: `PRESCRIPTION_BACKSTOP` defaults to `shadow` (`scripts/daily-prescription.py:367`); `plan_builder.py` calls `validate_week` without `ctl_today` or `weekly_tss_cap`, and both hard checks in `validate_plan.py:251,263` no-op on `None`; `plan_tools.required_tss` has no deload branch and returns no target in taper; the taper TID falls back to `base` (`session_library.py:238`); `_ankle_state` fails open to `(0, True)` and Kathryn's `current-state.json` has no `ankle` block. Sampled P1/P2 checks (raise-only FTP sync, run-cap `None` fallback on ICU error, `_parse_wellness` 4-vs-5 unpack crash, `race_predictor` absent from repo configs) also all confirmed. The audit is trustworthy; treat its findings as accepted.

**Race calendar drives the deadlines.**
- Athlete A (Jamie): race 2026-09-19. Taper begins ~22 Aug; heat block 22 Aug.
- Athlete B (Kathryn): race 2026-09-20.
- Athlete C (Calum): Marmotte 2026-08-29. Any heat protocol would start ~1 Aug, so that decision is needed by mid-July.

---

## Phase 0 - Preserve and reconcile (same day, ~1h)

0.1 **Fix the session-log ingestion freeze.** `athletes/*/session-log.json` frozen at 17 Jun (A, B) and 21 May (C) while the rest of the pipeline is current. Find why the writer stopped, backfill, and add session-log freshness to the ops_log heartbeat/alerts so a frozen feed can never again silently starve the validators. This is a real bug the audit only listed as a data caveat.

0.2 **Reconcile repo vs VM.** The served `racePredictor` config exists on the VM but not in the repo (`profile.json` / `config/athletes.json` have no `race_predictor` block), and public/private `training-data.json` copies have diverged. Run the standard VM sync process, pull VM-side config into git, and confirm both sides are on one commit before any code changes ship. [P1-4 first half]

## Phase 1 - Arm the existing guardrails (~half a day, this week) [P0-3, P0-4]

This completes remediation WS E + F-guard, which were already pending.

1.1 Review the shadow logs accumulated since ~9 Jun: diff the engine prescription against the LLM's for each day. If divergence is low/benign, flip `PRESCRIPTION_BACKSTOP` to authoritative (or block-on-divergence). The code comment explicitly anticipates this flip.
1.2 In `plan_builder.py`, pass `ctl_today` (fetch as `stage1-plan.py` already does) and `weekly_tss_cap` (the blueprint's hours x 100 x IF^2 ceiling) into `validate_week` so the ramp and TSS hard checks actually fire at push time.
1.3 **Injury state: athlete-scoped and structured.** CORRECTION (2026-07-05, from Jamie): Kathryn has NO ankle injury - the ankle concern is Jamie's and was contaminated into her context. Her `current-state.md` narrates an active R1 ankle block and her 2 Jul tempo was genuinely BLOCKED on it (event 120035084): a false positive that cost her a quality session. Actions: (a) purge all ankle references from Kathryn's files and correct the 2 Jul record; (b) trace how the LLM applied R1 to her (shared prompt text / cross-athlete leakage - cf. the 12 Jun per-athlete log fix, 2cbeb1f) and close the leak; (c) structured injury state per athlete remains the source of truth, and the LLM must never assert an injury the structured state doesn't contain - which the authoritative backstop (1.1) enforces mechanically, since the engine reads only structured state. Note the deterministic engine would have said GO here; this incident is direct evidence for the fully-authoritative flip.
1.4 Fail-noisy design: `validate_week` logs a loud "check SKIPPED: missing <input>" whenever a hard check no-ops. Silent disarming is the root cause of P0-4; make it impossible to repeat quietly.
1.5 Regression tests asserting each hard gate fires given a breaching week (ramp, TSS cap, forbidden day, ankle). Cheap insurance against future re-disarming.
1.6 Quick win while in the area: fix the `_parse_wellness` 4-vs-5 unpack crash (`recovery_score.py:216` vs `morning-checkin.py:428`) that silently disables recovery scoring on empty wellness. [P2-4]

Deploy: rsync to VM, `systemctl restart claudecoach-bot`, verify with a dry-run plan build and next morning's prescription log.

## Phase 2 - Build the missing training structures (1-2 days) [P0-1, P0-2, P1-5]

2.1 **Deload branch in `required_tss`**: every Nth week (config, default 4th) at 60-65% of the preceding week, plus the documented "missed >30% -> next week recovery" trigger. Needed soonest: A is mid-build now with a +3.6 CTL/wk monotonic climb on a partially torn ankle. Target: live before the week of 13 Jul so at least two deloads land before taper.
2.2 **Shaped taper**: taper branch in `required_tss` returning stepped weekly targets (70% -> 55% -> 40% of peak, per the blueprint), plus a `taper` TID row that holds intensity rather than reverting to base. Wire or delete the orphaned `volume_factor`. Must be live and validated by early August.
2.3 **Taper length - DECIDED (2026-07-05): ~2 weeks, evidence-based, both A and B.** A tapers from ~5 Sep (race 19 Sep); B from ~6 Sep. Fix B's race-day CTL milestone (84) sitting at peak (85) so the taper genuinely sheds fatigue. Note this moves A's taper start from 22 Aug to ~5 Sep - the heat block (22 Aug) now overlaps the final peak weeks rather than the taper; sanity-check that interaction when implementing.

## Phase 3 - Bring B and C to A's standard (1-2 days) [P1-1, P1-2, P1-6, P1-7, P1-8, P1-10]

3.1 **B - thresholds first**: validate LTHR 191 against recent data before acting on her load numbers; her +49% "spike" and grey-zone share may both be hrTSS inflation. Then re-read the ramp/TID picture.
3.2 **B - config**: structured `day_rules`, corrected injury state (1.3), and regenerate `system_prompt.txt` hard numbers from live config at build time instead of hand-maintained constants (the current prompt contradicts config on ramp, CTL and injuries). Add cycle context to the chat layer. Strength - DECIDED: enable the strength *option* for Kathryn, but she already does sufficient strength outside the plan, so the immediate job is counting/recognising her existing strength work (log it so day-rules and load see it), not adding sessions.
3.3 **C - coherent targets**: reconcile `race_min` against the achievability-capped peak (race-day CTL currently exceeds peak, which is impossible); enforce `max_hours_per_week` at plan time; explicit `nutrition_target_g_hr` (60-75, not the 90 default). Strength - DECIDED: no strength programme for Calum; his CrossFit is his strength allocation.
3.4 **C - CrossFit load - DECIDED: log estimated TSS entries** (~40-60 TSS per session) into ICU so CTL/ATL genuinely see the load and every downstream rule works unmodified. Implement as a quick-log path (bot button or weekly batch) so it survives contact with real life.
3.5 **C - heat - DECIDED: skip.** No heat protocol for the Marmotte; his limited hours stay on fitness. Document the decision in his blueprint/state (so it reads as deliberate, not a silent default) and cover heat on the day via pacing/fuelling guidance in his event notes.
3.6 **Realised-TID audit** in `activity-watcher`: classify actual activities by realised zone and flag both excess quality and missing quality against the phase TID. Closes the "validates the label, not the reality" gap for everyone.
3.7 **Threshold drift alert** (no-test regime respected): flag, never auto-cut, when eFTP sits >X% below configured FTP for N weeks; populate the near-empty decoupling/efficiency logs; passive run-threshold estimate from GAP-at-HR on easy runs.

## Phase 4 - Race predictor + drift register (~1 day) [P1-3, P1-4, P1-9, P1-11, P2]

4.1 Race predictor: re-derive the anchor from the real `prev_race.bike_if` 0.636, cap predicted IF at a physiological ceiling (~0.72-0.75 long course), decouple the run estimate from bike IF, and commit the config to the repo so regeneration is reproducible.
4.2 Add a run-volume check to `validate_week` so chat-path pushes cannot bypass the stage-1-only caps; align the cap rule with the stated <=10% week-on-week; run caps must not silently become `None` on an ICU fetch error.
4.3 Add a within-week acute-load or monotony guard (the CTL ramp cap alone permits ~800 TSS weeks for A).
4.4 Consolidate the three TSB threshold sets to one config-driven source. [P2-2]
4.5 Carb target - DECIDED: **plan for 90 g/hr on the bike** (Jamie can likely tolerate over 90); align `race-plan.md` / `run-execution.md` / config to 90. But fuelling targets must be a per-athlete conversation, not a hard rule: keep `nutrition_target_g_hr` athlete-level and have the chat/check-in layer treat it as a negotiated target reviewed against logged gut-training sessions, never a fixed prescription applied to every athlete. [P1-11]
4.6 Sweep the remaining 15-item drift register and P2 list; anything left over becomes seed input for the nightly bug-fixer.

---

## Decisions - RECORDED 2026-07-05

1. **Taper length**: ~2 weeks, evidence-based, for both A and B (see 2.3).
2. **Backstop flip**: fully authoritative - engine prescription is what reaches the athlete; LLM writes the wording (see 1.1). Shadow-log review still happens first.
3. **Calum heat**: skip; document as deliberate (see 3.5).
4. **Calum CrossFit**: log estimated TSS entries into ICU (see 3.4).
5. **Carbs**: plan for 90 g/hr; fuelling is a per-athlete conversation, never a hard global rule (see 4.5).
6. **Strength**: Kathryn gets the option, with her existing strength work counted rather than new sessions added; Calum none - CrossFit is his strength (see 3.2/3.3).
7. **CORRECTION**: Kathryn has no ankle injury; all ankle data in her context is contamination from Jamie's and must be purged, with the leak traced (see 1.3).

## Explicitly out of scope / protect (the audit's keep list)

Deterministic dose gating, LLM-free planned TSS, fail-safe push ordering, the fuelling and heat models, the humble cycle-tracking posture, and A's chat scaffold are strengths. No rework may regress them; the menstrual model in particular must not be "strengthened" into prescribing by phase.
