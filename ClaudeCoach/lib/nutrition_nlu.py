#!/usr/bin/env python3
"""nutrition_nlu.py - intent understanding for the nutrition bot. Conversational front
end, deterministic back end.

Jamie's instruction, 10 Aug 2026: it must be conversational, NOT a fixed-format logger
like the expenses bot. So the front end understands whatever he types; the numbers
still come from the ladder and the engine.

THE LINE THIS MODULE DRAWS, AND WHY IT MATTERS
The model is used for PARSING and PHRASING only. It decides what a message means and
splits it into items; it never supplies a macro figure. Macros come from
nutrition_resolve's ladder, and totals come from the store. That keeps the ladder's
"LLM last" rule intact - a conversational front end must not become a back door for
estimated numbers wearing a confident sentence.

When answering a question, the model is handed the facts and told to use only those.
It phrases; it does not compute. A bot that invents a total is worse than one that
refuses, because the athlete has no way to catch it.

WHAT WENT WRONG WITHOUT THIS
The first cut treated any non-command text as food. "How much protein have I had?"
was therefore sent to the resolution ladder, resolved as a food item, and offered for
logging. That is exactly the failure this module exists to prevent, and it is why
intent is classified BEFORE anything is resolved.

FAST PATHS FIRST, MODEL SECOND
Bare numbers, slash commands and yes/no answers are handled deterministically, with no
model call and no latency. The model is only consulted when the message genuinely
needs interpreting. This also means the bot keeps working for weight and commands if
the model is unavailable, degrading to "I could not read that" only on free text.

MULTIPLE ITEMS IN ONE MESSAGE
"porridge with blueberries, and a flat white" is three loggable items, not one string.
The first cut resolved the whole sentence as a single food, which both mis-costed it
and lost the per-item provenance the confidence flag depends on.
"""

import json
import re
from datetime import datetime
from re import error
import subprocess

INTENTS = ("log_food", "log_weight", "log_supplement", "question", "advice",
           "correction", "command", "confirm", "cancel", "smalltalk", "secret",
           "unknown")

YES = {"y", "yes", "yep", "yeah", "ok", "okay", "sure", "go on", "do it", "log it",
       "confirm", "correct", "that's right", "thats right", "aye", "please do"}
NO = {"n", "no", "nope", "cancel", "nah", "forget it", "drop it", "leave it",
      "don't", "dont"}

_WEIGHT_RE = re.compile(
    r"(?:^|\b)(?:weigh(?:ed|t|ing)?(?:\s+in)?(?:\s+at)?|i(?:'m| am)|scales?(?:\s+said)?)?"
    r"\s*(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:kg|kgs|kilos?)?\b", re.I)

WEIGHT_MIN, WEIGHT_MAX = 40.0, 200.0

# Words that mean "this is a question about my data", used only as a cheap pre-filter
# before the model is asked. Deliberately not the decision-maker: a keyword list would
# misread "how much protein is in this bar" (a lookup, still food) as a question.
_QUESTION_HINT = re.compile(
    r"\?|^(how|what|whats|what's|where|when|why|which|do i|have i|am i|can i|should i|"
    r"tell me|show me|give me|remind me)\b", re.I)


def looks_like_weight(text: str):
    """A weight statement in natural language, or None.

    Bounded to a plausible human range because an unbounded parse turns a mistyped
    portion into a weight reading, and a bad weight moves the rolling mean the deficit
    is driven from. Rejects anything with food-ish context so "200g chicken" is not
      read as a 200 kg weigh-in."""
    t = (text or "").strip()
    if not t:
        return None
    if re.search(r"\b(g|gram|grams|ml|oz|cal|kcal|slice|bowl|pack|bag|portion)\b", t, re.I):
        return None
    m = _WEIGHT_RE.search(t)
    if not m:
        return None
    # The number must be essentially the whole message, or "83" inside a sentence
    # about something else becomes a weigh-in.
    stripped = re.sub(r"[^a-z0-9.]", "", t.lower())
    numeric = re.sub(r"[^0-9.]", "", m.group(1))
    if len(stripped) - len(numeric) > 24:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return val if WEIGHT_MIN <= val <= WEIGHT_MAX else None


_BARCODE_RE = re.compile(r"^\s*(\d{8}|\d{12,14})\s*$")


def looks_like_barcode(text: str):
    """A bare EAN-8, UPC-12, EAN-13 or GTIN-14. Returns the digits or None.

    No overlap with weight parsing: weights are 2-3 digits plus an optional decimal,
    barcodes are 8 or 12-14 digits with none."""
    m = _BARCODE_RE.match(text or "")
    return m.group(1) if m else None


# Credential-shaped input. Checked BEFORE the model is called and before anything is
# resolved, because a key sent to a food logger would otherwise become a food entry, and
# `resolved_name` IS published to a PUBLIC repo. The nutrition log itself is gitignored,
# but the published subset is not, so an un-resolvable key would have gone out as the
# item's name and into permanent public git history.
_SECRET_PATTERNS = (
    re.compile(r"\b(sk|pk|rk)[-_][A-Za-z0-9_\-]{16,}", re.I),      # stripe/openai style
    re.compile(r"\b(gh[pousr]|glpat)_[A-Za-z0-9_\-]{16,}"),         # github/gitlab
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}"),                  # telegram bot token
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),                            # bare 32+ char token
    re.compile(r"\b(api[_ -]?key|token|secret|password|bearer)\b\s*[:=]", re.I),
)


def looks_like_secret(text: str) -> bool:
    """True if the message looks like a credential.

    Deliberately loose on the tell and strict on the consequence: a false positive costs
    one confused reply, a false negative writes a key into a public repo. A bare 32+
    character alphanumeric run is not a food, so the width is affordable."""
    t = (text or "").strip()
    if len(t) < 16:
        return False
    return any(p.search(t) for p in _SECRET_PATTERNS)


# "that was breakfast", "the oats was breakfast", "log it as dinner". Deterministic on
# purpose: this is a two-word instruction and sending it to the model to classify was how
# it ended up as an unrecognised message.
_MEAL_WORD = r"(breakfast|lunch|dinner|brunch|supper|tea|snack|snacks)"
_SET_MEAL = (
    re.compile(r"^(?:that|this|it|they|those)\s+(?:was|were|is|are)\s+(?:my\s+)?"
               + _MEAL_WORD + r"$", re.I),
    re.compile(r"^(?:the\s+)?(?P<item>.{2,60}?)\s+(?:was|were|is|are)\s+(?:my\s+)?"
               + _MEAL_WORD + r"$", re.I),
    re.compile(r"^(?:log|make|mark|put|move|count)\s+(?:that|this|it|them|"
               r"(?:the\s+)?(?P<item2>.{2,60}?))\s+(?:as|for|to|into)\s+(?:my\s+)?"
               + _MEAL_WORD + r"$", re.I),
    # "make that a snack" - the article instead of "as", which is how people actually say
    # it for snacks specifically.
    re.compile(r"^(?:make|mark|count|call)\s+(?:that|this|it|them)\s+"
               r"(?:a|an|my)?\s*" + _MEAL_WORD + r"$", re.I),
)
# The four buckets the app renders, and the words he actually uses for them. Canonical
# here rather than in the store because this is the lowest layer - the store imports it,
# along with PRODUCT_FACT_FIELDS, for the same reason: a meal word this file accepts and
# the store then drops fails silently.
MEALS = ("breakfast", "lunch", "dinner", "snacks")
MEAL_ALIASES = {"brunch": "breakfast", "supper": "dinner", "tea": "dinner",
                "snack": "snacks", "snacking": "snacks"}


def normalise_meal(meal) -> str:
    """One of MEALS, or "" for anything that is not a meal name.

    Every path arrives with his own word for it - the fast regex, the parse prompt, a
    correction - and three copies of the alias table is how "supper" lands under snacks on
    one path and dinner on another."""
    m = str(meal or "").strip().lower()
    m = MEAL_ALIASES.get(m, m)
    return m if m in MEALS else ""


_SET_IN_SESSION = re.compile(
    r"^(?:that|this|it|they|those)\s+(?:was|were|is|are)\s+(?:all\s+)?"
    r"(?:in[-\s]session|during\s+the\s+\w+|on\s+the\s+(?:bike|ride|run|move)|"
    r"mid[-\s]?\w+|in\s+the\s+(?:run|ride|swim|session))\b",
    re.I)
_SET_OUT_SESSION = re.compile(
    r"^(?:that|this|it|they|those)\s+(?:was|were|is|are)\s+(?:not\s+in[-\s]session|"
    r"out\s+of[-\s]session|before\s+the\s+\w+|after\s+the\s+\w+)\b", re.I)


def looks_like_session_tag(text: str) -> bool | None:
    """True/False when he is placing an entry in or out of a session; None otherwise.

    Needed BECAUSE the flag now defaults to False on anything the words do not support -
    so there has to be a way to say "that was during the run" and have it counted."""
    t = (text or "").strip().rstrip(".!")
    if _SET_OUT_SESSION.match(t):
        return False
    if _SET_IN_SESSION.match(t):
        return True
    return None


# "delete that", "remove the sandwich", "get rid of the coop one", "take that off".
#
# THE BUG THIS EXISTS FOR. He photographed a barcode, it resolved to the wrong product at 382
# kcal, he said "Actually that's wrong" - and the reply was "Nothing pending to correct",
# because correction only ever applied to an unconfirmed item. Nothing was removed, so when he
# then sent the label the sandwich was logged TWICE. There was no way to delete a logged entry
# except /undo, which only reaches the last one.
_DELETE = (
    re.compile(r"^(?:please\s+)?(?:delete|remove|bin|scrap|discard)\s+"
               r"(?:that|this|it|them|those)\b", re.I),
    re.compile(r"^(?:please\s+)?(?:delete|remove|bin|scrap|discard)\s+"
               r"(?:the\s+)?(?P<item>.{2,60}?)\s*$", re.I),
    re.compile(r"^(?:take|get)\s+(?:that|this|it|them|(?:the\s+)?(?P<item2>.{2,60}?))\s+"
               r"(?:off|out|rid of)\b.*$", re.I),
    re.compile(r"^get\s+rid\s+of\s+(?:that|this|it|(?:the\s+)?(?P<item3>.{2,60}?))\s*$",
               re.I),
)


# "Actually that's not the right product. Remove it" - the instruction arrives at the END,
# after the reason. Anchoring the patterns to the start of the message meant this read as a
# CORRECTION, so the bot looked the wrong product up again instead of removing it, and Jamie
# had to send a second message to delete the thing he had just asked to be deleted.
_DELETE_TRAILING = re.compile(
    r"\b(?:remove|delete|bin|scrap|discard)\s+(?:it|that|this|them|those)\b\s*$"
    r"|\b(?:get\s+rid\s+of|take\s+off)\s+(?:it|that|this|them)\b\s*$"
    r"|\btake\s+(?:it|that|this|them)\s+off\b\s*$", re.I)


def looks_like_delete(text: str) -> dict | None:
    """{'item': ...} when he is asking for something to be removed."""
    t = (text or "").strip().rstrip(".!")
    # A trailing instruction wins over the sentence it follows: "that's not right, remove it"
    # is a deletion with a reason attached, not a correction.
    if _DELETE_TRAILING.search(t):
        return {"item": ""}
    for rx in _DELETE:
        m = rx.match(t)
        if not m:
            continue
        item = ""
        for key in ("item", "item2", "item3"):
            if key in rx.groupindex:
                item = (m.group(key) or "").strip()
                if item:
                    break
        return {"item": item}
    return None


