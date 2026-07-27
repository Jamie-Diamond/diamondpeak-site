#!/usr/bin/env python3
"""
Race registry — the structured list of races per athlete, and the date branching that
turns it into a race-week / race-eve / race-day / race-completed code path.

Why this exists. `race_date` was structured data in three places (config/athletes.json,
athletes/<slug>/profile.json, the training blueprint) and every consumer treated it as a
planning input or a display string: the taper maths, the dashboard projection, the
on-demand race plan, and the "N days to <race>" countdown at the foot of the morning
card. NOTHING branched on it, so on race morning the card rendered the same template
with a 0 in the countdown, and nothing at all noticed a race had been completed. A
secondary race existed only as prose — Jamie's Dorney tri lived in current-state.md and
a training-plan markdown, never as data. See docs/tone-of-voice-guide.md §8.6/§8.7.

Design notes:

* The registry lives in `config/athletes.json` under a `races` key, because that file is
  what the bot and every scheduled script already read for the countdown
  (telegram/bot.py:310, :954, :1012). A new store would need every one of those callers
  taught about it.
* `race_date` / `race_name` stay exactly where they are and keep working. They are DERIVED
  from the A-race (`sync_legacy_fields`), so the many existing consumers are untouched.
  The A-race is the single race an athlete's plan is built around, which is what those
  fields have always meant.
* `priority` is A / B / C, and is NULLABLE by design. An athlete's A-race is a judgement,
  not an inference: where the evidence does not state a priority the field is left None
  and surfaced for the athlete to fill in, rather than guessed. The conversational capture
  path relies on the same property — it ASKS for a priority it was not given.
* Everything here that decides "what kind of day is it" takes `today` as an argument and
  reads no clock of its own, so a race-day path is testable for a pinned date without
  touching a scheduler.
"""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent          # ClaudeCoach/lib/
_BASE    = _LIB_DIR.parent                          # ClaudeCoach/
ATHLETES_CONFIG = _BASE / "config/athletes.json"

PRIORITIES = ("A", "B", "C")
STATUSES   = ("upcoming", "completed")

# Race-week starts this many days out. Seven gives the athlete the whole final week; the
# eve and day branches take precedence inside it (see `race_phase`).
RACE_WEEK_DAYS = 7
# A completed race stays "just raced" for this many days, so the morning-after card can
# lead on the result. Two days covers a Sunday race read on the Tuesday card.
POST_RACE_DAYS = 2


