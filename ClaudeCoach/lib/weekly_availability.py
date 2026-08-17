#!/usr/bin/env python3
"""weekly_availability.py — the athlete's OWN declaration of the hours they have
for one named week, and the resolver every consumer must go through.

WHY THIS EXISTS. Kathryn's persistent rules already require it — "Confirm each
week's available hours + any time caps via the Sunday check-in and build to that" —
and nothing in scripts/, lib/ or telegram/ ever asked. With no weekly figure in
existence, plan_builder._weekly_tss_cap fell back to profile.max_hours_per_week, a
static config constant, for the hours ceiling (hours x 100 x IF^2). Jamie's 15 is a
description of an athlete who trains less than he does: it gives a Peak ceiling of
778 TSS against an engine target up to 918, while last season he peaked at CTL 117.5
and raced at 103.7. Every Peak week therefore had to either breach the ceiling or
miss the target, and stage1-plan.py used to resolve that by quoting a config key at
a human ("raise max_hours_per_week to close the gap").

The fix is not a bigger constant. Hours are a question, asked once a week, answered
by the athlete, and valid for exactly the week they name.

DESIGN DECISIONS, and the alternatives rejected:

  * ONE FILE PER ATHLETE, A LIST OF DATED DECLARATIONS.
    athletes/<slug>/this-week-availability.json — the file stage1-plan.py ALREADY
    reads for per-week day-shape (Phase 5a). Extending it rather than adding a
    second record keeps one place an athlete's week-specific constraints live.
    athletes/ is gitignored wholesale, so nothing here can reach the PUBLIC repo.

  * EXPIRY IS BY NAMED WEEK, NOT BY DELETION.
    A declaration carries `week_start` (a Monday) and applies to THAT week only. It
    cannot leak into the next week, which is the failure mode a standing figure has
    and the reason `max_hours_per_week` was wrong in the first place. It is NOT
    implemented by deleting the record on Monday, because lib/plan_audit.py audits
    the CURRENT and NEXT week every morning at 06:25 and must resolve the same
    ceiling the generator used — deleting would make the audit retro-flag a week
    that was built correctly. Instead the list keeps the last _KEEP declarations and
    the resolver hands back only the one whose `week_start` matches the week asked
    about. Absent a match the answer is None and the caller falls back to config.

  * NO SILENT DEFAULT. There is deliberately no "assume last week's figure".
    A missing declaration resolves to None and the caller must say so to the
    athlete; guessing either way (unlimited or minimal) is the thing this module
    exists to stop.

  * LEGACY FLAT FILES KEEP WORKING, BUT YIELD NO HOURS.
    Before this module the file was a flat object of day-shape keys with no dates
    (`{"unavailable_days": ["Wed"], ...}`). Such a file has no week attached, so it
    is honoured for day shape exactly as today (see day_shape) and can never supply
    an hours figure — an undated number is precisely the standing constant being
    replaced.

SCHEMA (the list form; all fields bar week_start optional):

    {"declarations": [
      {"week_start": "2026-08-03",          # Monday of the week declared
       "hours": 17.5,                        # hours the athlete has THIS week
       "constraints": "away Thu-Fri, nothing long Mon-Thu",   # free prose, shown to the planner
       "unavailable_days": ["Thu", "Fri"],  # Phase 5a day-shape keys, unchanged
       "swim_days": [...], "bike_days": [...], "run_days": [...],
       "declared_at": "2026-08-02T07:14:03",
       "source": "telegram-reply"}          # or "manual" / "coach"
    ]}

`hours` is the athlete's total training time for the week. It is NOT a per-day cap;
day-level caps belong in `constraints` prose, which reaches the Stage-1 planner.
"""
from __future__ import annotations

import json
import re
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
FILENAME = "this-week-availability.json"

# How many dated declarations to retain. plan_audit's window is the current week plus
# the next, and the Sunday build writes next week's before that week begins, so three
# would do; six is a cheap margin that also leaves a short readable history for
# diagnosing "why was that week capped there?" without becoming an archive.
_KEEP = 6

# Sanity band on a declared figure. Below the floor is almost certainly a mistyped
# reply (or minutes); above the ceiling is not a week any of these athletes trains and
# would hand the generator a ceiling nobody has sanity-checked. Out-of-band values are
# REFUSED at write time and IGNORED at read time, so a bad number degrades to the
# config fallback rather than raising anyone's ceiling.
MIN_HOURS = 1.0
MAX_HOURS = 40.0

# Floor for an UNFRAMED bare number offered as a possible hours reply (tier 2 below).
# Deliberately higher than MIN_HOURS: the card this ask rides on also asks "Ankle score
# this morning? (0-10)", so a bare "3" is overwhelmingly a pain score rather than a
# three-hour training week for athletes whose standing figures are 8 and 15. An athlete
# who genuinely has a very light week can still declare it in the framed form ("3 hours
# this week"), which is unambiguous and bypasses this floor entirely. Between this floor
# and 10 the two questions genuinely overlap, which is why tier 2 never writes without
# an explicit confirmation tap.
BARE_MIN_HOURS = 5.0


# The day-shape keys a declaration can carry. `declared_days` is the list of days the
# athlete NAMED, and it is stored separately from the three sport lists because it is the
# only thing that can answer "did the athlete speak about Wednesday at all?". Without it a
# declaration of "Wednesday swim" is indistinguishable from a declaration that adds a swim
# to Wednesday and leaves the standing Wednesday run alone - which is the reading that put
# a run on the Wednesday Jamie had declared as a swim. `excluded_sports` names the sports
# taken off for the WHOLE week, which an empty day list can no longer mean on its own (see
# excluded_sports()).
DAY_SHAPE_KEYS = ("swim_days", "bike_days", "run_days", "unavailable_days",
                  "declared_days", "excluded_sports")


def path_for(slug: str, base: Path | str | None = None) -> Path:
    return Path(base or BASE) / "athletes" / slug / FILENAME


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def load_raw(slug: str, base: Path | str | None = None) -> dict:
    """The file as written, or {} when absent/unreadable. Never raises: an
    unparseable availability file must degrade to "no declaration", not kill a build."""
    p = path_for(slug, base)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _declarations(raw: dict) -> list[dict]:
    d = raw.get("declarations") if isinstance(raw, dict) else None
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


# Keys this module manages. Their presence proves the file was written by THIS module, so
# it cannot be a pre-existing Phase 5a flat file however empty it otherwise looks.
_MANAGED_KEYS = ("declarations", "asks", "legacy_day_shape")


def _is_legacy_flat(raw: dict) -> bool:
    """A pre-existing Phase 5a file: an object of day-shape keys, with no `declarations`
    list and no `week_start`.

    The `_MANAGED_KEYS` test is load-bearing, not defensive. `note_ask_sent` writes
    `{"asks": {...}, "declarations": []}` before any declaration exists, and an EMPTY
    declarations list is falsy — so without this check that file satisfies every clause
    above and `day_shape` hands the asks bookkeeping back to
    session_library.reconcile_day_rules as though it were the athlete's day shape, for
    every athlete, every Sunday, from the moment the ask goes out.
    """
    if not raw or any(k in raw for k in _MANAGED_KEYS):
        return False
    return not _declarations(raw) and not raw.get("week_start")


def _clean_hours(v) -> float | None:
    try:
        h = float(v)
    except (TypeError, ValueError):
        return None
    return h if MIN_HOURS <= h <= MAX_HOURS else None


def for_week(slug: str, week_start: date | str | None,
             base: Path | str | None = None) -> dict | None:
    """The declaration the athlete made for the week beginning `week_start`, or None.

    None — not a guess and not last week's — whenever there is no declaration for
    exactly that week. `week_start` is normalised to its Monday, so passing any day
    inside the week resolves the same record. A None `week_start` (the macro
    projection's ceiling lambda discards its week, lib/macro_projection.py:338)
    resolves to None by design: a declaration for one real week must never be
    stretched across a projection of many.
    """
    if week_start is None:
        return None
    ws = date.fromisoformat(week_start) if isinstance(week_start, str) else week_start
    ws = _monday(ws)
    raw = load_raw(slug, base)
    for d in _declarations(raw):
        try:
            if _monday(date.fromisoformat(str(d.get("week_start")))) == ws:
                return d
        except Exception:
            continue
    # Single-object dated form, for a hand-written file.
    if isinstance(raw, dict) and raw.get("week_start"):
        try:
            if _monday(date.fromisoformat(str(raw["week_start"]))) == ws:
                return raw
        except Exception:
            pass
    return None


