#!/usr/bin/env python3
"""nutrition_bot.py - the nutrition logger. A SECOND Telegram bot, not an extension
of the coach.

Own token, own long-poll process, own systemd unit. Shares lib/ (icu_api, plants,
nutrition_engine, nutrition_store, nutrition_resolve, tg) and config/athletes.json.
Separate bot must never mean separate codebase.

Why separate at all - and NOT for the reasons the incoming spec gave. The spec
claimed the coach is "stateless per exchange" and "reactive"; both are false in this
codebase (bot.py has load_history/save_history, _chat_lock, _stash_pending_capture,
day_overrides, and morning/evening/night-before cron sends). The real reasons:
bot.py is a 5,000-line single long-poll loop over one token and multiplexing a
second token through it is worse than a second unit; and food chatter would pollute
coach history and trip its rule-capture guard.

THE CONVERSATIONAL CONTRACT
Nothing is ever written without confirmation. A silently wrong entry corrupts the
longitudinal record and the athlete has no reason to notice. On a correction the item
is RE-PARSED rather than patched, because patching a misparse tends to preserve the
misparse.

Replies are short: mobile, mid-day, one hand. Running totals against the zone plus at
most one flag line. Full breakdowns belong on the tracking page.

WHAT THE REPLY MUST ALWAYS SAY
The resolution rung. An estimate must never render like label data, and a degraded
resolution must say a better source failed. See nutrition_resolve.describe_provenance.

PENDING CONFIRMATIONS ARE PERSISTED, not held in memory. The watchdog restarts this
process, and an in-memory pending item would leave the athlete's "yes" answering a
question the bot had forgotten - it would either do nothing or, worse, attach to the
next item.

NO STREAKS, SCORES OR RESTRICTION FRAMING (spec 10.4). Missing a fibre ceiling on a
pre-long-ride day is COMPLIANCE. The formatting helpers below never render a ceiling
as a progress bar and never emit a grade.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

import nutrition_engine as NE       # noqa: E402
import nutrition_gate as NG         # noqa: E402
import nutrition_nlu as NLU         # noqa: E402
import nutrition_reconcile as RC
import nutrition_resolve as NR      # noqa: E402
import plants as PL                 # noqa: E402
import restaurants as RS            # noqa: E402
import tg                           # noqa: E402
from icu_api import IcuClient       # noqa: E402
from nutrition_store import NutritionStore  # noqa: E402

CONFIG = Path(__file__).resolve().parent / "nutrition_config.json"
ATHLETES = BASE / "config" / "athletes.json"
LOG_FILE = Path(__file__).resolve().parent / "nutrition_bot.log"

POLL_TIMEOUT = 30
# 30, matching the coach bot, which polls the same way against the same 65 second
# socket and has never dropped an update. At 60 there were five seconds of margin,
# and every poll was expiring on the SOCKET before Telegram replied - failures
# exactly 65 seconds apart, a valid token, a healthy network and a bot that heard
# nothing. The socket timeout below is derived from this rather than inherited from
# a default that lives in another file.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
LLM_MODEL = "claude-sonnet-5"
# COSTING A COOKED MEAL GETS THE BEST MODEL AVAILABLE, and it is named here rather than
# taken from LLM_MODEL on purpose (Jamie, 14 Aug 2026: "I literally went on a generic
# Opus 5 and told it what I ate and it gave me that table... we have access to any Claude
# model and we can't do shit"). Every other call in this file is classification or
# extraction, where the faster model is the right trade. This one is the whole answer for a
# meal no database holds, it happens a few times a day, and a cheaper model costing his
# dinner badly is what the ladder was already doing.
MEAL_MODEL = "claude-opus-5"

HELP = (
    "Just talk to me normally. Some examples:\n\n"
    "\"half a bag of M&S nut collection\"\n"
    "\"porridge with blueberries and a flat white\"\n"
    "\"83.4 this morning\"\n"
    "\"gel on the bike\"\n"
    "\"how much protein have I had?\"\n"
    "\"should I have the pasta or the rice tonight?\"\n"
    "a photo of a barcode, a nutrition label, or your plate\n"
    "\"no, it was the whole bag\"\n\n"
    "Commands exist if you prefer them:\n"
    "/today  totals and zones\n"
    "/week  7-day view\n"
    "/plants  plant diversity\n"
    "/undo  remove the last entry\n"
    "/edit  re-log the last entry\n"
    "/close  close the day\n"
    "/target  today's zones and where the numbers come from"
)


def log(msg):
    """Print only. The systemd unit appends stdout to nutrition_bot.log, so writing
    the file here as well put every line in it TWICE - visible on the very first
    startup line after install. Duplicated logging is a recurring shape in this
    codebase (see the coach's duplicate-notify bug), so it gets one owner: systemd.\n/tomorrow - what tomorrow's session needs, and what to do with the rest of today\n"""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# --- config -----------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG.exists():
        raise SystemExit(
            f"missing {CONFIG}. Copy nutrition_config.json.example and add the "
            f"BotFather token. It is gitignored.")
    return json.loads(CONFIG.read_text())


def load_athlete(slug: str) -> dict:
    return json.loads(ATHLETES.read_text())[slug]


# --- pure formatting (tested offline) ---------------------------------------

def fmt_zone(name: str, consumed, zone: dict) -> str:
    """One macro line: consumed against its zone, with the BIAS made visible.

    A ceiling is written as `<= n`, never as `consumed / target`, because a bar
    reading low against a ceiling looks like failure when it is compliance. A floor
    shows the floor only: exceeding it is not an event and must not render as one."""
    c = 0 if consumed is None else round(float(consumed))
    lo, hi, bias = round(zone["low"]), round(zone["high"]), zone["bias"]
    if bias == NE.BIAS_CEILING:
        mark = "ok" if c <= hi else "over"
        return f"{name} {c} (ceiling {hi}, {mark})"
    if bias == NE.BIAS_FLOOR:
        mark = "ok" if c >= lo else f"{lo - c} short"
        return f"{name} {c} (floor {lo}, {mark})"
    return f"{name} {c} ({lo}-{hi})"


def fmt_totals(totals: dict, z: dict) -> str:
    """The running-totals block. Short by design."""
    lines = [
        f"*{round(totals.get('kcal') or 0):,} / {z['kcal_target']:,} kcal*"
        + (f"  ({z['kcal_confidence']})" if z.get("kcal_confidence") == "estimated" else ""),
        fmt_zone("P", totals.get("protein_g"), z["protein_g"]),
        fmt_zone("C", totals.get("carb_g"), z["carb_g"]),
        fmt_zone("F", totals.get("fat_g"), z["fat_g"]),
        fmt_zone("Fibre", totals.get("fibre_g"), z["fibre_g"]),
    ]
    if totals.get("non_counting_protein_g"):
        # Collagen is shown but excluded from the protein figure, so say so or the
        # two numbers look like an arithmetic error.
        lines.append(f"(+{round(totals['non_counting_protein_g'])} g collagen, "
                     f"not counted toward protein)")
    if totals.get("dietary_sodium_mg"):
        lines.append(f"Sodium {round(totals['dietary_sodium_mg']):,} mg (no target set)")
    if totals.get("in_session_carb_g"):
        split = NE.split_carbs(totals)
        lines.append(f"_of which {split['in_session_g']:.0f} g carbs in-session, "
                     f"{split['out_of_session_g']:.0f} g out._")
    if totals.get("in_session_from_coach"):
        lines.append(f"_Includes {round(totals['in_session_carb_g'])} g of ride fuel "
                     f"from the coach bot (energy derived from carbs)._")
    if totals.get("fuel_disagrees"):
        lines.append("_Heads up: the two bots have different ride-fuel figures. I am "
                     "using the itemised one._")
    return "\n".join(lines)


def fmt_flags(flags: list, limit: int = 1) -> str:
    """At most one flag line in chat. The rest belong on the tracking page."""
    if not flags:
        return ""
    f = flags[0]
    macro = f["macro"].replace("_g", "").replace("kcal", "energy")
    if f["direction"] == "cannot_reach_floor":
        return f"\n_{macro} is projecting {round(f['distance'])} short of its floor._"
    return f"\n_{macro} is {round(f['distance'])} over its ceiling._"


def fmt_confirm(item: dict) -> str:
    """The confirm prompt. States the rung every time."""
    # HIS OWN FIGURES SAY SO IN HIS OWN TERMS. describe_provenance renders the MANUAL rung
    # as "from the pack, as you gave it", which is wrong for a meal he reckoned up himself,
    # and an `estimate` confidence would add "roughly +/-10-15%" to numbers whose accuracy
    # is his business rather than ours.
    bits = [f"*{item['resolved_name']}*",
            ("Your figures, logged exactly as you gave them."
             if item.get("_stated")
             # describe_provenance returns the rung name itself for a rung it has no phrase
             # for, which is None when an item carries no rung at all - and a None in here
             # raises inside the join, so he gets NO reply rather than a slightly vague one.
             else NR.describe_provenance(item) or "source not recorded")]
    if item.get("needs_portion"):
        per = item.get("per_100g") or {}
        k = per.get("kcal")
        return (f"*{item['resolved_name']}*\nFound the label, but it is per 100 g and I "
                f"could not find the pack size"
                + (f" ({round(float(k))} kcal per 100 g)." if k else ".")
                + "\nHow much did you have? Grams, or the pack size.")
    if item.get("needs_input"):
        return "\n".join(bits)
    macros = " · ".join(
        f"{lbl} {round(item[k])}" for lbl, k in
        (("kcal", "kcal"), ("P", "protein_g"), ("C", "carb_g"), ("F", "fat_g"))
        if item.get(k) is not None)
    bits.append(macros)
    if item.get("portion_estimated"):
        # An ASSUMED amount is stated on the line he reads before confirming, never only in
        # the stored record. That is the condition the default portions were added on: a
        # teaspoon is a fair reading of "one teaspoon", but he has to be able to see it was
        # a reading and not a measurement.
        bits.append(f"_assumed {item.get('portion_assumed') or 'a standard portion'}; "
                    f"correct me if wrong._")
    if item.get("fibre_g"):
        bits.append(f"fibre {round(item['fibre_g'])} g")
    if item.get("_components"):
        # His breakdown, echoed back so he can see the rows were kept rather than
        # re-interpreted. Capped: this is a confirm message, not a spreadsheet.
        bits.append("_" + "; ".join(c[:60] for c in item["_components"][:6]) + "_")
    if item.get("species"):
        bits.append(f"{len(item['species'])} plant"
                    f"{'s' if len(item['species']) != 1 else ''}")
    return "\n".join(bits)


def fmt_target(z: dict) -> str:
    """/target - the zones AND where they come from. The basis strings exist so the
    athlete can see which numbers are sourced and which are reasoned."""
    lines = [f"*{z['day_type'].replace('_', ' ')}*"
             + (" (guessed from your usual week)"
                if z.get("confidence") == "low_confidence" else ""),
             f"{z['kcal_target']:,} kcal target"
             f" (maintenance {z['kcal_maintenance']:,})"]
    if z["deficit_applied_kcal"]:
        lines.append(f"deficit {z['deficit_applied_kcal']} kcal")
    if z.get("carb_load_surplus_kcal"):
        lines.append(f"carb load: {z['carb_load_surplus_kcal']:+,} vs maintenance")
    for label, key in (("Protein", "protein_g"), ("Carbs", "carb_g"),
                       ("Fat", "fat_g"), ("Fibre", "fibre_g")):
        zone = z[key]
        word = {NE.BIAS_FLOOR: "floor", NE.BIAS_CEILING: "ceiling",
                NE.BIAS_BAND: "band"}[zone["bias"]]
        lines.append(f"{label} {round(zone['low'])}-{round(zone['high'])} g "
                     f"({word}) - {zone['basis']}")
    for m in z.get("modifiers") or []:
        lines.append(f"· {m}")
    for w in z.get("warnings") or []:
        lines.append(f"! {w}")
    return "\n".join(lines)


def fmt_plants(div: dict) -> str:
    """Plant diversity. A variety prompt, never a score: no streak, no grade, and the
    evidence for the 30 figure stated rather than implied."""
    lines = [f"*{div['unique_7d']} plants* in 7 days (aiming around {div['target']})",
             f"{div['new_species_today']} new today"]
    if div.get("herb_spice_count"):
        lines.append(f"{div['herb_spice_count']} of them herbs or spices "
                     f"(counted as a quarter each)")
    if div.get("new_species_today_names"):
        lines.append("New: " + ", ".join(div["new_species_today_names"][:8]))
    lines.append("_30 is a variety prompt from an observational study, not a "
                 "threshold. 28 vs 32 means nothing._")
    return "\n".join(lines)


def parse_weight(text: str):
    """A bare number, or `weight 83.4`. Returns kg or None.

    Bounded to a plausible human range: an unbounded parse turns a mistyped food
    quantity into a weight reading, and a bad weight moves the rolling mean that the
    deficit is driven from."""
    t = (text or "").strip().lower().replace("kg", "").replace("weight", "").strip()
    try:
        val = float(t)
    except ValueError:
        return None
    return val if 40.0 <= val <= 200.0 else None


# --- ICU: day classification with the +1 lookahead --------------------------

def classify_today_and_tomorrow(icu: IcuClient, today: date, day_rules: dict):
    """Returns (today_type, tomorrow_type, confidence).

    The lookahead is mandatory, not a refinement: a rest day BEFORE a long ride is a
    low-fibre day even though its own type says high. Same day type, opposite bias,
    decided entirely by tomorrow's calendar.

    Today prefers COMPLETED activities and falls back to planned; tomorrow can only
    ever be planned. An empty calendar is NOT taken as rest - that would invert the
    fibre advice when a long ride is actually planned - so it falls back to the
    athlete's stated typical week and marks the day low_confidence."""
    start = today.isoformat()
    end = (today + timedelta(days=1)).isoformat()
    try:
        events = icu.get_events(start, end) or []
    except Exception as exc:
        log(f"icu events failed: {exc}")
        events = []
    try:
        done = [a for a in (icu.get_training_history(days=2) or [])
                if (a.get("start_date_local") or "")[:10] == start]
    except Exception as exc:
        log(f"icu history failed: {exc}")
        done = []

    def for_date(rows, d):
        return [r for r in rows
                if (r.get("start_date_local") or r.get("date") or "")[:10] == d]

    # COMPLETED PLUS STILL PLANNED, not one or the other.
    #
    # Preferring completed activities meant today was classified off a 42 minute swim while
    # a 33 km long run with a tempo finish sat on the calendar for the afternoon: day_type
    # came back "recovery", the targets were recovery-sized, and fibre was set as a FLOOR of
    # 40 g - on the morning of a long run, having correctly been flipped to a ceiling
    # yesterday when the same run was still "tomorrow".
    #
    # The asymmetry decides it. Fuelling for a session he then skips leaves him a few
    # hundred kcal up, which the rolling weight correction absorbs. Under-fuelling a 33 km
    # run because the plan was invisible costs him the session.
    planned_today = for_date(events, start)
    seen_ids = {(a.get("id") or a.get("activity_id")) for a in done}
    # "_done" is tagged here, not re-derived downstream: it is the ONLY place that still
    # knows which list a session came from once the two are merged, and today_brief needs
    # that to say "done" vs "still to come" rather than presenting both as identical.
    for a in done:
        a["_done"] = True
    remaining_today = [e for e in planned_today
                       if (e.get("id") or e.get("activity_id")) not in seen_ids
                       and not e.get("paired_activity_id")]
    for e in remaining_today:
        e["_done"] = False
    today_sessions = done + remaining_today
    tomorrow_sessions = for_date(events, end)

    confidence = "normal"
    if today_sessions:
        today_type = NE.classify_day(today_sessions)
    else:
        today_type, confidence = NE.classify_from_day_rules(today, day_rules)
    if tomorrow_sessions:
        tomorrow_type = NE.classify_day(tomorrow_sessions)
    else:
        tomorrow_type, _ = NE.classify_from_day_rules(today + timedelta(days=1), day_rules)
    # Tomorrow's SESSIONS come back too, not only their classification. They were being
    # computed and dropped, which is why the bot could shift today's fibre ceiling
    # "because of a long session tomorrow" and still be unable to say what that session
    # is. The whole reason it reads dumb is that this detail never left this function.
    return (today_type, tomorrow_type, confidence, today_sessions, tomorrow_sessions)


# --- the LLM rung -----------------------------------------------------------

def make_llm_fetch(log=log):
    """Bottom rung: a model estimate, clearly labelled as one.

    Asks for JSON only and refuses anything it cannot parse, returning None so the
    ladder records a miss rather than inventing numbers. A malformed reply must not
    become a confident zero."""
    def fetch(text: str, portion_g=None):
        prompt = (
            "You are a UK nutrition database lookup. Estimate the nutrition for the "
            "food described. Reply with ONLY a JSON object, no prose, with keys: "
            "resolved_name, kcal, protein_g, carb_g, fat_g, fibre_g, "
            "dietary_sodium_mg, ingredients. ingredients should be a plain "
            "comma-separated list of the plant and animal ingredients you believe are "
            "present, for species tagging. Figures are for the WHOLE portion "
            "described, not per 100 g. If you genuinely cannot estimate, reply {}.\n\n"
            f"Food: {text}\n"
            + (f"Portion: {portion_g} g\n" if portion_g else ""))
        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "--print", "--model", LLM_MODEL],
                input=prompt, capture_output=True, text=True, timeout=90)
        except Exception as exc:
            log(f"llm rung failed: {exc}")
            return None
        raw = (proc.stdout or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            got = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
        if not got or got.get("kcal") in (None, ""):
            return None
        return got
    return fetch


DEEP_PROMPT = """Find the nutrition LABEL for this exact product. Search the web if you \
need to; prefer the manufacturer's own page, then a UK retailer listing (Ocado, Tesco, \
M&S, Sainsbury's), then a reputable database.

Report the label AS PRINTED. Do not convert anything.

Reply with ONLY JSON:
{"resolved_name":"...",
 "per":"100g"|"portion"|"serving",
 "pack_g":<the pack or serving size in grams that the figures refer to, or null>,
 "kcal":n,"protein_g":n,"carb_g":n,"fat_g":n,"fibre_g":n,
 "dietary_sodium_mg":n or null,"salt_g":n or null,
 "ingredients":"<verbatim list if you find one>",
 "source_url":"<the page you used>",
 "source_kind":"manufacturer|retailer|database|estimate"}

Rules:
- `per` and `pack_g` are REQUIRED for the figures to be usable. If the label gives
  per-100g values, say per:"100g" and put the pack size in pack_g. If it gives
  per-pack values, say per:"portion" and put that pack size in pack_g.
- Do NOT scale anything yourself. Report what is printed and say which basis it is.
- salt_g if the label gives salt rather than sodium. Do not convert it.
- source_kind must be honest. Say "estimate" if you did not find the actual product.
- Return {} rather than guessing at a product you could not find, or if the form does
  not match (a capsule is not a bar).
- If the request names NO BRAND - "a ginger shot", "an oat drink" - do not pick a branded
  product to stand in for it. "100ml ginger shot" was answered with James White on one
  attempt and MOJU on the next: two brands, neither of them what he drank, and the first
  was a 70 ml bottle against the 100 ml he stated. Give a GENERIC composition for that kind
  of food with source_kind:"database", or return {} and let a lower rung answer. Naming a
  brand he did not is worse than a generic figure, because it looks specific.

Product: %s
Form: %s
Portion eaten: %s
%s"""

# Plausible energy density for real food, kcal per 100 g. Pure fat is ~900 and a
# zero-calorie drink is ~0, so anything outside this is a units or basis error rather
# than a food.
_MIN_KCAL_100G, _MAX_KCAL_100G = 0.0, 950.0
SALT_TO_SODIUM_MG = 400.0          # 1 g salt = 400 mg sodium


# Parsed chain menus live here for a week. The matrices are megabytes of HTML, so the
# rows are cached and the HTML is thrown away; a cache older than the TTL is a miss.
RESTAURANT_CACHE = BASE / "athletes" / "_shared" / "restaurant-cache"


def _restaurant_note(hint: dict) -> str:
    """Extra instructions when the thing is a dish from a named chain.

    Without this the prompt above is purely retail-shaped - "prefer the manufacturer's page,
    then a UK retailer listing (Ocado, Tesco, M&S)" - so for a Nando's dish it hunted
    supermarket listings for something only Nando's publishes, and came back with an
    estimate. Jamie found the same figures on the chain's own site in seconds, which is the
    fairest possible criticism of a nutrition lookup."""
    if not (hint and hint.get("category") == "restaurant_dish" and hint.get("brand")):
        return ""
    return (
        "\nRESTAURANT DISH from " + str(hint.get("brand")) + ". Search THAT CHAIN'S OWN "
        "site and its nutrition or allergen guide first - not supermarkets, and not a "
        "third-party copy of the menu. UK chains must publish calories by law, so the "
        "figures exist. A downloadable nutrition PDF is a good source; read it if you can.\n"
        "Per-portion figures with NO gram weight are normal and usable here: the portion IS "
        "the dish, so report per:\"portion\" with pack_g null rather than treating it as "
        "unusable. source_kind:\"manufacturer\" only for the chain's own published data.\n")


def make_deep_fetch(log=log):
    """The model doing a REAL search, with confidence set by what it lands on.

    This is the retailer rung, finally, and by a route that does not need a scraper per
    supermarket. A manufacturer or retailer page IS label data, so it is allowed to
    return `label`; anything vaguer comes back an estimate and is flagged as one."""
    def fetch(text, portion_g=None, hint=None):
        hint = hint or {}
        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "--print", "--model", LLM_MODEL,
                 "--allowedTools", "WebSearch,WebFetch"],
                input=DEEP_PROMPT % (
                    text, hint.get("form") or "unknown",
                    (f"{portion_g} g" if portion_g else "as described"),
                    _restaurant_note(hint)),
                capture_output=True, text=True, timeout=180)
        except Exception as exc:
            log(f"web rung failed: {exc}")
            return None
        raw = (proc.stdout or "").strip()
        a, b = raw.find("{"), raw.rfind("}")
        if a < 0 or b <= a:
            return None
        try:
            got = json.loads(raw[a:b + 1])
        except json.JSONDecodeError:
            return None
        if not got or got.get("kcal") in (None, ""):
            return None

        # WE do the arithmetic. Two different M&S prepared meals both came back as
        # "106 kcal", which is the signature of per-100g figures reported as per-portion:
        # the prompt asked for a portion and the model reported the label. Asking for the
        # basis and scaling here removes the ambiguity, rather than trusting an
        # instruction not to convert.
        fields = ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g")
        basis = (got.get("per") or "").lower()
        pack = got.get("pack_g")
        try:
            pack = float(pack) if pack else None
        except (TypeError, ValueError):
            pack = None
        eaten = portion_g or pack
        # ASK whenever the AMOUNT the figures refer to is unknown, whatever basis the
        # model claims. Restricting this to a declared per-100g basis was not enough: the
        # model returned per:"portion" while giving per-100g numbers, so two prepared
        # meals came back as 106 kcal each for a second time. A label figure without an
        # amount is unusable, and a claimed basis is not evidence.
        if not eaten:
            # Per-100g figures and no idea how much was eaten. ASK, do not assume
            # (Jamie's call, 10 Aug 2026). Assuming 100 g is what turned two prepared
            # meals into 106 kcal each, and a guess that lands inside a plausible range
            # is undetectable afterwards.
            log(f"web rung needs a portion for {got.get('resolved_name')!r}: "
                f"label is per 100 g and no pack size was found")
            return {"needs_portion": True,
                    "resolved_name": got.get("resolved_name") or text,
                    "per_100g": {f: got.get(f) for f in
                                 ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g")},
                    "ingredients": got.get("ingredients") or "",
                    "source_url": got.get("source_url") or "",
                    "source_kind": got.get("source_kind") or "estimate",
                    "confidence": "label"}
        if basis.startswith("100") and eaten:
            factor = eaten / 100.0
        elif pack and portion_g and pack > 0:
            factor = portion_g / pack          # per-pack figures, part of a pack eaten
        else:
            factor = 1.0
        for f in fields:
            if got.get(f) not in (None, ""):
                try:
                    got[f] = round(float(got[f]) * factor, 1)
                except (TypeError, ValueError):
                    got[f] = None
        if got.get("dietary_sodium_mg") in (None, "") and got.get("salt_g") not in (None, ""):
            try:
                got["dietary_sodium_mg"] = round(float(got["salt_g"]) * SALT_TO_SODIUM_MG
                                                 * factor)
                got["sodium_from_salt"] = True
            except (TypeError, ValueError):
                pass
        elif got.get("dietary_sodium_mg") not in (None, ""):
            try:
                got["dietary_sodium_mg"] = round(float(got["dietary_sodium_mg"]) * factor)
            except (TypeError, ValueError):
                got["dietary_sodium_mg"] = None

        # Plausibility, on the density rather than the total: a basis error survives every
        # other check because the numbers are internally consistent, just for the wrong
        # amount of food.
        if eaten and got.get("kcal") is not None:
            density = float(got["kcal"]) / eaten * 100.0
            if not (_MIN_KCAL_100G <= density <= _MAX_KCAL_100G):
                log(f"web rung rejected {got.get('resolved_name')!r}: "
                    f"{density:.0f} kcal/100g is not food")
                return None

        kind = (got.get("source_kind") or "estimate").lower()
        got["confidence"] = ("label" if kind in ("manufacturer", "retailer")
                            else "database" if kind == "database" else "estimate")
        got["portion_used_g"] = eaten
        return got
    return fetch


def build_fetchers(cfg: dict) -> dict:
    """Wire the ladder from config. A key that is absent leaves that rung
    not_configured, which is reported on every item rather than hidden."""
    deep = make_deep_fetch()
    fetchers = {NR.Rung.LLM: make_llm_fetch(),
                # takes a hint, so it is wrapped to the (text, portion) signature the
                # ladder calls with; offer_planned rebinds it per item with the form
                NR.Rung.WEB: lambda t, p, _d=deep: _d(t, p)}
    # The name-search databases are OFF the default path: each lost to a plain web
    # search, and USDA is what matched "collagen capsules" to "Soy protein isolate". They
    # are re-enabled per athlete with enable_name_databases, for anyone who wants a
    # deterministic rung ahead of the model even at the cost of accuracy.
    if cfg.get("enable_name_databases"):
        if cfg.get("fdc_api_key"):
            key = cfg["fdc_api_key"]
            fetchers[NR.Rung.USDA] = lambda t, p, _k=key: NR.usda_fetch(t, p, api_key=_k)
        fetchers[NR.Rung.OFF] = NR.off_fetch
        if cfg.get("nutritionix_app_id") and cfg.get("nutritionix_app_key"):
            aid, akey = cfg["nutritionix_app_id"], cfg["nutritionix_app_key"]
            fetchers[NR.Rung.NUTRITIONIX] = (
                lambda t, p, _a=aid, _k=akey: NR.nutritionix_fetch(t, p, app_id=_a,
                                                                   app_key=_k))
    return fetchers


# --- pending confirmations (persisted) --------------------------------------

def pending_path(store: NutritionStore) -> Path:
    return store.dir / "pending.json"


def set_pending(store: NutritionStore, item: dict) -> None:
    store.dir.mkdir(parents=True, exist_ok=True)
    # A FRESH OFFER IS NEVER A BLOCKED ONE. Every re-offer path rebuilds the record with
    # `{**pend, "batch": fresh}`, which would carry a stale gate block onto an offer he can
    # now see - and a blocked offer refuses to commit. Dropped here, in the one place every
    # offer goes through, rather than remembered in each caller; the gate re-marks the
    # record itself if it blocks the new text too.
    item = {k: v for k, v in (item or {}).items() if k != "_gate_blocked"}
    pending_path(store).write_text(json.dumps(item, indent=2))


def get_pending(store: NutritionStore):
    p = pending_path(store)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def clear_pending(store: NutritionStore) -> None:
    p = pending_path(store)
    if p.exists():
        p.unlink()


# --- the pre-send gate ------------------------------------------------------
#
# ONE CHOKE POINT for every reply that carries model- or ladder-derived content. Jamie,
# 14 Aug 2026: "everything should be verified by opus to make sure the output is sensible
# against the input and makes sense as a reply and isn't just crap."
#
# A wrapper rather than a call at each of the fifteen sites, for the reason this file has
# learned the hard way in every other hand-off: a rule applied by fifteen callers is a rule
# three of them will drop. The sites that stay OUTSIDE it are listed at EXEMPT_SENDS, with
# why, because an exemption nobody can see is the same failure.
#
# ONE GATE CALL PER INBOUND MESSAGE is the design constraint. The gate reads the FINAL
# composed message, never a fragment, and the mechanical one-liners that share a turn with
# a real reply ("Looking at that...", the trivial-dose note) are exempt so a single message
# cannot cost two Opus calls.

GATE_MODEL = "claude-opus-5"
# The test seam, and the only way this module is driven offline. None means the real CLI.
GATE_RUNNER = None
# Sends that never go through the gate. Kept as text so the reasoning is readable, and
# asserted by the tests so it cannot rot into a list of what somebody forgot to wire:
#   help/start text, "Dropped it.", "Looking at that...", "Looking at tomorrow...",
#   "Barcode NNN, looking it up...", the photo-download failure, the "nothing logged today
#   to X" refusals, the "I could not reach the model" notices, the credential warning, the
#   which-one-do-you-mean list, the deterministic today/target/plants/week/undo/edit/close
#   blocks, the trivial-dose note, the gate's own fallbacks and the blocked-offer refusal.
# Each is either a FIXED STRING or a straight read of the store, so there is no model or
# ladder figure in it to be insane, and two of them exist precisely to report that a model
# call failed - gating those would let a broken verifier silence the outage notice.
EXEMPT_SENDS = "see the comment above; asserted in test_nutrition_bot.py"


def set_inbound(ctx, text: str) -> None:
    """Record what the athlete just sent, for the gate to judge the reply against.

    Stashed on the per-message context rather than threaded through fifteen signatures.
    It MUST be set on every inbound path, including the inline buttons: Context lives for
    the whole process, so an unset field is not empty, it is the PREVIOUS message - and the
    gate would then judge a commit confirmation against something he typed an hour ago."""
    setattr(ctx, "_inbound", (text or "").strip())
    # A NEW MESSAGE STARTS WITH AN EMPTY LEDGER, for the same reason: Context outlives a
    # message, so actions left over from the last turn would make a false claim in this one
    # look substantiated. Reset here rather than in each handler, because this is the one
    # function every inbound path already has to call.
    setattr(ctx, "_actions", [])


def record_action(ctx, text: str) -> None:
    """Record something the code ACTUALLY did to his log this turn.

    THE LEDGER THE GATE CHECKS CLAIMS AGAINST (15:25, 14 Aug 2026). "You've added the pizza
    twice" fell into a re-resolution and the outgoing reply said the duplicate had been
    "noted and removed" while nothing had been removed. The gate passed it because every
    figure in it was plausible, which is all it could see. It is now shown this list, and a
    reply claiming an action absent from it is blocked.

    Written at the point of the store call, never at the point of the sentence: an entry
    appended here by the code that composes the reply would substantiate itself."""
    if not text:
        return
    actions = getattr(ctx, "_actions", None)
    if actions is None:
        actions = []
        setattr(ctx, "_actions", actions)
    actions.append(str(text)[:120])
    log(f"  [action] {str(text)[:100]}")


def _gate_numbers(batch: list) -> list:
    """The figures behind an offer, compact enough to sit in every gate prompt.

    NLU.batch_summaries alone is not enough here: a costed meal's absurdity lives in its
    component ROWS - 447 kcal for a stir fry is only visibly wrong next to "100 g steak,
    raw" - and a stated meal's authority lives in the rows he typed."""
    out = []
    for i, it in enumerate((batch or [])[:8]):
        row = {"index": i,
               "name": (it.get("resolved_name") or it.get("_raw") or "")[:70],
               "kcal": it.get("kcal"), "protein_g": it.get("protein_g"),
               "carb_g": it.get("carb_g"), "fat_g": it.get("fat_g"),
               "portion_used_g": it.get("portion_used_g"),
               "confidence": it.get("confidence")}
        if it.get("_stated"):
            row["figures_are_his_own"] = True
            row["his_rows"] = [str(c)[:80] for c in (it.get("_components") or [])[:12]]
        if it.get("_composed"):
            row["costed_as_a_whole_meal"] = True
            row["components"] = [
                {"name": str(c.get("name"))[:50], "portion_g": c.get("portion_g"),
                 "kcal": c.get("kcal")}
                for c in (it.get("_components_detail") or [])[:12]]
        out.append(row)
    return out


def gate_context(ctx, kind: str, numbers: list = None) -> dict:
    """What the gate is shown besides the message and the reply.

    Deliberately NOT facts_for_question: that dict is the day's whole model and none of it
    helps decide whether a sentence answers a question. Recent turns, the offer on the
    table, and the figures behind this reply."""
    turns = []
    store = getattr(ctx, "store", None)
    try:
        for t in (store.recent_chat() or [])[-6:]:
            turns.append({"at": t.get("at"), "role": t.get("role"),
                          "text": str(t.get("text") or "")[:200]})
    except Exception:
        turns = []
    if numbers is None:
        try:
            numbers = _gate_numbers((get_pending(store) or {}).get("batch") or [])
        except Exception:
            numbers = []
    return {"reply_kind": kind, "recent_conversation": turns,
            "figures_behind_this_reply": numbers or [],
            # WHAT THE CODE DID, so the gate can catch a reply that claims something else.
            # An empty list is meaningful and is sent as one: it says the log was not
            # touched, which is what makes "duplicate removed" checkable.
            "actions_this_turn": list(getattr(ctx, "_actions", []) or [])}


def send_verified(ctx, token, chat_id, text: str, kind: str = "reply",
                  numbers: list = None, reply_markup=None) -> bool:
    """Send `text`, but only if Opus agrees it is a sensible reply to what he just said.

    Returns True when the original text went out (verified or unverified), False when it
    was blocked and a fallback was sent instead. The return value is about WHAT was sent -
    every caller still counts the message as handled, or a block would fall through to a
    second reply and a second gate call.

    On a block the athlete always gets something honest: the gate's own fallback if it gave
    a usable one, otherwise the built line for the reason class. Never the crap, and never
    silence."""
    body = (text or "").strip()
    inbound = getattr(ctx, "_inbound", "") or ""
    if not body or not inbound:
        # Nothing to judge, or nothing to judge it against - a cron or startup send has no
        # inbound message and coherence is undefined for it.
        if body:
            log("[gate] skipped: no inbound message to check against")
        tg.send(token, chat_id, text, reply_markup=reply_markup, log=log)
        return True
    got = NG.verify_reply(inbound, body, gate_context(ctx, kind, numbers), CLAUDE_BIN,
                          model=GATE_MODEL, log=log, runner=GATE_RUNNER,
                          model_unavailable=NLU.model_unavailable)
    reason = (got.get("reason") or "")[:160]
    log(f"[gate] {got['verdict']} {got.get('ms')}ms kind={kind} "
        f"class={got.get('reason_class')} reason={reason!r}")
    if got.get("unverified"):
        log("[gate] unavailable - sent unverified")
    if got["verdict"] != "block":
        tg.send(token, chat_id, text, reply_markup=reply_markup, log=log)
        return True
    log(f"[gate] blocked: {reason}")
    fallback = got.get("fallback") or NG.built_fallback(got.get("reason_class"))
    if kind == "offer":
        # THE OFFER SURVIVES, BUT IT IS NOT CONFIRMABLE. Keeping the pending record intact
        # is what lets "it was 980 kcal" land on the thing he was arguing with. Leaving it
        # CONFIRMABLE would mean a "yes" to the honest line above commits the very figures
        # the gate just called absurd - one tap from the defect it exists to catch. So the
        # record is marked and commit_pending refuses it; any re-offer clears the mark,
        # because set_pending drops the key.
        _mark_pending_gate_blocked(ctx, reason or got.get("reason_class") or "blocked")
        reply_markup = None      # no "Log it" button on something he has not seen
    _chat(ctx, "coach", f"[gate] blocked a reply: {reason[:80]}")
    tg.send(token, chat_id, fallback, reply_markup=reply_markup, log=log)
    return False


def _mark_pending_gate_blocked(ctx, reason: str) -> None:
    """Flag the pending offer as one he was never shown."""
    store = getattr(ctx, "store", None)
    if store is None:
        return
    try:
        pend = get_pending(store)
        if not pend:
            return
        pending_path(store).write_text(json.dumps({**pend, "_gate_blocked": reason},
                                                  indent=2))
    except Exception as exc:
        log(f"[gate] could not mark the pending offer: {exc}")


# --- the runtime context ----------------------------------------------------

class Context:
    """Everything a reply needs, assembled once per message.

    Zones are computed fresh and SNAPSHOTTED onto the day by the store, so a day
    reviewed later shows the zones that were in force rather than what today's data
    would produce."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.slug = cfg.get("athlete", "jamie")
        self.athlete = load_athlete(self.slug)
        self.athlete_dir = BASE / "athletes" / self.slug
        self.store = NutritionStore(self.athlete_dir)
        self.table = PL.SpeciesTable()
        self.cofid = NR.CofidTable()
        self.fetchers = build_fetchers(cfg)
        self.icu = IcuClient(self.athlete["icu_athlete_id"], self.athlete["icu_api_key"])

    def local_today(self) -> date:
        """The athlete's LOCAL date from the ICU profile, never the server's UTC date.
        Europe/London is UTC+1 in summer, so a UTC-dated write after 23:00 local lands
        on the wrong day and corrupts two days at once."""
        try:
            prof = self.icu.get_athlete_profile() or {}
            iso = (prof.get("current_date_local") or "")[:10]
            if iso:
                return date.fromisoformat(iso)
        except Exception as exc:
            log(f"icu profile failed, falling back to server date: {exc}")
        return date.today()

    def prescribed_g_hr(self, sport: str) -> float:
        """READ the prescribed in-session rate from the existing fuelling primitives.

        Runs use run_fuel_target (ceilinged near 60 g/hr); rides use fuel_target, ramping
        toward the race figure. Never restated here, or the bot and the coach would
        disagree about the same session."""
        sys.path.insert(0, str(BASE / "ironman-analysis"))
        from primitives.nutrition import (fuel_target, last_ride_g_hr, last_run_g_hr,
                                          recent_avg_g_hr, recent_run_avg_g_hr,
                                          run_fuel_target)
        slog_path = self.athlete_dir / "session-log.json"
        slog = []
        if slog_path.exists():
            try:
                slog = json.loads(slog_path.read_text())
                if isinstance(slog, dict):
                    slog = slog.get("sessions") or slog.get("entries") or []
            except json.JSONDecodeError:
                slog = []
        if sport in ("Run", "VirtualRun", "TrailRun"):
            avg = recent_run_avg_g_hr(slog)
            avg = avg[0] if isinstance(avg, tuple) else avg
            return float(run_fuel_target(avg, last_g_hr=last_run_g_hr(slog)))
        avg = recent_avg_g_hr(slog)
        avg = avg[0] if isinstance(avg, tuple) else avg
        return float(fuel_target(avg, self.athlete.get("nutrition_target_g_hr") or 90,
                                 last_g_hr=last_ride_g_hr(slog)))

    def weight_readings(self, day: date, days: int = 14) -> list:
        """Store readings plus intervals.icu, with the sweat weigh-ins filtered out.

        ICU is the right source: the scale syncs there automatically and it is where he
        weighs anyway. But it holds ONE UNTIMESTAMPED weight per day and mixes morning
        weights with sweat-rate weigh-ins, so it is classified first. A bot-logged
        reading for a date always wins, because its provenance is known rather than
        inferred.

        Only `morning` readings come back. Letting a post-session reading into the
        rolling mean would read as progress that did not happen, and the deficit is
        driven off that mean."""
        own = self.store.measurements_range(day - timedelta(days=days - 1), day)
        by_date = {(m.get("date") or "")[:10]: m for m in own
                   if m.get("tag") == "morning"}
        try:
            rows = self.icu.get_wellness(days=days + 7) or []
        except Exception as exc:
            log(f"icu weights unavailable: {exc}")
            return own
        merged = list(own)
        for r in NE.classify_icu_weights(rows, existing_by_date=by_date):
            if r["tag"] != "morning":
                continue
            merged.append({"type": "weight", "date": r["date"], "value": r["value"],
                           # 06:00 so it passes the after-04:00 gate and counts as the
                           # day's first reading. ICU gives no time; this is a stand-in,
                           # not a claim about when he stood on the scale.
                           "logged_at": r["date"] + "T06:00", "tag": "morning",
                           "source": "intervals.icu"})
        return merged

    def _tag_session_types(self, sessions) -> None:
        """Annotate sessions in place with the coach's session_type, if it is reachable.

        Best-effort by design: a missing primitive must not cost the athlete his targets,
        and the engine's own token match covers the same ground less precisely. Logged
        rather than swallowed, because silently falling back would mean quality sessions
        were being fuelled as easy ones with nothing to say so."""
        if not sessions:
            return
        try:
            sys.path.insert(0, str(BASE / "ironman-analysis"))
            from primitives.modulation import classify_session_type
        except Exception as exc:
            log(f"session classifier unavailable, using the engine's own tokens: {exc}")
            return
        for s in sessions:
            if s.get("session_type"):
                continue
            s["session_type"] = classify_session_type(
                s.get("type") or s.get("category") or "",
                s.get("name") or "", s.get("description") or "")

    def zones_for(self, day: date) -> dict:
        rules = self.athlete.get("day_rules") or {}
        (today_type, tomorrow_type, conf, sessions,
         tomorrow_sessions) = classify_today_and_tomorrow(self.icu, day, rules)
        self._tomorrow_sessions = tomorrow_sessions
        self._tomorrow_type = tomorrow_type
        # TODAY'S sessions too, cached alongside tomorrow's for the same reason: they are
        # computed here (completed activities preferred, falling back to planned) and were
        # otherwise dropped, leaving today_brief() with no calendar to read from.
        self._today_sessions = sessions
        self._today_type = today_type
        yesterday_type = None
        try:
            prev = [a for a in (self.icu.get_training_history(days=3) or [])
                    if (a.get("start_date_local") or "")[:10]
                    == (day - timedelta(days=1)).isoformat()]
            if prev:
                yesterday_type = NE.classify_day(prev)
        except Exception:
            pass

        weight = NE.rolling_weight_kg(self.weight_readings(day), on=day)
        if weight is None:
            weight = float(self.athlete.get("weight_kg")
                           or json.loads((BASE / "athletes" / self.slug
                                          / "profile.json").read_text())
                           .get("weight_kg", 83.0))
        prof_path = BASE / "athletes" / self.slug / "profile.json"
        prof = json.loads(prof_path.read_text()) if prof_path.exists() else {}
        rmr = NE.mifflin_st_jeor(weight, float(prof.get("height_m") or 1.86),
                                 prof.get("dob") or "1995-05-06", "M", on=day)

        guard = {"active": False}
        try:
            guard = NE.rhr_guard(self.icu.get_wellness(days=35), on=day)
        except Exception as exc:
            log(f"rhr guard skipped: {exc}")

        race = self.athlete.get("race_date")
        days_to_race = ((date.fromisoformat(race) - day).days if race else None)

        # Tag every session with the COACH's own session_type before the engine sees it.
        # The demand tiers need to know which sessions are quality, and the classifier for
        # that already exists and is the one the plan validator and the modulation rules
        # use. Restating its keyword list inside nutrition_engine would give the same
        # session two classifications, which is how a threshold ride ends up fuelled as an
        # easy one. The engine's own token match stays as the fallback for callers that
        # cannot reach the primitives.
        self._tag_session_types(sessions)
        self._tag_session_types(tomorrow_sessions)

        z = NE.zones(day_type=today_type, rolling_weight=weight, rmr=rmr,
                     sessions=sessions, tomorrow_type=tomorrow_type,
                     tomorrow_sessions=tomorrow_sessions,
                     # An empty calendar and an unreadable one are different: the engine
                     # assumes an easy window when it cannot tell, and says so.
                     calendar_known=bool(sessions or tomorrow_sessions),
                     yesterday_type=yesterday_type, days_to_race=days_to_race,
                     deficit_enabled=bool(prof.get("deficit_enabled", True)),
                     rhr_guard_active=bool(guard.get("active")),
                     day_confidence=conf)
        # What TODAY's sessions will need on the move, recorded with the zone snapshot
        # because only the bot can see the calendar - the publish step reads the snapshot.
        # Without it the food budget silently contains the run's carbohydrate: he would be
        # told to eat it at lunch, then told to stop eating after the run, when recovery is
        # exactly when he should not.
        planned = 0.0
        for ev in sessions or []:
            sport_raw = (ev.get("type") or ev.get("category") or "").lower()
            sport = ("Run" if "run" in sport_raw
                     else "Ride" if ("ride" in sport_raw or "bike" in sport_raw) else None)
            mins = ((ev.get("moving_time") or ev.get("icu_training_load_time")
                     or ev.get("duration") or ev.get("time") or 0) or 0) / 60.0
            if not (sport and mins):
                continue
            try:
                planned += (self.prescribed_g_hr(sport) or 0) * mins / 60.0
            except Exception as exc:
                log(f"planned fuel unavailable for {sport}: {exc}")
        if planned:
            z["planned_in_session_carb_g"] = round(planned, 1)
        self.store.set_targets(day, z, day_type=today_type)
        return z


# --- message handling -------------------------------------------------------

def today_brief(ctx: Context, day: date) -> dict:
    """What is actually happening TODAY, so the chat model can see the calendar it is
    talking about rather than only tomorrow's.

    A 240-minute ride sitting on today's calendar with a 60-minute ride on tomorrow's is
    exactly the shape that broke this: facts_for_question injected tomorrow_brief and
    nothing about today, so the model told him about tomorrow's ride and reported no run
    "today or tomorrow" while the 240-minute session sat there unread. Completed activities
    are preferred and the rest of the day falls back to planned - the same sessions and the
    same _done tag classify_today_and_tomorrow already computed, read via the cache
    zones_for populates as a side effect rather than fetched again."""
    zones = ctx.zones_for(day)              # populates the cached sessions as a side effect
    sessions = getattr(ctx, "_today_sessions", None) or []
    out = {"date": day.isoformat(), "sessions": []}
    total_min = 0.0
    for ev in sessions:
        mins = ((ev.get("moving_time") or ev.get("icu_training_load_time")
                 or ev.get("duration") or 0) or 0) / 60.0
        if not mins and ev.get("time"):
            mins = float(ev["time"]) / 60.0
        total_min += mins
        out["sessions"].append({
            "sport": ev.get("type") or ev.get("category") or "unknown",
            "name": (ev.get("name") or "")[:80],
            "minutes": round(mins) or None,
            "planned_load": ev.get("icu_training_load") or ev.get("load_target"),
            "aim": (ev.get("description") or "")[:400] or None,
            "done": bool(ev.get("_done")),
        })
    out["total_minutes"] = round(total_min) or None
    return out


def tomorrow_brief(ctx: Context, day: date) -> dict:
    """What tomorrow actually is, and what it needs. Numbers only, no phrasing.

    Every figure here is READ from something that already computes it - the ICU calendar,
    the shared fuelling primitives, the zone engine - because the moment this file starts
    restating a prescription, the bot and the coach begin disagreeing about the same
    session."""
    zones = ctx.zones_for(day)              # populates the cached sessions as a side effect
    sessions = getattr(ctx, "_tomorrow_sessions", None) or []
    out = {"date": (day + timedelta(days=1)).isoformat(),
           "day_type": getattr(ctx, "_tomorrow_type", None),
           "from_calendar": bool(sessions), "sessions": []}
    total_min = 0.0
    for ev in sessions:
        mins = ((ev.get("moving_time") or ev.get("icu_training_load_time")
                 or ev.get("duration") or 0) or 0) / 60.0
        if not mins and ev.get("time"):
            mins = float(ev["time"]) / 60.0
        total_min += mins
        out["sessions"].append({
            "sport": ev.get("type") or ev.get("category") or "unknown",
            "name": (ev.get("name") or "")[:80],
            "minutes": round(mins) or None,
            "planned_load": ev.get("icu_training_load") or ev.get("load_target"),
            # The aim line the coach writes, which is the actual instruction for the
            # session - far more use than its duration alone.
            "aim": (ev.get("description") or "")[:400] or None,
        })
    out["total_minutes"] = round(total_min) or None
    # In-session fuel, PER SESSION, from the same primitives the coach uses.
    #
    # The first cut multiplied the run rate by the day's TOTAL minutes, so a 60 min swim
    # plus a 165 min run prescribed 263 g of carbohydrate for "the session" instead of
    # 192 g for the run. Nothing raised, the figure looked reasonable, and it went
    # straight into the coaching brief - a swim is not fuelled at run rates and the two
    # sessions are not one session.
    for sn in out["sessions"]:
        sport_raw = (sn.get("sport") or "").lower()
        sport = ("Run" if "run" in sport_raw
                 else "Ride" if ("ride" in sport_raw or "bike" in sport_raw) else None)
        if not (sport and sn.get("minutes")):
            # Swims and anything else that is not fuelled on the move say so, rather than
            # silently contributing zero to a total that looks complete.
            sn["in_session"] = None
            continue
        try:
            rate = ctx.prescribed_g_hr(sport)
        except Exception as exc:
            log(f"prescription unavailable for {sport}: {exc}")
            continue
        sn["in_session"] = {
            "sport": sport, "prescribed_carb_g_per_hr": rate,
            "minutes": sn["minutes"],
            "carb_g": round(rate * sn["minutes"] / 60.0) if rate else None}
    fuelled = [sn["in_session"] for sn in out["sessions"] if sn.get("in_session")]
    if fuelled:
        out["in_session_by_session"] = fuelled
        out["in_session_carb_g_total"] = sum(f["carb_g"] or 0 for f in fuelled)
        out["in_session_note"] = ("per session, not per day: only sessions fuelled on the "
                                 "move appear here")
    # What TODAY's zones are already doing about tomorrow, so advice does not contradict
    # the targets the athlete is looking at.
    out["todays_zones_because_of_tomorrow"] = [m for m in (zones.get("modifiers") or [])
                                               if "tomorrow" in m]
    return out


def macro_lean(entry: dict) -> str | None:
    """Which macro this food mostly IS, from its own energy split. None when there is
    not enough of it to say.

    Computed here rather than described by a model, because it is what makes a named
    suggestion answer a named gap: "jacket potato + tuna" is only the right answer to an
    open carbohydrate gap if something knows the potato is carbohydrate. Shares are of
    the macros' OWN energy, never of the entry's stated kcal - a label's kcal figure
    routinely disagrees with its macros by a few per cent, and dividing by it puts the
    dominance threshold at the mercy of that rounding."""
    p = (entry.get("protein_g") or 0) * 4
    c = (entry.get("carb_g") or 0) * 4
    f = (entry.get("fat_g") or 0) * 9
    total = p + c + f
    if total < 20:                          # a condiment or a splash of milk: no character
        return None
    share = {"protein-heavy": p / total, "carb-heavy": c / total, "fat-heavy": f / total}
    top, frac = max(share.items(), key=lambda kv: kv[1])
    return top if frac >= 0.5 else "mixed"


def eating_levers(ctx: Context, day: date, back_days: int = 21) -> list:
    """What he ACTUALLY eats, with real figures, most-used first.

    Suggestions have to come from his own shopping. Advice to "add some lean protein" is
    worthless; "swap the Twix for the M&S protein bar, 170 kcal less and 15 g more
    protein" is actionable, and only possible because both are in his own log with label
    figures behind them.

    Each item now also carries what it IS (`lean`), when he usually eats it (`usual_meal`)
    and whether it is training fuel (`in_session_fuel`). Without those the list is 25
    names and a wall of macros: enough for the model to check a suggestion afterwards, not
    enough to REACH one, so it kept inventing a meal and then quoting his food at it.
    In-session fuel stays in the list but is tagged, because his gels and drink mix are
    the right answer to "how do I get carbohydrate in before tomorrow" and the wrong
    answer to "what shall I have for dinner", and only a tag can tell those apart."""
    seen = {}
    for rec in ctx.store.get_range(day - timedelta(days=back_days), day):
        on = rec.get("date")
        for e in rec.get("entries") or []:
            name = e.get("resolved_name") or ""
            if not name or e.get("_supplement"):
                continue
            row = seen.setdefault(name, {"name": name, "times": 0,
                                         "kcal": e.get("kcal"),
                                         "protein_g": e.get("protein_g"),
                                         "carb_g": e.get("carb_g"),
                                         "fat_g": e.get("fat_g"),
                                         "fibre_g": e.get("fibre_g"),
                                         "confidence": e.get("confidence"),
                                         "lean": macro_lean(e),
                                         "in_session_fuel": bool(e.get("in_session")),
                                         "_meals": {}, "last_eaten": None})
            row["times"] += 1
            if on and (row["last_eaten"] or "") < on:
                row["last_eaten"] = on
            meal = e.get("meal") or ""
            if meal:
                row["_meals"][meal] = row["_meals"].get(meal, 0) + 1
    out = []
    for row in seen.values():
        meals = row.pop("_meals")
        # Ties broken by NAME, not by whichever meal the dict happened to see first:
        # this whole list is injected into a prompt and has to be the same list twice.
        row["usual_meal"] = (max(sorted(meals), key=lambda m: meals[m]) if meals else None)
        out.append(row)
    return sorted(out, key=lambda r: (-r["times"], r["name"]))[:25]


def macro_fact(z: dict, consumed, key: str) -> dict:
    """One macro's zone, its gap, and WHY the zone is where it is.

    Two things were missing and both cost the same thing - the ability to say why. The
    GAP was not here at all, so a reply could only name a gap by subtracting low from
    consumed, which the prompt forbids outright: the model is not allowed to do
    arithmetic, so with no gap in the facts it either broke that rule or gave a why with
    no size to it. And `basis` - the engine's own sentence explaining the bound, "demand
    band 8-10 g/kg (long session tomorrow)" - was computed, published and then dropped
    here, so the numbers arrived with their reason stripped off.

    Signs are fixed and stated rather than left to the reader: `gap_to_low_g` is how much
    MORE is wanted and never negative, `room_to_high_g` is what is left before the top and
    goes negative once he is past it."""
    zone = z.get(key) or {}
    out = {"consumed": consumed, "low": zone.get("low"), "high": zone.get("high"),
           "bias": zone.get("bias"), "basis": zone.get("basis")}
    if consumed is not None and zone.get("low") is not None:
        out["gap_to_low_g"] = round(max(0.0, zone["low"] - consumed), 1)
    # Each bound guarded on ITS OWN presence. Defaulting a missing high to zero would
    # publish room_to_high_g as minus everything he has eaten, which reads as a breach of
    # a zone that does not exist. The prompt is told that this figure only means anything
    # on a ceiling or a band: on a floor, being past the top is right.
    if consumed is not None and zone.get("high") is not None:
        out["room_to_high_g"] = round(zone["high"] - consumed, 1)
    # bound / kcal_share exist on carbohydrate and fat only, and the fibre PHASE only on a
    # day whose own session is still to come. Copied when present rather than defaulted:
    # a null `after_session` reads as "no phase" and is indistinguishable from a real one.
    for extra in ("bound", "kcal_share", "after_session", "phase_note"):
        if zone.get(extra) is not None:
            out[extra] = zone[extra]
    return out


def facts_for_question(ctx: Context, day: date) -> dict:
    """Everything a question could reasonably need, so the model phrases rather than
    computes. Nothing here is generated by a model: totals come from the store, zones
    from the engine, plants from the species table."""
    z = ctx.zones_for(day)
    totals = RC.merged_totals(ctx.store, ctx.athlete_dir, day)
    days = ctx.store.get_range(day - timedelta(days=6), day)
    div = PL.diversity(days, ctx.table, on=day)
    mean = NE.rolling_weight_kg(
        ctx.store.measurements_range(day - timedelta(days=6), day), on=day)
    entries = ctx.store.get_day(day).get("entries") or []
    return {
        "today": day.isoformat(),
        "day_type": z["day_type"],
        "day_type_confidence": z.get("confidence"),
        "energy": {"consumed_kcal": totals["kcal"], "target_kcal": z["kcal_target"],
                   "remaining_kcal": round(z["kcal_target"] - (totals["kcal"] or 0)),
                   "maintenance_kcal": z["kcal_maintenance"],
                   "deficit_applied_kcal": z["deficit_applied_kcal"],
                   "estimate_quality": z["kcal_confidence"]},
        "macros": {k: macro_fact(z, totals.get(k), k)
                   for k in ("protein_g", "carb_g", "fat_g", "fibre_g")},
        # WHAT THE FOOD IS FOR. The engine fuels for the work required, so the zones are
        # a consequence of the window ahead and unreadable without it: 8-10 g/kg of
        # carbohydrate is not a diet, it is Thursday's long ride arriving. This is the
        # difference between "you have 120 g of carbohydrate left" and "tonight is a
        # carb-forward night because the long ride is tomorrow" - same number, and only
        # the second one tells him what to cook. Guarded because a snapshot written
        # before the demand model existed has none of these keys.
        "demand_ahead": z.get("demand_ahead"),
        "carb_basis": z.get("carb_basis") or (z.get("carb_g") or {}).get("basis"),
        "fat_basis": z.get("fat_basis") or (z.get("fat_g") or {}).get("basis"),
        "collagen_protein_not_counted_g": totals.get("non_counting_protein_g"),
        "dietary_sodium_mg": totals.get("dietary_sodium_mg"),
        "sodium_note": "no sweat test done, so there is no personal sodium target",
        "plants_7d": div["unique_7d"], "plants_new_today": div["new_species_today"],
        "plants_target": div["target"],
        "weight_7d_mean_kg": mean, "weight_basis_kg": z["weight_basis_kg"],
        "items_logged_today": [{"name": e.get("resolved_name"), "kcal": e.get("kcal"),
                                "confidence": e.get("confidence")} for e in entries],
        "modifiers_today": z.get("modifiers"), "warnings_today": z.get("warnings"),
        # Tomorrow, and what he actually eats. Without these the model can only read
        # today's numbers back out - which is exactly how this bot came to feel like a
        # form rather than a coach.
        # Where he is, when the profile says. Needed for "what should I order" to name
        # places that exist rather than a chain the model assumes is nearby; absent rather
        # than guessed when unknown, and anything he says in chat overrides it.
        "location": (json.loads((BASE / "athletes" / ctx.slug / "profile.json").read_text())
                     .get("city") if (BASE / "athletes" / ctx.slug / "profile.json").exists()
                     else None),
        "today_sessions": today_brief(ctx, day),
        "tomorrow": tomorrow_brief(ctx, day),
        "foods_he_actually_eats": eating_levers(ctx, day),
    }


def handle_text(ctx: Context, text: str, token: str, chat_id) -> None:
    """Route on INTENT, before anything is resolved.

    The first cut treated every non-command message as food, so "how much protein have
    I had?" went to the resolution ladder, came back as a food item and was offered for
    logging. Intent is now decided first."""
    day = ctx.local_today()
    t = (text or "").strip()
    set_inbound(ctx, t)
    pend = get_pending(ctx.store)
    log(f"msg {t[:70]!r} pending={bool(pend)}")

    if t.lower().rstrip("!. ") in ("help", "start"):
        tg.send(token, chat_id, HELP, log=log)
        return

    got = NLU.classify(t, bool(pend), CLAUDE_BIN, LLM_MODEL, log=log)
    intent = got.get("intent")
    log(f"  intent={intent} items={len(got.get('items') or [])}")

    if intent == "secret":
        # The reply deliberately does NOT quote it back: echoing a credential into the
        # chat log is the same exposure again.
        tg.send(token, chat_id,
                "That looks like an API key or token, so I have not logged it or sent "
                "it anywhere. Keys go in `nutrition_config.json` on the VM, never "
                "through a bot. Assume anything pasted into a chat is burnt and get a "
                "fresh one.", log=log)
        return

    if intent == "command":
        handle_command(ctx, got.get("command", "/help"), day, token, chat_id)
        return

    if intent == "confirm" and pend:
        # The chat model reads recent_chat() and had no idea a food argument had just
        # happened - the whole exchange lived in the pending record, invisible to it.
        _chat(ctx, "athlete", t)
        commit_pending(ctx, pend, day, token, chat_id)
        return

    if intent == "cancel":
        clear_pending(ctx.store)
        tg.send(token, chat_id, "Dropped it.", log=log)
        return

    if intent == "delete_entry":
        named = (got.get("item") or "").strip()
        entry = ctx.store.find_entry(day, named)
        # A NAMED delete must actually match that name. find_entry falls back to the most
        # recent entry, which is right for "delete that" and catastrophic for "delete my
        # account details" - it would silently bin whatever he logged last. So a name that
        # matches nothing asks instead of guessing.
        if named and entry and not _name_matches(named, entry.get("resolved_name") or ""):
            names = [e.get("resolved_name") or "" for e
                     in (ctx.store.get_day(day).get("entries") or [])][-6:]
            tg.send(token, chat_id,
                    f"I cannot see {named!r} in today’s log. Today has: "
                    + ", ".join(n[:34] for n in names)
                    + ". Name one of those, or say “delete that” for the most recent.", log=log)
            return
        if not entry:
            tg.send(token, chat_id, "Nothing logged today to delete.", log=log)
            return
        ctx.store.remove_entry(day, entry["id"])
        record_action(ctx, f"removed entry {entry['id']} {entry.get('resolved_name')} "
                           f"({round(entry.get('kcal') or 0)} kcal)")
        # In-session totals change if that entry was fuel, and session-log has to follow or
        # the coach's ramp keeps counting food that no longer exists.
        fuel = RC.bot_in_session_totals(ctx.store, day)
        RC.write_back(ctx.athlete_dir, day, carb_g=fuel["carb_g"],
                      sodium_mg=fuel["sodium_mg"] or None, log=log, allow_clear=True)
        publish_now(ctx)
        # The delete itself never reached the chat store, so a follow-up question about
        # today's log had no idea an entry had gone.
        _chat(ctx, "athlete", t)
        _chat(ctx, 
            "coach", f"[log] deleted: {(entry.get('resolved_name') or named)[:60]}")
        send_verified(ctx, token, chat_id,
                      f"Deleted *{entry.get('resolved_name')}* "
                      f"({round(entry.get('kcal') or 0)} kcal).\n\n"
                      + today_block(ctx, day), kind="confirmation",
                      numbers=_gate_numbers([entry]))
        return

    if intent == "set_meal":
        entry = ctx.store.find_entry(day, got.get("item") or "")
        if not entry:
            tg.send(token, chat_id, "Nothing logged today to put in a meal yet.", log=log)
            return
        done = ctx.store.set_meal(day, entry["id"], got["meal"])
        if not done:
            tg.send(token, chat_id,
                    f"I can file things under breakfast, lunch, dinner or snacks - "
                    f"{got['meal']} is not one I know.", log=log)
            return
        record_action(ctx, f"updated entry {done['id']}: filed under {got['meal']}")
        publish_now(ctx)
        send_verified(ctx, token, chat_id,
                      f"Filed *{done.get('resolved_name')}* under {got['meal']}.",
                      kind="correction", numbers=_gate_numbers([done]))
        return

    if intent == "set_in_session":
        entry = ctx.store.find_entry(day, "")
        if not entry:
            tg.send(token, chat_id, "Nothing logged today to move yet.", log=log)
            return
        done = ctx.store.set_in_session(day, entry["id"], got["in_session"])
        record_action(ctx, f"updated entry {entry['id']}: in_session="
                           f"{bool(got['in_session'])}")
        # The coach's ramp reads session-log, so moving fuel in or out has to be pushed
        # there as well or the two disagree about the same session.
        fuel = RC.bot_in_session_totals(ctx.store, day)
        RC.write_back(ctx.athlete_dir, day, carb_g=fuel["carb_g"],
                      sodium_mg=fuel["sodium_mg"] or None, log=log, allow_clear=True)
        publish_now(ctx)
        send_verified(ctx, token, chat_id,
                      f"*{done.get('resolved_name')}* is now "
                      + ("in-session fuel." if got["in_session"]
                         else "out-of-session, so it is off your in-run figures."),
                      kind="correction", numbers=_gate_numbers([done]))
        return

    if intent == "log_weight":
        log_weight(ctx, got["weight_kg"], day, token, chat_id)
        return

    if intent == "correction":
        # Re-parse from the combined text rather than patching the parsed result:
        # patching a misparse tends to preserve whatever else was wrong about it.
        #
        # Chat had no idea an argument like this had just happened - both were logged
        # only in the pending record - which is how the chat model came to be blind to
        # what he had just disputed.
        _chat(ctx, "athlete", t)
        corr = got.get("correction") or t
        # WHAT THE CORRECTION MEANS IS THE MODEL'S CALL; the code only executes it.
        # (Jamie, 13 Aug 2026: "why are we relying so much on python when just using
        # any LLM ... would do better".) A pile of regexes reverse-engineering that
        # judgement is what registered '100g' as an excluded food and re-searched a
        # label it was holding. The model decides rescale/reidentify/meal; every
        # NUMBER is still computed here - the model never returns macros.
        batch = (pend or {}).get("batch") or []
        # WITH AN OFFER ON THE TABLE, A COMMITTED ENTRY IS NEVER THE TARGET. The old rule
        # was `batch[0] if len(batch) == 1 else find_entry(day, "")`, so a correction aimed
        # at a four-component meal awaiting confirmation was decided against the last thing
        # he had LOGGED - on 14 Aug 2026 a brookie from earlier in the day. He said "it was
        # a whole meal" about his stir-fry and got an answer about a biscuit.
        target_item = (batch[0] if len(batch) == 1
                       else None if batch
                       else ctx.store.find_entry(day, "") or None)
        # ASKED EVEN WITH NOTHING LOGGED. The model used to be consulted only when there
        # was an item to correct, which made "a rego scoop is half a portion" on an empty
        # day unanswerable - it fell through to "nothing logged today to correct", and
        # that is one of the four edits Jamie had to route through a human on 13 Aug 2026.
        # A fact about a product is not a fact about an entry.
        decision = NLU.decide_correction(corr, target_item or {}, CLAUDE_BIN, LLM_MODEL,
                                         log=log, batch=batch or None)
        if decision:
            kind = decision.get("kind")
            log(f"  correction decided: {kind} {decision}")
            if kind in ("rescale_all", "rescale_items", "meal_portions"):
                if apply_batch_rescale(ctx, pend, decision, day, token, chat_id):
                    return
            if kind in ("remember", "remember_and_rescale"):
                if apply_remember(ctx, decision, pend, day, token, chat_id):
                    return
            elif kind == "delete_duplicate":
                # ALWAYS handled, offer on the table or not. Falling through is what sent
                # "you've added the pizza twice" into a re-resolution and produced a reply
                # claiming a removal that never happened (15:25, 14 Aug 2026).
                if apply_delete_duplicate(ctx, decision, day, token, chat_id):
                    return
            elif kind == "retime" and not pend:
                # Retiming is only meaningful for something already written: a pending
                # offer has no entry yet, and a time stated on a NEW log arrives on the
                # item instead (see offer_items' `default_at`).
                if apply_retime(ctx, decision, day, token, chat_id):
                    return
            elif kind == "rename" and not pend:
                if apply_rename(ctx, decision, day, token, chat_id):
                    return
            elif kind == "rescale" and decision.get("grams"):
                if apply_quantity_correction(ctx, pend,
                                             {"grams": float(decision["grams"])},
                                             day, token, chat_id):
                    return
            elif kind == "rescale_factor" and decision.get("factor"):
                if apply_quantity_correction(ctx, pend,
                                             {"factor": float(decision["factor"])},
                                             day, token, chat_id):
                    return
            elif kind == "whole_pack":
                if apply_quantity_correction(ctx, pend, {"whole_pack": True},
                                             day, token, chat_id):
                    return
            elif kind == "meal" and decision.get("meal"):
                # BOTH with an offer on the table and without one. "That was breakfast"
                # while something is pending reaches here rather than the set_meal fast
                # path, because fast_intent deliberately keeps out of the way while a
                # yes/no is outstanding - and the branch it landed in required `not pend`,
                # so the meal was silently dropped exactly when he was still confirming
                # the item it belonged to.
                if apply_meal_correction(ctx, decision, pend, target_item, day,
                                         token, chat_id):
                    return
            elif kind == "reidentify":
                for phrase in decision.get("exclusions") or []:
                    ctx.store.add_exclusion(day, phrase)
                # falls through to the re-resolution path below, which now carries
                # the exclusions and the model's cleaned-up lookup text
                if decision.get("text"):
                    corr = decision["text"]
            # "unclear" and anything unhandled: fall through unchanged.
        else:
            # Model unavailable: the deterministic detectors are the FALLBACK, not
            # the decider - offline beats broken, but they never override the model.
            qc = quantity_correction(corr)
            if qc and apply_quantity_correction(ctx, pend, qc, day, token, chat_id):
                return
        # Register what he says it was NOT before anything is re-resolved. The ladder is
        # deterministic, so a re-resolution with no memory of the rejected candidate returns
        # it again - six times, on 12 Aug 2026, twice after he had said so explicitly.
        record_exclusions(ctx, day, corr)
        if not pend:
            # A correction after the fact used to be refused outright - "nothing pending" -
            # which is how a wrong sandwich survived being corrected and then got logged a
            # second time from the label. It now REPLACES the entry he is talking about.
            target = ctx.store.find_entry(day, corr)
            if not target:
                tg.send(token, chat_id, "Nothing logged today to correct. Tell me what you "
                                        "had and I will look it up.", log=log)
                return
            combined = NLU.apply_correction(target.get("raw_text") or "",
                                            corr)
            _chat(ctx, 
                "coach", f"[log] correction noted: {(got.get('correction') or t)[:60]}")
            offer_items(ctx, [{"text": combined, "portion_g": None,
                               "in_session": bool(target.get("in_session"))}],
                        day, token, chat_id, said=corr)
            mark_pending_replaces(ctx, target["id"], target.get("resolved_name") or "")
            return
        # A COSTED MEAL IS RE-TABLED, NEVER RE-SEARCHED. "The noodles were 400g" is a fact
        # about one row of a table the model built from his description, so the whole meal is
        # re-costed with that fact added - which keeps the assumptions and the components
        # consistent with each other. Sending it down the ladder instead would break a
        # coherent dinner into ingredient lookups again, which is the defect this path exists
        # to remove. Pure arithmetic (x1.5, per-component ratios) was already executed above
        # and never reaches here.
        if all(i.get("_composed") for i in (pend.get("batch") or [{}])):
            said = pending_subject(pend)
            if offer_composed(ctx, said, day, token, chat_id,
                              default_at=(pend["batch"][0].get("_at")),
                              default_meal=(pend["batch"][0].get("_meal")),
                              in_session=bool(pend["batch"][0].get("in_session")),
                              extra=corr):
                _chat(ctx, "coach", f"[log] re-tabled the meal: {corr[:60]}")
                return
            tg.send(token, chat_id,
                    "I could not reach the model to redo that table. The offer is "
                    "unchanged - say no if you would rather drop it and tell me again.",
                    log=log)
            return
        # FIGURES HE SUPPLIED ARE NEVER RE-RESOLVED, and this is the last door out of the
        # correction branch: everything below re-searches the pending subject's raw text,
        # which for a stated offer is his own pasted table. A decision that reached here
        # unexecuted - `unclear`, or a reidentify - would have sent that table back down the
        # ladder and re-priced his 980 kcal meal all over again, which is the reported defect
        # recurring one level down. Asking is the only honest move: the code cannot know
        # which of his numbers he means to change.
        if all(i.get("_stated") for i in (pend.get("batch") or [{}])):
            tg.send(token, chat_id,
                    "Those are your own figures, so I will not go looking them up again. "
                    "Tell me the number to change and what to - “make it 1,100 kcal” "
                    "or “all of that x1.5” - or say no and send the whole thing "
                    "again.", log=log)
            _chat(ctx, "coach",
                  "[log] declined to re-resolve figures the athlete supplied")
            return
        if correct_in_batch(ctx, pend, corr, day, token, chat_id):
            return
        subject = pending_subject(pend)
        combined = NLU.apply_correction(subject, corr)
        # A correction with no subject left in it is REFUSED. " (half the portion)" is not a
        # food, and resolving it produced an LLM estimate named after the correction itself -
        # the third time this shape has reached the log.
        if not _has_subject(combined, corr):
            tg.send(token, chat_id,
                    "I have lost track of what that refers to. Tell me the item and the "
                    "change together - \u201chalf a bag of the M&S nut collection\u201d - and "
                    "I will redo it.", log=log)
            return
        _chat(ctx, 
            "coach", f"[log] correction noted: {(got.get('correction') or t)[:60]}")
        offer_items(ctx, [{"text": combined, "portion_g": None,
                           "in_session": bool((pend.get("batch") or [{}])[0]
                                              .get("in_session"))}],
                    day, token, chat_id, said=corr)
        return

    if intent == "advice":
        debate(ctx, got, t, day, token, chat_id)
        return

    if intent == "question":
        answer = converse_reply(ctx, got.get("question") or t, day, token, chat_id)
        # Falling back to the deterministic block rather than nothing: an unavailable
        # model must not mean an unanswered question.
        if not answer:
            tg.send(token, chat_id, today_block(ctx, day), log=log)
        return

    if intent in ("log_food", "log_supplement") and got.get("items"):
        # Food-logging never reached the chat store, so the chat model was blind to a
        # meal he had just argued about with the bot two messages before this one.
        _chat(ctx, "athlete", t)
        # Interpret before resolving: work out what each thing IS and how to search for
        # it, then let the ladder search THAT rather than the athlete's sentence. The
        # model plans the lookup; it still never supplies a macro.
        # A time stated for the MESSAGE carries to anything in it that did not carry its
        # own. interpret() is a second model call and returns None when the model is
        # unavailable, so a stated time that only survived on its items would be lost in
        # exactly the case where the classify items are the ones resolved.
        stated_at = next((i.get("at") for i in (got.get("items") or []) if i.get("at")),
                         None)
        # And the MEAL he named, on the same terms. "For breakfast I had porridge and a
        # coffee" names it once for the whole message, so it carries to every item in it
        # that did not name its own - and it has to survive interpret() being the parse
        # that gets resolved, which is the usual case.
        stated_meal = next((i.get("meal") for i in (got.get("items") or [])
                            if i.get("meal")), None)
        # HIS OWN FIGURES SHORT-CIRCUIT EVERYTHING, and this check has to sit ABOVE the
        # interpret call rather than inside the offer. interpret() is a lookup PLANNER: hand
        # it a message that already contains the answer and it dutifully plans five searches
        # for the five rows of his table, which is how a 980 kcal meal he had already
        # costed came back at 2,400 (14 Aug 2026). There is nothing to plan.
        if any((i.get("stated") or {}).get("kcal") for i in got.get("items") or []):
            offer_stated(ctx, got["items"], day, token, chat_id,
                         default_at=stated_at, default_meal=stated_meal)
            return
        # A MEAL HE COOKED IS COSTED WHOLE, BY THE BEST MODEL, AND THE LADDER NEVER RUNS ON
        # IT. The composition tables hold ingredients, not dinners, so breaking a stir-fry
        # into four rows and looking each one up loses the portion, the cooking and the oil
        # every time - 447 kcal for a 980 kcal meal. Only for food nobody published figures
        # for: branded, barcoded, labelled and single-whole-food items keep the deterministic
        # path, where the ladder genuinely beats an estimate.
        if (got.get("composed_meal") and intent == "log_food"
                and not got.get("barcode")):
            # THE MEAL'S OWN FLAG, not the message's. `any()` here would tag a whole dinner as
            # in-run fuel because a gel earlier in the same message was, and fuel counted in
            # the session rewrites the g/hr history the coach prescribes from - which this
            # file already treats as worse than a wrong day total.
            if offer_composed(ctx, t, day, token, chat_id, default_at=stated_at,
                              default_meal=stated_meal,
                              in_session=bool((got.get("items") or [{}])[0]
                                              .get("in_session"))):
                return
            # Fell through: the model was unreachable. The interpret path below still asks
            # for cooked states and as-eaten portions, which is a poor second to a costed
            # table and far better than refusing to log his dinner.
        plan = NLU.interpret(t, CLAUDE_BIN, LLM_MODEL, log=log)
        if plan and plan.get("items"):
            offer_planned(ctx, plan["items"], day, token, chat_id, said=t,
                          default_at=stated_at, default_meal=stated_meal)
            return
        if got.get("nutritionally_trivial"):
            # Say it plainly rather than logging "kcal 1" and implying it counted.
            tg.send(token, chat_id,
                    f"{got['dose_mg']:.0f} mg is nutritionally negligible, so I will "
                    f"record it as a supplement dose with no macros rather than pretend "
                    f"it moves the day.", log=log)
        if got.get("degraded"):
            tg.send(token, chat_id, "I could not reach the model to split that up, so I "
                                    "am treating it as one item. Correct me if that is "
                                    "wrong.", log=log)
        offer_items(ctx, got["items"], day, token, chat_id,
                    supplement=(intent == "log_supplement"),
                    barcode=got.get("barcode"),
                    trivial=bool(got.get("nutritionally_trivial")),
                    dose_mg=got.get("dose_mg"), said=t, default_at=stated_at,
                    default_meal=stated_meal)
        return

    if intent == "smalltalk":
        # "Tell me what you have eaten and I will log it" was the whole reply here, which
        # is a form talking, not a coach. Anything that is not food, a command or a
        # correction is now just conversation.
        if converse_reply(ctx, t, day, token, chat_id):
            return
        tg.send(token, chat_id, "Tell me what you have eaten and I will log it.", log=log)
        return

    tg.send(token, chat_id,
            "I could not tell whether that was food, a weight or a question. "
            "Try telling me what you ate, or ask me something like "
            "\"how much protein have I had?\"", log=log)


def log_weight(ctx: Context, kg: float, day: date, token, chat_id) -> None:
    m = ctx.store.add_measurement(
        day, type="weight", value=kg,
        logged_at=datetime.now().isoformat(timespec="minutes"), source="telegram")
    record_action(ctx, f"added a weight measurement of {kg:.1f} kg")
    mean = NE.rolling_weight_kg(
        ctx.store.measurements_range(day - timedelta(days=6), day), on=day)
    note = ("" if m["tag"] == "morning" else
            "\n_Second reading today, so I have tagged it as a session weigh-in and "
            "kept it out of the trend._")
    tg.send(token, chat_id,
            f"{kg:.1f} kg logged."
            + (f" 7-day morning mean {mean:.1f} kg." if mean else "") + note, log=log)


def converse_reply(ctx: Context, message: str, day: date, token, chat_id,
                   extra_facts: dict = None) -> str | None:
    """One conversational turn: facts in, an actual answer out, both ends remembered.

    The transcript is recorded on BOTH sides. Storing only his messages would leave
    "why?" pointing at nothing, which is most of what a follow-up is."""
    facts = facts_for_question(ctx, day)
    if extra_facts:
        facts.update(extra_facts)
    history = ctx.store.recent_chat()
    out = NLU.converse(message, facts, history, CLAUDE_BIN, LLM_MODEL, log=log,
                       now_iso=datetime.now().strftime("%Y-%m-%dT%H:%M"))
    _chat(ctx, "athlete", message)
    if out:
        # RECORDED ONLY IF HE GOT IT. The turn used to be stored before the send, which with
        # a gate in front means a reply the gate rejected would sit in the transcript as
        # something the coach said - and the next turn reads that history back and follows a
        # thread he never saw. send_verified logs the block and stores its own marker.
        if send_verified(ctx, token, chat_id, out, kind="reply"):
            _chat(ctx, "coach", out)
    return out


def _chat(ctx: Context, role: str, text: str) -> None:
    """Append a chat turn when there is a store to hold it. Tests drive these paths
    with a bare ctx (store=None); a missing store means the transcript is simply not
    kept, never a crash."""
    store = getattr(ctx, "store", None)
    if store is not None and hasattr(store, "append_chat"):
        store.append_chat(role, text)


def _exclusions(ctx: Context, day: date) -> list:
    """The athlete's rejected-candidate phrases for the day, or [] when there is no
    store to ask. Tests drive offer paths with a bare ctx (store=None), and a missing
    store must mean "no exclusions", not a crash — the exclusion feature is a filter,
    never a prerequisite."""
    store = getattr(ctx, "store", None)
    if store is None or not hasattr(store, "get_exclusions"):
        return []
    return store.get_exclusions(day)


def debate(ctx: Context, got: dict, text: str, day: date, token, chat_id) -> None:
    """Discuss options rather than log them.

    Every option is resolved through the ladder FIRST, so the conversation is about
    real macros and the day's actual remaining room, not the model's impression of a
    food. Nothing is written: he is deciding, not reporting. The options are kept in
    `last_options` so "went with the rice" can be logged straight afterwards without
    him retyping it."""
    facts = facts_for_question(ctx, day)
    z = ctx.zones_for(day)
    totals = RC.merged_totals(ctx.store, ctx.athlete_dir, day)
    options = []
    exclude = _exclusions(ctx, day)
    for opt in (got.get("options") or [])[:5]:
        item = NR.resolve(opt, day=day, store=ctx.store, table=ctx.table,
                          fetchers=ctx.fetchers, cofid=ctx.cofid, exclude=exclude)
        landing = {}
        for key, macro in (("kcal", "kcal"), ("protein_g", "protein_g"),
                           ("carb_g", "carb_g"), ("fat_g", "fat_g"),
                           ("fibre_g", "fibre_g")):
            add = item.get(macro)
            if add is None:
                continue
            after = (totals.get(macro) or 0) + add
            zone = z.get(macro) if macro != "kcal" else z["kcal"]
            landing[macro] = {"after": round(after, 1), "zone_low": zone["low"],
                              "zone_high": zone["high"], "bias": zone["bias"],
                              "breaches_ceiling": (zone["bias"] != NE.BIAS_FLOOR
                                                   and after > zone["high"])}
        options.append({"option": opt, "resolved_name": item.get("resolved_name"),
                        "macros": {k: item.get(k) for k in NR.MACRO_FIELDS},
                        "confidence": item.get("confidence"),
                        "source": item.get("source_rung"),
                        "if_eaten": landing})
    facts["options"] = options
    facts["in_session_items_are_protected"] = True
    # The options are RESOLVED above, so they go in as facts and the
    # discussion is a normal turn of the same conversation - with the
    # thread behind it, rather than a standalone opinion.
    reply = NLU.converse(text, {**facts, "options_on_the_table": options},
                         ctx.store.recent_chat(), CLAUDE_BIN, LLM_MODEL, log=log,
                         now_iso=datetime.now().strftime("%Y-%m-%dT%H:%M"))
    _chat(ctx, "athlete", text)
    ctx.store.cache_put("_last_options", {"options": [o["option"] for o in options],
                                          "day": day.isoformat()})
    if reply:
        # Stored only once it has actually gone out, for the same reason as converse_reply.
        if send_verified(ctx, token, chat_id, reply, kind="reply"):
            _chat(ctx, "coach", reply)
    else:
        # Exempt: a fixed notice that the model could not be reached, plus the
        # deterministic block. Gating it would let a broken verifier hide the outage.
        tg.send(token, chat_id, "I could not reach the model to talk it through. "
                                "Here is where you are:\n\n" + today_block(ctx, day),
                log=log)


def handle_photo(ctx: Context, file_id: str, caption: str, day: date, token,
                 chat_id) -> None:
    """A photo is a barcode, a nutrition label, or a plate. Each takes a different path
    and lands at a DIFFERENT confidence, which is the point.

    barcode        -> database lookup, database confidence
    nutrition label-> the manufacturer's own printed figures, LABEL confidence
    plate          -> items identified by vision, macros still looked up per item

    A photo of a plate is an estimate and a photo of the printed panel is label data.
    Conflating them would put label-grade confidence on a guess."""
    set_inbound(ctx, f"[sent a photo] {caption}".strip() if caption
                else "[sent a photo, no caption]")
    tg.send(token, chat_id, "Looking at that...", log=log)
    path = download_photo(ctx, file_id, token)
    if not path:
        tg.send(token, chat_id, "I could not download that image.", log=log)
        return
    got = NLU.read_photo(str(path), CLAUDE_BIN, LLM_MODEL, log=log)
    kind = got.get("kind")
    # WHAT THE PHOTO WAS, not merely that there was one. "[sent a photo]" is an input the
    # gate cannot judge coherence against - nothing is named in it - so an offer derived
    # from a barcode would be arguably off-topic by construction. Refined once the photo has
    # been read, and it carries his caption because that is often the only wording there is.
    set_inbound(ctx, f"[sent a photo of a {kind.replace('_', ' ')}"
                     + (f", captioned {caption!r}]" if caption else "]")
                if kind and kind != "unknown"
                else f"[sent a photo I could not read"
                     + (f", captioned {caption!r}]" if caption else "]"))
    log(f"photo {path.name} kind={kind} vendor={got.get('vendor')!r} "
        f"items={[i['text'][:40] for i in (got.get('items') or [])]}")

    if kind == "barcode":
        tg.send(token, chat_id, f"Barcode {got['barcode']}, looking it up...", log=log)
        offer_items(ctx, [{"text": got["barcode"], "portion_g": None,
                           "in_session": False}], day, token, chat_id,
                    barcode=got["barcode"])
        return

    if kind == "nutrition_label":
        base = NLU.label_to_item(got)
        item = NR._finalise(base, caption or base["resolved_name"], NR.Rung.MANUAL,
                            "label", [{"rung": "photo", "outcome": "label_panel_read"}],
                            ctx.table, day, degraded=False)
        item["_raw"] = caption or base["resolved_name"]
        if got.get("sodium_from_salt"):
            item["_note"] = "sodium derived from the printed salt figure (salt / 2.5)"
        # A LABEL CAN BE A CORRECTION, not only a new item. See offer_label_as_correction:
        # treating every panel as new is what logged his pizza twice on 14 Aug 2026.
        if offer_label_as_correction(ctx, item, day, token, chat_id):
            return
        set_pending(ctx.store, {"batch": [item]})
        extra = ("\n_Sodium came from the salt figure on the pack, divided by 2.5._"
                 if got.get("sodium_from_salt") else "")
        kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
        send_verified(ctx, token, chat_id, fmt_confirm(item) + extra + "\n\nLog it?",
                      kind="offer", numbers=_gate_numbers([item]), reply_markup=kb)
        return

    if kind == "order":
        # Expand quantities into separate items to log, but compare UNITS against the
        # stated count: 1 bowl + 1 edamame + 3x soy sauce is 5 items on 3 lines.
        expanded = []
        for it in got["items"]:
            expanded.extend([dict(it)] * max(1, int(it.get("qty") or 1)))
        got["items"] = expanded
        NLU.photo_item_hints(got)
        units, stated = got.get("units_seen") or len(expanded), got.get("stated_item_count")
        who = got.get("vendor") or "that order"
        msg = f"{who}, {units} item{'s' if units != 1 else ''}. Looking each one up."
        if stated and stated > units:
            msg += (f"\n\n_The screen says {stated} and I can account for {units}, so the "
                    f"screenshot may be cropped. Tell me what is missing._")
        tg.send(token, chat_id, msg, log=log)
        # A restaurant dish has no label, so this leans on the web rung finding the
        # vendor's own nutrition and falls to an estimate when it cannot. Either way it
        # is flagged.
        offer_items(ctx, got["items"], day, token, chat_id)
        return

    if kind == "food_plate":
        tg.send(token, chat_id,
                "No label, so I have identified the components and will look each one "
                "up. Portions are my estimate, so correct me.", log=log)
        NLU.photo_item_hints(got)
        offer_items(ctx, got["items"], day, token, chat_id)
        return

    if got.get("model_unavailable"):
        # Not the photo. Saying "I could not read that" here sends him off to retake a
        # picture that was fine, and hides an outage that needs fixing on the VM.
        tg.send(token, chat_id,
                "My model access is failing right now - an expired token or a usage "
                "limit - so I cannot read images. Nothing to do with your photo. Try "
                "again shortly, and typed items with the figures still log fine.",
                log=log)
        return
    tg.send(token, chat_id,
            "I could not read that. A barcode or the nutrition panel works best; a "
            "plate is fine too, just tell me roughly what is on it.", log=log)


# How much of today the label is compared against. TODAY'S ENTRIES, not a clock window: the
# stamps on entries are the athlete's LOCAL day and datetime.now() on the VM is an hour off
# in BST, so "the last six hours" would be a comparison against the wrong six hours. The
# same rule the delete guard and entry_he_means already follow.
LABEL_CANDIDATES = 6


def label_candidates(ctx: Context, day: date) -> list:
    """What a photographed label might be correcting: today's recent items, pending first.

    A PENDING ITEM COUNTS. He photographs the pack while the offer is still on the table
    more often than after confirming it, and a label that lands as a second offer there is
    the same double-log one message earlier."""
    out = []
    for i, it in enumerate((get_pending(ctx.store) or {}).get("batch") or []):
        if it.get("_supplement"):
            continue
        out.append({"entry_id": f"pending:{i}",
                    "name": (it.get("resolved_name") or it.get("_raw") or "")[:70],
                    "kcal": it.get("kcal"),
                    "portion_used_g": it.get("portion_used_g"),
                    "figures_from": it.get("source_rung"),
                    "state": "awaiting his confirmation, nothing written yet"})
    for e in (ctx.store.get_day(day).get("entries") or [])[-LABEL_CANDIDATES:]:
        out.append({"entry_id": e.get("id"),
                    "name": (e.get("resolved_name") or e.get("raw_text") or "")[:70],
                    "kcal": e.get("kcal"),
                    "portion_used_g": e.get("portion_used_g") or e.get("portion_g"),
                    "figures_from": e.get("source_rung"),
                    "logged_at": e.get("logged_at"), "state": "logged"})
    return out


def whose_figures(row: dict) -> str:
    """Who produced the figures a label is about to replace: him, the meal model, or a
    lookup.

    THE RULE THIS EXISTS TO KEEP (14 Aug 2026, and it is the strongest rule in this file):
    figures HE supplied are never quietly re-priced. A photographed pack is better data than
    his own reckoning of a plate, and a label correcting a lookup is the whole point of this
    path - but a label landing on numbers he typed himself has to SAY so on the message he
    confirms, or the pack silently overrules the person holding it. `manual` covers both his
    typed figures and a pack he read out loud, so the confidence is what separates them: his
    own reckoning is an estimate, a pack reading is label data."""
    if row.get("_stated"):
        return "your own"
    if row.get("_composed"):
        return "my costed table"
    rung = (row.get("source_rung") or "").strip().lower()
    if rung == "manual" and (row.get("confidence") or "").strip().lower() != "label":
        return "your own"
    if rung == "llm":
        return "my costed table"
    return "a lookup"


def _whose_note(whose: str) -> str:
    """The sentence that says whose figures are being replaced, or nothing when a lookup's
    figures are being corrected - which is the unremarkable case."""
    if whose == "your own":
        return ("\n\n_Those were YOUR figures, not a lookup's. The pack's panel is better "
                "data, but say no and I will leave yours exactly as they are._")
    if whose == "my costed table":
        return ("\n\n_That entry was my costed estimate of the whole meal, so the pack "
                "replaces the table and its component rows with the printed panel._")
    return ""


def offer_label_as_correction(ctx: Context, item: dict, day: date, token,
                              chat_id) -> bool:
    """Offer a photographed label as a CORRECTION to something already there. False when
    it is a new item, which is the normal case and the safe one.

    THE DEFECT (14 Aug 2026). He logged "Coop Chianti beef pizza" by name at a web figure of
    1,147 kcal, then sent the label to fix it. Every label was offered as a new item, so
    after rescaling it to the whole pizza he confirmed a SECOND pizza at 964 kcal, and the
    duplicate had to be cleared out of his store by hand.

    NOTHING IS WRITTEN HERE. The replacement is offered and recorded on the pending record,
    so a label the model has misread costs him a "no" rather than an entry's figures - the
    same rule mark_pending_replaces follows."""
    cands = label_candidates(ctx, day)
    if not cands:
        return False
    decision = NLU.decide_label_target(item, cands, CLAUDE_BIN, LLM_MODEL, log=log)
    if not decision or decision.get("kind") != "replace":
        # Including an unreachable model: offering the label as a new item is what happened
        # before this path existed, so an outage costs him one correction, not a wrong write.
        return False
    target_id = str(decision.get("entry_id") or "")
    log(f"  label decided as a correction to {target_id}")
    if target_id.startswith("pending:"):
        return replace_pending_with_label(ctx, item, target_id, token, chat_id)
    entry = next((e for e in (ctx.store.get_day(day).get("entries") or [])
                  if e.get("id") == target_id), None)
    if entry is None:
        return False
    was = round(entry.get("kcal") or 0)
    now = round(item.get("kcal") or 0)
    set_pending(ctx.store, {"batch": [item],
                            "_apply_label_to": {"id": entry["id"],
                                                "name": entry.get("resolved_name") or "",
                                                "kcal": entry.get("kcal")}})
    kb = tg.inline([[("Replace", "confirm"), ("No", "cancel")]])
    send_verified(ctx, token, chat_id,
                  f"That label looks like the *{entry.get('resolved_name')}* you have "
                  f"already logged, rather than a second one. Replacing its figures with "
                  f"the pack's takes it from {was} to {now} kcal:\n\n"
                  + fmt_confirm(item)
                  + "\n\nReplace it? Say no and I will log the label as a separate item."
                  + _whose_note(whose_figures(entry)),
                  kind="offer", numbers=_gate_numbers([item]), reply_markup=kb)
    _chat(ctx, "coach", f"[log] offered to replace {(entry.get('resolved_name') or '')[:40]}"
                        f" with its label - awaiting confirm")
    return True


def replace_pending_with_label(ctx: Context, item: dict, target_id: str, token,
                               chat_id) -> bool:
    """Swap a PENDING item's figures for the label's and re-offer. False if it cannot.

    Nothing is written either way here - the item was never committed - so this is a
    straight re-offer rather than a second offer, which is the whole point: two offers is
    how a label became a second entry."""
    pend = get_pending(ctx.store) or {}
    batch = list(pend.get("batch") or [])
    try:
        idx = int(target_id.split(":", 1)[1])
    except (IndexError, ValueError):
        return False
    if not 0 <= idx < len(batch):
        return False
    old = batch[idx]
    fresh = dict(item)
    # The time, the meal and the in-session flag are facts about the EATING, which the
    # label knows nothing about. They were established when he logged it and they survive.
    for key in ("_at", "_meal", "in_session", "_supplement"):
        if old.get(key) is not None:
            fresh[key] = old[key]
    batch[idx] = fresh
    set_pending(ctx.store, {**pend, "batch": batch})
    body = "\n\n".join(fmt_confirm(i) for i in batch)
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    was = (old.get("resolved_name") or old.get("_raw") or "item")[:60]
    send_verified(ctx, token, chat_id,
                  f"That is the label for the *{was}* "
                  f"you have not confirmed yet, so I have used the pack's figures for it "
                  f"rather than offering it twice.\n\n" + body
                  + "\n\nLog " + ("these?" if len(batch) > 1 else "it?")
                  # A pending offer can be his own pasted figures or a costed table, and
                  # both outrank a lookup: the swap has to name whose numbers went.
                  + _whose_note(whose_figures(old)),
                  kind="offer", numbers=_gate_numbers(batch), reply_markup=kb)
    _chat(ctx, "coach", "[log] replaced the pending item's figures with its label "
                        "- awaiting confirm")
    return True


def download_photo(ctx: Context, file_id: str, token: str):
    """Fetch a Telegram photo to a temp file for the vision call."""
    import tempfile
    import urllib.request
    info = tg.post(token, "getFile", {"file_id": file_id}, log=log)
    path = ((info.get("result") or {}).get("file_path") or "")
    if not path:
        return None
    url = f"{tg.API}/file/bot{token}/{path}"
    suffix = Path(path).suffix or ".jpg"
    try:
        with urllib.request.urlopen(url, timeout=30, context=tg.SSL_CONTEXT) as r:
            data = r.read()
    except Exception as exc:
        log(f"photo download failed: {exc}")
        return None
    fd, tmp = tempfile.mkstemp(prefix="nut-photo-", suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return Path(tmp)


def offer_planned(ctx: Context, planned: list, day: date, token, chat_id,
                  said: str = "", default_at: str = None,
                  default_meal: str = None) -> None:
    """Resolve each INTERPRETED item, with its form and search terms.

    The interpretation is what makes the ladder honest: it searches good queries, and it
    can throw out a hit whose form is wrong rather than accepting anything whose name
    happens to share a word. A capsule and a protein bar share every meaningful token.

    `said` is his own wording, which the remembered product facts are matched against:
    the interpretation strips quantities, so the word "scoop" is usually gone from the
    canonical name by the time it gets here."""
    batch, notes = [], []
    exclude = _exclusions(ctx, day)
    planned = apply_product_facts(remembered_facts(ctx), planned, said)
    for it in planned[:8]:
        name = it["canonical_name"]
        if it["is_supplement"] or not it["expect_macros"]:
            # Supplements record a dose and are never searched against food data.
            batch.append({
                "raw_text": name, "_raw": name, "resolved_name": name,
                "confidence": "label", "source_rung": NR.Rung.MANUAL,
                "resolved_at": str(day)[:10], "species": [],
                "attempts": [{"rung": "supplement", "outcome": "dose recorded, no lookup",
                              "detail": "supplements are not searched against food data"}],
                "degraded": False, "needs_input": False, "_supplement": True,
                "_trivial": not it["expect_macros"], "_dose_mg": it.get("dose_mg"),
                "in_session": it["in_session"], "_at": it.get("at") or default_at,
                # Carried for shape only: add_supplement has no meal, and a dose is not
                # a meal anyway. Kept so a batch item never has to be tested for the key.
                "_meal": "",
                **{f: None for f in NR.MACRO_FIELDS}})
            dose = (f"{it['dose_mg']:.0f} mg" if it.get("dose_mg")
                    else (f"{it['portion_g']} g" if it.get("portion_g") else "as stated"))
            notes.append(f"*{name}*\nSupplement, {dose}. Recorded as a dose, not looked "
                         f"up against food data, and it does not touch your macros.")
            continue
        # The web rung needs the form to reject a wrong-form product, so it is rebound
        # per item rather than taken from the shared fetcher table.
        fetchers = dict(ctx.fetchers)
        deep = make_deep_fetch()
        fetchers[NR.Rung.WEB] = lambda q, p, _h=it, _d=deep: _d(q, p, hint=_h)
        item = NR.resolve(name, day=day, store=ctx.store, table=ctx.table,
                          portion_g=it.get("portion_g"), fetchers=fetchers,
                          cofid=ctx.cofid, hint=it, queries=it["search_terms"],
                          exclude=exclude)
        item["_raw"] = name
        # WHOSE NUMBER THE PORTION WAS. resolve() takes a caller-supplied portion as stated
        # fact and flags nothing, which is right when the athlete gave the grams and wrong
        # when the interpreter sized a described meal for him ("a large stir fry" -> 300 g of
        # noodles). Unflagged, an invented portion reads on the offer exactly like a weight
        # he supplied, and the assumption he is meant to be checking is invisible. Only set
        # when resolve did not already flag an assumption of its own.
        if (it.get("portion_estimated") and not item.get("portion_estimated")
                and item.get("portion_used_g") and not item.get("needs_input")):
            item["portion_estimated"] = True
            item["portion_assumed"] = (
                f"{float(item['portion_used_g']):.0f} g - my estimate for a portion "
                f"this size")
        item["in_session"] = it["in_session"]
        item["_supplement"] = False
        item["_trivial"] = False
        item["_dose_mg"] = None
        # A time he STATED, carried to the entry. Per item first, then the one stated for
        # the message as a whole, then nothing at all - and nothing means now-time.
        item["_at"] = it.get("at") or default_at
        # Same precedence for the meal, and nothing means the store files it by the clock
        # and marks that it guessed.
        item["_meal"] = ("" if it.get("in_session")
                         else (it.get("meal") or default_meal or ""))
        log(f"    -> {item.get('resolved_name')!r} {item.get('source_rung')}/"
            f"{item.get('confidence')} {item.get('kcal')} kcal")
        batch.append(item)
        notes.append(fmt_confirm(item))
    set_pending(ctx.store, {"batch": batch})
    if any(i.get("in_session") for i in batch):
        notes.append("_Tagged as in-session fuel, so it is protected from any trimming._")
    notes += _stated_time_note(batch) + _stated_meal_note(batch)
    if batch:
        _chat(ctx, "coach", _offer_summary(batch))
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    send_verified(ctx, token, chat_id, "\n\n".join(notes) + "\n\nLog "
                  + ("these?" if len(batch) > 1 else "it?"), kind="offer",
                  numbers=_gate_numbers(batch), reply_markup=kb)


_SAME_AS = re.compile(
    r"\bsame\s+(?:as|one\s+as)\s+(?:before|last\s+time|yesterday|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|the\s+other\s+day)\b"
    r"|\bas\s+(?:yesterday|before|last\s+time)\b"
    r"|\bthe\s+usual\b|\bmy\s+usual\b", re.I)


def from_history(ctx: Context, text: str, day: date, back_days: int = 30) -> dict | None:
    """The entry he means when he says "same as yesterday". None if there is no clear match.

    THE BUG THIS EXISTS FOR. He wrote "Fridge raiders (same as before)" and the bot asked him
    how much - having logged that exact product the previous day at 94 kcal from its label. And
    "Rubicon juice drink, same as yesterday" resolved to a 330 ml can at 66 kcal when yesterday's
    was 15. Both answers were already in his log.

    A figure he has already accepted beats anything a fresh search returns, so his history is
    searched FIRST and a hit is used verbatim - product, portion, figures and provenance."""
    if not _SAME_AS.search(text or ""):
        return None
    words = {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) > 2}
    words -= {"same", "before", "last", "time", "yesterday", "monday", "tuesday",
              "wednesday", "thursday", "friday", "saturday", "sunday", "the", "usual",
              "one", "other", "day", "and", "for", "with", "had"}
    if not words:
        return None
    best, score, when = None, 0, None
    for back in range(1, back_days + 1):          # most recent first
        d = day - timedelta(days=back)
        for e in ctx.store.get_day(d).get("entries") or []:
            name = {w for w in re.split(r"[^a-z0-9]+",
                                        ((e.get("resolved_name") or "") + " "
                                         + (e.get("raw_text") or "")).lower())
                    if len(w) > 2}
            hit = len(words & name)
            if hit > score:
                best, score, when = e, hit, d
        if best is not None:
            break                                  # do not reach past a day that matched
    if best is None or score < 1:
        return None
    log(f"  same-as: reusing {best.get('resolved_name')!r} from {when} "
        f"({best.get('kcal')} kcal)")
    item = {k: best.get(k) for k in
            ("resolved_name", "kcal", "protein_g", "carb_g", "fat_g", "fibre_g",
             "dietary_sodium_mg", "portion_g", "ingredients", "source_url", "confidence",
             "source_rung", "species")}
    item.update({"resolved_at": str(day)[:10], "degraded": False, "needs_input": False,
                 "species_from": best.get("species_from") or "name",
                 "species_unmatched": "", "attempts": [
                     {"rung": "history", "outcome": "hit",
                      "detail": f"same as {when}, as he said"}],
                 "note": f"same as {when.isoformat()}, reusing that entry's figures"})
    return item


def remembered_facts(ctx: Context) -> dict:
    """What he has told us about products, or {} if that cannot be read.

    Wrapped because this sits in front of every resolution: a remembered scoop weight is
    a convenience, and losing one must never be able to stop him logging his food."""
    try:
        return ctx.store.product_facts()
    except Exception as exc:
        log(f"product facts unavailable, continuing without them: {exc}")
        return {}


def _stated_time_note(batch: list) -> list:
    """One line saying the offer will be stamped at the time he stated, so a wrong or
    mis-read time is caught BEFORE it is written rather than after.

    Supplements are skipped: add_supplement has no timestamp field, so promising a time
    for one would be a claim the store cannot keep."""
    times = sorted({i.get("_at") for i in batch
                    if i.get("_at") and not i.get("_supplement")})
    if not times:
        return []
    return [f"_Logging at {', '.join(times)}, as you said._"]


def _stated_meal_note(batch: list) -> list:
    """One line naming the meal the batch will be filed under, when HE named it.

    Said before the write for the same reason the stated time is: a meal read out of his
    sentence is the one thing here that stops being questioned afterwards, so a misread
    one has to be visible while "No" is still an option. Silent when nothing was named -
    the clock fallback is not worth a line every time he logs a snack."""
    meals = sorted({i.get("_meal") for i in batch if i.get("_meal")})
    if not meals:
        return []
    return [f"_Filing under {', '.join(meals)}, as you said._"]


def stated_item(it: dict, day: date, default_at: str = None,
                default_meal: str = None) -> dict:
    """One batch item built from figures the ATHLETE gave, with no lookup at all.

    THE DEFECT THIS EXISTS FOR (14 Aug 2026). He pasted a complete macro table for a
    stir-fry - a total and a row per component - after the bot had mis-priced the meal
    twice. Every row was sent down the resolution ladder as a fresh search, which re-priced
    his 980 kcal dinner at 2,400: the dried-noodle row scaled wrong, and 100 g of oil at
    899 kcal. He had given the answer and was argued with using worse data.

    His figures are the most authoritative source in this system. Nobody knows more about
    what was on his plate than he does, so they are copied VERBATIM - no scaling, no
    rounding, no reconciliation of the total against the rows - and the ladder is not
    walked. The rung is MANUAL, which is what MANUAL has always meant: figures a person
    supplied rather than a source we searched."""
    stated = it.get("stated") or {}
    name = (it.get("text") or "").strip()[:120] or "meal as you described it"
    components = stated.get("components") or []
    macros = {f: stated.get(f) for f in NR.MACRO_FIELDS}
    item = {
        "raw_text": it.get("text") or name,
        "_raw": it.get("text") or name,
        "resolved_name": name,
        # An estimate unless he says he read it off a label. His own reckoning of a meal is
        # careful, not measured, and the log distinguishes the two everywhere else.
        "confidence": "label" if stated.get("basis") == "label" else "estimate",
        "source_rung": NR.Rung.MANUAL,
        "resolved_at": str(day)[:10],
        "species": [],
        "attempts": [{"rung": "manual", "outcome": "stated by the athlete",
                      "detail": "his own figures, used exactly as given; no lookup ran"}],
        "degraded": False,
        "needs_input": False,
        "_supplement": False,
        "_trivial": False,
        "_dose_mg": None,
        # The marker the offer text reads. Named with the batch-item underscore convention
        # because it is about how this item was OBTAINED, not part of the stored record.
        "_stated": True,
        "in_session": bool(it.get("in_session")),
        "_at": it.get("at") or default_at,
        "_meal": "" if it.get("in_session") else (it.get("meal") or default_meal or ""),
        # His own per-part rows, kept as text. They are what he wrote, so they belong in the
        # record - but as a breakdown, never as items that were looked up.
        "ingredients": "; ".join(components) if components else name,
        "_components": components,
        # No per_100g basis: there is no basis, because there was no lookup. That is the
        # honest answer and it also makes a later "x1.5" scale his total by ratio, which is
        # the only correct way to scale a figure he stated.
        **{f: v for f, v in macros.items()},
    }
    if it.get("portion_g"):
        try:
            item["portion_used_g"] = float(it["portion_g"])
        except (TypeError, ValueError):
            pass
    return item


def offer_stated(ctx: Context, items: list, day: date, token, chat_id,
                 default_at: str = None, default_meal: str = None) -> None:
    """Offer figures the athlete supplied, once, with his totals intact.

    Separate from offer_items on purpose rather than as a flag inside it: this path must be
    incapable of reaching NR.resolve. A branch inside the resolving function is one edit
    away from resolving anyway, which is exactly how the ladder came to be re-pricing his
    own table."""
    batch = [stated_item(it, day, default_at, default_meal)
             for it in items[:8] if (it.get("stated") or {}).get("kcal")]
    if not batch:
        return
    set_pending(ctx.store, {"batch": batch})
    _chat(ctx, "coach", _offer_summary(batch))
    body = "\n\n".join(fmt_confirm(i) for i in batch)
    if len(batch) > 1:
        body += f"\n\n*Total* {round(sum(i.get('kcal') or 0 for i in batch))} kcal"
    if any(i.get("in_session") for i in batch):
        body += "\n\n_Tagged as in-session fuel, so it is protected from any trimming._"
    for line in _stated_time_note(batch) + _stated_meal_note(batch):
        body += "\n\n" + line
    log(f"  stated figures accepted verbatim: "
        f"{[round(i.get('kcal') or 0) for i in batch]} kcal, no lookup")
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    send_verified(ctx, token, chat_id, body + "\n\nLog "
                  + ("these?" if len(batch) > 1 else "it?"), kind="offer",
                  numbers=_gate_numbers(batch), reply_markup=kb)


def composed_item(ctx: Context, table: dict, said: str, day: date,
                  default_at: str = None, default_meal: str = None,
                  in_session: bool = False) -> dict:
    """ONE batch item for a whole cooked meal, from the meal table.

    The components are kept on the item, with their portions and their own figures, for two
    reasons. They are what he reads before confirming - an assumption he cannot see is the
    real failure mode here, not an inaccurate gram - and they are what a later correction is
    applied to, so "the noodles were 400 g" is arithmetic on one row rather than a fresh
    guess at the whole dinner.

    Species are tagged by the DETERMINISTIC matcher from the plants the model named, not
    taken from it: the model is good at spotting the ginger in a stir-fry and has no idea
    which canonical species the diversity count uses, and the plant table is the thing that
    knows refined forms score nothing."""
    total = table.get("total") or {}
    plants = table.get("plants") or []
    components = table.get("components") or []
    # The ingredient string the species matcher reads: the plants he was told about, plus
    # the component names, so a plant named only inside a component still counts.
    ingredients = ", ".join(plants + [c["name"] for c in components])
    species, unmatched = [], ""
    if ctx.table is not None:
        try:
            res = ctx.table.match_food(ingredients, ingredients=ingredients)
            species = [{"id": s["id"], "score": s["score"]} for s in res["species"]]
            unmatched = res.get("unmatched") or ""
        except Exception as exc:
            # A meal must remain loggable when the plant table cannot be read.
            log(f"species tagging unavailable for this meal: {exc}")
    band = table.get("error_band_pct") or 15
    item = {
        "raw_text": said,
        "_raw": said,
        "resolved_name": table.get("meal_name") or "meal as you described it",
        # A DECLARED ESTIMATE. It carries a real error band and every assumption behind it,
        # which is what separates this from a figure wearing a database's authority.
        "confidence": "estimate",
        "source_rung": NR.Rung.LLM,
        "source_url": "",
        "resolved_at": str(day)[:10],
        "species": species,
        "species_from": "ingredients",
        "species_unmatched": unmatched,
        "attempts": [{"rung": "meal_model", "outcome": "costed as a whole meal",
                      "detail": f"described meal costed by {MEAL_MODEL}; no rung was "
                                f"walked, because no food table holds a cooked dinner"}],
        "degraded": False,
        "needs_input": False,
        "_supplement": False,
        "_trivial": False,
        "_dose_mg": None,
        # Marks the offer text and, later, the correction route: a composed meal is
        # re-tabled rather than re-searched.
        "_composed": True,
        "_components_detail": components,
        "_assumptions": table.get("assumptions") or [],
        "_error_band_pct": band,
        "note": f"a described meal, so roughly +/-{band}%",
        "in_session": bool(in_session),
        "_at": default_at,
        "_meal": "" if in_session else (default_meal or ""),
        "ingredients": ingredients,
        # Every portion in here was reasoned from his words, so the whole entry is an
        # assumed portion and the confirm line says so.
        "portion_used_g": (round(sum(c["portion_g"] for c in components
                                     if c.get("portion_g")), 1) or None),
        "portion_estimated": True,
        "portion_assumed": "portions worked out from your description",
        **{f: total.get(f) for f in NR.MACRO_FIELDS},
    }
    return item


def fmt_meal_confirm(item: dict) -> str:
    """The confirm block for a costed meal: the table, then the total, then the assumptions.

    He gets what he would have got by asking a model himself, which is the standard this
    path was held to. The assumptions are not an appendix - they are the part he corrects."""
    lines = [f"*{item['resolved_name']}*",
             f"Costed as a whole meal, roughly +/-{item.get('_error_band_pct') or 15}%."]
    for c in item.get("_components_detail") or []:
        grams = f"{c['portion_g']:.0f}g " if c.get("portion_g") else ""
        lines.append(f"· {grams}{c['name']} — {round(c['kcal'])} kcal"
                     + (f", {round(c['protein_g'])}P" if c.get("protein_g") is not None
                        else "")
                     + (f" {round(c['carb_g'])}C" if c.get("carb_g") is not None else "")
                     + (f" {round(c['fat_g'])}F" if c.get("fat_g") is not None else ""))
    lines.append(" · ".join(
        f"{lbl} {round(item[k])}" for lbl, k in
        (("*Total* kcal", "kcal"), ("P", "protein_g"), ("C", "carb_g"), ("F", "fat_g"))
        if item.get(k) is not None))
    if item.get("fibre_g"):
        lines.append(f"fibre {round(item['fibre_g'])} g")
    if item.get("species"):
        lines.append(f"{len(item['species'])} plant"
                     f"{'s' if len(item['species']) != 1 else ''}")
    for a in (item.get("_assumptions") or [])[:6]:
        lines.append(f"_assumed: {a}_")
    lines.append("_Correct any of that and I will redo the table._")
    return "\n".join(lines)


def offer_composed(ctx: Context, said: str, day: date, token, chat_id,
                   default_at: str = None, default_meal: str = None,
                   in_session: bool = False, extra: str = "") -> bool:
    """Cost a described meal in ONE call and offer it as ONE entry. False if that failed.

    Returns False rather than degrading in place, so the caller can fall back to the
    interpret-and-resolve path: a cooked-state, portioned ladder answer is a poor second to
    this and a great deal better than nothing, and an unreachable model must never mean an
    unloggable dinner."""
    ask = f"{said.strip()} ({extra.strip()})" if extra else said
    table = NLU.describe_meal(ask, CLAUDE_BIN, MEAL_MODEL, log=log)
    if not table:
        log("  meal table unavailable, falling back to the ladder")
        return False
    item = composed_item(ctx, table, said, day, default_at=default_at,
                         default_meal=default_meal, in_session=in_session)
    set_pending(ctx.store, {"batch": [item]})
    _chat(ctx, "coach", _offer_summary([item]))
    body = fmt_meal_confirm(item)
    if in_session:
        body += "\n\n_Tagged as in-session fuel, so it is protected from any trimming._"
    for line in _stated_time_note([item]) + _stated_meal_note([item]):
        body += "\n\n" + line
    log(f"  meal costed by {MEAL_MODEL}: {round(item.get('kcal') or 0)} kcal across "
        f"{len(item.get('_components_detail') or [])} components, no rung walked")
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    send_verified(ctx, token, chat_id, body + "\n\nLog it?", kind="offer",
                  numbers=_gate_numbers([item]), reply_markup=kb)
    return True


def offer_items(ctx: Context, items: list, day: date, token, chat_id,
                supplement: bool = False, barcode: str = None,
                trivial: bool = False, dose_mg: float = None,
                said: str = "", default_at: str = None,
                default_meal: str = None) -> None:
    """Resolve each item separately and ask once for the batch.

    Per-item resolution matters: a whole sentence resolved as one string both
    mis-costs it and loses the per-item provenance the confidence flag depends on."""
    items = apply_product_facts(remembered_facts(ctx), items, said)
    if supplement:
        # A supplement is a DOSE, not a food, so it never touches a food database.
        # Leaving it on the ladder is how "400mg of my protein collagen capsules"
        # name-matched a COLLAGEN PROTEIN BAR and picked up 4 plant species from that
        # bar's ingredient list. A capsule has no ingredients worth searching for and no
        # plants in it; what matters is what it is and how much.
        batch = []
        for it in items[:8]:
            batch.append({
                "raw_text": it["text"], "_raw": it["text"],
                "resolved_name": it["text"],
                "confidence": "label",          # he read it off his own pack
                "source_rung": NR.Rung.MANUAL,
                "resolved_at": str(day)[:10],
                "species": [],                  # a capsule is not a plant
                "attempts": [{"rung": "supplement", "outcome": "dose recorded, no lookup",
                              "detail": "supplements are not searched against food data"}],
                "degraded": False, "needs_input": False,
                "_supplement": True, "_trivial": bool(trivial), "_dose_mg": dose_mg,
                "in_session": bool(it.get("in_session")),
                "_at": it.get("at") or default_at,
                "_meal": "",                    # a dose is not a meal
                **{f: None for f in NR.MACRO_FIELDS},
            })
        set_pending(ctx.store, {"batch": batch})
        _chat(ctx, "coach", _offer_summary(batch))
        dose = (f"{dose_mg:.0f} mg" if dose_mg
                else (f"{items[0].get('portion_g')} g" if items[0].get("portion_g")
                      else "dose as stated"))
        kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
        send_verified(ctx, token, chat_id,
                      f"*{items[0]['text']}*\nSupplement, {dose}. Recorded as a dose, not "
                      f"looked up against food data, and it does not touch your macros."
                      + ("\n_Tell me the label figures if you want them counted._"
                         if not trivial else "")
                      + "\n\nLog it?", kind="offer",
                      numbers=_gate_numbers(batch), reply_markup=kb)
        return

    resolved = []
    for it in items[:8]:
        # HIS OWN LOG FIRST when he says "same as": no search can beat a figure he has
        # already accepted, and asking him again for something he told the bot yesterday is
        # what made lunch unloggable.
        prior = from_history(ctx, it["text"], day)
        if prior is not None:
            prior["_raw"] = it["text"]
            prior["in_session"] = bool(it.get("in_session"))
            prior["_supplement"] = supplement
            prior["_trivial"] = bool(trivial)
            prior["_dose_mg"] = dose_mg
            prior["_at"] = it.get("at") or default_at
            prior["_meal"] = ("" if it.get("in_session")
                              else (it.get("meal") or default_meal or ""))
            resolved.append(prior)
            continue
        log(f"  resolving {it['text'][:60]!r} portion={it.get('portion_g')}")
        # A barcode short-circuits the text ladder: an exact product lookup beats any
        # name search, so it is tried before the ordinary rungs rather than as one.
        fetchers = dict(ctx.fetchers)
        # A dish from a chain we hold nutrition for goes to the chain's own figures first.
        # Wired per ITEM because it depends on the vendor the photo identified - the same
        # hand-off that was being dropped entirely until tonight.
        hint = it.get("hint") or {}
        vendor = hint.get("brand")
        if vendor and hint.get("category") == "restaurant_dish":
            # No allowlist. A vendor we hold nothing for is DISCOVERED and verified
            # against the parse, then remembered - so this works for wherever he orders
            # from, not only for chains somebody approved in advance. A miss is cached
            # too, briefly, because discovery is a search plus a multi-megabyte fetch.
            discover = RS.make_discover(CLAUDE_BIN, LLM_MODEL, log=log)
            fetchers[NR.Rung.VENDOR] = (
                lambda t, p, _v=vendor, _d=discover: RS.lookup(
                    _v, t, RESTAURANT_CACHE, discover=_d))
        if barcode:
            fetchers[NR.Rung.RETAILER] = (
                lambda t, p, _c=barcode: NR.off_barcode_fetch(_c, p))
        # The hint has to be FORWARDED, not dropped. Every hint-based protection in the
        # ladder - the CoFID skip for anything that is not a whole food, the form-conflict
        # check - is inert without it, and this caller had no hint parameter at all. That
        # is how a Wagamama order came back as "Rice, brown, raw": read_photo knew it was
        # a restaurant order from a named vendor, and offer_items threw that away before
        # resolve ever saw it. Same class of bug as the dropped species score and the
        # dropped provisional flag: computed at one stage, lost at the hand-off.
        item = NR.resolve(it["text"], day=day, store=ctx.store, table=ctx.table,
                          portion_g=it.get("portion_g"), fetchers=fetchers,
                          cofid=ctx.cofid, hint=hint,
                          queries=hint.get("search_terms"),
                          exclude=_exclusions(ctx, day))
        item["_raw"] = it["text"]
        item["in_session"] = bool(it.get("in_session"))
        item["_supplement"] = supplement
        item["_trivial"] = bool(trivial)
        item["_dose_mg"] = dose_mg
        item["_at"] = it.get("at") or default_at
        item["_meal"] = ("" if it.get("in_session")
                         else (it.get("meal") or default_meal or ""))
        resolved.append(item)
    set_pending(ctx.store, {"batch": resolved})
    if resolved:
        _chat(ctx, "coach", _offer_summary(resolved))
    body = "\n\n".join(fmt_confirm(i) for i in resolved)
    if any(i.get("in_session") for i in resolved):
        body += "\n\n_Tagged as in-session fuel, so it is protected from any trimming._"
    for line in _stated_time_note(resolved) + _stated_meal_note(resolved):
        body += "\n\n" + line
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    send_verified(ctx, token, chat_id, body + "\n\nLog "
                  + ("these?" if len(resolved) > 1 else "it?"), kind="offer",
                  numbers=_gate_numbers(resolved), reply_markup=kb)


def publish_now(ctx: Context) -> None:
    """Push the app's data as soon as something is logged, in the background.

    Publishing was only on the hourly cron, so a protein bar logged at 08:17 did not
    reach the app until 09:09 and looked lost. Nothing was wrong with the log - the page
    simply had no way to know yet.

    Threaded because publish plus a push is seconds and the confirmation should not wait
    for it, and routed through cc-git-commit-push.sh so it takes the repo lock and the
    push retry that every other writer of this tree goes through - the crons write the
    same working copy every few minutes."""
    import threading

    def run():
        try:
            pub = subprocess.run(
                [sys.executable, str(BASE / "scripts" / "publish-nutrition-data.py")],
                capture_output=True, text=True, timeout=120)
            if pub.returncode != 0:
                log(f"publish failed: {(pub.stderr or pub.stdout or '')[:200]}")
                return
            rel = f"ClaudeCoach/public/nutrition-{ctx.slug}.json"
            if not (BASE.parent / rel).exists():
                log(f"publish wrote nothing for {ctx.slug} (tracker off?)")
                return
            push = subprocess.run(
                [str(BASE / "scripts" / "cc-git-commit-push.sh"),
                 f"nutrition: {ctx.slug} log update", rel],
                capture_output=True, text=True, timeout=180)
            log(f"published to the app: rc={push.returncode} "
                f"{(push.stdout or '').strip()[-120:]}")
        except Exception as exc:
            # Never let this break the logging path: the entry is already written and
            # the hourly cron will publish it regardless.
            log(f"publish_now failed, cron will catch it: {exc}")

    threading.Thread(target=run, daemon=True).start()


def _offer_summary(batch: list) -> str:
    """One terse line for the chat store: what was just offered, not the full confirm
    text - recent_chat() keeps only a handful of turns, and the full fmt_confirm block
    would push everything else out of it within a couple of exchanges."""
    if len(batch) == 1:
        name = (batch[0].get("resolved_name") or batch[0].get("_raw") or "that")[:50]
        kcal = batch[0].get("kcal")
        return (f"[log] offered: {name} ({round(kcal)} kcal) — awaiting confirm"
                if kcal else f"[log] offered: {name} — awaiting confirm")
    return f"[log] offered {len(batch)} items — awaiting confirm"


def _name_matches(named: str, resolved: str) -> bool:
    """Does the thing he named share an identifying word with this entry?"""
    import re as _re
    stop = {"the", "one", "that", "this", "my", "and", "of", "a", "an", "it"}
    want = {w for w in _re.split(r"[^a-z0-9]+", named.lower()) if len(w) > 2} - stop
    have = {w for w in _re.split(r"[^a-z0-9]+", resolved.lower()) if len(w) > 2}
    return bool(want & have)


def mark_pending_replaces(ctx: Context, entry_id: str, name: str) -> None:
    """Note that confirming this batch should REMOVE the entry it corrects.

    Recorded on the pending record rather than acted on now, because nothing may be deleted
    before he has confirmed the replacement - otherwise a declined correction has quietly
    destroyed the original."""
    pend = get_pending(ctx.store)
    if not pend:
        return
    pend["_replaces"] = {"id": entry_id, "name": name}
    # A gate block SURVIVES this. set_pending drops the mark, which is right for a re-offer
    # and wrong here: this is an annotation on the offer that was just sent, and it runs
    # AFTER it - so without re-marking, an offer the gate blocked would quietly become
    # confirmable again on exactly the two paths that replace an existing entry.
    blocked = pend.pop("_gate_blocked", "")
    set_pending(ctx.store, pend)
    if blocked:
        _mark_pending_gate_blocked(ctx, blocked)


# WHAT HE SAID IT WAS NOT. Read with regexes rather than by asking the model: this has to
# work on the message that is already the SECOND time he has said it, so it cannot depend
# on a model call that may time out, and a correction that silently fails to register is
# the whole bug. Four shapes cover every real example from 12 Aug 2026 - "not peanut
# butter", "it wasn't peanut butter", "I never said peanut butter", "remove the peanut
# butter".
_EXCLUDE_PATTERNS = (
    re.compile(r"\bnever\s+(?:said|had|mentioned|ate|asked\s+for)\s+(.{1,60})", re.I),
    re.compile(r"\b(?:it\s+)?(?:was\s+not|wasn'?t|isn'?t|is\s+not|are\s+not|aren'?t)"
               r"\s+(.{1,60})", re.I),
    re.compile(r"\bremove\s+(.{1,60})", re.I),
    re.compile(r"\bnot\s+(.{1,60})", re.I),          # last: the loosest of the four
)
# Words that are not the food. A phrase is cut at the first of these AFTER its first real
# word, so "peanut butter at all" and "peanut butter, it was just butter" both reduce to
# "peanut butter" rather than swallowing the rest of the sentence.
_EXCLUDE_FILLER = {"the", "a", "an", "any", "some", "my", "that", "this", "it", "its",
                   "was", "were", "is", "are", "just", "actually", "really", "even",
                   "ever", "at", "all", "and", "but", "or", "then", "again", "said",
                   "had", "have", "no", "not", "i", "you", "we", "to", "of", "in", "on",
                   "for", "with", "what", "did", "do", "please", "thing", "stuff",
                   # WHEN, never WHAT. "never had peanut butter today" left "today" on the
                   # phrase, and an exclusion carrying a word no product name contains
                   # matches nothing at all - it fails silently, which is the one outcome
                   # this feature cannot have.
                   "today", "tonight", "yesterday", "earlier", "morning", "afternoon",
                   "evening", "either", "anything", "never", "twice", "times", "again",
                   # amounts, never identities (see the quantity guard below)
                   "whole", "pack", "packet", "portion", "partial", "gram", "grams",
                   "half", "bigger", "smaller", "more", "less"}

# Quantity words that pass the three-letter test but still name an amount. A phrase
# made only of these is a quantity correction wearing exclusion clothes.
_EXCLUDE_QUANTITY = {"gram", "grams", "pack", "packet", "portion", "partial", "whole",
                     "half", "quarter", "double", "large", "small", "medium", "big"}


def exclusions_in(text: str) -> list:
    """The phrases this correction rules out, longest-first, at most 4 words each.

    THE BUG THIS EXISTS FOR. "butter" resolved to "Peanut butter, smooth" six times on
    12 Aug 2026, and twice of those were AFTER "I never said peanut butter". A correction
    re-runs a deterministic ladder, so without a memory of what was rejected it returns the
    same wrong answer and asks him to confirm it again. There is no number of corrections
    that breaks that loop, which is why the memory is the fix and not better matching."""
    out = []
    for pat in _EXCLUDE_PATTERNS:
        m = pat.search(text or "")
        if not m:
            continue
        words = re.findall(r"[a-z0-9'-]+", m.group(1).lower())
        kept = []
        for w in words:
            if w in _EXCLUDE_FILLER:
                if kept:
                    break                # the food has ended; the sentence carries on
                continue                 # leading filler: "not THE peanut butter"
            kept.append(w)
            if len(kept) == 4:
                break
        phrase = " ".join(kept)
        # An exclusion must name a FOOD. "That's 100g I had 160g" produced the
        # exclusion '100g' and "But I had the whole pack" produced 'partial portion'
        # (13 Aug 2026) - quantity words, which no product name contains, so they
        # match nothing and silently bloat the day's list. A phrase with no token of
        # three or more letters is an amount, not an identity.
        if not any(re.search(r"[a-z]{3}", w) and w not in _EXCLUDE_QUANTITY
                   for w in kept):
            continue
        if phrase and phrase not in out:
            out.append(phrase)
    return out


# --- quantity corrections: arithmetic, never a search ------------------------
#
# THE BUG THIS EXISTS FOR (13 Aug 2026, the tortilla label). He photographed a
# nutrition panel - manufacturer figures, the best data this bot ever holds - then
# said "That's 100g I had 160g". The correction path re-ran the LADDER on the string
# "item from label photo", throwing away the label to search for it. Then "But I had
# the whole pack" dead-ended on "needs a portion" with the pack weight printed on the
# very photo it had just read. A correction that only changes HOW MUCH is a
# multiplication against the item in hand; re-identifying the food is the one thing
# it must never do.

_QTY_UNIT = r"(g|grams?|kg|ml|l|litres?)"
_QTY_ANCHORED = re.compile(
    rf"\b(?:had|ate|was|about|closer\s+to|more\s+like)\s+(\d+(?:\.\d+)?)\s*{_QTY_UNIT}\b",
    re.I)
_QTY_ANY = re.compile(rf"\b(\d+(?:\.\d+)?)\s*{_QTY_UNIT}\b", re.I)
_QTY_WHOLE = re.compile(r"\b(?:whole|entire|all\s+of\s+the|full)\s+"
                        r"(?:pack(?:et)?|bag|tub|bar|box|bottle|pot|tin|can|thing|it)\b",
                        re.I)
_QTY_FACTOR = ((re.compile(r"\bhalf\b", re.I), 0.5),
               (re.compile(r"\b(?:twice|two\s+of\s+them|x\s*2|double)\b", re.I), 2.0))


def _qty_grams(m) -> float:
    n, unit = float(m.group(1)), m.group(2).lower()
    return n * 1000 if unit in ("kg", "l", "litre", "litres") else n


def quantity_correction(text: str):
    """{'grams': x} | {'whole_pack': True} | {'factor': f} | None.

    None whenever the correction also disputes WHAT the food was - "not peanut butter,
    it was 20g of jam" must go down the re-resolve path with its exclusion, not be
    rescaled into more of the wrong thing. exclusions_in() finding anything is the
    signal for that: an identity dispute names a food, a quantity dispute cannot."""
    t = text or ""
    if exclusions_in(t):
        return None
    m = _QTY_ANCHORED.search(t)
    if m:
        return {"grams": _qty_grams(m)}
    ms = list(_QTY_ANY.finditer(t))
    if ms:
        # Two bare numbers is "that's 100g I had 160g" shaped: the amount he ATE is
        # stated last, the label's basis first. Taking the first would re-log the basis.
        return {"grams": _qty_grams(ms[-1])}
    if _QTY_WHOLE.search(t):
        return {"whole_pack": True}
    for pat, f in _QTY_FACTOR:
        if pat.search(t):
            return {"factor": f}
    return None


_RESCALE_FIELDS = ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g",
                   "dietary_sodium_mg")


def rescale_item(item: dict, grams: float = None, factor: float = None,
                 estimated: bool = False, why: str = ""):
    """The same item at a different amount, or None when there is no basis to scale
    from. Prefers the per-100g basis (exact, from the label or table); falls back to
    scaling the current figures by the ratio of portions. Never touches identity.

    `estimated` says WHOSE number the new amount is. It defaults to False because almost
    every caller is executing an amount the athlete stated, but the meal-sizing path is
    not: there the grams are the model's reading of "it was a whole meal", and presenting
    those as "as stated" would put words in his mouth and hide the assumption from the one
    message where he can still say no."""
    out = dict(item)
    per = item.get("per_100g") or {}
    if grams is not None and per:
        for f in _RESCALE_FIELDS:
            if per.get(f) is not None:
                out[f] = round(float(per[f]) * grams / 100.0, 1)
    elif grams is not None and item.get("portion_used_g"):
        ratio = grams / float(item["portion_used_g"])
        for f in _RESCALE_FIELDS:
            if item.get(f) is not None:
                out[f] = round(float(item[f]) * ratio, 1)
    elif factor is not None:
        for f in _RESCALE_FIELDS:
            if item.get(f) is not None:
                out[f] = round(float(item[f]) * factor, 1)
        if item.get("portion_used_g"):
            grams = float(item["portion_used_g"]) * factor
    else:
        return None
    if out.get("dietary_sodium_mg") is not None:
        out["dietary_sodium_mg"] = round(out["dietary_sodium_mg"])
    if grams is not None:
        out["portion_used_g"] = grams
        out["portion_assumed"] = (f"{grams:.0f} g - {why or 'an estimated portion'}"
                                 if estimated else f"{grams:.0f} g - as stated")
    # A STATED amount is not an assumption - the flag exists to mark guesses.
    out["portion_estimated"] = bool(estimated)
    return out


def drop_stale_breakdown(item: dict) -> dict:
    """Forget a component breakdown whose figures no longer add up to the entry.

    His pasted rows are TEXT, so scaling the entry's totals cannot scale them - and leaving
    them on a rescaled item shows him "Egg noodles 380 kcal" under a 1,470 kcal heading. The
    breakdown was his description of the original amount and it stops being true the moment
    the amount changes; dropping it loses nothing that is still correct."""
    if not item.get("_components"):
        return item
    out = dict(item)
    out.pop("_components", None)
    out["ingredients"] = out.get("resolved_name") or out.get("ingredients") or ""
    return out


def apply_quantity_correction(ctx: Context, pend, qc: dict, day: date,
                              token, chat_id) -> bool:
    """Handle a pure-quantity correction against the pending offer or the latest
    entry. Returns True when handled (including by asking for the pack size)."""
    batch = (pend or {}).get("batch") or []
    if any(i.get("_composed") for i in batch):
        # Same reason as apply_batch_rescale: scaling a costed meal's totals would leave its
        # component rows contradicting them, and re-render without the table.
        log("  quantity correction declined: a costed meal is re-tabled instead")
        return False
    item = batch[0] if len(batch) == 1 else None
    target = None
    if item is None:
        if batch:
            # A MULTI-COMPONENT OFFER IS NOT A COMMITTED ENTRY. The same wrong-target trap as
            # the correction branch had: falling through to find_entry here would rescale
            # something he logged earlier because the thing in front of him has four parts.
            # A single ratio for a whole meal is rescale_all, which is handled before this.
            log(f"  quantity correction declined: {len(batch)} items are pending")
            return False
        target = ctx.store.find_entry(day, "")
        if not target:
            return False
        item = target
    grams, factor = qc.get("grams"), qc.get("factor")
    if qc.get("whole_pack"):
        pack = item.get("pack_g")
        if not pack:
            tg.send(token, chat_id,
                    "How many grams is the whole pack? It is not printed on what I "
                    "have - reply e.g. “380g” and I will scale it.", log=log)
            _chat(ctx, "coach", "[log] asked for pack size to scale a whole-pack claim")
            return True
        grams = float(pack)
    new = rescale_item(item, grams=grams, factor=factor)
    if new is None:
        return False              # no basis to scale from: let re-resolution handle it
    if target is None:
        batch[0] = drop_stale_breakdown(new)
        set_pending(ctx.store, {**pend, "batch": batch})
        kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
        # AN OFFER THAT REPLACES AN ENTRY STILL SAYS SO after a rescale. "Log it?" on its own
        # reads as a second helping, which is the exact misreading that logged his pizza
        # twice - and the confirmation genuinely does update rather than add.
        replaces = ((pend or {}).get("_apply_label_to") or {}).get("name")
        lead = (f"Still replacing *{replaces}*'s figures, at the new amount:\n\n"
                if replaces else "")
        send_verified(ctx, token, chat_id, lead + fmt_confirm(new) + "\n\nLog it?",
                      kind="offer", numbers=_gate_numbers([new]), reply_markup=kb)
        _chat(ctx, "coach", f"[log] rescaled offer to "
                            f"{new.get('portion_used_g') or '?'} g - awaiting confirm")
    else:
        patch = {f: new.get(f) for f in _RESCALE_FIELDS if new.get(f) is not None}
        patch.update({"portion_used_g": new.get("portion_used_g"),
                      "portion_estimated": False,
                      "portion_assumed": new.get("portion_assumed")})
        ctx.store.update_entry(day, target["id"], **patch)
        record_action(ctx, f"updated entry {target['id']} {target.get('resolved_name')} to "
                           f"{new.get('portion_used_g'):.0f} g, "
                           f"{round(new.get('kcal') or 0)} kcal")
        publish_now(ctx)
        send_verified(ctx, token, chat_id,
                      f"Rescaled *{target.get('resolved_name')}* to "
                      f"{new.get('portion_used_g'):.0f} g: "
                      f"{round(new.get('kcal') or 0)} kcal."
                      + "\n\n" + today_block(ctx, day), kind="correction",
                      numbers=_gate_numbers([new]))
        _chat(ctx, "coach", f"[log] rescaled {target.get('resolved_name')} to "
                            f"{new.get('portion_used_g'):.0f} g")
    return True


def apply_batch_rescale(ctx: Context, pend, decision: dict, day: date,
                        token, chat_id) -> bool:
    """Rescale some or all of a pending batch and re-offer it. True once handled.

    THE THREE FAILURES THIS ANSWERS, all from the same evening (14 Aug 2026). "Do all of
    that x1.5" was decided correctly as a factor and then applied to nothing, because the
    only executor took a single item. "Make the noodles, steak and sauce 1.5x and the
    vegetables 3x" had no shape to be expressed in at all. And "it was a whole meal" had no
    way to put a portion on four components at once.

    EVERY NUMBER HERE IS COMPUTED BY rescale_item, from each component's own basis. The
    model supplied only a factor or a portion, which is the invariant the whole module rests
    on: it decides meaning and quantity, the code does arithmetic."""
    batch = list((pend or {}).get("batch") or [])
    if not batch:
        return False
    # A COSTED MEAL IS NEVER SCALED IN PLACE. rescale_item moves the entry's totals and knows
    # nothing about _components_detail, so "all of that x1.5" would leave a 1,402 kcal entry
    # whose own rows still sum to 935 - and those rows are what the NEXT correction is applied
    # to. It would also re-render through fmt_confirm, losing the table and the assumptions
    # and stating a different error band from the one on the item. Declining sends it to the
    # re-table branch, which is one model call and keeps the meal internally consistent.
    if any(i.get("_composed") for i in batch):
        log("  batch rescale declined: a costed meal is re-tabled, not scaled in place")
        return False
    kind = decision.get("kind")
    if kind == "rescale_all":
        factor = decision.get("factor")
        specs = [{"index": i, "factor": factor} for i in range(len(batch))]
    else:
        specs = decision.get("items") or []
    if not specs:
        return False
    # A portion the MODEL sized is flagged as an estimate and stated on the offer; a factor
    # or a weight he gave is not. meal_portions is the only kind where the grams are ours.
    estimated = (kind == "meal_portions")
    why = "my estimate of the portion for a meal that size"
    fresh, changed, refused = list(batch), [], []
    for spec in specs:
        idx = spec["index"]
        # NOTHING TO SCALE IS NOT THE SAME AS SCALED. rescale_item's factor branch multiplies
        # whatever macro fields are present, so an item with none - one still waiting on
        # figures - passes through it unchanged and would be counted as done, and the reply
        # would claim to have scaled all of them.
        has_basis = (bool(fresh[idx].get("per_100g"))
                     or any(fresh[idx].get(f) is not None for f in _RESCALE_FIELDS))
        scaled = rescale_item(fresh[idx], grams=spec.get("grams"),
                              factor=spec.get("factor"),
                              estimated=estimated, why=why) if has_basis else None
        if scaled is None:
            # No per-100g basis and no current portion, so there is nothing to scale FROM.
            # Named in the reply rather than skipped in silence: a component that quietly
            # kept its old figure inside a rescaled meal is a wrong total he cannot see.
            refused.append(fresh[idx].get("resolved_name")
                           or fresh[idx].get("_raw") or f"item {idx + 1}")
            continue
        fresh[idx] = drop_stale_breakdown(scaled)
        changed.append(idx)
    if not changed:
        log(f"  batch rescale had no basis to work from: {refused}")
        return False
    set_pending(ctx.store, {**pend, "batch": fresh})
    body = "\n\n".join(fmt_confirm(i) for i in fresh)
    total = round(sum(i.get("kcal") or 0 for i in fresh))
    lead = {"rescale_all": f"Scaled all {len(fresh)} of them.",
            "rescale_items": f"Scaled {len(changed)} of them, the rest are unchanged.",
            "meal_portions": "Sized it as a meal - every portion below is my estimate, "
                             "so correct any that look wrong."}[kind]
    if refused:
        lead += (" I could not scale " + ", ".join(r[:40] for r in refused)
                 + ": there is no portion or per-100g basis behind "
                 + ("it" if len(refused) == 1 else "them")
                 + ", so tell me the grams and I will redo "
                 + ("it." if len(refused) == 1 else "them."))
    if len(fresh) > 1:
        body += f"\n\n*Total* {total} kcal"
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    send_verified(ctx, token, chat_id, lead + "\n\n" + body + "\n\nLog "
                  + ("these?" if len(fresh) > 1 else "it?"), kind="offer",
                  numbers=_gate_numbers(fresh), reply_markup=kb)
    _chat(ctx, "coach", f"[log] rescaled {len(changed)} of {len(fresh)} offered items "
                        f"to {total} kcal - awaiting confirm")
    return True


def _entry_haystack(entry: dict) -> str:
    """Everything about an entry that he might use to point at it.

    The name alone is not enough: he says "the 160g", which names the AMOUNT. Matching
    that against `resolved_name` only would have failed the guard below and asked him
    which entry he meant while pointing straight at it."""
    grams = entry.get("portion_used_g") or entry.get("portion_g")
    parts = [entry.get("resolved_name") or "", entry.get("raw_text") or ""]
    if grams:
        try:
            parts.append(f"{float(grams):.0f}g")
        except (TypeError, ValueError):
            pass
    return " ".join(parts)


def entry_he_means(ctx: Context, day: date, which: str, verb: str,
                   token, chat_id) -> dict | None:
    """The entry `which` names, or the latest when he named none. None once it has
    already replied to him.

    Same guard as the delete branch, for the same reason: a name it could not find must
    ask, because silently retiming or renaming the wrong entry is worse than asking -
    nothing in the reply would look wrong.

    The SEARCH and the GUARD deliberately use one vocabulary. find_entry matches
    `resolved_name` only and falls back to the latest entry, so pairing it with a guard
    that also reads raw_text and the portion would let "the 160g" pass only when the 160 g
    entry happened to be the last one logged - green in a two-entry fixture, wrong in a
    real day. Two vocabularies for one lookup is the shape that took three athletes'
    plans out in July.

    An AMBIGUOUS name asks as well. "The initial rye bread" is exactly the case where
    taking the newest match is wrong, and "initial" is a word no matcher here
    understands."""
    which = (which or "").strip()
    entries = ctx.store.get_day(day).get("entries") or []
    if not entries:
        tg.send(token, chat_id, f"Nothing logged today to {verb}.", log=log)
        return None
    if not which:
        return entries[-1]
    hits = [e for e in entries if _name_matches(which, _entry_haystack(e))]
    if len(hits) == 1:
        return hits[0]
    names = [e.get("resolved_name") or "" for e in entries][-6:]
    if not hits:
        tg.send(token, chat_id,
                f"I cannot see {which!r} in today’s log. Today has: "
                + ", ".join(n[:34] for n in names)
                + f". Name one of those and I will {verb} it.", log=log)
    else:
        tg.send(token, chat_id,
                f"Which one do you mean? I have "
                + ", ".join((e.get("resolved_name") or "?")[:30] for e in hits)
                + f". Name it and I will {verb} just that one.", log=log)
    return None


def apply_retime(ctx: Context, decision: dict, day: date, token, chat_id) -> bool:
    """Move an entry's logged_at. True once handled, including by asking.

    "The initial rye bread was 830am" had nowhere to land: entries carried a timestamp
    written when he typed the message, and the app buckets entries into meals by that
    clock, so a morning slice written up at lunchtime read as lunch and the only fix was
    editing the month file by hand.

    The stamp is built from `day` - the athlete's ICU local date - and never from the
    server clock, which is the store's standing rule about who decides a local day."""
    entry = entry_he_means(ctx, day, decision.get("which"), "retime", token, chat_id)
    if entry is None:
        return True
    stamp = f"{day.isoformat()}T{decision['time']}"
    done = ctx.store.update_entry(day, entry["id"], logged_at=stamp)
    if not done:
        return False
    record_action(ctx, f"updated entry {entry['id']} {done.get('resolved_name')}: "
                       f"logged_at={decision['time']}")
    # Entries are deliberately NOT re-sorted: /undo pops the tail and "delete that" reads
    # the last one, both meaning "the thing you logged most recently". Ordering by the
    # stated time would repoint those at a different entry.
    publish_now(ctx)
    _chat(ctx, "coach", f"[log] retimed {(done.get('resolved_name') or '')[:40]} "
                        f"to {decision['time']}")
    send_verified(ctx, token, chat_id,
                  f"Moved *{done.get('resolved_name')}* to {decision['time']}.",
                  kind="correction", numbers=_gate_numbers([done]))
    return True


def apply_meal_correction(ctx: Context, decision: dict, pend, target_item, day: date,
                          token, chat_id) -> bool:
    """File something under a meal he has just named. True once handled.

    Two cases, and only the second one used to work:

    PENDING OFFER. The meal lands on the batch so it is written WITH the entry, rather
    than being applied to whatever was logged before it - which is what a set_meal here
    would have done, since the item he is talking about does not exist yet. Every item in
    the batch takes it: "that was breakfast" against a two-item offer means both, and a
    matcher guessing which of two foods he meant would be wrong silently.

    NOTHING PENDING. set_meal against the entry, which also clears the inferred flag: it
    is his word for it now, not the clock's guess.

    In-session items are skipped either way - fuel is not a meal."""
    meal = NLU.normalise_meal(decision.get("meal"))
    if not meal:
        log(f"  meal correction with an unusable meal: {decision.get('meal')!r}")
        return False
    if pend:
        batch = pend.get("batch") or []
        touched = [i for i in batch if not i.get("_supplement")
                   and not i.get("in_session")]
        if not touched:
            return False
        for item in touched:
            item["_meal"] = meal
        set_pending(ctx.store, dict(pend, batch=batch))
        send_verified(ctx, token, chat_id,
                      f"Noted - filing {'them' if len(touched) > 1 else 'it'} under "
                      f"{meal} when you confirm.", kind="correction",
                      numbers=_gate_numbers(touched))
        return True
    if not target_item:
        return False
    # set_meal, never update_entry, even though `meal` is patchable now: the alias
    # normalisation, the validation and the clearing of `meal_inferred` live there and
    # must stay on one path.
    done = ctx.store.set_meal(day, target_item["id"], meal)
    if not done:
        return False
    record_action(ctx, f"updated entry {target_item['id']} {done.get('resolved_name')}: "
                       f"filed under {meal}")
    publish_now(ctx)
    _chat(ctx, "coach", f"[log] filed {(done.get('resolved_name') or '')[:40]} "
                        f"under {meal}")
    send_verified(ctx, token, chat_id,
                  f"Filed *{done.get('resolved_name')}* under {meal}.", kind="correction",
                  numbers=_gate_numbers([done]))
    return True


# WHOSE COPY SURVIVES A DE-DUPLICATION, best first. A label is the manufacturer's own panel;
# `manual` is what he read off a pack or typed himself; then a web figure for the actual
# product, then a composition-table match for something like it, then a model estimate. The
# order matters because the two copies of one food are usually NOT equally good - the whole
# reason the second one exists is that he was correcting the first - and binning the label
# copy to keep a lookup would undo the correction while reporting success.
DEDUP_KEEP_ORDER = ("label", "manual", "web", "cofid", "estimate")


def provenance_bucket(entry: dict) -> str:
    """Which rung of DEDUP_KEEP_ORDER this entry's figures came from."""
    rung = (entry.get("source_rung") or "").strip().lower()
    if "label" in (entry.get("source_url") or "").lower():
        return "label"                      # photographed panel, however it was rung
    if rung == "manual":
        return "manual"
    if rung in ("vendor", "retailer", "web", "nutritionix", "openfoodfacts", "usda"):
        return "web"
    if rung in ("cofid", "cache", "computed"):
        return "cofid"
    return "estimate"


def _identity_tokens(text: str) -> set:
    """The words that NAME a food, for deciding whether two entries are the same thing."""
    stop = {"the", "one", "that", "this", "my", "and", "of", "a", "an", "it", "with",
            "from", "for", "was", "had"}
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) > 2} - stop


