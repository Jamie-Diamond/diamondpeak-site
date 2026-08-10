"""restaurants.py - nutrition published by a restaurant CHAIN, read from the source.

WHY THIS RUNG EXISTS
Deliveroo is a regular entry, and until this existed a restaurant order could only ever
be an LLM estimate. On Jamie's Wagamama order those estimates were wrong in both
directions and by a lot:

    gochujang salmon rice bowl   estimated 1080 kcal   published  786 kcal
    edamame, chilli + garlic     estimated  150 kcal   published  287 kcal

The rule for this ladder is that a rung must beat the model simply googling it, and a
plain search cannot get here: the chains' own menu pages are JavaScript-rendered, so
WebFetch sees a cookie banner and the "adults need around 2,000 kcal a day" footnote,
and an open search lands on SEO copies of the menu with no provenance.

What DOES work is the data platform the chains publish through. Wagamama's allergen and
nutrition matrix is served as static HTML with a full per-dish breakdown - energy,
protein, carb, sugars, fat, saturates, sodium, salt and fibre - so it can be parsed
deterministically, with no model in the loop and no scraping of a rendered page.

TWO SELF-CHECKS, BECAUSE A MIS-MAPPED COLUMN IS THE FAILURE THAT LOOKS FINE
Reading a table by column POSITION is how a fat figure gets logged as carbohydrate: the
numbers stay plausible, nothing raises, and the day is quietly wrong. So every row must
pass two identities that pin the mapping independently of the header:

  1. protein x 4 + carb x 4 + fat x 9 must account for the stated energy
  2. salt must equal sodium x 2.5

A row failing either is DROPPED, not corrected. The parse also refuses to start unless
the expected header order is actually present in the document.
"""

import json
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CONFIG = Path(__file__).resolve().parent.parent / "config" / "restaurants.json"
CACHE_TTL_S = 7 * 24 * 3600          # matrices change with the menu, a few times a year
FETCH_TIMEOUT_S = 90
USER_AGENT = "Mozilla/5.0 (compatible; ClaudeCoach nutrition)"

# The nutrient columns, in the order the matrix prints them. Mapped by NAME from the
# header below, never by bare position, and then verified per row.
FIELDS = ["kcal", "kj", "protein_g", "carb_g", "sugars_g", "fat_g", "saturates_g",
          "sodium_g", "salt_g", "fibre_g"]

HEADER_ORDER = ["energy (kcal)", "energy (kj)", "protein (g)", "carb (g)",
                "of which sugars (g)", "fat (g)", "sat fat (g)", "sodium (g)",
                "salt (g)", "fibre (g)"]

_NUM = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_TAG = re.compile(r"<[^>]+>")

# Words that describe how a dish was CHANGED. A change the published row does not include
# has to be reported, because silently logging the standard dish understates a double
# portion by hundreds of kcal and looks exactly like a correct answer.
_MODIFIER = re.compile(
    r"\b(extra|double|added|add|extra-large|large|no|without|swap(?:ped)?|instead of|"
    r"upgrade[d]? to|with extra)\b\s+([a-z][a-z' ]{2,28})", re.I)

# Not identifying words for dish matching. Deliberately short: a dish name is mostly
# content words, and over-stripping would let two different dishes look identical.
# Verbs that ADD a published extra. A removal ("no egg") cannot be subtracted without
# inventing the egg's contribution, so those are reported rather than guessed at.
_ADDITIVE = {"extra", "double", "added", "add", "with extra", "upgrade to",
             "upgraded to"}

_STOP = {"the", "and", "with", "of", "a", "an", "in", "on", "or", "your", "our",
         "served", "topped", "side", "sides", "new", "vg", "ve", "gf", "may", "contain",
         "contains", "small", "bones", "g", "ml", "kcal"}


def _tokens(text: str) -> set:
    out = set()
    for w in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(w) >= 2 and w not in _STOP and not w.isdigit():
            out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def load_registry(path: Path = None) -> dict:
    path = Path(path or CONFIG)
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("vendors", {})


