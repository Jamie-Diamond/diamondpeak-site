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
    # How MUCH, never WHAT. These have to be stopwords for the coverage rule to work:
    # "a handful of almonds" was refused by CoFID because "handful" counted as an
    # identifying token the table row failed to explain, so a label-grade match for a
    # plain whole food fell through to an LLM estimate. Deliberately excluded: "bowl",
    # "bar" and the like, which do carry meaning about the product.
    "handful", "handfuls", "pinch", "cup", "cups", "spoon", "spoonful", "tbsp", "tsp",
    "tablespoon", "teaspoon", "glass", "can", "tin", "bottle", "box", "few", "couple",
    "about", "roughly", "approx", "approximately", "big", "little", "each",
}


# How much of a query a CoFID row is allowed to leave unexplained. One spare token covers
# the ordinary descriptors that survive the stopword list ("handful of almonds",
# "extra virgin olive oil"); anything beyond that is a composite dish, not a table row.
COFID_MAX_UNEXPLAINED_TOKENS = 1


def _tokens(text: str) -> set:
    out = set()
    for w in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(w) >= 3 and w not in _STOPWORDS and not w.isdigit():
            out.add(w.rstrip("s") if len(w) > 4 and w.endswith("s") else w)
    return out


# A query naming a DOSE form must not match a candidate naming a FOOD form. "collagen
# capsules" shares the token "collagen" with "COLLAGEN PROTEIN BAR, LEMON COOKIE", so the
# token test alone passed it. Capsules are not a bar.
_DOSE_FORMS = {"capsule", "capsules", "cap", "caps", "pill", "pills", "tablet",
               "tablets", "tab", "tabs", "softgel", "softgels", "gummy", "gummies"}
_FOOD_FORMS = {"bar", "bars", "cookie", "cookies", "biscuit", "biscuits", "drink",
               "smoothie", "shake", "yogurt", "yoghurt", "cake", "brownie", "cereal",
               "crisps", "chips", "meal", "pizza", "sandwich", "wrap", "soup"}


def _form_conflict(query: str, name: str) -> bool:
    """True when one side is a dose form and the other is a food form."""
    q = set(re.split(r"[^a-z]+", (query or "").lower()))
    n = set(re.split(r"[^a-z]+", (name or "").lower()))
    return bool((q & _DOSE_FORMS and n & _FOOD_FORMS)
                or (n & _DOSE_FORMS and q & _FOOD_FORMS))


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
    if _form_conflict(query, name):
        return False
    q, n = _tokens(query), _tokens(name)
    if not q:
        return True                      # nothing to check against
    return bool(q & n)


class Rung:
    CACHE = "cache"
    # The chain's own published per-dish nutrition, read from the platform it publishes
    # through. Ahead of everything else for a restaurant dish: it is the manufacturer of
    # that dish, so its figures are label data, and no search can beat them.
    VENDOR = "vendor"
    WEB = "web"
    MANUAL = "manual"
    RETAILER = "retailer"
    COFID = "cofid"
    USDA = "usda"
    OFF = "openfoodfacts"
    NUTRITIONIX = "nutritionix"
    LLM = "llm"


# Order IS the preference. Adding a source is one entry here.
# THE TEST EVERY RUNG HAS TO PASS (Jamie, 10 Aug 2026): "the ladder should be the same or
# better than just using llm to do it, if its worse than the llm just googling it, then we
# should just use that". Applied honestly, that prunes it hard.
#
# KEPT, because each genuinely beats a web search:
#   cache   a previous good answer. Instant, free, already checked.
#   cofid   Public Health England's own composition tables. For an unbranded UK whole
#           food this IS the reference, it is deterministic, and it needs no network.
#   web     the model with search. The benchmark, and it can reach a manufacturer page,
#           which is label data.
#   llm     a bare estimate, for when even search finds nothing. Always flagged.
# Plus barcode, which short-circuits everything: an exact GTIN lookup cannot be ambiguous.
#
# DROPPED from the default path, having lost that comparison:
#   usda          American, name-searched, and it is what matched "collagen capsules" to
#                 "Soy protein isolate". For UK whole foods CoFID is better; for UK
#                 branded products the web is better. It was winning nowhere.
#   openfoodfacts NAME search only. Crowd-sourced and patchy on the own-brand prepared
#                 food that is most of this athlete's intake. Its BARCODE endpoint is
#                 excellent and is still used - the two are different queries.
#   nutritionix   keyed, commercial, and its natural-language parsing is the one thing
#                 the interpret pass already does.
#   retailer      never built. The web rung is this, without a scraper per supermarket.
#
# All four remain implemented and tested, and can be re-enabled per athlete via config.
# They are off the default path because they lost on merit, not because they are broken.
LADDER = (Rung.COFID, Rung.WEB, Rung.LLM)
OPTIONAL_RUNGS = (Rung.VENDOR, Rung.RETAILER, Rung.USDA, Rung.OFF, Rung.NUTRITIONIX)
# Preference order for EVERY rung, default or optional. An optional rung that a caller
# supplies a fetcher for joins the ladder at its proper place rather than being ignored -
# otherwise enabling one would silently do nothing, which is the same class of bug as a
# parameter nobody passes.
FULL_ORDER = (Rung.VENDOR, Rung.RETAILER, Rung.COFID, Rung.USDA, Rung.OFF,
              Rung.NUTRITIONIX, Rung.WEB, Rung.LLM)


