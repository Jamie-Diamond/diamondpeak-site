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
import subprocess

INTENTS = ("log_food", "log_weight", "log_supplement", "question", "advice",
           "correction", "command", "confirm", "cancel", "smalltalk", "unknown")

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


def fast_intent(text: str, has_pending: bool) -> dict | None:
    """Deterministic classification, no model call. None means "ask the model"."""
    t = (text or "").strip()
    low = t.lower().rstrip("!. ")
    if not t:
        return {"intent": "unknown"}
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
    return None


PARSE_PROMPT = """You are the message parser for a personal nutrition logging bot. \
Classify the message and extract structure. Reply with ONLY a JSON object, no prose.

Keys:
  intent: one of log_food, log_supplement, question, advice, correction, smalltalk,
          unknown
  items: array (log_food/log_supplement only) of {text, portion_g, in_session}
         - text: a SINGLE food or supplement, self-contained enough to look up
         - portion_g: grams if stated or confidently inferable, else null
         - in_session: true only if eaten DURING a training session (gel, drink mix,
           "on the bike", "mid-run")
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
  - If they are amending something just logged, use correction.
  - NEVER invent nutrition numbers. You extract text and portions only.

Message: %s
"""


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
            items.append({"text": it, "portion_g": None, "in_session": False})
        elif isinstance(it, dict) and it.get("text"):
            items.append({"text": str(it["text"]),
                          "portion_g": it.get("portion_g"),
                          "in_session": bool(it.get("in_session"))})
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

Be conversational and concise: a few sentences. Give a recommendation, say why, and \
name the trade-off. It is fine to disagree with him.

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

PHOTO_PROMPT = """Read the image at %s. It is one of three things. Reply with ONLY a \
JSON object, no prose.

If it is a BARCODE:
  {"kind":"barcode","barcode":"<the digits, exactly as printed>"}

If it is a NUTRITION LABEL or ingredients panel:
  {"kind":"nutrition_label","per":"100g"|"portion","portion_g":<grams or null>,
   "kcal":n,"protein_g":n,"carb_g":n,"fat_g":n,"fibre_g":n,
   "dietary_sodium_mg":n or null,"salt_g":n or null,
   "product":"<name if visible>","ingredients":"<the ingredients list, verbatim>"}
  Transcribe the printed figures EXACTLY. Do not convert, round or estimate them.
  Report salt_g separately if the panel gives salt rather than sodium; do not convert.

If it is a PLATE OF FOOD (no label visible):
  {"kind":"food_plate","items":[{"text":"<single food>","portion_g":<estimate or null>}]}
  Identify the components. Estimate portions only where you reasonably can.
  Do NOT provide any nutrition figures: those are looked up separately.

If you cannot tell, reply {"kind":"unknown"}.
"""

SALT_TO_SODIUM = 1 / 2.5   # UK labels give salt; sodium = salt / 2.5


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
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"kind": "unknown"}
    try:
        got = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"kind": "unknown"}
    if got.get("kind") not in ("barcode", "nutrition_label", "food_plate"):
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
    if got["kind"] == "food_plate":
        got["items"] = [{"text": str(i["text"]), "portion_g": i.get("portion_g"),
                         "in_session": False}
                        for i in (got.get("items") or [])
                        if isinstance(i, dict) and i.get("text")]
        if not got["items"]:
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
    for f in fields:
        v = label.get(f)
        if v not in (None, ""):
            try:
                out[f] = round(float(v) * factor, 1)
            except (TypeError, ValueError):
                pass
    if "dietary_sodium_mg" in out:
        out["dietary_sodium_mg"] = round(out["dietary_sodium_mg"])
    out["resolved_name"] = label.get("product") or "item from label photo"
    out["ingredients"] = label.get("ingredients") or ""
    out["source_url"] = "photo of the product label"
    return out
