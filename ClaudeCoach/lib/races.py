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
    an ordinary training message gets treated as a race announcement."""
    if not (text or "").strip():
        return False
    if not _RACE_TRIGGER_RE.search(text):
        return False
    return resolve_date(text, today) is not None


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
    out["name"] = name or None

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
    already be in the agreed race plan — the caller passes them in precisely so this
    function cannot invent one."""
    name = race.get("name") or "your race"
    lines = []
    if phase == "race_day":
        lines.append(f"Race day, {first_name}. {name}.")
    else:
        lines.append(f"{name} tomorrow, {first_name}.")
    if block_fact:
        lines.append(block_fact.strip().rstrip(".") + ".")
    picked = [f.strip().rstrip(".") for f in (focus or []) if f and f.strip()][:3]
    if picked:
        lines.append("Three things, all of them things you've already done: "
                     + "; ".join(picked) + ".")
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