def looks_like_meal_tag(text: str) -> dict | None:
    """{'meal': ..., 'item': ...} when he is naming which meal something was."""
    t = (text or "").strip().rstrip(".!")
    for rx in _SET_MEAL:
        m = rx.match(t)
        if not m:
            continue
        meal = normalise_meal(m.group(m.lastindex)) or m.group(m.lastindex).lower()
        item = ""
        for key in ("item", "item2"):
            try:
                item = (m.group(key) or "") if key in rx.groupindex else ""
            except (IndexError, error):
                item = ""
            if item:
                break
        # "chicken was lunch" is a meal tag; "that was lovely" is not, and the meal word
        # is what separates them - so an unmatched meal word means this is not one.
        return {"meal": meal, "item": item.strip()}
    return None


def fast_intent(text: str, has_pending: bool) -> dict | None:
    """Deterministic classification, no model call. None means "ask the model"."""
    t = (text or "").strip()
    low = t.lower().rstrip("!. ")
    if not t:
        return {"intent": "unknown"}
    if looks_like_secret(t):
        # Never reaches the model and never reaches the store.
        return {"intent": "secret"}
    if t.startswith("/"):
        return {"intent": "command", "command": low.split()[0]}
    if has_pending and low in YES:
        return {"intent": "confirm"}
    if has_pending and low in NO:
        return {"intent": "cancel"}
    code = looks_like_barcode(t)
    if code:
        return {"intent": "log_food", "barcode": code,
                "items": [{"text": code, "portion_g": None, "in_session": False}]}
    w = looks_like_weight(t)
    if w is not None:
        return {"intent": "log_weight", "weight_kg": w}
    gone = looks_like_delete(t)
    if gone is not None and not has_pending:
        return {"intent": "delete_entry", **gone}
    tag = looks_like_meal_tag(t)
    if tag and not has_pending:
        return {"intent": "set_meal", **tag}
    sess = looks_like_session_tag(t)
    if sess is not None and not has_pending:
        return {"intent": "set_in_session", "in_session": sess}
    return None


PARSE_PROMPT = """You are the message parser for a personal nutrition logging bot. \
Classify the message and extract structure. Reply with ONLY a JSON object, no prose.

Keys:
  intent: one of log_food, log_supplement, question, advice, correction, smalltalk,
          unknown
  items: array (log_food/log_supplement only) of {text, portion_g, in_session, at, meal,
         stated}
         - text: a SINGLE food or supplement, self-contained enough to look up
         - portion_g: grams if stated or confidently inferable, else null
         - stated: HIS OWN NUMBERS, when he has given them. Normally null. See
           "WHEN HE STATES THE NUMBERS" below - this is the one field where numbers are
           allowed, because they are his, not yours.
         - at: "HH:MM" (24h) ONLY when the message states a clock time for this item -
           "at 1350" -> "13:50", "8:30am" -> "08:30", "at half seven tonight" -> "19:30".
           NEVER guess: "this morning", "earlier", "before the ride" and anything else
           without a clock give null, and the logger stamps it with now. A guessed time
           is worse than no time, because the app files entries into meals by the clock.
         - ONE PRODUCT AND ITS CONTENTS IS ONE ITEM, NOT SEVERAL. "100ml ginger shot,
           orange, apple" is a single 100 ml shot whose ingredients are ginger, orange and
           apple - it is not a shot AND an orange AND an apple. This was split into three
           and would have logged two pieces of fruit he never ate on top of the drink.
           A comma-separated list following a NAMED product describes that product; use it
           to identify the product and put the whole phrase in ONE item's text. Only emit
           several items when they are separately EATEN things - "toast and a banana", "a
           coffee and two biscuits". If genuinely unsure, emit ONE item: an over-split day
           double counts, while an under-split one is a single figure he can correct.
         - meal: "breakfast" | "lunch" | "dinner" | "snacks", or null. Set it ONLY when
           the message SAYS which meal this was, or so clearly implies it that there is
           nothing to guess:
             "for breakfast I had porridge"        -> meal "breakfast"
             "porridge and a coffee, late lunch"   -> meal "lunch" on BOTH items
             "had a curry with dinner"             -> meal "dinner"
             "picked at some nuts mid-afternoon"   -> null (a time of day, not a meal)
           A CLOCK TIME IS EVIDENCE, NOT PROOF, so a stated time alone gives null: the
           logger files an untimed guess by the clock itself and marks it as a guess,
           which is honest, whereas a meal named here is treated as HIS OWN word for it
           and stops being questioned. "toast at 8:30" is `at` "08:30" and meal null.
           Two worked examples of the difference:
             "rye bread at 8:30 this morning" -> {"at":"08:30","meal":null}
             "rye bread for breakfast at 8:30" -> {"at":"08:30","meal":"breakfast"}
         - in_session: true only if eaten DURING a training session (gel, drink mix,
           "on the bike", "mid-run"). In-session fuel is never a meal: if in_session is
           true, meal is null.
  composed_meal: (log_food only) true when this is a MEAL SOMEBODY COOKED rather than a
          product with figures somewhere. See "A COMPOSED MEAL" below.
  question: (question only) a short restatement of what they are asking
  options: (advice only) array of the candidate foods/meals being weighed up, as
           plain lookup-able strings. Include every option mentioned, even in
           passing.
  correction: (correction only) what to change, e.g. "half the portion", "it was 2 not 1"
  note: optional short free text worth keeping

Rules:
  - Split multiple foods into separate items. "porridge with blueberries and a coffee"
    is three items.
  - "how much protein have I had" is a question. "how much protein is in a chicken
    breast" is a question too (they want a lookup, not a log).
  - Use advice when they are DECIDING rather than reporting: "should I have the pasta
    or the rice", "what should I eat before tomorrow's ride", "is a curry a bad idea
    tonight". Put every candidate in options.
  - Advice is NOT a log. Only move to log_food once they say what they actually had.
  - Only use log_food when they are telling you they ATE or DRANK something.
  - FOOD ALREADY EATEN WITH A TIME ON IT IS STILL log_food. "add second slice of toast
    with butter at 1350" is a log with at="13:50", not a correction and not a question.
  - If they are amending something just logged, use correction.
  - A FLAT STATEMENT ABOUT THE LOG IS A CORRECTION, not smalltalk. These look like
    remarks rather than instructions and were answered as unrecognised messages, so he
    had to get a human to edit the file:
      "the initial rye bread was 830am"      -> correction (it retimes an entry)
      "the 160g was a pack of bbq chicken"   -> correction (it renames one)
      "a rego scoop is half a portion"       -> correction (it states a lasting fact
                                                about a product)
    Anything that asserts something about an entry, a time, a name, or how big a
    product's scoop/portion/pack is, is a correction. What it CHANGES is decided later.
  - NEVER invent nutrition numbers. You extract text and portions only.

A COMPOSED MEAL IS ONE MEAL, AND NO DATABASE KNOWS IT.
Set composed_meal true when he describes food that was COOKED FROM PARTS - "a large stir
fry with egg noodles, a small steak, soy ginger garlic sauce and veg", "chilli con carne
with rice", "a fry-up", "chicken salad I made". Several things cooked or assembled
together, with no brand, no barcode and no label anywhere in it.

composed_meal is FALSE for:
  - a branded or packaged product, or anything with a barcode or a label ("M&S satay
    chicken", "a Nakd bar", "500ml Rubicon") - those have real published figures
  - a single whole food ("a banana", "200g chicken breast", "a handful of almonds") - the
    composition tables are authoritative for those and better than any estimate
  - a restaurant or takeaway order - the chain publishes its own nutrition
  - a supplement or a dose of anything
Items are still split as usual, because a message can hold a composed meal AND a coffee.
When composed_meal is true, put the WHOLE meal in ONE item's text, exactly as he described
it, wording and size words intact: a full-intelligence model reads that description and
tables the meal, and every word he used is evidence it needs.

WHEN HE STATES THE NUMBERS, THE NUMBERS ARE THE ANSWER.
  If the message carries an explicit energy figure with macros - "roughly 700 kcal, 40g
  protein, 60g carbs" - or a table of components with kcal against them, then he has
  already done the work and there is nothing to look up. Put his figures in `stated` and
  they are logged verbatim. Do not restate them differently, do not round them, do not
  reconcile a total against its parts, and do not convert anything.

  `stated` shape (omit any figure he did not give; kcal is required or it is not a
  specification at all):
    {"kcal": n, "protein_g": n, "carb_g": n, "fat_g": n, "fibre_g": n,
     "dietary_sodium_mg": n, "basis": "estimate"|"label", "components": ["<his rows>"]}
  basis is "label" ONLY when he says he read it off a pack or a label; his own reckoning,
  however detailed, is "estimate". `components` is his own per-part lines, copied as text,
  so the log keeps the breakdown without pretending each part was looked up.

  A PASTED TABLE IS ONE MEAL, NOT N LOOKUPS. This is the failure this rule exists for
  (14 Aug 2026): he pasted a full breakdown of a stir-fry, and it was read as five foods
  to search for - which re-priced his own meal at 2,400 kcal, including 100 g of oil at
  899 kcal. Emit ONE item for the meal, carrying HIS total, with the rows in
  `components`. Worked example:

    "Large stir-fry bowl ~980 kcal
     Egg noodles (300g cooked) 380 kcal, 12P, 75C, 3F
     Steak (100g) 220 kcal, 26P, 0C, 13F
     Soy/ginger/garlic sauce 80 kcal, 2P, 8C, 4F
     Vegetables (200g) 90 kcal, 4P, 15C, 1F
     Oil 210 kcal, 0P, 0C, 23F"
  ->
    {"intent":"log_food","items":[{"text":"large stir-fry bowl with egg noodles, steak,
      soy ginger garlic sauce, vegetables and oil","portion_g":null,"in_session":false,
      "at":null,"meal":null,
      "stated":{"kcal":980,"protein_g":44,"carb_g":98,"fat_g":44,"basis":"estimate",
        "components":["Egg noodles (300g cooked) 380 kcal, 12P, 75C, 3F",
                      "Steak (100g) 220 kcal, 26P, 0C, 13F",
                      "Soy/ginger/garlic sauce 80 kcal, 2P, 8C, 4F",
                      "Vegetables (200g) 90 kcal, 4P, 15C, 1F",
                      "Oil 210 kcal, 0P, 0C, 23F"]}}]}

  HIS HEADLINE TOTAL WINS over the sum of his rows. If the rows add to 968 and he wrote
  980, kcal is 980: he is telling you what the meal was, not setting an arithmetic
  exercise. Only add the rows up when he gave no total at all.
  Per-macro totals: use his stated totals when he gives them, otherwise the sum of the
  rows for that macro, otherwise omit the field.
  A bare "about 600 calories" with no macros is still `stated` - kcal only.
  A message that states its own figures is log_food EVEN WHEN it reads like a correction
  of something you just offered ("no - it was 980 kcal, 44P 98C 44F"). His figures replace
  the offer wholesale, so there is nothing to correct: it is a fresh, authoritative log.

Message: %s
"""


# The fields a stated specification may carry, with the largest value each may hold. A
# ceiling rather than a plausibility judgement: these are HIS figures and the code's only
# job is to catch a mis-read decimal point before it becomes a 40,000 kcal day.
_STATED_BOUNDS = {"kcal": 20000, "protein_g": 2000, "carb_g": 3000, "fat_g": 2000,
                  "fibre_g": 500, "dietary_sodium_mg": 40000}