def find_vendor(name: str, registry: dict = None) -> tuple:
    """Match a vendor name from a delivery app to a registry entry.

    Returns (key, entry) or (None, None). Deliveroo writes "Wagamama - Camden" and
    "wagamama (halal)", so this matches on containment either way rather than equality."""
    registry = registry if registry is not None else load_registry()
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).strip()
    if not n:
        return None, None
    best = (0, None, None)
    for key, entry in registry.items():
        for alias in [key] + list(entry.get("aliases") or []):
            a = alias.lower()
            if a and (a in n or n in a):
                # Longest alias wins, so "pizza express" does not lose to "pizza".
                if len(a) > best[0]:
                    best = (len(a), key, entry)
    return best[1], best[2]


def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as fh:
        raw = fh.read()
        if (fh.headers.get("Content-Encoding") or "").lower() == "gzip":
            import gzip
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def header_present(html: str) -> bool:
    """The expected column order, proven present rather than assumed.

    If a platform reorders its columns this returns False and the whole parse refuses,
    which is the point: no output at all beats a plausible mis-mapping."""
    flat = re.sub(r"\s+", " ", _TAG.sub(" ", html)).lower()
    pattern = ".{0,400}?".join(re.escape(h) for h in HEADER_ORDER)
    return re.search(pattern, flat, re.S) is not None


def _row_is_consistent(row: dict) -> bool:
    """The two identities that pin the column mapping. See the module docstring."""
    kcal = row.get("kcal") or 0
    calc = (row.get("protein_g") or 0) * 4 + (row.get("carb_g") or 0) * 4 \
        + (row.get("fat_g") or 0) * 9
    # 15%, set by measurement rather than taste: it drops NONE of the 161 rows on the
    # real Wagamama matrix, and it does catch a carb/fat swap, which lands ~18% out
    # because fat carries 9 kcal/g against carbohydrate's 4. At 25% that swap passed -
    # the tolerance has to be tighter than the error it exists to catch.
    if kcal and abs(calc - kcal) > max(60.0, 0.15 * kcal):
        return False
    sodium, salt = row.get("sodium_g"), row.get("salt_g")
    if sodium and salt is not None:
        if abs(salt - sodium * 2.5) > max(0.2, 0.12 * max(salt, 0.1)):
            return False
    return True


def parse_tenkites(html: str) -> list:
    """Dish rows from a tenkites matrix.

    Anchored on data-calories, an explicitly NAMED kcal figure, and the numeric run that
    follows must START with that same figure. That is what makes the mapping
    self-checking instead of trusted - a run that does not begin where we know it should
    is misaligned, and the row is dropped."""
    if not header_present(html):
        return []
    rows, seen = [], set()
    for m in re.finditer(r'data-calories="(\d+)"', html):
        kcal = float(m.group(1))
        tail = html[m.end():m.end() + 60000]
        name = ""
        for chunk in _TAG.split(tail[:4000]):
            c = chunk.strip()
            if len(c) > 2 and not _NUM.match(c):
                name = re.sub(r"\s+", " ", c)
                break
        if not name:
            continue
        nums = []
        for chunk in _TAG.split(tail):
            c = chunk.strip().replace(",", "")
            if _NUM.match(c):
                nums.append(float(c))
                if len(nums) == 1 and abs(nums[0] - kcal) > 0.5:
                    nums = []               # not this dish's run yet
                elif len(nums) == len(FIELDS):
                    break
        if len(nums) != len(FIELDS):
            continue
        row = dict(zip(FIELDS, nums))
        if not _row_is_consistent(row):
            continue
        # "steamed brown rice | 250g" states its own weight. Keep it: it is the only
        # gram figure a restaurant menu ever gives.
        portion_g = None
        pm = re.search(r"\|\s*(\d{2,4})\s*g\b", name)
        if pm:
            portion_g = float(pm.group(1))
        clean = re.sub(r"\s*\|.*$", "", name)
        clean = re.sub(r"\([^)]*\)", "", clean).strip(" -,")
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        rows.append({"name": clean, "raw_name": name, "portion_g": portion_g, **row})
    return rows


