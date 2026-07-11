#!/usr/bin/env python3
"""
Session sync — runs hourly (07:00-22:00) via VM crontab.

Reads the last N message pairs from history.json, extracts any new coaching rules
that weren't written during the session, prunes expired/stale entries, and alerts
Jamie if ClaudeCoach made promises it hasn't confirmed completing.

Growth guards (11 Jul 2026): this sync used to only ever ADD [perm] rules and never
remove them, so the standing-rule pile grew unbounded — the root cause of the coach
degradation. It now (1) refuses to let the model append a [perm] rule that contradicts
a confirmed preference, (2) refuses appends once the pile is at/over the ceiling, and
(3) drops exact duplicates. These are prompt instructions AND a deterministic post-check
that reverts any offending line the model appended anyway. Session-sync only ever PREVENTS
growth; SHRINKING the pile stays the human-reviewed bug-fixer/prune path — this code never
removes a pre-existing rule.
"""
import importlib.util
import json, re, subprocess, sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MODEL   = "claude-sonnet-5"
TOOLS   = "Read,Write,Edit"

sys.path.insert(0, str(BASE / "lib"))
import claude_call

# Reuse the Phase 7 rule-hygiene helpers rather than re-implementing them: the bug-fixer
# is the single source of truth for the ceiling, the confirmed-preference scan and the
# token/forbidden-term logic. The filename is hyphenated so it cannot be imported by name;
# load it by path. A failure here raises and kills the run loudly (nothing syncs, the error
# lands in the cron log) — that is deliberately preferable to appending rules unguarded.
_bf_spec  = importlib.util.spec_from_file_location(
    "cc_bug_fixer", str(BASE / "scripts" / "bug-fixer.py"))
bug_fixer = importlib.util.module_from_spec(_bf_spec)
_bf_spec.loader.exec_module(bug_fixer)

CEILING  = bug_fixer.RULE_COUNT_CEILING
# A [perm] standing rule (the append-only lines that bloated). [expires:] lines are transient
# and self-prune, so the growth guards below act on [perm] appends only.
_PERM_RE = re.compile(r"^\s*\[perm\]", re.I)


def _norm(line: str) -> str:
    """Whitespace/case-insensitive normal form for exact-duplicate comparison."""
    return re.sub(r"\s+", " ", line.strip()).lower()


def _line_conflicts(line: str, prefs: list) -> str:
    """Return the confirmed preference a proposed [perm] line contradicts, else ''.
    Mirrors bug-fixer._rule_conflict's deterministic backstop: a line that ASSERTS terms a
    confirmed preference explicitly FORBIDS, on the same topic, is a conflict. Conservative —
    only ever used to reject a would-be-appended line, never to remove an existing rule.
    This is the check that would have blocked the bare 'reply only Logged.' rule that fought
    Jamie's locked-in 'do not stop asking' preference."""
    ptoks = bug_fixer._tokens(line)
    if not ptoks:
        return ""
    for pref in prefs:
        ctoks = bug_fixer._tokens(pref)
        if not ctoks:
            continue
        forbidden = bug_fixer._forbidden_terms(pref) & ptoks
        overlap   = len(ptoks & ctoks) / max(1, len(ptoks | ctoks))
        if forbidden and overlap >= 0.25:
            return pref
    return ""