def hours_for_week(slug: str, week_start: date | str | None,
                   base: Path | str | None = None) -> float | None:
    """Declared hours for that week, or None. Out-of-band figures return None so a
    typo degrades to the config fallback instead of moving a ceiling."""
    d = for_week(slug, week_start, base)
    return _clean_hours(d.get("hours")) if d else None


def constraints_for_week(slug: str, week_start: date | str | None,
                         base: Path | str | None = None) -> str:
    d = for_week(slug, week_start, base)
    return str((d or {}).get("constraints") or "").strip()


def rest_day_waiver_for_week(slug: str, week_start: date | str | None,
                             base: Path | str | None = None) -> str | None:
    """The stated reason this week trains through its rest day, or None.

    The rest-day rule's escape hatch: `validate_week` downgrades `no_rest_day` to a soft
    `no_rest_day_waived` when it is given a reason, and lib/rest_day stands down rather
    than clearing a day. So this is the one input that can turn off a hard safety-adjacent
    check, and it is deliberately its OWN key rather than being read out of
    `constraints` — a week where the athlete happened to mention a travel block would
    otherwise waive their rest day without anyone deciding to.

    FAILS CLOSED. Anything that is not a non-blank string is None, i.e. the week keeps
    its hard requirement. A waiver that cannot be read is not a waiver.
    """
    d = for_week(slug, week_start, base)
    w = (d or {}).get("rest_day_waiver")
    return w.strip() if isinstance(w, str) and w.strip() else None


def day_shape(slug: str, week_start: date | str | None,
              base: Path | str | None = None) -> dict | None:
    """The day-shape keys session_library.reconcile_day_rules consumes, or None.

    Two sources, in precedence order: this week's declaration, else a LEGACY flat
    file. The legacy branch is what preserves today's behaviour byte-for-byte for any
    file written before this module existed — it is honoured for day shape and, per
    the module docstring, can never supply hours.
    """
    d = for_week(slug, week_start, base)
    if d:
        shape = {k: d[k] for k in DAY_SHAPE_KEYS if isinstance(d.get(k), list)}
        return shape or None
    raw = load_raw(slug, base)
    return raw if _is_legacy_flat(raw) else None


def _atomic_write(p: Path, payload: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".wa-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=1, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def record(slug: str, week_start: date | str, *, hours=None, constraints: str = "",
           rest_day_waiver: str = "", source: str = "manual",
           base: Path | str | None = None, **day_keys) -> dict:
    """Write (or replace) the declaration for one week. Returns the stored record.

    Raises ValueError on an out-of-band `hours`, rather than storing a figure that
    would silently become somebody's training ceiling.

    `rest_day_waiver` is its own named parameter and not one of `**day_keys` because it
    is the only input here that can stand a hard check down (see
    rest_day_waiver_for_week): a caller has to ask for it by name, and a typo lands as an
    unknown kwarg rather than quietly waiving a rest day.
    """
    ws = date.fromisoformat(week_start) if isinstance(week_start, str) else week_start
    ws = _monday(ws)
    rec: dict = {"week_start": ws.isoformat(),
                 "declared_at": datetime.now().isoformat(timespec="seconds"),
                 "source": source}
    if hours is not None:
        h = _clean_hours(hours)
        if h is None:
            raise ValueError(f"hours {hours!r} outside {MIN_HOURS}-{MAX_HOURS} — not stored")
        rec["hours"] = h
    if constraints:
        rec["constraints"] = str(constraints).strip()
    if str(rest_day_waiver or "").strip():
        rec["rest_day_waiver"] = str(rest_day_waiver).strip()
    for k in DAY_SHAPE_KEYS:
        v = day_keys.get(k)
        if isinstance(v, list):
            rec[k] = list(v)

    raw = load_raw(slug, base)
    # A legacy flat file is carried forward whole under `legacy_day_shape` rather than
    # discarded: it is the athlete's standing Phase 5a shape and deleting it on the
    # first declaration would silently widen their week.
    out: dict = {}
    if _is_legacy_flat(raw):
        out["legacy_day_shape"] = raw
    elif isinstance(raw, dict):
        out = {k: v for k, v in raw.items() if k != "declarations"}
    decls = [d for d in _declarations(raw) if str(d.get("week_start")) != rec["week_start"]]
    replaced = next((d for d in _declarations(raw)
                     if str(d.get("week_start")) == rec["week_start"]), None)
    decls.append(rec)
    decls.sort(key=lambda d: str(d.get("week_start")))
    out["declarations"] = decls[-_KEEP:]
    _atomic_write(path_for(slug, base), out)
    _audit(slug, rec, replaced, base)
    return rec


def _audit(slug: str, rec: dict, replaced: dict | None,
           base: Path | str | None) -> None:
    """Write one ops-alerts line per declaration write. Best-effort, never raises.

    On 10 Aug 2026 a day-shape nobody typed appeared against Jamie's w/c 17 Aug -
    "swim Wed, bike Wed, run Tue" - and the coach rebuilt his week off it. It was
    deleted before anyone could look at it, and there was NO WAY to find out what had
    written it: record() stores a `source` string but logged nothing, so the who, the
    when and the triggering text were all unrecoverable. An hour of his morning is
    still unexplained for exactly that reason.

    This also names what was OVERWRITTEN. record() REPLACES a week's declaration, so a
    capture firing on a stray sentence can silently discard the athlete's real one, and
    that is the failure that costs a week rather than a message.

    Skipped when `base` is passed, which only tests do - so the suite does not write to
    the operator's alert log.
    """
    if base is not None:
        return
    try:
        import ops_log
        days = {k: rec.get(k) for k in DAY_SHAPE_KEYS if rec.get(k)}
        was = ""
        if replaced:
            prior = {k: replaced.get(k) for k in DAY_SHAPE_KEYS if replaced.get(k)}
            was = (f" REPLACED a {replaced.get('source')!r} record from "
                   f"{replaced.get('declared_at')}: {prior or 'no day keys'}")
        ops_log.record_run(
            "availability-record", athlete=slug, ok=True,
            detail=(f"wrote {rec.get('source')!r} declaration for w/c "
                    f"{rec.get('week_start')}: {days or 'no day keys'}"
                    f"{' hours=' + str(rec['hours']) if rec.get('hours') else ''}{was}"))
    except Exception:
        pass


def has_declaration(slug: str, week_start: date | str | None,
                    base: Path | str | None = None) -> bool:
    """True when the athlete has already answered for that week — the gate that stops
    the Sunday ask being sent twice."""
    return for_week(slug, week_start, base) is not None


# ---------------------------------------------------------------------------
# THE SUNDAY ASK
# ---------------------------------------------------------------------------
# WHEN. Sunday morning, appended to the morning check-in (06:00-09:00 poll), for the
# week starting the NEXT day. The Sunday build is `0 18 * * 0`, so the athlete has at
# least nine hours to answer before the plan is generated. Every other Sunday push
# lands too late to be useful: weekly-summary 20:00, evening-checkin 21:00,
# night-before-brief 20:30 — all after the build.
#
# WHY IT PIGGYBACKS RATHER THAN BEING ITS OWN PUSH. The athletes already receive
# ~35-45 pushes a week, and three evening messages were merged into two this morning
# because Kathryn stopped answering three consecutive asks. A new Sunday cron would
# spend the one unit of attention this question needs on a notification. Appended to
# a message that is already going out, it costs zero extra pushes. It is also
# DETERMINISTIC text, appended after the LLM card is extracted and before the send —
# not a prompt instruction the model may quietly drop, which for the one question the
# whole mechanism depends on is not a risk worth taking.
#
# COACHING LEVEL. coaching_levels.level_block() only shapes LLM PROMPTS, so a
# hardcoded string bypasses it entirely and the level variants have to be written by
# hand. They are, below. Calum is `beginner`: no hours-vs-Load framing, no jargon.
#
# ILLNESS. Silent while the illness flag is active (lib/illness). A sick athlete is
# not planning their training week, the reduced week is already driven by the flag,
# and asking would burn attention on a question whose answer we would override.

_ASK = {
    "beginner": (
        "⏱ *Next week* — how many hours can you train? Just reply with a number.\n"
        "Tell me too if any days are out (away, work, family) or if a day needs to be "
        "short. I put next week together this evening."
    ),
    "mid": (
        "⏱ *Next week* — how many hours have you actually got? Reply with a number "
        "(e.g. 12), plus anything that boxes the week in: travel, nothing long Mon-Thu, "
        "a cap on a particular day.\n"
        "I build next week this evening, so today is the time to tell me."
    ),
    "pro": (
        "⏱ *Next week* — how many hours have you actually got? Reply with a number "
        "(e.g. 17.5), plus any constraints that shape it: travel block, nothing long "
        "Mon-Thu, a hard cap on a day.\n"
        "I build next week this evening and the weekly Load ceiling comes off that "
        "figure, so today is the time to tell me."
    ),
}

