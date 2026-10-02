#!/usr/bin/env python3
"""Nightly rule tidy (Jamie, 2 Oct 2026: a 'smart' way to manage athlete rules, so the
system keeps them short rather than asking the athlete to read them all).

Two jobs, both per athlete, both safe to re-run:

  TIDY    A long rule is rewritten as the CURRENT instruction only. Its backstory (past
          dates, superseded values, evidence, how it came about) moves to
          athletes/<slug>/reference/rule-notes.md with the original, word for word. A
          second model call checks that every instruction still in force survived; only
          then is the line replaced. A locked-in ("confirmed <date>") rule keeps its
          confirmation so the live capture guard still protects it.
  DIGEST  Each rule gets a topic and a one-line plain-English summary for the athlete's
          Your rules page (lib/athlete_rules.py), stored in the rule registry against
          the rule's current wording.

Why a new pass rather than the bug-fixer prune: that prune must keep every number and
reduce the rule COUNT, so a rule full of dated backstory can never shrink and every run
ended "no change - discarded" (28 Sep to 2 Oct 2026), while Jamie's rules sat at about
twice the surface budget. Here the archive is what makes shortening loss-free.

    python3 lib/rule_tidy.py --athlete jamie [--dry-run] [--max 12] [--no-digest]
    python3 lib/rule_tidy.py --all
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))
BASE = LIB.parent                                         # ClaudeCoach/

import athlete_rules as ar                               # noqa: E402

MAX_PER_RUN = 12              # bounded nightly work per athlete (the rest wait a night)
# No word limits (Jamie, 2 Oct 2026: "it could be 120 words but information dense, or 60
# words and fluffy - it's about information density, not arbitrary counts"). Every rule
# is reviewed ONCE per wording (registry tidy_hash); the model removes only what is not
# instruction, and a rule that is already all instruction is left exactly as it is.

_CONFIRMED_DATE = re.compile(
    r"\bconfirmed\b(?:\s+by\s+[A-Za-z]+)?\s*[,:]?\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}", re.I)

TIDY_PROMPT = """You are tidying ONE standing coaching rule for an athlete. It is sent with
every message the coach writes, so it must state only what applies NOW.

Make it information-dense - this is about density, not length:
- keep every instruction: every limit, number, day, product, threshold, method step,
  preference and "never do X" that still applies today, however long that is
- remove only what is NOT instruction: past-event dates, superseded values ("was X, now Y"
  becomes just Y), quotes, evidence, the story of how it came about, repetition
- plain and direct; do not add anything new
- if it is already all instruction, return it unchanged

Put everything you removed into "backstory". It is kept in a notes file, so nothing is lost.

Rule:
{rule}

Reply with JSON only: {{"rule": "...", "backstory": "..."}}"""

CHECK_PROMPT = """Original coaching rule:
{orig}

Shortened rule:
{new}

Did the shortened rule lose any INSTRUCTION - something the coach must or must not do,
or a limit, number, day, product or preference it must apply - that still applies today?
Examples, evidence, rationale, costs and history may be dropped; do not list those. Only
list an instruction that is now missing or has changed. Reply with JSON only:
{{"ok": true or false, "missing": ["..."]}}"""

DIGEST_PROMPT = """For each coaching rule below, give a topic and a one-line summary the
athlete will read on a "Your rules" page.

