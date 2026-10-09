#!/usr/bin/env python3
"""
Session sync — runs every 2 hours (07:40-21:40) via VM crontab.

Every slot, in Python: prunes expired rules and stale travel rows, keeps a small
"Latest data" block current, and runs the rule-pile tiers B and C below. At the last
slot of the day, and only if the athlete wrote something new, Claude reads the chat
for rules the live chat did not save and rewrites the rolling context summary
(see "Python every slot, Claude once a night" below, 28 Sep 2026).

Rule-pile self-maintenance (11 Jul 2026). The sync used to only ever ADD [perm] rules and
never remove them, so the standing-rule pile grew without bound — the root cause of the coach
degradation. It now closes the loop in three tiers:

  A. CAPTURE GUARD (per-run): a [perm] line the model appends is reverted if it contradicts a
     confirmed preference, exactly duplicates an existing rule, or pushes the pile over the
     ceiling. The model may ALSO fold a refinement into the existing rule it extends (edit in
     place) instead of appending a near-duplicate — the churn fix. Such an edit is permitted only
     when loss-free (every removed rule's content survives, numbers included, in a rule still on
     file); any lossy edit fails the invariant and the whole write is refused (file untouched).

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
import hashlib
import importlib.util
import json, re, subprocess, sys, time
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

MODEL   = "haiku"     # Haiku 5.5 for behind-the-scenes jobs (Jamie, 9 Oct 2026)
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
import ops_log
import planning_pause    # tracking-only athletes: no prescribing, no adherence

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

CEILING = bug_fixer.RULE_COUNT_CEILING

# TIER A capture guard (_enforce_rule_guards + its content/token helpers) now lives in
# lib/rules_capture.py, shared with telegram/bot.py's live capture path so both callers
# apply the exact same fold-on-write invariant instead of two copies that could drift.
import rules_capture
import rule_registry
_PERM_RE            = rules_capture._PERM_RE
_EXPIRES_RE         = rules_capture._EXPIRES_RE
_TAG_RE             = rules_capture._TAG_RE
_norm               = rules_capture._norm
_content_key        = rules_capture._content_key
_sig_tokens         = rules_capture._sig_tokens
_line_conflicts     = rules_capture._line_conflicts
_enforce_rule_guards = rules_capture.enforce_rule_guards


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
            f"Do NOT APPEND any new standalone [perm] rule this run. **\n"
            f"   You MAY still FOLD a refinement into an existing rule it extends (task 1b below) —\n"
            f"   that is count-neutral and keeps the pile tidy. Otherwise skip appends and do tasks\n"
            f"   2-4 only. Shrinking an over-ceiling pile is handled separately by the\n"
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
   Read {rules_file} FIRST, in full, so you know what each existing rule already covers.
{ceiling_note}

   For each thing worth capturing, decide APPEND vs FOLD:

   1a. APPEND — only for a GENUINELY NEW topic no existing rule covers. Add one line with Edit:
       Format: [perm] <rule text>                — permanent, no expiry
           OR: [expires:YYYY-MM-DD] <rule text>  — event/block specific; use event end date

   1b. FOLD — if the new detail REFINES or EXTENDS a topic an existing rule already covers (e.g.
       a new nutrition item for the nutrition-item glossary rule, a new nuance on the long-run
       progression rule), do NOT append a second near-duplicate line. Instead EDIT that existing
       rule in place to incorporate the detail, KEEPING every fact already in it (every number,
       product name and clause) and adding the new detail. This is the correct home for a
       refinement — a separate near-paraphrase line only gets merged away later.
       Loss-free only: never drop or change an existing figure/fact while folding; if you cannot
       fold without losing something, leave the rule alone and append instead.

   GATES — do NOT append/fold a rule if ANY of these hold:
     - CONFLICT: it contradicts a confirmed preference above (e.g. it tells the coach to do
       something a preference says never to do). Skip it entirely.
     - EXACT RESTATEMENT: an existing rule already says the same thing in the same way. Skip it
       (nothing to add). If it says a RELATED but not identical thing, FOLD (1b), don't append.
     - ALREADY ENFORCED: it merely restates something in the ENFORCED-IN-CODE list. Skip it —
       do not re-add as prose what code already guarantees.
     - OVER CEILING: the pile is at/over {CEILING} (see the note above). Do not APPEND; folding
       (1b) is still allowed.

2. MAINTAIN the rolling context summary in {state_file} so the coach keeps context
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
   The "## Latest data (auto)" section just above it (last sessions, weight, due actions)
   is rewritten by code every two hours: never edit it, and do not repeat its lines.
   (Expired rules and stale travel rows are also removed by code; do not prune them.)
   Use the Edit tool to replace only this section's contents (match from the
   "## Recent context (auto-summary)" header to the next "## " header).

OUTPUT FORMAT:
- Use tools to write/edit files for tasks 1-2.
- No text output under any circumstances. Absolute silence.
"""