def duplicate_sets(entries: list, which: str = "") -> list:
    """Groups of today's entries that name the same food. Only groups of two or more.

    Matched on shared identity tokens, the same vocabulary find_entry and _name_matches use,
    because a duplicate is created by logging one food twice and the two copies rarely have
    identical names - "Coop Chianti beef pizza" and "Chianti pizza, stone baked" are the
    reported pair. `which` narrows the pool to what he pointed at; with nothing named, every
    group in the day is a candidate and more than one of them ASKS rather than guessing."""
    pool = [e for e in entries
            if not which or _name_matches(which, _entry_haystack(e))]
    groups = []
    for e in pool:
        tokens = _identity_tokens(_entry_haystack(e))
        if not tokens:
            continue
        for g in groups:
            if any(tokens & _identity_tokens(_entry_haystack(m)) for m in g):
                g.append(e)
                break
        else:
            groups.append([e])
    return [g for g in groups if len(g) > 1]


def apply_delete_duplicate(ctx: Context, decision: dict, day: date, token,
                           chat_id) -> bool:
    """Remove one copy of a food that is in the log twice. ALWAYS True once called.

    THE DEFECT (15:25, 14 Aug 2026). "You've added the pizza twice" had no verb to land on,
    so it was decided as `unclear`, fell through to the re-resolution path, and the reply he
    got said the duplicate had been "noted and removed" - while both entries sat there and a
    third pizza was being offered. Nothing in the reply was implausible, which is why the
    gate passed it.

    TRUE ON EVERY BRANCH, including the ones that only ask him something. A False here falls
    back into that same re-resolution and offers him another one, which is the defect rather
    than a degraded version of the fix. Every message it sends is built from the store's own
    result: no model writes a word of the outcome."""
    which = (decision.get("which") or "").strip()
    entries = ctx.store.get_day(day).get("entries") or []
    pend = get_pending(ctx.store)
    if not entries:
        tg.send(token, chat_id,
                "Nothing is logged today, so there is nothing in there twice."
                + (" The offer on the table is not logged yet - say no and I will drop it."
                   if pend else ""), log=log)
        return True
    sets_ = duplicate_sets(entries, which)
    if not sets_:
        names = [(e.get("resolved_name") or "")[:34] for e in entries][-6:]
        tg.send(token, chat_id,
                "I cannot see the same thing logged twice today. Today has: "
                + ", ".join(names)
                + ("." if not pend else ". The item I offered you is not logged yet, so it "
                                        "is not a duplicate - say no to drop it.")
                + " Name the two you mean and I will remove one.", log=log)
        return True
    if len(sets_) > 1 or len(sets_[0]) > 2:
        # AMBIGUOUS ASKS, WITH IDS. More than one pair, or three of something, and there is
        # no way to know which copy he means - and picking one would be a silent wrong
        # deletion that reads perfectly in the reply.
        lines = []
        for g in sets_:
            lines.append("; ".join(f"{e.get('id')} {(e.get('resolved_name') or '')[:34]} "
                                   f"({round(e.get('kcal') or 0)} kcal)" for e in g))
        tg.send(token, chat_id,
                "More than one thing could be the duplicate, so I have not removed "
                "anything. I have:\n" + "\n".join(f"• {l}" for l in lines)
                + "\n\nTell me which id to remove.", log=log)
        return True
    pair = sets_[0]
    # Best provenance wins; on a tie the NEWER copy stays, because the later one is the one
    # that embodies whatever correction he was making when he logged it again.
    ranked = sorted(enumerate(pair),
                    key=lambda p: (DEDUP_KEEP_ORDER.index(provenance_bucket(p[1])), -p[0]))
    keep, drop = ranked[0][1], ranked[-1][1]
    gone = ctx.store.remove_entry(day, drop["id"])
    if not gone:
        tg.send(token, chat_id,
                "I could not remove that entry - nothing has changed. Try “delete "
                f"{(drop.get('resolved_name') or '')[:34]}”.", log=log)
        return True
    record_action(ctx, f"removed entry {gone['id']} {gone.get('resolved_name')} "
                       f"({round(gone.get('kcal') or 0)} kcal) as a duplicate")
    # In-session totals move if the duplicate was fuel, and the coach's ramp reads them.
    fuel = RC.bot_in_session_totals(ctx.store, day)
    RC.write_back(ctx.athlete_dir, day, carb_g=fuel["carb_g"],
                  sodium_mg=fuel["sodium_mg"] or None, log=log, allow_clear=True)
    publish_now(ctx)
    _chat(ctx, "coach", f"[log] removed a duplicate: {(gone.get('resolved_name') or '')[:40]}")
    left = ctx.store.get_day(day).get("entries") or []
    total = round(sum(e.get("kcal") or 0 for e in left))
    send_verified(ctx, token, chat_id,
                  f"Removed the duplicate *{gone.get('resolved_name')}* "
                  f"({round(gone.get('kcal') or 0)} kcal) and kept "
                  f"*{keep.get('resolved_name')}* ({round(keep.get('kcal') or 0)} kcal, "
                  f"{provenance_bucket(keep)} figures). Today is now {total} kcal across "
                  f"{len(left)} item{'s' if len(left) != 1 else ''}.\n\n"
                  + today_block(ctx, day),
                  kind="correction", numbers=_gate_numbers([gone, keep]))
    return True


