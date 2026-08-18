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

THE CACHE IS KEYED ON THE PRODUCT, NOT ON THE SENTENCE HE TYPED
Nobody says a thing the same way twice, and the cache key was literally his words - so a
photographed label saved as "One cookie" was unreachable the next day as "a whole matcha
cookie", and the best-graded row this module can hold was re-guessed from scratch every
time it was rephrased. A row is now keyed on the resolved product and the amount, with
his words kept as an alias pointing at it. See "the cache key" further down for the
guards that stop a looser lookup reaching the wrong product, which is the risk this
trades against.

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


def _token_list(text: str) -> list:
    """The identifying words, IN THE ORDER THEY WERE SAID.

    Split out of `_tokens` (which is a set and always was) because the cache key needs
    the LAST identifying word of a name - English puts the product type at the end, so
    "peanut butter" and "peanut butter protein bar" are told apart by nothing else. A
    set has no last element, and `max()` or `sorted()[-1]` of one is alphabetical order
    wearing the look of word order."""
    out = []
    for w in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(w) >= 3 and w not in _STOPWORDS and not w.isdigit():
            out.append(w.rstrip("s") if len(w) > 4 and w.endswith("s") else w)
    return out


def _tokens(text: str) -> set:
    return set(_token_list(text))


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


# How a composition row states that it is NOT the food as eaten, and how a query states
# that it is. Matched against the raw name string rather than through _tokens, because
# "raw" is a stopword there and therefore invisible to the tokeniser.
#
# "dried" is a raw-state word for a food that is REHYDRATED by cooking, and PHE writes the
# cooked rows as "dried, boiled in unsalted water" - so a cooked word anywhere in the name
# settles it, and only a row with no cooked word at all counts as raw. That also keeps
# genuinely dried foods reachable: "dried apricots" names no cooked state, so nothing here
# applies to it.
_RAW_STATE_WORDS = ("raw", "dried", "uncooked", "dry weight", "as purchased")
_COOKED_STATE_WORDS = (
    "cooked", "boiled", "steamed", "fried", "grilled", "griddled", "roast", "baked",
    "poached", "barbecued", "braised", "casseroled", "stewed", "microwaved", "toasted",
    "as served", "as eaten", "takeaway", "homemade", "slow cooked", "reheated")


def _has_word(text: str, words) -> bool:
    low = (text or "").lower()
    return any(w in low for w in words)


def _is_raw_row(name: str) -> bool:
    """True when this table row is the food BEFORE it was cooked."""
    return (_has_word(name, _RAW_STATE_WORDS)
            and not _has_word(name, _COOKED_STATE_WORDS))


