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


def _is_legacy_flat(raw: dict) -> bool:
    """A pre-existing Phase 5a file: an object with day-shape keys and no
    `declarations` list and no `week_start`."""
    return bool(raw) and not _declarations(raw) and not raw.get("week_start")


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
        shape = {k: d[k] for k in ("swim_days", "bike_days", "run_days", "unavailable_days")
                 if isinstance(d.get(k), list)}
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
           source: str = "manual", base: Path | str | None = None,
           **day_keys) -> dict:
    """Write (or replace) the declaration for one week. Returns the stored record.

    Raises ValueError on an out-of-band `hours`, rather than storing a figure that
    would silently become somebody's training ceiling.
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
    for k in ("swim_days", "bike_days", "run_days", "unavailable_days"):
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
    decls.append(rec)
    decls.sort(key=lambda d: str(d.get("week_start")))
    out["declarations"] = decls[-_KEEP:]
    _atomic_write(path_for(slug, base), out)
    return rec


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
