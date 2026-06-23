#!/usr/bin/env python3
"""
ClaudeCoach nightly bug-fixer — two-stage pipeline.

Stage 1 (default, read-only): reads the feedback/bug log, uses an agent to
(a) check the codebase + git history for what's already been fixed,
(b) consolidate open entries that share a root cause into work groups, and
(c) classify each group fixable_now / needs_human / already_resolved with a
plan. Outputs a structured plan (JSON).

Stage 2 (--fix / --refix): for each fixable_now group, drafts the fix on a
temporary git worktree branch, posts a Telegram review card with ✅ Yes /
❌ No / ✏️ Edit, and merges to main only on an explicit Yes reply.
--refix revises a draft after an Edit instruction without creating a new entry.

Run:  python3 ClaudeCoach/scripts/bug-fixer.py [--athlete jamie] [--json]
      python3 ClaudeCoach/scripts/bug-fixer.py --fix <group_id>
      python3 ClaudeCoach/scripts/bug-fixer.py --refix <group_id> "<instruction>"
Cron: 0 0 * * *  (midnight, Stage 1 only)
"""
import argparse, json, re, sys, subprocess, py_compile
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent.parent          # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/
sys.path.insert(0, str(BASE / "lib"))
import claude_call

# Read-only tools — the planner must NOT modify anything.
TOOLS = "Read,Bash"

# Stage 2 (fixer) constants.
FIX_TOOLS     = "Read,Write,Edit,Bash"
WORKTREE_BASE = "/tmp/cc-bugfix"
REVIEWS_FILE  = BASE / ".bug-reviews.json"     # gitignored review state (awaiting/merged/dismissed)
TG_CONFIG     = BASE / "telegram/config.json"


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

BE EFFICIENT AND TIME-BOUNDED. Do NOT read the whole codebase. Run `git log --oneline -60`
ONCE for recent-fix context, and use at most one or two targeted `git grep` / file reads
per OPEN issue to confirm status. Entries older than ~10 days are very likely already
resolved — spend a quick git grep to confirm, don't deep-dive. Put your effort into
consolidation and classification of the OPEN items, not exhaustive verification.

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


# ── Stage 2: fixer (draft on a worktree branch + review card) ─────────────────

FIX_PROMPT = """You are fixing ONE consolidated bug in the ClaudeCoach codebase. You are in a
fresh git worktree on a dedicated branch — make ONLY the minimal change for this bug. Do NOT
commit, push, deploy, or change anything unrelated. Do NOT edit athlete data under
ClaudeCoach/athletes/.

BUG: {title}
ROOT CAUSE: {root_cause}
PLAN: {plan}
LIKELY FILES: {files}

Implement the fix now (Read/Edit/Write, Bash for read-only inspection), tight and consistent
with the surrounding code. When done, output ONE line: a <=140-char summary of what you changed."""


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
    Uses HTML parse mode with escaped fields: the title/summary/stat come from the
    fixer agent and routinely contain underscores, backticks and asterisks that break
    Telegram's legacy Markdown (silent HTTP 400, so the card never arrives)."""
    import html, ssl, urllib.request
    try:
        token = json.loads(TG_CONFIG.read_text())["bot_token"]
        rid = review["id"]
        title   = html.escape(review.get("title", ""))
        summary = html.escape(review.get("summary", ""))
        stat    = html.escape(review.get("stat", "").strip())
        text = (f"🛠 <b>Bug fix ready</b>: {title}\n\n<i>{summary}</i>\n\n"
                f"<pre>{stat}</pre>\n\nMerge to live?")
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
    """Draft the fix for one group on a dedicated branch. Returns review id or None.
    Worktree is always removed; the branch persists only for a real (non-dry-run)
    change that compiles — otherwise the branch is deleted too. Never merges."""
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
        summary = ((res.stdout or "").strip().splitlines() or ["(fix attempted)"])[-1][:140]
        _git(["add", "-A"], cwd=wt)
        stat = _git(["diff", "--staged", "--stat"], cwd=wt).stdout
        if not stat.strip():
            print(f"[bug-fixer] {rid}: agent made no changes — discarding", file=sys.stderr)
            return None
        for n in _git(["diff", "--staged", "--name-only"], cwd=wt).stdout.split():
            if n.endswith(".py"):
                try:
                    py_compile.compile(str(Path(wt) / n), doraise=True)
                except Exception as e:
                    print(f"[bug-fixer] {rid}: {n} fails compile ({e}) — discarding", file=sys.stderr)
                    return None
        _git(["commit", "-m", f"bugfix: {group.get('title', '')}"], cwd=wt)
        review = {"id": rid, "branch": branch, "slug": slug, "title": group.get("title", ""),
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


def run_fix(slug, dry_run):
    groups = [g for g in plan(slug).get("groups", []) if g.get("verdict") == "fixable_now"]
    if not groups:
        print("No fixable_now groups — nothing to fix.")
        return
    # Dedup: skip groups whose log entries already have a review (awaiting / merged /
    # dismissed) so the nightly run doesn't re-post the same bug every night. The
    # feedback log is append-only, so entry indices are stable across runs.
    if not dry_run:
        seen = {e for rv in _load_reviews().values() for e in rv.get("entries", [])}
        groups = [g for g in groups if not (set(g.get("entries", [])) & seen)]
        if not groups:
            print("All fixable groups already have reviews — nothing new.")
            return
    print(f"{len(groups)} fixable group(s){' (dry-run)' if dry_run else ''}.")
    for i, g in enumerate(groups):
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
                  f"<=140-char line summarising the revision.")
        res = claude_call.run_claude(prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
                                     allowed_tools=FIX_TOOLS, cwd=wt, timeout=900, label=f"refix:{rid}")
        summary = ((res.stdout or "").strip().splitlines() or ["(revised)"])[-1][:140]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", default="jamie")
    ap.add_argument("--json", action="store_true", help="print raw JSON plan")
    ap.add_argument("--fix", action="store_true", help="draft fixes on branches + post review cards")
    ap.add_argument("--dry-run", action="store_true", help="with --fix: build branches but never post or keep them")
    ap.add_argument("--refix", metavar="RID", help="revise an existing review per --feedback, then re-post")
    ap.add_argument("--feedback", default="", help="the revision instruction for --refix")
    ap.add_argument("--repost", action="store_true", help="re-post cards for all awaiting reviews (e.g. after a failed post)")
    args = ap.parse_args()
    if args.refix:
        refix(args.refix, args.feedback)
    elif args.repost:
        repost_awaiting(args.athlete)
    elif args.fix:
        run_fix(args.athlete, args.dry_run)
    else:
        p = plan(args.athlete)
        print(json.dumps(p, indent=2) if args.json else _render(p))


if __name__ == "__main__":
    main()
