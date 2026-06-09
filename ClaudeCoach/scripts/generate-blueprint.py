#!/usr/bin/env python3
"""
Generate a personalised training blueprint for an athlete.

Usage:
  python3 ClaudeCoach/scripts/generate-blueprint.py --athlete {slug}
  python3 ClaudeCoach/scripts/generate-blueprint.py --athlete {slug} --fitness-choice B

Reads:
  ClaudeCoach/athletes/{slug}/profile.json
  Live CTL via icu_fetch.py

Writes:
  ClaudeCoach/athletes/{slug}/reference/training-blueprint.md
"""
import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)

# Validate the structured sidecar against the shared schema (remediation WS B).
sys.path.insert(0, str(BASE / "ironman-analysis"))
from primitives.blueprint import (  # noqa: E402
    validate_blueprint, canonical_phases, resolve_phases,
    phase_structure, assign_dates, SCHEMA_VERSION,
    EVENT_SPORTS, CYCLING_EVENTS, event_sports,
    event_key as _event_key,
)

# Brick targets by phase family — single source for both the markdown table and
# the structured sidecar.
BRICK_MIN = {"base": "1", "build": "2–3", "peak": "3–4", "taper": "1"}
BRICK_TYPE = {
    "base":  "Short brick (30–45 min ride + 10–20 min easy run)",
    "build": "Standard and quality bricks",
    "peak":  "Include at least 1 long brick (race simulation)",
    "taper": "Short brick only; no fatigue accumulation",
}


# -- Performance-test policy --------------------------------------------------
# The coach's standing decision (Jamie, 2026-06-08): do NOT schedule or nag
# athletes to run formal FTP / LTHR / CSS field tests. Thresholds come from
# intervals.icu directly — cycling FTP from eFTP, run threshold pace and swim CSS
# from intervals' own estimates off normal training data. If an athlete DOES log a
# test (e.g. names a ride "FTP test"), _resolve_ftp still uses it — we just never
# schedule one, push test events to the calendar, or send "test due" reminders.
# Flip to True to restore the scheduled-testing programme.
SCHEDULE_PERFORMANCE_TESTS = False


# -- Mesocycle algorithm ------------------------------------------------------
# phase_structure / assign_dates / resolve_phases now live in
# primitives.blueprint — the single source shared with the planner. Imported at
# the top of this module (re-exported here so gb.phase_structure still resolves).


# -- TSS ceiling --------------------------------------------------------------

IF_TARGETS = {
    "base":  0.65,
    "build": 0.68,
    "peak":  0.72,
}

def phase_family(name: str) -> str:
    n = name.lower()
    if "base" in n:
        return "base"
    if "specific" in n:
        return "specific"
    if "build" in n:
        return "build"
    if "peak" in n:
        return "peak"
    return "taper"


def _css_seconds(css) -> int | None:
    """Parse swim_css_per_100m to integer seconds.

    Tolerant of both representations seen in profiles: integer seconds (e.g. 99)
    and an "m:ss" string (e.g. "1:39"). Returns None if absent/unparseable.
    """
    if css is None or css == "":
        return None
    try:
        return int(css)
    except (ValueError, TypeError):
        try:
            mm, ss = str(css).split(":")
            return int(mm) * 60 + int(ss)
        except (ValueError, AttributeError):
            return None


def content_family(family: str) -> str:
    """Map a structural phase family to the family whose content tables apply.

    The blueprint's content tables (distribution, fuelling, IF, CTL ranges,
    bricks) are defined for base/build/peak/taper. A 'specific' phase is
    late-build race-specific work, so it reuses build-family content (its own
    CTL *target* still comes from athletes.json phase_ctl). See remediation
    decision 2026-06-07.
    """
    return "build" if family == "specific" else family

def tss_ceiling(max_hours: float, phase_name: str) -> float | None:
    fam = content_family(phase_family(phase_name))
    if fam == "taper":
        return None
    IF = IF_TARGETS[fam]
    return round(max_hours * 100 * IF ** 2, 0)


# Event → sports partition (EVENT_SPORTS, CYCLING_EVENTS, event_sports,
# _event_key) now lives in primitives.blueprint — the single source shared with
# the planner. Imported at the top of this module.