# ── Python every slot, Claude once a night (28 Sep 2026, Jamie) ─────────────────
# The sync ran Claude 8 times a day per athlete (~$80/month at API prices even after a
# nothing-new gate). Only two of its jobs need a language model: spotting rules the
# athlete stated that the chat did not save, and the conversation-derived part of the
# rolling summary (decisions, open questions, promises). Measured over the last two
# weeks it added a rule in 6 of 240 runs; the chat itself saves a rule the moment the
# athlete states one (engine._FEEDBACK_LOG_RULE + rules_capture guards), so "save it
# now" never waited on this job. So:
#   - EVERY SLOT, Python only: expired rules and duplicates (tier B), the reviewed
#     consolidation trigger (tier C), stale travel-block rows, and a small
#     "## Latest data (auto)" section (last sessions, weight, due actions) that keeps
#     the summary's data current between the nightly passes.
#   - THE LAST SLOT OF THE DAY (21:40, CLAUDE_PASS_HOUR), Claude, and only if the
#     athlete wrote something since its last pass: rules the chat missed, and the
#     "## Recent context (auto-summary)" narrative.
# Coach-only history entries (morning cards, debriefs, briefs) are not "new chat": they
# carry nothing the athlete said.
CLAUDE_PASS_HOUR = 21
_SYNC_STATE = ".session-sync-state.json"
LATEST_HEADING = "## Latest data (auto)"
SUMMARY_HEADING = "## Recent context (auto-summary)"


def _chat_fingerprint(history: list) -> str:
    said = [(p.get("user", ""), p.get("assistant", "")) for p in history
            if (p.get("user") or "").strip()]
    return hashlib.sha256(json.dumps(said, ensure_ascii=False).encode()).hexdigest()


def _last_fingerprint(adir: Path) -> str | None:
    try:
        return json.loads((adir / _SYNC_STATE).read_text()).get("chat_fingerprint")
    except Exception:
        return None


def _save_fingerprint(adir: Path, fp: str) -> None:
    try:
        (adir / _SYNC_STATE).write_text(json.dumps(
            {"chat_fingerprint": fp, "at": datetime.now().isoformat(timespec="seconds")}))
    except Exception:
        pass    # worst case tomorrow night's pass re-reads the same chat


_ISO = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _prune_travel_blocks(md: str, today: date) -> tuple[str, list]:
    """Drop travel/training-block table rows whose LAST date is more than 7 days ago.
    Rows without a parseable ISO date are left alone: a wrong deletion is worse than a
    stale row."""
    out, removed, in_travel = [], [], False
    for ln in md.split("\n"):
        if ln.startswith("## "):
            in_travel = "travel" in ln.lower()
        if in_travel and ln.startswith("|") and not set(ln) <= set("|-: "):
            first = ln.split("|")[1] if ln.count("|") >= 2 else ""
            dates = _ISO.findall(first)
            if dates and (today - date.fromisoformat(max(dates))).days > 7:
                removed.append(ln)
                continue
        out.append(ln)
    return "\n".join(out), removed