# The no-reply sentence, appended to the ask so the athlete knows the consequence of
# silence BEFORE they stay silent. Two forms, because the fallback genuinely differs:
# an athlete with a standing figure degrades to it; Kathryn has none by a permanent
# rule (10 Jul 2026: "Do NOT reinstate a fixed hours cap ... fall back to full
# phase-required load if she doesn't specify") and degrades to the phase load ceiling.
_FALLBACK_WITH_HOURS = ("If I don't hear from you I'll build to your usual "
                        "{hours:g} hours and say so in the plan.")
_FALLBACK_NO_HOURS = ("If I don't hear from you I'll build the full week the plan "
                      "calls for and say so.")


def sunday_hours_ask(slug: str, week_start: date | str, *, coaching_level: str = "mid",
                     base: Path | str | None = None) -> str:
    """The one-per-week hours question, or "" when it must not be asked.

    Returns "" when (a) the athlete has ALREADY declared for that week — the answer is
    in, do not ask twice; or (b) the illness flag is active. The caller owns the
    Sunday-only gate and the send; this function owns the copy and the two content
    gates, so a future caller cannot forget them.
    """
    b = Path(base or BASE)
    if has_declaration(slug, week_start, b):
        return ""
    try:
        import illness as _illness
        st = _illness.state_from_dir(b / "athletes" / slug)
        if st and st.get("active"):
            return ""
    except Exception:
        pass                                    # a missing illness module must not gag the ask
    ask = _ASK.get(coaching_level, _ASK["mid"])
    try:
        prof = json.loads((b / "athletes" / slug / "profile.json").read_text())
        standing = _clean_hours(prof.get("max_hours_per_week"))
    except Exception:
        standing = None
    tail = (_FALLBACK_WITH_HOURS.format(hours=standing) if standing
            else _FALLBACK_NO_HOURS)
    return f"{ask}\n_{tail}_"


# ---------------------------------------------------------------------------
# THE REPLY — recognising an hours declaration in free text
# ---------------------------------------------------------------------------
# The ask above goes out on the Sunday morning card; until this half existed nothing
# parsed the answer and a declaration had to be hand-written by an agent. Shape mirrors
# lib/races.py and lib/illness.py: a NARROW detector plus a parser, both refusing rather
# than guessing.
#
# WHY THERE ARE TWO TIERS, and why a bare number never writes on its own.
# The morning check-in card this ask is appended to ALSO asks questions whose answer is
# a bare number in the very band a plausible hours figure occupies:
# "Ankle score this morning? (0-10)", "Injury pain score before heading out? (0-10)",
# "Weight this morning?" (scripts/morning-checkin.py:60-72). An athlete replying "7" to
# the ankle question would, under a one-tier "bare number 1-40 on a Sunday" detector,
# silently become a 7-hour week and cap their Load ceiling at less than half their real
# one. That is a worse failure than not capturing at all, because it is invisible: the
# plan message would read "the {n} hours you told me you have" about a number the
# athlete never said. So:
#
#   TIER 1 — UNAMBIGUOUS (`looks_like_hours_declaration`). The message carries a figure
#            AND says it is about a week ("14 hours next week", "about 14 this week",
#            "20, big week"). Nothing else on the card is phrased that way, so this
#            writes immediately and echoes the consequence back.
#   TIER 2 — CONTEXTUAL (`looks_like_hours_reply`). A figure with no weekly framing
#            ("12 max, nothing long midweek", or a bare "14"). Only ever considered
#            while the ask is genuinely OUTSTANDING (`ask_outstanding`), and even then
#            it does NOT write — the caller asks the athlete to confirm and writes on
#            the tap. An ambiguous number costs one tap; a wrong ceiling costs a week.
#
# Both tiers refuse a QUESTION outright, the lesson races.looks_like_race_statement
# already learned the hard way ("Am I racing on Saturday?" became a race called "?").
# Both also refuse a REPORT of hours already done — "I did 14 hours last week" is not a
# declaration about the week ahead, and treating it as one would cap the coming week off
# the previous one.

# A figure with an explicit hours unit: "14h", "14 hrs", "17.5 hours".
_HOURS_TOKEN_RE = re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:h\b|hr\b|hrs\b|hours?\b)", re.I)
# A range — "maybe 12-13", "12 to 14". The LOWER bound is taken: the spec is explicit
# about it, and building to the top of a range the athlete hedged would overshoot.
# A hedged hours range ("maybe 12-13"). The trailing (?![...]) is load-bearing: without
# it "the wheels came off at 15-20k" parsed as 15 HOURS and the bot asked Jamie to confirm
# "15 hours of training for next week" while quoting his sentence about run pacing back at
# him (3 Aug 2026). A number followed by a unit is a distance, a pace, a power or a weight
# - never a weekly hours budget.
_NON_HOURS_UNIT = r"(?!\s*(?:k\b|km|m\b|mi\b|mile|%|s\b|sec|min|bpm|w\b|watt|kg|lb|°|C\b|:))"
_HOURS_RANGE_RE = re.compile(
    r"(\d{1,2}(?:\.\d)?)\s*(?:-|–|to)\s*(\d{1,2}(?:\.\d)?)" + _NON_HOURS_UNIT, re.I)
# A bare figure, used only once the message is already known to be about hours.
# The BARE path is reached only after the range path declined, so it must also reject a
# number that is part of a hyphenated/slashed/colonned construct: in "came off at 15-20k",
# the range guard correctly rejects 15-20, and without this the bare path then picks "15"
# out of it on its own and calls it 15 hours.
_BARE_NON_HOURS = (r"(?!\s*(?:[-–/:]|k\b|km|m\b|mi\b|mile|%|s\b|sec|min|bpm|w\b|watt|"
                   r"kg|lb|°|C\b))")
_BARE_NUM_RE = re.compile(r"\b(\d{1,2}(?:\.\d)?)\b" + _BARE_NON_HOURS)

# "…this week", "next week", "big week". NB `\bweek\b` deliberately does NOT match
# "midweek", so "nothing long midweek" is a CONSTRAINT and not weekly framing — which is
# what puts "12 max, nothing long midweek" in the contextual tier where it belongs.
_WEEK_FRAMING_RE = re.compile(
    r"\b(?:this|next|coming|the)\s+week\b|"
    r"\b(?:big|easy|light|quiet|heavy|full|short|normal)\s+week\b|"
    r"\bweek\s+(?:i|i'?ve|ive)\b", re.I)

_THIS_WEEK_RE = re.compile(r"\bthis\s+week\b", re.I)
_NEXT_WEEK_RE = re.compile(r"\bnext\s+week\b", re.I)

# A question is never a declaration. Beyond the trailing "?" (which races.py relies on),
# an interrogative opener catches the unpunctuated form — "how many hours should I do".
_QUESTION_RE = re.compile(
    r"^\s*(?:how|what|when|which|why|should|shall|can|could|would|do|does|did|is|are|am)\b"
    r"|\bhow\s+(?:many|much|long)\b|\bshould\s+i\b|\bdo\s+i\s+(?:have|need)\b", re.I)

# A REPORT of training already done, not a declaration of time available. "I did 14
# hours last week", "managed 12", "slept 7 hours", "ended up with 9".
_PAST_REPORT_RE = re.compile(
    r"\b(?:did|done|managed|logged|completed|ended\s+up|got\s+through|totalled|totaled|"
    r"slept|sleep\s+was|ran|rode|swam|trained)\b|\blast\s+week\b|\byesterday\b", re.I)

# A SINGLE SESSION's duration, not a week's budget: "2 hour ride", "90 min run",
# "long run of 3 hours". Without this, "I've got a 2 hour ride tomorrow" is a 2-hour
# week. Weekly framing overrides it (see below) so "12 hours this week, one 3 hour ride"
# still reads as 12.
# The Sunday ask is about a WEEK. A figure attached to a single day is answering a
# different question - "I only have 3-4h today", "tomorrow I'll do 3hrs" - and recording it
# as the week's budget would cap the whole week at one day's training. Checked BEFORE the
# session-noun test, because "3-4h today" carries no session noun at all.
_SINGLE_DAY_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|arvo)|"
    r"right\s+now|later\s+(?:today|on))\b", re.I)

_SESSION_CTX_RE = re.compile(
    r"\b(?:ride|rides|run|runs|swim|swims|session|sessions|bike|turbo|gym|race|"
    r"long\s+run|long\s+ride|brick|interval|intervals|tempo|workout)\b", re.I)


