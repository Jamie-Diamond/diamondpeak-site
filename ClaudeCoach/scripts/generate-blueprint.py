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


# ── Mesocycle algorithm ──────────────────────────────────────────────────────

def phase_structure(weeks: int) -> list[dict]:
    """Return ordered list of phase dicts given weeks_to_race."""
    if weeks >= 24:
        return [
            {"name": "Base1",  "weeks": 6},
            {"name": "Base2",  "weeks": 4},
            {"name": "Base3",  "weeks": 4},
            {"name": "Build1", "weeks": 4},
            {"name": "Build2", "weeks": 4},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 24, 3)},
        ]
    elif weeks >= 20:
        return [
            {"name": "Base1",  "weeks": 6},
            {"name": "Base2",  "weeks": 4},
            {"name": "Build1", "weeks": 4},
            {"name": "Build2", "weeks": 4},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 20, 3)},
        ]
    elif weeks >= 16:
        return [
            {"name": "Base",   "weeks": 6},
            {"name": "Build1", "weeks": 4},
            {"name": "Build2", "weeks": 4},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 16, 2)},
        ]
    elif weeks >= 12:
        return [
            {"name": "Base",   "weeks": 4},
            {"name": "Build",  "weeks": 4},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 12, 2)},
        ]
    elif weeks >= 8:
        return [
            {"name": "Base",   "weeks": 3},
            {"name": "Build",  "weeks": 3},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 8, 2)},
        ]
    else:
        return [
            {"name": "Build",  "weeks": max(weeks - 4, 1)},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": 2},
        ]


def assign_dates(phases: list[dict], start: date) -> list[dict]:
    """Assign start/end dates to each phase."""
    current = start
    for p in phases:
        p["start"] = current
        p["end"] = current + timedelta(weeks=p["weeks"]) - timedelta(days=1)
        current = p["end"] + timedelta(days=1)
    return phases


# ── TSS ceiling ──────────────────────────────────────────────────────────────

IF_TARGETS = {
    "base":  0.65,
    "build": 0.68,
    "peak":  0.72,
}

def phase_family(name: str) -> str:
    n = name.lower()
    if "base" in n:
        return "base"
    if "build" in n:
        return "build"
    if "peak" in n:
        return "peak"
    return "taper"

def tss_ceiling(max_hours: float, phase_name: str) -> float | None:
    fam = phase_family(phase_name)
    if fam == "taper":
        return None
    IF = IF_TARGETS[fam]
    return round(max_hours * 100 * IF ** 2, 0)


# ── CTL phase targets ─────────────────────────────────────────────────────────

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
    fam = phase_family(phase_name)
    return CTL_TARGETS.get(event, {}).get(fam)


# ── Live CTL fetch ────────────────────────────────────────────────────────────

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


# ── Course modifier ──────────────────────────────────────────────────────────

COURSE_NOTES = {
    "flat":         "Standard distribution applies.",
    "rolling":      "+5% Z3 bike in Build phases. Add 1×45 min sweet-spot per week.",
    "hilly":        "+10% Z4–5 bike in Build. Add climbing repeats (2×20 min Z4). Reduce volume by 5%.",
    "mountainous":  "Specialist climbing blocks. Reduce weekly volume 10%. All long rides on hilly terrain.",
}

def course_note(course_type: str) -> str:
    return COURSE_NOTES.get(course_type.lower(), "Standard distribution applies.")


# ── Heat protocol ────────────────────────────────────────────────────────────

def heat_note(race_conditions: str, race_dt: date) -> str:
    if race_conditions != "hot":
        return "Not active."
    start = race_dt - timedelta(weeks=4)
    return (
        f"Active. Sauna protocol begins {start.isoformat()} "
        f"(3–4×/week, 20–30 min at 80–90°C, post-exercise). "
        f"Outdoor heat sessions: 2–3×/week in peak ambient ≥ 25°C from same date."
    )


# ── Fuelling targets ─────────────────────────────────────────────────────────

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
}

def fuelling_note(event: str, phase_name: str) -> str:
    fam = phase_family(phase_name)
    event_fuel = FUELLING.get(event, FUELLING["Full Ironman"])
    return event_fuel.get(fam, "Follow phase-progressive protocol.")


# ── Test schedule ────────────────────────────────────────────────────────────

ATHLETES_CONFIG = BASE / "config/athletes.json"


