#!/usr/bin/env python3
"""
ClaudeCoach nightly bug-fixer - two-stage pipeline.

Stage 1 (default, read-only): reads the feedback/bug log AND the whole rule surface
the coach actually applies (persistent-rules.md + system_prompt.txt + engine.py's
code-injected rules), then uses an agent to
(a) audit the rule surface for duplicate/stale/contradictory rules,
(b) check the codebase + git history for what's already been fixed,
(c) consolidate open entries that share a root cause into work groups, and
(d) classify each group fixable_now / needs_human / already_resolved / recurring with
    an action (prune / merge / code_fix / add_rule) and a plan. Outputs a plan (JSON).

The planner is biased AWAY from adding rules: it must prefer pruning, merging and code
fixes, must consolidate before adding once the rule count exceeds RULE_COUNT_CEILING,
must root-cause every issue before reaching for a prompt rule, must route recurrences
to a human, and must never propose a rule that contradicts a confirmed preference.

Stage 2 (--fix / --refix): for each fixable_now group, drafts the fix and posts a
Telegram review card with ✅ Yes / ❌ No / ✏️ Edit.
  - code_fix groups draft on a temporary git worktree branch and merge to main only on
    an explicit Yes reply (handled by the bot).
  - prune / merge / add_rule groups edit the athlete's persistent-rules.md (the single
    sanctioned exception to the athletes/ ban). Because athletes/ is gitignored and the
    repo is public, these do NOT use git: the proposal is carried in the review record
    and written to the live file only by --apply-prune, after an explicit Yes.
--refix revises a draft after an Edit instruction without creating a new entry.

Run:  python3 ClaudeCoach/scripts/bug-fixer.py [--athlete jamie] [--json]
      python3 ClaudeCoach/scripts/bug-fixer.py --fix <group_id>
      python3 ClaudeCoach/scripts/bug-fixer.py --refix <group_id> "<instruction>"
      python3 ClaudeCoach/scripts/bug-fixer.py --apply-prune <review_id>
Cron: 0 0 * * *  (midnight, Stage 1 only)
"""
import argparse, json, re, sys, subprocess, py_compile, difflib
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent.parent          # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/
sys.path.insert(0, str(BASE / "lib"))
import claude_call
import ops_log
import rules_lint

# Read-only tools - the planner must NOT modify anything.
TOOLS = "Read,Bash"

# Stage 2 (fixer) constants.
FIX_TOOLS     = "Read,Write,Edit,Bash"
PRUNE_TOOLS   = "Read,Edit"                    # rule consolidation edits ONE file, no shell
WORKTREE_BASE = "/tmp/cc-bugfix"
REVIEWS_FILE  = BASE / ".bug-reviews.json"     # gitignored review state (awaiting/merged/dismissed)
TG_CONFIG     = BASE / "telegram/config.json"

# Prevention rails (Phase 7, 11 Jul 2026). The rule pile bloated because triage was
# additive. The ceiling makes "prefer pruning" actionable: over it, the planner must
# consolidate before it may add anything.
RULE_COUNT_CEILING = 90

# A standing-rule line in persistent-rules.md: "[perm] ..." or "[expires:YYYY-MM-DD] ...".
_RULE_LINE_RE = re.compile(r"^\s*\[(perm|expires:[^\]]*)\]", re.I)

# Markers on a [perm] rule that mean Jamie has explicitly locked the preference in - a
# proposed rule must never contradict one of these.
_CONFIRMED_MARKERS = ("reconfirm", "explicitly reject", "rejects", "do not stop asking",
                      "do not suppress", "never regress", "jamie said")

# Negation cues that start a prohibition inside a confirmed preference ("do not X",
# "never X"). The deterministic conflict backstop treats the content words that FOLLOW
# such a cue as "forbidden" and flags any proposed rule that asserts them.
_NEG_WORDS = {"not", "never", "no", "dont", "cannot", "cant", "avoid"}

# Stop-words dropped before comparing message/rule token overlap.
_STOP = set("the a an and or to of for is are was were be been being in on at with without "
            "do not this that it when if so only just as by from into your you his her their "
            "have has had will would should could can may might must not no yes".split())


def _load_entries(slug: str):
    f = BASE / "athletes" / slug / "feedback-log.json"
    try:
        return json.loads(f.read_text())
    except Exception:
        return []


def _rules_path(slug: str) -> Path:
    return BASE / "athletes" / slug / "persistent-rules.md"


def _count_rules(text: str) -> int:
    """Number of standing-rule lines ([perm]/[expires:...]) in a persistent-rules.md body."""
    return sum(1 for ln in text.splitlines() if _RULE_LINE_RE.match(ln))


def _engine_rule_constants() -> dict:
    """Extract the rule strings engine.py injects for every athlete, WITHOUT importing it
    (import would pull heavy deps and run alongside concurrent engine edits). Adjacent
    string literals are folded by the parser, so ast.literal_eval reads each constant whole."""
    import ast
    out = {}
    try:
        tree = ast.parse((BASE / "lib" / "engine.py").read_text())
        want = {"_FEEDBACK_LOG_RULE", "_ACCURACY_RULE"}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if getattr(t, "id", None) in want:
                        try:
                            out[t.id] = ast.literal_eval(node.value)
                        except Exception:
                            pass
    except Exception as e:
        print(f"[bug-fixer] engine rule scan failed: {e}", file=sys.stderr)
    return out