def _wants_cooked(query: str) -> bool:
    """True when the query asks for a cooked food and does not ask for a raw one.

    A query that says "dried" or "raw" itself is asking for exactly that and must be left
    alone - "dried apricots" and "raw carrot" are things people eat."""
    return (_has_word(query, _COOKED_STATE_WORDS)
            and not _has_word(query, _RAW_STATE_WORDS))


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
        # THE STATE HE ATE IT IN. PHE holds a row per state and they are different foods:
        # "Noodles, egg, dried, raw" is 338 kcal per 100 g, the boiled row 166. On
        # 14 Aug 2026 a stir-fry was priced from the DRIED noodle row and a RAW steak row,
        # which is not a meal anyone has ever eaten. An exact or alias hit is normally
        # authoritative and skips the candidate loop entirely, so a raw row reached by name
        # has to be sent back through it to look for a cooked sibling of the same food.
        if food is not None and _wants_cooked(q) and _is_raw_row(food.get("name")):
            food = None
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
            cooked_wanted = _wants_cooked(q)
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
                    # STATE OUTRANKS EVERY OTHER PREFERENCE for a query that names a cooked
                    # food. On overlap and coverage alone the raw row WINS - it is the
                    # shortest name, so it adds least - which is precisely how the dried
                    # noodle row beat the boiled one. The cooked row's extra words
                    # ("boiled in unsalted water") count against it under every other
                    # measure here, so the preference has to sort ahead of them.
                    cooked_ok = 0 if (cooked_wanted and _is_raw_row(f.get("name"))) else 1
                    hits.append((cooked_ok, len(shared), -len(nt - qt), name, f))
            if not hits:
                return None
            # Best row: the right state, then most of the query explained, then the row that
            # adds the LEAST the query did not ask for. Preferring the longest name instead
            # picked the most embellished row, so a bare "almonds" could land on a roasted
            # salted one.
            hits.sort(reverse=True)
            food = hits[0][4]
            if cooked_wanted and _is_raw_row(food.get("name")):
                # He ate it cooked and this table has only the raw form. Returning it would
                # put a raw figure in a food log wearing `label` confidence; falling through
                # sends the query to the web rung, which can find a cooked figure. A missing
                # rung is recoverable, a confidently wrong one is not.
                return None
            if not _relevant(query, food.get("name") or ""):
                return None
        per_100 = {f: food.get(f) for f in MACRO_FIELDS if food.get(f) is not None}
        sodium = per_100.pop("dietary_sodium_mg", None)
        out = _scale(per_100, portion_g)
        if sodium is not None:
            out["dietary_sodium_mg"] = round(sodium * ((portion_g or 100.0) / 100.0))
        # THE BASIS TRAVELS WITH THE FIGURES. Every other rung that scales a portion hands
        # back the per-100g row it scaled from, and this one did not - so a whole food had no
        # basis at all, and "make the noodles 1.5x" could only be applied as a blind ratio
        # while "300 g of that" could not be applied at all (rescale_item returns None with
        # neither a basis nor a portion). It also lets the offer say what portion it used.
        if per_100 or sodium is not None:
            basis = dict(per_100)
            if sodium is not None:
                basis["dietary_sodium_mg"] = sodium
            out["per_100g"] = basis
        if portion_g:
            out["portion_used_g"] = float(portion_g)
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


# A MACRO HE STATED WITHOUT A KCAL FIGURE (17 Aug 2026). "chicken salad with 21g protein"
# gives one number and describes the rest, and it was answered with a composition table's
# protein: his own figure discarded in silence, which is the one rule this whole area
# exists to keep. Two separate rules had been tangled into one test for kcal.
#
# The rule that was RIGHT: a block with no energy figure must not take the verbatim path.
# There is no total to log, so it is a description, and a description belongs on the
# ladder. That rule now lives with the callers that route to that path, which test kcal
# themselves. The rule that was WRONG: throwing the figures away. So the ladder still
# runs, and his numbers are laid over the top of whatever it found.
#
# Nothing is invented on the way. No kcal is derived from macros by Atwater factors - a
# computed total is indistinguishable, a week later, from one he gave, which is the same
# objection stated_macros already makes about rounding - and no macro he said nothing
# about is touched, so the lookup keeps the rest of its row.
#
# Three things the overlay must NOT do, each of them a way of trading this bug for a
# quieter one:
#   - a stated kcal of 0 is an ABSENCE, not a statement, and is dropped. Zeroing a real
#     lookup's energy is the "a zero-calorie entry looks like data" failure this module's
#     docstring is arranged against. A zero for any OTHER macro is a real figure: "no fat
#     in it" is something he can truthfully say, and there is nothing to lose by taking it.
#   - the rung and the confidence are UNCHANGED, in BOTH directions. Precedent is the
#     assumed-portion guard: the rest of the figures are still the source's. His "21 g off
#     the pack" must not relabel the table's kcal and carbs as label data either.
#   - an overlaid item is NEVER CACHED (see cache_resolved), or his 21 g comes back a week
#     later against words he stated nothing about, wearing the source's confidence.
def _stated_overlay(stated) -> dict:
    """The macro fields the ATHLETE supplied, cleaned, or {} when he supplied none.

    Deliberately re-checked here rather than trusted from the caller: resolve is the last
    place that can tell a figure of his from a figure of ours, and a bad value reaching
    the overlay would be written over good data instead of merely being dropped."""
    out = {}
    for field, raw in (stated or {}).items():
        if field not in MACRO_FIELDS:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # Negative is nonsense and zero energy is an absence. Neither is clamped or
        # repaired - a "corrected" figure here would read, in the log, as one he gave.
        if value < 0 or (field == "kcal" and not value):
            continue
        out[field] = value
    return out