def _as_date(v):
    """A date from a date, a datetime or an ISO 'YYYY-MM-DD' string; None if unparseable."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def normalise(raw: dict) -> dict:
    """One race in canonical shape. Unknown priority stays None rather than being
    defaulted — a wrong A/B/C is worse than an absent one."""
    pri = (raw.get("priority") or "").strip().upper() or None
    if pri not in PRIORITIES:
        pri = None
    status = (raw.get("status") or "").strip().lower()
    if status not in STATUSES:
        status = None
    d = _as_date(raw.get("date"))
    out = {
        "name":     (raw.get("name") or "").strip(),
        "date":     d.isoformat() if d else None,
        "priority": pri,
        "distance": (raw.get("distance") or "").strip() or None,
        # Status is derived from the date when it was not stated: a race in the past has
        # happened. Explicit status still wins, so a DNS/withdrawn race can be recorded.
        "status":   status or _implied_status(d),
    }
    for k in ("notes", "source", "icu_event_id"):
        if raw.get(k):
            out[k] = raw[k]
    # Set once the post-race message has actually been sent, so it fires ONCE. Without it
    # the race_completed branch re-fires for every day inside POST_RACE_DAYS and the
    # athlete gets asked how a race went two mornings running.
    if raw.get("post_race_sent"):
        out["post_race_sent"] = True
    return out


def _implied_status(d, today=None) -> str:
    if d is None:
        return "upcoming"
    return "completed" if d < (today or date.today()) else "upcoming"


def _load_config(path=None) -> dict:
    return json.loads(Path(path or ATHLETES_CONFIG).read_text())


def load_races(slug: str, config: dict = None, path=None) -> list:
    """Every known race for `slug`, earliest first.

    Falls back to synthesising a single race from the legacy `race_date`/`race_name` pair
    when no registry is present, so an athlete configured before the registry existed —
    or one just created by the onboarding flow — still gets a race-aware code path."""
    cfg = config if config is not None else _load_config(path)
    a = cfg.get(slug) or {}
    races = [normalise(r) for r in (a.get("races") or []) if (r.get("name") or r.get("date"))]
    if not races and a.get("race_date"):
        races = [normalise({"name": a.get("race_name") or "race",
                            "date": a["race_date"], "priority": "A",
                            "source": "legacy race_date"})]
    return sorted(races, key=lambda r: r["date"] or "9999-12-31")


def a_race(races: list) -> dict:
    """The A-race, or None. Where several are marked A (a genuine possibility across
    seasons) the earliest UPCOMING one wins, because that is the one being trained for."""
    a = [r for r in races if r["priority"] == "A"]
    upcoming = [r for r in a if r["status"] == "upcoming"]
    return (upcoming or a or [None])[0]


def next_race(races: list, today=None) -> dict:
    """The soonest race on or after `today` that has not been marked completed."""
    t = _as_date(today) or date.today()
    future = [r for r in races
              if r["date"] and _as_date(r["date"]) >= t and r["status"] != "completed"]
    return future[0] if future else None


def days_to(race: dict, today=None):
    """Whole days from `today` to the race; None if the race has no date."""
    d = _as_date((race or {}).get("date"))
    if not d:
        return None
    return (d - (_as_date(today) or date.today())).days


def race_phase(races: list, today=None) -> dict:
    """What kind of race day is `today`? The single branch point the whole system was
    missing.

    Returns {"phase", "race", "days_to"}. `phase` is one of:
      'race_day'       — the race is today
      'race_eve'       — the race is tomorrow (the night-before surface)
      'race_completed' — a race fell within the last POST_RACE_DAYS days
      'race_week'      — a race is within RACE_WEEK_DAYS but not eve or day
      None             — an ordinary day; every existing template applies unchanged

    Precedence is deliberate and ordered soonest-first: race day beats race week, and a
    race TOMORROW beats a race completed YESTERDAY (back-to-back race weekends are real,
    and the pre-race message matters more than a second debrief). `phase` None means
    callers fall through to their existing behaviour, which is what keeps this additive.
    """
    t = _as_date(today) or date.today()
    dated = [r for r in races if r["date"]]

    for r in dated:
        if days_to(r, t) == 0:
            return {"phase": "race_day", "race": r, "days_to": 0}
    for r in dated:
        if days_to(r, t) == 1:
            return {"phase": "race_eve", "race": r, "days_to": 1}
    # Most recently finished first, so the freshest result is the one acknowledged. A race
    # already acknowledged is skipped — the post-race message is a one-off, not a daily
    # state the athlete sits in for POST_RACE_DAYS.
    for r in sorted(dated, key=lambda x: x["date"], reverse=True):
        n = days_to(r, t)
        if -POST_RACE_DAYS <= n < 0 and not r.get("post_race_sent"):
            return {"phase": "race_completed", "race": r, "days_to": n}
    for r in dated:
        n = days_to(r, t)
        if n is not None and 1 < n <= RACE_WEEK_DAYS and r["status"] != "completed":
            return {"phase": "race_week", "race": r, "days_to": n}
    return {"phase": None, "race": None, "days_to": None}


# -- Writing -------------------------------------------------------------------

def sync_legacy_fields(entry: dict) -> dict:
    """Point the legacy `race_date`/`race_name` at the A-race, in place.

    This is what lets the registry land without touching the many consumers that read
    those two keys (the countdown, the taper maths, the blueprint, the dashboard). If
    there is no A-race the existing values are LEFT ALONE — clearing them would break
    those consumers, which is the opposite of the point.

    `race_name` is only rewritten when it is EMPTY or when the A-race date has actually
    moved. The registry's spelling of a name is often tidier than the configured one
    ("Tour de stations, marmottes" -> "Tour de Stations / Marmottes"), and quietly
    restyling it would be a live behaviour change for no gain: bot.py:329 builds a regex
    from `race_name` to strip the countdown line back out of a message, so the two copies
    have to agree. When the A-race genuinely changes, the name must follow the date."""
    races = [normalise(r) for r in (entry.get("races") or [])]
    a = a_race(sorted(races, key=lambda r: r["date"] or "9999-12-31"))
    if a and a["date"]:
        moved = entry.get("race_date") != a["date"]
        entry["race_date"] = a["date"]
        if a["name"] and (moved or not entry.get("race_name")):
            entry["race_name"] = a["name"]
    return entry


def add_race(slug: str, name: str, race_date, priority=None, distance=None,
             status=None, notes=None, source=None, path=None) -> dict:
    """Record a race for `slug`. Returns the stored race.

    Refuses rather than guesses: a missing name or an unparseable date raises, and a
    priority that is not A/B/C is stored as None (unknown) instead of being coerced. The
    caller is expected to have ASKED. An existing race with the same date is UPDATED in
    place rather than duplicated, so a confirmed priority can be filled in later."""
    d = _as_date(race_date)
    if not (name or "").strip():
        raise ValueError("a race needs a name")
    if d is None:
        raise ValueError(f"could not read a date from {race_date!r}")
    p = Path(path or ATHLETES_CONFIG)
    cfg = json.loads(p.read_text())
    if slug not in cfg:
        raise KeyError(f"unknown athlete {slug!r}")
    entry = cfg[slug]
    races = [normalise(r) for r in (entry.get("races") or [])]
    race = normalise({"name": name, "date": d, "priority": priority,
                      "distance": distance, "status": status,
                      "notes": notes, "source": source})
    existing = next((i for i, r in enumerate(races) if r["date"] == race["date"]), None)
    if existing is None:
        races.append(race)
    else:
        merged = dict(races[existing])
        merged.update({k: v for k, v in race.items() if v is not None})
        races[existing] = normalise(merged)
        race = races[existing]
    entry["races"] = sorted(races, key=lambda r: r["date"] or "9999-12-31")
    sync_legacy_fields(entry)
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    return race


def mark_post_race_sent(slug: str, race_date, path=None) -> bool:
    """Record that the post-race message went out, so it never fires twice. Returns True
    if a race was marked. Called only AFTER a successful send — a failed send should be
    retried on the next run, which is why this is not set optimistically."""
    d = _as_date(race_date)
    p = Path(path or ATHLETES_CONFIG)
    cfg = json.loads(p.read_text())
    entry = cfg.get(slug) or {}
    hit = False
    for r in entry.get("races") or []:
        if _as_date(r.get("date")) == d:
            r["post_race_sent"] = True
            hit = True
    if hit:
        p.write_text(json.dumps(cfg, indent=2) + "\n")
    return hit


# -- Parsing free text ---------------------------------------------------------

_PRIORITY_RE = re.compile(r"\b(?:as\s+(?:an?\s+)?)?([abc])[\s-]*(?:race|priority)\b", re.I)
_ISO_RE      = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_WEEKDAYS    = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS      = ["january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december"]
# Months match on their first three letters plus any trailing letters, so "Sep", "Sept"
# and "September" all work. A fixed "september|sep" alternation looks equivalent but fails
# on "Sept": "sep" matches, the pattern then wants whitespace and finds a "t". The month
# still has to BE a month — these regexes also strip the date phrase out of a race name in
# `parse_race_message`, and a generic word pattern would eat part of the name.
_MONTH_ALT  = "(?:" + "|".join(m[:3] for m in _MONTHS) + r")[a-z]*\.?"
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_ALT + r")\b", re.I)
_MONTH_DAY_RE = re.compile(r"\b(" + _MONTH_ALT + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_RACING_RE = re.compile(
    r"\b(?:i'?m\s+)?(?:racing|i\s+race|doing|entered(?:\s+for)?|signed\s+up\s+for)\s+(.+)",
    re.I)


def resolve_date(text: str, today=None):
    """An absolute date from free text: ISO, '26 July', 'July 26', 'Saturday',
    'this/next Saturday', 'tomorrow'. Returns None if nothing datelike is present.

    A bare weekday resolves FORWARD to the next such day (today counts as 0 days away
    only for 'today'), which is what "I'm racing on Saturday" means. The caller must
    echo the resolved absolute date back to the athlete rather than storing it silently —
    a misread weekday is otherwise invisible."""
    t = _as_date(today) or date.today()
    low = text.lower()

    m = _ISO_RE.search(text)
    if m:
        return _as_date(m.group(1))

    for rx, order in ((_DAY_MONTH_RE, "dm"), (_MONTH_DAY_RE, "md")):
        m = rx.search(text)
        if m:
            day_s, mon_s = (m.group(1), m.group(2)) if order == "dm" else (m.group(2), m.group(1))
            mon = next((i + 1 for i, name in enumerate(_MONTHS)
                        if name.startswith(mon_s.lower()[:3])), None)
            if not mon:
                continue
            year_m = re.search(r"\b(20\d{2})\b", text)
            try:
                if year_m:
                    return date(int(year_m.group(1)), mon, int(day_s))
                cand = date(t.year, mon, int(day_s))
            except ValueError:
                continue
            # No year given: assume the next occurrence. A date a little in the past is
            # still meant as this year (an athlete reporting a race they just did), so
            # only roll forward once it is clearly historic.
            if (t - cand).days > 180:
                try:
                    cand = date(t.year + 1, mon, int(day_s))
                except ValueError:
                    pass
            return cand

    if "tomorrow" in low:
        return t + timedelta(days=1)
    if "today" in low:
        return t
    for i, wd in enumerate(_WEEKDAYS):
        if re.search(r"\b" + wd + r"\b", low):
            ahead = (i - t.weekday()) % 7
            if ahead == 0:
                ahead = 7                       # "on Saturday" said ON Saturday means next
            if re.search(r"\bnext\s+" + wd + r"\b", low):
                ahead += 7
            return t + timedelta(days=ahead)
    return None


# Detection is deliberately STRICTER than extraction. `_RACING_RE` includes loose verbs
# ("doing") because once we know a message is about a race those help pull the name out;
# but a detector that fired on "doing" would hijack "I'm doing my long run tomorrow" and
# try to record a race. So the trigger needs a racing-specific verb AND a resolvable date.
_RACE_TRIGGER_RE = re.compile(
    r"\b(?:racing|i\s+race|race(?:ing)?\s+is|entered\s+(?:for\s+)?|signed\s+up\s+for|"
    r"my\s+(?:next\s+)?race)\b", re.I)


def looks_like_race_statement(text: str, today=None) -> bool:
    """True only for a message that is plainly telling us about a race AND names a date.
    Both halves matter: without the date there is nothing to record, and without the verb
    an ordinary training message gets treated as a race announcement.

    A QUESTION is never a statement of fact. "Am I racing on Saturday?" clears both the
    verb and the date test, and the name-stripping below then reduces it to a bare "?" —
    which is how the athlete ends up being asked to assign a priority to a race called
    "?". Asking about a race is not announcing one, so a trailing question mark is a
    hard no."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return False
    if not _RACE_TRIGGER_RE.search(t):
        return False
    return resolve_date(t, today) is not None