def effective_ladder(fetchers: dict = None, cofid_ready: bool = True) -> tuple:
    """The rungs that will actually be walked, in order."""
    supplied = set(fetchers or {})
    out = []
    for rung in FULL_ORDER:
        if rung in LADDER or rung in supplied:
            if rung == Rung.COFID and not (cofid_ready or Rung.COFID in supplied):
                continue
            out.append(rung)
    return tuple(out)


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
            # Substring matching alone was the bug: "chicken" appears inside "satay
            # chicken with black rice and mango", so a five-word branded dish matched raw
            # chicken breast. A partial match now needs the TABLE name to be essentially
            # contained in the query, and at least two shared identifying tokens, so a
            # single ingredient word can never carry a match.
            #
            # SHARED TOKENS ARE NOT ENOUGH, AND TWO OF THEM ARE EASY TO HIT BY ACCIDENT.
            # "gochujang salmon rice bowl with brown rice and extra salmon, Wagamama"
            # shares BOTH "rice" and "brown" with the table row "Rice, brown, raw", so
            # the two-token bar passed and a restaurant dish was logged as 357 kcal of raw
            # grain. What separates the two cases is COVERAGE, not overlap: for a real
            # whole-food query the table name accounts for essentially the whole query
            # ("brown rice", "porridge oats", "chicken breast" each leave nothing over),
            # whereas the dish leaves gochujang, salmon, bowl, extra and Wagamama
            # unexplained. So a candidate must also leave at most one query token
            # unaccounted for.
            #
            # The coverage test belongs HERE, inside the candidate loop, not after the
            # sort: applied to the winner it would let a high-overlap, poor-coverage row
            # shadow a lower-overlap row that actually covers the query.
            qt = _tokens(q)
            hits = []
            for name, f in self.foods.items():
                nt = _tokens(name)
                if not nt:
                    continue
                # COVERAGE HAS TO BE TESTED BOTH WAYS ROUND. Everything above measures
                # how much of the QUERY the row explains, and nothing measured whether
                # the query asked for the row at all - so a one-token query matched any
                # row containing that token. "butter" resolved to "Peanut butter, smooth"
                # six times on 12 Aug 2026, twice after the athlete said "I never said
                # peanut butter": {butter} shares a token with the row, leaves nothing
                # unexplained, and sailed through every check below.
                #
                # A row's IDENTITY is the first comma-segment of its published name -
                # what the thing IS, before the qualifiers. PHE names are written that
                # way throughout: "Peanut butter, smooth" -> {peanut, butter},
                # "Apple, eating, flesh and skin" -> {apple}, "Bread, wholemeal" ->
                # {bread}. Every identity token must appear in the query, so "butter"
                # cannot reach peanut butter while "peanut butter" still can, and a
                # qualifier the query adds ("eating apple") is still free to be ignored.
                #
                # Read off the CANONICAL name, not the alias this key came in under: the
                # alias is a search convenience and may carry words the published name
                # does not ("tinned tuna"), which would reject a query that names the
                # food correctly. Exact and alias dict hits never reach here at all.
                identity = _tokens((f.get("name") or "").split(",")[0])
                if identity and not identity <= qt:
                    continue
                shared = qt & nt
                if len(qt - nt) > COFID_MAX_UNEXPLAINED_TOKENS:
                    continue
                # ONE shared token is enough when the row explains the WHOLE query. The
                # two-token bar was standing in for coverage, and it cost real matches:
                # "a handful of almonds" and "half a large banana" are exactly what this
                # table is for, and both fell through to an LLM estimate because they
                # reduce to a single identifying word. Coverage is now checked directly
                # above, so the bar can come down without reopening the "chicken" bug -
                # "satay chicken with black rice and mango" leaves four tokens
                # unexplained and never reaches here.
                if (len(shared) >= 2 or (name in q and len(nt) >= 2)
                        or (shared and not (qt - nt))):
                    hits.append((len(shared), -len(nt - qt), name, f))
            if not hits:
                return None
            # Best row: most of the query explained, then the row that adds the LEAST the
            # query did not ask for. Preferring the longest name instead picked the most
            # embellished row, so a bare "almonds" could land on a roasted salted one.
            hits.sort(reverse=True)
            food = hits[0][3]
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