topic: one of schedule (days, hours, availability), training (sessions, intensity, tests),
fuelling (food, drink, carbs), health (injuries, sleep, weight, illness), data (devices,
apps, uploads), style (how the coach writes or talks to them), other.
summary: at most 14 words, plain English, addressed to the athlete ("Weekday rides: about
3 hours, one ~2h and one ~1h, no fixed days"). No dates of past events.

Rules:
{rules}

Reply with JSON only: [{{"id": "...", "topic": "...", "summary": "..."}}]"""


def _llm(prompt: str, label: str) -> str:
    import claude_call
    r = claude_call.run_claude(prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
                               timeout=240, label=f"rule-tidy:{label}")
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _json(text: str):
    """The first JSON object or array in a model reply, or None (whichever opens first)."""
    pairs = sorted((("{", "}"), ("[", "]")),
                   key=lambda oc: text.find(oc[0]) if text.find(oc[0]) != -1 else len(text))
    for opener, closer in pairs:
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except ValueError:
                continue
    return None


def _confirmation(text: str) -> str:
    m = list(_CONFIRMED_DATE.finditer(text or ""))
    return m[-1].group(0) if m else ""


def tidy_one(text: str, label: str, llm=_llm) -> dict | None:
    """{"rule", "backstory"} for one rule's text (tag stripped), or None when it could not
    be shortened safely."""
    got = _json(llm(TIDY_PROMPT.format(rule=text), label) or "")
    if not isinstance(got, dict) or not str(got.get("rule") or "").strip():
        return None
    new = re.sub(r"\s+", " ", str(got["rule"]).strip())
    conf = _confirmation(text)
    if conf and not _CONFIRMED_DATE.search(new):
        new = new.rstrip(". ") + f". ({conf})"           # stays locked for the capture guard
    if len(new) >= len(text) or not str(got.get("backstory") or "").strip():
        return None                                      # nothing that wasn't instruction
    check = _json(llm(CHECK_PROMPT.format(orig=text, new=new), label) or "")
    if not isinstance(check, dict) or check.get("ok") is not True:
        return None
    return {"rule": new, "backstory": str(got.get("backstory") or "").strip()}


def tidy(slug: str, base: Path | None = None, max_rules: int = MAX_PER_RUN,
         dry_run: bool = False, llm=_llm) -> dict:
    """Review up to `max_rules` rules not yet reviewed in their current wording (longest
    first, where the most non-instruction usually sits)."""
    import rule_registry as rr
    base = base or BASE
    rules = ar._active(slug, base)
    long_ = sorted(((rid, e, raw) for rid, e, raw in rules
                    if e.get("tidy_hash") != e.get("hash")),
                   key=lambda x: -len(x[2]))[:max_rules]
    done, skipped = [], []
    for rid, e, raw in long_:
        tag, _exp, text = ar.split_tag(raw)
        res = tidy_one(text, slug, llm=llm)
        if not res:
            skipped.append(rid)
            continue
        done.append({"id": rid, "raw": raw, "new": f"{tag} {res['rule']}",
                     "backstory": res["backstory"], "before_words": len(text.split()),
                     "after_words": len(res["rule"].split())})
    out = {"athlete": slug, "tidied": [{k: d[k] for k in ("id", "before_words", "after_words")}
                                       for d in done],
           "skipped": skipped, "dry_run": dry_run}
    if dry_run:
        return out
    # Reviewed and left as it is: not asked again until its wording changes.
    if skipped:
        reg = rr.load_registry(base, slug)
        for rid in skipped:
            if rid in reg["rules"]:
                reg["rules"][rid]["tidy_hash"] = reg["rules"][rid].get("hash")
        rr.registry_path(base, slug).write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n",
                                                encoding="utf-8")
    if not done:
        return out
    ar.archive(slug, [{"reason": "tidied", "id": d["id"], "rule": d["raw"], "now": d["new"],
                       "backstory": d["backstory"]} for d in done], base)
    p = ar._rules_path(slug, base)
    shutil.copy2(p, p.with_name(p.name + f".bak-rule-tidy-{datetime.now():%Y%m%d%H%M%S}"))
    swap = {d["raw"]: d["new"] for d in done}
    lines = [swap.get(l.strip(), l) for l in p.read_text(encoding="utf-8").splitlines()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Each tidied rule keeps its ID (and its topic / check dates) under the new wording.
    reg = rr.load_registry(base, slug)
    for d in done:
        if d["id"] in reg["rules"]:
            h = rr.content_hash(d["new"])
            reg["rules"][d["id"]].update({"hash": h, "tidy_hash": h,
                                          "fingerprint": rr.fingerprint(d["new"]),
                                          "summary": None, "summary_hash": None,
                                          "tidied": date.today().isoformat()})
    rr.registry_path(base, slug).write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n",
                                            encoding="utf-8")
    rr.sync(base, slug, write=True)
    return out


def digest(slug: str, base: Path | None = None, llm=_llm, batch: int = 25) -> int:
    """Topic + one-line summary for every rule whose wording has no current summary."""
    import rule_registry as rr
    base = base or BASE
    todo = [(rid, e, raw) for rid, e, raw in ar._active(slug, base)
            if e.get("summary_hash") != e.get("hash")]
    if not todo:
        return 0
    reg = rr.load_registry(base, slug)
    n = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        listing = "\n".join(f"- id {rid}: {ar.split_tag(raw)[2]}" for rid, _e, raw in chunk)
        got = _json(llm(DIGEST_PROMPT.format(rules=listing), slug) or "")
        if not isinstance(got, list):
            continue
        by_id = {str(g.get("id")): g for g in got if isinstance(g, dict)}
        for rid, e, raw in chunk:
            g = by_id.get(rid)
            if not g or not str(g.get("summary") or "").strip():
                continue
            topic = g.get("topic") if g.get("topic") in ar.TOPIC_KEYS else ar.guess_topic(raw)
            reg["rules"][rid].update({"topic": topic,
                                      "summary": re.sub(r"\s+", " ", str(g["summary"]).strip()),
                                      "summary_hash": reg["rules"][rid].get("hash")})
            n += 1
    rr.registry_path(base, slug).write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n",
                                            encoding="utf-8")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=MAX_PER_RUN)
    ap.add_argument("--no-digest", action="store_true")
    a = ap.parse_args()
    if a.all:
        cfg = json.loads((BASE / "config" / "athletes.json").read_text())
        slugs = [s for s, v in cfg.items() if isinstance(v, dict) and v.get("active")] + ["_shared"]
    elif a.athlete:
        slugs = [a.athlete]
    else:
        ap.error("--athlete or --all")
    for slug in slugs:
        if not ar._rules_path(slug).exists():
            continue
        t = tidy(slug, max_rules=a.max, dry_run=a.dry_run)
        msg = (f"[rule-tidy] {datetime.now():%Y-%m-%d %H:%M} {slug}: tidied {len(t['tidied'])} "
               f"({sum(x['before_words'] for x in t['tidied'])} -> "
               f"{sum(x['after_words'] for x in t['tidied'])} words), "
               f"left {len(t['skipped'])} as they were")
        if slug != "_shared" and not a.no_digest and not a.dry_run:
            msg += f", summarised {digest(slug)}"
        print(msg + (" (dry run)" if a.dry_run else ""), flush=True)


if __name__ == "__main__":
    main()