def parse_race_message(text: str, today=None) -> dict:
    """Pull a race out of a chat message.

    Returns {"name", "date", "priority", "missing": [...]}. `missing` is the whole point:
    it lists the fields the message did not state, so the caller can ASK instead of
    writing a race that is part invention. Priority is almost always missing — an athlete
    saying "I'm racing Dorney on Saturday" has not told us how much it matters."""
    out = {"name": None, "date": None, "priority": None, "missing": []}
    if not (text or "").strip():
        out["missing"] = ["name", "date", "priority"]
        return out

    pm = _PRIORITY_RE.search(text)
    if pm:
        out["priority"] = pm.group(1).upper()

    d = resolve_date(text, today)
    out["date"] = d.isoformat() if d else None

    m = _RACING_RE.search(text)
    name = m.group(1) if m else text
    # Strip the date phrase, the priority phrase and the joining preposition, leaving the
    # race name. Done on the matched tail rather than the whole message so a leading
    # "I'm racing" verb never lands in the name.
    name = _ISO_RE.sub("", name)
    name = _DAY_MONTH_RE.sub("", name)
    name = _MONTH_DAY_RE.sub("", name)
    name = _PRIORITY_RE.sub("", name)
    name = re.sub(r"\b(on|this|next|tomorrow|today|as|a|an|the)\b", " ", name, flags=re.I)
    for wd in _WEEKDAYS:
        name = re.sub(r"\b" + wd + r"\b", " ", name, flags=re.I)
    name = re.sub(r"[,\.]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -–—")
    # A "name" with no letters in it is punctuation left over from stripping, not a race.
    # Without this, "Am I racing on Saturday?" yields the name "?" — the strip chain above
    # removes the words but not the question mark — and a race called "?" gets written.
    out["name"] = name if re.search(r"[A-Za-z]{2}", name or "") else None

    for k in ("name", "date", "priority"):
        if not out[k]:
            out["missing"].append(k)
    return out