def _enforce_rule_guards(before_text: str, after_text: str, prefs: list):
    """Deterministic backstop run AFTER the model has edited persistent-rules.md.
    Reverts ONLY newly-appended [perm] lines that (a) contradict a confirmed preference,
    (b) exactly duplicate an existing [perm] line, or (c) push the standing-rule count above
    the ceiling. It NEVER removes a pre-existing rule — the resulting [perm] multiset always
    still contains every pre-run [perm] line at least as many times (shrinking stays the
    reviewed bug-fixer/prune path). Returns (new_text, drops) where drops is a list of
    (reason, text). An empty drops with new_text == after_text means the model's edit stands.
    A drops entry whose reason starts 'ABORT' means the guard refused to write anything."""
    after_lines = after_text.splitlines(keepends=True)
    perm_idx    = [i for i, l in enumerate(after_lines) if _PERM_RE.match(l)]

    before_norm = Counter(_norm(l) for l in before_text.splitlines() if _PERM_RE.match(l))
    after_norm  = Counter(_norm(after_lines[i]) for i in perm_idx)
    appended    = after_norm - before_norm            # multiset of NEW [perm] lines

    if not appended:
        return after_text, []

    # Attribute the appended copies to the TAIL-most physical lines, so we only ever touch
    # newly-added lines and never an identical pre-existing one earlier in the file.
    budget       = dict(appended)
    appended_idx = []
    for i in reversed(perm_idx):
        n = _norm(after_lines[i])
        if budget.get(n, 0) > 0:
            appended_idx.append(i)
            budget[n] -= 1
    appended_idx.sort()

    dropped = {}                                      # idx -> reason

    # (a) conflict with a confirmed preference, then (b) exact duplicate of an existing rule.
    for i in appended_idx:
        line = after_lines[i]
        pref = _line_conflicts(line, prefs)
        if pref:
            dropped[i] = f"conflicts with confirmed preference: {pref.strip()[:120]}"
            continue
        if before_norm.get(_norm(line), 0) > 0:
            dropped[i] = "exact duplicate of an existing [perm] rule"

    # (c) ceiling: the resulting standing-rule count must not exceed the ceiling. Drop the
    # newest-appended, not-yet-dropped [perm] lines (tail first) until within the ceiling.
    # We only ever drop APPENDED lines here; if appends alone cannot bring an already-bloated
    # pile back under the ceiling, the remainder is left for the reviewed consolidation path.
    def _standing_count():
        kept = [l for j, l in enumerate(after_lines) if j not in dropped]
        return bug_fixer._count_rules("".join(kept))

    for i in sorted((j for j in appended_idx if j not in dropped), reverse=True):
        if _standing_count() <= CEILING:
            break
        dropped[i] = f"append would exceed the standing-rule ceiling ({CEILING})"

    if not dropped:
        return after_text, []

    new_text  = "".join(l for j, l in enumerate(after_lines) if j not in dropped)

    # Invariant: never shrink the pile below its pre-run state. If a removal would have taken
    # out a pre-existing rule, refuse to write anything and surface it.
    final_norm = Counter(_norm(l) for l in new_text.splitlines() if _PERM_RE.match(l))
    for n, c in before_norm.items():
        if final_norm.get(n, 0) < c:
            return after_text, [("ABORT: guard would shrink pre-existing rules; left file untouched", "")]

    drops = [(reason, after_lines[i].strip()) for i, reason in sorted(dropped.items())]
    return new_text, drops


def _build_prompt(slug: str, first_name: str, history: list, today: str,
                  rule_count: int, confirmed_prefs: list, engine_rules: dict) -> str:
    # Format recent messages
    msg_lines = []
    for pair in history:
        u = pair.get("user", "").strip()
        a = pair.get("assistant", "").strip()
        if u:
            msg_lines.append(f"{first_name}: {u}")
        if a:
            msg_lines.append(f"ClaudeCoach: {a}")
    messages = "\n".join(msg_lines) if msg_lines else "(no messages)"

    rules_file    = BASE / f"athletes/{slug}/persistent-rules.md"
    state_file    = BASE / f"athletes/{slug}/current-state.md"

    over_ceiling = rule_count >= CEILING
    prefs_block  = "\n".join(confirmed_prefs) if confirmed_prefs else "(none marked)"
    engine_block = ("\n".join(f"- {v}" for v in engine_rules.values())
                    if engine_rules else "(none available)")

    if over_ceiling:
        ceiling_note = (
            f"   ** The standing-rule pile is AT/OVER its ceiling ({rule_count} of {CEILING}). "
            f"Do NOT append ANY new [perm] rule this run. **\n"
            f"   Skip the rest of task 1 and do tasks 2-4 only (prune expired [expires:] lines and\n"
            f"   maintain state). Shrinking an over-ceiling pile is handled separately by the\n"
            f"   human-reviewed bug-fixer — not here.")
    else:
        ceiling_note = (
            f"   The pile holds {rule_count} standing rules against a {CEILING} ceiling — appending\n"
            f"   is allowed but stay strict per the gates below.")

    return f"""\
Session sync — {today}

You are the ClaudeCoach session sync. Review the recent conversation and maintain two persistent files.

== RECENT MESSAGES ==
{messages}

== CONFIRMED PREFERENCES (locked in — a new rule must NEVER contradict any of these) ==
{prefs_block}

== ALREADY ENFORCED IN CODE (do NOT restate any of these as a new rule) ==
The coach already applies these for every athlete via the engine and Phase 1 code:
accuracy/single-source training load, no eyeballing of numbers, summing session loads
correctly, showing units, and preview-before-write. Also:
{engine_block}

== TASKS ==

1. SCAN for new rules or preferences {first_name} stated or ClaudeCoach agreed to.
   Read {rules_file} first to avoid duplicates.
{ceiling_note}
   For each genuinely new rule (subject to the gates below): append one line to {rules_file}
   using the Edit tool.
   Format: [perm] <rule text>                    — permanent, no expiry
       OR: [expires:YYYY-MM-DD] <rule text>      — event/block specific; use event end date
   Append only — never rewrite or remove existing lines.
   GATES — do NOT append a rule if ANY of these hold:
     - CONFLICT: it contradicts a confirmed preference above (e.g. it tells the coach to do
       something a preference says never to do). Skip it entirely.
     - DUPLICATE: {rules_file} already captures it, even in different wording or as a near-
       paraphrase. Skip it.
     - ALREADY ENFORCED: it merely restates something in the ENFORCED-IN-CODE list. Skip it —
       do not re-add as prose what code already guarantees.
     - OVER CEILING: the pile is at/over {CEILING} (see the note above). Append nothing.

2. PRUNE expired entries from {rules_file}.
   Remove any line where [expires:YYYY-MM-DD] date is strictly before today ({today}).
   Use the Edit tool to remove those lines only. Leave all [perm] lines untouched.

3. PRUNE stale entries from {state_file}:
   - Travel/training block table rows where the block end date + 7 days < {today} → remove the row
   - Open actions where status = done AND the completion date > 7 days ago → remove the entry
   Use the Edit tool for surgical removals — never rewrite whole sections.
   If nothing qualifies for pruning, skip this task entirely.

4. MAINTAIN the rolling context summary in {state_file} so the coach keeps context
   across long conversations. Keep a section headed EXACTLY "## Recent context (auto-summary)".
   If it does not exist, create it once (insert near the top, just after the title /
   "Last updated" line). Each run, REPLACE only THIS section's body (leave every other
   section completely untouched) with a concise bullet digest of what the coach should
   remember right now:
   - the last ~5 sessions with RPE / how-it-felt if given
   - current injury / pain status and any active protocol
   - latest weight + trend vs race-day target
   - any open commitments or things {first_name} recently asked for
   - notable preferences or changes from the recent conversation NOT already a [perm] rule
   Keep it under ~15 bullets; drop anything older than ~10 days unless still relevant.
   Use the Edit tool to replace only this section's contents (match from the
   "## Recent context (auto-summary)" header to the next "## " header).

OUTPUT FORMAT:
- Use tools to write/edit files for tasks 1-4.
- No text output under any circumstances. Absolute silence.
"""