def _lower_of_range(text: str):
    """The lower bound of a hedged range, or None. "maybe 12-13" -> 12.0."""
    m = _HOURS_RANGE_RE.search(text)
    if not m:
        return None
    try:
        a, b = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    # Guard against a date or a score being read as a range: both ends must be a
    # plausible weekly figure and the range must actually ascend.
    if not (MIN_HOURS <= a <= MAX_HOURS and MIN_HOURS <= b <= MAX_HOURS and a < b):
        return None
    return a


def _strip_hours_phrase(text: str) -> str:
    """The message with the hours figure and its unit removed, leaving the prose that
    is the athlete's CONSTRAINTS. Kept verbatim otherwise — it reaches the Stage-1
    planner as written, so paraphrasing it here would lose the athlete's own words."""
    out = _HOURS_RANGE_RE.sub(" ", text)
    out = _HOURS_TOKEN_RE.sub(" ", out)
    # A stranded unit word: the range regex removes "12-13" and leaves "hours" behind,
    # which would otherwise be stored as the athlete constraint "hours".
    out = re.sub(r"\b(?:h|hr|hrs|hours?)\b", " ", out, flags=re.I)
    out = re.sub(r"\b(?:i'?ve\s+got|ive\s+got|i\s+have|i'?ve|got|about|maybe|around|"
                 r"roughly|approx\w*|only|just|max|maximum|up\s+to|at\s+most)\b",
                 " ", out, flags=re.I)
    out = _WEEK_FRAMING_RE.sub(" ", out)
    out = _BARE_NUM_RE.sub(" ", out)
    # Tidy the punctuation the removals leave behind, without touching interior prose.
    out = re.sub(r"\s+", " ", out).strip(" ,.;:-–—")
    return out


def parse_hours_message(text: str) -> dict:
    """Pull a weekly hours figure and the athlete's constraints out of a chat message.

    Returns {"hours": float|None, "constraints": str, "framed": bool, "refused": str}.
    `refused` names the reason when the message is readable but must NOT be treated as a
    declaration, so a caller can log WHY nothing was captured instead of failing silently.
    `framed` is the tier-1 signal: the message itself says it is about a week.
    """
    out = {"hours": None, "constraints": "", "framed": False, "refused": ""}
    t = (text or "").strip()
    if not t:
        out["refused"] = "empty"
        return out
    if t.endswith("?") or _QUESTION_RE.search(t):
        out["refused"] = "question"
        return out
    if _PAST_REPORT_RE.search(t):
        out["refused"] = "report of hours already done, not hours available"
        return out

    framed = bool(_WEEK_FRAMING_RE.search(t))
    # A single-day figure is not a week's budget, unless the message ALSO frames a week
    # ("3h today, maybe 12 across the week").
    if _SINGLE_DAY_RE.search(t):
        if not framed:
            out["refused"] = "figure attached to a single day, not a week's budget"
            return out
        # Both a day marker AND week framing, e.g. "3h today, maybe 12 across the week".
        # Which figure is the week's is genuinely ambiguous, and guessing writes a ceiling.
        # Refuse and let the athlete say it plainly.
        # Count ALL three patterns together. Counting them separately undercounts: in
        # "3h today, maybe 12 across the week" the "3" of "3h" is invisible to
        # _BARE_NUM_RE (no word boundary before the h), so each pattern sees one figure
        # while the message plainly contains two.
        n_figures = (len(_HOURS_TOKEN_RE.findall(t))
                     + len(_HOURS_RANGE_RE.findall(t))
                     + len(_BARE_NUM_RE.findall(t)))
        if n_figures > 1:
            out["refused"] = ("a day figure and a week figure in one message - ambiguous, "
                              "ask which is the week")
            return out
    # A session noun with no weekly framing is a single session's duration, not a week.
    if _SESSION_CTX_RE.search(t) and not framed:
        out["refused"] = "reads as one session's duration, not a week's budget"
        return out

    h = _lower_of_range(t)
    if h is None:
        m = _HOURS_TOKEN_RE.search(t)
        if m:
            h = _clean_hours(m.group(1))
        elif framed:
            # No unit, but the message says "week" — a bare figure is the hours.
            nums = [_clean_hours(n) for n in _BARE_NUM_RE.findall(t)]
            nums = [n for n in nums if n is not None]
            h = nums[0] if len(nums) == 1 else None
        else:
            # UNFRAMED and unitless. Held to BARE_MIN_HOURS, not MIN_HOURS: see the
            # constant. A single figure only — two numbers with no unit and no weekly
            # framing name nothing we can attribute.
            nums = [_clean_hours(n) for n in _BARE_NUM_RE.findall(t)]
            nums = [n for n in nums if n is not None]
            h = nums[0] if len(nums) == 1 else None
            if h is not None and h < BARE_MIN_HOURS:
                out["refused"] = (f"bare {h:g} is below the {BARE_MIN_HOURS:g}h floor for an "
                                  f"unframed figure — reads as a score, not a week")
                return out
    if h is None:
        out["refused"] = out["refused"] or "no single plausible hours figure"
        return out

    out["hours"] = h
    out["framed"] = framed
    out["constraints"] = _strip_hours_phrase(t)
    return out


def looks_like_hours_declaration(text: str) -> bool:
    """TIER 1 — the message is plainly declaring a week's available hours, with no
    surrounding context needed. A figure AND weekly framing, and not a question or a
    report. Safe to write on, because nothing else either athlete-facing card asks is
    phrased this way."""
    p = parse_hours_message(text)
    return p["hours"] is not None and p["framed"]


# Longest a message can be and still plausibly be an ANSWER to the Sunday hours ask.
# "12", "about 12 hours", "maybe 12-13, away Thu" all fit. The 3 Aug 2026 false positive
# was 21 words about where a race went wrong, which is not an answer to anything.
_REPLY_MAX_WORDS = 12


def looks_like_hours_reply(text: str) -> bool:
    """TIER 2 — the message COULD be the answer to the Sunday ask: a figure, no weekly
    framing, nothing disqualifying. True here is not permission to write; the caller
    must have checked `ask_outstanding` and must confirm with the athlete first, because
    a bare number on that card is equally plausibly an ankle score."""
    if len((text or "").split()) > _REPLY_MAX_WORDS:
        return False
    p = parse_hours_message(text)
    return p["hours"] is not None


# ---------------------------------------------------------------------------
# WAS THE ASK ACTUALLY SENT?
# ---------------------------------------------------------------------------
# The contextual tier must not fire on a guess that the ask went out. "It is Sunday and
# there is no declaration" is NOT the same statement: `sunday_hours_ask` returns "" and
# sends nothing when the illness flag is up, and a card can fail to send at all. In both
# of those cases a bare "7" is answering something else, and offering to record it as
# hours would be the bot inventing a conversation it never had. So the send is recorded
# and the window is measured from it.
_ASK_WINDOW_HOURS = 36          # generous: the ask lands 06:00-09:00 Sunday and the
                                # build is 18:00 Sunday, but an athlete who answers
                                # Monday morning is still answering THIS ask.


def note_ask_sent(slug: str, week_start: date | str, base: Path | str | None = None) -> None:
    """Record that the hours ask for `week_start` was sent, so a later bare-number reply
    can be attributed to it. Best-effort and never raises: failing to record the send
    must not stop the card going out — it only costs the contextual tier."""
    try:
        ws = date.fromisoformat(week_start) if isinstance(week_start, str) else week_start
        ws = _monday(ws)
        raw = load_raw(slug, base)
        out = {k: v for k, v in raw.items() if k != "declarations"} if isinstance(raw, dict) else {}
        if _is_legacy_flat(raw):
            out = {"legacy_day_shape": raw}
        asks = out.get("asks") if isinstance(out.get("asks"), dict) else {}
        asks[ws.isoformat()] = {"sent": datetime.now().isoformat(timespec="seconds"),
                                "consumed": False}
        out["asks"] = dict(sorted(asks.items())[-_KEEP:])
        out["declarations"] = _declarations(raw)
        _atomic_write(path_for(slug, base), out)
    except Exception:
        pass