# Product forms, grouped. A hit whose form family differs from the one the interpreter
# stated is the wrong product, however well the words overlap: a collagen CAPSULE and a
# collagen protein BAR share every meaningful token and are not the same thing.
_HINT_FORM_WORDS = {
    "capsule": {"capsule", "capsules", "cap", "caps", "softgel", "softgels"},
    "tablet": {"tablet", "tablets", "tab", "tabs", "lozenge"},
    "powder": {"powder", "powdered", "isolate", "concentrate", "scoop"},
    "bar": {"bar", "bars", "flapjack", "brownie"},
    "drink": {"drink", "juice", "smoothie", "shake", "squash", "cordial", "soda"},
    "prepared_meal": {"meal", "ready", "curry", "salad", "pizza", "sandwich", "wrap",
                      "soup", "risotto"},
    "bakery": {"cookie", "cookies", "biscuit", "biscuits", "cake", "muffin", "bread"},
    "confectionery": {"chocolate", "sweets", "candy", "bar"},
}
_DOSE_FAMILY = {"capsule", "tablet", "powder"}


def _hint_conflict(hint: dict, name: str) -> bool:
    """True when a candidate's name betrays a different product FORM than expected.

    This is the structural version of the guard that word lists kept failing at. The
    interpreter states what the thing IS (capsule, bar, prepared_meal), and a candidate
    naming a form from the other family is rejected. Only a stated expectation makes this
    possible, which is why interpretation now comes first."""
    if not hint:
        return False
    want = (hint.get("form") or "").lower()
    if want not in _HINT_FORM_WORDS:
        return False
    words = set(re.split(r"[^a-z]+", (name or "").lower()))
    want_dose = want in _DOSE_FAMILY
    for form, tokens in _HINT_FORM_WORDS.items():
        if form == want or not (words & tokens):
            continue
        if (form in _DOSE_FAMILY) != want_dose:
            return True          # dose form against food form, or the reverse
    return False


# ASSUMED PORTIONS, for the case where the figures are good and the AMOUNT is the only
# thing missing. "How much did you have?" is the right question for a prepared meal off a
# per-100g label, and the wrong one for a teaspoon of butter: he told us the amount in the
# words he used, in the units people use for that food. Refusing to convert them is how a
# resolution with perfect label data dead-ended at kcal None.
#
# Every figure here is a stated ASSUMPTION, never a silent one: the offer says which
# default was applied so a wrong one is corrected in one message. That is the whole
# difference between this and the 100 g guess the web rung is forbidden from making - a
# guess at how much of an unknown pack he ate is undetectable afterwards, whereas
# "assumed 5 g - a teaspoon" is either right or obviously wrong.
#
# (word pattern, grams PER UNIT, the unit's name, food words it needs)
_UNIT_PORTIONS = (
    (r"\btea\s?spoons?\b|\btsps?\b", 5, "teaspoon", ()),
    (r"\btable\s?spoons?\b|\btbsps?\b", 15, "tablespoon", ()),
    (r"\bknobs?\b", 10, "knob", ()),
    # A handful is only a portion for things eaten by the handful. A handful of chicken is
    # not a unit anybody means, so it falls through to the question rather than to 30 g.
    (r"\bhandfuls?\b", 30, "handful", ("nut", "almond", "cashew", "walnut", "peanut",
                                       "pistachio", "raisin", "sultana", "sweet",
                                       "haribo", "crisp", "seed")),
    # A slice is bread-sized ONLY for bread. A slice of cake and a slice of ham are
    # different foods with nothing in common but the word.
    (r"\bslices?\b", 36, "slice", ("bread", "toast", "loaf", "bap", "bagel")),
    (r"\bsmall\b[\w\s]{0,12}\bbananas?\b", 90, "small banana", ()),
    (r"\blarge\b[\w\s]{0,12}\bbananas?\b", 136, "large banana", ()),
)

