# ClaudeCoach standing review checklist

Run this every review of the coaching bot. It exists because of the 22 Jul 2026
failure: the bot answered Kathryn's "what do next week's runs look like to hit
the target?" by improvising a week from stale prose rules — it dropped her
Build-phase Run Z4–5 slice (blueprint spec: 78% Z1–2 / 12% Z3 / 10% Z4–5),
narrated week-13 Build as "start of Peak", and asserted the whole thing was
spec-compliant. None of that was caught because reviews had only ever checked
that components existed, never that the *live bot* answered a forward-planning
question correctly.

The checklist is adversarial on purpose. A green build is not evidence the bot
plans correctly; only the questions below are.

## 1. Adversarial forward-plan Q&A against the blueprint (mandatory, every review)

Ask the **live bot** (Telegram, or `call_claude` against the real prompt) the
forward-planning questions an athlete actually asks, then audit its answer
against the numeric blueprint — do not accept the bot's own compliance claim.

- [ ] "What do next week's runs look like to hit the target?" — for an athlete
      mid-phase AND for one within a week of a phase boundary (the phase label
      must come from `athletes.json` config, not memory).
- [ ] "How do we hit [race goal / weekly load] next week?"
- [ ] "Give me next week's plan."
- For each answer:
  - [ ] Extract the stated week as zoned segments and run the gate:
        `python3 ClaudeCoach/lib/plan_distribution.py --athlete <slug>
        --week-start <Mon> --sessions '<json>'`. It must exit 0 / report
        `on_spec: true`. A non-zero exit or any OFF-SPEC finding is a FAIL —
        the bot must not have claimed compliance.
  - [ ] Confirm the answer came from the deterministic engine (calendar sessions
        or the FORWARD WEEK context block), NOT free-associated from prose.
  - [ ] If the week is not generated yet, the bot must SAY SO and give the
        blueprint target — it must not invent a session-by-session week.
  - [ ] Every required slice is present: for each sport with a non-zero Z3 or
        Z4–5 target, the plan contains that work (check BOTH directions — a
        missing slice AND excess quality both fail).
  - [ ] Phase label stated matches `primitives.blueprint.current_phase` for that
        week (no Build-narrated-as-Peak).

## 2. No unscoped verbal verdicts (mandatory)

Ban blanket claims like "the bot is spec-compliant" or "planning is fixed".

- [ ] Every capability claim names the component it is about (e.g. "the chat-path
      distribution gate flags a missing Z4–5 slice" — not "distribution is
      handled").
- [ ] Every claim carries its written caveats: what it does NOT check. Known
      limits to restate each time:
  - the gate audits **Run and Bike only** (swims/bricks excluded — name-based
    zone detection is unreliable there);
  - it needs the week expressed as **named zoned segments** (the engine proposal
    form). It does not reverse-map intervals.icu `%FTP`/`%pace` step bands,
    because run bands overlap (easy 78–88% vs z3 80–86%);
  - the always-on authority rule and FORWARD WEEK context are **prompt guidance**,
    not a hard interlock — they make the right answer the path of least
    resistance, they cannot mechanically block a bad chat reply. The gate is the
    only deterministic check, and only when actually invoked.
- [ ] No "done"/"resolved" without the evidence (command run + output) beside it.

## 3. Re-verify previously "resolved" items (mandatory)

Regressions hide behind old ticks. Each review, re-run — do not trust the last
review's checkmark.

- [ ] Re-run the full gate test suite in the live tree:
      `cd ironman-analysis && python3 -m pytest tests/test_plan_distribution.py -q`.
- [ ] Re-run the phase-label check for the current + next week for every active
      athlete (it moves as the calendar advances and as `athletes.json` changes).
- [ ] Re-run the two adversarial CLI cases against **live** data (they depend on
      `config/athletes.json` + the gitignored blueprint, which drift):
      a compliant week (exit 0) and a Z4–5-missing week (exit 1).
- [ ] Confirm `engine._AUTHORITY_RULE` is still injected by `build_prompt` AND
      `call_claude_with_image` (a prompt refactor can silently drop it):
      `python3 -c "import sys;sys.path.insert(0,'lib');import engine;
      assert 'PLANNING AUTHORITY' in engine.build_prompt('x',[],'s','A','c')"`.
- [ ] Confirm the running service is on the current code
      (`systemctl show claudecoach-bot -p ExecMainStartTimestamp` is after the
      last deploy) — a fix not restarted is not live.

## Sign-off

A review passes only when every mandatory box is ticked with evidence attached.
Record the date, the reviewer, and the athletes/weeks the adversarial Q&A
covered.
