"""Menstrual-cycle state and phase rules, shared by the cron scripts and bot.

Three layers, mirroring lib/heat.py:
  - profile.json `menstrual_tracking: true` — athlete-level opt-in (absent/False = off
    entirely; the planner, prescription and morning card all stay silent)
  - profile.json `cycle_length_days` — optional default cycle length (28 if absent)
  - current-state.json `menstrual_cycle` block — the dynamic state, athlete-logged via
    the Telegram bot ("period started" / "cycle day N"):
      {"last_period_start": "2026-06-10", "cycle_length_days": 28,
       "starts": ["2026-05-13", "2026-06-10"]}

Phase model (standard cycle physiology — the luteal phase is ~fixed at 14 days, so
ovulation day scales with cycle length; cues match the coach's table in Kathryn's
current-state.md):
  menstrual   days 1-5
  follicular  day 6 to ovulation-1
  ovulation   day (cycle_length - 14)  -> day 14 of a 28-day cycle
  luteal      ovulation+1 to cycle_length

The anchor is ONLY moved by a logged period start (bot or coach edit) — never by
prediction arithmetic. Past the predicted next start the phase clamps to luteal and
`overdue` is set so the morning card can ask; >7 days overdue the phase is withheld
entirely (a stale anchor must not feed wrong phases into planning).

intervals.icu wellness rows natively carry a `menstrualPhase` field (athlete-logged
in ICU / synced from Garmin). When a row dated today has it, it overrides the
computed phase for that day only. Field values are mapped defensively (enum not yet
observed from a real athlete log — verify on first live data).
"""
import json
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/

DEFAULT_CYCLE_LEN = 28
MENSTRUAL_END_DAY = 5     # days 1-5 = menstrual phase
LUTEAL_LEN        = 14    # ovulation day = cycle_length - LUTEAL_LEN
MIN_CYCLE_LEN     = 21    # observed cycle lengths outside this band are ignored
MAX_CYCLE_LEN     = 35    #   (likely a missed log, not a real cycle)
STALE_AFTER_DAYS  = 7     # this many days past predicted next start -> phase unknown

CUES = {
    "menstrual":  "lower energy possible — hold targets loosely, RPE over pace/power",
    "follicular": "rising energy and strength — good window for quality sessions",
    "ovulation":  "peak performance window",
    "luteal":     ("higher core temp and RPE, greater fatigue — sessions feel harder "
                   "than the numbers suggest; watch heat stacking"),
}


def _state_file(slug: str) -> Path:
    return BASE / "athletes" / slug / "current-state.json"


def _load_state(slug: str) -> dict:
    f = _state_file(slug)
    try:
        return json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        return {}


def enabled(slug: str, profile: dict | None = None) -> bool:
    """Athlete-level opt-in: profile.json `menstrual_tracking: true`."""
    if profile is None:
        p = BASE / "athletes" / slug / "profile.json"
        try:
            profile = json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            profile = {}
    return bool(profile.get("menstrual_tracking"))


def cycle_state(slug: str, profile: dict | None = None) -> dict | None:
    """The athlete's cycle block {last_period_start, cycle_length_days, starts},
    or None if tracking is off or no period start has been logged yet."""
    if not enabled(slug, profile):
        return None
    mc = _load_state(slug).get("menstrual_cycle") or {}
    if not mc.get("last_period_start"):
        return None
    if not mc.get("cycle_length_days"):
        if profile is None:
            p = BASE / "athletes" / slug / "profile.json"
            try:
                profile = json.loads(p.read_text()) if p.exists() else {}
            except Exception:
                profile = {}
        mc["cycle_length_days"] = int(profile.get("cycle_length_days")
                                      or DEFAULT_CYCLE_LEN)
    return mc


def phase_from_day(day: int, cycle_len: int = DEFAULT_CYCLE_LEN) -> str:
    """Pure phase lookup for a 1-based cycle day (day > cycle_len clamps to luteal)."""
    ovulation_day = max(MENSTRUAL_END_DAY + 2, cycle_len - LUTEAL_LEN)
    if day <= MENSTRUAL_END_DAY:
        return "menstrual"
    if day < ovulation_day:
        return "follicular"
    if day == ovulation_day:
        return "ovulation"
    return "luteal"


_ICU_PHASE_MAP = (("PERIOD", "menstrual"), ("MENSTRU", "menstrual"),
                  ("FOLLIC", "follicular"), ("OVUL", "ovulation"),
                  ("LUTEAL", "luteal"))


def _icu_phase(wellness: list | None, on: date) -> str | None:
    """Phase from an intervals.icu wellness row dated `on`, or None."""
    for row in reversed(wellness or []):
        if str(row.get("id") or row.get("date") or "")[:10] != on.isoformat():
            continue
        raw = str(row.get("menstrualPhase") or "").upper()
        for prefix, phase in _ICU_PHASE_MAP:
            if raw.startswith(prefix):
                return phase
        return None
    return None