# Per-PIECE defaults, for a whole item counted rather than weighed.
_PIECE_PORTIONS = (("banana", 118, "medium banana"),
                   ("apple", 180, "medium apple"),
                   ("orange", 130, "medium orange"),
                   ("egg", 50, "egg"))

# How many of them, when he said. A unit default multiplied by a stated count rather than
# applied once: "two slices of toast" assumed as one slice halves the entry, and it does it
# quietly, which is the failure mode this whole module is arranged against.
_COUNT_WORDS = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "couple": 2,
                # "half a large banana" is the file's own worked example of a query this
                # table is for, and reading it as a whole one doubles the entry. A fraction
                # is a count like any other.
                "half": 0.5, "quarter": 0.25}
_COUNT_BEFORE = re.compile(r"(\d+|one|two|three|four|five|six|couple|half|quarter|a|an)"
                           r"\s+(?:\w+\s+){0,2}$")


def _stated_count(said: str, at: int) -> float:
    """The number in front of the unit word, or 1. Anything unparseable is 1: an assumed
    portion is stated in the reply, so being wrong about the count is correctable, whereas
    refusing to read "two" is a dead end."""
    m = _COUNT_BEFORE.search(said[:at])
    if not m:
        return 1
    tok = m.group(1)
    n = int(tok) if tok.isdigit() else _COUNT_WORDS.get(tok, 1)
    return n if 0.25 <= n <= 12 else 1


def _plural(noun: str, n: float) -> str:
    """"a teaspoon" / "2 teaspoons" / "half a slice". The article is chosen on the vowel so
    the assumption reads like a sentence - he is being asked to check it, so it has to be
    readable."""
    article = ("an " if noun[:1] in "aeiou" else "a ") + noun
    if n == 1:
        return article
    if n < 1:
        return {0.5: "half ", 0.25: "a quarter of "}.get(n, f"{n} of ") + article
    return f"{n:g} {noun}s"


def _default_portion(raw_text: str, resolved_name: str):
    """(grams, how it reads back) for the amount his words imply, or (None, "").

    The UNIT comes from what he said - a resolved product name never says "teaspoon" - but
    the FOOD may be named on either side, because "a slice of it" only becomes bread once
    the row is known."""
    said = (raw_text or "").lower()
    subject = said + " " + (resolved_name or "").lower()
    for pattern, grams, noun, needs in _UNIT_PORTIONS:
        m = re.search(pattern, said)
        if not m:
            continue
        if needs and not any(re.search(rf"\b{w}", subject) for w in needs):
            continue
        n = _stated_count(said, m.start())
        return float(grams) * n, _plural(noun, n)
    for word, grams, noun in _PIECE_PORTIONS:
        m = re.search(rf"\b{word}s?\b", subject)
        if not m:
            continue
        n = _stated_count(subject, m.start())
        if n == 1 and m.group(0).endswith("s"):
            # A bare plural is a COUNT QUESTION, not a piece: "bananas" could be two or
            # five, and answering it with one banana's weight understates the entry
            # silently. One named piece is the only case this rule is safe for.
            continue
        return float(grams) * n, _plural(noun, n)
    return None, ""


def default_portion_g(raw_text: str, resolved_name: str = "") -> float | None:
    """The assumed portion in grams, or None when nothing here applies."""
    return _default_portion(raw_text, resolved_name)[0]


def _excluded_by(candidate_name: str, exclude) -> str:
    """The rejected phrase this candidate matches, or "".

    Matched on IDENTIFYING TOKENS rather than as a substring, in both directions of the
    problem. "peanut butter" has to block "Peanut butter, smooth" (which a bare substring
    test would miss on the comma and the case) and must NOT block "Butter, salted" - the
    athlete rejected peanut butter, not butter, and blocking the thing he actually ate
    would be the same bug wearing the opposite sign."""
    for phrase in exclude or ():
        want = _tokens(phrase)
        if want and want <= _tokens(candidate_name):
            return phrase
    return ""