def apply_rename(ctx: Context, decision: dict, day: date, token, chat_id) -> bool:
    """Correct an entry's NAME. True once handled, including by re-resolving instead.

    "The 160g was a pack of bbq chicken" against figures he read off that pack himself:
    the name was wrong and the numbers were his. Re-resolving would have thrown away the
    label reading and searched for a name he had just typed, which is the failure that
    made a whole class of correction unusable.

    Where the figures came from a LOOKUP rather than his own label, the name is what
    produced them, so a new name invalidates them - that falls through to re-resolution
    against the entry we have already identified, rather than the fuzzy match the generic
    correction path would make on a name that is deliberately new."""
    name = decision.get("name") or ""
    entry = entry_he_means(ctx, day, decision.get("which"), "rename", token, chat_id)
    if entry is None:
        return True
    done = ctx.store.rename_entry(day, entry["id"], name)
    if done:
        record_action(ctx, f"updated entry {entry['id']}: renamed to {name[:50]}")
        publish_now(ctx)
        _chat(ctx, "coach", f"[log] renamed {(done.get('renamed_from') or '')[:34]} "
                            f"to {name[:34]}")
        send_verified(ctx, token, chat_id,
                      f"Renamed it to *{name}*. Kept your figures - "
                      f"{round(done.get('kcal') or 0)} kcal"
                      + (f", {done['portion_used_g']:.0f} g"
                         if done.get("portion_used_g") else "")
                      + " - since they came off the pack, not a lookup.",
                      kind="correction", numbers=_gate_numbers([done]))
        return True
    grams = entry.get("portion_used_g") or entry.get("portion_g")
    tg.send(token, chat_id,
            f"Those figures came from a lookup rather than your own label, so a new name "
            f"means the lookup was wrong. Looking *{name}* up instead.", log=log)
    offer_items(ctx, [{"text": name, "portion_g": grams,
                       "in_session": bool(entry.get("in_session"))}],
                day, token, chat_id)
    mark_pending_replaces(ctx, entry["id"], entry.get("resolved_name") or "")
    return True