# -- CTL phase targets ---------------------------------------------------------

CTL_TARGETS = {
    "Full Ironman": {
        "base":  (55, 70),
        "build": (70, 85),
        "peak":  (80, 95),
        "taper": (85, 100),
    },
    "70.3": {
        "base":  (40, 55),
        "build": (55, 65),
        "peak":  (60, 75),
        "taper": (65, 80),
    },
}

def ctl_range(event: str, phase_name: str) -> tuple[int, int] | None:
    """Return (low, high) CTL target for entry to a phase, or None if unknown."""
    fam = content_family(phase_family(phase_name))
    return CTL_TARGETS.get(_event_key(event), {}).get(fam)


# -- Live CTL fetch ------------------------------------------------------------

def fetch_ctl(slug: str) -> float | None:
    try:
        result = subprocess.run(
            ["python3", "ClaudeCoach/lib/icu_fetch.py",
             "--athlete", slug, "--endpoint", "fitness", "--days", "3"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        # fitness endpoint returns a list; find most recent entry with ctl
        if isinstance(data, list) and data:
            for entry in reversed(data):
                ctl = entry.get("ctl")
                if ctl is not None:
                    return float(ctl)
    except Exception:
        pass
    return None


# -- Course modifier ----------------------------------------------------------

COURSE_NOTES = {
    "flat":         "Standard distribution applies.",
    "rolling":      "+5% Z3 bike in Build phases. Add 1×45 min sweet-spot per week.",
    "hilly":        "+10% Z4–5 bike in Build. Add climbing repeats (2×20 min Z4). Reduce volume by 5%.",
    "mountainous":  "Specialist climbing blocks. Reduce weekly volume 10%. All long rides on hilly terrain.",
}

def course_note(course_type: str) -> str:
    return COURSE_NOTES.get(course_type.lower(), "Standard distribution applies.")


# -- Heat protocol ------------------------------------------------------------

def heat_note(race_conditions: str, race_dt: date) -> str:
    if race_conditions != "hot":
        return "Not active."
    start = race_dt - timedelta(weeks=4)
    return (
        f"Active. Sauna protocol begins {start.isoformat()} "
        f"(3–4×/week, 20–30 min at 80–90°C, post-exercise). "
        f"Outdoor heat sessions: 2–3×/week in peak ambient ≥ 25°C from same date."
    )


# -- Fuelling targets ---------------------------------------------------------

FUELLING = {
    "Full Ironman": {
        "base":  "40–55 g CHO/hr on sessions > 60 min",
        "build": "60–75 g CHO/hr on sessions > 45 min",
        "peak":  "75–90 g CHO/hr; race-simulation sessions at race rate",
        "taper": "80–90 g CHO/hr on race-simulation sessions only",
    },
    "70.3": {
        "base":  "40–55 g CHO/hr on sessions > 60 min",
        "build": "55–65 g CHO/hr on sessions > 45 min",
        "peak":  "65–75 g CHO/hr; race-simulation sessions at race rate",
        "taper": "70–80 g CHO/hr on race-simulation sessions only",
    },
    "Sportive": {
        "base":  "40–55 g CHO/hr on rides > 60 min",
        "build": "55–65 g CHO/hr on rides > 45 min",
        "peak":  "60–75 g CHO/hr; long-ride/event-simulation at event rate",
        "taper": "60–75 g CHO/hr on event-simulation rides only",
    },
}

def fuelling_note(event: str, phase_name: str) -> str:
    fam = content_family(phase_family(phase_name))
    event_fuel = FUELLING.get(_event_key(event), FUELLING["Full Ironman"])
    return event_fuel.get(fam, "Follow phase-progressive protocol.")


# -- Test schedule ------------------------------------------------------------

ATHLETES_CONFIG = BASE / "config/athletes.json"


def _test_events(phases: list[dict], sports: list[str] | None = None) -> list[dict]:
    """Return structured performance tests with dates, only for the event's sports.

    sports defaults to full triathlon for backward compatibility. A bike-only
    event (Sportive) gets FTP tests only — no run LTHR or swim CSS.
    """
    if not SCHEDULE_PERFORMANCE_TESTS:
        # Policy: thresholds come from intervals.icu (eFTP / threshold pace / CSS),
        # never from a scheduled field test. No tests scheduled, pushed, or nudged.
        return []
    if sports is None:
        sports = ["swim", "bike", "run"]
    # Baselines anchor to the PLAN START, not date.today(). Anchoring to today
    # re-dated every baseline to the regeneration day on each run, so a mid-plan
    # athlete (who tested at the start and already has thresholds) got nudged to
    # "redo baseline test today" every time the blueprint was regenerated. With
    # plan-start anchoring, a mid-plan athlete's baselines sit in the past
    # (never "due"), while a genuinely new athlete's land at their start date.
    plan_start = phases[0]["start"] if phases else date.today()
    events: list[dict] = []

    def add(test_type: str, label: str, dt: date, protocol: str):
        events.append({
            "type": test_type, "label": label, "date": dt.isoformat(),
            "protocol": protocol, "notified": False, "completed": False,
        })

    build_phases = [p for p in phases if phase_family(p["name"]) == "build"]

    # FTP (cycling)
    if "bike" in sports:
        add("ftp", "FTP Baseline", plan_start, "20-min test × 0.95 or ramp test")
        for p in phases:
            fam = phase_family(p["name"])
            if fam == "base":
                mid = p["start"] + timedelta(weeks=p["weeks"] // 2)
                add("ftp", f"FTP Mid-{p['name']}", mid, "Ramp test")
            elif fam == "build" and build_phases and p == build_phases[-1]:
                add("ftp", "FTP End-Build", p["end"] - timedelta(days=2), "20-min test × 0.95")

    # LTHR (running)
    if "run" in sports:
        add("lthr", "LTHR Baseline", plan_start, "30-min run TT; avg HR final 20 min = LTHR")
        base_phases = [p for p in phases if "base" in p["name"].lower()]
        if base_phases:
            add("lthr", "LTHR End-Base", base_phases[-1]["end"] - timedelta(days=2),
                "30-min run TT; avg HR final 20 min = LTHR")

    # CSS (swimming)
    if "swim" in sports:
        add("css", "CSS Baseline", plan_start, "400m + 200m TT (CSS calculator)")
        if build_phases:
            add("css", "CSS Mid-Build", build_phases[0]["start"] + timedelta(weeks=2),
                "400m + 200m TT (CSS calculator)")

    return events


def push_test_events(slug: str, events: list[dict]) -> int:
    """Push events without an icu_event_id to the intervals.icu calendar. Returns count pushed."""
    if not ATHLETES_CONFIG.exists():
        return 0
    athletes = json.loads(ATHLETES_CONFIG.read_text())
    cfg = athletes.get(slug, {})
    icu_id = cfg.get("icu_athlete_id")
    icu_key = cfg.get("icu_api_key")
    if not icu_id or not icu_key:
        print(f"Warning: no icu credentials for {slug} — skipping event push.", file=sys.stderr)
        return 0

    sys.path.insert(0, str(BASE / "lib"))
    from icu_api import IcuClient  # noqa: PLC0415
    client = IcuClient(icu_id, icu_key)

    TYPE_SPORT = {"ftp": "Ride", "lthr": "Run", "css": "Swim"}
    pushed = 0
    for ev in events:
        if ev.get("icu_event_id"):
            continue
        sport = TYPE_SPORT.get(ev["type"], "Ride")
        try:
            result = client.push_workout(
                sport=sport,
                event_date=ev["date"],
                name=f"🔬 {ev['label']}",
                description_raw=f"Protocol: {ev['protocol']}",
            )
            ev["icu_event_id"] = result.get("id")
            pushed += 1
        except Exception as e:
            print(f"Warning: failed to push {ev['label']}: {e}", file=sys.stderr)
    return pushed


def test_schedule(phases: list[dict], sports: list[str] | None = None) -> list[str]:
    """Format test events as text lines for the blueprint."""
    today = date.today()
    lines: list[str] = []
    current_type: str | None = None
    type_headings = {"ftp": "Cycling FTP:", "lthr": "Running LTHR:", "css": "Swim CSS:"}

    for t in _test_events(phases, sports):
        if t["type"] != current_type:
            lines.append(type_headings.get(t["type"], t["type"].upper() + ":"))
            current_type = t["type"]
        dt = date.fromisoformat(t["date"])
        marker = " ← TODAY" if dt == today else ""
        lines.append(f"  {t['label']:<35} {t['date']}{marker}")

    return lines


# -- Fitness check -------------------------------------------------------------

def fitness_check(slug: str, event: str, current_ctl: float,
                  phases: list[dict], choice: str | None) -> str | None:
    """
    Returns None if no issue, or a status string.
    If AWAITING_DECISION, returns the full decision block.
    If choice is set, applies it and returns a short note.
    """
    # Check entry fitness for the phase containing TODAY — a mid-plan regen must
    # not compare current CTL against the long-finished first phase (CTL 80 vs
    # Base 55–70 misfired nine weeks into the plan). Before plan start there is
    # no containing phase and we fall back to the first non-taper phase as before.
    today = date.today()

    def _contains_today(p):
        try:
            return (date.fromisoformat(str(p["start"]))
                    <= today <= date.fromisoformat(str(p["end"])))
        except (KeyError, ValueError, TypeError):
            return False

    current = next((p for p in phases if _contains_today(p)), None)
    if current is not None and phase_family(current["name"]) == "taper":
        return None  # being over-fit entering taper is not a coaching problem
    first_non_taper = current or next(
        (p for p in phases if phase_family(p["name"]) != "taper"), None
    )
    if not first_non_taper:
        return None

    target_range = ctl_range(event, first_non_taper["name"])
    if not target_range:
        return None  # event not fully specified yet

    low, high = target_range
    phase_target_mid = (low + high) / 2

    if current_ctl > high * 1.10:
        if choice:
            return (
                f"Fitness choice {choice} applied. "
                f"Current CTL {current_ctl:.0f} vs entry target {low}–{high}. "
                f"Blueprint generated with choice {choice} noted."
            )
        return (
            f"AWAITING_DECISION: athlete={slug} current_ctl={current_ctl:.0f} "
            f"phase={first_non_taper['name']} phase_target_ctl={low}–{high}\n\n"
            f"Your fitness ({current_ctl:.0f} CTL) is above the recommended entry level "
            f"for {first_non_taper['name']} ({low}–{high} CTL).\n\n"
            f"A  Taper down first — reduce volume 1–2 weeks to lower fatigue (TSB), then enter phase on schedule.\n"
            f"   Best for: athletes who feel flat or fatigued despite good numbers.\n\n"
            f"B  Increase quality now — enter Build early, adding race-pace work to convert fitness to form.\n"
            f"   Best for: athletes feeling sharp, healthy, and training well.\n\n"
            f"C  Hold and compress — maintain current load, compress next phase by 1 week.\n"
            f"   Best for: athletes who want to stay the course but are ahead of schedule.\n\n"
            f"D  Custom — flag for manual coach review.\n\n"
            f"Run with: python3 ClaudeCoach/scripts/generate-blueprint.py "
            f"--athlete {slug} --fitness-choice [A|B|C|D]"
        )

    if current_ctl < low * 0.85:
        return (
            f"WARNING: Current CTL {current_ctl:.0f} is below the recommended entry range "
            f"for {first_non_taper['name']} ({low}–{high}). "
            f"Blueprint generated; TSS targets will be moderated to actual fitness level."
        )

    return None


# -- Distribution table --------------------------------------------------------

DISTRIBUTION = {
    "Full Ironman": {
        "base":  {"Swim": "70% Z1–2 / 20% Z3–4 / 10% Z5",
                  "Bike": "80% Z1–2 / 12% Z3 / 8% Z4–5",
                  "Run":  "85% Z1–2 / 10% Z3 / 5% Z4–5"},
        "build": {"Swim": "65% Z1–2 / 25% Z3–4 / 10% Z5",
                  "Bike": "75% Z1–2 / 15% Z3 / 10% Z4–5",
                  "Run":  "80% Z1–2 / 12% Z3 / 8% Z4–5"},
        "peak":  {"Swim": "60% Z1–2 / 25% Z3–4 / 15% Z5",
                  "Bike": "70% Z1–2 / 15% Z3 / 15% Z4–5",
                  "Run":  "75% Z1–2 / 12% Z3 / 13% Z4–5"},
    },
    "70.3": {
        "base":  {"Swim": "70% Z1–2 / 20% Z3–4 / 10% Z5",
                  "Bike": "78% Z1–2 / 14% Z3 / 8% Z4–5",
                  "Run":  "83% Z1–2 / 12% Z3 / 5% Z4–5"},
        "build": {"Swim": "65% Z1–2 / 22% Z3–4 / 13% Z5",
                  "Bike": "70% Z1–2 / 18% Z3 / 12% Z4–5",
                  "Run":  "78% Z1–2 / 12% Z3 / 10% Z4–5"},
        "peak":  {"Swim": "58% Z1–2 / 25% Z3–4 / 17% Z5",
                  "Bike": "65% Z1–2 / 18% Z3 / 17% Z4–5",
                  "Run":  "72% Z1–2 / 14% Z3 / 14% Z4–5"},
    },
    "Sportive": {
        "base":  {"Bike": "80% Z1–2 / 12% Z3 / 8% Z4–5"},
        "build": {"Bike": "70% Z1–2 / 18% Z3 / 12% Z4–5"},
        "peak":  {"Bike": "65% Z1–2 / 18% Z3 / 17% Z4–5"},
    },
}

def dist_table(event: str, phases: list[dict]) -> list[str]:
    event_dist = DISTRIBUTION.get(_event_key(event))
    if not event_dist:
        return [f"  Intensity distribution not yet defined for event: {event}"]
    lines = []
    seen_fams = set()
    for p in phases:
        fam = content_family(phase_family(p["name"]))
        if fam == "taper" or fam in seen_fams:
            continue
        seen_fams.add(fam)
        dist = event_dist.get(fam, {})
        lines.append(f"  {p['name']} ({fam.capitalize()}):")
        for sport, d in dist.items():
            lines.append(f"    {sport:<6} {d}")
    return lines


# -- Main blueprint renderer ---------------------------------------------------

def render_blueprint(slug: str, profile: dict, phases: list[dict],
                     current_ctl: float | None, fitness_note: str | None,
                     choice: str | None) -> str:
    first_name = profile.get("name", slug).split()[0]
    race_date_str = profile.get("race_date", "")
    race_name = profile.get("race_name", "Race")
    event = profile.get("race_distance", "Full Ironman")
    max_hours = profile.get("max_hours_per_week", 10)
    ftp = profile.get("ftp_watts", 0)
    css = profile.get("swim_css_per_100m")
    a_goal = profile.get("a_goal", "—")
    course_type = profile.get("course_type", "flat")
    race_conditions = profile.get("race_conditions", "temperate")
    injuries = profile.get("injuries", [])

    try:
        race_dt = date.fromisoformat(race_date_str)
        weeks_to_race = (race_dt - date.today()).days / 7
    except Exception:
        race_dt = None
        weeks_to_race = 0

    today = date.today()
    lines = []
    lines.append(f"# Training Blueprint — {first_name}")
    lines.append(f"_Generated: {today.isoformat()}_\n")

    lines.append("## Athlete Overview")
    lines.append(f"- **Race:** {race_name} ({race_date_str}) — {event}")
    lines.append(f"- **A goal:** {a_goal}")
    lines.append(f"- **FTP:** {ftp} W")
    css_s = _css_seconds(css)
    if css_s:
        m, s = divmod(css_s, 60)
        lines.append(f"- **CSS:** {m}:{s:02d}/100m")
    lines.append(f"- **Max training hours/week:** {max_hours}")
    ctl_str = f"{current_ctl:.0f}" if current_ctl is not None else "unknown"
    lines.append(f"- **Current CTL:** {ctl_str}")
    if injuries:
        inj_list = "; ".join(
            f"{i.get('location','?')} — {i.get('description','')}" for i in injuries
        )
        lines.append(f"- **Active injuries:** {inj_list}")
    lines.append("")

    if fitness_note:
        lines.append("## Fitness Check")
        lines.append(fitness_note)
        lines.append("")
        if "AWAITING_DECISION" in fitness_note:
            return "\n".join(lines)

    lines.append("## Phase Structure")
    lines.append(f"Weeks to race: {weeks_to_race:.1f}")
    lines.append("")
    lines.append("| Phase | Start | End | Weeks | TSS ceiling |")
    lines.append("|---|---|---|---|---|")
    for p in phases:
        ceil = tss_ceiling(max_hours, p["name"])
        ceil_str = f"{int(ceil)}" if ceil else "40–50% of peak"
        lines.append(
            f"| {p['name']} | {p['start'].isoformat()} | {p['end'].isoformat()} "
            f"| {p['weeks']} | {ceil_str} |"
        )
    lines.append("")

    lines.append("## TSS Targets by Phase")
    for p in phases:
        fam = phase_family(p["name"])
        if fam == "taper":
            lines.append(f"- **{p['name']}:** 40–50% of preceding peak week. Intensity touches maintained.")
        else:
            ceil = tss_ceiling(max_hours, p["name"])
            IF = IF_TARGETS.get(content_family(fam), 0.65)
            lines.append(
                f"- **{p['name']}:** Target up to {int(ceil)} TSS/week "
                f"(IF {IF:.2f}, {max_hours} hr ceiling). "
                f"Ramp +10%/week max; 3-week load + 1-week recovery."
            )
    lines.append("")

    lines.append("## Intensity Distribution")
    lines.append("Weekly average per sport (not per session). Some sessions will be pure Z1–2; others will reach Z4–5.")
    lines.append("")
    dist_lines = dist_table(event, phases)
    lines.extend(dist_lines)
    lines.append("")

    lines.append("## Course Modifier")
    lines.append(f"- **Course type:** {course_type.capitalize()}")
    lines.append(f"- {course_note(course_type)}")
    lines.append("")

    lines.append("## Environmental Protocol")
    if race_dt:
        lines.append(f"- **Heat:** {heat_note(race_conditions, race_dt)}")
    altitude = profile.get("altitude_m", 0)
    if altitude and altitude > 1500:
        lines.append(f"- **Altitude:** Race at {altitude}m. Reduce intensity targets by 5–8% in first 10 days.")
    else:
        lines.append("- **Altitude:** Not applicable.")
    lines.append("")

    lines.append("## Fuelling Protocol")
    seen_fams = set()
    for p in phases:
        fam = phase_family(p["name"])
        if fam in seen_fams:
            continue
        seen_fams.add(fam)
        lines.append(f"- **{p['name']} ({fam.capitalize()}):** {fuelling_note(event, p['name'])}")
    lines.append("")

    lines.append("## Test / Retest Schedule")
    lines.extend(test_schedule(phases, event_sports(event)))
    lines.append("")

    lines.append("## Brick Session Schedule")
    _sports = event_sports(event)
    if "bike" in _sports and "run" in _sports:
        lines.append("| Phase | Min bricks | Type |")
        lines.append("|---|---|---|")
        for p in phases:
            fam = content_family(phase_family(p["name"]))
            lines.append(f"| {p['name']} | {BRICK_MIN.get(fam, '—')} | {BRICK_TYPE.get(fam, '—')} |")
    else:
        lines.append("Not applicable — single-discipline event (no bike→run transition).")
    lines.append("")

    lines.append("## Recovery Triggers")
    lines.append("The following signals prompt an automatic recovery intervention:")
    lines.append("- HRV > 15% below 7-day average → suggest Z1 or rest day")
    lines.append("- RHR > 5 bpm above 7-day average → suggest Z1 or rest day")
    lines.append("- TSB < –30 → insert 3-day recovery block")
    if injuries:
        lines.append("- Injury pain score ≥ 3/10 → modify affected-sport sessions; halt if ≥ 5")
    lines.append("- > 2 sessions missed in a week → treat following week as recovery week")
    lines.append("")

    lines.append("## Notes")
    lines.append("- All zone targets recalculate automatically when a new FTP/LTHR/CSS test is logged.")
    lines.append("- Blueprint regenerates when race, goals, or max hours change.")
    lines.append("- See ClaudeCoach/blueprints/blueprint.md for full methodology reference.")

    return "\n".join(lines)


# -- Structured sidecar --------------------------------------------------------

def build_blueprint_data(slug: str, profile: dict, phases: list[dict],
                         current_ctl: float | None,
                         fitness_note: str | None) -> dict:
    """Serialise the computed methodology into the machine-readable sidecar.

    This is the structured counterpart to render_blueprint()'s prose — same
    values, consumable by the planner/validator. Phase windows are whatever
    `phases` carries (see the anchor decision in remediation-plan WS B); this
    function does not re-derive them.
    """
    event = profile.get("race_distance", "Full Ironman")
    max_hours = profile.get("max_hours_per_week", 10)
    race_date_str = profile.get("race_date", "")
    try:
        race_dt: date | None = date.fromisoformat(race_date_str)
        weeks_to_race = round((race_dt - date.today()).days / 7, 1)
    except (ValueError, TypeError):
        race_dt = None
        weeks_to_race = 0.0

    event_dist = DISTRIBUTION.get(_event_key(event), {})
    sports = event_sports(event)
    bricks_apply = "bike" in sports and "run" in sports  # a brick is bike→run

    phase_objs: list[dict] = []
    for p in phases:
        fam = phase_family(p["name"])
        cfam = content_family(fam)          # specific -> build for content tables
        ceil = tss_ceiling(max_hours, p["name"])
        entry = ctl_range(event, p["name"])
        phase_objs.append({
            "name": p["name"],
            "family": fam,
            "start": p["start"].isoformat(),
            "end": p["end"].isoformat(),
            "weeks": p["weeks"],
            "tss_ceiling": int(ceil) if ceil else None,
            "if_target": IF_TARGETS.get(cfam),
            "ctl_entry_low": entry[0] if entry else None,
            "ctl_entry_high": entry[1] if entry else None,
            "distribution": event_dist.get(cfam, {}),
            "fuelling": fuelling_note(event, p["name"]),
            "brick_min": BRICK_MIN.get(cfam) if bricks_apply else None,
            "brick_type": BRICK_TYPE.get(cfam) if bricks_apply else None,
        })

    race_conditions = profile.get("race_conditions", "temperate")
    heat = {"active": False, "starts": None}
    if race_conditions == "hot" and race_dt:
        heat = {"active": True, "starts": (race_dt - timedelta(weeks=4)).isoformat()}
    altitude = profile.get("altitude_m", 0) or 0
    course_type = profile.get("course_type", "flat")

    return {
        "schema_version": SCHEMA_VERSION,
        "slug": slug,
        "generated": date.today().isoformat(),
        "event_type": event,
        "sports": sports,
        "race_name": profile.get("race_name", "Race"),
        "race_date": race_date_str,
        "weeks_to_race": weeks_to_race,
        "max_hours_per_week": max_hours,
        "current_ctl": current_ctl,
        "fitness_note": fitness_note,
        "phases": phase_objs,
        "tests": _test_events(phases, event_sports(event)),
        "env_protocols": {
            "heat": heat,
            "altitude": {"active": bool(altitude and altitude > 1500),
                         "altitude_m": altitude},
        },
        "course": {"type": course_type, "note": course_note(course_type)},
    }


# -- Entry point ---------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate training blueprint for an athlete.")
    parser.add_argument("--athlete", required=True, help="Athlete slug (e.g. jamie)")
    parser.add_argument(
        "--fitness-choice",
        choices=["A", "B", "C", "D"],
        default=None,
        help="Resolution choice if athlete is over-fitness for entry phase",
    )
    parser.add_argument(
        "--skip-events", action="store_true",
        help="Skip pushing test events to intervals.icu",
    )
    args = parser.parse_args()

    slug = args.athlete
    choice = args.fitness_choice

    profile_path = BASE / f"athletes/{slug}/profile.json"
    if not profile_path.exists():
        print(f"Error: no profile found at {profile_path}", file=sys.stderr)
        sys.exit(1)

    profile = json.loads(profile_path.read_text())

    race_date_str = profile.get("race_date", "")
    if not race_date_str:
        print("Error: profile.json has no race_date.", file=sys.stderr)
        sys.exit(1)

    try:
        race_dt = date.fromisoformat(race_date_str)
    except ValueError:
        print(f"Error: invalid race_date format '{race_date_str}'.", file=sys.stderr)
        sys.exit(1)

    weeks_to_race = (race_dt - date.today()).days / 7
    if weeks_to_race < 0:
        print("Error: race_date is in the past.", file=sys.stderr)
        sys.exit(1)

    # Canonical phases come from athletes.json (plan_start + phase_tss end-weeks),
    # so the blueprint windows agree with what the planner prescribes
    # (remediation decision 2026-06-07). Fall back to weeks-to-race
    # auto-derivation only for athletes with no phase config (e.g. calum).
    acfg = {}
    if ATHLETES_CONFIG.exists():
        try:
            acfg = json.loads(ATHLETES_CONFIG.read_text()).get(slug, {})
        except Exception:
            acfg = {}
    plan_start_str = acfg.get("plan_start")
    try:
        plan_start = date.fromisoformat(plan_start_str) if plan_start_str else None
    except ValueError:
        plan_start = None
    phases = resolve_phases(plan_start, acfg.get("phase_tss"), race_dt, date.today())
    if plan_start and acfg.get("phase_tss"):
        print(f"Phases from athletes.json config (anchor {plan_start}).", file=sys.stderr)
    else:
        print("No phase config — falling back to weeks-to-race auto-derivation.", file=sys.stderr)

    print(f"Fetching live CTL for {slug}...", file=sys.stderr)
    current_ctl = fetch_ctl(slug)
    if current_ctl is None:
        print("Warning: could not fetch CTL from intervals.icu; fitness check skipped.", file=sys.stderr)

    event = profile.get("race_distance", "Full Ironman")
    fitness_note = None
    if current_ctl is not None:
        fitness_note = fitness_check(slug, event, current_ctl, phases, choice)

    blueprint = render_blueprint(slug, profile, phases, current_ctl, fitness_note, choice)

    if fitness_note and "AWAITING_DECISION" in fitness_note:
        print(blueprint)
        sys.exit(0)

    out_dir = BASE / f"athletes/{slug}/reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "training-blueprint.md"
    out_path.write_text(blueprint)
    print(f"Blueprint written to {out_path}", file=sys.stderr)

    # Structured sidecar — machine-readable counterpart consumed by the planner
    # (remediation WS B). Validate before writing so a malformed sidecar fails
    # loudly here, not at planning time.
    data = build_blueprint_data(slug, profile, phases, current_ctl, fitness_note)
    errs = validate_blueprint(data)
    if errs:
        print("ERROR: blueprint sidecar failed schema validation:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    json_path = out_dir / "training-blueprint.json"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Blueprint sidecar written to {json_path}", file=sys.stderr)

    # Write test schedule — merge with existing to preserve completion/notification state
    fresh_events = _test_events(phases, event_sports(event))
    test_schedule_f = BASE / f"athletes/{slug}/test-schedule.json"
    if test_schedule_f.exists():
        try:
            old_by_key = {
                (t["type"], t["label"]): t
                for t in json.loads(test_schedule_f.read_text())
            }
            for ev in fresh_events:
                old = old_by_key.get((ev["type"], ev["label"]))
                if old:
                    ev["notified"]  = old.get("notified", False)
                    ev["completed"] = old.get("completed", False)
                    if old.get("icu_event_id"):
                        ev["icu_event_id"] = old["icu_event_id"]
        except Exception:
            pass
    test_schedule_f.write_text(json.dumps(fresh_events, indent=2))
    print(f"Test schedule written to {test_schedule_f}", file=sys.stderr)

    if not args.skip_events:
        n = push_test_events(slug, fresh_events)
        if n:
            test_schedule_f.write_text(json.dumps(fresh_events, indent=2))
            print(f"Pushed {n} test events to intervals.icu.", file=sys.stderr)

    print(blueprint)


if __name__ == "__main__":
    main()