def resolve(raw_text: str, *, day, store=None, portion_g: float = None,
            table=None, fetchers: dict = None, cofid: CofidTable = None,
            hint: dict = None, queries=None, on: date = None, exclude=()) -> dict:
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
    hint = hint or {}
    # Search the INTERPRETED terms, not the athlete's sentence. "400mg of my protein
    # collagen capsules" is a poor query; "collagen peptides" is a good one.
    search_queries = [q for q in (queries or hint.get("search_terms") or [raw_text]) if q]
    key = (raw_text or "").strip().lower()
    on = on or (date.fromisoformat(str(day)[:10]) if day else date.today())

    def record(rung, outcome, detail=""):
        attempts.append({"rung": rung, "outcome": outcome, "detail": detail})

    # cache first, and a hit short-circuits everything below it
    if store is not None:
        hit = store.cache_get(key, on=on)
        rejected = _excluded_by(hit.get("resolved_name") or "", exclude) if hit else ""
        if hit and rejected:
            # The cache is the rung most likely to hold the thing he just rejected: it is
            # keyed on his own words, and a wrong answer he once confirmed is exactly what
            # gets re-served for a year. Skipping it here re-walks the ladder rather than
            # handing back the same mistake instantly.
            record(Rung.CACHE, "excluded_by_athlete",
                   f"{hit.get('resolved_name')!r} matches {rejected!r}")
        elif hit:
            record(Rung.CACHE, "hit", f"resolved_at {hit.get('resolved_at')}")
            return _finalise(dict(hit), raw_text, Rung.CACHE,
                             hit.get("confidence", "estimate"), attempts, table, day,
                             degraded=False)
        else:
            record(Rung.CACHE, "miss", f"absent or older than {CACHE_MAX_AGE_DAYS} days")

    # CoFID is a local table, so wire it in automatically when present
    if Rung.COFID not in fetchers:
        cofid = cofid if cofid is not None else CofidTable()
        if cofid.available:
            fetchers[Rung.COFID] = lambda t, p, _c=cofid: _c.lookup(t, p)

    # CoFID is Public Health England's WHOLE FOOD composition table. Letting it answer a
    # branded product is how "M&S Satay Chicken with Black Rice & Mango" became
    # "Chicken, breast, skinless, raw" at 106 kcal, and the overnight oats pot became
    # "Oats, porridge, raw" - both wearing label confidence, both matched on one word.
    # Exactly the failure USDA was dropped for, in the rung I kept.
    category = (hint.get("category") or "").lower()
    skip = set()
    if category and category not in ("whole_food", "homemade", ""):
        skip.add(Rung.COFID)

    degraded = False
    for rung in effective_ladder(fetchers, cofid_ready=True):
        if rung in skip:
            record(rung, "skipped", f"a {category} is not in a whole-food table")
            continue
        fetch = fetchers.get(rung)
        if fetch is None:
            # Not built is NOT degradation: nothing failed. Conflating the two would
            # make a real outage look like normal operation.
            record(rung, "not_configured")
            continue
        try:
            got = None
            for q in search_queries:
                cand = fetch(q, portion_g)
                if not cand:
                    continue
                cname = cand.get("resolved_name") or ""
                # A REJECTED CANDIDATE IS A MISS, on every rung, and this check comes
                # FIRST. Checked before needs_portion in particular: a per-100g peanut
                # butter with no pack size would otherwise stop the ladder to ask "how
                # much?" about the very thing he had just said twice he never ate.
                rejected = _excluded_by(cname, exclude)
                if rejected:
                    record(rung, "excluded_by_athlete",
                           f"{cname!r} matches {rejected!r}, which he ruled out today")
                    continue
                if _hint_conflict(hint, cname):
                    record(rung, "wrong_form",
                           f"{cname!r} is not a {hint.get('form')}")
                    continue
                # Relevance is checked HERE, on every candidate from every rung, not
                # inside each fetcher. It used to live in the fetchers, so an injected or
                # newly added rung got no check at all and the guard was something each
                # future rung had to remember to call. The golden fixtures caught that on
                # their first run: a stubbed USDA returned "Soy protein isolate" for
                # collagen capsules and sailed straight through.
                # Relevance applies to LOOKUPS, not to estimates. A lookup has to return
                # the thing that was asked for; an estimate is BY DEFINITION about the
                # thing that was asked for, so its name may legitimately differ ("mixed
                # nuts (estimated)" for "something homemade"). Checking an estimate for
                # relevance would reject the fallback that exists for exactly the case
                # where nothing matches.
                estimating = (rung == Rung.LLM
                              or (cand.get("source_kind") or "").lower() == "estimate")
                if not estimating and not _relevant(q, cname):
                    record(rung, "irrelevant", f"{cname!r} does not match {q!r}")
                    continue
                got = cand
                break
        except Exception as exc:
            # A configured rung that FAILS is degradation and must be visible. This
            # is the difference between "not found" and "we did not really look".
            record(rung, "error", f"{type(exc).__name__}: {exc}")
            degraded = True
            continue
        if got and got.get("needs_portion"):
            per_100 = {k: v for k, v in (got.get("per_100g") or {}).items()
                       if v not in (None, "")}
            assumed, phrase = _default_portion(raw_text, got.get("resolved_name") or "")
            if assumed and per_100:
                # HE ALREADY SAID HOW MUCH, in the units that food is eaten in. Asking
                # "how much butter, in grams?" after "one teaspoon of butter" is the
                # question that left this resolution at kcal None with a perfectly good
                # label behind it. The confidence is NOT downgraded: the figures are still
                # the label's, and only the amount is assumed - which the offer states, so
                # a wrong default costs one message rather than being invisible.
                record(rung, "portion_assumed", f"{phrase}, {assumed:.0f} g")
                conf = got.get("confidence") or RUNG_CONFIDENCE[rung]
                if conf not in CONFIDENCE_LEVELS:
                    conf = RUNG_CONFIDENCE[rung]
                return _finalise({**got, **_scale(per_100, assumed),
                                  "portion_used_g": assumed,
                                  "portion_estimated": True,
                                  "portion_assumed": f"{assumed:.0f} g - {phrase}"},
                                 raw_text, rung, conf, attempts, table, day,
                                 degraded=degraded)
            # A rung found the right product but cannot know how much was eaten. That is
            # a question, not a result: it is recorded and the ladder stops, because a
            # lower rung guessing would overwrite a good label with a worse guess.
            record(rung, "needs_portion", got.get("resolved_name") or "")
            out = _finalise(got, raw_text, rung, "label", attempts, table, day,
                            degraded=degraded)
            out.update({f: None for f in MACRO_FIELDS})
            out["needs_input"] = True
            out["needs_portion"] = True
            out["per_100g"] = got.get("per_100g") or {}
            return out
        if got:
            record(rung, "hit", got.get("source_kind") or "")
            if rung == Rung.COFID and portion_g is None:
                # A CoFID hit with no portion IS per-100g wearing a finished look:
                # CofidTable._scale leaves the figures unscaled when portion_g is None,
                # so "one teaspoon of butter" without an interpreted portion would log
                # 100 g of butter (744 kcal) without anyone having said so. Same
                # assumption as the needs_portion path above: if his words name the
                # amount in the units the food is eaten in, scale to it and say so.
                assumed, phrase = _default_portion(raw_text,
                                                   got.get("resolved_name") or "")
                if assumed:
                    record(rung, "portion_assumed", f"{phrase}, {assumed:.0f} g")
                    factor = assumed / 100.0
                    scaled = {k: round(got[k] * factor, 1)
                              for k in MACRO_FIELDS if got.get(k) is not None}
                    if got.get("dietary_sodium_mg") is not None:
                        scaled["dietary_sodium_mg"] = round(
                            got["dietary_sodium_mg"] * factor)
                    got = {**got, **scaled, "portion_used_g": assumed,
                           "portion_estimated": True,
                           "portion_assumed": f"{assumed:.0f} g - {phrase}"}
            # A rung may declare its own confidence: `web` is label data when it lands on
            # a manufacturer or retailer page and an estimate when it does not.
            conf = got.get("confidence") or RUNG_CONFIDENCE[rung]
            if conf not in CONFIDENCE_LEVELS:
                conf = RUNG_CONFIDENCE[rung]
            return _finalise(got, raw_text, rung, conf, attempts,
                             table, day, degraded=degraded)
        record(rung, "no_match")

    if store is not None:
        store.log_unresolved(raw_text, day=day)
    out = _finalise({}, raw_text, Rung.LLM, "estimate", attempts, table, day,
                    degraded=degraded)
    out["needs_input"] = True
    out.update({f: None for f in MACRO_FIELDS})
    return out