def apply_remember(ctx: Context, decision: dict, pend, day: date, token, chat_id) -> bool:
    """Store a lasting fact about a product, and rescale the item if that is what it
    also fixes. True once handled.

    "A rego scoop is half a portion" was a fact the bot could hear and not keep, so every
    scoop of REGO cost the same conversation again. Facts are consulted by
    apply_product_facts on the resolution path, deterministically - a stored number, never
    a model guess at logging time."""
    fact = NLU.product_fact(decision)
    if not fact:
        return False
    rec = ctx.store.set_product_fact(fact["product"], fact["field"], fact["value"],
                                     note=str(decision.get("note") or "").strip())
    if not rec:
        return False
    record_action(ctx, f"remembered a product fact: {fact['product']} "
                       f"{fact['field']}={fact['value']}")
    # State exactly what was stored. A fact is permanent and consulted silently, so a
    # model wobble has to be visible on the spot rather than the next time he logs it.
    if fact["field"] == "means":
        line = (f"Noted - *{fact['product']}* means “{fact['value']}”. I will look that "
                f"up whenever you say it.")
    else:
        unit = {"scoop_g": "scoop", "portion_g": "portion", "pack_g": "pack"}[fact["field"]]
        line = (f"Noted - a *{fact['product']}* {unit} is {fact['value']:.0f} g. I will "
                f"use that from now on.")
    tg.send(token, chat_id, line, log=log)
    _chat(ctx, "coach", f"[log] remembered: {fact['product']} "
                        f"{fact['field']}={fact['value']}")
    if decision.get("kind") == "remember_and_rescale" and decision.get("grams"):
        if not apply_quantity_correction(ctx, pend, {"grams": float(decision["grams"])},
                                        day, token, chat_id):
            tg.send(token, chat_id,
                    "I have kept the fact, but there is no per-100g basis on that item "
                    "to rescale from - tell me what you had and I will redo it.", log=log)
    return True