def _latest_data(adir: Path, slug: str, today: date) -> str:
    """Last sessions, latest weight and due actions, straight from the athlete's files."""
    lines = [LATEST_HEADING,
             f"_Updated {datetime.now():%Y-%m-%d %H:%M} from the session log and state files. "
             f"The coach's own notes are in the auto-summary below._", ""]
    try:
        log = json.loads((adir / "session-log.json").read_text())
    except Exception:
        log = []
    recent = sorted((e for e in log if isinstance(e, dict) and e.get("date")),
                    key=lambda e: e["date"], reverse=True)[:5]
    for e in recent:
        d = date.fromisoformat(e["date"][:10])
        bits = [f"{d:%a %d %b}", e.get("sport") or "", f"\"{(e.get('name') or '').strip()}\""]
        if e.get("duration_min"):
            bits.append(f"{round(float(e['duration_min']))} min")
        if e.get("tss"):
            bits.append(f"Load {round(float(e['tss']))}")
        bits.append(f"RPE {e['rpe']}" if e.get("rpe") is not None else "RPE not given")
        lines.append("- " + " · ".join(b for b in bits if b))
    try:
        cs = json.loads((adir / "current-state.json").read_text())
        w = [r for r in cs.get("weight_readings") or [] if r.get("kg")]
        if w:
            lines.append(f"- Weight: {w[-1]['kg']} kg ({w[-1].get('date')})")
    except Exception:
        pass
    try:
        import open_actions as _oa
        due = [i for i in _oa.open_items(_oa.evaluate(slug, today))
               if i["bucket"] in ("overdue", "due_soon")][:3]
        for i in due:
            lines.append("- Action: " + _oa.render_line(i).replace("*", ""))
    except Exception:
        pass
    return "\n".join(lines) + "\n"


def _upsert_section(md: str, heading: str, body: str) -> str:
    """Replace the section starting at `heading` (up to the next "## "), or insert it just
    before the auto-summary, or after the first heading block if there is none."""
    lines = md.split("\n")
    if heading in lines:
        i = lines.index(heading)
        j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
        return "\n".join(lines[:i] + body.rstrip("\n").split("\n") + [""] + lines[j:])
    if SUMMARY_HEADING in lines:
        i = lines.index(SUMMARY_HEADING)
    else:
        i = next((k for k in range(1, len(lines)) if lines[k].startswith("## ")), len(lines))
    return "\n".join(lines[:i] + body.rstrip("\n").split("\n") + [""] + lines[i:])