def _load_rule_surface(slug: str) -> dict:
    """Assemble the FULL rule surface the coach actually applies for this athlete:
    persistent-rules.md + system_prompt.txt + the rule constants engine.py injects for
    everyone. Returns {'text', 'rule_count'} for the planner to audit for intra-surface
    duplicates and contradictions before it proposes anything."""
    rules_p = _rules_path(slug)
    sys_p   = BASE / "athletes" / slug / "system_prompt.txt"
    rules_txt = rules_p.read_text() if rules_p.exists() else "(no persistent-rules.md)"
    sys_txt   = sys_p.read_text()   if sys_p.exists()   else "(no system_prompt.txt)"
    count = _count_rules(rules_txt)
    blocks = [
        f"### persistent-rules.md - {count} standing rules (the append-only pile that bloated)",
        rules_txt.strip(),
        "",
        "### system_prompt.txt - the athlete's system prompt",
        sys_txt.strip(),
        "",
        "### engine.py - rules injected in code for EVERY athlete (not in the md, cannot be pruned there)",
    ]
    for k, v in _engine_rule_constants().items():
        blocks.append(f"[{k}] {v}")
    return {"text": "\n".join(blocks), "rule_count": count}


def _confirmed_preferences(slug: str) -> list:
    """The [perm] rules Jamie has explicitly locked in (RECONFIRMED / 'explicitly rejects'
    / 'do not suppress' / equivalents). A proposed rule must not contradict these - this is
    what the 'Logged.' rule violated (it fought his stated preference to keep asking)."""
    p = _rules_path(slug)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        s = ln.strip()
        if _RULE_LINE_RE.match(s) and any(m in s.lower() for m in _CONFIRMED_MARKERS):
            out.append(s)
    return out


def _tokens(msg: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (msg or "").lower())
            if len(w) > 3 and w not in _STOP}


def _prior_fix(entry: dict, slug: str, entries: list = None) -> dict:
    """If this bug looks like one ALREADY fixed before, return {commit, via, ref}. Two signals
    of different strength:
      via='prior_resolved_entry' (STRONG): a prior RESOLVED feedback entry whose (verbose)
        message is >=60% similar. High precision - drives the deterministic recurrence gate.
      via='prior_bugfix_commit'  (WEAK/advisory): a past 'bugfix' commit whose terse subject
        shares >=3 salient terms. Verbose report vs terse subject is noisy, so this only
        INFORMS the planner (it verifies via git before deciding), never a hard gate.
    Else {}."""
    toks = _tokens(entry.get("message"))
    if len(toks) < 3:
        return {}
    # (a) STRONG: a prior RESOLVED feedback entry describing a similar bug
    for e in (entries if entries is not None else _load_entries(slug)):
        if e is entry or e.get("status") != "resolved":
            continue
        other = _tokens(e.get("message"))
        if not other:
            continue
        jaccard = len(toks & other) / max(1, len(toks | other))
        if jaccard >= 0.6:
            return {"commit": e.get("resolution_commit") or "?", "via": "prior_resolved_entry",
                    "ref": (e.get("message") or "").strip()[:120]}
    # (b) WEAK/advisory: a prior 'bugfix' commit sharing >=3 salient terms (best match wins).
    best = None
    for line in _git(["log", "--oneline", "-n", "400", "-i", "--grep", "bugfix"]).stdout.splitlines():
        h, _, subj = line.partition(" ")
        shared = toks & _tokens(subj)
        if len(shared) >= 3 and (best is None or len(shared) > best[0]):
            best = (len(shared), h, subj.strip()[:120])
    if best:
        return {"commit": best[1], "via": "prior_bugfix_commit", "ref": best[2]}
    return {}


def _recurrence_map(slug: str, entries: list = None) -> dict:
    """{entry_index: prior_fix} for every OPEN entry that appears to have been fixed before
    (both strong and advisory signals - for the planner's RECURRENCE NOTES)."""
    entries = entries if entries is not None else _load_entries(slug)
    out = {}
    for i, e in enumerate(entries):
        if e.get("status") in ("resolved", "dismissed"):
            continue
        pf = _prior_fix(e, slug, entries)
        if pf:
            out[i] = pf
    return out


def _hard_recurrences(slug: str, entries: list = None) -> set:
    """Indices with the STRONG recurrence signal only (prior_resolved_entry). This is what
    run_fix uses to deterministically short-circuit to needs_human - the advisory git-log
    matches are left for the planner to verify, so an unrelated open bug is never blocked."""
    return {i for i, pf in _recurrence_map(slug, entries).items()
            if pf.get("via") == "prior_resolved_entry"}


def _forbidden_terms(pref: str) -> set:
    """The content words a confirmed preference explicitly forbids - the tokens that follow
    a negation cue ('do not suppress them; ... not to stop asking' -> {suppress, stop, asking}).
    A proposed rule that asserts these is contradicting the preference."""
    toks = re.findall(r"[a-z0-9']+", pref.lower())
    out = set()
    for i, w in enumerate(toks):
        if w in _NEG_WORDS:
            for nxt in toks[i + 1:i + 4]:
                if len(nxt) > 3 and nxt not in _STOP:
                    out.add(nxt)
    return out


def _rule_conflict(group: dict, prefs: list) -> str:
    """Return the confirmed preference a group's proposed rule contradicts, or ''.
    Trusts the planner's own conflicts_with first; then a deterministic backstop for
    add_rule / prompt_rule proposals: a proposal that ASSERTS terms a confirmed preference
    explicitly FORBIDS, on the same topic, is flagged. Conservative - this only ever routes
    a proposal to a human, it never suppresses an existing rule. (This is the check that
    would have blocked the 'reply only Logged.' rule that fought 'do not stop asking'.)"""
    declared = (group.get("conflicts_with") or "").strip()
    if declared:
        return declared
    proposed = (group.get("proposed_rule") or "").strip()
    is_rule  = (group.get("action") == "add_rule") or (group.get("fix_type") == "prompt_rule")
    if not proposed or not is_rule:
        return ""
    ptoks = _tokens(proposed)
    for pref in prefs:
        ctoks = _tokens(pref)
        if not ctoks:
            continue
        forbidden = _forbidden_terms(pref) & ptoks
        overlap = len(ptoks & ctoks) / max(1, len(ptoks | ctoks))
        if forbidden and overlap >= 0.25:
            return pref[:160]
    return ""