# What his words have to say for a remembered scoop or portion weight to be used. A fact
# about a scoop is not a fact about a bar: "a SiS REGO bar" must not pick up the scoop
# weight just because REGO is the product named.
_SAYS_SCOOP = re.compile(r"\bscoops?\b", re.I)
_SAYS_PORTION = re.compile(r"\bportions?\b|\bservings?\b", re.I)


def apply_product_facts(facts: dict, items: list, said: str = "") -> list:
    """Apply what he has told us about a product BEFORE the ladder runs.

    Two things, both deterministic and both code-side: a `means` alias is rewritten into
    the lookup text, and a remembered scoop or portion weight becomes the item's
    portion_g when his words asked for a scoop or a portion of that product.

    Rewriting search_terms as well as the canonical name is the point. offer_planned
    passes `queries=it["search_terms"]` into the ladder, so an alias applied to the name
    alone would never reach a search - the same computed-here-lost-at-the-hand-off shape
    that dropped the photo hint and the species score.

    A gram amount he stated OUTRANKS a remembered weight: portion_g is only filled when
    it is still empty."""
    if not facts or not items:
        return items
    # Longest key first, so "sis rego chocolate" wins over "sis rego" when both are known.
    keys = sorted((k for k in facts if k), key=len, reverse=True)
    for it in items:
        blob = " ".join(str(it.get(f) or "") for f in ("canonical_name", "text",
                                                       "raw_text"))
        blob += " " + " ".join(str(t) for t in (it.get("search_terms") or []))
        low = f"{blob} {said}".lower()
        key = next((k for k in keys if k in low), None)
        if not key:
            continue
        rec = facts.get(key) or {}
        alias = rec.get("means")
        if alias:
            pat = re.compile(re.escape(key), re.I)
            for field in ("canonical_name", "text", "raw_text"):
                if it.get(field):
                    it[field] = pat.sub(str(alias), it[field])
            if it.get("search_terms"):
                it["search_terms"] = [pat.sub(str(alias), str(t))
                                      for t in it["search_terms"]]
            log(f"  product fact: {key!r} means {alias!r}")
        if it.get("portion_g") in (None, ""):
            words = f"{blob} {said}"
            grams = None
            if rec.get("scoop_g") and _SAYS_SCOOP.search(words):
                grams = float(rec["scoop_g"])
            elif rec.get("portion_g") and _SAYS_PORTION.search(words):
                grams = float(rec["portion_g"])
            if grams:
                # "2 scoops" is two of them. count comes from the interpretation, so a
                # bare number in the sentence cannot inflate it.
                try:
                    n = int(it.get("count") or 1)
                except (TypeError, ValueError):
                    n = 1
                it["portion_g"] = round(grams * max(1, n), 1)
                it["portion_from_fact"] = f"{it['portion_g']:.0f} g - you told me a " \
                                          f"{key} scoop/portion weighs that"
                log(f"  product fact: {key!r} portion_g={it['portion_g']}")
    return items