def phase_for(slug: str, on: date | None = None, profile: dict | None = None,
              wellness: list | None = None) -> dict | None:
    """{day, phase, cue, source, overdue, next_period_expected} for an athlete on a
    date, or None (tracking off, no anchor, or anchor too stale to trust).

    `wellness` (optional): intervals.icu wellness rows — a row dated `on` with a
    menstrualPhase value overrides the computed phase for that day only.
    """
    on = on or date.today()
    mc = cycle_state(slug, profile)
    if not mc:
        return None
    try:
        anchor = date.fromisoformat(str(mc["last_period_start"]))
    except (ValueError, TypeError):
        return None
    cycle_len = int(mc["cycle_length_days"])
    day = (on - anchor).days + 1
    if day < 1:
        return None                      # anchor in the future — bad log; stay silent
    overdue = day > cycle_len
    if day > cycle_len + STALE_AFTER_DAYS:
        return {"day": day, "phase": None, "cue": None, "source": "computed",
                "overdue": True,
                "next_period_expected": (anchor + timedelta(days=cycle_len)).isoformat()}
    phase = phase_from_day(min(day, cycle_len), cycle_len)
    source = "computed"
    icu = _icu_phase(wellness, on)
    if icu:
        phase, source = icu, "icu"
    return {"day": day, "phase": phase, "cue": CUES.get(phase), "source": source,
            "overdue": overdue,
            "next_period_expected": (anchor + timedelta(days=cycle_len)).isoformat()}


def log_period_start(slug: str, start: date, profile: dict | None = None) -> dict:
    """Record a period start (the bot's 'period started' path). Moves the anchor,
    appends to the starts history, and re-derives cycle_length_days as the mean of
    the last 3 plausible observed gaps (configured/default length until 2+ starts).
    Returns the updated menstrual_cycle block. Caller persists nothing — this writes
    current-state.json itself."""
    state = _load_state(slug)
    mc = state.setdefault("menstrual_cycle", {})
    starts = [s for s in (mc.get("starts") or []) if s != start.isoformat()]
    # A coach-seeded anchor may predate the starts history — keep its gap observable.
    if mc.get("last_period_start") and mc["last_period_start"] not in starts \
            and mc["last_period_start"] != start.isoformat():
        starts.append(mc["last_period_start"])
    starts.append(start.isoformat())
    starts = sorted(set(starts))[-12:]
    mc["starts"] = starts
    mc["last_period_start"] = starts[-1]

    gaps = []
    ds = []
    for s in starts:
        try:
            ds.append(date.fromisoformat(s))
        except ValueError:
            continue
    for a, b in zip(ds, ds[1:]):
        gap = (b - a).days
        if MIN_CYCLE_LEN <= gap <= MAX_CYCLE_LEN:
            gaps.append(gap)
    if gaps:
        mc["cycle_length_days"] = round(sum(gaps[-3:]) / len(gaps[-3:]))
    elif not mc.get("cycle_length_days"):
        if profile is None:
            p = BASE / "athletes" / slug / "profile.json"
            try:
                profile = json.loads(p.read_text()) if p.exists() else {}
            except Exception:
                profile = {}
        mc["cycle_length_days"] = int(profile.get("cycle_length_days")
                                      or DEFAULT_CYCLE_LEN)

    state["last_updated"] = date.today().isoformat()
    _state_file(slug).write_text(json.dumps(state, indent=2))
    return mc


def set_cycle_day(slug: str, day: int, on: date | None = None) -> dict:
    """Correct the anchor from a 'cycle day N' message: anchor = on - (N-1) days.
    A correction, not an observed start — the starts history is left alone."""
    on = on or date.today()
    state = _load_state(slug)
    mc = state.setdefault("menstrual_cycle", {})
    mc["last_period_start"] = (on - timedelta(days=day - 1)).isoformat()
    mc.setdefault("cycle_length_days", DEFAULT_CYCLE_LEN)
    state["last_updated"] = date.today().isoformat()
    _state_file(slug).write_text(json.dumps(state, indent=2))
    return mc


def forecast_block(slug: str, start: date, days: int = 14,
                   profile: dict | None = None) -> str:
    """Compact phase forecast for a planning window — one line per contiguous phase
    run, for injection into the generate-plan prompt. "" if tracking is off/unset."""
    mc = cycle_state(slug, profile)
    if not mc:
        return ""
    runs = []        # [phase, first_date, last_date, first_day, last_day]
    for i in range(days):
        d = start + timedelta(days=i)
        info = phase_for(slug, d, profile)
        if not info or not info["phase"]:
            continue
        if runs and runs[-1][0] == info["phase"]:
            runs[-1][2], runs[-1][4] = d, info["day"]
        else:
            runs.append([info["phase"], d, d, info["day"], info["day"]])
    if not runs:
        return ""
    WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    lines = []
    for phase, d1, d2, day1, day2 in runs:
        span = (f"{WD[d1.weekday()]} {d1.isoformat()}" if d1 == d2 else
                f"{WD[d1.weekday()]} {d1.isoformat()} → {WD[d2.weekday()]} {d2.isoformat()}")
        days_lbl = f"day {day1}" if day1 == day2 else f"days {day1}–{day2}"
        lines.append(f"  {span}: {phase.upper()} ({days_lbl}) — {CUES[phase]}")
    return "\n".join(lines)