# Keys a fetcher may set that MUST survive finalisation.
#
# The dict _finalise returns is an allowlist, so anything a fetcher computes and this
# tuple does not name is dropped in silence. That has now happened three times - the
# matched species score, the `provisional` diversity flag, and the vendor rung's
# modifier note, which was computed correctly and never reached the confirm message.
# Naming them in one place makes the next addition a one-line change instead of a
# silent loss.
PASSTHROUGH_FIELDS = ("note", "vendor", "components", "swaps", "modifiers_unaccounted",
                      "per", "pack_g", "portion_used_g", "sodium_from_salt",
                      # An ASSUMED portion has to reach the offer text, or the assumption
                      # is made silently - which is the thing the default was allowed on
                      # condition of never doing.
                      "portion_estimated", "portion_assumed",
                      # The per-100g basis is what makes "I had 160g" a multiplication
                      # instead of a fresh search (13 Aug 2026, the tortilla label).
                      "per_100g")


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
        # match_food, not match_text: an ultra-processed product contributes no plants
        # whatever its ingredient list name-drops. A protein bar listing "cocoa" is not a
        # serving of cacao, and no refined form fixes that - "cocoa" is a genuine
        # whole-food word doing nothing for him inside that bar.
        res = table.match_food(subject, ingredients=ingredients or None)
        # id AND matched score: a refined form scores 0 and must stay 0 when read back.
        species = [{"id": s["id"], "score": s["score"]} for s in res["species"]]
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
        "species_suppressed": (res.get("species_suppressed") if table is not None
                               else None),
        "processing_markers": (res.get("processing_markers") if table is not None
                               else None),
        "degraded": degraded,
        "attempts": attempts,
        "needs_input": False,
        "sodium_confidence": (confidence if got.get("dietary_sodium_mg") is not None
                              else "unknown"),
    }
    for f in MACRO_FIELDS:
        out[f] = got.get(f)
    for f in PASSTHROUGH_FIELDS:
        if got.get(f) is not None:
            out[f] = got[f]
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
    if item.get("portion_estimated"):
        # AN ASSUMED PORTION HAS TO SURVIVE THE CACHE. The macros here were scaled by a
        # default, and the cache payload is an allowlist, so without these three keys the
        # second time he says "a teaspoon of butter" the cache hit renders as plain label
        # data with the assumption silently dropped - which is the one thing the default
        # portions were allowed on condition of never doing. Same allowlist trap as the
        # dropped species score and the dropped vendor note.
        payload.update({"portion_estimated": True,
                        "portion_assumed": item.get("portion_assumed"),
                        "portion_used_g": item.get("portion_used_g")})
    store.cache_put((item.get("raw_text") or "").strip().lower(), payload)