def record_exclusions(ctx: Context, day: date, text: str) -> list:
    """Store what he says it was not, for the rest of the day, before re-resolving."""
    found = exclusions_in(text)
    for phrase in found:
        ctx.store.add_exclusion(day, phrase)
    if found:
        log(f"  excluded for {day}: {found}")
    return found


def _has_subject(combined: str, correction: str) -> bool:
    """Is there any food left in the string, beyond the correction itself?"""
    import re as _re

    def words(x):
        return {w for w in _re.split(r"[^a-z0-9]+", (x or "").lower()) if len(w) > 2}

    stop = {"the", "had", "was", "one", "half", "portion", "bag", "actually", "just",
            "some", "more", "less", "only", "double", "extra", "that", "this", "it"}
    return bool(words(combined) - words(correction) - stop)


def pending_subject(pend: dict) -> str:
    """The raw text of what is pending, whatever shape the record is in.

    Three shapes exist and only one of them was handled: a bare item with `_raw`, a batch of
    one, and a batch of many. A batch of ONE fell through every guard and read pend["_raw"],
    which a batch does not have - so the correction was applied to an empty string and the food
    disappeared from it. Reading the subject in one place is what stops the next shape doing the
    same thing."""
    batch = pend.get("batch") or []
    if len(batch) == 1:
        it = batch[0]
        return it.get("_raw") or it.get("raw_text") or it.get("resolved_name") or ""
    return pend.get("_raw") or pend.get("raw_text") or ""