# -- intervals.icu ------------------------------------------------------------

def icu_race_events(client, start: str, end: str) -> list:
    """RACE-category calendar events as registry-shaped races.

    intervals.icu tags events WORKOUT / RACE / NOTE / HOLIDAY (lib/icu_api.py:116) and
    every consumer filtered to WORKOUT, so the one field that marks a race was discarded
    system-wide. This reads the other side of that filter. Priority is NOT inferred from
    an ICU event — the API has no such field, so it comes back None for the athlete to
    confirm."""
    try:
        evs = client.get_events(start, end) or []
    except Exception:
        return []
    out = []
    for e in evs:
        if (e.get("category") or "").upper() != "RACE":
            continue
        out.append(normalise({
            "name": e.get("name") or "race",
            "date": (e.get("start_date_local") or e.get("date") or "")[:10],
            "distance": e.get("type") or None,
            "source": "intervals.icu",
            "icu_event_id": e.get("id"),
        }))
    return out


# -- Deriving the pre-race focus points ---------------------------------------
# §8.6 allows at most three things to focus on, and "every one must be something already
# trained and already agreed". Jamie asked for these to be worked out from his training and
# nutrition history rather than curated by hand.
#
# HOW THIS CANNOT INVENT A NUMBER, in three layers:
#
# 1. Nothing is generated at runtime. The focus lines are a FIXED CATALOGUE of pre-authored
#    sentences. The only decision made at run time is which of them the athlete's own data
#    supports. There is no Claude call on this path — a generative step at 20:30 on race eve
#    is precisely the failure §8.6 warns about, and selection from a fixed set gives the same
#    relevance with none of the exposure.
# 2. Every line is QUALITATIVE and contains no figures at all. §8.6 also says "no numbers
#    unless the athlete asks for them", so the right reading of "derive it from the fuelling
#    rate" is that the rate decides WHICH reminder is relevant, not that the rate gets
#    printed. A measured g/hr is a training fact, not a race-eve instruction.
# 3. `_FOCUS_DIGIT_RE` re-checks every selected line and DROPS any that contains a digit,
#    recording it as suppressed. Layer 3 is redundant while layer 1 holds, which is the
#    point: it is what stops a future edit to the catalogue from quietly reintroducing a
#    figure. It is asserted directly in the tests.
#
# Each candidate carries its provenance, and suppressed candidates are returned WITH the
# reason, so "why did this point appear" is answerable without re-deriving it by hand.

