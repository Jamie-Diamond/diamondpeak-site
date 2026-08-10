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
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

import nutrition_engine as NE       # noqa: E402
import nutrition_nlu as NLU         # noqa: E402
import nutrition_reconcile as RC     # noqa: E402
import nutrition_resolve as NR      # noqa: E402
import plants as PL                 # noqa: E402
import tg                           # noqa: E402
from icu_api import IcuClient       # noqa: E402
from nutrition_store import NutritionStore  # noqa: E402

CONFIG = Path(__file__).resolve().parent / "nutrition_config.json"
ATHLETES = BASE / "config" / "athletes.json"
LOG_FILE = Path(__file__).resolve().parent / "nutrition_bot.log"

POLL_TIMEOUT = 50
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
LLM_MODEL = "claude-sonnet-5"

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
    codebase (see the coach's duplicate-notify bug), so it gets one owner: systemd."""
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
    bits = [f"*{item['resolved_name']}*", NR.describe_provenance(item)]
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
    if item.get("fibre_g"):
        bits.append(f"fibre {round(item['fibre_g'])} g")
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

    today_sessions = done or for_date(events, start)
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
    return today_type, tomorrow_type, confidence, today_sessions


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

Product: %s
Form: %s
Portion eaten: %s
"""

# Plausible energy density for real food, kcal per 100 g. Pure fat is ~900 and a
# zero-calorie drink is ~0, so anything outside this is a units or basis error rather
# than a food.
_MIN_KCAL_100G, _MAX_KCAL_100G = 0.0, 950.0
SALT_TO_SODIUM_MG = 400.0          # 1 g salt = 400 mg sodium


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
                input=DEEP_PROMPT % (text, hint.get("form") or "unknown",
                                     (f"{portion_g} g" if portion_g else "as described")),
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

    def zones_for(self, day: date) -> dict:
        rules = self.athlete.get("day_rules") or {}
        today_type, tomorrow_type, conf, sessions = classify_today_and_tomorrow(
            self.icu, day, rules)
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

        z = NE.zones(day_type=today_type, rolling_weight=weight, rmr=rmr,
                     sessions=sessions, tomorrow_type=tomorrow_type,
                     yesterday_type=yesterday_type, days_to_race=days_to_race,
                     deficit_enabled=bool(prof.get("deficit_enabled", True)),
                     rhr_guard_active=bool(guard.get("active")),
                     day_confidence=conf)
        self.store.set_targets(day, z, day_type=today_type)
        return z


# --- message handling -------------------------------------------------------

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
        "macros": {k: {"consumed": totals.get(k), "low": z[k]["low"],
                       "high": z[k]["high"], "bias": z[k]["bias"]}
                   for k in ("protein_g", "carb_g", "fat_g", "fibre_g")},
        "collagen_protein_not_counted_g": totals.get("non_counting_protein_g"),
        "dietary_sodium_mg": totals.get("dietary_sodium_mg"),
        "sodium_note": "no sweat test done, so there is no personal sodium target",
        "plants_7d": div["unique_7d"], "plants_new_today": div["new_species_today"],
        "plants_target": div["target"],
        "weight_7d_mean_kg": mean, "weight_basis_kg": z["weight_basis_kg"],
        "items_logged_today": [{"name": e.get("resolved_name"), "kcal": e.get("kcal"),
                                "confidence": e.get("confidence")} for e in entries],
        "modifiers_today": z.get("modifiers"), "warnings_today": z.get("warnings"),
    }


def handle_text(ctx: Context, text: str, token: str, chat_id) -> None:
    """Route on INTENT, before anything is resolved.

    The first cut treated every non-command message as food, so "how much protein have
    I had?" went to the resolution ladder, came back as a food item and was offered for
    logging. Intent is now decided first."""
    day = ctx.local_today()
    t = (text or "").strip()
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
        commit_pending(ctx, pend, day, token, chat_id)
        return

    if intent == "cancel":
        clear_pending(ctx.store)
        tg.send(token, chat_id, "Dropped it.", log=log)
        return

    if intent == "log_weight":
        log_weight(ctx, got["weight_kg"], day, token, chat_id)
        return

    if intent == "correction":
        # Re-parse from the combined text rather than patching the parsed result:
        # patching a misparse tends to preserve whatever else was wrong about it.
        if not pend:
            tg.send(token, chat_id, "Nothing pending to correct. Tell me what you had "
                                    "and I will look it up.", log=log)
            return
        combined = NLU.apply_correction(
            pend.get("_raw") or pend.get("raw_text") or "", got.get("correction") or t)
        offer_items(ctx, [{"text": combined, "portion_g": None, "in_session": False}],
                    day, token, chat_id)
        return

    if intent == "advice":
        debate(ctx, got, t, day, token, chat_id)
        return

    if intent == "question":
        answer = NLU.answer_question(got.get("question") or t,
                                     facts_for_question(ctx, day),
                                     CLAUDE_BIN, LLM_MODEL, log=log)
        # Falling back to the deterministic block rather than nothing: an unavailable
        # model must not mean an unanswered question.
        tg.send(token, chat_id, answer or today_block(ctx, day), log=log)
        return

    if intent in ("log_food", "log_supplement") and got.get("items"):
        # Interpret before resolving: work out what each thing IS and how to search for
        # it, then let the ladder search THAT rather than the athlete's sentence. The
        # model plans the lookup; it still never supplies a macro.
        plan = NLU.interpret(t, CLAUDE_BIN, LLM_MODEL, log=log)
        if plan and plan.get("items"):
            offer_planned(ctx, plan["items"], day, token, chat_id)
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
                    dose_mg=got.get("dose_mg"))
        return

    if intent == "smalltalk":
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
    mean = NE.rolling_weight_kg(
        ctx.store.measurements_range(day - timedelta(days=6), day), on=day)
    note = ("" if m["tag"] == "morning" else
            "\n_Second reading today, so I have tagged it as a session weigh-in and "
            "kept it out of the trend._")
    tg.send(token, chat_id,
            f"{kg:.1f} kg logged."
            + (f" 7-day morning mean {mean:.1f} kg." if mean else "") + note, log=log)


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
    for opt in (got.get("options") or [])[:5]:
        item = NR.resolve(opt, day=day, store=ctx.store, table=ctx.table,
                          fetchers=ctx.fetchers, cofid=ctx.cofid)
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
    reply = NLU.advise(text, facts, CLAUDE_BIN, LLM_MODEL, log=log)
    ctx.store.cache_put("_last_options", {"options": [o["option"] for o in options],
                                          "day": day.isoformat()})
    tg.send(token, chat_id, reply or ("I could not reach the model to talk it through. "
                                      "Here is where you are:\n\n"
                                      + today_block(ctx, day)), log=log)


def handle_photo(ctx: Context, file_id: str, caption: str, day: date, token,
                 chat_id) -> None:
    """A photo is a barcode, a nutrition label, or a plate. Each takes a different path
    and lands at a DIFFERENT confidence, which is the point.

    barcode        -> database lookup, database confidence
    nutrition label-> the manufacturer's own printed figures, LABEL confidence
    plate          -> items identified by vision, macros still looked up per item

    A photo of a plate is an estimate and a photo of the printed panel is label data.
    Conflating them would put label-grade confidence on a guess."""
    tg.send(token, chat_id, "Looking at that...", log=log)
    path = download_photo(ctx, file_id, token)
    if not path:
        tg.send(token, chat_id, "I could not download that image.", log=log)
        return
    got = NLU.read_photo(str(path), CLAUDE_BIN, LLM_MODEL, log=log)
    kind = got.get("kind")
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
        set_pending(ctx.store, {"batch": [item]})
        extra = ("\n_Sodium came from the salt figure on the pack, divided by 2.5._"
                 if got.get("sodium_from_salt") else "")
        kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
        tg.send(token, chat_id, fmt_confirm(item) + extra + "\n\nLog it?",
                reply_markup=kb, log=log)
        return

    if kind == "order":
        # Expand quantities into separate items to log, but compare UNITS against the
        # stated count: 1 bowl + 1 edamame + 3x soy sauce is 5 items on 3 lines.
        expanded = []
        for it in got["items"]:
            expanded.extend([dict(it)] * max(1, int(it.get("qty") or 1)))
        got["items"] = expanded
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
        offer_items(ctx, got["items"], day, token, chat_id)
        return

    tg.send(token, chat_id,
            "I could not read that. A barcode or the nutrition panel works best; a "
            "plate is fine too, just tell me roughly what is on it.", log=log)


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


def offer_planned(ctx: Context, planned: list, day: date, token, chat_id) -> None:
    """Resolve each INTERPRETED item, with its form and search terms.

    The interpretation is what makes the ladder honest: it searches good queries, and it
    can throw out a hit whose form is wrong rather than accepting anything whose name
    happens to share a word. A capsule and a protein bar share every meaningful token."""
    batch, notes = [], []
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
                "in_session": it["in_session"],
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
                          cofid=ctx.cofid, hint=it, queries=it["search_terms"])
        item["_raw"] = name
        item["in_session"] = it["in_session"]
        item["_supplement"] = False
        item["_trivial"] = False
        item["_dose_mg"] = None
        log(f"    -> {item.get('resolved_name')!r} {item.get('source_rung')}/"
            f"{item.get('confidence')} {item.get('kcal')} kcal")
        batch.append(item)
        notes.append(fmt_confirm(item))
    set_pending(ctx.store, {"batch": batch})
    if any(i.get("in_session") for i in batch):
        notes.append("_Tagged as in-session fuel, so it is protected from any trimming._")
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    tg.send(token, chat_id, "\n\n".join(notes) + "\n\nLog "
            + ("these?" if len(batch) > 1 else "it?"), reply_markup=kb, log=log)


def offer_items(ctx: Context, items: list, day: date, token, chat_id,
                supplement: bool = False, barcode: str = None,
                trivial: bool = False, dose_mg: float = None) -> None:
    """Resolve each item separately and ask once for the batch.

    Per-item resolution matters: a whole sentence resolved as one string both
    mis-costs it and loses the per-item provenance the confidence flag depends on."""
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
                **{f: None for f in NR.MACRO_FIELDS},
            })
        set_pending(ctx.store, {"batch": batch})
        dose = (f"{dose_mg:.0f} mg" if dose_mg
                else (f"{items[0].get('portion_g')} g" if items[0].get("portion_g")
                      else "dose as stated"))
        kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
        tg.send(token, chat_id,
                f"*{items[0]['text']}*\nSupplement, {dose}. Recorded as a dose, not "
                f"looked up against food data, and it does not touch your macros."
                + ("\n_Tell me the label figures if you want them counted._"
                   if not trivial else "")
                + "\n\nLog it?", reply_markup=kb, log=log)
        return

    resolved = []
    for it in items[:8]:
        log(f"  resolving {it['text'][:60]!r} portion={it.get('portion_g')}")
        # A barcode short-circuits the text ladder: an exact product lookup beats any
        # name search, so it is tried before the ordinary rungs rather than as one.
        fetchers = dict(ctx.fetchers)
        if barcode:
            fetchers[NR.Rung.RETAILER] = (
                lambda t, p, _c=barcode: NR.off_barcode_fetch(_c, p))
        item = NR.resolve(it["text"], day=day, store=ctx.store, table=ctx.table,
                          portion_g=it.get("portion_g"), fetchers=fetchers,
                          cofid=ctx.cofid)
        item["_raw"] = it["text"]
        item["in_session"] = bool(it.get("in_session"))
        item["_supplement"] = supplement
        item["_trivial"] = bool(trivial)
        item["_dose_mg"] = dose_mg
        resolved.append(item)
    set_pending(ctx.store, {"batch": resolved})
    body = "\n\n".join(fmt_confirm(i) for i in resolved)
    if any(i.get("in_session") for i in resolved):
        body += "\n\n_Tagged as in-session fuel, so it is protected from any trimming._"
    kb = tg.inline([[("Log it", "confirm"), ("No", "cancel")]])
    tg.send(token, chat_id, body + "\n\nLog "
            + ("these?" if len(resolved) > 1 else "it?"), reply_markup=kb, log=log)


def commit_pending(ctx: Context, pend: dict, day: date, token, chat_id) -> None:
    """Write the pending batch. Called only after an explicit confirmation."""
    batch = pend.get("batch") or [pend]
    wrote, asked = 0, []
    for item in batch:
        if item.get("needs_input"):
            asked.append(item.get("resolved_name") or item.get("_raw") or "that one")
            continue
        commit_one(ctx, item, day)
        wrote += 1
    clear_pending(ctx.store)
    # Push the day's in-session total into session-log so the coach's g/hr ramp keeps
    # being fed. Without this, logging fuel here silently starves recent_avg_g_hr and
    # the race-fuelling prescription goes blind.
    if any(i.get("in_session") for i in batch):
        fuel = RC.bot_in_session_totals(ctx.store, day)
        res = RC.write_back(ctx.athlete_dir, day, carb_g=fuel["carb_g"],
                            sodium_mg=fuel["sodium_mg"] or None, log=log)
        if not res["written"]:
            log(f"fuel write-back deferred: {res['reason']}")
    msg = (f"Logged{'' if wrote == 1 else f' {wrote} items'}.\n\n"
           + today_block(ctx, day)) if wrote else ""
    if asked:
        msg = ((msg + "\n\n") if msg else "") + (
            "I could not find figures for " + ", ".join(asked)
            + ". Send me the pack values and I will log them as label data.")
    tg.send(token, chat_id, msg, log=log)


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
        logged_at=datetime.now().isoformat(timespec="minutes"))
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
        tg.send(token, chat_id,
                (f"Removed {gone['resolved_name']}." if gone else "Nothing to undo."),
                log=log)
    elif cmd.startswith("/edit"):
        gone = store.undo_last(day)
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
        res = tg.get(token, "getUpdates", params, log=log)
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