PARSERS = {"tenkites": parse_tenkites}


def load_menu(vendor_key: str, entry: dict, cache_dir: Path, now=None) -> dict:
    """Parsed rows for a vendor, cached. A cache past its TTL is a MISS, not a warning -
    chains reformulate, and label confidence on a stale figure is the worst of both."""
    now = now or time.time()
    cache_dir = Path(cache_dir)
    cache = cache_dir / f"{vendor_key}.json"
    if cache.exists():
        try:
            got = json.loads(cache.read_text())
            if now - got.get("fetched_at", 0) < CACHE_TTL_S and got.get("rows"):
                return got
        except (json.JSONDecodeError, OSError):
            pass
    parser = PARSERS.get(entry.get("platform"))
    url = entry.get("nutrition_url")
    if not (parser and url):
        return {"rows": [], "error": "no parser or url for this vendor"}
    html = _download(url)
    rows = parser(html)
    out = {"vendor": vendor_key, "source_url": url, "fetched_at": now, "rows": rows}
    if not rows:
        # No write on an empty parse: caching a failure would hide the outage for a week.
        out["error"] = "matrix fetched but nothing parsed (header changed?)"
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


def match_dish(rows: list, query: str) -> dict | None:
    """The published row for an ordered dish, or None.

    Coverage, then breadth. The row's own words must be largely present in the order line
    - so "steamed brown rice" cannot answer a salmon bowl - and where two rows are both
    fully covered the one matching MORE of the order wins, so "brown rice" does not beat
    "gochujang salmon rice bowl" on a line that names both."""
    q = _tokens(query)
    if not q:
        return None
    best, runner = None, None
    for row in rows:
        rt = _tokens(row["name"])
        if not rt:
            continue
        shared = q & rt
        coverage = len(shared) / len(rt)
        if coverage < 0.7 or (len(shared) < 2 and len(rt) > 1):
            continue
        score = (len(shared), coverage)
        if best is None or score > best[0]:
            best, runner = (score, row), best
        elif runner is None or score > runner[0]:
            runner = (score, row)
    if best is None:
        return None
    # An ambiguous winner is not a winner. Two rows scoring the same with materially
    # different energy means the order line does not identify the dish.
    if runner and runner[0] == best[0]:
        a, b = best[1]["kcal"], runner[1]["kcal"]
        if abs(a - b) > max(30.0, 0.1 * max(a, b)):
            return None
    return best[1]


def unaccounted_modifiers(query: str, row_name: str) -> list:
    """Changes named in the order that the published row cannot include.

    Wagamama publish the standard bowl. "extra salmon" is several hundred kcal that the
    row does not contain, and logging the row alone would understate the meal while
    looking like label data."""
    out, rt = [], _tokens(row_name)
    for verb, what in _MODIFIER.findall(query or ""):
        what = what.strip()
        # "large" inside the dish's own name is not a modification of it.
        if _tokens(what) <= rt and verb.lower() in ("large", "add"):
            continue
        out.append(f"{verb.lower()} {what}")
    return out


def apply_swaps(out: dict, dish: str, base_row: dict, rows: list,
                groups: list) -> list:
    """Adjust for a component ORDERED INSTEAD of the published default.

    A swap hides from modifier detection because nothing in the order line marks it as a
    change: "gochujang salmon rice bowl with brown rice" reads exactly like the dish's own
    description. Left undetected it is small on energy and not small on fibre - brown
    instead of sticky is -8 kcal but +3.5 g fibre, and fibre before a long run is a
    ceiling he actually trains against.

    Only the DIFFERENCE between two published rows is applied, never an invented figure,
    and it is reported on the entry."""
    applied = []
    by_name = {r["name"].lower(): r for r in rows}
    dish_tokens = _tokens(dish)
    for group in groups or []:
        default = by_name.get((group.get("default_row") or "").lower())
        if not default:
            continue
        # Only for dishes the default actually applies to. Adjusting a salad for a rice
        # swap would be arithmetic on a component it never had.
        gate = set(group.get("applies_to_tokens") or [])
        if gate and not (gate & _tokens(base_row["name"])):
            continue
        for opt in group.get("options") or []:
            row = by_name.get((opt.get("row") or "").lower())
            if not row or row["name"].lower() == default["name"].lower():
                continue
            if not any(_tokens(m) <= dish_tokens for m in (opt.get("match") or [])):
                continue
            for field in ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g"):
                if out.get(field) is not None and row.get(field) is not None \
                        and default.get(field) is not None:
                    out[field] = round(out[field] + row[field] - default[field], 1)
            applied.append(f"{row['name']} instead of {default['name']}")
            break          # one option per group; a dish has one rice
    return applied