# --- the cache key ----------------------------------------------------------
#
# WHAT THE CACHE IS KEYED ON, AND WHY IT STOPPED BEING HIS SENTENCE (18 Aug 2026).
# The key was `raw_text.strip().lower()` at both ends - literally the words he happened
# to type. A photographed Co-op cookie label went in on 16 Aug under "One cookie", so it
# was saved as `"one cookie"`; the next day the same biscuit was "a whole matcha cookie",
# "same Co-op item as yesterday" and "the matcha white choc one", and every one of them
# missed. Nobody says a thing the same way twice, so the best-graded row this module can
# hold - a label he photographed and confirmed - was unreachable the moment he rephrased,
# and the ladder re-guessed it from scratch each time. That was the root of a 17-hour,
# multi-photo saga in which the cookie never actually got logged.
#
# So the row is keyed on the PRODUCT, and his words become an alias pointing at it:
#
#   "chocolate cookie matcha white#x1"   <- the payload. identity # amount
#   "one cookie"                         -> {"alias_of": "chocolate cookie matcha white#x1"}
#
# IDENTITY is the resolved_name's identifying tokens, sorted: lower-cased, punctuation
# and word order gone, stopwords and quantity words dropped by the same tokeniser the
# rest of this module matches with. resolved_name is not stable WORDING - the rungs build
# it differently ("brands + product_name" at OFF, `description` at USDA, the published
# PHE name at CoFID, whatever the page said at the web rung) - but it is stable IDENTITY,
# which is all a key needs. Where two spellings do differ ("Oat, rolled" against "Oats,
# rolled" - `_tokens` only strips a plural over four letters) the cost is an extra row and
# a miss, never a wrong hit.
#
# AMOUNT is in the key because the payload is a resolution of a PORTION, not of a food.
# Identity alone would file "50 g of oats" and "150 g of oats" as one row, the second
# would overwrite the first, and the first phrasing would then answer with the second's
# macros - a silent trebling, which is worse than the miss this whole change is about.
# It is read off his words at BOTH ends and never from the `portion_g` the interpreter
# supplies, because the two ends have to agree: an asymmetric key buys no safety, only
# permanent misses.


# Explicit amounts. Longest alternative first so "grams" cannot be read as "g".
_MEASURE = re.compile(r"(\d+(?:\.\d+)?)\s*"
                      r"(kgs?|grams?|millilitres?|litres?|ounces?|oz|ml|[gl])\b")
_UNIT_CANON = {"g": "g", "gram": "g", "grams": "g", "kg": "kg", "kgs": "kg",
               "ml": "ml", "millilitre": "ml", "millilitres": "ml",
               "l": "l", "litre": "l", "litres": "l",
               "oz": "oz", "ounce": "oz", "ounces": "oz"}
_COUNT_ANY = re.compile(r"\b(\d+(?:\.\d+)?|"
                        + "|".join(sorted(_COUNT_WORDS, key=len, reverse=True))
                        + r")\b")
# Size words are quantity words that carry no number, and they are all STOPWORDS to the
# tokeniser - deliberately, since "large" says nothing about what a product IS. That is
# exactly why they have to be caught here instead: without them "a small bowl of porridge"
# and "a big bowl of porridge" have the same identity AND the same count, and the cache
# would answer one with the other's figures.
#
# "whole" is NOT in here, though it looks like it belongs. "a whole matcha cookie" means
# one cookie, which is the default already, and treating it as a size word would put that
# phrasing in a different row from "one cookie" - the very miss this change exists to fix.
_SIZE_WORDS = ("small", "little", "mini", "medium", "large", "big", "huge", "giant",
               "double", "triple")