# What the model might call each field if it does not use the store's own name. Mapped
# rather than dropped: every value here is one the athlete supplied, so losing one to a
# spelling is a silent loss of his own data, and there is no invention risk in accepting
# "carbs" for "carb_g".
_STATED_ALIASES = {
    "calories": "kcal", "cals": "kcal", "energy_kcal": "kcal", "energy": "kcal",
    "protein": "protein_g", "carbs": "carb_g", "carb": "carb_g",
    "carbohydrate": "carb_g", "carbohydrates": "carb_g", "carbs_g": "carb_g",
    "fat": "fat_g", "fats": "fat_g", "fibre": "fibre_g", "fiber": "fibre_g",
    "fiber_g": "fibre_g", "sodium": "dietary_sodium_mg",
    "sodium_mg": "dietary_sodium_mg",
}


def stated_macros(block) -> dict | None:
    """HIS OWN figures, validated but never recomputed, or None if this is not one.

    THE DEFECT THIS EXISTS FOR (14 Aug 2026). He pasted a complete macro table for a
    stir-fry - a total and a row per component - and every row was sent down the
    resolution ladder as a fresh lookup, which re-priced his 980 kcal meal at 2,400. A
    figure the athlete states is the most authoritative source there is: he ate it, and no
    composition table knows more about his dinner than he does.

    So this validator's whole contract is that it either passes his numbers through
    UNTOUCHED or refuses them. It does not derive kcal from macros, does not reconcile a
    total against its rows and does not round: a "corrected" figure here would be
    indistinguishable, in the log, from one he gave. kcal is required, because a
    specification with no energy figure is a description, and a description belongs on the
    ladder."""
    if not isinstance(block, dict):
        return None
    out = {}
    for key, raw in block.items():
        field = _STATED_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        if field not in _STATED_BOUNDS:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # A negative or out-of-range figure is DROPPED, not clamped: clamping would
        # invent a number and then present it as his.
        if 0 <= value <= _STATED_BOUNDS[field]:
            out[field] = value
    if not out.get("kcal"):
        return None
    basis = str(block.get("basis") or "").strip().lower()
    out["basis"] = "label" if basis == "label" else "estimate"
    out["components"] = [str(c).strip() for c in (block.get("components") or [])
                         if str(c or "").strip()][:20]
    return out