def consume_ask(slug: str, week_start: date | str,
                base: Path | str | None = None) -> None:
    """Close the free-scan window on the hours ask.

    The ask used to stay live for _ASK_WINDOW_HOURS (36), and during that whole window ANY
    one-or-two-digit number in ANY message was a candidate answer. That is a text scanner
    pretending to be a form field, and on 3 Aug 2026 it misread a distance range, an
    elapsed-minutes reference and two single-day figures as weekly budgets - four times in
    24 hours.

    Called after the FIRST athlete message following the ask, whether or not that message
    turned out to be an hours figure. So the ask is answerable by the next thing they say
    and nothing after it. A later declaration still works through the self-framing tier
    ("14 hours next week"), which needs no outstanding ask at all.

    Best-effort and never raises: failing to close the window must not drop a reply.
    """
    try:
        ws = date.fromisoformat(week_start) if isinstance(week_start, str) else week_start
        ws = _monday(ws)
        raw = load_raw(slug, base)
        if not isinstance(raw, dict):
            return
        asks = raw.get("asks")
        if not isinstance(asks, dict) or ws.isoformat() not in asks:
            return
        rec = asks[ws.isoformat()]
        if isinstance(rec, dict):
            if rec.get("consumed"):
                return
            rec["consumed"] = True
        else:
            asks[ws.isoformat()] = {"sent": str(rec), "consumed": True}
        out = {k: v for k, v in raw.items() if k != "declarations"}
        out["asks"] = asks
        out["declarations"] = _declarations(raw)
        _atomic_write(path_for(slug, base), out)
    except Exception:
        pass


def ask_outstanding(slug: str, week_start: date | str, now: datetime | None = None,
                    base: Path | str | None = None) -> bool:
    """True when the ask for `week_start` was sent, is still within its answer window,
    and has NOT yet been answered — the only state in which an unframed figure may be
    offered to the athlete as an hours declaration."""
    if has_declaration(slug, week_start, base):
        return False
    try:
        ws = date.fromisoformat(week_start) if isinstance(week_start, str) else week_start
        ws = _monday(ws)
        asks = load_raw(slug, base).get("asks") or {}
        rec = asks[ws.isoformat()]
        # Two stored shapes: a bare ISO string (legacy) or {"sent":..., "consumed":...}.
        if isinstance(rec, dict):
            if rec.get("consumed"):
                # The athlete has already spoken since the ask went out and said something
                # that was not an hours figure. The moment has passed: a number typed later
                # is about something else. This is what stopped "came off at 15-20k" and
                # "you told me 10 mins ago" being read as 15 and 10 HOURS a day after a
                # Sunday ask (3 Aug 2026, four occurrences in 24h).
                return False
            sent = datetime.fromisoformat(str(rec.get("sent")))
        else:
            sent = datetime.fromisoformat(str(rec))
    except Exception:
        return False
    n = now or datetime.now()
    return timedelta(0) <= (n - sent) <= timedelta(hours=_ASK_WINDOW_HOURS)


def outstanding_ask_week(slug: str, now: datetime | None = None,
                         base: Path | str | None = None) -> date | None:
    """The week of the most recent ask that was sent, is in its window and is unanswered.

    This is what a reply is ABOUT, and it must be read rather than recomputed from the
    calendar. "The Monday after today" is only the same thing on a Sunday: an athlete who
    answers on Monday morning — well inside `_ASK_WINDOW_HOURS`, which exists precisely so
    that reply still counts — would otherwise have their figure recorded against the
    FOLLOWING week, leaving the week they were asked about on the config fallback and
    silently capping a week they never declared.
    """
    asks = load_raw(slug, base).get("asks") or {}
    if not isinstance(asks, dict):
        return None
    best = None
    for ws, _sent in asks.items():
        try:
            w = _monday(date.fromisoformat(str(ws)))
        except Exception:
            continue
        if ask_outstanding(slug, w, now=now, base=base) and (best is None or w > best):
            best = w
    return best


def target_week(slug: str, text: str = "", today: date | None = None,
                now: datetime | None = None, base: Path | str | None = None) -> date:
    """The Monday a reply should be recorded against, in precedence order:

    1. the week of an OUTSTANDING ask — a recorded fact about what was actually asked,
       which beats any inference from the calendar or the wording;
    2. "next week" in the message — the following Monday;
    3. "this week" in the message on a Mon-Sat — the CURRENT Monday. Said mid-week that
       plainly means the week in progress, and defaulting to the next one would record a
       figure against a week the athlete was not talking about;
    4. otherwise the next Monday, which is what the Sunday ask is about and the right
       reading of a bare figure.

    Sunday deliberately falls through 3 to 4: it is the last day of the current week and
    the ask that day is about the week starting tomorrow.
    """
    t = today or date.today()
    pending = outstanding_ask_week(slug, now=now, base=base)
    if pending:
        return pending
    nxt = t + timedelta(days=(7 - t.weekday()) % 7 or 7)
    low = (text or "").lower()
    if _NEXT_WEEK_RE.search(low):
        return nxt
    if _THIS_WEEK_RE.search(low) and t.weekday() != 6:
        return _monday(t)
    return nxt


# Last weekday on which an unframed day-shape declaration may be read as the week IN
# PROGRESS: Thursday. A Thursday message naming days from Thursday on IS about this week
# (there are four of them left); typed on a Friday, Saturday or Sunday it is not, because
# what remains of the week cannot carry a week's shape and the Sunday ask's reading (next
# Monday) is then the right default. Naming only days that have already gone falls through
# to next Monday on any day of the week - see rule 4.
_DAY_SHAPE_CURRENT_WEEK_LAST_WD = 3          # Mon=0 ... Thu=3


def day_shape_target_week(slug: str, text: str = "", named_days=(),
                          today: date | None = None, now: datetime | None = None,
                          base: Path | str | None = None) -> date:
    """The Monday a DAY-SHAPE declaration belongs to.

    Separate from `target_week` because the two are answering different questions. That one
    resolves a reply to the SUNDAY HOURS ASK, whose subject is next week, so an unframed
    figure rightly falls through to the next Monday. A day-shape declaration is unsolicited
    and arrives on any day, and the same fall-through is what caused the 10 Aug 2026
    argument: Jamie restated his availability on a MONDAY, it was recorded against w/c 17
    Aug, `day_shape()` for the current week returned None, stage1-plan therefore saw no
    declaration at all and rebuilt the week off day_rules - which is where the Wednesday
    run and the Friday ride he had not asked for came back from. He was then asked to
    confirm which week he meant, three times.

    Precedence:
      1. an OUTSTANDING ask - a recorded fact about the week actually under discussion;
      2. "next week" in the message;
      3. "this week" in the message on a Mon-Sat (as `target_week`);
      4. Mon-Thu, and the message names a day still to come this week - the week IN
         PROGRESS. "Monday rest, Tuesday swim..." typed on a Monday is about today;
      5. otherwise the next Monday.

    Rule 4 can still read a week wrong, so it is deliberately paired with the caller's
    read-back ("Recorded for w/c ..."): a wrong week is then visible at a glance and
    correctable in one message, which is the standard this module holds itself to - never
    discard a declaration, but never hide which week it landed on either.
    """
    t = today or date.today()
    pending = outstanding_ask_week(slug, now=now, base=base)
    if pending:
        return pending
    low = (text or "").lower()
    if _NEXT_WEEK_RE.search(low):
        return t + timedelta(days=(7 - t.weekday()) % 7 or 7)
    if _THIS_WEEK_RE.search(low) and t.weekday() != 6:
        return _monday(t)
    if t.weekday() <= _DAY_SHAPE_CURRENT_WEEK_LAST_WD:
        ahead = [d for d in named_days or ()
                 if _canon_day(str(d)) in _DAY_ORDER
                 and _DAY_ORDER.index(_canon_day(str(d))) >= t.weekday()]
        if ahead:
            return _monday(t)
    return t + timedelta(days=(7 - t.weekday()) % 7 or 7)


# ---------------------------------------------------------------------------
# CONFIRMING BACK
# ---------------------------------------------------------------------------
# Written by hand per coaching level for the same reason `_ASK` is: these are
# deterministic strings, and coaching_levels.level_block() only shapes LLM prompts, so
# it cannot reach them. Calum is `beginner` — no Load/TSS/ceiling framing at all.
# The confirmation states the figure, the constraints as STORED, and the consequence,
# so a mis-parse is visible in the athlete's next glance rather than in next week's plan.

_CONFIRM = {
    "beginner": "👍 Got it — *{hours:g} hours* next week{cons}. I'll build the week around that.",
    "mid":      "👍 Logged — *{hours:g} hours* next week{cons}. I'll build to that this evening.",
    "pro":      ("👍 Logged — *{hours:g} hours* next week{cons}. The weekly Load ceiling "
                 "comes off that figure; I build this evening."),
}
# Said INSTEAD of "I build this evening" once the 18:00 build has already run. Recording
# a correction is right; silently rebuilding a week the athlete has already been sent is
# not (spec item 5), so the rebuild is offered and not taken.
_CONFIRM_LATE = {
    "beginner": ("👍 Got it — *{hours:g} hours* next week{cons}. Next week is already put "
                 "together, so say the word and I'll redo it to fit."),
    "mid":      ("👍 Logged — *{hours:g} hours* next week{cons}. Next week is already built, "
                 "so tell me if you want it rebuilt to that figure."),
    "pro":      ("👍 Logged — *{hours:g} hours* next week{cons}. Next week is already built "
                 "against the old ceiling — say the word and I'll rebuild to this one."),
}