def run_athlete(slug: str, athlete_cfg: dict) -> None:
    adir     = BASE / f"athletes/{slug}"
    chat_id  = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "session-sync.log"

    history_file = adir / "telegram/history.json"
    if not history_file.exists():
        return

    try:
        history = json.loads(history_file.read_text())
    except Exception as e:
        print(f"[{slug}] Failed to read history: {e}", file=sys.stderr)
        return

    if not history:
        return

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass
    first_name = profile.get("name", slug).split()[0]

    today  = date.today().isoformat()

    # Snapshot the rule surface BEFORE the model runs so the deterministic guards can tell
    # which [perm] lines are genuinely new and revert any that break a gate.
    rules_file  = adir / "persistent-rules.md"
    before_text = rules_file.read_text() if rules_file.exists() else ""
    rule_count  = bug_fixer._count_rules(before_text)
    prefs       = bug_fixer._confirmed_preferences(slug)
    engine_rules = bug_fixer._engine_rule_constants()

    prompt = _build_prompt(slug, first_name, history, today,
                           rule_count, prefs, engine_rules)

    with open(log_file, "a") as lf:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lf.write(f"[{ts}] [{slug}] running sync (rules={rule_count}/{CEILING})\n")
        # Sonnet -> Haiku fallback (frequent, low-stakes): keeps sync alive when
        # the Sonnet weekly bucket is maxed, without draining the all-models pool.
        result = claude_call.run_claude(
            prompt, model=claude_call.SONNET, allowed_tools=TOOLS,
            stderr=lf, cwd=PROJECT_DIR, timeout=300, label=slug,
        )

    output = (result.stdout or "").strip()
    if output:
        with open(log_file, "a") as lf:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lf.write(f"[{ts}] [{slug}] unexpected output: {output[:200]}\n")

    # Deterministic post-check: revert any [perm] line the model appended that breaks a gate.
    after_text = rules_file.read_text() if rules_file.exists() else ""
    if after_text != before_text:
        new_text, drops = _enforce_rule_guards(before_text, after_text, prefs)
        if drops:
            with open(log_file, "a") as lf:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if new_text != after_text:
                    rules_file.write_text(new_text)
                for reason, text in drops:
                    lf.write(f"[{ts}] [{slug}] rule guard dropped append — {reason}: {text}\n")

    # Over-ceiling flag for ops visibility: the pile needs consolidation, which the reviewed
    # bug-fixer handles (its planner independently sees the same count when it next runs for an
    # athlete that has feedback-log entries). This log line does NOT itself trigger the fixer.
    final_count = bug_fixer._count_rules(after_text)
    if final_count >= CEILING:
        with open(log_file, "a") as lf:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lf.write(f"[{ts}] [{slug}] OVER-CEILING: {final_count}/{CEILING} standing rules — "
                     f"needs consolidation via the reviewed bug-fixer/prune path\n")


def main() -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] session-sync starting", file=sys.stderr)

    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
    except Exception as e:
        print(f"[{ts}] Failed to load athletes config: {e}", file=sys.stderr)
        sys.exit(1)

    for slug, cfg in athletes.items():
        if not cfg.get("active", True):
            continue
        try:
            run_athlete(slug, cfg)
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] session-sync error: {exc}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