def _amount_key(text: str) -> str:
    """How much he said, canonically: "75g x1", "large x1", "x2", "x0.5".

    Never empty, so a phrase that names no amount at all ("porridge oats") files under
    the same "x1" as one that says "a portion of porridge oats" - the count IS one in
    both, and splitting them would cost a hit for no gain."""
    said = (text or "").lower()
    measures, spans = [], []
    for m in _MEASURE.finditer(said):
        spans.append(m.span())
        measures.append(f"{float(m.group(1)):g}{_UNIT_CANON.get(m.group(2), m.group(2))}")
    count = 1.0
    for m in _COUNT_ANY.finditer(said):
        # A number that belongs to a measure is not a count: "75 g" is one portion of
        # 75 grams, not seventy-five of something.
        if any(s <= m.start() < e for s, e in spans):
            continue
        # Nor is a clock time. He logs at times ("half a cookie at 13:50"), and reading
        # the hour as a count files the row where nothing will ever find it again.
        if said[m.start() - 1:m.start()] == ":" or said[m.end():m.end() + 1] == ":":
            continue
        tok = m.group(1)
        try:
            n = float(tok) if tok[0].isdigit() else float(_COUNT_WORDS.get(tok, 1))
        except ValueError:
            n = 1.0
        # Same bounds as `_stated_count`: outside them it is a year, a price or a typo.
        # KEEP LOOKING rather than settling for the default, which is the whole difference
        # between "at 1350, half a cookie" filing as x0.5 and filing as x1 - and x1 is the
        # row for a WHOLE cookie, so stopping here would serve double what he ate. A
        # nonsense number is not a statement about how much; it is not a number at all.
        if 0.25 <= n <= 12:
            count = n
            break
    sizes = [w for w in _SIZE_WORDS if re.search(rf"\b{w}\b", said)]
    return " ".join(sorted(measures) + sorted(sizes) + [f"x{count:g}"])


def _identity(text: str) -> str:
    """The identifying words of a name, sorted - the product, with the phrasing gone."""
    return " ".join(sorted(_tokens(text)))


def _head_noun(text: str) -> str:
    """The LAST identifying word, which in English is what the thing IS.

    The discriminator that keeps a loose match honest. Everything else about "peanut
    butter" is shared with "Peanut Butter Protein Bar" - same tokens, one a subset of the
    other, "protein" a stopword - and the only thing that says they are different foods is
    that one is a butter and the other is a bar.

    Read off the FIRST COMMA-SEGMENT for the same reason `CofidTable.lookup` reads a row's
    identity there: a published name states what the food is, then qualifies it, so
    "Peanut butter, smooth" is a butter and "Bread, wholemeal" is a bread."""
    words = _token_list((text or "").split(",")[0])
    return words[-1] if words else ""


def cache_keys(resolved_name: str, raw_text: str) -> tuple:
    """(the key the payload lives under, the alias keys pointing at it).

    His exact words stay as an alias so the commonest case - saying the same thing again -
    is still one dict lookup and still hits, including on a cache file written before any
    of this existed, where his words ARE the key."""
    raw_key = (raw_text or "").strip().lower()
    identity = _identity(resolved_name)
    if not identity:
        # Nothing identifying in the name (a bare "300 kcal", a number). There is no
        # product to key on, so it files under his words exactly as it always did.
        return raw_key, ()
    return f"{identity}#{_amount_key(raw_text)}", ((raw_key,) if raw_key else ())


# How many identifying words a phrase must carry before it is allowed to match a saved
# product it does not name in full. One is not enough and never will be: "butter" against
# a saved "Peanut butter, smooth" is this module's own worked example of a one-word match
# reaching the wrong food, and it happened six times on 12 Aug 2026.
_CACHE_MIN_TOKENS = 2


def _bare_plural(raw_text: str, name: str, amount: str) -> bool:
    """He said MORE THAN ONE of this and did not say how many.

    A BARE PLURAL IS A COUNT QUESTION, NOT A PORTION. `_default_portion` refuses to read
    "bananas" as one banana for precisely this reason, and a cached row is the figures for
    ONE stated amount: answering "matcha cookies" with the row saved for one cookie halves
    the entry, and does it quietly, which is the failure this module is arranged against.
    How many is a question the ladder can ask and a cache lookup cannot.

    An explicit count ("two matcha cookies") never gets here - it is a different amount
    and therefore a different row. Only the AMBIGUOUS plural is refused."""
    head = _head_noun(name)
    return bool(head and amount.endswith("x1")
                and re.search(rf"\b{re.escape(head)}s\b", (raw_text or "").lower()))