def confirmation(hours: float, constraints: str = "", *, coaching_level: str = "mid",
                 after_build: bool = False) -> str:
    """The one-line read-back. `after_build` switches to the form that offers a rebuild
    rather than promising one."""
    table = _CONFIRM_LATE if after_build else _CONFIRM
    tmpl = table.get(coaching_level, table["mid"])
    cons = f", noted: _{constraints.strip()}_" if (constraints or "").strip() else ""
    return tmpl.format(hours=hours, cons=cons)


# ---------------------------------------------------------------------------
# DAY-SHAPE DECLARATIONS (added 2026-08-03)
# ---------------------------------------------------------------------------
# `record()` has always accepted swim_days / bike_days / run_days /
# unavailable_days, but nothing ever produced them: the only capture path was
# parse_hours_message, which needs a NUMBER. So a message like
#
#   "Monday rest Tuesday swim morning long run evening, Wednesday swim.
#    Thursday long ride. Friday/Saturday run Sunday rest. (Travelling)"
#
# (Jamie, 1 Aug 2026) carried a complete week shape and no hours figure, matched
# nothing, and was written only into current-state.md prose. stage1-plan.py does
# not read prose or persistent-rules.md - it reads this file - so the generator
# fell back to the default day_rules and produced a Friday-threshold /
# Saturday-long-ride week. Jamie then had to restate his availability on 27 Jul,
# 1 Aug, 2 Aug and 3 Aug. No standing rule could have fixed that; it is a write
# bug, and this is the missing write.
#
# Discipline matches the hours tiers: a wrong declaration silently becomes the
# planner's constraint set, so detection demands MULTIPLE named days each
# carrying a sport or rest, and bails on anything interrogative.

_DAY_CANON = {
    "mon": "Mon", "monday": "Mon", "tue": "Tue", "tues": "Tue", "tuesday": "Tue",
    "wed": "Wed", "weds": "Wed", "wednesday": "Wed", "thu": "Thu", "thur": "Thu",
    "thurs": "Thu", "thursday": "Thu", "fri": "Fri", "friday": "Fri",
    "sat": "Sat", "saturday": "Sat", "sun": "Sun", "sunday": "Sun",
}
_DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_TOKEN_RE = re.compile(
    r"\b(mon|monday|tues?|tuesday|weds?|wednesday|thur?s?|thursday|fri|friday|"
    r"sat|saturday|sun|sunday)\b", re.I)

# Sport cues. Order matters only for readability; a segment may carry several.
_SPORT_CUES = (
    ("swim_days", re.compile(r"\b(swim|swimming|pool|css|ows)\b", re.I)),
    ("bike_days", re.compile(r"\b(bike|ride|riding|cycl\w*|turbo|spin|brick)\b", re.I)),
    ("run_days",  re.compile(r"\b(run|running|jog\w*|tempo run|long run)\b", re.I)),
)

# A NEGATED sport mention within a segment ("run not ride", "swim, no turbo"). Stripped
# before the cues above are matched, otherwise "Fri and Sat I run not ride" reads the
# "ride" inside "not ride" as a bike cue and adds Fri/Sat to bike_days in the very
# message that excludes them from it — silently reinstating the day the athlete just
# said was out (Jamie, "Thu is my only bike day ... Fri and Sat I run not ride").
_NEG_SPORT_CUE_RE = re.compile(
    r"\b(?:not|no|n't|without)\s+(?:any\s+)?"
    r"(?:swim\w*|pool|css|ows|bike|ride[sd]?|riding|cycl\w*|turbo|spin|brick|"
    r"run\w*|jog\w*)\b", re.I)
_REST_RE = re.compile(r"\b(rest|off|nothing|no training|day off|travel\w*)\b", re.I)
# Anything that means "this is not a declaration".
_DAY_SHAPE_DISQUALIFY_RE = re.compile(
    r"\?|\b(what|when|which|how|why|shall|should i|can i|did|does|was|were|"
    r"remind me|talk me through|debrief)\b", re.I)
# "Thursday till Sunday", "Thu-Sun", "Friday/Saturday"
_RANGE_RE = re.compile(
    r"\b(\w+?)\s*(?:till|til|to|through|thru|-|–|/)\s*(\w+?)\b", re.I)


def _canon_day(tok: str) -> str | None:
    return _DAY_CANON.get((tok or "").strip().lower().rstrip("."))


def _expand_range(a: str, b: str) -> list[str]:
    """Mon..Sun inclusive span between two day tokens, [] if either is not a day."""
    ca, cb = _canon_day(a), _canon_day(b)
    if not ca or not cb:
        return []
    i, j = _DAY_ORDER.index(ca), _DAY_ORDER.index(cb)
    if i <= j:
        return _DAY_ORDER[i:j + 1]
    return _DAY_ORDER[i:] + _DAY_ORDER[:j + 1]


def parse_day_shape_message(text: str) -> dict:
    """Pull a per-day sport shape out of free text.

    Returns {"swim_days", "bike_days", "run_days", "unavailable_days", "named_days",
    "days_named", "framed"}. Day lists are canonical "Mon".."Sun" and de-duplicated in
    week order. `days_named` counts every distinct day mentioned, so the caller can insist
    on a real week shape rather than a single-day aside. `named_days` is that same set of
    days, and it is what `merge_day_rules` needs: a day the athlete NAMED is a day the
    standing day_rules no longer decide, even when the sport cue for it did not parse.
    Never raises.
    """
    out = {"swim_days": [], "bike_days": [], "run_days": [],
           "unavailable_days": [], "named_days": [], "days_named": 0, "framed": False}
    if not text or not text.strip():
        return out
    # Parenthesised asides are dropped before anything is attributed. Without this,
    # "Wednesday swim (will be tired after run)" marks Wednesday as a RUN day, because
    # the aside sits inside Wednesday's segment - a real misparse of Jamie's 1 Aug
    # message, and precisely the kind that silently becomes the planner's day_rules.
    t = re.sub(r"\([^)]*\)", " ", str(text))
    out["framed"] = bool(re.search(
        r"\b(next week|this week|the week|week of|availability|available)\b", t, re.I))

    matches = list(_DAY_TOKEN_RE.finditer(t))
    if not matches:
        return out

    # Group adjacent day tokens separated ONLY by a joiner, so "Friday/Saturday run"
    # and "Thursday till Sunday" share the sport that follows the group. Attributing
    # to the last day alone (the naive reading) dropped Friday's run entirely.
    _JOIN_ONLY = re.compile(r"^\s*(?:till|til|to|through|thru|and|[-–/,&])\s*$", re.I)
    groups: list[tuple[list[str], int]] = []   # (days in group, end offset of group)
    i = 0
    while i < len(matches):
        j = i
        while (j + 1 < len(matches)
               and _JOIN_ONLY.match(t[matches[j].end():matches[j + 1].start()])):
            j += 1
        if j > i:
            # A span joiner ("till"/"to"/"-") fills the range; a list joiner
            # ("/", "and", ",") takes only the named days.
            gap = t[matches[i].end():matches[i + 1].start()]
            if re.search(r"till|til|to|through|thru|[-–]", gap, re.I):
                days = _expand_range(matches[i].group(0), matches[j].group(0))
            else:
                days = [_canon_day(m.group(0)) for m in matches[i:j + 1]]
        else:
            days = [_canon_day(matches[i].group(0))]
        days = [d for d in days if d]
        if days:
            groups.append((days, matches[j].end()))
        i = j + 1

    named: list[str] = []
    for gi, (days, end) in enumerate(groups):
        seg_end = len(t)
        if gi + 1 < len(groups):
            # Segment runs to the first day token of the NEXT group.
            nxt = groups[gi + 1]
            m = _DAY_TOKEN_RE.search(t, end)
            seg_end = m.start() if m else nxt[1]
        seg = t[end:seg_end]
        for d in days:
            if d not in named:
                named.append(d)
        hit = False
        cue_seg = _NEG_SPORT_CUE_RE.sub(" ", seg)
        for key, rx in _SPORT_CUES:
            if rx.search(cue_seg):
                hit = True
                for d in days:
                    if d not in out[key]:
                        out[key].append(d)
        if not hit and _REST_RE.search(seg):
            for d in days:
                if d not in out["unavailable_days"]:
                    out["unavailable_days"].append(d)
    for key in ("swim_days", "bike_days", "run_days", "unavailable_days"):
        out[key] = [d for d in _DAY_ORDER if d in out[key]]
    out["named_days"] = [d for d in _DAY_ORDER if d in named]
    out["days_named"] = len(named)
    return out