def describe_provenance(item: dict) -> str:
    """One short line for the bot's confirm message. The rung is always stated: an
    estimate must never look like label data, and a degraded resolution must say so."""
    if item.get("needs_input"):
        return "Could not resolve this one. Give me the pack figures?"
    label = {Rung.VENDOR: ("from " + (item.get("vendor") or "the chain")
                           + "'s published nutrition"),
             Rung.CACHE: "from your saved items",
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
    if item.get("note"):
        # An unaccounted modifier changes the number materially - "extra salmon" is 282
        # kcal at Wagamama - so it belongs on the line he reads BEFORE confirming, not
        # only in the stored record.
        bits.append(item["note"])
    if item.get("degraded"):
        failed = [a["rung"] for a in item.get("attempts", []) if a["outcome"] == "error"]
        bits.append("a better source failed: " + ", ".join(failed))
    return " - ".join(bits)


def ladder_status(fetchers: dict = None, cofid: CofidTable = None) -> dict:
    """Which rungs will actually be walked, in order, and which are off.

    Reports every rung, not just the default ones, so "off by default" is visible rather
    than looking like a missing feature."""
    fetchers = fetchers or {}
    cofid = cofid if cofid is not None else CofidTable()
    out = {}
    for rung in FULL_ORDER:
        if rung == Rung.COFID:
            out[rung] = "ready" if (Rung.COFID in fetchers or cofid.available) \
                else "not_configured"
        elif rung in fetchers:
            out[rung] = "ready"
        elif rung in LADDER:
            out[rung] = "not_configured"
        else:
            out[rung] = "off_by_default"
    return out