def _bug_mark_feedback(slug: str, entries: list, status: str, commit_hash: str = None):
    """Write status + resolution_commit back to the athlete's feedback-log.json,
    normalising any stale alternative field names in the same pass."""
    f = BASE / "athletes" / slug / "feedback-log.json"
    try:
        d = json.loads(f.read_text())
        for i in entries:
            if isinstance(i, int) and 0 <= i < len(d):
                entry = d[i]
                for old in ("resolution", "resolved", "fix"):
                    entry.pop(old, None)
                entry["status"] = status
                entry["resolution_commit"] = commit_hash
        f.write_text(json.dumps(d, indent=2))
    except Exception as e:
        print(f"[bug-fixer] mark feedback failed: {e}", file=sys.stderr)


def reconcile(slug: str):
    """Normalise feedback-log.json schema and back-fill resolution_commit from merged reviews."""
    f = BASE / "athletes" / slug / "feedback-log.json"
    if not f.exists():
        print("No feedback log found - nothing to reconcile.")
        return
    d = json.loads(f.read_text())
    changed = False
    # Step 1: normalise schema for every entry
    for entry in d:
        for old in ("resolution", "resolved", "fix"):
            if old in entry:
                if not entry.get("status"):
                    entry["status"] = "resolved" if entry[old] else "open"
                entry.pop(old)
                changed = True
        if "status" not in entry:
            entry["status"] = "open"
            changed = True
        if "resolution_commit" not in entry:
            entry["resolution_commit"] = None
            changed = True
    # Step 2: back-fill resolution_commit from merged reviews in .bug-reviews.json
    reviews = _load_reviews()
    for rv in reviews.values():
        if rv.get("slug", "jamie") != slug or rv.get("status") != "merged":
            continue
        rid = rv["id"]
        rv_entries = rv.get("entries", [])
        log_out = _git(["log", "--oneline", "--grep", f"bugfix {rid}:"]).stdout.strip()
        if not log_out:
            continue
        commit_hash = log_out.split()[0]
        for i in rv_entries:
            if isinstance(i, int) and 0 <= i < len(d):
                if not d[i].get("resolution_commit"):
                    d[i]["resolution_commit"] = commit_hash
                    d[i]["status"] = "resolved"
                    changed = True
    if changed:
        f.write_text(json.dumps(d, indent=2))
        print(f"[reconcile] updated {slug}/feedback-log.json")
    else:
        print(f"[reconcile] {slug}/feedback-log.json already consistent - no changes")


def _format_entries(entries):
    lines = []
    for i, e in enumerate(entries):
        lines.append(
            f"[{i}] {e.get('date','?')} | {e.get('type','?')}"
            f"{' | status=' + e['status'] if e.get('status') else ''}\n"
            f"    {(e.get('message') or '').strip()}"
        )
    return "\n".join(lines)


def _format_recurrence(rmap: dict) -> str:
    if not rmap:
        return "(none detected)"
    strength = {"prior_resolved_entry": "STRONG (duplicate of a resolved report)",
                "prior_bugfix_commit": "possible - VERIFY with git show before deciding"}
    return "\n".join(
        f"[{i}] {strength.get(pf.get('via'), pf.get('via','?'))} - prior fix commit "
        f"{pf.get('commit','?')}: {pf.get('ref','')}"
        for i, pf in sorted(rmap.items())
    )


