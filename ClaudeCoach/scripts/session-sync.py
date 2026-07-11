#!/usr/bin/env python3
"""
Session sync — runs hourly (07:00-22:00) via VM crontab.

Reads the last N message pairs from history.json, extracts any new coaching rules
that weren't written during the session, prunes expired/stale entries, and alerts
Jamie if ClaudeCoach made promises it hasn't confirmed completing.

Rule-pile self-maintenance (11 Jul 2026). The sync used to only ever ADD [perm] rules and
never remove them, so the standing-rule pile grew without bound — the root cause of the coach
degradation. It now closes the loop in three tiers:

  A. APPEND GUARD (per-run, on newly-appended lines only): a [perm] line the model appends is
     reverted if it contradicts a confirmed preference, exactly duplicates an existing rule, or
     pushes the pile over the ceiling. This pass never removes a pre-existing rule.

  B. AUTO-CLEAR (per-run, whole file): trivially-safe redundancy is removed deterministically,
     after backing the file up to a timestamped .bak and logging every removal — duplicate
     [perm] lines with identical normalised content (keep the fullest), already-expired
     [expires:] lines, and [perm] lines whose normalised content is identical to a rule the
     engine already injects for everyone. Content is matched case/whitespace/punctuation-
     insensitive with digits preserved (never token-sets, which would merge 'rule 5' with
     'rule 6'). These are low-risk, reversible removals; nothing judgement-based is auto-deleted.

  C. AUTO-TRIGGER REVIEW (debounced): judgement work — semantic merges, contradictions, or a
     pile still at/over the ceiling after auto-clear — is NEVER auto-deleted here. Instead
     session-sync kicks off the human-reviewed bug-fixer prune flow (bug-fixer.py --fix) so it
     posts the existing yes/no/edit card and applies via the backed-up --apply-prune path.

When in doubt, tier B routes to tier C rather than deleting: only exact-content duplicate /
expired / engine-restatement lines are ever auto-cleared. Everything else stays.
"""
import importlib.util
import json, re, subprocess, sys, time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
BUGFIXER        = BASE / "scripts/bug-fixer.py"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MODEL   = "claude-sonnet-5"
TOOLS   = "Read,Write,Edit"

# Do not re-kick the reviewed consolidation more than once a day per athlete, even while the
# pile stays over ceiling (a card is already pending / a human just dismissed one).
CONSOLIDATE_MIN_INTERVAL = 24 * 3600

# bug-fixer's --fix builds worktrees/branches/review-ids keyed on the DATE + group index, not
# the athlete (rid = "<today>-<idx>"), so two concurrent --fix processes would collide on the
# same worktree path and clobber .bug-reviews.json. session-sync walks athletes sequentially and
# could otherwise launch one detached --fix each, so we serialise: at most ONE consolidation is
# launched per session-sync run. Reset at the top of main(); the per-athlete 24h marker then
# round-robins which athlete triggers on subsequent hourly runs.
_LAUNCH_STATE = {"fired": False}

sys.path.insert(0, str(BASE / "lib"))
import claude_call

# Reuse the Phase 7 rule-hygiene helpers rather than re-implementing them: the bug-fixer
# is the single source of truth for the ceiling, the confirmed-preference scan, the engine
# rule constants, the review state and the token/forbidden-term logic. The filename is
# hyphenated so it cannot be imported by name; load it by path. A failure here raises and
# kills the run loudly (nothing syncs, the error lands in the cron log) — that is deliberately
# preferable to appending or clearing rules unguarded.
_bf_spec  = importlib.util.spec_from_file_location(
    "cc_bug_fixer", str(BUGFIXER))
bug_fixer = importlib.util.module_from_spec(_bf_spec)
_bf_spec.loader.exec_module(bug_fixer)

CEILING  = bug_fixer.RULE_COUNT_CEILING
# A [perm] standing rule (the append-only lines that bloated); [expires:] lines carry a date.
_PERM_RE    = re.compile(r"^\s*\[perm\]", re.I)
_EXPIRES_RE = re.compile(r"^\s*\[expires:(\d{4}-\d{2}-\d{2})\]", re.I)
_TAG_RE     = re.compile(r"^\s*\[(?:perm|expires:[^\]]*)\]\s*", re.I)