_FOCUS_DIGIT_RE = re.compile(r"\d")

# (id, rank, text, why-template). Lower rank = offered first. Ranking is by what the
# athlete's own history says the thing has COST them, not by topic tidiness.
FOCUS_CATALOGUE = [
    ("pace_first_half", 10,
     "hold the first stretch back — last time the time went late, not early",
     "past race notes record a late fade"),
    ("run_patience", 20,
     "start the run slower than it feels you should",
     "past race notes record the run coming apart in the back half"),
    ("fuel_front_load", 30,
     "first feed early, then something regularly from the start — not saved for the climbs",
     "a standing rule of the athlete's own sets front-loaded fuelling as the agreed fix"),
    ("fuel_take_it_on", 35,
     "take fuel on steadily from the start — it is what your rides have been short of",
     "a standing rule asks for race-nutrition risk to be flagged, and logged rides run low"),
    ("run_fuel", 40,
     "keep taking carbs on the run, not only on the bike",
     "a standing rule names run fuelling as the open focus"),
    ("caffeine_spread", 50,
     "spread the caffeine rather than stacking it before the swim",
     "a standing rule set after the last race asks for spread dosing"),
    ("electrolyte_every_bottle", 55,
     "a tab in every bottle, not just some",
     "a standing rule set after a confirmed cramping episode"),
    ("hr_anchor", 60,
     "let the power be whatever it is at your race heart rate",
     "a standing rule anchors race pacing to heart rate rather than a power figure"),
    ("transitions", 70,
     "transitions calm, in the order you have drilled",
     "past race transitions ran slower than the agreed plan"),
    ("ride_to_feel", 80,
     "ride to feel, not to the numbers",
     "a standing rule makes ride-to-feel the default for this event"),
]


