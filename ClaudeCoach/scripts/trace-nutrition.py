#!/usr/bin/env python3
"""trace-nutrition.py - follow one food string through every stage of the pipeline.

    python3 scripts/trace-nutrition.py "half a bag of M&S nut collection, 75g pack"
    python3 scripts/trace-nutrition.py --live "collagen capsules"
    python3 scripts/trace-nutrition.py --day 2026-08-10 --entry 3

WHY THIS EXISTS
Four bugs on 10 Aug 2026 were the same shape: a value computed at one stage and then lost
or transformed on its way to the next, invisibly.

  - the supplement intent routed correctly, then the item went through the food ladder
    anyway and matched a collagen protein BAR
  - `last_g_hr` was added to the fuelling ramp and no caller passed it
  - an optional ladder rung was enabled and then ignored, because resolve walked a
    constant instead of what was supplied
  - the matched species SCORE was computed and discarded on write, so refined
    derivatives were read back as whole plants and a 7-day count read 40

None of those raise. Each produced a plausible number, which is why they survived. They
would all have been obvious in one trace, because a trace shows the value at every hand-off
rather than only at the end.

It also answers the question I kept getting wrong by theorising: WHERE DID THIS NUMBER COME
FROM. `--day/--entry` prints the provenance of something already logged, which is what I
should have run before explaining two identical 106 kcal figures as a units error when the
entry was named "Chicken, breast, skinless, raw".

Offline by default: rungs are stubbed unless --live is given, so it is safe to run anywhere
and fast enough to use while thinking.
"""

import argparse
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

import nutrition_engine as NE       # noqa: E402
import nutrition_nlu as NLU         # noqa: E402
import nutrition_resolve as NR      # noqa: E402
import plants as PL                 # noqa: E402
from nutrition_store import NutritionStore  # noqa: E402

W, R, G, Y, D = "\033[97m", "\033[91m", "\033[92m", "\033[93m", "\033[0m"


def head(n, title):
    print(f"\n{W}{n}. {title}{D}\n" + "-" * 72)


def kv(k, v, colour=""):
    print(f"   {k:<26} {colour}{v}{D}")


def load_bot():
    spec = importlib.util.spec_from_file_location(
        "nb", BASE / "telegram" / "nutrition_bot.py")
    nb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nb)
    return nb


