#!/usr/bin/env python3
"""
Generate a race-day execution plan for an athlete.
Outputs: athletes/{slug}/reference/race-plan.md

Usage:
  python3 generate-race-plan.py --athlete jamie
"""

import argparse, json, subprocess, sys
from datetime import date, timedelta
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/

# ---------------------------------------------------------------------------
# Race profiles — distances, target IF, swim buffers, pacing band multipliers
# ---------------------------------------------------------------------------

RACE_PROFILES = {
    "Full Ironman": {
        "swim_m": 3800, "bike_km": 180, "run_km": 42.2,
        "bike_if": 0.72, "swim_buffer_s": 10, "run_factor": 1.18,
        "bike_bands":  [(0, 60, 0.68), (60, 160, 0.73), (160, 180, 0.70)],
        "run_bands":   [(0, 15, 1.22), (15, 32, 1.17), (32, 42.2, 1.10)],
        "bike_hr_pct": (0.74, 0.80), "run_hr_pct":  (0.78, 0.84), "swim_hr_pct": (0.72, 0.76),
    },
    "Half Ironman": {
        "swim_m": 1900, "bike_km": 90, "run_km": 21.1,
        "bike_if": 0.80, "swim_buffer_s": 6, "run_factor": 1.08,
        "bike_bands":  [(0, 25, 0.77), (25, 70, 0.82), (70, 90, 0.79)],
        "run_bands":   [(0, 7, 1.12), (7, 15, 1.07), (15, 21.1, 1.03)],
        "bike_hr_pct": (0.78, 0.84), "run_hr_pct":  (0.82, 0.88), "swim_hr_pct": (0.72, 0.76),
    },
    "Olympic": {
        "swim_m": 1500, "bike_km": 40, "run_km": 10,
        "bike_if": 0.88, "swim_buffer_s": 3, "run_factor": 1.03,
        "bike_bands":  [(0, 10, 0.85), (10, 30, 0.90), (30, 40, 0.87)],
        "run_bands":   [(0, 3, 1.07), (3, 7, 1.02), (7, 10, 0.97)],
        "bike_hr_pct": (0.84, 0.90), "run_hr_pct":  (0.88, 0.93), "swim_hr_pct": (0.75, 0.80),
    },
    "Sprint": {
        "swim_m": 750, "bike_km": 20, "run_km": 5,
        "bike_if": 0.95, "swim_buffer_s": 1, "run_factor": 0.97,
        "bike_bands":  [(0, 5, 0.92), (5, 15, 0.97), (15, 20, 0.94)],
        "run_bands":   [(0, 1.5, 1.01), (1.5, 3.5, 0.97), (3.5, 5, 0.93)],
        "bike_hr_pct": (0.88, 0.94), "run_hr_pct":  (0.92, 0.97), "swim_hr_pct": (0.78, 0.83),
    },
    "Aquabike Full": {
        "swim_m": 3800, "bike_km": 180, "run_km": 0,
        "bike_if": 0.75, "swim_buffer_s": 10,
        "bike_bands":  [(0, 60, 0.71), (60, 140, 0.76), (140, 180, 0.73)],
        "bike_hr_pct": (0.74, 0.82), "swim_hr_pct": (0.72, 0.76),
    },
    "Aquabike Half": {
        "swim_m": 1900, "bike_km": 90, "run_km": 0,
        "bike_if": 0.83, "swim_buffer_s": 6,
        "bike_bands":  [(0, 25, 0.80), (25, 70, 0.85), (70, 90, 0.81)],
        "bike_hr_pct": (0.78, 0.86), "swim_hr_pct": (0.72, 0.76),
    },
    "Marathon": {
        "swim_m": 0, "bike_km": 0, "run_km": 42.2,
        "run_factor": 1.04,
        "run_bands":  [(0, 10, 1.10), (10, 30, 1.04), (30, 42.2, 0.99)],
        "run_hr_pct": (0.82, 0.90),
    },
    "Half Marathon": {
        "swim_m": 0, "bike_km": 0, "run_km": 21.1,
        "run_factor": 1.01,
        "run_bands":  [(0, 5, 1.05), (5, 16, 1.01), (16, 21.1, 0.97)],
        "run_hr_pct": (0.86, 0.93),
    },
    "10k": {
        "swim_m": 0, "bike_km": 0, "run_km": 10,
        "run_factor": 0.96,
        "run_bands":  [(0, 2, 1.00), (2, 7, 0.96), (7, 10, 0.92)],
        "run_hr_pct": (0.90, 0.97),
    },
    "5k": {
        "swim_m": 0, "bike_km": 0, "run_km": 5,
        "run_factor": 0.92,
        "run_bands":  [(0, 1, 0.96), (1, 4, 0.92), (4, 5, 0.88)],
        "run_hr_pct": (0.93, 0.99),
    },
}