def _cache_candidate(store, raw_text: str, queries, *, on, hint, exclude):
    """The saved resolution for this food, however he worded it today.

    Returns (payload, how, blocked): the row, a phrase for the attempt log saying HOW it
    was matched, and the exclusion that stopped an otherwise-good row - so the caller can
    still record `excluded_by_athlete` rather than a bare miss.

    Three lookups, loosest last:
      1. his exact words        - one dict hop, and what an old cache file holds
      2. the same identity      - "matcha white chocolate cookie" for the same product
      3. a contained identity   - everything he named is in the product's name

    Only (3) can reach a product he did not fully name, so only (3) carries the guards:
    at least two identifying words, the same head noun, the same amount, no form conflict,
    and - if two saved products both fit - a MISS. Ambiguity resolved by dict order would
    be a coin toss wearing label confidence, and re-walking the ladder costs one lookup."""
    raw_key = (raw_text or "").strip().lower()
    amount = _amount_key(raw_text)
    # His words first, and his words for THIS item ahead of the interpreter's search
    # terms, which are a rewrite of them.
    phrasings = [q for q in [raw_text] + list(queries or []) if q]
    blocked = ""

    def offer(payload, how):
        """The row unless he has ruled it out today, remembering the ruling either way."""
        nonlocal blocked
        name = (payload or {}).get("resolved_name") or ""
        rejected = _excluded_by(name, exclude)
        if rejected:
            # The cache is the rung most likely to hold the thing he just rejected: a
            # wrong answer once confirmed is exactly what gets re-served for a year.
            blocked = blocked or f"{name!r} matches {rejected!r}"
            return None
        return payload, how

    hit = store.cache_get(raw_key, on=on) if raw_key else None
    if hit:
        got = offer(hit, "")
        if got:
            return got + (blocked,)

    rows = store.cache_rows(on=on)
    by_key = {k: p for k, p in rows}
    for phrase in phrasings:
        identity = _identity(phrase)
        if not identity:
            continue
        hit = by_key.get(f"{identity}#{amount}")
        # Stopwords and stripped plurals make this reachable on a plural: "Co-op Cookie"
        # keys as `cookie#x1` (co and op are too short to identify anything) and so does
        # "cookies". The exact-WORDS path above is exempt - a repeat of a sentence he
        # confirmed once is the contract the cache has always had - but everything looser
        # than that has to answer the same question about how many.
        if hit and not _bare_plural(raw_text, hit.get("resolved_name") or "", amount):
            got = offer(hit, f" - saved as {hit.get('resolved_name')!r}")
            if got:
                return got + (blocked,)

    found = []
    for key, payload in rows:
        name = payload.get("resolved_name") or ""
        known = _tokens(name)
        if not known:
            continue                      # not a resolution: nothing to match on
        # AMOUNT BEFORE AMBIGUITY. A saved "two cookies" row is not a candidate for a
        # one-cookie question at all, and counting it as one would make a single
        # unambiguous match read as a coin toss and fall through.
        if (payload.get("amount_key") or _amount_key(key)) != amount:
            continue
        head = _head_noun(name)
        if not head:
            continue
        if _bare_plural(raw_text, name, amount):
            continue
        for phrase in phrasings:
            said = _tokens(phrase)
            if len(said) < _CACHE_MIN_TOKENS or not said <= known:
                continue
            if _head_noun(phrase) != head:
                continue
            if _form_conflict(phrase, name) or _hint_conflict(hint, name):
                continue
            found.append((payload, name))
            break
    kept = [(p, n) for p, n in found if not _excluded_by(n, exclude)]
    if len(kept) < len(found):
        gone = next(n for p, n in found if _excluded_by(n, exclude))
        blocked = blocked or f"{gone!r} matches {_excluded_by(gone, exclude)!r}"
    distinct = {_identity(n) for p, n in kept}
    if len(distinct) > 1:
        return None, ("ambiguous: " + ", ".join(sorted({n for p, n in kept}))
                      + " all fit what you said"), blocked
    if kept:
        # One product, but an old flat file can hold it twice under two of his phrasings.
        # NEWEST WINS, rather than whichever the file lists first: the two were resolved
        # months apart, retailers reformulate, and a tie broken by dict order is a tie
        # broken by nothing.
        kept.sort(key=lambda pn: str(pn[0].get("resolved_at") or ""), reverse=True)
        return kept[0][0], f" - saved as {kept[0][1]!r}, matched on the product", blocked
    return None, "", blocked