_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalise_hhmm(value) -> str | None:
    """"13:50" or a bare "1350" as "HH:MM", else None.

    Validation of a model field, in the same spirit as the `intent not in INTENTS`
    check: the model is asked for HH:MM, and anything that is not a real clock time is
    DROPPED rather than repaired, so the entry falls back to now-time. A half-read time
    is worse than no time - it files the entry into the wrong meal on the app and looks
    like something he typed."""
    s = str(value or "").strip()
    if not s:
        return None
    if s.isdigit() and len(s) in (3, 4):
        s = f"{s[:-2]}:{s[-2:]}"
    m = _HHMM.match(s)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def parse_with_model(text: str, claude_bin: str, model: str, log=print,
                     timeout: int = 60, runner=None) -> dict:
    """Ask the model what the message means. Returns {'intent': 'unknown'} on any
    failure, so an unparseable message never silently becomes a food entry."""
    runner = runner or subprocess.run
    try:
        proc = runner([claude_bin, "--print", "--model", model],
                      input=PARSE_PROMPT % text, capture_output=True, text=True,
                      timeout=timeout)
    except Exception as exc:
        log(f"nlu parse failed: {exc}")
        return {"intent": "unknown", "error": str(exc)}
    raw = (getattr(proc, "stdout", "") or "").strip()
    err = (getattr(proc, "stderr", "") or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        # Log the head of whatever came back. Returning unknown SILENTLY made a failing
        # model indistinguishable from an unreadable message, which is how an expired
        # OAuth token went unnoticed: the bot just kept saying it did not understand.
        log(f"nlu: no JSON in model reply; out={raw[:160]!r} err={err[:160]!r}")
        return {"intent": "unknown"}
    try:
        got = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        log(f"nlu: unparseable JSON: {raw[start:start + 160]!r}")
        return {"intent": "unknown"}
    if got.get("intent") not in INTENTS:
        log(f"nlu: unexpected intent {got.get('intent')!r}")
        return {"intent": "unknown"}
    items = []
    for it in got.get("items") or []:
        if isinstance(it, str):
            items.append({"text": it, "portion_g": None, "in_session": False,
                          "at": None, "meal": ""})
        elif isinstance(it, dict) and it.get("text"):
            in_session = (bool(it.get("in_session"))
                          and during_session_evidence(text))
            items.append({"text": str(it["text"]),
                          "portion_g": it.get("portion_g"),
                          # The meal HE named. Validated to one of the four buckets and
                          # otherwise dropped, like `at`: the logger's clock fallback is a
                          # better answer than a bucket nothing renders. In-session fuel
                          # is fuel, not a meal, so the flag wins over any meal word.
                          "meal": ("" if in_session
                                   else normalise_meal(it.get("meal"))),
                          # A STATED time only. "add the second slice of toast at 1350"
                          # was logged at the moment he typed it, so a log written up
                          # after the fact landed in the wrong meal on the app and there
                          # was no verb to move it.
                          "at": normalise_hhmm(it.get("at")),
                          # CONFIRMED, not asserted: the model's flag only survives if
                          # the words place the food in a session. Erring to False is the
                          # safe direction - an out-of-session item counted in the day is
                          # merely a day total, while a breakfast counted as in-run fuel
                          # rewrites the fuelling history the coach prescribes from.
                          "in_session": in_session,
                          # HIS FIGURES, if he gave any. Named here explicitly because
                          # this rebuild is an allowlist: a field the model returns and
                          # this loop does not copy is dropped in silence, which is the
                          # same hand-off bug that lost the photo hint and the species
                          # score. Validated, never recomputed.
                          "stated": stated_macros(it.get("stated"))})
    got["items"] = items
    return got


def classify(text: str, has_pending: bool, claude_bin: str, model: str, log=print,
             runner=None) -> dict:
    """Fast path, then the model. Intent is decided BEFORE anything is resolved."""
    fast = fast_intent(text, has_pending)
    if fast is not None:
        return fast
    qish = _QUESTION_HINT.search(text or "")
    logish = re.search(r"\b(i (just )?(had|ate|drank|finished)\b|logged\b)", text or "",
                       re.I)
    # A trailing question mark WINS over the "sounds like a log" exclusion. Without
    # that precedence, "how much protein have I had?" matched `i had` and lost its
    # question fallback, so a failed model call sent it to the resolution ladder as
    # food - the exact bug this module exists to prevent.
    if qish and ((text or "").strip().endswith("?") or not logish):
        # Pre-filter only: the model still decides, but this stops an obvious question
        # being resolved as food if the model call fails.
        got = parse_with_model(text, claude_bin, model, log=log, runner=runner)
        if got.get("intent") == "unknown":
            return {"intent": "question", "question": text}
        return got
    got = parse_with_model(text, claude_bin, model, log=log, runner=runner)
    # A MESSAGE CARRYING HIS OWN FIGURES IS FOOD, and none of the reroutes below apply to
    # it. A pasted macro table mentions milligrams of sodium, which tiny_dose_mg reads as a
    # supplement dose and would have announced as "nutritionally negligible" about a
    # 980 kcal dinner.
    if any((i.get("stated") or {}).get("kcal") for i in got.get("items") or []):
        got["intent"] = "log_food"
        got["stated"] = True
        return got
    # A dose form overrides a log_food classification. The model reads "had 400mg of my
    # collagen capsules" as eating, which it is, but the SUPPLEMENT path is the one that
    # records a dose and keeps collagen out of the protein target.
    if got.get("intent") == "log_food" and looks_like_supplement(text):
        got["intent"] = "log_supplement"
        got["form_detected"] = True
    # A SCOOP of something with real macros is food, whatever the spoon is called.
    # "1 scoop sis rego chocolate" was forced to the supplement path by the word
    # "scoop" and never got macros; he had to say "It's a food" and got nowhere
    # (13 Aug 2026). Recovery/carb/protein drink mixes carry meaningful energy and
    # belong on the ladder.
    if got.get("intent") == "log_supplement" and re.search(
            r"\b(whey|protein\s+powder|rego|recovery\s+(?:drink|shake|mix)|"
            r"carb(?:ohydrate)?\s+(?:drink|mix|powder)|maurten|beta\s+fuel|"
            r"drink\s+mix|mass\s+gainer|meal\s+replacement)\b", text or "", re.I):
        got["intent"] = "log_food"
        got["form_detected"] = False
    if got.get("intent") in ("log_food", "log_supplement"):
        mg = tiny_dose_mg(text)
        if mg is not None:
            got["dose_mg"] = mg
            got["nutritionally_trivial"] = mg < 2000
    if got.get("intent") == "unknown" and looks_like_eating(text):
        # Degrade to something useful. One unsplit item is worse than three items, and
        # far better than telling him his sentence was incomprehensible.
        log(f"nlu fell back to a single unsplit item: {text[:80]!r}")
        return {"intent": "log_food", "degraded": True,
                "items": [{"text": text, "portion_g": None, "in_session": False}]}
    return got


# An explicit statement of having eaten. Used ONLY as a fallback when the model is
# unavailable: deliberately narrow, because the wide version is what sent
# "how much protein have I had?" to the resolution ladder as food.
_ATE_RE = re.compile(
    r"\b(?:i(?:'ve|ve| have| just)?\s+(?:just\s+)?(?:had|ate|eaten|drank|drunk|"
    r"finished|demolished)|just\s+had|had\s+a|having\s+a)\b", re.I)


def looks_like_eating(text: str) -> bool:
    """True for an unambiguous "I ate X" statement, with no question mark.

    The bot must not answer "I could not tell whether that was food" to "I've just had
    500 ml of Rubicon". When the model is unavailable this carries the message through
    as a single unsplit item rather than failing outright. Narrow on purpose: a looser
    pattern is exactly how a question became a food entry."""
    t = (text or "").strip()
    if not t or t.endswith("?"):
        return False
    return bool(_ATE_RE.search(t))


# Dose forms. A capsule, pill or tablet is a SUPPLEMENT, never a food: it belongs on the
# supplement path, where collagen is kept out of the protein target and the dose is
# recorded as a dose. "400mg of my protein collagen capsules (1 pill)" was classified as
# food, sent to a name search, and came back as soy protein isolate.
_SUPPLEMENT_FORM = re.compile(
    r"\b(capsule|capsules|caps|pill|pills|tablet|tablets|tabs?|softgel|softgels|"
    r"gummies|sachet|scoop|scoops|drops?)\b", re.I)
# A dose stated in milligrams or micrograms is nutritionally trivial as food. 400 mg of
# anything is under 2 kcal, so reporting "kcal 1" as though it were a meal is noise.
_TINY_DOSE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(mg|mcg|ug|µg)\b", re.I)


# Phrases that actually place food INSIDE a session. Nothing else counts.
#
# THE BUG THIS EXISTS FOR. "M&s overnight oats salted caramel" was flagged in_session by
# the model, with no session mentioned anywhere in it. The prompt already said "true only
# if eaten DURING a training session" and the model set it anyway. The consequence was not
# cosmetic: in-session fuel is written back to session-log.json, so a bowl of breakfast
# oats entered the fuelling history as 37 g of in-run carbohydrate and fed the g/hr ramp
# the coach prescribes from.
#
# Jamie's whole reason for separating in-run from out-of-run was that a good day total must
# not be able to hide an under-fuelled run. A flag the model can set on a hunch destroys
# exactly that separation, so the model may now only CONFIRM what the words support.
_DURING_SESSION = re.compile(
    r"\b(?:"
    r"during|whilst|while)\b[^.]{0,24}\b(?:ride|riding|run|running|swim|swimming|"
    r"session|race|bike|turbo|long one|intervals)\b"
    r"|\b(?:on|in)\s+the\s+(?:bike|ride|run|turbo|road|trail|race)\b"
    r"|\bmid[-\s]?(?:ride|run|race|session|swim)\b"
    r"|\bin[-\s]session\b"
    r"|\bon\s+the\s+move\b"
    r"|\bper\s+hour\b|\bg/?hr\b|\bg\s+an\s+hour\b"
    r"|\bevery\s+\d+\s*(?:min|minutes|k|km)\b"
    r"|\b(?:took|taking|had)\b[^.]{0,20}\b(?:out\s+there|en\s+route|on\s+course)\b",
    re.I)


def during_session_evidence(text: str) -> bool:
    """True when the words themselves place this food inside a session."""
    return bool(_DURING_SESSION.search(text or ""))


def looks_like_supplement(text: str) -> bool:
    return bool(_SUPPLEMENT_FORM.search(text or ""))


def tiny_dose_mg(text: str):
    """Dose in mg if the message states one in mg/mcg, else None.

    Used to say "that is nutritionally negligible" rather than logging a 1 kcal entry
    and implying it was food worth counting."""
    m = _TINY_DOSE.search(text or "")
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    return val if unit == "mg" else val / 1000.0


CONVERSE_PROMPT = """You are this athlete's nutrition and fuelling coach, mid-conversation
with him on Telegram. He is an experienced Ironman athlete - talk to him as a peer who
knows the physiology, not as an app explaining itself.

WHAT YOU KNOW (every figure here was computed from his logged food, his ICU calendar and
the fuelling primitives his coach uses):
%s

NOW: %s

THE CONVERSATION SO FAR (oldest first, each line timed; yours are "coach"). These
timestamps are real. If the previous exchange is hours or days old, it is stale
background, not a live thread - answer the new message on TODAY's facts above, not on
whatever was being discussed back then:
%s

HIS MESSAGE:
%s

How to answer:
- You are talking TO him, not about him. Always address him directly as "you". Never
  write about him in the third person - "he wants" or "he's not answering" - your entire
  reply is sent to him verbatim, so there is no notes-to-self register to slip into.
- Actually engage with what he asked. Reason about it. If he asks why, explain the
  mechanism. If he is weighing options, have an opinion and say which you would pick.
- Follow the thread. "why?" or "what about the other one?" refers to what was just said.
- Go as deep as the question deserves and no deeper. A factual question gets a sentence;
  "how should I fuel tomorrow" gets a real answer. Never pad, and never repeat numbers he
  can already see on the app unless they carry the point.
- You may ask him ONE question back when the answer genuinely turns on something you do
  not know - what time he is running, how his gut handled the last long one. Do not
  interrogate him. If you already asked something in the conversation above and he has
  not answered it, do NOT ask again: he has decided it does not matter, so answer with a
  stated assumption instead. Asking the same thing three turns running is what a form
  does, not a coach.
- It is fine to disagree with him, and fine to say "either is fine" when it is true.

WHAT TO EAT, when he asks for options. NAMED MEALS FIRST, in this order:
- Open with two or three NAMED options, one line each, and nothing before them. No
  preamble about the day and no macro summary first - the first thing he reads is food he
  could put on a plate. "Jacket potato with tuna and sweetcorn" is an option; "something
  carb-forward" is not, and neither is "add some carbohydrate".
- Each option carries a one-clause WHY, naming the gap or the demand it answers. The facts
  hold all of it: `demand_ahead` is what is coming and when, `carb_basis` and `fat_basis`
  are the engine's own reasons for where the zones sit, and each macro's `gap_to_low_g`
  (how much more is wanted) and `room_to_high_g` (what is left before the top, negative
  when he is past it) are the size of the gap. A negative `room_to_high_g` only means
  something where that macro's `bias` is a ceiling or a band. On a FLOOR - protein, and
  fibre on most days - being past the top is exactly right, so do not mention it and never
  offer a smaller option because of it. So: "closes most of the carbohydrate gap
  before tomorrow's long ride", "fat has room on a quiet day", "protein is still under
  its floor". The demand is the point - he is fuelling for the work required, not eating
  to a budget.
- Options come from `foods_he_actually_eats` BEFORE anything invented. That list is his own
  repeat foods, each with `lean` (what it mostly is), `usual_meal` and its real figures.
  Reach outside it only when nothing in it answers the gap, or when he asked about delivery.
  Anything tagged `in_session_fuel` is training fuel and is not a meal: offer those for
  fuelling a session, never as dinner.
- THEN at most ONE line of numbers, and only if it carries something he cannot see on the
  app. Numbers support the choice; they never lead it.
- THEN which single option you would pick, and why it beats the others, in one line.
- Respect the fibre PHASE when you choose: after a long session the ceiling has expired
  and fibre is wanted back, before one it is not.
- His city is in the facts if it is known. For delivery, search for what is genuinely
  available there rather than naming a chain you assume exists.
- Figures for anything not in the facts are ESTIMATES and must be labelled as such. If he
  wants one logged properly, say so - the ladder will resolve it from the vendor's own
  published data when he tells you what he ordered.

WHEN HE IS WEIGHING NAMED OPTIONS (`options_on_the_table` is in the facts): every one has
already been resolved and each carries `if_eaten`, where the day lands if he has it. Lead
with your PICK and the gap it closes, in one sentence, then the trade-off against the
others, then the numbers if they matter. Same order as above: the choice first, the
arithmetic behind it second, and never a table of both.

Rules that are not style preferences:
- NEVER do arithmetic and never state a figure that is not in the facts above. Everything
  there was computed deliberately; a number you produce yourself is indistinguishable
  from a real one and it ends up in his training decisions. If you need something absent,
  say what is missing.
- Suggest food from `foods_he_actually_eats` where you can. He can act on a swap between
  two things in his own fridge.
- A zone is a landing area, not a rule. Never moralise about food, never use restriction
  language, and never imply he has failed. He is lean, near his essential-fat floor, and
  the job is adequate intake - not restraint.
- Never suggest cutting anything marked in_session: that is fuel taken while training.
- UK English, no headings, no bullet-point macro dumps.
"""


_WANTS_OPTIONS = re.compile(
    r"\bwhat\s+(?:shall|should|can|do)\s+i\s+(?:eat|have|order|make|cook)\b"
    r"|\bwhat\s+to\s+(?:eat|have|order|make)\b"
    r"|\b(?:give|suggest|recommend)\b[^.]{0,20}\b(?:option|options|ideas|something)\b"
    r"|\b(?:deliveroo|just\s?eat|uber\s?eats|takeaway|take-?away|delivery)\b"
    r"|\bwhat\s+(?:kind\s+of\s+)?thing\s+should\s+i\s+(?:make|cook|eat)\b"
    r"|\bi(?:\047|\u2019)?ve?\s+f\w*shed\s+my\s+\w+.{0,30}\beat\b",
    re.I)


def wants_options(message: str) -> bool:
    """True when he is asking WHAT to eat, which needs more than his own log."""
    return bool(_WANTS_OPTIONS.search(message or ""))


def converse(message: str, facts: dict, history: list, claude_bin: str, model: str,
             log=print, runner=None, timeout: int = 150, now_iso: str = None) -> str | None:
    """A turn of actual conversation. None when the model is unavailable.

    This replaced two single-shot prompts - one capped at "one or two sentences", the
    other with no memory of the previous turn - which between them made the bot incapable
    of a conversation however good the facts were.

    now_iso is passed IN by the caller rather than read here: the bot flattened
    "role: text" with no timestamps and no notion of the current time, so a two-day-old
    exchange read back as though it were still live. A stub runner in a test has no clock
    of its own to fake, so the current time has to arrive as a parameter."""
    runner = runner or subprocess.run
    now_iso = now_iso or datetime.now().strftime("%Y-%m-%dT%H:%M")
    convo = "\n".join(
        f"{t['at']} {t.get('role')}: {t.get('text')}" if t.get("at")
        else f"{t.get('role')}: {t.get('text')}"
        for t in (history or []))
    # WEB TOOLS ONLY WHEN HE IS ASKING WHAT TO EAT. "what shall I eat" and "options on
    # Deliveroo" cannot be answered from his own log alone - a takeaway menu is not in it -
    # and a tool-less model would either invent a restaurant or fall back to "have some lean
    # protein", which is the useless advice this is meant to replace. Every other kind of
    # turn stays tool-free, because tools cost 30-60s and most questions are about figures
    # that are already in the facts.
    cmd = [claude_bin, "--print", "--model", model]
    if wants_options(message):
        cmd += ["--allowedTools", "WebSearch,WebFetch"]
    try:
        proc = runner(cmd,
                      input=CONVERSE_PROMPT % (json.dumps(facts, indent=2, default=str),
                                               now_iso, convo or "(nothing yet)", message),
                      capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        log(f"converse failed: {exc}")
        return None
    raw = ((getattr(proc, "stdout", "") or "").strip()) or None
    if raw and model_unavailable(raw):
        log(f"converse: MODEL UNAVAILABLE - {raw[:100]}")
        return None
    return raw


ANSWER_PROMPT = """You are a nutrition logging assistant answering a short question \
from the athlete. Be brief and conversational, one or two sentences, no bullet lists, \
no markdown headings.

CRITICAL: use ONLY the facts below. Do not calculate anything that is not given, and \
do not invent a number. If the facts do not contain the answer, say plainly that you \
do not have it and offer what you do have.

FACTS
%s

QUESTION
%s
"""


def answer_question(question: str, facts: dict, claude_bin: str, model: str,
                    log=print, runner=None, timeout: int = 60) -> str | None:
    """Phrase an answer from injected facts. Returns None if the model is unavailable,
    so the caller can fall back to a plain deterministic summary rather than nothing.

    The model phrases, it does not compute. Every figure it can use is in `facts`,
    which comes from the store and the engine."""
    runner = runner or subprocess.run
    body = json.dumps(facts, indent=2, default=str)
    try:
        proc = runner([claude_bin, "--print", "--model", model],
                      input=ANSWER_PROMPT % (body, question),
                      capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        log(f"nlu answer failed: {exc}")
        return None
    out = (getattr(proc, "stdout", "") or "").strip()
    return out or None


CORRECTION_PROMPT = """You are deciding what a correction to a food log means. The \
athlete has an item in front of him (below) and has sent a correction. Reply with ONLY \
a JSON object.

WHAT IS IN FRONT OF HIM (as currently resolved; per_100g is the label/table basis if
known). This may be EMPTY, which means nothing is logged yet - a "remember" decision is
still valid. A JSON OBJECT is one item. A JSON ARRAY is a whole meal awaiting his
confirmation, numbered by `index`, and every component of it is his to correct - the
index-carrying decisions below exist for that case and are valid ONLY when you are shown
an array:
%s

HIS CORRECTION:
%s

Decide which ONE of these he means and reply in that shape:
  {"kind":"rescale","grams":<number>}
      - he is changing HOW MUCH, and states or implies an amount in grams/ml
        ("that's 100g, I had 160g" -> 160; "only had half" of a known 40g portion -> 20)
  {"kind":"rescale_factor","factor":<number>}
      - he is changing how much by a ratio with no known base amount ("half of it" -> 0.5,
        "I had two of them" -> 2)
  {"kind":"whole_pack"}
      - he ate the whole pack/bag/tub (use the item's pack_g; the code will ask if unknown)
  {"kind":"reidentify","text":"<what to look up instead>","exclusions":["<food he says it
        was NOT, if any>"]}
      - he is disputing WHAT the food is, with or without a new amount
  {"kind":"meal","meal":"breakfast|lunch|dinner|snacks"}
      - he is filing it under a meal, nothing else
  {"kind":"retime","time":"HH:MM","which":"<words identifying the entry, or null>"}
      - he is saying WHEN he ate it: "the initial rye bread was 830am" -> time "08:30",
        which "initial rye bread". 24h, and null `which` means the latest entry.
  {"kind":"rename","name":"<the correct name>","which":"<as above, or null>"}
      - he is naming the product WITHOUT disputing the figures: "the 160g was a pack of
        bbq chicken" against an entry whose confidence is label or manual. He read those
        figures off the pack himself, so they stand and only the name was wrong.
      - if the item's confidence is NOT label/manual the figures came from a lookup, so a
        new name means the lookup was wrong: use reidentify instead.
  {"kind":"remember","product":"<name>","field":"scoop_g|portion_g|pack_g|means",
   "value":<number, or the text a `means` alias expands to>}
      - he is telling you a LASTING fact about a product, to be applied every time it
        comes up: "a rego scoop is half a portion" where a portion is 50 g is
        {"product":"sis rego","field":"scoop_g","value":25}. Use `means` for an alias:
        "sis choco is the go energy choco fudge bar" ->
        {"product":"sis choco","field":"means","value":"SiS GO Energy Choco Fudge bar"}.
  {"kind":"remember_and_rescale","product":...,"field":...,"value":...,"grams":<n>}
      - the same fact, where it ALSO fixes the amount on the item in front of him. The
        REGO one is this shape: remember that a scoop is 25 g, and this entry was 25 g.
  {"kind":"rescale_all","factor":<number>}
      - EVERYTHING in front of him changes by one ratio: "do all of that x1.5", "it was
        half of what you have there", "double the lot". Use this rather than repeating one
        factor across every index.
  {"kind":"rescale_items","items":[{"index":i,"factor":<n>} or {"index":i,"grams":<n>}]}
      - DIFFERENT components change differently: "make the noodles, steak and sauce 1.5x
        and the vegetables 3x" is three items at factor 1.5 and one at factor 3. Give
        grams when he states an amount for that component, factor when he gives a ratio.
        Name only the components he is changing; the rest stay as they are.
  {"kind":"meal_portions","items":[{"index":i,"grams":<n>}]}
      - HE IS TELLING YOU IT WAS A REAL MEAL AND ASKING YOU TO SIZE IT: "it was a whole
        meal", "that was a full dinner plate, work it out". The components were priced per
        100 g with no portion, which is not a meal, and he should not have to weigh his
        dinner retrospectively to fix that.
        THIS IS THE ONE PLACE YOU MAY ESTIMATE A QUANTITY. Give every component a
        plausible as-eaten weight in GRAMS - noodles/pasta/rice around 300 g cooked for a
        large serving, meat 150 g, vegetables 200 g, sauce 40 g, oil 15 g, scaled to any
        size word he used. GRAMS ONLY: never a kcal or macro figure, because the code
        scales those from each component's own per-100g basis, and a number you supplied
        would silently replace a sourced one. Every component gets a portion, and the
        offer will tell him each one was your estimate.
  {"kind":"unclear"}

Rules:
- The decision is about MEANING, not keywords. "That's 100g I had 160g" names two
  numbers; only 160 is what he ate.
- Do not compute any nutrition figures. Never return macros. The code does the maths.
  Deriving a scoop weight from a stated portion weight is not a nutrition figure - it is
  the fact he is asking you to store - but the portion weight must be one he or the item
  states, never one you assume.
- A correction that disputes the food AND gives an amount is reidentify (put the amount
  in the text, e.g. "20g of jam").
- An `index` is one you were SHOWN. Never invent one, never use an index when you were
  given a single object rather than an array, and match his words to the component names in
  the array rather than to their order in his sentence.
- `which` is his words, not a guess: quote the part of the message that names the entry
  ("initial rye bread", "the 160g"), or null if he did not name one. The code matches it
  against the log and ASKS when it matches nothing, so a vague `which` is safe and an
  invented one is not.
"""


# Keys that make a scaling decision a MACRO decision, which the model is never allowed to
# make. Checked by name against everything it returns for a component, because the harm is
# silent: a kcal figure the model supplied would overwrite one scaled from a real per-100g
# basis and read back as sourced data.
_FORBIDDEN_IN_DECISION = {
    "kcal", "calories", "cals", "energy", "protein", "protein_g", "carb", "carbs",
    "carb_g", "carbohydrate", "carbohydrates", "fat", "fat_g", "fibre", "fibre_g",
    "fiber", "fiber_g", "sodium", "sodium_mg", "dietary_sodium_mg", "per_100g",
}


def _carries_macros(spec) -> bool:
    """True if this decision tries to hand back a nutrition figure."""
    if not isinstance(spec, dict):
        return False
    return any(str(k).strip().lower() in _FORBIDDEN_IN_DECISION for k in spec)


def batch_summaries(batch: list) -> list:
    """The compact per-item view the model is shown for a pending meal.

    Deliberately NOT the resolved items themselves: those carry attempt logs, ingredient
    strings and provenance that would crowd out the correction itself, and the only things
    a scaling decision needs are which component is which, how big each currently is, and
    whether there is a per-100g basis to scale from."""
    out = []
    for i, it in enumerate(batch or []):
        out.append({
            "index": i,
            "name": (it.get("resolved_name") or it.get("_raw") or "")[:70],
            "kcal": it.get("kcal"),
            "portion_used_g": it.get("portion_used_g"),
            # Stated as a fact rather than as figures: with a basis, a portion becomes a
            # multiplication; without one, only a ratio can be applied.
            "has_per_100g_basis": bool(it.get("per_100g")),
        })
    return out


def _scale_specs(got: dict, n: int, log) -> list | None:
    """Validated [{'index':i,'factor':f}|{'index':i,'grams':g}], or None to refuse.

    REFUSES THE WHOLE DECISION on any bad component rather than applying the good ones. A
    partial application is the worst outcome available here: he would be shown a meal in
    which some components moved and some did not, with nothing saying which, and the
    arithmetic he is being asked to confirm would be wrong in a way he cannot see."""
    specs = got.get("items")
    if not isinstance(specs, list) or not specs:
        log("decide_correction: per-item decision with no items")
        return None
    out = []
    for spec in specs:
        if not isinstance(spec, dict):
            return None
        if _carries_macros(spec):
            # The model may size a portion; it may never price one.
            log(f"decide_correction: refused a decision carrying macros: {spec}")
            return None
        try:
            idx = int(spec["index"])
        except (KeyError, TypeError, ValueError):
            log(f"decide_correction: component with no usable index: {spec}")
            return None
        if not 0 <= idx < n:
            # An out-of-range index means the model was not reading the batch it was
            # shown, so nothing it said about that batch can be trusted.
            log(f"decide_correction: index {idx} is not one of the {n} items shown")
            return None
        if spec.get("grams") is not None:
            try:
                grams = float(spec["grams"])
            except (TypeError, ValueError):
                return None
            if not 0 < grams <= MAX_PORTION_G:
                log(f"decide_correction: implausible portion {grams}")
                return None
            out.append({"index": idx, "grams": grams})
        elif spec.get("factor") is not None:
            factor = _usable_factor(spec.get("factor"), log)
            if factor is None:
                return None
            out.append({"index": idx, "factor": factor})
        else:
            log(f"decide_correction: component with neither grams nor factor: {spec}")
            return None
    return out


# Bounds on what the code will execute, not judgements about what he ate. A factor of 40
# or a 9 kg portion is a mis-read decision rather than a meal, and executing one silently
# rewrites his day.
MAX_RESCALE_FACTOR = 20
MAX_PORTION_G = 5000


def _usable_factor(value, log) -> float | None:
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 < factor <= MAX_RESCALE_FACTOR:
        log(f"decide_correction: implausible factor {value!r}")
        return None
    return factor


def decide_correction(message: str, item: dict, claude_bin: str, model: str,
                      log=print, runner=None, timeout: int = 45,
                      batch: list = None) -> dict | None:
    """What the correction MEANS, decided by the model; the code only executes it.

    This replaced a growing pile of regexes (quantity detectors, exclusion
    extractors, unit tables) that each existed to reverse-engineer one judgement the
    model makes natively. The failure mode of the pile was 13 Aug 2026: '100g'
    registered as an excluded food, a label's own figures re-searched instead of
    scaled. The model returns a decision, never a number the code could compute -
    macros stay deterministic. None when the model is unavailable, so the caller can
    fall back to the deterministic detectors."""
    runner = runner or subprocess.run
    # `confidence` is in here because rename-versus-reidentify turns on it: his own label
    # figures survive a renaming, a lookup's figures do not. The code checks it again
    # before renaming - this is so the model reaches the right kind in the first place.
    summary = {k: (item or {}).get(k) for k in
               ("resolved_name", "confidence", "kcal", "protein_g", "carb_g", "fat_g",
                "portion_used_g", "pack_g", "per_100g", "portion_assumed")
               if (item or {}).get(k) is not None}
    # THE WHOLE MEAL, when there is one on the table. Showing a single item was the defect
    # of 14 Aug 2026: with a four-component stir-fry awaiting confirmation, the caller had
    # no rule for which component to show, so it showed the last thing he had COMMITTED -
    # a brookie from earlier - and the model was asked to interpret "it was a whole meal"
    # against a food that had nothing to do with it. With a batch shown, a committed entry
    # can no longer be the target of a correction at all.
    n = len(batch or [])
    if n:
        summary = batch_summaries(batch)
    try:
        proc = runner([claude_bin, "--print", "--model", model],
                      input=CORRECTION_PROMPT % (json.dumps(summary, default=str),
                                                 message),
                      capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        log(f"decide_correction failed: {exc}")
        return None
    raw = (getattr(proc, "stdout", "") or "").strip()
    if not raw or model_unavailable(raw):
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        log(f"decide_correction unparseable: {raw[:80]}")
        return None
    try:
        got = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        log(f"decide_correction unparseable: {raw[:80]}")
        return None
    kind = got.get("kind")
    if kind in ("rescale", "rescale_factor", "whole_pack", "reidentify", "meal",
                "unclear"):
        return got
    if kind in ("rescale_all", "rescale_items", "meal_portions"):
        # UNVALIDATED IS NOT THE SAME AS UNAVAILABLE. Returning None here would be read by
        # the caller as "the model could not be reached" and would run the regex fallback
        # against the message, which for "make the noodles 1.5x and the veg 3x" produces
        # one wrong number applied to one wrong item. `unclear` says so honestly instead.
        if not n:
            log(f"decide_correction: {kind} with no batch to apply it to")
            return {"kind": "unclear"}
        if _carries_macros(got):
            log(f"decide_correction: refused a {kind} carrying macros")
            return {"kind": "unclear"}
        if kind == "rescale_all":
            factor = _usable_factor(got.get("factor"), log)
            return ({"kind": "rescale_all", "factor": factor} if factor
                    else {"kind": "unclear"})
        specs = _scale_specs(got, n, log)
        if not specs:
            return {"kind": "unclear"}
        if kind == "meal_portions" and any("grams" not in s for s in specs):
            # Sizing a meal is a GRAMS judgement. A ratio here would be scaling something
            # by a number with no basis, which is the shape that produced 447 kcal.
            log("decide_correction: meal_portions must be grams for every component")
            return {"kind": "unclear"}
        return {"kind": kind, "items": specs}
    if kind == "retime":
        hhmm = normalise_hhmm(got.get("time"))
        if not hhmm:
            log(f"decide_correction: retime without a usable time: {got.get('time')!r}")
            return None
        return {"kind": "retime", "time": hhmm,
                "which": (str(got.get("which")).strip() if got.get("which") else "")}
    if kind == "rename":
        name = str(got.get("name") or "").strip()
        if not name:
            return None
        return {"kind": "rename", "name": name,
                "which": (str(got.get("which")).strip() if got.get("which") else "")}
    if kind in ("remember", "remember_and_rescale"):
        fact = product_fact(got)
        if not fact:
            return None
        out = {"kind": kind, **fact}
        if kind == "remember_and_rescale":
            # A remember that also fixes the entry still has to state the amount in
            # grams; the code will not derive it from the fact, because the fact is
            # about the product and the rescale is about this one entry.
            try:
                out["grams"] = float(got.get("grams"))
            except (TypeError, ValueError):
                log("decide_correction: remember_and_rescale without usable grams")
                return {"kind": "remember", **fact}
        return out
    return None


# The fields a remembered product fact may set. A closed set on purpose: this file is
# PERMANENT and per-athlete, unlike the day's exclusions, so an unrecognised field name
# from a model wobble would sit there for ever being consulted by nothing.
PRODUCT_FACT_FIELDS = ("scoop_g", "portion_g", "pack_g", "means")


def product_fact(got: dict) -> dict | None:
    """{'product','field','value'} from a remember decision, or None if unusable.

    Weights are coerced to a number and a `means` alias must be real text, because the
    injection that consults these facts is deterministic: whatever lands here is applied
    to a lookup verbatim, every time, with no model in the loop to notice it is wrong."""
    product = str(got.get("product") or "").strip().lower()
    field = str(got.get("field") or "").strip()
    if not product or field not in PRODUCT_FACT_FIELDS:
        return None
    if field == "means":
        value = str(got.get("value") or "").strip()
        if len(value) < 3:
            return None
    else:
        try:
            value = float(got.get("value"))
        except (TypeError, ValueError):
            return None
        if not 0 < value <= 5000:
            return None
    return {"product": product, "field": field, "value": value}


def apply_correction(original_text: str, correction: str) -> str:
    """Fold a correction into the original text and RE-PARSE, rather than patching the
    parsed result.

    Patching tends to preserve the original misparse: if "a bag of nuts" was read as
    30 g and the athlete says "no, the whole 200 g bag", editing the portion keeps
    whatever else was wrong about the match. Re-resolving from the combined text gets a
    fresh answer."""
    return f"{original_text.strip()} ({correction.strip()})"


# --- advice / debate --------------------------------------------------------

ADVICE_PROMPT = """You are this athlete's nutrition sounding board. He is weighing up \
what to eat and wants a short, direct opinion, not a lecture.

Be conversational and concise: a few sentences. It is fine to disagree with him.

THE SHAPE OF THE ANSWER, in this order:
- Your PICK, named, in the first sentence. Not a survey of the options and not the numbers
  first: he asked which one, so answer which one.
- WHY, as one clause naming the gap or the demand it closes. `demand_ahead` says what
  session is coming and when, `carb_basis` and `fat_basis` say why the zones sit where they
  do, and each macro's `gap_to_low_g` and `room_to_high_g` are the size of what is open.
  "It closes the carbohydrate gap before tomorrow's long ride" is the answer; "it is higher
  in carbohydrate" is not, because it does not say what that is for.
- The trade-off against the option you did not pick, in one line.
- Numbers LAST, at most one line of them, and only where they carry the point. Every option
  already has its real figures in the facts; he can read the totals on the app.
- If the honest answer is that either is fine, say so in the first sentence instead.

CRITICAL RULES
- Use ONLY the numbers in the facts below. Every option has been looked up already.
  Do not invent or recalculate a macro figure.
- If an option would breach a CEILING, say so plainly. If it helps reach a FLOOR, say
  that too. Note that a floor is not a maximum: going over a protein floor is fine.
- Never suggest reducing anything marked in_session: that is fuel taken during
  training and it is protected.
- Do not moralise about food, do not use restriction language, and never imply he has
  failed at anything. He is a lean endurance athlete near his essential-fat floor;
  the job is adequate intake, not restraint.
- If the honest answer is "either is fine", say that.

FACTS
%s

WHAT HE ASKED
%s
"""


COACH_PROMPT = """You are this athlete's nutrition coach. Below is everything known
about today and tomorrow, computed from his logged food, his ICU calendar and the
fuelling primitives his coach already uses.

FACTS (the only figures you may use):
%s

Write him a short brief. Cover, in this order and only where it matters:
  1. What tomorrow is - the session, in one line, using the aim if there is one.
  2. What to do with the REST OF TODAY. Be concrete and specific.
  3. Tomorrow's in-session fuel, if a rate is given.

Rules that matter more than style:
- NEVER do arithmetic and never state a number that is not in the facts above. If you
  need a figure that is not there, say what is missing instead of estimating it. Every
  number in those facts was computed deliberately; a plausible one you invent is
  indistinguishable from a real one and corrupts the log.
- Suggest swaps from `foods_he_actually_eats` ONLY. He can act on "swap the Twix for the
  protein bar"; "add some lean protein" is useless to him.
- A zone is a landing area, not a rule. If he is over on something, say what it costs
  tomorrow rather than telling him off. If nothing needs changing, say so and stop.
- No tables, no bullet-point macro dumps - he can read those on the app. Under 130 words.
- UK English. Speak plainly, like a coach who knows him.
"""


def coach_brief(facts: dict, claude_bin: str, model: str, log=print, runner=None,
                timeout: int = 120) -> str | None:
    """The coaching brief. Returns None when the model is unavailable, so the caller can
    fall back to the deterministic block rather than saying nothing."""
    runner = runner or subprocess.run
    try:
        proc = runner([claude_bin, "--print", "--model", model],
                      input=COACH_PROMPT % json.dumps(facts, indent=2, default=str),
                      capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        log(f"coach brief failed: {exc}")
        return None
    raw = ((getattr(proc, "stdout", "") or "").strip()) or None
    if raw and model_unavailable(raw):
        log(f"coach brief: MODEL UNAVAILABLE - {raw[:100]}")
        return None
    return raw


def advise(question: str, facts: dict, claude_bin: str, model: str, log=print,
           runner=None, timeout: int = 90) -> str | None:
    """Discuss options against the day's real remaining room.

    Every option's macros are resolved through the ladder BEFORE this is called, so
    the debate is about real figures rather than the model's impression of a food.
    Returns None if the model is unavailable so the caller can fall back."""
    runner = runner or subprocess.run
    try:
        proc = runner([claude_bin, "--print", "--model", model],
                      input=ADVICE_PROMPT % (json.dumps(facts, indent=2, default=str),
                                             question),
                      capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        log(f"nlu advise failed: {exc}")
        return None
    return ((getattr(proc, "stdout", "") or "").strip()) or None


# --- photographs ------------------------------------------------------------

PHOTO_PROMPT = """Read the image at %s. It is one of FOUR things. Reply with ONLY a \
JSON object, no prose.

If it is a BARCODE:
  {"kind":"barcode","barcode":"<the digits, exactly as printed>"}

If it is a NUTRITION LABEL or ingredients panel:
  {"kind":"nutrition_label","per":"100g"|"portion","portion_g":<grams or null>,
   "kcal":n,"protein_g":n,"carb_g":n,"fat_g":n,"fibre_g":n,
   "dietary_sodium_mg":n or null,"salt_g":n or null,
   "pack_g":<the pack's total net weight in grams if printed anywhere (e.g. "380g",
             "Net wt 250g"), else null>,
   "product":"<name if visible>","ingredients":"<the ingredients list, verbatim>"}
  Transcribe the printed figures EXACTLY. Do not convert, round or estimate them.
  Report salt_g separately if the panel gives salt rather than sodium; do not convert.
  pack_g matters: "I had the whole pack" is only answerable if you read it now.

If it is an ORDER, RECEIPT or MENU screenshot (Deliveroo, Just Eat, Uber Eats, a
restaurant bill or menu):
  {"kind":"order","vendor":"<restaurant or shop name>",
   "stated_item_count":<the number the screen claims, e.g. from "Your order (5 items)",
                        else null>,
   "items":[{"text":"<the dish name WITH its options folded in>","qty":<the Nx number,
              default 1>,"portion_g":null}]}

  Fold each dish's modifiers INTO its text. A Wagamama order reading
    1x  new! gochujang salmon rice bowl
        brown rice (vg)
        extra salmon
  is ONE item: "gochujang salmon rice bowl with brown rice and extra salmon".

  A modifier is NEVER its own item. Returning "(meal is with double salmon and brown
  rice)" as an item is wrong, because it names no dish.

  Strip marketing and dietary markers from the text: "new!", "(vg)", "(ve)", "(v)",
  "NEW", "chef's special". They are noise in a search query.

  STATED_ITEM_COUNT COUNTS UNITS, NOT LINES. "Your order (5 items)" on a screen showing
  1x rice bowl, 1x edamame and 3x soy sauce is 1 + 1 + 3 = 5, and nothing is missing.
  Report the number the screen states, and put the Nx figure in each item's qty. The
  logger compares the SUM OF QUANTITIES against it, so a genuinely cropped screenshot is
  the only thing that looks short.

  Keep condiments and sauces as items. 3x soy sauce is negligible energy but real
  sodium, which this athlete tracks. Give it qty 3 rather than three separate lines.
  Include the vendor in each text if the dish name alone would be ambiguous.
  Ignore prices, delivery fees, tips, cutlery and bag charges.
  Do NOT provide nutrition figures.

If it is a PLATE OF FOOD (no label visible):
  {"kind":"food_plate","items":[{"text":"<single food>","portion_g":<estimate or null>}]}
  Identify the components. Estimate portions only where you reasonably can.
  Do NOT provide any nutrition figures: those are looked up separately.

If you cannot tell, reply {"kind":"unknown"}.
"""

SALT_TO_SODIUM = 1 / 2.5   # UK labels give salt; sodium = salt / 2.5


# The CLI prints this to STDOUT with a non-zero exit rather than raising, so a caller that
# only looks for JSON sees "no JSON" and reports the input as unreadable. It is neither the
# photo's fault nor the food's: the VM's token has expired and every model call is failing.
# Telling the two apart is the difference between "send a clearer picture" and "go and
# re-authenticate", and guessing wrong wastes the user's time on a fine photo.
_AUTH_FAILURE = re.compile(r"401|oauth|access token|authenticat|usage limit", re.I)


def model_unavailable(raw: str) -> bool:
    """True when output is the CLI refusing to run, not an answer."""
    head = (raw or "")[:400]
    return bool(head and _AUTH_FAILURE.search(head) and "{" not in head)


def read_photo(img_path: str, claude_bin: str, model: str, log=print, runner=None,
               timeout: int = 180) -> dict:
    """Classify and extract from a photo. Returns {'kind': 'unknown'} on any failure.

    The vision model does IDENTIFICATION, never nutrition arithmetic. A barcode goes
    to a database lookup; a label panel is transcribed and counts as label data; a
    plate becomes a list of items that each go through the resolution ladder. That
    keeps the confidence model honest - a photo of a plate is an ESTIMATE, a photo of
    the printed panel is a LABEL, and the two must not be conflated."""
    runner = runner or subprocess.run
    try:
        proc = runner([claude_bin, "--print", "--model", model,
                       "--allowedTools", "Read"],
                      input=PHOTO_PROMPT % img_path, capture_output=True, text=True,
                      timeout=timeout)
    except Exception as exc:
        log(f"photo read failed: {exc}")
        return {"kind": "unknown", "error": str(exc)}
    raw = (getattr(proc, "stdout", "") or "").strip()
    # Every one of the exits below used to return a bare {"kind": "unknown"} with nothing
    # logged, so a photo that failed for an infrastructure reason was indistinguishable
    # from a blurry one. On 10 Aug the VM's OAuth token expired and the bot answered "I
    # could not read that" to a perfectly legible Deliveroo screenshot.
    if model_unavailable(raw):
        log(f"photo read: MODEL UNAVAILABLE - {raw[:120]}")
        return {"kind": "unknown", "model_unavailable": True, "error": raw[:200]}
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        log(f"photo read: no JSON in {len(raw)} chars of output - {raw[:120]!r}")
        return {"kind": "unknown"}
    try:
        got = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        log(f"photo read: JSON did not parse ({exc}) - {raw[start:start + 120]!r}")
        return {"kind": "unknown"}
    if got.get("kind") not in ("barcode", "nutrition_label", "food_plate", "order"):
        log(f"photo read: unusable kind {got.get('kind')!r}")
        return {"kind": "unknown"}
    if got["kind"] == "nutrition_label":
        # UK panels usually print SALT, not sodium. Convert here, once, rather than
        # letting the model do it: salt is sodium x 2.5 and a model that converts
        # silently produces a 150% error that looks entirely plausible.
        if got.get("dietary_sodium_mg") in (None, "") and got.get("salt_g") not in (None, ""):
            try:
                got["dietary_sodium_mg"] = round(float(got["salt_g"]) * SALT_TO_SODIUM * 1000)
                got["sodium_from_salt"] = True
            except (TypeError, ValueError):
                pass
    if got["kind"] in ("food_plate", "order"):
        vendor = (got.get("vendor") or "").strip()
        # The trailing punctuation has to be consumed by the pattern: a word boundary
        # cannot eat the "!" in "new!", which left "! gochujang salmon rice bowl".
        _NOISE_MARKERS = re.compile(
            r"\((?:vg|ve|v)\)|\b(?:new|vegan|vegetarian|plant based|chef'?s special)\b[!\s]*",
            re.I)
        items = []
        for i in (got.get("items") or []):
            if not isinstance(i, dict) or not i.get("text"):
                continue
            text = str(i["text"]).strip()
            # A parenthetical modifier is not a dish. The first cut returned
            # "(meal is with double salmon and brown rice)" as an item from a Deliveroo
            # screenshot, which then matched raw brown rice in the composition tables.
            if text.startswith("(") or len(text) < 4:
                continue
            text = re.sub(r"\s{2,}", " ", _NOISE_MARKERS.sub(" ", text)).strip(" ,-!.+")
            if len(text) < 4:
                continue
            if vendor and got["kind"] == "order" and vendor.lower() not in text.lower():
                text = f"{text}, {vendor}"
            try:
                qty = max(1, int(i.get("qty") or 1))
            except (TypeError, ValueError):
                qty = 1
            items.append({"text": text, "qty": qty,
                          "portion_g": i.get("portion_g"), "in_session": False})
        got["items"] = items
        # UNITS, not lines. Jamie's Wagamama order stated 5 items and had 3 lines, because
        # one was 3x soy sauce. Comparing the stated count to the LINE count would have
        # called a complete screenshot cropped, which is crying wolf on correct input.
        got["units_seen"] = sum(i["qty"] for i in items)
        if not items:
            log(f"photo read: {got['kind']} had no usable items after cleaning")
            return {"kind": "unknown"}
    return got


def label_to_item(label: dict) -> dict:
    """Turn a transcribed nutrition panel into a resolved item at LABEL confidence.

    This is the best rung available: it is the manufacturer's own printed figures.
    Scales per-100g panels to the stated portion; leaves a per-portion panel alone.
    Anything the panel did not show stays absent rather than becoming zero."""
    fields = ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g", "dietary_sodium_mg")
    portion = label.get("portion_g")
    factor = 1.0
    if (label.get("per") or "100g").startswith("100") and portion:
        factor = float(portion) / 100.0
    out = {}
    per_100 = {}
    for f in fields:
        v = label.get(f)
        if v not in (None, ""):
            try:
                out[f] = round(float(v) * factor, 1)
                if (label.get("per") or "100g").startswith("100"):
                    per_100[f] = float(v)
                elif portion:
                    per_100[f] = round(float(v) * 100.0 / float(portion), 2)
            except (TypeError, ValueError):
                pass
    if "dietary_sodium_mg" in out:
        out["dietary_sodium_mg"] = round(out["dietary_sodium_mg"])
    # The per-100g BASIS travels with the item. "That's 100g, I had 160g" against a
    # label whose figures were sitting right there was answered by re-running the whole
    # ladder on the string "item from label photo" (13 Aug 2026) - the basis had been
    # scaled away, so the only correction available was to start again. With the basis
    # kept, a quantity correction is a multiplication, not a search.
    if per_100:
        out["per_100g"] = per_100
    if portion:
        out["portion_used_g"] = float(portion)
    if label.get("pack_g"):
        try:
            out["pack_g"] = float(label["pack_g"])
        except (TypeError, ValueError):
            pass
    out["resolved_name"] = label.get("product") or "item from label photo"
    out["ingredients"] = label.get("ingredients") or ""
    out["source_url"] = "photo of the product label"
    return out


# --- interpret first, resolve second (Jamie's design, 10 Aug 2026) ------------

INTERPRET_PROMPT = """You are the lookup planner for a nutrition logger. Work out what \
each item actually IS and how to search for it. Reply with ONLY a JSON object.

{"items":[{
  "canonical_name": "<the clearest plain name for this exact thing>",
  "brand": "<brand ONLY if the user stated one, else null>",
  "form": "capsule|tablet|powder|liquid|bar|drink|whole_food|prepared_meal|bakery|"
          "confectionery|dairy|other",
  "category": "supplement|whole_food|branded_packaged|restaurant|homemade",
  "is_supplement": true|false,
  "expect_macros": true|false,
  "portion_g": <grams as eaten, or null>,
  "portion_estimated": true|false,
  "dose_mg": <milligrams if stated in mg/mcg, else null>,
  "count": <number of units if stated, else null>,
  "in_session": true|false,
  "at": "<HH:MM in 24h ONLY if the message states a clock time for this item, else null.
         'at 1350' -> '13:50'. Never guess: 'this morning' is null>",
  "meal": "<breakfast|lunch|dinner|snacks ONLY if the message names the meal - 'for
           breakfast I had...', 'with dinner', 'late lunch'. A clock time is evidence,
           not proof: 'toast at 8:30' is null and the logger files it by the clock.
           Null for anything in_session - fuel is not a meal>",
  "search_terms": ["<best query first>", "<fallback>", "..."]
}]}

Rules that matter:
- NEVER give nutrition figures. You are planning a search, not answering one.
- NEVER invent a brand. If the user did not name one, brand is null.
- search_terms should be what a food database would actually match: strip quantities,
  possessives and filler. "400mg of my protein collagen capsules" searches best as
  "collagen peptides", not as the original sentence.
- expect_macros is false when the amount is nutritionally trivial, e.g. a 400 mg
  capsule or an electrolyte tablet. Say false and the logger will record a dose only.
- is_supplement true for anything in a capsule, tablet, softgel or measured scoop taken
  for a nutrient rather than eaten as food.
- form is what the product physically IS. A collagen capsule is "capsule". A collagen
  protein bar is "bar". These are different products and the logger uses this to throw
  out wrong matches.
- Split multiple items. Keep each one self-contained.

A COMPOSED MEAL: COOKED STATES AND A PORTION FOR EVERY COMPONENT.
"a large stir fry with egg noodles, a small steak, soy ginger garlic sauce and veg" is a
plate of food, and each component needs two things it was not getting (14 Aug 2026: this
returned four components at no portion at all, priced from the RAW and DRIED rows - dried
noodles and raw steak - and offered 447 kcal for a 980 kcal dinner).

1. SEARCH FOR THE STATE HE ATE IT IN. Food databases hold a row per state, and the raw or
   dried row is a different food from the one on the plate: dried egg noodles are 338 kcal
   per 100 g, boiled ones 166. So search_terms and canonical_name must name the state:
     "egg noodles, cooked"   NEVER "egg noodles, dried" or "egg noodles, raw"
     "steak, grilled"        NEVER "beef steak, raw"
     "vegetables, stir-fried"
   THE STATE HE ATE IT IN IS THE ONLY STATE THAT EVER BELONGS IN A FOOD LOG. Say "raw" or
   "dried" only when he ate it that way, which for meat and noodles he did not.
   If the cooking method is not stated, use the one the dish implies: a stir-fry is
   stir-fried, a curry is cooked, a steak is grilled.
2. GIVE EVERY COMPONENT A PORTION, scaled to the size he described, and say it is a guess
   by setting portion_estimated true. A per-100g figure with no portion is not an answer,
   and "large" and "small" are real information: a large bowl of noodles is not 100 g.
   Ordinary as-eaten weights for a LARGE serving: noodles/pasta/rice 300 g cooked,
   meat 150 g, vegetables 200 g, sauce 40 g, cooking oil 15 g. For a SMALL serving take
   roughly half, for an unqualified one roughly two thirds. Cooking oil is a component of
   a stir-fry whether or not he mentioned it, but keep it to the teaspoons actually used -
   never a 100 g portion of oil.
   portion_estimated is false ONLY when the grams come from HIS words ("300g of noodles").
   The logger states every estimate on the line he confirms, so a wrong guess costs one
   message; a missing portion costs the whole meal.

Message: %s
"""

# --- composed meals: one full-intelligence call, no ladder ------------------

MEAL_TABLE_PROMPT = """You are a sports nutritionist costing ONE meal that this athlete \
cooked and ate. He has described it in his own words. Produce the table.

Reply with ONLY a JSON object:

{"meal_name": "<short plain name for the meal, e.g. 'Large beef stir-fry with egg
                noodles'>",
 "components": [
   {"name": "<the component, in the state it was EATEN: 'egg noodles, cooked', not
             'dried'>",
    "portion_g": <as-eaten grams>,
    "portion_basis": "<how you arrived at that weight, in a few words: 'a large bowl of
                      cooked noodles', 'his words: 300g', '2 tsp for the pan'>",
    "kcal": <n>, "protein_g": <n>, "carb_g": <n>, "fat_g": <n>, "fibre_g": <n>}],
 "total": {"kcal": <n>, "protein_g": <n>, "carb_g": <n>, "fat_g": <n>, "fibre_g": <n>},
 "error_band_pct": <your honest uncertainty on the total energy, as a percentage>,
 "plants": ["<each distinct plant species in the meal, named plainly: 'garlic', 'ginger',
             'onion', 'red pepper', 'wheat'>"],
 "assumptions": ["<each thing you had to assume, one per line, in plain English>"]}

How to do it well:
- COOK THE FOOD. Every weight and every figure is for the food AS EATEN. Dried noodles are
  338 kcal per 100 g and boiled ones 166; raw steak and grilled steak are different foods.
  A log records what went in his mouth.
- SIZE IT FROM HIS WORDS. "Large", "small", "a bowl of", "a bit of" are real information.
  Ordinary as-eaten weights for a LARGE serving: noodles/pasta/rice around 300 g cooked,
  meat 150 g, vegetables 200 g, sauce 40 g. Roughly half that for a small one, two thirds
  for an unqualified one.
- INCLUDE WHAT COOKING ADDS. A stir-fry has oil in it whether or not he mentioned it, and
  it is the single biggest thing missed when a meal is costed component by component - but
  it is the teaspoons that went in the pan, not a 100 g portion of oil.
- DO NOT PAD THE MEAL. Components he did not describe and the cooking does not require are
  not in his dinner.
- SAY WHAT YOU ASSUMED. Every portion you inferred and every ingredient you added belongs
  in `assumptions`, in his language, because he reads them and corrects the ones that are
  wrong. An unstated assumption is the failure mode here, not an inaccurate gram.
- BE HONEST ABOUT THE BAND. A described home-cooked meal is typically +/-15-20%%. Say so
  rather than implying precision you do not have.
- `plants` is every distinct plant species that was really in it, whole foods and the herbs
  and spices, named simply. Not brands, not dishes, not "vegetables".
- Component figures must be for the portion_g you stated, NOT per 100 g.
- The total is the sum of the components. Do not round it to something tidy.

His description: %s
"""


# What a costed meal may contain, and the most any one figure may be. Bounds rather than
# judgements: this rung is a model estimate, so the code's job is to catch a table that is
# arithmetically absurd before it becomes a day's food record.
_MEAL_BOUNDS = {"kcal": 8000, "protein_g": 600, "carb_g": 1200, "fat_g": 600,
                "fibre_g": 200}
MEAL_MAX_COMPONENTS = 16
# Above this, the model's own total and the sum of its components disagree enough that the
# table is not internally consistent, and the entry would not survive being corrected
# component by component.
MEAL_TOTAL_TOLERANCE = 0.10


def _meal_figures(block, bounds=None) -> dict:
    """The macro fields of one row, coerced and bounded. {} if there is nothing usable."""
    out = {}
    for field, ceiling in (bounds or _MEAL_BOUNDS).items():
        raw = block.get(field)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= value <= ceiling:
            out[field] = round(value, 1)
    return out


def describe_meal(text: str, claude_bin: str, model: str, log=print, runner=None,
                  timeout: int = 120) -> dict | None:
    """Cost one composed meal with a single full-intelligence call. None if unusable.

    JAMIE, 14 AUG 2026, AND HE IS RIGHT: "I literally went on a generic Opus 5 and told it
    what I ate and it gave me that table... we have access to any Claude model and we can't
    do shit". A described home-cooked meal is the one thing this whole ladder cannot do. The
    composition tables hold ingredients, not dinners, so a meal had to be broken into parts,
    each part looked up separately, and the answer assembled from four rows that each missed
    the portion, the cooking and the oil - 447 kcal for a 980 kcal dinner.

    So this path does not walk the ladder at all. It hands the athlete's own words to a
    capable model and asks for the table he would have got by asking one himself.

    THIS DELIBERATELY BREAKS THE MODULE'S OLDEST INVARIANT - that the model decides meaning
    and the code does every number - and the exception is scoped as narrowly as it can be.
    It applies ONLY to meals no database can know: anything branded, barcoded, labelled, or
    a single whole food keeps its deterministic path, because for those the ladder is
    genuinely better. What is returned is labelled an ESTIMATE, carries the model's own
    error band, and states every assumption on the message he confirms - so it is a
    declared estimate rather than a figure wearing a source's authority.

    The arithmetic stays the code's: the entry total is the SUM of the components, computed
    here, and a table whose own total disagrees with its parts is rejected rather than
    reconciled. That keeps a per-component correction ("the noodles were 400 g") exact."""
    runner = runner or subprocess.run
    try:
        proc = runner([claude_bin, "--print", "--model", model],
                      input=MEAL_TABLE_PROMPT % text, capture_output=True, text=True,
                      timeout=timeout)
    except Exception as exc:
        log(f"describe_meal failed: {exc}")
        return None
    raw = (getattr(proc, "stdout", "") or "").strip()
    if not raw or model_unavailable(raw):
        log("describe_meal: model unavailable")
        return None
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        log(f"describe_meal: no JSON; out={raw[:140]!r}")
        return None
    try:
        got = json.loads(raw[a:b + 1])
    except json.JSONDecodeError:
        log("describe_meal: unparseable JSON")
        return None
    components = []
    for row in (got.get("components") or [])[:MEAL_MAX_COMPONENTS]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        figures = _meal_figures(row)
        # A row with no name or no energy is not a component of anything. Dropped rather
        # than defaulted: a zero-kcal component reads as a measurement.
        if not name or not figures.get("kcal"):
            continue
        portion = None
        try:
            portion = round(float(row.get("portion_g")), 1)
        except (TypeError, ValueError):
            portion = None
        if portion is not None and not 0 < portion <= MAX_PORTION_G:
            portion = None
        components.append({"name": name[:80], "portion_g": portion,
                           "portion_basis": str(row.get("portion_basis") or "").strip()[:90],
                           **figures})
    if not components:
        log("describe_meal: a meal with no usable components")
        return None
    # THE SUM IS THE TOTAL, computed here. A model's addition is not a source of truth, and
    # the components are what a correction is applied to - so if the entry total came from
    # somewhere else, rescaling one component would leave the two disagreeing.
    total = {}
    for field in _MEAL_BOUNDS:
        parts = [c[field] for c in components if c.get(field) is not None]
        if parts:
            total[field] = round(sum(parts), 1)
    stated = _meal_figures(got.get("total") or {})
    if stated.get("kcal") and total.get("kcal"):
        drift = abs(stated["kcal"] - total["kcal"]) / total["kcal"]
        if drift > MEAL_TOTAL_TOLERANCE:
            # Not reconciled, REFUSED. A table whose own total is 20% from the sum of its
            # rows is not a table anyone can correct a line of, and quietly preferring one
            # number over the other would hide that from him.
            log(f"describe_meal: total {stated['kcal']} is {drift:.0%} from the sum of its "
                f"components ({total['kcal']}); refusing the table")
            return None
    try:
        band = float(got.get("error_band_pct"))
    except (TypeError, ValueError):
        band = 0.0
    return {
        "meal_name": (str(got.get("meal_name") or "").strip()[:90]
                      or "meal as you described it"),
        "components": components,
        "total": total,
        # Bounded to something sayable. A band of 0 is the model claiming precision it does
        # not have, so a floor is applied rather than repeating the claim.
        "error_band_pct": round(min(max(band, 10.0), 40.0)),
        "plants": [str(p).strip()[:40] for p in (got.get("plants") or [])
                   if str(p or "").strip()][:30],
        "assumptions": [str(a).strip()[:140] for a in (got.get("assumptions") or [])
                        if str(a or "").strip()][:12],
    }


_FORM_FAMILIES = {
    "dose": {"capsule", "tablet", "softgel", "powder"},
    "food": {"bar", "drink", "whole_food", "prepared_meal", "bakery",
             "confectionery", "dairy", "liquid", "other"},
}


# What a photo of each kind establishes about the items in it. This lives here, rather
# than inline in handle_photo where it started, so that a test can reach it: the order
# branch shipped with its hint never arriving at the ladder and nothing could see that.
_PHOTO_HINT_BY_KIND = {
    # A named vendor cooked this. It is not a row in a whole-food composition table, and
    # not saying so is how a Wagamama order came back as 357 kcal of raw brown rice.
    "order": {"category": "restaurant_dish", "form": "prepared_meal"},
    # Components of a plate genuinely ARE whole foods, so CoFID is the right rung for
    # them. Stated explicitly rather than achieved by leaving the hint empty.
    "food_plate": {"category": "whole_food"},
}


def photo_item_hints(got: dict) -> list:
    """Annotate the items read off a photo with what the photo itself established.

    read_photo works out the kind and the vendor, and until this existed the caller threw
    both away before resolution. Every hint-driven guard in the ladder - the CoFID skip
    for anything that is not a whole food, the form-conflict check - was therefore inert
    on the photo path while looking perfectly wired on the text path."""
    items = got.get("items") or []
    base = _PHOTO_HINT_BY_KIND.get(got.get("kind"))
    if base is None:
        return items
    for it in items:
        hint = dict(base)
        hint.update({"canonical_name": it["text"], "search_terms": [it["text"]],
                     "expect_macros": True})
        if got.get("vendor"):
            hint["brand"] = got["vendor"]
        it.setdefault("hint", hint)
    return items


def form_family(form: str) -> str:
    for fam, forms in _FORM_FAMILIES.items():
        if (form or "").lower() in forms:
            return fam
    return "unknown"


def interpret(text: str, claude_bin: str, model: str, log=print, runner=None,
              timeout: int = 90) -> dict | None:
    """Plan the lookup before doing it. Returns {'items': [...]} or None.

    Jamie's design, and it is better than what it replaces. The ladder used to search the
    athlete's raw sentence, which is a poor query: "400mg of my protein collagen capsules"
    matched a COLLAGEN PROTEIN BAR because the sentence happens to contain the word
    protein. Every fix I bolted on after that was a hand-written word list trying to guess
    what the sentence meant.

    So the model goes FIRST, as an interpreter: what is this, what form is it, what should
    we search for. It still never supplies a NUMBER, which is the property that made the
    ladder trustworthy in the first place - first for meaning, last for macros.

    The returned `form` and `category` are what the ladder validates hits against, which
    replaces guesswork with a stated expectation."""
    runner = runner or subprocess.run
    try:
        proc = runner([claude_bin, "--print", "--model", model],
                      input=INTERPRET_PROMPT % text, capture_output=True, text=True,
                      timeout=timeout)
    except Exception as exc:
        log(f"interpret failed: {exc}")
        return None
    raw = (getattr(proc, "stdout", "") or "").strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        log(f"interpret: no JSON; out={raw[:140]!r}")
        return None
    try:
        got = json.loads(raw[a:b + 1])
    except json.JSONDecodeError:
        log("interpret: unparseable JSON")
        return None
    out = []
    for it in got.get("items") or []:
        if not isinstance(it, dict) or not (it.get("canonical_name") or it.get("search_terms")):
            continue
        terms = [t for t in (it.get("search_terms") or []) if isinstance(t, str) and t.strip()]
        name = (it.get("canonical_name") or (terms[0] if terms else "")).strip()
        if not terms:
            terms = [name]
        out.append({
            "canonical_name": name,
            # A brand is only ever what the athlete said. A model-invented brand is how a
            # confident wrong product gets chosen.
            "brand": (it.get("brand") or None),
            "form": (it.get("form") or "other").lower(),
            "category": (it.get("category") or "other").lower(),
            "is_supplement": bool(it.get("is_supplement")),
            "expect_macros": it.get("expect_macros", True) is not False,
            "portion_g": it.get("portion_g"),
            # WHOSE NUMBER THE PORTION IS. resolve() treats a portion the caller hands it
            # as stated fact and flags nothing, so a portion the model reasoned out for a
            # described meal ("a large stir fry" -> 300 g of noodles) would reach the offer
            # looking like a weight he gave. The offer has to be able to say it guessed.
            "portion_estimated": bool(it.get("portion_estimated")),
            "dose_mg": it.get("dose_mg"),
            "count": it.get("count"),
            "in_session": bool(it.get("in_session")),
            # Carried on BOTH parse paths. interpret() returns None whenever the model
            # is unavailable, in which case the caller resolves classify()'s items
            # instead - so a stated time that only survived here would be lost exactly
            # when the fallback ran. The meal he named travels for the same reason, and
            # this is the path that usually wins, so it is the one that matters most.
            "at": normalise_hhmm(it.get("at")),
            "meal": ("" if it.get("in_session") else normalise_meal(it.get("meal"))),
            "search_terms": terms[:4],
        })
    return {"items": out} if out else None