def _mmss_seconds(v):
    """Seconds from a loose time string: 'm:ss' ('~10:00', '≤5:30') or bare minutes
    ('~5 min'). None if it does not look like either.

    Both forms are needed because the two fields being compared are written differently by
    hand: one athlete's target transition time is "≤5:30" and another's is "~5 min". Reading
    only the colon form silently returned None for the second and dropped a valid focus
    point. Used only to COMPARE a past race with the agreed target — never rendered."""
    if not isinstance(v, str):
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", v)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d{1,3})\s*(?:min|mins|minutes)\b", v, re.I)
    if m:
        return int(m.group(1)) * 60
    return None


def derive_focus(profile: dict, rules_text: str = "", avg_g_hr=None,
                 race_target_g_hr=None) -> tuple:
    """Up to three already-practised things to focus on, derived from the athlete's own
    history and standing rules. Returns (selected, suppressed).

    PURE: takes data in and reads nothing from disk, imports nothing outside the stdlib.
    The caller supplies `avg_g_hr` (from primitives.nutrition.recent_avg_g_hr) because
    importing that here would make `lib/races.py` unimportable without the
    ironman-analysis path on sys.path.

    `selected` is [{"id", "text", "why"}], ranked. `suppressed` is [{"id", "reason"}] and
    exists so the decision is auditable — a point NOT offered is as interesting as one that
    is, particularly when a standing rule is what silenced it."""
    prof = profile or {}
    rules = (rules_text or "").lower()
    prev = prof.get("prev_race") or {}
    prev_notes = (prev.get("notes") or "").lower()
    targets = prof.get("race_targets") or {}
    bike_how = (targets.get("bike_how") or "").lower()

    def has(*needles):
        return any(n in rules for n in needles)

    fired, suppressed = {}, []

    # A standing rule may forbid the very comparison a candidate rests on. Jamie's fuelling
    # rule is the live case: the 90 g/hr race target is explicitly "NOT a training minimum -
    # do not flag or compare easy/Z2 nutrition to it", and his bike capacity is separately
    # recorded as proven. So an under-fuelling nudge built from his Z2 training average
    # would be both rule-breaking and wrong on the merits.
    fuel_compare_blocked = has("not a training minimum", "do not flag or compare")

    # -- pacing / execution, from the PAST RACE's own notes -----------------------------
    if any(w in prev_notes for w in ("fade", "decoupl")) or "first 90 min" in bike_how:
        fired["pace_first_half"] = "prev_race.notes / race_targets.bike_how"
    if any(w in prev_notes for w in ("walk-break", "walk break", "walk breaks")):
        fired["run_patience"] = "prev_race.notes"

    # -- fuelling ----------------------------------------------------------------------
    if has("front-load carbs", "front-load the carbs", "front-loading"):
        fired["fuel_front_load"] = "persistent-rules.md (front-loaded fuelling rule)"
    if has("flag race-day nutrition risk"):
        if fuel_compare_blocked:
            suppressed.append({"id": "fuel_take_it_on",
                               "reason": "a standing rule forbids comparing training "
                                         "nutrition to the race target"})
        elif avg_g_hr is not None and race_target_g_hr and avg_g_hr < float(race_target_g_hr) * 0.85:
            fired["fuel_take_it_on"] = ("persistent-rules.md (flag nutrition risk) + "
                                        "logged fuelling below the race rate")
    if has("open focus is run fuelling", "the open focus is run"):
        fired["run_fuel"] = "persistent-rules.md (run fuelling is the open focus)"

    # -- habits the athlete has already agreed with themselves -------------------------
    if "caffeine" in rules and has("do not front-load all caffeine", "front-load all caffeine",
                                   "spread any remaining caffeine"):
        fired["caffeine_spread"] = "persistent-rules.md (race-day caffeine dosing rule)"
    if "electrolyte" in rules and has("every bottle"):
        fired["electrolyte_every_bottle"] = "persistent-rules.md (electrolyte rule)"
    if has("anchor race-day bike pacing", "rather than to %ftp", "anchor race-day bike"):
        fired["hr_anchor"] = "persistent-rules.md (HR-anchored race pacing rule)"
    if has("ride-to-feel", "ride to feel"):
        fired["ride_to_feel"] = "persistent-rules.md (ride-to-feel default)"

    # -- transitions: only when the PAST race was slower than the AGREED target ---------
    prev_t = _mmss_seconds(prev.get("t1t2_time"))
    targ_t = _mmss_seconds(targets.get("t1t2_time"))
    if prev_t and targ_t and prev_t > targ_t:
        fired["transitions"] = "prev_race.t1t2_time slower than race_targets.t1t2_time"
    elif any(w in prev_notes for w in ("transition", "race number")):
        fired["transitions"] = "prev_race.notes record trouble in transition"

    selected = []
    for cid, _rank, text, why in sorted(FOCUS_CATALOGUE, key=lambda c: c[1]):
        if cid not in fired:
            continue
        # Layer 3 of the no-new-numbers guarantee. Redundant while the catalogue stays
        # qualitative — which is exactly why it is here: it fails closed if that changes.
        if _FOCUS_DIGIT_RE.search(text):
            suppressed.append({"id": cid,
                               "reason": "focus text contains a figure; a pre-race focus "
                                         "point must carry no numbers (§8.6)"})
            continue
        if len(selected) >= 3:
            suppressed.append({"id": cid, "reason": "over the three-point cap (§8.6)"})
            continue
        selected.append({"id": cid, "text": text, "why": f"{why} — {fired[cid]}"})
    return selected, suppressed


