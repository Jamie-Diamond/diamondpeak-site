#!/usr/bin/env python3
"""nutrition_resolve.py - the preferred-source ladder for turning food text into macros.

Jamie's decision, 10 Aug 2026: a ladder of preferred sources with the LLM LAST, not
an LLM with lookups as a fallback. Extended 10 Aug on his instruction to add further
databases.

  rung           source                                  confidence  needs
  cache          a previous resolution, under 365 days   inherited   -
  manual         the athlete read it off the pack        label       -
  retailer       M&S / Ocado / Tesco product listing    label       scraper (hook)
  cofid          PHE McCance & Widdowson (UK official)  label       local table
  usda           USDA FoodData Central                  database    api key
  openfoodfacts  OFF search                             database    -
  nutritionix    Nutritionix natural-language endpoint  database    app id + key
  llm            model estimate                         estimate    model wrapper

WHY THIS ORDER
Branded prepared food is most of this athlete's intake, so the retailer listing
outranks everything: it is the actual product. Below that, CoFID is the right UK
source for WHOLE foods - it is Public Health England's official composition dataset
(McCance & Widdowson), it is UK-specific, it ships as a local table so it needs no
network and cannot be rate-limited, and it does not suffer the crowd-sourced
variance that makes Open Food Facts unreliable on staples. USDA FoodData Central is
larger and better curated than OFF but American, so portion conventions and
fortification differ; it sits above OFF but below CoFID for a UK athlete.
Nutritionix is last of the databases because it is commercial, keyed, and its
strength (natural-language parsing of "two slices of toast") overlaps what the LLM
rung does anyway.

Each rung simply returns None when it cannot help, so the order alone expresses the
preference and adding a source is one entry in LADDER.

THREE RULES THAT STOP THE LADDER QUIETLY DEGRADING
This is the only way a ladder like this fails, so all three are load-bearing:
  - The rung used is RECORDED on every entry and stated in the bot's reply. An
    estimate must never render like label data.
  - A rung is never skipped silently. A configured rung that ERRORS sets
    `degraded: True` and logs the exception; a rung that is simply NOT BUILT logs
    `not_configured` and does not set degraded. Conflating the two would make a
    real outage indistinguishable from normal operation.
  - A cache entry past its age is a MISS, not a warning. UK retailers reformulate,
    and label-grade confidence on a figure nobody has checked in two years is the
    worst of both worlds.

SPECIES COME FROM THE INGREDIENTS, NOT THE PRODUCT NAME
Learned the hard way in test: "M&S nut collection" tagged ZERO species, because the
name does not say which nuts. Composite products need their ingredient list, so
every fetcher may return `ingredients` and species matching runs on that in
preference to the name. Where a source carries no ingredients the item is still
logged, and the unmatched text is queued for review rather than guessed at - a
composite product credited with the wrong species inflates the headline diversity
metric silently, which is exactly what the review queue exists to prevent.

WHAT IS DELIBERATELY NOT IMPLEMENTED
The retailer rung ships as a HOOK with no default. Scraping M&S, Ocado and Tesco is
the most brittle part of this build - markup changes, bot protection, per-retailer
quirks - and a half-working scraper that silently returns nothing is worse than an
absent rung, because the ladder would read as complete while every item resolved one
rung lower. CoFID likewise ships with a small seed and a documented import path:
the full PHE dataset is a published spreadsheet and must be imported deliberately,
not half-guessed here.

SODIUM IS RESOLVED, NOT ESTIMATED, WHERE POSSIBLE
Sodium comes back from labels reliably and from a model badly, worse than the macros
do, because products in the same category differ enormously and category
pattern-matching fails on it. `sodium_confidence` is reported separately for that
reason.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nutrition_store import (CACHE_MAX_AGE_DAYS, CONFIDENCE_LEVELS,  # noqa: E402
                             RUNG_CONFIDENCE, SOURCE_RUNGS)

TIMEOUT_S = 6
USER_AGENT = "ClaudeCoach-Nutrition/1.0 (personal training log)"

OFF_ENDPOINT = "https://world.openfoodfacts.org/cgi/search.pl"
OFF_PRODUCT = "https://world.openfoodfacts.org/api/v2/product"
USDA_ENDPOINT = "https://api.nal.usda.gov/fdc/v1/foods/search"
NUTRITIONIX_ENDPOINT = "https://trackapi.nutritionix.com/v2/natural/nutrients"

COFID_TABLE = Path(__file__).resolve().parent.parent / "config" / "cofid.json"

MACRO_FIELDS = ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g", "dietary_sodium_mg")

# Words that carry no product identity, so they must never be the thing a database hit
# and a query have "in common". "protein" is the one that did the damage.
#
# NOT in here, deliberately: bar, powder, isolate, concentrate. Those are product FORMS
# and they discriminate - a bar is not an isolate, and "collagen powder" against "soy
# protein isolate" should fail on form as well as on substance. Treating them as noise
# left "protein bar" with no identifying tokens at all, which made the guard abstain.
_STOPWORDS = {
    "the", "and", "with", "of", "a", "an", "in", "my", "some", "half", "one", "two",
    "raw", "fresh", "plain", "whole", "large", "small", "medium", "pack", "packet",
    "bag", "pot", "tub", "slice", "slices", "portion", "serving", "g", "kg",
    "mg", "ml", "l", "oz", "protein", "high", "low", "free", "light", "extra", "new",
    "value", "brand", "own", "food", "foods", "drink", "mix", "flavour", "flavoured",
    "style", "type", "supplement", "capsule",
    "capsules", "pill", "pills", "tablet", "tablets", "this", "morning", "had",
}


def _tokens(text: str) -> set:
    out = set()
    for w in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(w) >= 3 and w not in _STOPWORDS and not w.isdigit():
            out.add(w.rstrip("s") if len(w) > 4 and w.endswith("s") else w)
    return out


def _relevant(query: str, name: str) -> bool:
    """Does this database hit actually correspond to what was asked for?

    THE BUG THIS EXISTS FOR. "400mg of my protein collagen capsules" resolved to
    "Soy protein isolate" from USDA, with confident macros, a `database` badge and a
    soy plant species tagged against it - a product he never ate, inflating the
    diversity count. USDA matched on the word "protein" and the fetcher took the first
    result that carried an energy figure.

    So a name-searched hit must now share at least one IDENTIFYING token with the query.
    "protein", "isolate", "capsule" and friends are stopwords precisely because they are
    what a wrong match latches onto. `collagen` against {soy, isolate} shares nothing, so
    it is rejected and the ladder moves on.

    Erring toward rejection is right: a rejected hit falls through to the next rung and
    ultimately to an LLM estimate that is LABELLED an estimate, whereas a wrong hit wears
    a database badge and looks trustworthy."""
    q, n = _tokens(query), _tokens(name)
    if not q:
        return True                      # nothing to check against
    return bool(q & n)


class Rung:
    CACHE = "cache"
    MANUAL = "manual"
    RETAILER = "retailer"
    COFID = "cofid"
    USDA = "usda"
    OFF = "openfoodfacts"
    NUTRITIONIX = "nutritionix"
    LLM = "llm"


# Order IS the preference. Adding a source is one entry here.
LADDER = (Rung.RETAILER, Rung.COFID, Rung.USDA, Rung.OFF, Rung.NUTRITIONIX, Rung.LLM)


def _scale(per_100g: dict, portion_g: float) -> dict:
    """Scale per-100g figures to the portion. Returns only the fields PRESENT, so a
    source carrying no fibre yields no fibre rather than a confident zero."""
    factor = (portion_g or 100.0) / 100.0
    return {k: round(float(v) * factor, 1) for k, v in per_100g.items() if v is not None}


def _get_json(url: str, headers: dict = None, body: dict = None, timeout=TIMEOUT_S):
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.loads(fh.read().decode("utf-8", "replace"))


# --- rung: Open Food Facts ---------------------------------------------------

OFF_KEYS = {"energy-kcal_100g": "kcal", "proteins_100g": "protein_g",
            "carbohydrates_100g": "carb_g", "fat_100g": "fat_g",
            "fiber_100g": "fibre_g"}


def off_fetch(query: str, portion_g: float = None) -> dict | None:
    """Open Food Facts search.

    Takes the first product that actually carries an energy figure. OFF is
    crowd-sourced and a large share of hits have a name and nothing else, so
    "first result" without that check returns every macro null and the ladder would
    score it as a successful database resolution."""
    params = {"search_terms": query, "search_simple": 1, "action": "process",
              "json": 1, "page_size": 8,
              "fields": "product_name,brands,code,url,ingredients_text,"
                        + ",".join(OFF_KEYS) + ",sodium_100g"}
    data = _get_json(f"{OFF_ENDPOINT}?{urllib.parse.urlencode(params)}")
    for product in data.get("products") or []:
        if product.get("energy-kcal_100g") in (None, ""):
            continue
        out = _scale({ours: product.get(theirs) for theirs, ours in OFF_KEYS.items()},
                     portion_g)
        sodium_g = product.get("sodium_100g")
        if sodium_g not in (None, ""):
            # OFF stores sodium in GRAMS per 100 g. Never read the `salt` field
            # instead: salt is sodium x 2.5 and mixing them is a silent 150% error.
            out["dietary_sodium_mg"] = round(float(sodium_g) * 1000
                                             * ((portion_g or 100.0) / 100.0))
        name = " ".join(x for x in ((product.get("brands") or "").split(",")[0].strip(),
                                    product.get("product_name") or "") if x).strip()
        if not _relevant(query, name):
            continue                     # wrong product; keep looking
        return {**out, "resolved_name": name or query,
                "ingredients": product.get("ingredients_text") or "",
                "source_url": product.get("url") or "",
                "barcode": product.get("code") or ""}
    return None


def off_barcode_fetch(barcode: str, portion_g: float = None) -> dict | None:
    """Direct barcode lookup. Far more reliable than a name search, which is why a
    scanned barcode short-circuits the text ladder entirely.

    Open Food Facts barcode coverage on UK packaged goods is good; own-brand prepared
    food is patchier, and a miss here simply falls through to the rest of the ladder."""
    code = "".join(ch for ch in str(barcode or "") if ch.isdigit())
    if not code:
        return None
    fields = ("product_name,brands,code,url,ingredients_text,serving_quantity,"
              + ",".join(OFF_KEYS) + ",sodium_100g")
    data = _get_json(f"{OFF_PRODUCT}/{code}.json?fields={urllib.parse.quote(fields)}")
    if data.get("status") != 1:
        return None
    product = data.get("product") or {}
    if product.get("energy-kcal_100g") in (None, ""):
        return None
    # A barcode with no stated portion means the whole pack is ambiguous, so fall back
    # to the label serving size rather than silently assuming 100 g.
    if portion_g is None and product.get("serving_quantity"):
        try:
            portion_g = float(product["serving_quantity"])
        except (TypeError, ValueError):
            portion_g = None
    out = _scale({ours: product.get(theirs) for theirs, ours in OFF_KEYS.items()},
                 portion_g)
    sodium_g = product.get("sodium_100g")
    if sodium_g not in (None, ""):
        out["dietary_sodium_mg"] = round(float(sodium_g) * 1000
                                         * ((portion_g or 100.0) / 100.0))
    name = " ".join(x for x in ((product.get("brands") or "").split(",")[0].strip(),
                                product.get("product_name") or "") if x).strip()
    return {**out, "resolved_name": name or f"barcode {code}",
            "ingredients": product.get("ingredients_text") or "",
            "source_url": product.get("url") or f"https://world.openfoodfacts.org/product/{code}",
            "barcode": code, "portion_used_g": portion_g}


# --- rung: USDA FoodData Central --------------------------------------------

USDA_NUTRIENTS = {1008: "kcal", 1003: "protein_g", 1005: "carb_g", 1004: "fat_g",
                  1079: "fibre_g", 1093: "dietary_sodium_mg"}


def usda_fetch(query: str, portion_g: float = None, api_key: str = None) -> dict | None:
    """USDA FoodData Central. Requires FDC_API_KEY (DEMO_KEY works, rate-limited).

    Prefers SR Legacy and Foundation data over Branded: the branded set is
    manufacturer-submitted and American, so a UK athlete searching "oats" wants the
    composition entry, not a US cereal box."""
    key = api_key or os.environ.get("FDC_API_KEY")
    if not key:
        return None
    params = {"query": query, "api_key": key, "pageSize": 10,
              "dataType": "Foundation,SR Legacy,Branded"}
    data = _get_json(f"{USDA_ENDPOINT}?{urllib.parse.urlencode(params)}")
    foods = data.get("foods") or []
    foods.sort(key=lambda f: 0 if f.get("dataType") in ("Foundation", "SR Legacy") else 1)
    for food in foods:
        per_100 = {}
        for n in food.get("foodNutrients") or []:
            field = USDA_NUTRIENTS.get(n.get("nutrientId"))
            if field and n.get("value") is not None:
                per_100[field] = n["value"]
        if "kcal" not in per_100:
            continue
        if not _relevant(query, food.get("description") or ""):
            continue                     # wrong product; keep looking
        sodium = per_100.pop("dietary_sodium_mg", None)
        out = _scale(per_100, portion_g)
        if sodium is not None:
            # USDA reports sodium in MILLIGRAMS per 100 g already, unlike OFF.
            out["dietary_sodium_mg"] = round(sodium * ((portion_g or 100.0) / 100.0))
        return {**out, "resolved_name": food.get("description") or query,
                "ingredients": food.get("ingredients") or "",
                "source_url": f"https://fdc.nal.usda.gov/fdc-app.html#/food-details/"
                              f"{food.get('fdcId')}/nutrients" if food.get("fdcId") else ""}
    return None


# --- rung: Nutritionix ------------------------------------------------------

def nutritionix_fetch(query: str, portion_g: float = None,
                      app_id: str = None, app_key: str = None) -> dict | None:
    """Nutritionix natural-language endpoint. Requires NUTRITIONIX_APP_ID and _KEY.

    This one parses the portion itself ("two slices of toast"), so it is NOT scaled
    by portion_g - doing both would double-count the portion. That asymmetry is why
    it sits at the bottom of the database rungs."""
    app_id = app_id or os.environ.get("NUTRITIONIX_APP_ID")
    app_key = app_key or os.environ.get("NUTRITIONIX_APP_KEY")
    if not (app_id and app_key):
        return None
    data = _get_json(NUTRITIONIX_ENDPOINT,
                     headers={"x-app-id": app_id, "x-app-key": app_key},
                     body={"query": query})
    foods = data.get("foods") or []
    if not foods:
        return None
    if not any(_relevant(query, f.get("food_name") or "") for f in foods):
        return None
    total = {"kcal": 0.0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0,
             "fibre_g": 0.0, "dietary_sodium_mg": 0.0}
    names = []
    for f in foods:
        total["kcal"] += f.get("nf_calories") or 0
        total["protein_g"] += f.get("nf_protein") or 0
        total["carb_g"] += f.get("nf_total_carbohydrate") or 0
        total["fat_g"] += f.get("nf_total_fat") or 0
        total["fibre_g"] += f.get("nf_dietary_fiber") or 0
        total["dietary_sodium_mg"] += f.get("nf_sodium") or 0
        names.append(f.get("food_name") or "")
    return {**{k: round(v, 1) for k, v in total.items()},
            "dietary_sodium_mg": round(total["dietary_sodium_mg"]),
            "resolved_name": ", ".join(n for n in names if n) or query,
            "ingredients": "", "source_url": "https://www.nutritionix.com/"}


# --- rung: CoFID (PHE McCance & Widdowson) ----------------------------------

class CofidTable:
    """Public Health England's Composition of Foods Integrated Dataset, locally.

    The authoritative UK composition source for WHOLE foods, and the best rung in
    this ladder for anything unbranded: UK-specific, no network, no rate limit, no
    crowd-sourced variance. Values are per 100 g as published.

    Ships with a small seed. The full dataset is a published PHE spreadsheet and
    must be imported deliberately - `config/cofid.json` is a plain
    {name: {per-100g fields}, aliases: [...]} map, so importing it is a conversion
    script, not a code change. Deliberately NOT half-populated by guesswork here:
    a wrong composition figure carrying `label` confidence is worse than no rung."""

    def __init__(self, path=None, data=None):
        if data is None:
            path = Path(path or COFID_TABLE)
            if not path.exists():
                self.foods = {}
                self.available = False
                return
            data = json.loads(path.read_text())
        self.foods = {}
        for food in data.get("foods", []):
            for name in [food["name"]] + list(food.get("aliases") or []):
                self.foods[name.strip().lower()] = food
        self.available = bool(self.foods)

    def lookup(self, query: str, portion_g: float = None) -> dict | None:
        """Exact then longest-substring match. No fuzzy matching on purpose: a fuzzy
        hit here would carry `label` confidence, and a confidently wrong composition
        figure is the worst output this module can produce."""
        q = (query or "").strip().lower()
        if not q:
            return None
        food = self.foods.get(q)
        if food is None:
            hits = [(name, f) for name, f in self.foods.items()
                    if name in q or q in name]
            if not hits:
                return None
            hits.sort(key=lambda h: len(h[0]), reverse=True)
            food = hits[0][1]
            if not _relevant(query, food.get("name") or ""):
                return None
        per_100 = {f: food.get(f) for f in MACRO_FIELDS if food.get(f) is not None}
        sodium = per_100.pop("dietary_sodium_mg", None)
        out = _scale(per_100, portion_g)
        if sodium is not None:
            out["dietary_sodium_mg"] = round(sodium * ((portion_g or 100.0) / 100.0))
        return {**out, "resolved_name": food["name"],
                "ingredients": food.get("ingredients", food["name"]),
                "source_url": food.get("source_url",
                                       "PHE Composition of Foods Integrated Dataset")}


# --- the ladder -------------------------------------------------------------

def resolve(raw_text: str, *, day, store=None, portion_g: float = None,
            table=None, fetchers: dict = None, cofid: CofidTable = None,
            on: date = None) -> dict:
    """Walk the ladder and return one resolved item plus a full attempt log.

    `fetchers` maps a rung name to a callable (text, portion_g) -> dict|None. Any
    rung absent from it is reported `not_configured` rather than silently skipped.
    `day` is the athlete's LOCAL date and is required: it stamps `resolved_at` and
    dates the review-queue entry, and this module never decides the local day itself
    (a UTC-dated write after 23:00 London lands on the wrong day).

    Never returns a bare failure. If every rung fails the result is still a usable
    record with `confidence: estimate`, macros None and `needs_input: True`, so the
    bot asks rather than logging zeroes - a zero-calorie entry is far more damaging
    to the record than an absent one, because it looks like data."""
    attempts = []
    fetchers = dict(fetchers or {})
    key = (raw_text or "").strip().lower()
    on = on or (date.fromisoformat(str(day)[:10]) if day else date.today())

    def record(rung, outcome, detail=""):
        attempts.append({"rung": rung, "outcome": outcome, "detail": detail})

    # cache first, and a hit short-circuits everything below it
    if store is not None:
        hit = store.cache_get(key, on=on)
        if hit:
            record(Rung.CACHE, "hit", f"resolved_at {hit.get('resolved_at')}")
            return _finalise(dict(hit), raw_text, Rung.CACHE,
                             hit.get("confidence", "estimate"), attempts, table, day,
                             degraded=False)
        record(Rung.CACHE, "miss", f"absent or older than {CACHE_MAX_AGE_DAYS} days")

    # CoFID is a local table, so wire it in automatically when present
    if Rung.COFID not in fetchers:
        cofid = cofid if cofid is not None else CofidTable()
        if cofid.available:
            fetchers[Rung.COFID] = lambda t, p, _c=cofid: _c.lookup(t, p)

    degraded = False
    for rung in LADDER:
        fetch = fetchers.get(rung)
        if fetch is None:
            # Not built is NOT degradation: nothing failed. Conflating the two would
            # make a real outage look like normal operation.
            record(rung, "not_configured")
            continue
        try:
            got = fetch(raw_text, portion_g)
        except Exception as exc:
            # A configured rung that FAILS is degradation and must be visible. This
            # is the difference between "not found" and "we did not really look".
            record(rung, "error", f"{type(exc).__name__}: {exc}")
            degraded = True
            continue
        if got:
            record(rung, "hit")
            return _finalise(got, raw_text, rung, RUNG_CONFIDENCE[rung], attempts,
                             table, day, degraded=degraded)
        record(rung, "no_match")

    if store is not None:
        store.log_unresolved(raw_text, day=day)
    out = _finalise({}, raw_text, Rung.LLM, "estimate", attempts, table, day,
                    degraded=degraded)
    out["needs_input"] = True
    out.update({f: None for f in MACRO_FIELDS})
    return out


def _finalise(got: dict, raw_text: str, rung: str, confidence: str, attempts, table,
              day, degraded: bool) -> dict:
    """Shape one resolved item, tag species from its INGREDIENTS, and state how good
    the figures are."""
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError(f"bad confidence {confidence!r}")
    if rung not in SOURCE_RUNGS:
        raise ValueError(f"bad rung {rung!r}")
    name = got.get("resolved_name") or raw_text
    ingredients = got.get("ingredients") or ""
    species, unmatched = [], ""
    if table is not None:
        # Ingredients first: a composite product's NAME does not say what is in it.
        # "M&S nut collection" tags zero species off the name alone.
        #
        # And when there are no ingredients, tag from the RAW TEXT only, never from a
        # resolved product name. Tagging from the name credited a soy species against
        # "collagen capsules" because the resolver had matched the wrong product - a
        # plant he never ate, in the headline diversity count.
        subject = ingredients or raw_text
        res = table.match_text(subject)
        species = [s["id"] for s in res["species"]]
        unmatched = res["unmatched"]
    out = {
        "raw_text": raw_text,
        "resolved_name": name,
        "ingredients": ingredients,
        "confidence": confidence,
        "source_rung": rung,
        "source_url": got.get("source_url", ""),
        "resolved_at": str(day)[:10],
        "species": species,
        "species_from": "ingredients" if ingredients else "name",
        "species_unmatched": unmatched,
        "degraded": degraded,
        "attempts": attempts,
        "needs_input": False,
        "sodium_confidence": (confidence if got.get("dietary_sodium_mg") is not None
                              else "unknown"),
    }
    for f in MACRO_FIELDS:
        out[f] = got.get(f)
    return out


def cache_resolved(store, item: dict) -> None:
    """Write a successful resolution back to the cache.

    Only label and database rungs are cached. Caching an LLM estimate would freeze
    one guess and re-serve it for a year as though it had been looked up, which is
    exactly the false confidence the ladder exists to prevent."""
    if item.get("confidence") not in ("label", "database"):
        return
    if item.get("needs_input"):
        return
    payload = {f: item.get(f) for f in MACRO_FIELDS}
    payload.update({"resolved_name": item.get("resolved_name"),
                    "ingredients": item.get("ingredients", ""),
                    "confidence": item.get("confidence"),
                    "source_rung": item.get("source_rung"),
                    "source_url": item.get("source_url", ""),
                    "species": item.get("species", []),
                    "resolved_at": item.get("resolved_at")})
    store.cache_put((item.get("raw_text") or "").strip().lower(), payload)


def describe_provenance(item: dict) -> str:
    """One short line for the bot's confirm message. The rung is always stated: an
    estimate must never look like label data, and a degraded resolution must say so."""
    if item.get("needs_input"):
        return "Could not resolve this one. Give me the pack figures?"
    label = {Rung.CACHE: "from your saved items",
             Rung.MANUAL: "from the pack, as you gave it",
             Rung.RETAILER: "from the retailer listing",
             Rung.COFID: "from the UK composition tables (CoFID)",
             Rung.USDA: "from USDA FoodData Central",
             Rung.OFF: "from Open Food Facts",
             Rung.NUTRITIONIX: "from Nutritionix",
             Rung.LLM: "estimated"}.get(item.get("source_rung"), item.get("source_rung"))
    bits = [label]
    if item.get("confidence") == "estimate":
        bits.append("roughly +/-10-15%")
    if item.get("degraded"):
        failed = [a["rung"] for a in item.get("attempts", []) if a["outcome"] == "error"]
        bits.append("a better source failed: " + ", ".join(failed))
    return " - ".join(bits)


def ladder_status(fetchers: dict = None, cofid: CofidTable = None) -> dict:
    """Which rungs are actually available. For a startup log and the /target reply,
    so an unbuilt ladder is visible without reading an item's attempt log."""
    fetchers = fetchers or {}
    cofid = cofid if cofid is not None else CofidTable()
    out = {}
    for rung in LADDER:
        if rung == Rung.COFID:
            out[rung] = "ready" if (Rung.COFID in fetchers or cofid.available) \
                else "not_configured"
        else:
            out[rung] = "ready" if rung in fetchers else "not_configured"
    return out