def resolve(raw_text: str, *, day, store=None, portion_g: float = None,
            table=None, fetchers: dict = None, cofid: CofidTable = None,
            hint: dict = None, queries=None, on: date = None, exclude=(),
            stated: dict = None) -> dict:
    """Walk the ladder and return one resolved item plus a full attempt log.

    `fetchers` maps a rung name to a callable (text, portion_g) -> dict|None. Any
    rung absent from it is reported `not_configured` rather than silently skipped.
    `day` is the athlete's LOCAL date and is required: it stamps `resolved_at` and
    dates the review-queue entry, and this module never decides the local day itself
    (a UTC-dated write after 23:00 London lands on the wrong day).

    `stated` is any macro figure the ATHLETE gave for this item without giving a full
    specification. The ladder is walked exactly as it would be without it, and his
    figures are laid over the result at the end - see `_stated_overlay`.

    Never returns a bare failure. If every rung fails the result is still a usable
    record with `confidence: estimate`, macros None and `needs_input: True`, so the
    bot asks rather than logging zeroes - a zero-calorie entry is far more damaging
    to the record than an absent one, because it looks like data."""
    attempts = []
    overlay = _stated_overlay(stated)
    fetchers = dict(fetchers or {})
    hint = hint or {}
    # Search the INTERPRETED terms, not the athlete's sentence. "400mg of my protein
    # collagen capsules" is a poor query; "collagen peptides" is a good one.
    search_queries = [q for q in (queries or hint.get("search_terms") or [raw_text]) if q]
    on = on or (date.fromisoformat(str(day)[:10]) if day else date.today())

    def record(rung, outcome, detail=""):
        attempts.append({"rung": rung, "outcome": outcome, "detail": detail})

    # cache first, and a hit short-circuits everything below it
    if store is not None:
        hit, how, blocked = _cache_candidate(store, raw_text, search_queries,
                                             on=on, hint=hint, exclude=exclude)
        if hit:
            # WHICH WORDS FOUND IT is in the log, not just that something did. A row
            # reached through the product identity rather than through his own sentence
            # is the one that can be the wrong product, so it has to be readable
            # afterwards which of the two happened.
            record(Rung.CACHE, "hit", f"resolved_at {hit.get('resolved_at')}{how}")
            return _finalise(dict(hit), raw_text, Rung.CACHE,
                             hit.get("confidence", "estimate"), attempts, table, day,
                             degraded=False, stated=overlay)
        elif blocked:
            # He ruled this out today. Re-walking the ladder hands back something else
            # rather than the same mistake instantly.
            record(Rung.CACHE, "excluded_by_athlete", blocked)
        else:
            record(Rung.CACHE, "miss",
                   how or f"absent or older than {CACHE_MAX_AGE_DAYS} days")

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
                                 degraded=degraded, stated=overlay)
            # A rung found the right product but cannot know how much was eaten. That is
            # a question, not a result: it is recorded and the ladder stops, because a
            # lower rung guessing would overwrite a good label with a worse guess.
            record(rung, "needs_portion", got.get("resolved_name") or "")
            out = _finalise(got, raw_text, rung, "label", attempts, table, day,
                            degraded=degraded, stated=overlay)
            # Everything the ladder found is per-100g and unusable until he says how much,
            # so it is cleared - but a figure HE gave is for what he actually ate and does
            # not depend on the answer to that question. Blanking it here would discard
            # his number in exactly the case where it is one of the few we have.
            out.update({f: None for f in MACRO_FIELDS if f not in overlay})
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
                             table, day, degraded=degraded, stated=overlay)
        record(rung, "no_match")

    if store is not None:
        store.log_unresolved(raw_text, day=day)
    out = _finalise({}, raw_text, Rung.LLM, "estimate", attempts, table, day,
                    degraded=degraded, stated=overlay)
    out["needs_input"] = True
    # THE CASE HIS FIGURE MATTERS MOST IN. Every rung missed, so the only number anybody
    # has for this food is the one he gave, and an overlay applied on the success path
    # alone would lose it precisely here. The item still asks - a protein figure is not a
    # meal - but it asks while holding his 21 g rather than instead of it.
    out.update({f: None for f in MACRO_FIELDS if f not in overlay})
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
              day, degraded: bool, stated: dict = None) -> dict:
    """Shape one resolved item, tag species from its INGREDIENTS, and state how good
    the figures are.

    `stated` is the athlete's own figures for individual macros, already cleaned by
    `_stated_overlay`. Applied HERE, at the single point every path out of resolve goes
    through, rather than at each return: an overlay wired into the hits and forgotten on
    the miss would lose his number in the case it is most needed."""
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
    if stated:
        # HIS FIGURES GO ON LAST, over everything the ladder produced, including any
        # portion scaling above: what he stated is what he ate, not a per-100g basis to
        # be multiplied by an assumed teaspoon.
        out.update(stated)
        # WHICH of them were his. Without this the item is indistinguishable from a clean
        # lookup, and three things downstream need to tell them apart: the confirm line he
        # reads, the cache (which must refuse it), and anyone reading the record later.
        # Set on `out` directly rather than through PASSTHROUGH_FIELDS - it describes how
        # the item was obtained, not something a fetcher returned. Ordered as MACRO_FIELDS
        # is, not alphabetically: this list is read back to him in words, and "sodium,
        # protein" is not how anybody says it.
        out["stated_fields"] = [f for f in MACRO_FIELDS if f in stated]
        if "dietary_sodium_mg" in stated:
            # A sodium figure of his is a figure rather than an unknown, and it is HIS
            # RECKONING - graded as an estimate, never at the rung's grade. Sodium has its
            # own confidence field precisely because it is the figure that goes wrong
            # quietly, so this is the one place the overlay could overstate provenance:
            # taking `confidence` here would have made "about 800mg of salt in it" read as
            # label data off the back of a CoFID hit that returned no sodium at all.
            # `_stated_overlay` drops `basis` deliberately, so there is no way to know he
            # read it off a pack, and the safe direction is the modest one.
            out["sodium_confidence"] = "estimate"
        # Rule one of this module: the rung used is RECORDED on every entry. An overlay is
        # the one thing that can change a figure after the rung has answered, so it is in
        # the attempt log too, or the log would show a clean CoFID hit and a protein
        # figure CoFID never returned.
        attempts.append({"rung": rung, "outcome": "stated_override",
                         "detail": ", ".join(f"{f} {stated[f]:g}"
                                             for f in out["stated_fields"])
                                   + " - his own figures, kept over the lookup"})
    return out