def focus_for(profile: dict, rules_text: str = "", avg_g_hr=None,
              race_target_g_hr=None) -> list:
    """The focus STRINGS for `render_pre_race`. A curated `race_focus` list in profile.json
    wins outright — a human who has written the points down has said what matters better
    than any derivation will, and the override is the documented way to say so."""
    curated = [str(f) for f in (profile or {}).get("race_focus") or [] if str(f).strip()]
    if curated:
        return curated[:3]
    selected, _ = derive_focus(profile, rules_text, avg_g_hr, race_target_g_hr)
    return [f["text"] for f in selected]


# -- Athlete-facing wording ---------------------------------------------------
# docs/tone-of-voice-guide.md §8.6 is the specification these two render. The rules that
# shape the code, not just the prose: pre-race introduces NOTHING new and carries no
# numbers unless asked; post-race acknowledges the RESULT FIRST and does not analyse on
# the day. Both are deterministic templates rather than a Claude prompt on purpose — the
# single most important pre-race rule is "do not introduce anything new", and a generative
# prompt on race eve is exactly how a new fuelling number gets introduced at 20:30 the
# night before.

def render_pre_race(race: dict, first_name: str, phase: str = "race_eve",
                    block_fact: str = "", focus: list = None) -> str:
    """The race-eve or race-morning message. Under 80 words, no numbers, no new advice.

    `block_fact` is one specific already-true fact about the work behind the race (§8.6
    "name the work behind it"). `focus` is up to three reminders, and every one must
    already be in the agreed race plan — see `derive_focus` for how they are produced
    without inventing anything, or pass a curated list.

    A focus point carrying a DIGIT is dropped here as well as in `derive_focus`. This is
    the boundary a curated `profile.json` race_focus list also comes through, and a
    hand-written "hold 250 W" would be a race-eve number the athlete did not ask for."""
    name = race.get("name") or "your race"
    lines = []
    if phase == "race_day":
        lines.append(f"Race day, {first_name}. {name}.")
    else:
        lines.append(f"{name} tomorrow, {first_name}.")
    if block_fact:
        lines.append(block_fact.strip().rstrip(".") + ".")
    picked = [f.strip().rstrip(".") for f in (focus or [])
              if f and f.strip() and not _FOCUS_DIGIT_RE.search(f)][:3]
    if picked:
        lead = {1: "One thing, already done before:",
                2: "Two things, both already done before:",
                3: "Three things, all already done before:"}[len(picked)]
        lines.append(lead + " " + "; ".join(picked) + ".")
    lines.append("Nothing new today — the plan is the plan. Good luck.")
    return "\n\n".join(lines)