PLAN_PROMPT = """You are the ClaudeCoach nightly bug-triage PLANNER. ANALYSIS ONLY - \
do NOT edit, write, fix, commit or deploy anything. You have Read and Bash (use Bash \
only for read-only inspection: git log, git grep, cat, ls).

You are given, below: the FULL bug/feedback log (oldest first); the CURRENT RULE SURFACE \
the coach already applies; the athlete's CONFIRMED PREFERENCES (locked in - never \
contradict); and RECURRENCE NOTES (open bugs that look already-fixed).

The standing rule set has grown large, duplicative and self-contradictory because past \
triage has been ADDITIVE - it keeps appending rules and never prunes. Your PRIMARY job this \
run is to STOP that: PREFER removing, merging and code fixes over adding any new rule.

Work in this order:

1. RULE-SURFACE AUDIT. Read the CURRENT RULE SURFACE FIRST. Detect duplicate, overlapping, \
   stale, expired or mutually contradictory rules BEFORE proposing anything new. There are \
   {rule_count} standing rules; the ceiling is {ceiling}. If the count exceeds the ceiling \
   you MUST propose prune/merge consolidations and MUST NOT propose ANY new rule until the \
   count is brought back under the ceiling.

2. CONTEXT. For each log entry, check whether it is ALREADY addressed in the current \
   codebase or was already fixed (see RECURRENCE NOTES). Run `git log --oneline -60` ONCE \
   for recent-fix context and use at most one or two targeted `git grep`/file reads per OPEN \
   issue. Entries older than ~10 days are very likely already resolved - confirm quickly, \
   don't deep-dive. Mark done items already_resolved with one line of evidence.

3. RECURRENCE. Check each OPEN entry against the RECURRENCE NOTES. A STRONG match (duplicate \
   of a resolved report) IS a recurrence: set verdict "recurring", put the prior commit in \
   prior_commit, and do NOT draft a fresh patch - a human must see why the earlier fix \
   regressed. A "possible" match is only a lead: confirm it with `git show <commit>` before \
   deciding; if it is not actually the same bug, treat the entry as normal.

4. ROOT CAUSE BEFORE RULE. For every genuinely OPEN issue, FIRST evaluate whether a code fix \
   or a tool-wiring fix removes the root cause. Set fix_type "prompt_rule" ONLY if a code or \
   tool-wiring fix is genuinely impossible, and then you MUST justify why in code_fix_ruled_out. \
   A new prompt rule is the LAST resort, never the default.

5. CONSOLIDATE the OPEN entries that share ONE root cause into ONE group. For each group set \
   an action: "prune" (delete dead/duplicate/superseded rules), "merge" (combine overlapping \
   rules into one, preserving every distinct preference), "code_fix" (change code / tool \
   wiring), or "add_rule" (add exactly ONE new standing rule - last resort only).

6. NEVER propose a rule that contradicts a CONFIRMED PREFERENCE. If a candidate rule would \
   collide with one, drop it and name the clashing preference in conflicts_with.

Classify each group's verdict:
   - "fixable_now": a clear bounded change (prune / merge / code_fix / add_rule). Concrete plan + files.
   - "needs_human": deep, ambiguous or methodology-level (no safe mechanical fix). Say what Jamie must decide.
   - "already_resolved": with one line of evidence.
   - "recurring": already fixed before and back again - route to a human, give prior_commit.

OUTPUT: ONLY a JSON object wrapped in <plan></plan>, no other prose:
<plan>
{"groups":[
  {"title":"short title",
   "entries":[<indices from the log>],
   "verdict":"fixable_now|needs_human|already_resolved|recurring",
   "action":"prune|merge|code_fix|add_rule",
   "fix_type":"code|tool_wiring|prompt_rule",
   "code_fix_ruled_out":"why code/tool-wiring cannot fix it (required when fix_type=prompt_rule), else ''",
   "proposed_rule":"the exact one-line rule to add (required when action=add_rule), else ''",
   "conflicts_with":"the confirmed preference this would contradict, or ''",
   "prior_commit":"the prior fix commit hash (required when verdict=recurring), else ''",
   "root_cause":"one or two sentences",
   "evidence":"for already_resolved: the commit/code that handles it; else ''",
   "plan":"for fixable_now: concrete steps + files; for needs_human: the decision needed; else ''",
   "files":["likely files to change, or []"]}
]}
</plan>

CURRENT RULE SURFACE ({rule_count} standing rules; ceiling {ceiling}):
{rule_surface}

CONFIRMED PREFERENCES (never contradict any of these):
{confirmed_prefs}

RECURRENCE NOTES (open entries that look already-fixed):
{recurrence}

THE LOG:
{log}
"""


def plan(slug: str) -> dict:
    entries = _load_entries(slug)
    if not entries:
        return {"groups": [], "_note": "no log entries"}
    surface  = _load_rule_surface(slug)
    prefs    = _confirmed_preferences(slug)
    rmap     = _recurrence_map(slug, entries)
    prompt = (PLAN_PROMPT
              .replace("{rule_count}", str(surface["rule_count"]))
              .replace("{ceiling}", str(RULE_COUNT_CEILING))
              .replace("{rule_surface}", surface["text"])
              .replace("{confirmed_prefs}", "\n".join(prefs) if prefs else "(none marked)")
              .replace("{recurrence}", _format_recurrence(rmap))
              .replace("{log}", _format_entries(entries)))
    result = claude_call.run_claude(
        prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
        allowed_tools=TOOLS, cwd=PROJECT_DIR, timeout=800, label=f"bugplan:{slug}",
    )
    out = (result.stdout or "").strip()
    m = re.search(r"<plan>(.*?)</plan>", out, re.DOTALL)
    if not m:
        return {"groups": [], "_error": "no <plan> block", "_raw": out[:800]}
    try:
        return json.loads(m.group(1).strip())
    except Exception as e:
        return {"groups": [], "_error": f"json parse: {e}", "_raw": m.group(1)[:800]}


def _render(plan_obj: dict) -> str:
    if plan_obj.get("_error"):
        return f"Planner error: {plan_obj['_error']}\n{plan_obj.get('_raw','')}"
    groups = plan_obj.get("groups", [])
    if not groups:
        return plan_obj.get("_note", "No groups produced.")
    icon = {"fixable_now": "🛠", "needs_human": "🧑", "already_resolved": "✅", "recurring": "🔁"}
    lines = [f"BUG TRIAGE - {date.today().isoformat()} - {len(groups)} group(s)\n"]
    for g in groups:
        tags = [g.get("verdict")]
        if g.get("action"):   tags.append(f"action={g['action']}")
        if g.get("fix_type"): tags.append(f"fix_type={g['fix_type']}")
        lines.append(f"{icon.get(g.get('verdict'),'•')} {g.get('title','(untitled)')}  "
                     f"[{'  '.join(t for t in tags if t)}]  entries={g.get('entries')}")
        if g.get("root_cause"):         lines.append(f"    root: {g['root_cause']}")
        if g.get("code_fix_ruled_out"): lines.append(f"    code ruled out: {g['code_fix_ruled_out']}")
        if g.get("proposed_rule"):      lines.append(f"    proposed rule: {g['proposed_rule']}")
        if g.get("conflicts_with"):     lines.append(f"    ⚠ conflicts with: {g['conflicts_with']}")
        if g.get("prior_commit"):       lines.append(f"    prior fix: {g['prior_commit']}")
        if g.get("evidence"):           lines.append(f"    evidence: {g['evidence']}")
        if g.get("plan"):               lines.append(f"    plan: {g['plan']}")
        if g.get("files"):              lines.append(f"    files: {', '.join(g['files'])}")
        lines.append("")
    return "\n".join(lines)


# ── Stage 2: fixer (draft on a worktree branch + review card) ─────────────────