def lookup(vendor: str, dish: str, cache_dir, registry: dict = None,
           now=None) -> dict | None:
    """Fetcher-shaped result for one ordered dish, or None to fall through the ladder."""
    key, entry = find_vendor(vendor, registry)
    if not key:
        return None
    menu = load_menu(key, entry, cache_dir, now=now)
    if not menu.get("rows"):
        return {"error": menu.get("error") or "no rows"} if menu.get("error") else None
    row = match_dish(menu["rows"], dish)
    if not row:
        return None
    out = {"resolved_name": f"{row['name']} ({entry.get('display') or key})",
           "kcal": row["kcal"], "protein_g": row["protein_g"],
           "carb_g": row["carb_g"], "fat_g": row["fat_g"],
           "fibre_g": row["fibre_g"],
           # sodium is printed in GRAMS here. Storing it as mg without converting is the
           # unit trap that has already bitten this codebase on three other sources.
           "dietary_sodium_mg": (round(row["sodium_g"] * 1000)
                                 if row.get("sodium_g") is not None else None),
           "per": "portion", "pack_g": row.get("portion_g"),
           "source_url": menu["source_url"],
           # The chain's own published figures for its own dish. That is label data.
           "source_kind": "manufacturer", "confidence": "label",
           "vendor": entry.get("display") or key,
           "authoritative_host": urlparse(menu["source_url"]).netloc}
    # MODIFIERS. Wagamama publish "extra salmon" as its own row, 282 kcal, so an order
    # line saying "with extra salmon" can be answered entirely from published data
    # instead of guessed at or quietly ignored. Logging the standard bowl alone would
    # have understated this meal by a third while wearing a label badge.
    added, unaccounted = [], []
    for verb, what in _MODIFIER.findall(dish or ""):
        phrase = f"{verb.lower()} {what.strip()}"
        if verb.lower() in _ADDITIVE:
            extra = match_dish(menu["rows"], phrase)
            if extra and extra["name"].lower() != row["name"].lower():
                for field, key in (("kcal", "kcal"), ("protein_g", "protein_g"),
                                   ("carb_g", "carb_g"), ("fat_g", "fat_g"),
                                   ("fibre_g", "fibre_g")):
                    if out.get(field) is not None and extra.get(key) is not None:
                        out[field] = round(out[field] + extra[key], 1)
                if (out.get("dietary_sodium_mg") is not None
                        and extra.get("sodium_g") is not None):
                    out["dietary_sodium_mg"] += round(extra["sodium_g"] * 1000)
                added.append(f"{extra['name']} ({extra['kcal']:.0f} kcal)")
                continue
        # Not published, or a removal we cannot subtract without inventing a figure.
        unaccounted.append(phrase)
    swapped = apply_swaps(out, dish, row, menu["rows"], entry.get("swap_groups"))
    notes = []
    if swapped:
        out["swaps"] = swapped
        notes.append(", ".join(swapped)
                     + " (the difference between those published rows)")
    if added:
        out["components"] = [row["name"]] + [a.split(" (")[0] for a in added]
        out["resolved_name"] += " + " + " + ".join(a.split(" (")[0] for a in added)
        notes.append("includes " + ", ".join(added) + " from the same published menu")
    if unaccounted:
        out["modifiers_unaccounted"] = unaccounted
        notes.append("published figures for the standard dish, so "
                     + ", ".join(unaccounted) + " is NOT included")
    if notes:
        out["note"] = "; ".join(notes)
    return out