def render_post_race(race: dict, first_name: str, good_day=None, result: str = "") -> str:
    """The morning-after message. The result comes FIRST; analysis is offered, never
    delivered unhandled.

    `good_day` is a three-way on purpose: True and False change the wording materially
    (§8.6 splits them before writing a word), and None means nobody has told us yet — in
    which case the honest move is to ASK how it went rather than to assume it went well.
    A bad day gets no diagnosis attached, per §8.6."""
    name = race.get("name") or "your race"
    if good_day is None:
        return (f"{name} done, {first_name}. How did it go?\n\n"
                "Tell me as much or as little as you want — I'll leave the numbers alone "
                "until you ask for them.")
    if good_day:
        head = f"{name} done, {first_name} — that's a good day."
        if result:
            head += f" {result.strip().rstrip('.')}."
        return (head + "\n\nEnjoy it. I'll leave the analysis until you want it — "
                "there's nothing in the data that won't keep.")
    head = f"{name} done, {first_name} — that wasn't the day you wanted."
    if result:
        head += f" {result.strip().rstrip('.')}."
    return (head + "\n\nNo analysis today. When you want to go through it, say so "
            "and we will. Otherwise, rest.")


def prompt_block(races: list, today=None, first_name: str = "") -> str:
    """A race-awareness block to inject into the Claude-generated surfaces (the morning
    card, the night-before brief). Empty string on an ordinary day, so nothing changes on
    the ~350 days a year that are not race-adjacent."""
    ph = race_phase(races, today)
    if not ph["phase"]:
        return ""
    r, n = ph["race"], ph["days_to"]
    pri = f"{r['priority']}-race" if r["priority"] else "priority not confirmed"
    head = f"\n## Race awareness — TODAY IS NOT AN ORDINARY DAY\n"
    body = {
        "race_day": (f"{r['name']} IS TODAY ({pri}). Do not write a training card. "
                     "Say good luck, restate nothing new, keep it under 80 words, no numbers "
                     "unless asked (tone-of-voice guide §8.6)."),
        "race_eve": (f"{r['name']} is TOMORROW ({pri}). Introduce NOTHING new — no new pacing "
                     "target, no new fuelling number, no fresh analysis. Confidence, not "
                     "optimisation (§8.6)."),
        "race_week": (f"{r['name']} is in {n} days ({pri}). Race week: hold the plan, do not "
                      "add work, and do not start optimising."),
        "race_completed": (f"{r['name']} was {abs(n)} day{'s' if abs(n) != 1 else ''} ago ({pri}). "
                           "Acknowledge the result FIRST. Do not lead with metrics and do not "
                           "convert it into next block's targets (§8.6)."),
    }[ph["phase"]]
    return head + body + "\n"