FIX_PROMPT = """You are fixing ONE consolidated bug in the ClaudeCoach codebase. You are in a
fresh git worktree on a dedicated branch: make ONLY the minimal change for this bug. Do NOT
commit, push, deploy, or change anything unrelated. Do NOT edit athlete data under
ClaudeCoach/athletes/.

BUG: {title}
ROOT CAUSE: {root_cause}
PLAN: {plan}
LIKELY FILES: {files}

Implement the fix now (Read/Edit/Write, Bash for read-only inspection), tight and consistent
with the surrounding code. When done, output ONE line: a plain-English EXECUTIVE SUMMARY of the
change for Jamie's review card, describing the OUTCOME (what now behaves differently), not the
code mechanics. Do NOT name files, functions, variables, line numbers, or diff stats. Lead with
the verb. Good: "Planning now counts strength-session load and no longer double-counts warm-up
TSS." Bad: "Edited plan_builder.py, extracted _calc_load(), +12 lines." Max 200 chars."""


PRUNE_PROMPT = """You are consolidating ClaudeCoach's STANDING RULES for one athlete. The rule
set has grown bloated and self-contradictory. You may edit ONLY the single file in this working
directory: persistent-rules.md. (This is the ONLY sanctioned exception to the ban on editing
athlete data - change NOTHING else.)

WORK ITEM: {title}
ACTION: {action}
ROOT CAUSE / RATIONALE: {root_cause}
PLAN: {plan}
{proposed_rule_line}

Apply this now by editing ./persistent-rules.md:
 - prune: delete rules that are dead, expired, superseded, or exact/near duplicates.
 - merge: combine overlapping rules into ONE clear rule, preserving every distinct preference.
 - add_rule: append EXACTLY the one new rule given above, and nothing else.

HARD CONSTRAINTS: preserve the file's header comment lines and its line format
([perm]/[expires:YYYY-MM-DD] prefixes). NEVER drop a rule that states a unique, still-valid
preference. Do NOT invent preferences. Change ONLY persistent-rules.md.

When done, output ONE plain-English line (<=200 chars): the OUTCOME for Jamie's review card,
e.g. "Merged 6 overlapping scheduling rules into 2 and removed 3 duplicates (net -7 rules)."
Do NOT name files, functions, or line numbers."""


def _load_reviews():
    try:
        return json.loads(REVIEWS_FILE.read_text())
    except Exception:
        return {}


def _save_reviews(r):
    REVIEWS_FILE.write_text(json.dumps(r, indent=2))


def _git(args, cwd=PROJECT_DIR):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _tg_card(chat_id, review):
    """Post the review card with ✅Yes/❌No/✏️Edit. Best-effort.
    Uses HTML parse mode with escaped fields: the title/summary come from the fixer
    agent and routinely contain underscores, backticks and asterisks that break
    Telegram's legacy Markdown (silent HTTP 400, so the card never arrives). The card
    leads with the plain-English outcome; file scope is a small footnote, not the focus.
    Rule-consolidation (prune) cards show the rule-count change instead of file names."""
    import html, ssl, urllib.request
    try:
        token = json.loads(TG_CONFIG.read_text())["bot_token"]
        rid = review["id"]
        title   = html.escape(review.get("title", ""))
        summary = html.escape(review.get("summary", ""))
        if review.get("kind") == "prune":
            head   = "🧹 <b>Rule cleanup ready</b>"
            footer = html.escape(review.get("stat", ""))
        else:
            head   = "🛠 <b>Bug fix ready</b>"
            names  = [Path(f).name for f in review.get("files", []) if f]
            footer = html.escape(", ".join(names[:6]) + (f" +{len(names) - 6} more" if len(names) > 6 else ""))
        text = (f"{head}\n\n<b>{title}</b>\n{summary}\n\n"
                + (f"<i>{footer}</i>\n\n" if footer else "")
                + "Merge to live?")
        kb = {"inline_keyboard": [[
            {"text": "✅ Yes",  "callback_data": f"bf:yes:{rid}"},
            {"text": "❌ No",   "callback_data": f"bf:no:{rid}"},
            {"text": "✏️ Edit", "callback_data": f"bf:edit:{rid}"},
        ]]}
        ctx = ssl.create_default_context(
            cafile="/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None)
        body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                           "reply_markup": kb}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10, context=ctx)
    except Exception as e:
        print(f"[bug-fixer] card post failed: {e}", file=sys.stderr)


def _fix_group(group, idx, slug, dry_run):
    """Draft a CODE fix for one group on a dedicated branch. Returns review id or None.
    Worktree is always removed; the branch persists only for a real (non-dry-run)
    change that compiles - otherwise the branch is deleted too. Never merges."""
    rid    = f"{date.today().isoformat()}-{idx}"
    branch = f"bugfix/{rid}"
    wt     = f"{WORKTREE_BASE}-{rid}"
    keep_branch = False
    if _git(["worktree", "add", "-b", branch, wt, "HEAD"]).returncode != 0:
        print(f"[bug-fixer] {rid}: worktree add failed", file=sys.stderr)
        return None
    try:
        prompt = FIX_PROMPT.format(title=group.get("title", ""), root_cause=group.get("root_cause", ""),
                                   plan=group.get("plan", ""), files=", ".join(group.get("files") or []))
        res = claude_call.run_claude(prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
                                     allowed_tools=FIX_TOOLS, cwd=wt, timeout=900, label=f"bugfix:{rid}")
        summary = ((res.stdout or "").strip().splitlines() or ["(fix attempted)"])[-1][:200]
        _git(["add", "-A"], cwd=wt)
        stat = _git(["diff", "--staged", "--stat"], cwd=wt).stdout
        if not stat.strip():
            print(f"[bug-fixer] {rid}: agent made no changes - discarding", file=sys.stderr)
            return None
        for n in _git(["diff", "--staged", "--name-only"], cwd=wt).stdout.split():
            if n.endswith(".py"):
                try:
                    py_compile.compile(str(Path(wt) / n), doraise=True)
                except Exception as e:
                    print(f"[bug-fixer] {rid}: {n} fails compile ({e}) - discarding", file=sys.stderr)
                    return None
        _git(["commit", "-m", f"bugfix: {group.get('title', '')}"], cwd=wt)
        review = {"id": rid, "branch": branch, "slug": slug, "kind": "code", "title": group.get("title", ""),
                  "entries": group.get("entries", []), "summary": summary, "stat": stat,
                  "files": _git(["diff", "HEAD~1", "--name-only"], cwd=wt).stdout.split(),
                  "status": "awaiting", "created": date.today().isoformat()}
        if dry_run:
            print(f"\n--- WOULD POST [{rid}] {review['title']} ---\n{summary}\n{stat}")
        else:
            keep_branch = True
            reviews = _load_reviews(); reviews[rid] = review; _save_reviews(reviews)
            chat_id = json.loads((BASE / "config/athletes.json").read_text())[slug].get("chat_id", "")
            if chat_id:
                _tg_card(chat_id, review)
        return rid
    finally:
        _git(["worktree", "remove", "--force", wt])
        if not keep_branch:
            _git(["branch", "-D", branch])