def _norm(line: str) -> str:
    """Whitespace/case-insensitive normal form for exact-string comparison."""
    return re.sub(r"\s+", " ", line.strip()).lower()


def _content_key(line: str) -> str:
    """Normalised rule CONTENT (tag stripped; case/whitespace-insensitive; surrounding
    punctuation trimmed). Deliberately NOT token-set based: bug_fixer._tokens drops digits and
    short words, which would wrongly merge distinct enumerated rules ('rule 5' vs 'rule 6').
    Two lines with the same content key are the same rule bar case / spacing / trailing
    punctuation — a genuinely safe duplicate. Reordered or reworded variants differ here by
    design and fall through to the reviewed prune (the safe direction)."""
    s = re.sub(r"\s+", " ", _TAG_RE.sub("", line)).strip().lower()
    return s.strip(".,;:!?-–— ")


def _line_conflicts(line: str, prefs: list) -> str:
    """Return the confirmed preference a [perm] line contradicts, else ''. Mirrors
    bug-fixer._rule_conflict's deterministic backstop: a line that ASSERTS terms a confirmed
    preference explicitly FORBIDS, on the same topic, is a conflict. Conservative — only ever
    used to reject an append or route a contradiction to review, never to auto-delete a rule.
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
    """TIER A — append guard. Run AFTER the model has edited persistent-rules.md; reverts ONLY
    newly-appended [perm] lines that (a) contradict a confirmed preference, (b) exactly
    duplicate an existing [perm] line, or (c) push the standing-rule count above the ceiling.
    This pass NEVER removes a pre-existing rule (the resulting [perm] multiset still contains
    every pre-run [perm] line at least as many times) — that is asserted below. Returns
    (new_text, drops) where drops is a list of (reason, text); a drops entry whose reason
    starts 'ABORT' means the guard refused to write anything."""
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

    for i in appended_idx:
        line = after_lines[i]
        pref = _line_conflicts(line, prefs)
        if pref:
            dropped[i] = f"conflicts with confirmed preference: {pref.strip()[:120]}"
            continue
        if before_norm.get(_norm(line), 0) > 0:
            dropped[i] = "exact duplicate of an existing [perm] rule"

    def _standing_count():
        kept = [l for j, l in enumerate(after_lines) if j not in dropped]
        return bug_fixer._count_rules("".join(kept))

    for i in sorted((j for j in appended_idx if j not in dropped), reverse=True):
        if _standing_count() <= CEILING:
            break
        dropped[i] = f"append would exceed the standing-rule ceiling ({CEILING})"

    if not dropped:
        return after_text, []

    new_text = "".join(l for j, l in enumerate(after_lines) if j not in dropped)

    # Invariant: tier A never shrinks the pile below its pre-run state. If a removal would have
    # taken out a pre-existing rule, refuse to write anything and surface it.
    final_norm = Counter(_norm(l) for l in new_text.splitlines() if _PERM_RE.match(l))
    for n, c in before_norm.items():
        if final_norm.get(n, 0) < c:
            return after_text, [("ABORT: guard would shrink pre-existing rules; left file untouched", "")]

    drops = [(reason, after_lines[i].strip()) for i, reason in sorted(dropped.items())]
    return new_text, drops


_AUTO_CLEAR_CATEGORIES = ("exact-dup", "expired", "engine-restatement")


def _auto_clear(text: str, today: str, engine_rules: dict):
    """TIER B — deterministic auto-clear of trivially-safe redundancy. Returns
    (new_text, removals) where removals is a list of (category, text). Removes ONLY:
      - 'exact-dup': [perm] lines with the same normalised content — keep the fullest, drop the rest;
      - 'expired':   [expires:YYYY-MM-DD] lines whose date is strictly before today;
      - 'engine-restatement': a [perm] line whose normalised content is IDENTICAL to a rule the
        engine already injects for everyone (pure restatement; code enforces it regardless).
    Content is compared via _content_key (case/whitespace/punctuation-insensitive, digits kept),
    NOT token-sets, so distinct enumerated rules are never merged. Anything that is not a provable
    member of one of these categories is left untouched and is the reviewed prune path's job. The
    invariant is asserted before returning: every removed line carries an allowed category, so
    this pass can never silently delete a judgement rule."""
    lines    = text.splitlines(keepends=True)
    perm     = [(i, l) for i, l in enumerate(lines) if _PERM_RE.match(l)]
    remove   = {}                                     # idx -> category

    # Exact restatement of an engine-injected rule (identical normalised content, not overlap).
    engine_keys = {_content_key(v) for v in (engine_rules or {}).values()}
    engine_keys.discard("")

    by_key = {}
    for i, l in perm:
        by_key.setdefault(_content_key(l), []).append((i, l))

    for key, group in by_key.items():
        if not key:                                   # no content — never touch
            continue
        if key in engine_keys:
            for i, _ in group:
                remove[i] = "engine-restatement"
            continue
        if len(group) > 1:                            # exact duplicates — keep the fullest wording
            keep_i = max(group, key=lambda il: len(_norm(il[1])))[0]
            for i, _ in group:
                if i != keep_i:
                    remove[i] = "exact-dup"

    # Already-expired dated lines (deterministic; do not trust the model prompt for this).
    for i, l in enumerate(lines):
        m = _EXPIRES_RE.match(l)
        if m and m.group(1) < today:
            remove[i] = "expired"

    if not remove:
        return text, []

    # Invariant: every removed line is a provable auto-clear category. Anything else stays.
    assert all(cat in _AUTO_CLEAR_CATEGORIES for cat in remove.values())

    new_text = "".join(l for i, l in enumerate(lines) if i not in remove)
    removals = [(remove[i], lines[i].strip()) for i in sorted(remove)]
    return new_text, removals


def _contradictions(text: str, prefs: list) -> list:
    """Existing [perm] lines (not themselves a confirmed preference) that contradict a confirmed
    preference. These are judgement cases — never auto-deleted, they route to the reviewed prune."""
    confirmed = {_norm(p) for p in prefs}
    out = []
    for l in text.splitlines():
        if not _PERM_RE.match(l) or _norm(l) in confirmed:
            continue
        if _line_conflicts(l, prefs):
            out.append(l.strip())
    return out


def _has_awaiting_prune(reviews: dict, slug: str) -> bool:
    return any(r.get("slug") == slug and r.get("kind") == "prune"
               and r.get("status") == "awaiting"
               for r in (reviews or {}).values())


def _marker_age_ok(marker: Path, now: float, min_interval: int = CONSOLIDATE_MIN_INTERVAL) -> bool:
    """True if the reviewed consolidation has NOT been kicked off for this athlete within
    min_interval (or never). Closes the detached-fire race (the marker is set the moment we
    launch, so we cannot re-fire the next hour before the card lands) and BOUNDS the
    dismiss-then-refire loop to at most once per 24h — a dismissed card for a still-eligible
    pile will re-surface after the window rather than every hour, which the awaiting-review
    check alone would not prevent."""
    try:
        return (now - marker.stat().st_mtime) >= min_interval
    except FileNotFoundError:
        return True


def _should_trigger(final_count: int, contradictions: list, reviews: dict,
                    slug: str, marker: Path, now: float):
    """TIER C decision (pure/testable). Kick off the reviewed prune only when there is
    judgement work — the pile is still at/over the ceiling after auto-clear, or a contradiction
    remains — AND no prune card is already awaiting for this athlete AND we have not already
    triggered within the debounce window. Returns (should_fire, reason)."""
    if final_count < CEILING and not contradictions:
        return False, ""
    if _has_awaiting_prune(reviews, slug):
        return False, "prune review already awaiting"
    if not _marker_age_ok(marker, now):
        return False, "consolidation already triggered within 24h"
    bits = []
    if final_count >= CEILING:
        bits.append(f"over ceiling ({final_count}/{CEILING})")
    if contradictions:
        bits.append(f"{len(contradictions)} contradiction(s)")
    return True, " + ".join(bits)


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

    def _log(msg: str) -> None:
        with open(log_file, "a") as lf:
            lf.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{slug}] {msg}\n")

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

    # Snapshot the rule surface BEFORE the model runs so the append guard can tell which
    # [perm] lines are genuinely new and revert any that break a gate.
    rules_file   = adir / "persistent-rules.md"
    before_text  = rules_file.read_text() if rules_file.exists() else ""
    rule_count   = bug_fixer._count_rules(before_text)
    prefs        = bug_fixer._confirmed_preferences(slug)
    engine_rules = bug_fixer._engine_rule_constants()

    prompt = _build_prompt(slug, first_name, history, today,
                           rule_count, prefs, engine_rules)

    _log(f"running sync (rules={rule_count}/{CEILING})")
    with open(log_file, "a") as lf:
        # Sonnet -> Haiku fallback (frequent, low-stakes): keeps sync alive when
        # the Sonnet weekly bucket is maxed, without draining the all-models pool.
        result = claude_call.run_claude(
            prompt, model=claude_call.SONNET, allowed_tools=TOOLS,
            stderr=lf, cwd=PROJECT_DIR, timeout=300, label=slug,
        )

    output = (result.stdout or "").strip()
    if output:
        _log(f"unexpected output: {output[:200]}")

    # TIER A — append guard: revert any [perm] line the model appended that breaks a gate.
    text = rules_file.read_text() if rules_file.exists() else ""
    if text != before_text:
        guarded, drops = _enforce_rule_guards(before_text, text, prefs)
        if drops:
            if guarded != text:
                rules_file.write_text(guarded)
                text = guarded
            for reason, dline in drops:
                _log(f"append guard dropped — {reason}: {dline}")

    # TIER B — auto-clear trivially-safe redundancy (backup + log every removal).
    cleared, removals = _auto_clear(text, today, engine_rules)
    if removals:
        stamp  = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = rules_file.with_suffix(f".bak-autoclear-{stamp}.md")
        backup.write_text(rules_file.read_text())     # back up the exact pre-clear file
        rules_file.write_text(cleared)
        text = cleared
        _log(f"auto-clear backup at {backup.name}")
        for cat, cline in removals:
            _log(f"auto-cleared [{cat}]: {cline}")

    # TIER C — kick off the reviewed consolidation for judgement work (debounced + detached).
    final_count    = bug_fixer._count_rules(text)
    contradictions = _contradictions(text, prefs)
    reviews        = bug_fixer._load_reviews()
    marker         = LOG_DIR / f".consolidate-trigger-{slug}"
    fire, reason   = _should_trigger(final_count, contradictions, reviews,
                                     slug, marker, time.time())
    if fire and _LAUNCH_STATE["fired"]:
        # Another athlete's --fix already launched this run; serialise to avoid a colliding
        # concurrent worktree/review-id. This athlete triggers on a later hourly run.
        _log(f"consolidation needed ({reason}) but a --fix already launched this run — deferring")
    elif fire:
        try:
            subprocess.Popen(
                ["python3", str(BUGFIXER), "--fix", "--athlete", slug],
                cwd=PROJECT_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _LAUNCH_STATE["fired"] = True
            marker.write_text(datetime.now().isoformat())
            _log(f"auto-triggered reviewed consolidation ({reason}); bug-fixer --fix launched")
        except Exception as e:
            _log(f"consolidation trigger failed to launch: {e}")
    elif final_count >= CEILING or contradictions:
        why = reason or "awaiting/debounced"
        _log(f"OVER-CEILING/contradiction ({final_count}/{CEILING}, "
             f"{len(contradictions)} contradiction(s)) — not triggering: {why}")


def main() -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] session-sync starting", file=sys.stderr)
    _LAUNCH_STATE["fired"] = False   # at most one reviewed consolidation launched per run

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