def looks_like_day_shape_declaration(text: str) -> bool:
    """True only for a message that plainly lays out a week's shape.

    Deliberately strict, because a false positive rewrites the planner's day_rules:
    at least THREE distinct days named, at least TWO of them carrying a sport or an
    explicit rest, and nothing interrogative anywhere in the message. "Long ride is
    now Friday. Can brick tomorrow?" is a question and a single day, so it fails on
    both counts and is left to the day-override path.
    """
    if not text or _DAY_SHAPE_DISQUALIFY_RE.search(text):
        return False
    p = parse_day_shape_message(text)
    if p["days_named"] < 3:
        return False
    assigned = {d for k in ("swim_days", "bike_days", "run_days", "unavailable_days")
                for d in p[k]}
    return len(assigned) >= 2


def day_shape_summary(p: dict) -> str:
    """One-line readback of a parsed shape, for the confirmation message. Named so the
    caller never hand-formats it (rule S38: write the file, then restate what saved)."""
    bits = []
    for key, label in (("swim_days", "swim"), ("bike_days", "bike"),
                       ("run_days", "run"), ("unavailable_days", "rest")):
        if p.get(key):
            bits.append(f"{label} {'/'.join(p[key])}")
    return "; ".join(bits) if bits else "no days resolved"


# ---------------------------------------------------------------------------
# PRECEDENCE: DECLARATION vs STANDING day_rules (added 2026-08-10)
# ---------------------------------------------------------------------------
# Jamie, 10 Aug 2026: "Are you stupid... I already told you what my availability was for
# this week. Find it and sort your shit out." His 1 Aug declaration ("Monday rest, Tuesday
# swim morning long run evening, Wednesday swim... Thursday long ride. Friday/Saturday run.
# Sunday rest") was acknowledged, then dropped as junk because it clashed with his standing
# day_rules, and the week was rebuilt off the day_rules - three times in one conversation.
# The coach's own admission: "Your declaration outranks my day rules; I had it backwards."
#
# THE RULE, one place, so every consumer inherits it:
#   * the declaration is AUTHORITATIVE for every day it NAMES;
#   * day_rules fill ONLY the days it does not name;
#   * a sport is never added to a named day and never moved off one. Duration and
#     intensity remain the engine's to move; the sport-to-day mapping does not;
#   * a clash is REPORTED, never silently resolved either way.
#
# WHY THE OLD MERGE WAS NOT ENOUGH. session_library.reconcile_day_rules replaced day_rules
# PER SPORT KEY, so a declaration naming only "Wednesday swim" replaced swim_days and left
# run_days = [Tue, Wed, Sat, Sun] untouched - a run on the Wednesday the athlete had given
# to a swim. The same hole ran through `swim_focus`: Jamie's {"Thu": ["css"]} kept a CSS
# swim on the Thursday he declared as his long ride.
#
# A KEY PRESENT BUT EMPTY IS A WHOLE-WEEK EXCLUSION, not a per-day statement. That is how
# parse_sport_exclusion_message records "no cycling this week" (Kathryn, 12 Jul 2026:
# auto-sync put cycling on Thu and Sat against a week-specific agreement to drop it), and
# per-day precedence must not quietly reinstate it from day_rules.

_SPORT_DAY_KEYS = ("swim_days", "bike_days", "run_days")
_SPORT_LABEL = {"swim_days": "swim", "bike_days": "bike", "run_days": "run"}


def _canon_or_raw(x) -> str:
    """Canonical "Mon".."Sun", or the token unchanged when it is not a day name. Unknown
    tokens are kept rather than dropped: day_rules is hand-maintained config and silently
    deleting something we failed to recognise would widen a week."""
    return _canon_day(str(x)) or str(x)


def _order_days(days) -> list[str]:
    """De-duplicated, week order, with any non-day token appended in first-seen order."""
    seen: list[str] = []
    for d in days:
        if d not in seen:
            seen.append(d)
    known = [d for d in _DAY_ORDER if d in seen]
    return known + [d for d in seen if d not in _DAY_ORDER]


def declared_days(declaration: dict | None) -> list[str]:
    """Every day the athlete NAMED in this declaration, canonical and in week order.

    Read from the stored `declared_days` when the record carries it (the chat capture
    passes parse_day_shape_message's `named_days`), else the union of the sport lists and
    `unavailable_days`. The union is what a hand-written record, the `--availability` JSON
    path in stage1-plan.py and a legacy Phase 5a flat file all give. It differs from the
    stored list only for a day the athlete mentioned WITHOUT a sport or rest cue, and such
    a day carries no instruction to enforce, so the fallback is safe rather than merely
    convenient: a legacy file of {"unavailable_days": ["Wed"]} names Wed and nothing else,
    which is exactly today's behaviour.
    """
    if not isinstance(declaration, dict):
        return []
    explicit = declaration.get("declared_days")
    if isinstance(explicit, list):
        src = explicit
    else:
        src = [d for k in _SPORT_DAY_KEYS + ("unavailable_days",)
               if isinstance(declaration.get(k), list)
               for d in declaration[k]]
    return _order_days(_canon_day(str(d)) for d in src if _canon_day(str(d)))


def excluded_sports(declaration: dict | None) -> list[str]:
    """The day_rules keys the athlete has taken OFF for the whole week.

    Named explicitly by `excluded_sports` (what the sport-exclusion capture writes), and
    that explicitness is load-bearing: parse_day_shape_message returns ALL FOUR day keys
    every time, so a week whose declaration happens to name no bike day arrives with
    `bike_days: []`. Reading that as "no cycling this week" would delete the bike from
    every partial declaration - "Monday rest, Tuesday swim, Wednesday swim" would silently
    cancel Jamie's Friday, Saturday and Sunday rides.

    LEGACY RECORDS ONLY: a record carrying neither `excluded_sports` nor `declared_days`
    predates both keys, and for it an empty list DID mean the sport was replaced wholesale
    (the old per-sport reconciliation). Honoured so a declaration already on disk keeps the
    meaning it was written with; such records expire with the week they name.
    """
    if not isinstance(declaration, dict):
        return []
    named = declaration.get("excluded_sports")
    if isinstance(named, list):
        return [k for k in _SPORT_DAY_KEYS if k in named]
    if "declared_days" in declaration:
        return []
    return [k for k in _SPORT_DAY_KEYS if declaration.get(k) == []]


def _declared_sports_by_day(declaration: dict) -> dict[str, list[str]]:
    """day -> the sports the athlete gave that day, for the conflict wording."""
    out: dict[str, list[str]] = {}
    for key in _SPORT_DAY_KEYS:
        for d in (declaration.get(key) or []):
            out.setdefault(_canon_or_raw(d), []).append(_SPORT_LABEL[key])
    for d in (declaration.get("unavailable_days") or []):
        out.setdefault(_canon_or_raw(d), []).append("rest")
    return out