def cache_resolved(store, item: dict) -> None:
    """Write a successful resolution back to the cache.

    Only label and database rungs are cached. Caching an LLM estimate would freeze
    one guess and re-serve it for a year as though it had been looked up, which is
    exactly the false confidence the ladder exists to prevent.

    WHAT IS SAFE TO CACHE IS UNCHANGED BY THE NEW KEY (18 Aug 2026). The three refusals
    below decide WHETHER a row is written and the key decides WHERE - they are separate
    questions and the key change touched neither. If anything the refusals matter more
    now: a row is reachable from more phrasings than the one it was saved under, so an
    estimate or a figure of his that slipped in here would be re-served against more
    sentences, not fewer."""
    if item.get("confidence") not in ("label", "database"):
        return
    if item.get("needs_input"):
        return
    if item.get("stated_fields"):
        # AN OVERLAID FIGURE IS HIS, ABOUT ONE MEAL, AND MUST NOT BE RE-SERVED (17 Aug
        # 2026). The cache is keyed on his words and stamped with the rung's confidence,
        # so caching "chicken salad with 21g protein" would hand back his 21 g for a year,
        # against a sentence in which he stated nothing, dressed as the source's own
        # figure. The same reasoning that keeps LLM estimates out of the cache: what gets
        # frozen here has to be a lookup, not a one-off.
        return
    raw_text = item.get("raw_text") or ""
    payload = {f: item.get(f) for f in MACRO_FIELDS}
    payload.update({"resolved_name": item.get("resolved_name"),
                    "ingredients": item.get("ingredients", ""),
                    "confidence": item.get("confidence"),
                    "source_rung": item.get("source_rung"),
                    "source_url": item.get("source_url", ""),
                    "species": item.get("species", []),
                    "resolved_at": item.get("resolved_at"),
                    # WHAT HE SAID, and how much of it he said he had. The key is the
                    # product now, so without these two the row loses every trace of the
                    # message that produced it - and cache.json is a file people read by
                    # hand when a figure looks wrong. `amount_key` is stored rather than
                    # re-derived at read time because it is what a search compares
                    # against, and re-deriving it from a key that is no longer his words
                    # would compare the wrong thing.
                    "raw_text": raw_text,
                    "amount_key": _amount_key(raw_text)})
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
    primary, aliases = cache_keys(item.get("resolved_name") or "", raw_text)
    store.cache_put(primary, payload, aliases=aliases)


