#!/usr/bin/env python3
"""
ClaudeCoach nightly bug-fixer — STAGE 1: triage / consolidate / plan (read-only).

Reads the whole feedback/bug log, uses an agent to (a) check the codebase + git
history for what's already been fixed, (b) consolidate open entries that share a
root cause into work groups, and (c) classify each group fixable_now / needs_human
/ already_resolved with a plan. Outputs a structured plan (JSON).

Stage 2 (separate, later): for each fixable_now group, draft the fix on a git
worktree branch, post a Telegram card with ✅ Yes / ❌ No / ✏️ Edit, and merge to
main only on an explicit Yes. NOTHING in this file fixes, writes, merges or deploys.

Run:  python3 ClaudeCoach/scripts/bug-fixer.py [--athlete jamie] [--json]
Cron (later): 0 0 * * *  (midnight)
"""
import argparse, json, re, sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent.parent          # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/
sys.path.insert(0, str(BASE / "lib"))
import claude_call

# Read-only tools — the planner must NOT modify anything.
TOOLS = "Read,Bash"


def _load_entries(slug: str):
    f = BASE / "athletes" / slug / "feedback-log.json"
    try:
        return json.loads(f.read_text())
    except Exception:
        return []


def _format_entries(entries):
    lines = []
    for i, e in enumerate(entries):
        lines.append(
            f"[{i}] {e.get('date','?')} | {e.get('type','?')}"
            f"{' | status=' + e['status'] if e.get('status') else ''}\n"
            f"    {(e.get('message') or '').strip()}"
        )
    return "\n".join(lines)


PLAN_PROMPT = """You are the ClaudeCoach nightly bug-triage PLANNER. ANALYSIS ONLY — \
do NOT edit, write, fix, commit or deploy anything. You have Read and Bash (use Bash \
only for read-only inspection: git log, git grep, cat, ls).

Below is the FULL bug/feedback log (oldest first). Some entries are old and were \
already fixed in earlier work; some are open; some are feature requests; some are \
notes. Your job, in order:

1. CONTEXT FIRST. For each entry, check whether it is ALREADY addressed in the \
   current codebase. Use `git log --oneline`, `git grep`, and read the relevant files \
   under ClaudeCoach/. Many older entries are already done. Mark those already_resolved \
   with one line of evidence (commit subject or the code that handles it).

2. CONSOLIDATE the entries that are still OPEN. Group ones that share a single root \
   cause into ONE work item — do not treat near-duplicate reports as separate fixes. \
   (E.g. the planning-algorithm and TSS-estimation entries may be facets of one \
   planning-engine issue.)

3. For each consolidated OPEN group, classify:
   - "fixable_now": a clear, bounded code change. Give a concrete plan and the files.
   - "needs_human": deep, ambiguous, or methodology-level (no safe mechanical fix). \
     Say why, and what decision is needed from Jamie.

OUTPUT: ONLY a JSON object wrapped in <plan></plan>, no other prose:
<plan>
{"groups":[
  {"title":"short title",
   "entries":[<indices from the log>],
   "verdict":"fixable_now|needs_human|already_resolved",
   "root_cause":"one or two sentences",
   "evidence":"for already_resolved: the commit/code that handles it; else ''",
   "plan":"for fixable_now: concrete steps + files; for needs_human: the decision needed; else ''",
   "files":["likely files to change, or []"]}
]}
</plan>

THE LOG:
{log}
"""


def plan(slug: str) -> dict:
    entries = _load_entries(slug)
    if not entries:
        return {"groups": [], "_note": "no log entries"}
    prompt = PLAN_PROMPT.replace("{log}", _format_entries(entries))
    result = claude_call.run_claude(
        prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
        allowed_tools=TOOLS, cwd=PROJECT_DIR, timeout=600, label=f"bugplan:{slug}",
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
    icon = {"fixable_now": "🛠", "needs_human": "🧑", "already_resolved": "✅"}
    lines = [f"BUG TRIAGE — {date.today().isoformat()} — {len(groups)} group(s)\n"]
    for g in groups:
        lines.append(f"{icon.get(g.get('verdict'),'•')} {g.get('title','(untitled)')}  "
                     f"[{g.get('verdict')}]  entries={g.get('entries')}")
        if g.get("root_cause"): lines.append(f"    root: {g['root_cause']}")
        if g.get("evidence"):   lines.append(f"    evidence: {g['evidence']}")
        if g.get("plan"):       lines.append(f"    plan: {g['plan']}")
        if g.get("files"):      lines.append(f"    files: {', '.join(g['files'])}")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", default="jamie")
    ap.add_argument("--json", action="store_true", help="print raw JSON plan")
    args = ap.parse_args()
    p = plan(args.athlete)
    print(json.dumps(p, indent=2) if args.json else _render(p))


if __name__ == "__main__":
    main()