def correct_in_batch(ctx: Context, pend: dict, correction: str, day: date,
                     token, chat_id) -> bool:
    """Correct ONE item of a pending batch, keeping the rest. False if not applicable.

    The item is chosen by the words he used, and re-resolved from ITS OWN raw text - not from
    the batch record, which has no raw text of its own. Reading a batch as though it were a
    single item is what turned a Nando's order into raw chicken breast and dropped three
    other items on the floor."""
    batch = pend.get("batch") or []
    if len(batch) < 2:
        return False
    matches = [i for i, it in enumerate(batch)
               if _name_matches(correction, (it.get("resolved_name") or "")
                                + " " + (it.get("_raw") or ""))]
    blocked = [i for i, it in enumerate(batch)
               if it.get("needs_portion") or it.get("needs_input")]
    # A BLOCKING match wins. "It's one chicken breast so find the weight of that" matched both
    # the chicken butterfly and the 1/4 chicken on words alone, and taking the first would have
    # corrected the wrong dish - while the item actually waiting on him was the other one. What
    # he is answering is the question the bot just asked.
    both = [i for i in matches if i in blocked]
    idx = (both[0] if len(both) == 1
           else matches[0] if len(matches) == 1
           else None)
    if idx is None:
        # Nothing named, or an ambiguous name: fall back to the single blocking item.
        if len(blocked) != 1:
            names = ", ".join((it.get("resolved_name") or "?")[:30] for it in batch)
            tg.send(token, chat_id,
                    "Which one do you mean? I have " + names
                    + ". Name it and I will redo just that one.", log=log)
            return True
        idx = blocked[0]

    target = batch[idx]
    combined = NLU.apply_correction(target.get("_raw") or target.get("raw_text") or "",
                                    correction)
    log(f"  correcting batch item {idx}: {combined[:70]!r}")
    hint = {"canonical_name": combined, "search_terms": [combined],
            "expect_macros": True}
    fetchers = dict(ctx.fetchers)
    deep = make_deep_fetch(log=log)
    fetchers[NR.Rung.WEB] = lambda q, pg, _h=hint, _d=deep: _d(q, pg, hint=_h)
    item = NR.resolve(combined, day=day, store=ctx.store, table=ctx.table,
                      fetchers=fetchers, cofid=ctx.cofid, hint=hint,
                      # portion_used_g FIRST. A resolved item does not carry `portion_g` -
                      # it is not in PASSTHROUGH_FIELDS, so this was always None - and with
                      # components now sized to a meal, re-resolving one by name dropped it
                      # back to a per-100g basis inside a portioned dinner. A wrong total he
                      # cannot see, which is what every other path here refuses to produce.
                      portion_g=(target.get("portion_used_g")
                                 or target.get("portion_g")),
                      exclude=_exclusions(ctx, day))
    item["_raw"] = combined
    item["in_session"] = bool(target.get("in_session"))
    item["_supplement"] = bool(target.get("_supplement"))
    item["_trivial"] = bool(target.get("_trivial"))
    item["_dose_mg"] = target.get("_dose_mg")

    fresh = list(batch)
    fresh[idx] = item
    set_pending(ctx.store, {**pend, "batch": fresh})
    body = "\n\n".join(fmt_confirm(i) for i in fresh)
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    send_verified(ctx, token, chat_id,
                  "Redone that one, the rest are unchanged.\n\n" + body
                  + "\n\nLog " + ("these?" if len(fresh) > 1 else "it?"),
                  kind="offer", numbers=_gate_numbers(fresh), reply_markup=kb)
    return True