def run_athlete(slug: str, athlete_cfg: dict, claude_pass: bool = False) -> None:
    adir     = BASE / f"athletes/{slug}"
    chat_id  = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "session-sync.log"

    def _log(msg: str) -> None:
        with open(log_file, "a") as lf:
            lf.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{slug}] {msg}\n")

    # HEARTBEATS on the three early exits. An athlete who has never chatted has no
    # history file, and one who cleared it has an empty one — both are "ran, nothing to
    # sync", i.e. SUCCESS. Left uninstrumented these would show as a permanent daily gap
    # for that athlete and the alarm would be crying wolf from day one. An UNREADABLE
    # history is different: that is corruption, and it records a failure.
    history_file = adir / "telegram/history.json"
    if not history_file.exists():
        ops_log.record_run("session-sync", athlete=slug, ok=True,
                           detail="silent (no history file yet)")
        return

    try:
        history = json.loads(history_file.read_text())
    except Exception as e:
        print(f"[{slug}] Failed to read history: {e}", file=sys.stderr)
        ops_log.alert("session-sync", f"history.json unreadable: {e}", athlete=slug)
        return

    if not history:
        ops_log.record_run("session-sync", athlete=slug, ok=True,
                           detail="silent (empty history)")
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

    # PYTHON PASS (every slot): stale travel rows, and the always-current data block.
    state_md_path = adir / "current-state.md"
    if state_md_path.exists():
        md_before = state_md_path.read_text()
        md_after, pruned = _prune_travel_blocks(md_before, date.fromisoformat(today))
        md_after = _upsert_section(md_after, LATEST_HEADING,
                                   _latest_data(adir, slug, date.fromisoformat(today)))
        if md_after != md_before:
            state_md_path.write_text(md_after)
        for row in pruned:
            _log(f"pruned stale travel row: {row[:100]}")

    # CLAUDE PASS (last slot of the day, only on new athlete chat).
    chat_fp = _chat_fingerprint(history)
    ran_model = claude_pass and chat_fp != _last_fingerprint(adir)
    model_ok = True
    if not claude_pass:
        _log(f"python pass (rules={rule_count}/{CEILING}); model runs at the {CLAUDE_PASS_HOUR}:40 slot")
    elif not ran_model:
        _log(f"nightly pass: no new athlete chat since the last one, model not run")
    else:
        _log(f"nightly pass: running sync (rules={rule_count}/{CEILING})")
        with open(log_file, "a") as lf:
            # Haiku 5.5 first (Jamie, 9 Oct 2026: behind-the-scenes jobs off Sonnet);
            # claude_call falls back to Sonnet if Haiku fails.
            result = claude_call.run_claude(
                prompt, model=claude_call.HAIKU, allowed_tools=TOOLS,
                stderr=lf, cwd=PROJECT_DIR, timeout=300, label=slug,
            )
        model_ok = result.returncode == 0

        output = (result.stdout or "").strip()
        if output:
            _log(f"unexpected output: {output[:200]}")

    # TIER A — capture guard: revert any appended [perm] line that breaks a gate; permit a
    # loss-free in-place fold of a refinement into the rule it extends; refuse any lossy edit.
    text = rules_file.read_text() if rules_file.exists() else ""
    if text != before_text:
        guarded, drops = _enforce_rule_guards(before_text, text, prefs, slug=slug)
        if drops:
            if guarded != text:
                rules_file.write_text(guarded)
                text = guarded
            for reason, dline in drops:
                _log(f"append guard dropped — {reason}: {dline}")

    # IDENTITY: give every rule now on disk a stable ID and a default type (sidecar
    # registry; the rule files themselves are never touched). Runs after the capture
    # guard so a reverted write is never registered. Cannot raise — identity
    # bookkeeping must not be able to undo or block a rule write.
    if text != before_text:
        rule_registry.register_after_write(BASE, slug)

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
        # Word-for-word into the athlete's rule notes, so the Your rules page can say what
        # ended (lib/athlete_rules.recently_ended) and nothing leaves without a record.
        try:
            sys.path.insert(0, str(BASE / "lib"))
            import athlete_rules
            athlete_rules.archive(slug, [{"reason": {"expired": "expired"}.get(cat, f"removed ({cat})"),
                                          "rule": cline} for cat, cline in removals])
        except Exception as e:
            _log(f"rule notes not written: {e}")

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

    # Completed the whole pass for this athlete. The sync is silent by design (the
    # prompt forbids output), so there is no "sent" to record — the heartbeat IS the
    # only evidence it ran, which is why its absence had to become detectable.
    if ran_model and model_ok:
        _save_fingerprint(adir, chat_fp)    # a failed run leaves it, so tomorrow retries
    ops_log.record_run("session-sync", athlete=slug, ok=True,
                       detail=(f"synced (rules {rule_count}->{final_count})" if ran_model
                               else f"python pass, model not run (rules {final_count})"))


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-now", action="store_true",
                    help="run the nightly Claude pass now, whatever the hour")
    args = ap.parse_args()
    claude_pass = args.claude_now or datetime.now().hour >= CLAUDE_PASS_HOUR
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
        # Tracking-only: this athlete keeps the activity watcher and the bot, but is
        # prescribed nothing and judged on nothing (lib/planning_pause.py).
        if planning_pause.is_paused(slug, cfg):
            print(planning_pause.skip_line(slug, "session-sync", cfg), file=sys.stderr)
            continue
        try:
            run_athlete(slug, cfg, claude_pass=claude_pass)
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] session-sync error: {exc}",
                  file=sys.stderr)
            ops_log.alert("session-sync", f"exception: {exc}", athlete=slug)


if __name__ == "__main__":
    main()
