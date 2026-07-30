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
import public_text_guard


def build_description(first_name: str, sport: str, entry: dict, detail: dict, events: list) -> str:
    tss     = entry.get("tss") or detail.get("icu_training_load")
    np_w    = entry.get("norm_power") or detail.get("icu_weighted_avg_watts")
    ftp     = detail.get("icu_ftp") or 316
    # RPE and injury/pain scores are private — never published to Strava.
    carbs   = entry.get("nutrition_g_carb")
    # `feel` is NEVER published either. It was stripped 2026-07-30: removing the `rpe`
    # and `injury_pain_during` fields from the metrics line did not close the leak,
    # because `feel` is free text the athlete writes FOR THE COACH and it routinely
    # contains both banned categories in their own words, plus medical detail. Real
    # examples that were being pushed to a public description verbatim:
    #   "Stomach unsettled pre-run, emergency loo stop ~10k in; ankle 1/10 from 15k"
    #   "Ankle 2/10 first 4k then settled to 0."
    #   "Most of the ride RPE 4 (steady); 350-400W efforts felt RPE 9/10"
    # A pattern-based scrub of free text would leak again on the next phrasing, so the
    # field is simply not published. Third private-data leak into public Strava; treat
    # any new athlete-authored free-text field as private by default.
    feel    = ""
    dur     = entry.get("duration_min") or round((detail.get("moving_time") or 0) / 60)
    dist    = entry.get("distance_km") or (round((detail.get("distance") or 0) / 1000, 1) or None)

    # Ambient temp is ONLY trustworthy when the activity has real weather-station
    # data (has_weather). The watch onboard sensor over-reads and must NEVER be
    # cited as ambient — so if has_weather is false, drop temp entirely.
    has_weather = bool(detail.get("has_weather"))
    avg_t   = detail.get("average_temp") if has_weather else None
    min_t   = detail.get("min_temp")     if has_weather else None
    max_t   = detail.get("max_temp")     if has_weather else None
    # Heat is only a real stimulus at confirmed ambient ≥25°C.
    heat_confirmed = has_weather and max(
        v for v in (avg_t, max_t) if v is not None
    ) >= 25 if (avg_t is not None or max_t is not None) else False

    metrics = []
    if np_w:  metrics.append(f"NP {np_w}W · IF {round(np_w/ftp, 2):.2f}")
    if tss:   metrics.append(f"TSS {tss}")
    if dur:   metrics.append(f"{dur} min")
    if dist:  metrics.append(f"{dist:.1f}km")
    # nutrition_g_carb is a TOTAL in grams, never a rate — derive g/hr from duration.
    if carbs:
        if dur:
            metrics.append(f"{carbs}g carbs ({round(carbs / (dur / 60))}g/hr)")
        else:
            metrics.append(f"{carbs}g carbs")
    if avg_t is not None:
        t_str = f"{round(avg_t)}°C avg"
        if min_t is not None and max_t is not None:
            t_str += f" ({min_t}–{max_t}°C)"
        metrics.append(t_str)
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

    heat_rule = (
        "You MAY note the heat as a training factor."
        if heat_confirmed else
        "Do NOT mention heat, temperature, or weather — no confirmed ambient ≥25°C data is available for this session."
    )

    prompt = f"""\
Write a Strava activity description for {first_name}.

Sport: {sport_line}
{plan_block}
Metrics: {metrics_str}

Write exactly 3 lines, plain text, no markdown, no hashtags, no exclamation marks:
Line 1 — "Aim: [one plain sentence on what the session was targeting]"
Line 2 — [one neutral, factual sentence describing what was actually done — key metrics (zones, NP/IF, decoupling, pace). Describe what happened, NOT how it deviated from plan. NEVER snide, sarcastic, wry, or negative. NEVER imply the athlete quit, gave up, fell short, or underperformed. A shorter-than-planned session is reported plainly by its actual numbers, with no commentary on the gap. Never state RPE or any injury/pain score — those are private. {heat_rule}]
Line 3 — "ClaudeCoach"

Examples of the right tone (neutral, factual, no judgement):
- "Held Z2 throughout, 107 min in zone. Decoupling 3.2%."
- "1.2km open water, avg 2:03/100m, HR 129. Firm aerobic effort in the heat."
- "Intervals completed at NP 254W, IF 0.85. Steady across all reps."
- "8.9km continuous, avg GAP 5:10/km, HR 143 — within the Z2 band."

Total under 300 characters. Output nothing else."""

    fallback = f"Aim: {entry.get('name', sport)}.\n{metrics_str}\nClaudeCoach"
    try:
        result = subprocess.run(
            [CLAUDE, "-p", "--model", "claude-haiku-4-5-20251001"],
            input=prompt,  # prompt on stdin, not argv (MAX_ARG_STRLEN)
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=60,
        )
        text = (result.stdout or "").strip()
        if result.returncode != 0 or not _looks_like_description(text):
            print(f"Claude returned no usable description (rc={result.returncode}): {text[:120]!r}", file=sys.stderr)
            return fallback
        return text
    except Exception as e:
        print(f"Claude call failed: {e}", file=sys.stderr)

    return fallback


def _looks_like_description(text: str) -> bool:
    """Reject CLI error output so it is never written to Strava as a description."""
    if not text:
        return False
    low = text.lower()
    bad_markers = (
        "api error", "failed to authenticate", "invalid authentication",
        "401", "403", "429", "rate limit", "credit balance", "usage limit",
        "overloaded", "internal server error", "execution error", "command not found",
    )
    if any(m in low for m in bad_markers):
        return False
    # A real description is multi-line and ends on the ClaudeCoach signature.
    return "claudecoach" in low


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
        # Hard gate, not a prompt request. The LLM writes line 2 of this description
        # freely, so even with `feel` stripped it can still surface a pain score it saw
        # in its context. Fails CLOSED: no description beats a leaked one. Three prior
        # leaks (25 Jul, and twice on 30 Jul) all got through prompt-level guards.
        description = public_text_guard.assert_publishable(description)
        sc.update_description(strava_id, description)
        print(f"Updated Strava {strava_id} for {slug}/{icu_id}", file=sys.stderr)
    except Exception as e:
        print(f"Update failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