def _test_events(phases: list[dict]) -> list[dict]:
    """Return structured list of performance tests with dates."""
    today = date.today()
    events: list[dict] = []

    def add(test_type: str, label: str, dt: date, protocol: str):
        events.append({
            "type": test_type, "label": label, "date": dt.isoformat(),
            "protocol": protocol, "notified": False, "completed": False,
        })

    # FTP
    add("ftp", "FTP Baseline", today, "20-min test × 0.95 or ramp test")
    build_phases = [p for p in phases if phase_family(p["name"]) == "build"]
    for p in phases:
        fam = phase_family(p["name"])
        if fam == "base":
            mid = p["start"] + timedelta(weeks=p["weeks"] // 2)
            add("ftp", f"FTP Mid-{p['name']}", mid, "Ramp test")
        elif fam == "build" and build_phases and p == build_phases[-1]:
            add("ftp", "FTP End-Build", p["end"] - timedelta(days=2), "20-min test × 0.95")

    # LTHR
    add("lthr", "LTHR Baseline", today, "30-min run TT; avg HR final 20 min = LTHR")
    base_phases = [p for p in phases if "base" in p["name"].lower()]
    if base_phases:
        add("lthr", "LTHR End-Base", base_phases[-1]["end"] - timedelta(days=2),
            "30-min run TT; avg HR final 20 min = LTHR")

    # CSS
    add("css", "CSS Baseline", today, "400m + 200m TT (CSS calculator)")
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


def test_schedule(phases: list[dict]) -> list[str]:
    """Format test events as text lines for the blueprint."""
    today = date.today()
    lines: list[str] = []
    current_type: str | None = None
    type_headings = {"ftp": "Cycling FTP:", "lthr": "Running LTHR:", "css": "Swim CSS:"}

    for t in _test_events(phases):
        if t["type"] != current_type:
            lines.append(type_headings.get(t["type"], t["type"].upper() + ":"))
            current_type = t["type"]
        dt = date.fromisoformat(t["date"])
        marker = " ← TODAY" if dt == today else ""
        lines.append(f"  {t['label']:<35} {t['date']}{marker}")

    return lines


# ── Fitness check ─────────────────────────────────────────────────────────────

def fitness_check(slug: str, event: str, current_ctl: float,
                  phases: list[dict], choice: str | None) -> str | None:
    """
    Returns None if no issue, or a status string.
    If AWAITING_DECISION, returns the full decision block.
    If choice is set, applies it and returns a short note.
    """
    first_non_taper = next(
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


# ── Distribution table ────────────────────────────────────────────────────────

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
}

def dist_table(event: str, phases: list[dict]) -> list[str]:
    event_dist = DISTRIBUTION.get(event)
    if not event_dist:
        return [f"  Intensity distribution not yet defined for event: {event}"]
    lines = []
    seen_fams = set()
    for p in phases:
        fam = phase_family(p["name"])
        if fam == "taper" or fam in seen_fams:
            continue
        seen_fams.add(fam)
        dist = event_dist.get(fam, {})
        lines.append(f"  {p['name']} ({fam.capitalize()}):")
        for sport, d in dist.items():
            lines.append(f"    {sport:<6} {d}")
    return lines


# ── Main blueprint renderer ───────────────────────────────────────────────────

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
    if css:
        m, s = divmod(int(css), 60)
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
            IF = IF_TARGETS.get(fam, 0.65)
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
    lines.extend(test_schedule(phases))
    lines.append("")

    lines.append("## Brick Session Schedule")
    lines.append("| Phase | Min bricks | Type |")
    lines.append("|---|---|---|")
    for p in phases:
        fam = phase_family(p["name"])
        if fam == "base":
            lines.append(f"| {p['name']} | 1 | Short brick (30–45 min ride + 10–20 min easy run) |")
        elif fam == "build":
            lines.append(f"| {p['name']} | 2–3 | Standard and quality bricks |")
        elif fam == "peak":
            lines.append(f"| {p['name']} | 3–4 | Include at least 1 long brick (race simulation) |")
        elif fam == "taper":
            lines.append(f"| {p['name']} | 1 | Short brick only; no fatigue accumulation |")
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


# ── Entry point ───────────────────────────────────────────────────────────────

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

    phases = phase_structure(int(weeks_to_race))
    phases = assign_dates(phases, date.today())
    phases[-1]["end"] = race_dt  # extend last phase to race day

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

    # Write test schedule — merge with existing to preserve completion/notification state
    fresh_events = _test_events(phases)
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