def _prune_group(group, idx, slug, dry_run):
    """Draft a persistent-rules.md consolidation (prune / merge / add_rule) on a PRIVATE temp
    copy, validate it against the guardrails, and record the proposal for review. The live
    rules file is NEVER touched here - apply_prune() writes it only after an explicit Yes.
    Rules live outside git (athletes/ is gitignored and the repo is public), so this path
    uses no git branch/merge; the full proposed content is carried in the review record.
    Returns review id or None."""
    import tempfile, hashlib
    rid    = f"{date.today().isoformat()}-{idx}"
    action = (group.get("action") or "prune").lower()
    rules_p = _rules_path(slug)
    if not rules_p.exists():
        print(f"[bug-fixer] {rid}: no persistent-rules.md for {slug} - skipping {action}", file=sys.stderr)
        return None
    original  = rules_p.read_text()
    old_count = _count_rules(original)
    with tempfile.TemporaryDirectory(prefix=f"cc-prune-{rid}-") as td:
        work = Path(td) / "persistent-rules.md"
        work.write_text(original)
        prline = ("NEW RULE TO ADD (verbatim, keep the [perm] prefix): "
                  + (group.get("proposed_rule") or "")) if action == "add_rule" else ""
        prompt = PRUNE_PROMPT.format(title=group.get("title", ""), action=action,
                                     root_cause=group.get("root_cause", ""),
                                     plan=group.get("plan", ""), proposed_rule_line=prline)
        res = claude_call.run_claude(prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
                                     allowed_tools=PRUNE_TOOLS, cwd=td, timeout=600, label=f"prune:{rid}")
        summary  = ((res.stdout or "").strip().splitlines() or ["(consolidation attempted)"])[-1][:200]
        proposed = work.read_text()
    new_count = _count_rules(proposed)

    # Guardrails - a bad consolidation is discarded before it can reach a review card.
    if proposed.strip() == original.strip():
        print(f"[bug-fixer] {rid}: {action} produced no change - discarding", file=sys.stderr); return None
    if "# Persistent coaching rules" not in proposed:
        print(f"[bug-fixer] {rid}: header lost - discarding", file=sys.stderr); return None
    if action in ("prune", "merge"):
        if new_count >= old_count:
            print(f"[bug-fixer] {rid}: {action} did not reduce rule count "
                  f"({old_count}->{new_count}) - discarding", file=sys.stderr); return None
        if new_count < max(1, old_count // 2):
            print(f"[bug-fixer] {rid}: {action} would remove more than half the rules "
                  f"({old_count}->{new_count}) - discarding for manual review", file=sys.stderr); return None
    elif action == "add_rule":
        if new_count != old_count + 1:
            print(f"[bug-fixer] {rid}: add_rule changed rule count by !=1 "
                  f"({old_count}->{new_count}) - discarding", file=sys.stderr); return None
        if new_count > RULE_COUNT_CEILING:
            print(f"[bug-fixer] {rid}: add_rule would exceed the ceiling "
                  f"({new_count}>{RULE_COUNT_CEILING}) - consolidate first; discarding", file=sys.stderr); return None
    else:
        print(f"[bug-fixer] {rid}: unknown rule action {action!r} - discarding", file=sys.stderr); return None

    diff = "".join(difflib.unified_diff(
        original.splitlines(True), proposed.splitlines(True),
        fromfile="persistent-rules.md (current)", tofile="persistent-rules.md (proposed)", n=1))[:4000]
    stat = f"standing rules {old_count} -> {new_count} ({action})"
    review = {"id": rid, "branch": "", "slug": slug, "kind": "prune", "action": action,
              "title": group.get("title", ""), "entries": group.get("entries", []),
              "summary": summary, "stat": stat, "files": [str(rules_p)],
              "proposal": proposed, "base_sha": hashlib.sha256(original.encode()).hexdigest(),
              "old_count": old_count, "new_count": new_count,
              "status": "awaiting", "created": date.today().isoformat()}
    if dry_run:
        print(f"\n--- WOULD POST (PRUNE) [{rid}] {review['title']} ---\n{summary}\n{stat}\n{diff}")
        return rid
    reviews = _load_reviews(); reviews[rid] = review; _save_reviews(reviews)
    chat_id = json.loads((BASE / "config/athletes.json").read_text())[slug].get("chat_id", "")
    if chat_id:
        _tg_card(chat_id, review)
    return rid


def apply_prune(rid):
    """Apply an APPROVED rule consolidation to the live persistent-rules.md. This is the ONLY
    place the bug-fixer writes athlete data, and it runs only for an 'awaiting' prune review -
    i.e. after Jamie taps Yes. It backs up the current file first, and refuses to apply if the
    live rules changed since the consolidation was drafted (session-sync may have appended
    rules meanwhile) so an approval can never silently clobber newer rules."""
    import hashlib
    reviews = _load_reviews(); rv = reviews.get(rid)
    if not rv or rv.get("kind") != "prune":
        print(f"[bug-fixer] apply-prune: no prune review {rid}", file=sys.stderr); return
    if rv.get("status") != "awaiting":
        print(f"[bug-fixer] apply-prune {rid}: not awaiting (status={rv.get('status')})", file=sys.stderr); return
    slug    = rv.get("slug", "jamie")
    rules_p = _rules_path(slug)
    if not rules_p.exists():
        print(f"[bug-fixer] apply-prune {rid}: rules file missing", file=sys.stderr); return
    current = rules_p.read_text()
    if hashlib.sha256(current.encode()).hexdigest() != rv.get("base_sha"):
        print(f"[bug-fixer] apply-prune {rid}: rules changed since draft - refusing to apply. "
              f"Re-run the fixer to redraft.", file=sys.stderr); return
    proposal = rv.get("proposal")
    if not proposal:
        print(f"[bug-fixer] apply-prune {rid}: no stored proposal - cannot apply", file=sys.stderr); return
    backup = rules_p.with_suffix(f".bak-{rid}.md")
    backup.write_text(current)
    rules_p.write_text(proposal)
    rv["status"] = "applied"; _save_reviews(reviews)
    _bug_mark_feedback(slug, rv.get("entries", []), "resolved", commit_hash=f"prune:{rid}")
    print(f"[bug-fixer] apply-prune {rid}: applied ({rv.get('stat','')}); backup at {backup.name}")


def run_fix(slug, dry_run):
    plan_obj  = plan(slug)
    all_groups = plan_obj.get("groups", [])
    if not all_groups:
        print("No groups produced - nothing to fix.")
        return
    # Deterministic backstops (belt and braces on the planner):
    #  - recurrence: never re-draft a patch for a bug we already fixed once; route to a human.
    #  - conflict: never draft a rule that contradicts a confirmed preference.
    recurring = _hard_recurrences(slug)      # STRONG signal only; advisory hits go to the planner
    prefs     = _confirmed_preferences(slug)
    groups, blocked = [], []
    for g in all_groups:
        if g.get("verdict") != "fixable_now":
            continue
        gi = {i for i in g.get("entries", []) if isinstance(i, int)}
        if gi & recurring:
            g["verdict"] = "recurring"
            g.setdefault("prior_commit", "see feedback log")
            blocked.append((g, "recurring - routed to human, not drafted"))
            continue
        conflict = _rule_conflict(g, prefs)
        if conflict:
            g["conflicts_with"] = conflict
            g["verdict"] = "needs_human"
            blocked.append((g, f"conflicts with confirmed preference: {conflict[:80]}"))
            continue
        groups.append(g)
    for g, why in blocked:
        print(f"  [gated] {g.get('title')}: {why}")
    if not groups:
        print("No draftable groups after recurrence/conflict gates - nothing to fix.")
        return
    # Dedup: skip groups whose log entries already have a review (awaiting / merged /
    # dismissed) so the nightly run doesn't re-post the same bug every night. The
    # feedback log is append-only, so entry indices are stable across runs.
    if not dry_run:
        seen = {e for rv in _load_reviews().values() for e in rv.get("entries", [])}
        groups = [g for g in groups if not (set(g.get("entries", [])) & seen)]
        if not groups:
            print("All fixable groups already have reviews - nothing new.")
            return
    print(f"{len(groups)} fixable group(s){' (dry-run)' if dry_run else ''}.")
    for i, g in enumerate(groups):
        action = (g.get("action") or "code_fix").lower()
        if action in ("prune", "merge", "add_rule"):
            rid = _prune_group(g, i, slug, dry_run)
        else:
            rid = _fix_group(g, i, slug, dry_run)
        print(f"  {g.get('title')}: {'review ' + rid if rid else 'no change / discarded'}")


def repost_awaiting(slug):
    """Re-post review cards for every 'awaiting' review of this athlete. Use after a
    failed post run (e.g. the HTTP-400 Markdown bug) to deliver cards that never arrived.
    Idempotent: it only re-sends the card, it does not touch branches or review state."""
    reviews = _load_reviews()
    try:
        chat_id = json.loads((BASE / "config/athletes.json").read_text()).get(slug, {}).get("chat_id", "")
    except Exception:
        chat_id = ""
    if not chat_id:
        print(f"[bug-fixer] repost: no chat_id for {slug}", file=sys.stderr); return
    pending = [r for r in reviews.values()
               if r.get("status") == "awaiting" and r.get("slug", "jamie") == slug]
    if not pending:
        print("No awaiting reviews to repost."); return
    print(f"Reposting {len(pending)} awaiting card(s) for {slug}.")
    for rv in sorted(pending, key=lambda r: r.get("id", "")):
        _tg_card(chat_id, rv)
        print(f"  reposted {rv['id']}: {rv.get('title','')}")


def refix(rid, feedback):
    """Revise an existing awaiting review's branch per Jamie's Edit feedback, then re-post
    the card. Invoked by the bot when Jamie taps ✏️ Edit and sends his change."""
    reviews = _load_reviews(); rv = reviews.get(rid)
    if not rv:
        print(f"[bug-fixer] refix: no review {rid}", file=sys.stderr); return
    branch = rv["branch"]; wt = f"{WORKTREE_BASE}-{rid}-edit"
    if _git(["worktree", "add", wt, branch]).returncode != 0:
        print(f"[bug-fixer] refix {rid}: worktree add failed", file=sys.stderr); return
    try:
        prompt = (f"You are revising an in-progress bug fix on branch {branch} (its current diff is "
                  f"already committed). The athlete asked for this change:\n\n{feedback}\n\nApply it "
                  f"(Read/Edit/Write; Bash read-only), keep it minimal, do not commit. Output ONE "
                  f"plain-English line (<=200 chars) describing the OUTCOME of the revision for "
                  f"Jamie's review card: what now behaves differently. Do NOT name files, functions, "
                  f"variables, line numbers or diff stats.")
        res = claude_call.run_claude(prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
                                     allowed_tools=FIX_TOOLS, cwd=wt, timeout=900, label=f"refix:{rid}")
        summary = ((res.stdout or "").strip().splitlines() or ["(revised)"])[-1][:200]
        _git(["add", "-A"], cwd=wt)
        if _git(["diff", "--staged", "--quiet"], cwd=wt).returncode != 0:
            for n in _git(["diff", "--staged", "--name-only"], cwd=wt).stdout.split():
                if n.endswith(".py"):
                    try:
                        py_compile.compile(str(Path(wt) / n), doraise=True)
                    except Exception as e:
                        print(f"[bug-fixer] refix {rid}: {n} fails compile ({e})", file=sys.stderr); return
            _git(["commit", "-m", f"bugfix revise {rid}"], cwd=wt)
        rv["summary"] = summary
        rv["stat"]    = _git(["diff", "main", "--stat"], cwd=wt).stdout
        rv["files"]   = _git(["diff", "main", "--name-only"], cwd=wt).stdout.split()
        rv["status"]  = "awaiting"
        reviews[rid]  = rv; _save_reviews(reviews)
        chat_id = json.loads((BASE / "config/athletes.json").read_text())[rv.get("slug", "jamie")].get("chat_id", "")
        if chat_id:
            _tg_card(chat_id, rv)
    finally:
        _git(["worktree", "remove", "--force", wt])


def rules_lint_report(dry_run: bool = False):
    """Deterministic stale-rules guard: flag any prose coaching rule that WITHHOLDS a
    blueprint-required intensity slice (lib/rules_lint.py). Runs every nightly bug-fixer
    pass across ALL athletes so a methodology change can never silently leave a stale
    rule behind. Findings are LOUD - ops_log.alert (surfaces in the 21:30 coach ops
    digest) + a one-off coach Telegram when the finding set changes - and are surfaced
    in this report. Human review only: it never auto-edits a rule. A rule confirmed
    intentional is accepted in the athlete rules-lint-accepted.json (hash-keyed) and
    then stays quiet until its text changes."""
    import hashlib
    try:
        findings = rules_lint.lint_all(BASE)
    except Exception as e:
        print(f"[rules-lint] error: {e}", file=sys.stderr)
        return
    flat = [f for fs in findings.values() for f in fs]
    if not flat:
        print("[rules-lint] clean - no rule withholds a blueprint-required slice")
        return
    print(f"[rules-lint] {len(flat)} finding(s) across {len(findings)} athlete(s):")
    for f in flat:
        print(f"  WITHHOLD {f['slug']}/{f.get('file')}: {f['reason']} :: {f['rule'][:160]}")
        ops_log.alert("rules-lint", f"{f['reason']} :: {f['rule'][:160]}", athlete=f.get("slug", ""))
    state_p = Path.home() / "Library/Logs/ClaudeCoach" / "rules-lint-state.json"
    cur_sig = hashlib.sha256("|".join(sorted(f.get("hash", "") for f in flat)).encode()).hexdigest()
    try:
        prev = json.loads(state_p.read_text()).get("sig")
    except Exception:
        prev = None
    if cur_sig != prev and not dry_run:
        msg = (f"RULES-LINT: {len(flat)} rule(s) may withhold a blueprint-required intensity "
               "slice. Review, then either accept in the athlete rules-lint-accepted.json or "
               "time-box the rule with [expires:YYYY-MM-DD]:\n"
               + "\n".join(f"- {f['slug']}: {f['rule'][:120]}" for f in flat[:8]))
        try:
            subprocess.run([sys.executable, str(BASE / "telegram/notify.py"), "--no-history", msg], timeout=30)
        except Exception as e:
            print(f"[rules-lint] telegram send failed: {e}", file=sys.stderr)
        try:
            state_p.parent.mkdir(parents=True, exist_ok=True)
            state_p.write_text(json.dumps({"sig": cur_sig, "when": date.today().isoformat()}))
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", default="jamie")
    ap.add_argument("--json", action="store_true", help="print raw JSON plan")
    ap.add_argument("--fix", action="store_true", help="draft fixes on branches + post review cards")
    ap.add_argument("--dry-run", action="store_true", help="with --fix: build branches but never post or keep them")
    ap.add_argument("--refix", metavar="RID", help="revise an existing review per --feedback, then re-post")
    ap.add_argument("--feedback", default="", help="the revision instruction for --refix")
    ap.add_argument("--apply-prune", metavar="RID", dest="apply_prune",
                    help="apply an APPROVED rule consolidation to the live rules file (invoke on Yes for a prune review)")
    ap.add_argument("--repost", action="store_true", help="re-post cards for all awaiting reviews (e.g. after a failed post)")
    ap.add_argument("--reconcile", action="store_true", help="normalise feedback-log.json schema and back-fill resolution_commit from git history")
    args = ap.parse_args()
    if args.apply_prune:
        apply_prune(args.apply_prune)
    elif args.refix:
        refix(args.refix, args.feedback)
    elif args.repost:
        repost_awaiting(args.athlete)
    elif args.reconcile:
        reconcile(args.athlete)
    elif args.fix:
        rules_lint_report(args.dry_run)
        run_fix(args.athlete, args.dry_run)
    else:
        p = plan(args.athlete)
        print(json.dumps(p, indent=2) if args.json else _render(p))


if __name__ == "__main__":
    main()