RACE_ALIASES = {
    "full ironman": "Full Ironman",
    "ironman": "Full Ironman",
    "140.6": "Full Ironman",
    "im": "Full Ironman",
    "half ironman": "Half Ironman",
    "70.3": "Half Ironman",
    "half im": "Half Ironman",
    "olympic": "Olympic",
    "olympic distance": "Olympic",
    "olympic triathlon": "Olympic",
    "sprint": "Sprint",
    "sprint triathlon": "Sprint",
    "aquabike": "Aquabike Full",
    "aquabike full": "Aquabike Full",
    "aquabike half": "Aquabike Half",
    "marathon": "Marathon",
    "half marathon": "Half Marathon",
    "half-marathon": "Half Marathon",
    "10k": "10k",
    "10km": "10k",
    "5k": "5k",
    "5km": "5k",
}

CTL_TARGETS = {
    "Full Ironman":   90,
    "Half Ironman":   72,
    "Olympic":        55,
    "Sprint":         40,
    "Aquabike Full":  80,
    "Aquabike Half":  60,
    "Marathon":       72,
    "Half Marathon":  58,
    "10k":            45,
    "5k":             35,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_pace(val) -> int | None:
    """Parse a pace value into seconds. Accepts 'M:SS' string or numeric seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return None
    try:
        return int(float(s))
    except ValueError:
        return None


def fmt_pace(seconds_per_km: float) -> str:
    m, s = divmod(int(seconds_per_km), 60)
    return f"{m}:{s:02d}"


def fmt_time(total_minutes: float) -> str:
    h, m = divmod(int(total_minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m:02d}m"


def normalise_distance(raw: str | None) -> str | None:
    if not raw:
        return None
    return RACE_ALIASES.get(raw.lower().strip(), raw)


def fetch_fitness(slug: str) -> tuple[float | None, float | None]:
    """Return (current_ctl, weekly_ramp_4wk). weekly_ramp may be None."""
    result = subprocess.run(
        ["python3", "ClaudeCoach/lib/icu_fetch.py",
         "--athlete", slug, "--endpoint", "fitness", "--days", "35"],
        capture_output=True, text=True, cwd=PROJECT_DIR,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
    try:
        data = json.loads(result.stdout)
        if not data:
            return None, None
        current_ctl = data[-1].get("ctl")
        if current_ctl is None:
            return None, None
        if len(data) >= 28:
            old_ctl = data[-28].get("ctl") or current_ctl
            weekly_ramp = (current_ctl - old_ctl) / 4
        else:
            weekly_ramp = 0.0
        return float(current_ctl), float(weekly_ramp)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Document sections
# ---------------------------------------------------------------------------

def _viability_block(current_ctl, weekly_ramp, days_to_race, race_distance) -> str:
    target = CTL_TARGETS.get(race_distance, 85)
    f_ctl = current_ctl + weekly_ramp * (days_to_race / 7)
    certainty = "near-certain" if days_to_race <= 42 else "projected"

    if f_ctl >= target * 0.97:
        verdict = "✅ On track"
        note = f"Forecast CTL {f_ctl:.0f} meets race target of {target}."
    elif f_ctl >= target * 0.88:
        verdict = "⚠️ Marginal"
        note = (
            f"Forecast CTL {f_ctl:.0f} is below race target ({target}). "
            f"Treat pacing targets as conservative ceilings — don't push above them."
        )
    else:
        verdict = "❌ Undercooked"
        note = (
            f"Forecast CTL {f_ctl:.0f} is well below race target ({target}). "
            f"Adjust targets or reassess the race goal."
        )

    return (
        f"## Race Readiness — {verdict}\n\n"
        f"| Metric | Value |\n|--------|-------|\n"
        f"| Current CTL | {current_ctl:.1f} |\n"
        f"| 4-week ramp | {weekly_ramp:+.1f}/wk |\n"
        f"| {certainty.title()} CTL at race day | {f_ctl:.0f} |\n"
        f"| Race target CTL | {target} |\n"
        f"| Days to race | {days_to_race} |\n\n"
        f"_{note}_\n\n"
        f"_Last updated: {date.today().isoformat()}_\n"
    )


def _swim_section(profile, rp) -> str:
    swim_m = rp.get("swim_m", 0)
    if swim_m == 0:
        return ""

    css_s = parse_pace(profile.get("swim_css_per_100m"))
    if css_s is None:
        return (
            "## Swim\n\n"
            "> ⚠️ **CSS not set.** Add `swim_css_per_100m` to your profile "
            "(e.g. `1:40` for 1:40/100m) before targets can be calculated.\n\n"
        )

    buffer_s = rp.get("swim_buffer_s", 8)
    target_s = css_s + buffer_s
    total_min = swim_m / 100 * target_s / 60

    lthr = profile.get("lthr")
    lo_pct, hi_pct = rp.get("swim_hr_pct", (0.72, 0.76))
    hr_str = (
        f"{int(lthr * lo_pct)}–{int(lthr * hi_pct)} bpm"
        if lthr else "_LTHR not set_"
    )

    return (
        f"## Swim — {swim_m:,}m\n\n"
        f"| | Target |\n|--|--|\n"
        f"| CSS | {fmt_pace(css_s)}/100m |\n"
        f"| Race pace | {fmt_pace(target_s)}/100m (+{buffer_s}s buffer) |\n"
        f"| HR | {hr_str} |\n"
        f"| Est. time | {fmt_time(total_min)} |\n\n"
        "**Strategy**: Start wide to avoid the washing machine. Settle by 200m. "
        "Sight every 8–10 strokes. Exit feeling like you've done nothing.\n"
    )


def _bike_section(profile, rp, f_ctl, ctl_target) -> str:
    bike_km = rp.get("bike_km", 0)
    if bike_km == 0:
        return ""

    ftp = profile.get("ftp_watts") or profile.get("indoor_ftp_watts")
    if not ftp:
        return (
            "## Bike\n\n"
            "> ⚠️ **FTP not set.** Add `ftp_watts` to your profile before targets can be calculated.\n\n"
        )

    overall_if = rp.get("bike_if", 0.72)
    if f_ctl is not None and ctl_target and f_ctl < ctl_target * 0.88:
        overall_if = round(overall_if - 0.02, 2)

    target_w = round(ftp * overall_if)

    lthr = profile.get("lthr")
    lo_pct, hi_pct = rp.get("bike_hr_pct", (0.74, 0.80))
    hr_str = (
        f"{int(lthr * lo_pct)}–{int(lthr * hi_pct)} bpm"
        if lthr else "_LTHR not set_"
    )

    avg_speed_kmh = 26 + (overall_if - 0.65) * 40
    total_min = bike_km / avg_speed_kmh * 60

    bands_rows = ""
    for start, end, band_if in rp.get("bike_bands", []):
        band_w = round(ftp * band_if)
        bands_rows += f"| km {start}–{end} | {band_if:.0%} FTP | {band_w} W |\n"

    nutrition = ""
    if bike_km >= 90:
        nutrition = (
            "\n### Nutrition\n\n"
            "| Segment | Carbs | Fluid |\n|---------|-------|-------|\n"
            f"| km 0–{bike_km//3} | 60g/hr | 500ml/hr |\n"
            f"| km {bike_km//3}–{2*bike_km//3} | 70g/hr | 600ml/hr |\n"
            f"| km {2*bike_km//3}–{bike_km} | 60g/hr | 500ml/hr |\n\n"
            "_Start eating at km 20. Start drinking at km 10. Aim for a slight reduction in the last third "
            "to avoid GI distress on the run._\n"
        )

    return (
        f"## Bike — {bike_km} km\n\n"
        f"| | Target |\n|--|--|\n"
        f"| Overall IF | {overall_if:.0%} FTP |\n"
        f"| Target power | {target_w} W |\n"
        f"| HR | {hr_str} |\n"
        f"| Est. time | {fmt_time(total_min)} |\n\n"
        "### Pacing Bands\n\n"
        "| Segment | IF | Watts |\n|---------|-----|-------|\n"
        f"{bands_rows}\n"
        f"**Strategy**: Ride to power, not HR. "
        + (
            f"If HR climbs above {int(lthr * hi_pct)} bpm in the first third, ease off — "
            if lthr else
            "If HR is rising through the first third rather than settling, ease off — "
        )
        + "the run decides the race."
        f"{nutrition}\n"
    )


def _run_section(profile, rp, f_ctl, ctl_target) -> str:
    run_km = rp.get("run_km", 0)
    if run_km == 0:
        return ""

    thr_s = parse_pace(profile.get("run_threshold_pace_per_km"))
    if thr_s is None:
        return (
            "## Run\n\n"
            "> ⚠️ **Run threshold pace not set.** Add `run_threshold_pace_per_km` to your profile "
            "(e.g. `4:15`) before targets can be calculated.\n\n"
        )

    overall_factor = rp.get("run_factor", 1.18)
    if f_ctl is not None and ctl_target and f_ctl < ctl_target * 0.88:
        overall_factor += 0.03

    target_s = thr_s * overall_factor
    total_min = run_km * target_s / 60

    lthr = profile.get("lthr")
    lo_pct, hi_pct = rp.get("run_hr_pct", (0.78, 0.84))
    hr_str = (
        f"{int(lthr * lo_pct)}–{int(lthr * hi_pct)} bpm"
        if lthr else "_LTHR not set_"
    )

    bands_rows = ""
    for start, end, factor in rp.get("run_bands", []):
        seg_s = thr_s * factor
        seg_min = (end - start) * seg_s / 60
        bands_rows += (
            f"| km {start}–{end} | {fmt_pace(seg_s)}/km | {fmt_time(seg_min)} |\n"
        )

    return (
        f"## Run — {run_km} km\n\n"
        f"| | Target |\n|--|--|\n"
        f"| Threshold pace | {fmt_pace(thr_s)}/km |\n"
        f"| Race pace | {fmt_pace(target_s)}/km |\n"
        f"| HR | {hr_str} |\n"
        f"| Est. time | {fmt_time(total_min)} |\n\n"
        "### Pacing Bands\n\n"
        "| Segment | Pace | Est. split |\n|---------|------|------------|\n"
        f"{bands_rows}\n"
        "**Strategy**: The first 5km will feel too easy — that's correct. "
        + (
            f"If HR is above {int(lthr * hi_pct)} bpm before km 10, walk the next aid station."
            if lthr else
            "If effort feels harder than conversational before km 10, walk the next aid station."
        )
        + "\n"
    )


def _transitions(rp) -> str:
    swim_m = rp.get("swim_m", 0)
    bike_km = rp.get("bike_km", 0)
    run_km = rp.get("run_km", 0)

    rows = ""
    if swim_m and bike_km:
        rows += "| T1 — Swim → Bike | ≤3:00 |\n"
    if bike_km and run_km:
        rows += "| T2 — Bike → Run | ≤2:30 |\n"

    if not rows:
        return ""

    return (
        "## Transitions\n\n"
        f"| | Target |\n|--|--|\n{rows}\n"
        "_Kit check the night before. No decisions on race morning._\n"
    )


def _overall_summary(profile, rp, f_ctl, ctl_target) -> str:
    swim_m  = rp.get("swim_m", 0)
    bike_km = rp.get("bike_km", 0)
    run_km  = rp.get("run_km", 0)

    ftp     = profile.get("ftp_watts") or profile.get("indoor_ftp_watts")
    css_s   = parse_pace(profile.get("swim_css_per_100m"))
    thr_s   = parse_pace(profile.get("run_threshold_pace_per_km"))

    overall_if     = rp.get("bike_if", 0.72)
    run_factor     = rp.get("run_factor", 1.18)
    swim_buffer_s  = rp.get("swim_buffer_s", 8)

    if f_ctl is not None and ctl_target and f_ctl < ctl_target * 0.88:
        overall_if  = round(overall_if - 0.02, 2)
        run_factor += 0.03

    rows = ""
    total_min = 0.0

    if swim_m and css_s:
        swim_s   = css_s + swim_buffer_s
        swim_min = swim_m / 100 * swim_s / 60
        total_min += swim_min
        rows += f"| Swim | {fmt_pace(swim_s)}/100m | {fmt_time(swim_min)} |\n"

    if swim_m and bike_km:
        total_min += 3
        rows += "| T1 | — | ~3:00 |\n"

    if bike_km and ftp:
        bike_w   = round(ftp * overall_if)
        avg_spd  = 26 + (overall_if - 0.65) * 40
        bike_min = bike_km / avg_spd * 60
        total_min += bike_min
        rows += f"| Bike | {bike_w} W (IF {overall_if:.0%}) | {fmt_time(bike_min)} |\n"

    if bike_km and run_km:
        total_min += 2.5
        rows += "| T2 | — | ~2:30 |\n"

    if run_km and thr_s:
        run_s   = thr_s * run_factor
        run_min = run_km * run_s / 60
        total_min += run_min
        rows += f"| Run | {fmt_pace(run_s)}/km | {fmt_time(run_min)} |\n"

    if not rows:
        return ""

    return (
        "## Overall Target\n\n"
        "| Segment | Pace / Power | Est. time |\n"
        "|---------|-------------|----------|\n"
        f"{rows}"
        f"| **Total** | | **{fmt_time(total_min)}** |\n\n"
        "_Times are estimates. Conditions, transitions, and race-day execution will vary._\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate(slug: str) -> str:
    adir = BASE / "athletes" / slug
    profile_path = adir / "profile.json"

    if not profile_path.exists():
        print(f"No profile found for athlete '{slug}'", file=sys.stderr)
        sys.exit(1)

    profile = json.loads(profile_path.read_text())

    # Normalise race distance
    race_distance_raw = profile.get("race_distance", "")
    race_distance     = normalise_distance(race_distance_raw)
    rp                = RACE_PROFILES.get(race_distance)

    if not rp:
        supported = ", ".join(RACE_PROFILES)
        print(
            f"Unknown race distance: '{race_distance_raw}'.\n"
            f"Supported: {supported}\n"
            f"Set race_distance in athletes/{slug}/profile.json.",
            file=sys.stderr,
        )
        sys.exit(1)

    race_name    = profile.get("race_name", "Race")
    race_date_s  = profile.get("race_date")
    days_to_race: int | None = None
    if race_date_s:
        try:
            race_dt      = date.fromisoformat(race_date_s)
            days_to_race = (race_dt - date.today()).days
        except Exception:
            pass

    # Fitness
    current_ctl, weekly_ramp = fetch_fitness(slug)
    ctl_target = CTL_TARGETS.get(race_distance, 85)
    f_ctl = (
        current_ctl + (weekly_ramp or 0.0) * (days_to_race / 7)
        if current_ctl is not None and days_to_race is not None
        else current_ctl
    )

    # Check for missing profile fields
    missing = []
    if not profile.get("lthr"):
        missing.append("`lthr` — lactate threshold HR (e.g. 175)")
    if not profile.get("swim_css_per_100m"):
        missing.append("`swim_css_per_100m` — critical swim speed (e.g. `1:40`)")
    if not profile.get("run_threshold_pace_per_km"):
        missing.append("`run_threshold_pace_per_km` — threshold run pace (e.g. `4:15`)")
    if not profile.get("ftp_watts"):
        missing.append("`ftp_watts` — bike FTP in watts")

    # Build document
    parts = [
        f"# Race Plan — {race_name} ({race_distance})\n",
    ]

    if days_to_race is not None:
        race_dt_fmt = date.fromisoformat(race_date_s).strftime("%-d %B %Y")
        parts.append(f"_{race_dt_fmt} · {days_to_race} days to go · Generated {date.today().isoformat()}_\n")

    if missing:
        parts.append(
            "## ⚠️ Missing Profile Data\n\n"
            "These fields are missing — HR targets and some sections will be incomplete. "
            "Add them to `profile.json` and regenerate.\n\n"
            + "".join(f"- {m}\n" for m in missing) + "\n"
        )

    if current_ctl is not None and days_to_race is not None:
        parts.append(_viability_block(current_ctl, weekly_ramp or 0.0, days_to_race, race_distance))
    elif current_ctl is None:
        parts.append(
            "_Fitness data unavailable — viability check skipped. "
            "Run `icu_fetch.py --athlete {slug} --endpoint fitness` to diagnose._\n\n"
        )

    if rp.get("swim_m", 0) > 0 or rp.get("bike_km", 0) > 0 or rp.get("run_km", 0) > 0:
        parts.append(_overall_summary(profile, rp, f_ctl, ctl_target))

    if rp.get("swim_m", 0) > 0:
        parts.append(_swim_section(profile, rp))

    if rp.get("bike_km", 0) > 0:
        parts.append(_bike_section(profile, rp, f_ctl, ctl_target))

    if rp.get("run_km", 0) > 0:
        parts.append(_run_section(profile, rp, f_ctl, ctl_target))

    if rp.get("swim_m", 0) > 0 and (rp.get("bike_km", 0) > 0 or rp.get("run_km", 0) > 0):
        parts.append(_transitions(rp))

    md = "\n".join(p for p in parts if p)

    out_path = adir / "reference/race-plan.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)

    # Summary line for bot
    summary_parts = [f"Race plan updated for {race_name}"]
    if current_ctl is not None and days_to_race is not None and f_ctl is not None:
        verdict = (
            "✅ On track" if f_ctl >= ctl_target * 0.97
            else "⚠️ Marginal" if f_ctl >= ctl_target * 0.88
            else "❌ Undercooked"
        )
        summary_parts.append(f"{verdict} — forecast CTL {f_ctl:.0f} (target {ctl_target})")
    if missing:
        summary_parts.append(f"Missing: {', '.join(f.split('`')[1] for f in missing if '`' in f)}")

    print("\n".join(summary_parts))
    return md


def main():
    parser = argparse.ArgumentParser(description="Generate race-day execution plan.")
    parser.add_argument("--athlete", required=True, help="Athlete slug (e.g. jamie)")
    args = parser.parse_args()
    generate(args.athlete)


if __name__ == "__main__":
    main()