def commit_pending(ctx: Context, pend: dict, day: date, token, chat_id) -> None:
    """Write the pending batch. Called only after an explicit confirmation."""
    # AN OFFER HE WAS NEVER SHOWN IS NOT CONFIRMABLE. The gate blocked its text, so the
    # figures below are ones it judged absurd; a bare "ok" - or the "Log it" button on an
    # older message - would otherwise write them, one tap from the defect the gate exists
    # to catch. The record itself is kept, so a correction can still land on it, and any
    # re-offer clears the mark. Exempt from the gate: a fixed refusal with no figures.
    if (pend or {}).get("_gate_blocked"):
        log(f"[gate] refused to commit a blocked offer: {pend['_gate_blocked'][:120]}")
        tg.send(token, chat_id,
                "I am not logging that one - I could not make sense of it and never "
                "showed it to you properly. Tell me what it was, or your own figures, "
                "and I will log that instead.", log=log)
        return
    # A LABEL CORRECTING AN ENTRY UPDATES IT; it does not write a second one. Handled here
    # rather than in the photo path because this is the only place a confirmation lands -
    # both the typed "yes" and the button come through here - and because the update must
    # not happen until he has confirmed it. Delete-and-relog would have been the easier
    # wiring and it is the shape update_entry exists to avoid: it loses the entry's id, its
    # place in the day and anything already corrected on it.
    label_for = (pend or {}).get("_apply_label_to") or {}
    if label_for.get("id"):
        item = (pend.get("batch") or [{}])[0]
        done = ctx.store.apply_label_to_entry(day, label_for["id"], item)
        clear_pending(ctx.store)
        if not done:
            tg.send(token, chat_id,
                    "That entry is not there any more, so there was nothing to correct. "
                    "Send the label again and I will log it as its own item.", log=log)
            return
        record_action(ctx, f"updated entry {done['id']} to the label's figures: "
                           f"{done.get('resolved_name')} {round(done.get('kcal') or 0)} kcal")
        # BOTH MARKS AT ONCE cannot happen today - offer_label_as_correction writes a fresh
        # record, so a `_replaces` from an earlier offer is gone - but this branch returns
        # before the removal below, and an orphan left by a future path would sit in the log
        # under a reply that says "One entry, not two". Cheaper to honour it here.
        orphan = ((pend.get("_replaces") or {}).get("id") or "")
        if orphan and orphan != done["id"]:
            was = ctx.store.remove_entry(day, orphan)
            if was:
                record_action(ctx, f"removed entry {was['id']} {was.get('resolved_name')}, "
                                   f"replaced by the label correction")
        publish_now(ctx)
        _chat(ctx, "coach",
              f"[log] applied the label to {(done.get('resolved_name') or '')[:40]}")
        was = round(float(label_for.get("kcal") or 0))
        send_verified(ctx, token, chat_id,
                      f"Replaced *{done.get('resolved_name')}*'s figures with the label's: "
                      f"{was} to {round(done.get('kcal') or 0)} kcal"
                      + (f", {done['portion_used_g']:.0f} g"
                         if done.get("portion_used_g") else "")
                      + ". One entry, not two.\n\n" + today_block(ctx, day),
                      kind="confirmation", numbers=_gate_numbers([done]))
        return
    batch = pend.get("batch") or [pend]
    wrote, asked = 0, []
    for item in batch:
        if item.get("needs_input"):
            asked.append(item.get("resolved_name") or item.get("_raw") or "that one")
            continue
        commit_one(ctx, item, day)
        record_action(ctx, "added "
                      + ("supplement " if item.get("_supplement") else "entry ")
                      + f"{(item.get('resolved_name') or item.get('_raw') or '')[:50]}"
                      + (f" ({round(item['kcal'])} kcal)" if item.get("kcal") else ""))
        wrote += 1
    # A confirmed replacement removes what it replaced - AFTER the new entry is written, and
    # only if one was, so a declined or failed correction never destroys the original.
    replaces = pend.get("_replaces") or {}
    if wrote and replaces.get("id"):
        gone = ctx.store.remove_entry(day, replaces["id"])
        log(f"replaced entry {replaces['id']}: removed={bool(gone)}")
        if gone:
            record_action(ctx, f"removed entry {gone['id']} {gone.get('resolved_name')}, "
                               f"replaced by what was just added")
    clear_pending(ctx.store)
    # Push the day's in-session total into session-log so the coach's g/hr ramp keeps
    # being fed. Without this, logging fuel here silently starves recent_avg_g_hr and
    # the race-fuelling prescription goes blind.
    if any(i.get("in_session") for i in batch):
        fuel = RC.bot_in_session_totals(ctx.store, day)
        res = RC.write_back(ctx.athlete_dir, day, carb_g=fuel["carb_g"],
                            sodium_mg=fuel["sodium_mg"] or None, log=log,
                            allow_clear=True)
        if not res["written"]:
            log(f"fuel write-back deferred: {res['reason']}")
    if wrote:
        publish_now(ctx)
        committed = [i for i in batch if not i.get("needs_input")]
        if len(committed) == 1:
            it = committed[0]
            name = (it.get("resolved_name") or it.get("_raw") or "that")[:50]
            kcal = it.get("kcal")
            _chat(ctx, 
                "coach", f"[log] committed: {name}"
                + (f", {round(kcal)} kcal" if kcal else ""))
        else:
            _chat(ctx, "coach", f"[log] committed: {wrote} items")
    msg = (f"Logged{'' if wrote == 1 else f' {wrote} items'}.\n\n"
           + today_block(ctx, day)) if wrote else ""
    if asked:
        msg = ((msg + "\n\n") if msg else "") + (
            "I could not find figures for " + ", ".join(asked)
            + ". Send me the pack values and I will log them as label data.")
    send_verified(ctx, token, chat_id, msg, kind="confirmation",
                  numbers=_gate_numbers([i for i in batch if not i.get("needs_input")]))


def commit_one(ctx: Context, item: dict, day: date) -> None:
    if item.get("needs_input"):
        return
    if item.get("_supplement"):
        # Supplements record a DOSE. Macros are only carried when they are meaningful:
        # a 400 mg capsule contributes nothing and a nominal 1 kcal reads as data.
        trivial = bool(item.get("_trivial"))
        ctx.store.add_supplement(
            day, nutrient=item.get("_raw") or item.get("resolved_name") or "",
            dose=item.get("_dose_mg") or item.get("portion_g"),
            unit="mg" if item.get("_dose_mg") else "g",
            # Collagen is excluded from the protein target anyway, and a supplement
            # never carries macros from a food lookup, so this is only ever a figure the
            # athlete stated himself.
            protein_g=0 if trivial else (item.get("protein_g") or 0),
            note=("dose below a nutritionally meaningful amount" if trivial
                  else "recorded as a dose; not looked up against food data"))
        return
    ctx.store.add_entry(
        day, raw_text=item.get("raw_text") or item.get("_raw") or "",
        resolved_name=item.get("resolved_name") or "",
        kcal=item.get("kcal") or 0, protein_g=item.get("protein_g") or 0,
        carb_g=item.get("carb_g") or 0, fat_g=item.get("fat_g") or 0,
        fibre_g=item.get("fibre_g") or 0,
        dietary_sodium_mg=item.get("dietary_sodium_mg") or 0,
        confidence=item.get("confidence", "estimate"),
        source_rung=item.get("source_rung", "llm"),
        source_url=item.get("source_url", ""),
        resolved_at=item.get("resolved_at"), species=item.get("species") or [],
        ingredients=item.get("ingredients") or "",
        in_session=bool(item.get("in_session")),
        # A time HE STATED wins over the moment he typed the message, and it is composed
        # from `day` - the ICU local date - rather than the server clock, which is an hour
        # off in BST. "Add the second slice of toast at 1350" was stamped at whatever time
        # the message arrived, so the app filed it under the wrong meal.
        logged_at=(f"{day.isoformat()}T{item['_at']}" if item.get("_at")
                   else datetime.now().isoformat(timespec="minutes")),
        # The meal he NAMED, or "" - and "" means the store files it by the clock and
        # says it guessed. Meals were inferred at publish time from the log timestamp
        # alone, so a breakfast written up at 13:49 read as lunch and nothing at log
        # time ever asked or read the message (Jamie, 13 Aug 2026).
        meal=item.get("_meal") or "")
    NR.cache_resolved(ctx.store, item)


def in_session_line(ctx: Context, day: date) -> str:
    """The in-run verdict, stated apart from the day's macros.

    A day carb total is an energy budget; a session is a delivery RATE. Reporting only
    the day figure let a big total hide an under-fuelled long run, which is a rate
    problem dinner cannot fix."""
    rec = RC.reconcile(ctx.store, ctx.athlete_dir, day)
    sessions = rec.get("sessions") or []
    longest = max(sessions, key=lambda x: float(x.get("duration_min") or 0), default=None)
    if not longest or float(longest.get("duration_min") or 0) < 90:
        return ""
    try:
        target = ctx.prescribed_g_hr(longest.get("sport") or "")
    except Exception as exc:
        log(f"prescribed rate unavailable: {exc}")
        return ""
    ins = NE.in_session_requirement(
        session_minutes=float(longest["duration_min"]),
        carbs_in_session_g=(rec["fuel"].get("carb_g") or 0),
        target_g_hr=target,
        alert_g_hr=ctx.athlete.get("nutrition_alert_threshold_g_hr"),
        sport=longest.get("sport") or "")
    if not ins:
        return ""
    tail = (" _Under the prescribed rate, and a rate cannot be made up later._"
            if ins["verdict"] == "under" else "")
    return (f"\n\n*In-run:* {ins['g_per_hr']:.0f} g/hr of {ins['target_g_hr']:.0f} "
            f"over {ins['session_minutes']} min"
            + (f", {ins['shortfall_g']} g short" if ins["shortfall_g"] else "") + tail)


def fmt_tomorrow(facts: dict) -> str:
    """The same facts without the model. Used when the model is down, so /tomorrow always
    answers with something true rather than an apology."""
    t = facts.get("tomorrow") or {}
    bits = ["*Tomorrow*"]
    if not t.get("sessions"):
        bits.append("Nothing on the calendar, so this is from your typical week: "
                    + str(t.get("day_type")))
    for sn in t.get("sessions") or []:
        line = " - ".join(str(x) for x in
                          (sn.get("sport"), sn.get("name") or None,
                           (f"{sn['minutes']} min" if sn.get("minutes") else None)) if x)
        bits.append(line)
        if sn.get("aim"):
            bits.append("_" + sn["aim"][:220] + "_")
    for ins in t.get("in_session_by_session") or []:
        bits.append(f"{ins['sport']} fuel: {ins['prescribed_carb_g_per_hr']:.0f} g carb/hr"
                    + (f" over {ins['minutes']} min, about {ins['carb_g']} g"
                       if ins.get("carb_g") else ""))
    for m in t.get("todays_zones_because_of_tomorrow") or []:
        bits.append("Today, because of it: " + m)
    e = facts.get("energy") or {}
    if e.get("remaining_kcal") is not None:
        bits.append(f"Left today: {e['remaining_kcal']} kcal against target.")
    return "\n".join(bits)


def today_block(ctx: Context, day: date) -> str:
    z = ctx.zones_for(day)
    # merged_totals, never store.day_totals: fuel logged in the COACH bot is otherwise
    # invisible here, and a 200 g carb ride is 800 kcal missing - enough to fire the
    # under-fuelling guard falsely. Counted exactly once either way.
    totals = RC.merged_totals(ctx.store, ctx.athlete_dir, day)
    # No stated meal plan yet, so the projection is open-ended and only an
    # already-breached ceiling can flag. That is deliberate: inventing a plausible
    # remainder would manufacture flags out of an assumption.
    proj = NE.project({k: totals.get(k) for k in
                       ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g")})
    flags = NE.zone_flags(z, proj)
    return fmt_totals(totals, z) + fmt_flags(flags) + in_session_line(ctx, day)


def handle_command(ctx: Context, cmd: str, day: date, token, chat_id) -> None:
    store = ctx.store
    if cmd.startswith("/tomorrow") or cmd.startswith("/brief"):
        tg.send(token, chat_id, "Looking at tomorrow...", log=log)
        facts = facts_for_question(ctx, day)
        brief = NLU.coach_brief(facts, CLAUDE_BIN, LLM_MODEL, log=log)
        if brief:
            send_verified(ctx, token, chat_id, brief, kind="reply")
        else:
            # Never silence. The deterministic brief is worse prose and the same numbers.
            tg.send(token, chat_id, fmt_tomorrow(facts), log=log)
        return
    if cmd.startswith("/today"):
        tg.send(token, chat_id, today_block(ctx, day), log=log)
    elif cmd.startswith("/target"):
        tg.send(token, chat_id, fmt_target(ctx.zones_for(day)), log=log)
    elif cmd.startswith("/plants"):
        days = store.get_range(day - timedelta(days=6), day)
        tg.send(token, chat_id, fmt_plants(PL.diversity(days, ctx.table, on=day)),
                log=log)
    elif cmd.startswith("/undo"):
        gone = store.undo_last(day)
        if gone:
            record_action(ctx, f"removed entry {gone['id']} {gone.get('resolved_name')}")
        tg.send(token, chat_id,
                (f"Removed {gone['resolved_name']}." if gone else "Nothing to undo."),
                log=log)
    elif cmd.startswith("/edit"):
        gone = store.undo_last(day)
        if gone:
            record_action(ctx, f"removed entry {gone['id']} {gone.get('resolved_name')} "
                               f"for a re-parse")
        tg.send(token, chat_id,
                (f"Removed {gone['resolved_name']}. Send it again and I will re-parse it."
                 if gone else "Nothing to edit."), log=log)
    elif cmd.startswith("/close"):
        z = ctx.zones_for(day)
        # The guard must see coach-logged fuel too, or a fully fuelled long ride reads
        # as under-eating.
        merged = RC.merged_totals(store, ctx.athlete_dir, day)
        entries = (store.get_day(day).get("entries") or [])
        if merged.get("in_session_from_coach"):
            entries = entries + [{"kcal": merged["in_session_kcal"], "in_session": True}]
        store.close_day(day, when=datetime.now().isoformat(timespec="minutes"))
        record_action(ctx, f"closed the day {day.isoformat()}")
        under = NE.underfuel_flag(entries, z, z["kcal_maintenance"] / NE.NEAT_TEF_MULTIPLIER)
        msg = "Day closed.\n\n" + fmt_totals(
            RC.merged_totals(store, ctx.athlete_dir, day), z)
        if under:
            store.add_flag(day, type="underfuel", severity="high", payload=under)
            msg += f"\n\n*{under['message']}* {under['shortfall_kcal']} kcal short."
        tg.send(token, chat_id, msg, log=log)
    elif cmd.startswith("/week"):
        days = store.get_range(day - timedelta(days=6), day)
        div = PL.diversity(days, ctx.table, on=day)
        mean = NE.rolling_weight_kg(store.measurements_range(day - timedelta(days=6), day),
                                    on=day)
        logged = sum(1 for d in days if d.get("entries"))
        tg.send(token, chat_id,
                f"*Last 7 days*\n{logged}/7 days logged\n"
                f"{div['unique_7d']} plants\n"
                + (f"weight 7-day mean {mean:.1f} kg" if mean else "no weights logged"),
                log=log)
    else:
        tg.send(token, chat_id, HELP, log=log)


# --- poll loop --------------------------------------------------------------

def main():
    cfg = load_config()
    token = cfg["bot_token"]
    allowed = str(cfg.get("chat_id") or "")
    tg.force_ipv4()
    ctx = Context(cfg)
    log(f"nutrition bot up for {ctx.slug}; ladder: "
        f"{NR.ladder_status(ctx.fetchers, ctx.cofid)}")

    offset = None
    while True:
        params = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            params["offset"] = offset
        res = tg.get(token, "getUpdates", params, log=log,
                     timeout=POLL_TIMEOUT + 20)
        for upd in res.get("result") or []:
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    chat_id = cq["message"]["chat"]["id"]
                    if str(chat_id) != allowed:
                        continue
                    tg.answer_callback(token, cq["id"], log=log)
                    pend = get_pending(ctx.store)
                    # The gate judges the reply against what he DID, and a tap is what he
                    # did. Left unset, Context outlives the message and the field would
                    # still hold whatever he last typed, so a commit confirmation would be
                    # checked against an unrelated sentence.
                    set_inbound(ctx, "[tapped Log it]"
                                if cq.get("data") == "confirm" else "[tapped No]")
                    if cq.get("data") == "confirm" and pend:
                        commit_pending(ctx, pend, ctx.local_today(), token, chat_id)
                    elif cq.get("data") == "cancel":
                        clear_pending(ctx.store)
                        tg.send(token, chat_id, "Dropped it.", log=log)
                    continue
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                if str(chat_id) != allowed:
                    continue
                if msg.get("photo"):
                    # Telegram sends several sizes; the last is the largest.
                    handle_photo(ctx, msg["photo"][-1]["file_id"],
                                 msg.get("caption") or "", ctx.local_today(),
                                 token, chat_id)
                    continue
                text = msg.get("text")
                if not text:
                    continue
                handle_text(ctx, text, token, chat_id)
            except Exception as exc:
                # One bad update must never take down the loop.
                log(f"update {upd.get('update_id')} failed: {type(exc).__name__}: {exc}")
                try:
                    tg.send(token, allowed, "Something went wrong logging that. "
                                            "It is in the log; nothing was saved.",
                            log=log)
                except Exception:
                    pass
        time.sleep(0.5)


if __name__ == "__main__":
    main()
