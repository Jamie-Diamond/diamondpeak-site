#!/usr/bin/env python3
"""
Regenerate and write the Strava description for one activity.
Called as a background process from both activity-watcher and bot.

Usage: python3 ClaudeCoach/scripts/strava-update-activity.py --athlete jamie --icu-id i149586944
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

BASE        = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)
CLAUDE      = "/usr/bin/claude"

sys.path.insert(0, str(BASE / "lib"))
from icu_api import IcuClient
from strava_client import StravaClient


def build_description(first_name: str, sport: str, entry: dict, detail: dict, events: list) -> str:
    tss     = entry.get("tss") or detail.get("icu_training_load")
    np_w    = entry.get("norm_power") or detail.get("icu_weighted_avg_watts")
    ftp     = detail.get("icu_ftp") or 316
    rpe     = entry.get("rpe")
    carbs   = entry.get("nutrition_g_carb")
    feel    = entry.get("feel") or ""
    pain    = entry.get("injury_pain_during")
    dur     = entry.get("duration_min") or round((detail.get("moving_time") or 0) / 60)
    dist    = entry.get("distance_km") or (round((detail.get("distance") or 0) / 1000, 1) or None)

    metrics = []
    if np_w:  metrics.append(f"NP {np_w}W · IF {round(np_w/ftp, 2):.2f}")
    if tss:   metrics.append(f"TSS {tss}")
    if dur:   metrics.append(f"{dur} min")
    if dist:  metrics.append(f"{dist:.1f}km")
    if rpe:   metrics.append(f"RPE {rpe}")
    if carbs: metrics.append(f"{carbs}g/hr carbs")
    if pain is not None: metrics.append(f"pain {pain}/10 during")
    metrics_str = " · ".join(metrics) if metrics else "no metrics"

    if feel:
        metrics_str += f" · feel: {feel}"

    plan_block = "No planned session found."
    act_sport  = sport.lower()
    for ev in events:
        ev_sport = (ev.get("type") or ev.get("category") or "").lower()
        if ev_sport in act_sport or act_sport in ev_sport:
            pname = ev.get("name") or ev_sport
            ptss  = ev.get("icu_training_load") or ev.get("load") or "?"
            if ptss and tss:
                delta = round((float(tss) - float(ptss)) / float(ptss) * 100, 1)
                sign = "+" if delta >= 0 else ""
                plan_block = f"Planned: {pname} (target TSS {ptss}, actual {tss}, {sign}{delta}%)"
            else:
                plan_block = f"Planned: {pname}"
            break

    sport_line = sport
    if dur:  sport_line += f", {dur} min"
    if dist: sport_line += f", {dist:.1f}km"

    prompt = f"""\
Write a Strava activity description for {first_name}.

Sport: {sport_line}
{plan_block}
Metrics: {metrics_str}

Write exactly 3 lines, plain text, no markdown, no hashtags, no exclamation marks:
Line 1 — "Aim: [one plain sentence on what the session was targeting]"
Line 2 — [one dry, understated observation about how it went vs the aim. Deadpan British wit — matter-of-fact, slightly wry, never gushing. If they hit the target: note it plainly with quiet satisfaction. If they missed: a raised eyebrow, not a pep talk. No cheerleading, no "nailed it", no "amazing". If RPE or feel data is present, factor it in.]
Line 3 — "ClaudeCoach"

Examples of the right tone:
- "Held Z2 throughout. Decoupling 3.2%. The plan had a good day."
- "Came in 8% under target TSS. The legs had opinions."
- "Intervals completed. NP 4W above target. RPE 7 — earned."
- "Ran without walking. The data agrees it was Z2, mostly."

Total under 300 characters. Output nothing else."""

    try:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--model", "claude-haiku-4-5-20251001"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=60,
        )
        text = (result.stdout or "").strip()
        if text:
            return text
    except Exception as e:
        print(f"Claude call failed: {e}", file=sys.stderr)

    return f"Aim: {entry.get('name', sport)}.\n{metrics_str}\nClaudeCoach"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--athlete", required=True)
    parser.add_argument("--icu-id",  required=True, dest="icu_id")
    args = parser.parse_args()

    slug   = args.athlete
    icu_id = args.icu_id if args.icu_id.startswith("i") else f"i{args.icu_id}"

    try:
        athletes = json.loads((BASE / "config/athletes.json").read_text())
        a = athletes[slug]
        icu = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        sc  = StravaClient(slug)
    except FileNotFoundError:
        sys.exit(0)  # no tokens yet — nothing to do
    except Exception as e:
        print(f"Init error: {e}", file=sys.stderr)
        sys.exit(1)

    # Load session-log entry for this activity
    log_path = BASE / "athletes" / slug / "session-log.json"
    entry = {}
    if log_path.exists():
        try:
            for e in json.loads(log_path.read_text()):
                if str(e.get("activity_id", "")) in (icu_id, icu_id.lstrip("i")):
                    entry = e
                    break
        except Exception:
            pass

    try:
        detail   = icu.get_activity_detail(icu_id)
        strava_id = detail.get("strava_id")
        if not strava_id:
            print(f"No strava_id for {icu_id}", file=sys.stderr)
            sys.exit(0)

        act_date = (detail.get("start_date_local") or "")[:10]
        events   = icu.get_events(act_date, act_date) if act_date else []

        profile = {}
        profile_path = BASE / "athletes" / slug / "profile.json"
        if profile_path.exists():
            profile = json.loads(profile_path.read_text())
        first_name = profile.get("name", slug).split()[0]
        sport = entry.get("sport") or detail.get("type", "session")

        description = build_description(first_name, sport, entry, detail, events)
        sc.update_description(strava_id, description)
        print(f"Updated Strava {strava_id} for {slug}/{icu_id}", file=sys.stderr)
    except Exception as e:
        print(f"Update failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