def trace_text(text: str, live: bool, day: date):
    table = PL.SpeciesTable()
    cofid = NR.CofidTable()

    head(1, "INTENT (before anything is resolved)")
    fast = NLU.fast_intent(text, has_pending=False)
    kv("fast path", fast["intent"] if fast else "none, needs the model")
    if fast and fast.get("intent") == "secret":
        kv("STOPPED", "credential-shaped, never logged or sent anywhere", R)
        return
    kv("looks like eating", NLU.looks_like_eating(text))
    kv("looks like a supplement", NLU.looks_like_supplement(text))
    kv("dose in mg", NLU.tiny_dose_mg(text))
    kv("looks like a weight", NLU.looks_like_weight(text))
    kv("looks like a barcode", NLU.looks_like_barcode(text))

    head(2, "INTERPRETATION (what is it, how should we search)")
    hint = {}
    if live:
        nb = load_bot()
        plan = NLU.interpret(text, nb.CLAUDE_BIN, nb.LLM_MODEL, log=lambda *a: None)
        hint = (plan or {}).get("items", [{}])[0] if plan else {}
    if not hint:
        # Offline stand-in, so the shape of the rest of the trace is still visible.
        hint = {"canonical_name": text, "form": "other", "category": "",
                "is_supplement": NLU.looks_like_supplement(text),
                "expect_macros": NLU.tiny_dose_mg(text) is None,
                "portion_g": None, "search_terms": [text]}
        kv("source", "OFFLINE STUB, pass --live for the real interpretation", Y)
    for k in ("canonical_name", "brand", "form", "category", "is_supplement",
              "expect_macros", "portion_g", "dose_mg", "search_terms"):
        kv(k, hint.get(k))

    if hint.get("is_supplement") or not hint.get("expect_macros"):
        head(3, "LADDER")
        kv("SKIPPED", "supplements record a dose and are never searched", G)
        kv("would store", f"dose only, no macros, no species")
        return

    head(3, f"LADDER (searching {hint.get('search_terms')})")
    fetchers = {}
    if live:
        nb = load_bot()
        fetchers = nb.build_fetchers(nb.load_config())
        deep = nb.make_deep_fetch(log=lambda *a: None)
        fetchers[NR.Rung.WEB] = lambda q, p, _h=hint, _d=deep: _d(q, p, hint=_h)
    else:
        kv("mode", "OFFLINE: only CoFID is real, web and llm are absent", Y)
    kv("will walk", " -> ".join(NR.effective_ladder(fetchers)))
    item = NR.resolve(hint.get("canonical_name") or text, day=day, store=None,
                      table=table, portion_g=hint.get("portion_g"),
                      fetchers=fetchers, cofid=cofid, hint=hint,
                      queries=hint.get("search_terms"))
    print()
    for a in item["attempts"]:
        col = {"hit": G, "error": R, "wrong_form": Y, "skipped": Y,
               "needs_portion": Y}.get(a["outcome"], "")
        print(f"   {col}{a['rung']:<16} {a['outcome']:<16}{D} {a.get('detail', '')[:34]}")

    head(4, "RESOLVED ITEM")
    kv("resolved_name", item.get("resolved_name"))
    kv("rung / confidence", f"{item.get('source_rung')} / {item.get('confidence')}",
       G if item.get("confidence") == "label" else Y)
    kv("degraded", item.get("degraded"), R if item.get("degraded") else "")
    kv("needs input", item.get("needs_input"), Y if item.get("needs_input") else "")
    for f in NR.MACRO_FIELDS:
        kv(f, item.get(f))
    kv("source_url", (item.get("source_url") or "")[:60])

    head(5, "SPECIES (the count is only as honest as these)")
    kv("tagged from", item.get("species_from"))
    sp = item.get("species") or []
    if not sp:
        kv("species", "none", Y)
    for s in sp:
        sid = s["id"] if isinstance(s, dict) else s
        score = s.get("score") if isinstance(s, dict) else None
        meta = table.species.get(sid, {})
        counts = (score is None and meta.get("score", 0) > 0) or (score or 0) > 0
        kv(meta.get("canonical", sid),
           f"score {score if score is not None else meta.get('score')}"
           + ("  counts" if counts else "  ZERO, refined derivative"),
           G if counts else "")
    if item.get("species_unmatched"):
        kv("unmatched text", item["species_unmatched"][:50], Y)

    head(6, "WHAT WOULD BE STORED")
    kv("in_session", bool(hint.get("in_session")))
    kv("ingredients kept", bool(item.get("ingredients")))
    kv("plants claimed", hint.get("plants_claimed"))
    print(f"\n   {Y}Run with --live for the real interpretation and web lookup.{D}"
          if not live else "")


def trace_entry(slug: str, day_iso: str, index: int):
    """Provenance of something already logged. The question I kept answering by guessing."""
    store = NutritionStore(BASE / "athletes" / slug)
    table = PL.SpeciesTable()
    rec = store.get_day(day_iso)
    entries = rec.get("entries") or []
    if not entries:
        print(f"no entries on {day_iso}")
        return
    head(1, f"{day_iso}: {len(entries)} entries")
    for i, e in enumerate(entries):
        mark = ">" if (index is None or i == index) else " "
        print(f" {mark} [{i}] {e.get('resolved_name', '')[:44]:<44} "
              f"{e.get('kcal')} kcal  {e.get('confidence')}/{e.get('source_rung')}")
    if index is None:
        return
    e = entries[index]
    head(2, "PROVENANCE")
    for k in ("raw_text", "resolved_name", "confidence", "source_rung", "source_url",
              "resolved_at", "portion_g", "logged_at", "in_session", "plants_claimed"):
        kv(k, e.get(k))
    head(3, "MACROS")
    for f in ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g", "dietary_sodium_mg"):
        kv(f, e.get(f))
    head(4, "SPECIES")
    for s in (e.get("species") or []):
        sid = s["id"] if isinstance(s, dict) else s
        score = s.get("score") if isinstance(s, dict) else None
        meta = table.species.get(sid, {})
        stored = score is not None
        kv(meta.get("canonical", sid),
           f"score {score if stored else meta.get('score')}"
           + ("" if stored else "  NO STORED SCORE, category default used"),
           "" if stored else Y)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("text", nargs="?", help="the food string to trace")
    ap.add_argument("--live", action="store_true",
                    help="use the real interpreter and web lookup")
    ap.add_argument("--athlete", default="jamie")
    ap.add_argument("--day", help="trace an already-logged day instead")
    ap.add_argument("--entry", type=int, help="index within that day")
    a = ap.parse_args(argv)
    if a.day:
        trace_entry(a.athlete, a.day, a.entry)
    elif a.text:
        trace_text(a.text, a.live, date.today())
    else:
        ap.error("give a food string, or --day to inspect a logged day")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