# How a stated macro reads back to him. His own words for the macro, and the unit it is
# quoted in, so the line says "your own figure: protein 21 g" rather than "protein_g 21".
_STATED_LABELS = {"kcal": ("kcal", ""), "protein_g": ("protein", " g"),
                  "carb_g": ("carbs", " g"), "fat_g": ("fat", " g"),
                  "fibre_g": ("fibre", " g"), "dietary_sodium_mg": ("sodium", " mg")}


def _stated_phrase(item: dict) -> str:
    """"your own figure: protein 21 g", or "" when nothing was overlaid."""
    bits = []
    for f in item.get("stated_fields") or ():
        name, unit = _STATED_LABELS.get(f, (f, ""))
        value = item.get(f)
        bits.append(f"{name} {value:g}{unit}" if value is not None else name)
    if not bits:
        return ""
    return ("your own " + ("figures: " if len(bits) > 1 else "figure: ")
            + ", ".join(bits))


def describe_provenance(item: dict) -> str:
    """One short line for the bot's confirm message. The rung is always stated: an
    estimate must never look like label data, and a degraded resolution must say so."""
    if item.get("needs_input"):
        # SAY WHAT SURVIVED. Nothing resolved, but if he gave a figure it is on the item
        # and asking as though he had said nothing invites him to repeat himself - and
        # makes him doubt, reasonably, that his number was heard at all.
        kept = _stated_phrase(item)
        return ("Could not resolve this one"
                + (f", though I have {kept}" if kept else "")
                + ". Give me the pack figures?")
    label = {Rung.VENDOR: ("from " + (item.get("vendor") or "the chain")
                           + "'s published nutrition"),
             Rung.CACHE: "from your saved items",
             Rung.MANUAL: "from the pack, as you gave it",
             Rung.RETAILER: "from the retailer listing",
             Rung.COFID: "from the UK composition tables (CoFID)",
             Rung.USDA: "from USDA FoodData Central",
             Rung.OFF: "from Open Food Facts",
             Rung.NUTRITIONIX: "from Nutritionix",
             # The WEB rung had no phrase of its own, so a searched-out figure rendered as
             # the bare rung name - "web" - on the line he reads before confirming. Every
             # other rung says where it came from in words; this one is a real source (a
             # vendor page, a retailer listing, a published table) found by searching, and
             # saying so is the difference between a sourced figure and an unexplained one.
             Rung.WEB: "found online, from the product's own published figures",
             Rung.LLM: "estimated"}.get(item.get("source_rung"), item.get("source_rung"))
    bits = [label]
    if item.get("stated_fields"):
        # A MIXED ITEM HAS TO SAY SO. Most of this row came from the rung named above, one
        # figure came from him, and the line he confirms is the only place that difference
        # is ever visible - the stored entry carries one confidence for the whole row.
        # Named first among the qualifiers because it is the part he can actually check.
        bits.append(_stated_phrase(item) + ", kept as you gave it")
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