def merge_day_rules(day_rules: dict | None, declaration: dict | None, *,
                    run_limited: bool = False) -> tuple[dict | None, list[str]]:
    """Fold one week's declaration into the athlete's standing day_rules.

    Returns (effective_rules, conflicts). `conflicts` is prose the caller must SHOW - each
    line names the day, what the athlete declared and what day_rules wanted instead, so the
    coach can tell the athlete rather than either side being rewritten in silence.

    No declaration returns `day_rules` unchanged (the same object, as before), so an
    athlete who has said nothing this week is unaffected.
    """
    if not declaration:
        return day_rules, []
    dr = json.loads(json.dumps(day_rules or {}))
    named = declared_days(declaration)
    by_day = _declared_sports_by_day(declaration)
    conflicts: list[str] = []
    reported: set[tuple[str, str]] = set()

    def _clash(day: str, key: str) -> None:
        if (day, key) in reported:
            return
        reported.add((day, key))
        gave = "/".join(by_day.get(day) or []) or "nothing"
        conflicts.append(
            f"{day}: you declared {gave}; your standing day_rules put a "
            f"{_SPORT_LABEL[key]} there. Your declaration wins for this week, so the "
            f"{_SPORT_LABEL[key]} is not on {day}.")

    off = excluded_sports(declaration)
    for key in _SPORT_DAY_KEYS:
        v = declaration.get(key)
        if not isinstance(v, list) and key not in off:
            continue                    # sport not spoken about: day_rules stand entire
        standing = [_canon_or_raw(x) for x in (dr.get(key) or [])]
        wanted = [_canon_or_raw(x) for x in (v or [])]
        if key in off:
            dr[key] = []
            if standing:
                conflicts.append(
                    f"{_SPORT_LABEL[key]}: declared off for the whole week; your standing "
                    f"day_rules had {'/'.join(_order_days(standing))}. The declaration wins.")
                for d in standing:
                    reported.add((d, key))
            continue
        for d in standing:
            if d in named and d not in wanted:
                _clash(d, key)
        dr[key] = _order_days(wanted + [d for d in standing if d not in named])

    for d in (declaration.get("unavailable_days") or []):
        day = _canon_or_raw(d)
        for key in _SPORT_DAY_KEYS:
            if not isinstance(dr.get(key), list):
                continue
            if day in [_canon_or_raw(x) for x in dr[key]]:
                _clash(day, key)
            dr[key] = [x for x in dr[key] if _canon_or_raw(x) != day]

    if run_limited:
        # The rehab structure is a FLOOR (swim_focus days kept, run frequency never
        # raised) - but only over days the athlete did NOT speak about. Re-adding a swim
        # to a day they declared as a rest or a ride is the invention this module exists
        # to stop, so those days are reported instead of overridden.
        base = day_rules or {}
        sf = base.get("swim_focus") or {}
        if sf and isinstance(dr.get("swim_days"), list):
            for wd in sf:
                day = _canon_or_raw(wd)
                if day in named:
                    # Reported once, by the swim_focus prune below - not twice.
                    continue
                if day not in [_canon_or_raw(x) for x in dr["swim_days"]]:
                    dr["swim_days"] = _order_days([_canon_or_raw(x) for x in dr["swim_days"]]
                                                  + [day])
        if isinstance(base.get("run_days"), list) and isinstance(dr.get("run_days"), list):
            budget = len(base["run_days"])
            if len(dr["run_days"]) > budget:
                # Trim the days the athlete did NOT name first. The old slice took the
                # front of the list, which could cut a declared run day to make room for a
                # standing one - the declaration losing to the very rules it outranks.
                keep = [d for d in dr["run_days"] if _canon_or_raw(d) in named]
                spare = [d for d in dr["run_days"] if _canon_or_raw(d) not in named]
                if len(keep) > budget:
                    # The athlete has declared MORE run days than the rehab pattern runs.
                    # They win, and that is exactly the case the coach must raise with them
                    # rather than have the floor trim it away unseen.
                    conflicts.append(
                        f"run: you declared {len(keep)} run days "
                        f"({'/'.join(_order_days(keep))}); your rehab pattern runs "
                        f"{budget}. Your declaration stands - flag the extra frequency.")
                dr["run_days"] = _order_days(keep + spare[:max(0, budget - len(keep))])

    # swim_focus is a day-keyed map, so it is a second place a sport can land on a day.
    # Prune it to the days that survived, or Jamie's {"Thu": ["css"]} puts a CSS swim on
    # the Thursday he declared as his long ride.
    sf = dr.get("swim_focus")
    if isinstance(sf, dict) and isinstance(dr.get("swim_days"), list):
        keep_days = [_canon_or_raw(x) for x in dr["swim_days"]]
        dropped = [k for k in sf if _canon_or_raw(k) not in keep_days]
        for k in dropped:
            day = _canon_or_raw(k)
            focus = "/".join(sf[k]) if isinstance(sf[k], list) else str(sf[k])
            conflicts.append(
                f"{day}: your standing swim focus ({focus}) is dropped this week - "
                f"you declared {'/'.join(by_day.get(day) or ['nothing'])} on {day}.")
        dr["swim_focus"] = {k: v for k, v in sf.items() if k not in dropped}
    # Day order, because these lines are read out to the athlete as a week. Sport-key
    # iteration order would list Thursday's clash before Wednesday's.
    conflicts.sort(key=lambda c: _DAY_ORDER.index(c.split(":")[0])
                   if c.split(":")[0] in _DAY_ORDER else -1)
    return dr, conflicts


def effective_day_rules(slug: str, week_start: date | str | None, day_rules: dict | None,
                        *, run_limited: bool = False,
                        base: Path | str | None = None) -> tuple[dict | None, list[str]]:
    """merge_day_rules against whatever the athlete declared for THAT week.

    The one call a consumer needs. plan_builder validates with this rather than raw
    config, so a declared move (Jamie's Thursday long ride against bike_days
    [Fri,Sat,Sun]) is not a hard `ride_forbidden_day` that fails every attempt - the
    "no clean week EXISTS" loop stage1-plan.py already documents for Kathryn.
    """
    return merge_day_rules(day_rules, day_shape(slug, week_start, base),
                           run_limited=run_limited)


# ---------------------------------------------------------------------------
# WHOLE-WEEK SPORT EXCLUSIONS (added 2026-08-03)
# ---------------------------------------------------------------------------
# Kathryn, 12 Jul 2026: "Auto-sync overwrote the explicitly-agreed locked week
# (13-19 Jul) with a generic template that put cycling on Thu and Sat, violating
# standing no-cycling constraints."
#
# Her STANDING bike_days are ["Tue","Thu","Sat"] - Thu and Sat are legitimately
# permitted - so the auto-sync was not wrong about the standing shape. What it never
# saw was a WEEK-SPECIFIC agreement to drop cycling, because that agreement lived only
# in conversation. stage1-plan.py flexes day_rules from weekly_availability.day_shape()
# for the week being planned, so a recorded declaration would have held; nothing wrote
# one.
#
# parse_day_shape_message needs THREE named days, so it cannot catch this: "no cycling
# this week" names none. This is the negative form - a sport removed for one week.
#
# Deliberately requires explicit WEEK framing. "No cycling today" is a single-day
# deviation and belongs to lib/day_overrides.py, not here; recording it as a week
# exclusion would silently delete two other sessions.

_SPORT_TO_DAYKEY = {
    "swim": "swim_days", "swimming": "swim_days", "pool": "swim_days",
    "bike": "bike_days", "biking": "bike_days", "cycling": "bike_days",
    "cycle": "bike_days", "ride": "bike_days", "rides": "bike_days",
    "riding": "bike_days", "turbo": "bike_days",
    "run": "run_days", "runs": "run_days", "running": "run_days",
}
_WEEK_FRAMED_RE = re.compile(r"\b(this week|next week|the week|all week|whole week)\b", re.I)
_NEG_SPORT_RE = re.compile(
    r"\b(?:no|not|zero|avoid|skip|drop|without)\s+"
    r"(?:any\s+|more\s+)?"
    r"(swim\w*|pool|bike\w*|cycl\w*|rid\w*|turbo|run\w*)\b", re.I)
# A question or a report is not a declaration.
_EXCL_DISQUALIFY_RE = re.compile(
    r"\?|\b(why|what|when|shall|should i|can i|did i|was there|how come)\b", re.I)


def parse_sport_exclusion_message(text: str) -> dict:
    """Sports removed for a whole week. {"bike_days": [], ...} plus "framed".

    Returns only the keys that were explicitly excluded, each mapped to an EMPTY list,
    which is how `record` stores "declared, and it is none" as distinct from absent.
    Never raises.
    """
    out: dict = {"framed": False, "excluded": {}}
    if not text or not text.strip():
        return out
    t = re.sub(r"\([^)]*\)", " ", str(text))
    out["framed"] = bool(_WEEK_FRAMED_RE.search(t))
    for m in _NEG_SPORT_RE.finditer(t):
        word = m.group(1).lower()
        key = _SPORT_TO_DAYKEY.get(word)
        if key is None:                       # stemmed forms: cycling/riding/running…
            for stem, k in _SPORT_TO_DAYKEY.items():
                if word.startswith(stem[:4]):
                    key = k
                    break
        if key:
            out["excluded"][key] = []
    return out


def looks_like_sport_exclusion(text: str) -> bool:
    """True only for an unambiguous whole-week sport exclusion.

    Requires explicit week framing AND at least one negated sport AND nothing
    interrogative, because the consequence of a false positive is deleting a sport from
    a week's plan.
    """
    if not text or _EXCL_DISQUALIFY_RE.search(text):
        return False
    p = parse_sport_exclusion_message(text)
    return bool(p["framed"] and p["excluded"])


def sport_exclusion_summary(excluded: dict) -> str:
    """One-line readback for the confirmation message."""
    label = {"swim_days": "swim", "bike_days": "bike", "run_days": "run"}
    names = [label.get(k, k) for k in ("swim_days", "bike_days", "run_days")
             if k in (excluded or {})]
    return ("no " + "/".join(names)) if names else "nothing excluded"
