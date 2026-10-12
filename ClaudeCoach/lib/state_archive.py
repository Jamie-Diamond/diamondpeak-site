#!/usr/bin/env python3
"""Keep current-state.md short: move old dated log entries into an archive file, nightly
(Jamie, 12 Oct 2026: reduce token counts without losing anything).

Every job that reads current-state.md (morning card, session sync, daily prescription,
watchdog, chat when it opens it) paid for its whole history: Jamie's file was ~74k
tokens, two-thirds of it watchdog-flag entries back to May. Nothing in code reads log
entries older than a week (offplan_log: 7 days; morning card flags: 3 days; the chat
prefetch reads only the top section), so they move to
athletes/<slug>/reference/current-state-archive.md, word for word, under their section.

Only LOG sections are touched: a section where at least LOG_SHARE of its non-empty lines
start with an ISO date (Watchdog flags, Off-plan in last 7 days, Plan changes, Illness /
Health, Heat Acclimatisation...). Standing sections (injury protocols, goals, open actions,
the recent-context summary) have no dated lines and are never touched. A dated entry older
than KEEP_DAYS moves with its indented continuation lines. The file is backed up first.

    python3 lib/state_archive.py --all [--dry-run]       # cron 03:40
"""
from __future__ import annotations

import argparse
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
KEEP_DAYS = 30
LOG_SHARE = 0.7
_DATED = re.compile(r"^\s*(?:[-*|]\s*)?(?:\*\*)?\s*(\d{4}-\d{2}-\d{2})")
POINTER = "_(Entries older than {n} days are in reference/current-state-archive.md.)_"


def _sections(lines: list) -> list:
    """[(start, end)] line ranges of each section body (after its heading), plus the top."""
    heads = [i for i, l in enumerate(lines) if l.startswith("#")]
    bounds, prev = [], -1
    for h in heads + [len(lines)]:
        bounds.append((prev + 1, h))
        prev = h
    return bounds


def split(md: str, today: date, keep_days: int = KEEP_DAYS) -> tuple[str, list]:
    """(the file with old log entries removed, [(section heading, [moved lines])])."""
    lines = md.splitlines()
    cut = (today - timedelta(days=keep_days)).isoformat()
    drop, moved = set(), []
    for start, end in _sections(lines):
        body = list(range(start, end))
        filled = [i for i in body if lines[i].strip() and not lines[i].startswith("_(")]
        dated = [i for i in filled if _DATED.match(lines[i])]
        if len(dated) < 3 or len(dated) < LOG_SHARE * len(filled):
            continue                                    # a standing section: untouched
        head = lines[start - 1] if start > 0 else "(top)"
        out, moving = [], False
        for i in body:
            m = _DATED.match(lines[i])
            if m:
                moving = m.group(1) < cut
            elif not (lines[i].startswith(("  ", "\t")) and lines[i].strip()):
                moving = False
            if moving:
                drop.add(i)
                out.append(lines[i])
        if out:
            moved.append((head, out))
    if not moved:
        return md, []
    kept, pointed = [], set()
    for i, l in enumerate(lines):
        if i in drop:
            continue
        kept.append(l)
        if l.startswith("#") and l in {h for h, _ in moved} and l not in pointed:
            pointed.add(l)
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if not nxt.startswith("_(Entries older than"):
                kept.append(POINTER.format(n=KEEP_DAYS))
    return "\n".join(kept) + ("\n" if md.endswith("\n") else ""), moved


def archive(slug: str, base: Path | None = None, today: date | None = None,
            dry_run: bool = False) -> int:
    """Move old log entries for one athlete. Returns the number of lines moved."""
    base = base or BASE
    p = base / "athletes" / slug / "current-state.md"
    if not p.exists():
        return 0
    md = p.read_text(encoding="utf-8")
    new, moved = split(md, today or date.today())
    n = sum(len(ls) for _, ls in moved)
    if not n or dry_run:
        return n
    arc = base / "athletes" / slug / "reference" / "current-state-archive.md"
    arc.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = [f"\n# Archived {stamp} (older than {KEEP_DAYS} days)"]
    for head, ls in moved:
        block += ["", head] + ls
    if not arc.exists():
        block.insert(0, f"# current-state.md archive for {slug}\n"
                        "Old dated log entries moved out of current-state.md by "
                        "lib/state_archive.py, word for word. Newest batches at the end.")
    shutil.copy2(p, p.with_name(p.name + f".bak-archive-{datetime.now():%Y%m%d%H%M%S}"))
    if p.read_text(encoding="utf-8") != md:          # written meanwhile: try tomorrow
        return 0
    with open(arc, "a", encoding="utf-8") as fh:
        fh.write("\n".join(block) + "\n")
    p.write_text(new, encoding="utf-8")
    return n


def main():
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    slugs = [a.athlete] if a.athlete else [
        s for s, v in json.loads((BASE / "config" / "athletes.json").read_text()).items()
        if isinstance(v, dict)]
    for s in slugs:
        before = (BASE / "athletes" / s / "current-state.md")
        size = before.stat().st_size if before.exists() else 0
        n = archive(s, dry_run=a.dry_run)
        after = before.stat().st_size if before.exists() else 0
        print(f"[state-archive] {s}: moved {n} lines"
              + ("" if a.dry_run else f", ~{size // 4000}k -> ~{after // 4000}k tokens")
              + (" (dry run)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
