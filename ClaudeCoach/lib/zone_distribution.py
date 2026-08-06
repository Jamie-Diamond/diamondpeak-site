"""Time-in-zone by sport, against the blueprint's phase target.

Feeds the Peak app's Trends > Zones view. Pure functions plus one blueprint read; the
activity list is whatever the caller already fetched, so this adds NO Intervals.icu
calls (get_training_history returns full activity objects, zone arrays included).

THE BANDS COME FROM THE BLUEPRINT ROW, not from a table in here. That is the whole
design, and it is not a stylistic choice:

    Bike  "80% Z1-2 / 12% Z3 / 8% Z4-5"     -> three bands, Z3 alone in the middle
    Run   "85% Z1-2 / 10% Z3 / 5% Z4-5"     -> same shape
    Swim  "70% Z1-2 / 20% Z3-4 / 10% Z5"    -> DIFFERENT: Z3-4 middle, Z5 top

A fixed easy/z3/z45 bucketing is correct for run and bike and WRONG for swim, and the
canonical parser proves it: plan_distribution.parse_distribution() reads that swim row
as {easy:70, z3:20, z45:0} because its top-band regex looks for "z4" and the row says
"Z5". The 10% simply vanishes and the target sums to 90%. That parser is not buggy -
it is scoped to Run and Bike, which is exactly what plan_distribution.check_week
covers - so it is deliberately NOT reused or modified here. Editing it would touch the
gated plan validator to serve a chart.

Reporting a fabricated swim target is the failure mode this module exists to avoid, so
each sport is compared against the bands its own blueprint row states, and a row that
states nothing (an empty taper distribution) yields a target of None rather than a
default. See also lib/plan_distribution.py's header on why one taxonomy is imported
rather than copied - the same reasoning applies, which is why the ZONE SOURCE below is
the only mapping this module owns.

ZONE SOURCE per sport, and why:
    Ride   icu_zone_times      power zones, the signal Jamie actually trains to.
                               "SS" (sweetspot) is DROPPED: it is an overlapping
                               marker, not a disjoint zone - a ride reporting
                               Z3 5136s also reports SS 2383s drawn from the same
                               seconds, so summing it double-counts the middle band.
    Run    gap_zone_times      grade-adjusted pace when Intervals.icu sets
           or pace_zone_times  use_gap_zone_times, raw pace otherwise. GAP is what
                               makes a hilly run classify honestly; ICU already
                               decides which is trustworthy, so we follow its flag
                               rather than second-guessing it.
    Swim   pace_zone_times     pace against threshold (CSS).
"""

import json
import re
from datetime import date, timedelta

# intervals.icu activity type -> the blueprint's display sport name. Anything absent
# (Transition, Workout, WeightTraining, Hike...) carries no zone target and is skipped
# rather than folded into a sport it does not belong to.
SPORT_DISPLAY = {
    "Ride": "Bike", "VirtualRide": "Bike", "GravelRide": "Bike", "MountainBikeRide": "Bike",
    "Run": "Run", "TrailRun": "Run", "VirtualRun": "Run",
    "Swim": "Swim", "OpenWaterSwim": "Swim",
}

_BAND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*z\s*([1-7])(?:\s*[-–—]\s*([1-7]))?", re.I)


def parse_bands(row):
    """'70% Z1-2 / 20% Z3-4 / 10% Z5' -> ordered bands with the zones each covers.

    [{'label': 'Z1-2', 'zones': [1, 2], 'target': 70.0}, ...]

    Returns None when the row states no bands at all (a blank taper row), so the
    caller can say "no target for this phase" instead of inventing one.
    """
    bands = []
    for pct, lo, hi in _BAND_RE.findall(str(row or "")):
        lo = int(lo)
        hi = int(hi) if hi else lo
        zones = list(range(lo, hi + 1))
        bands.append({
            "label": f"Z{lo}" if lo == hi else f"Z{lo}–{hi}",
            "zones": zones,
            "target": float(pct),
        })
    return bands or None


# Preferred basis per sport: the signal the athlete actually trains to.
PREFERRED_BASIS = {"Bike": "power", "Run": "pace", "Swim": "pace"}

# Below this share of the sport's moving time being classifiable, the preferred basis
# is abandoned for heart rate. It is not a nicety - measured 6 Aug 2026, the preferred
# basis alone produced two numbers that were worse than no number:
#   * Kathryn's runs: 1 of 44 carried pace zones (her run threshold was only recently
#     set in intervals.icu, so historical runs were never classified against it). The
#     page would have read "Run 48% Z1-2 vs target 78%" off a single 48-minute session
#     and presented it as four weeks of training.
#   * Calum has no power meter: 0 of 19 rides carried power zones, so a power-only
#     bike page was blank for the one sport he does.
# Heart rate is present on essentially everything (123/123 and 17/19 respectively), so
# the fallback is what makes the view exist at all for those two. The basis actually
# used is published per sport and shown on the card, because a zone split is not
# interpretable without knowing what it was measured on.
MIN_COVERAGE = 0.6

BASIS_LABEL = {"power": "power", "pace": "pace", "hr": "heart rate",
               "gap": "grade-adjusted pace"}


def zone_seconds(activity, basis):
    """{zone_index: seconds} for one activity on a given basis, or {}.

    Zone indices are 1-based to match the Z1..Z7 the blueprint and the app both speak.
    {} means this activity carries nothing usable on this basis, which the caller
    counts as unclassified rather than silently treating as easy.
    """
    sport = SPORT_DISPLAY.get(activity.get("type") or "")
    if not sport:
        return {}

    if basis == "power":
        if sport != "Bike":
            return {}
        out = {}
        for z in (activity.get("icu_zone_times") or []):
            zid = str(z.get("id") or "")
            # SS overlaps Z3/Z4 rather than partitioning time - see module header.
            if not zid.upper().startswith("Z"):
                continue
            try:
                idx = int(zid[1:])
            except ValueError:
                continue
            out[idx] = out.get(idx, 0) + int(z.get("secs") or 0)
        return out

    if basis == "hr":
        arr = activity.get("icu_hr_zone_times")
    elif sport == "Run" and activity.get("use_gap_zone_times") and \
            activity.get("gap_zone_times"):
        # GAP when intervals.icu says GAP is the trustworthy one: it is what makes a
        # hilly run classify honestly. Following ICU's own flag beats second-guessing it.
        arr = activity.get("gap_zone_times")
    else:
        arr = activity.get("pace_zone_times")

    if not arr:
        return {}
    return {i + 1: int(v or 0) for i, v in enumerate(arr) if v}


def _basis_used(activity, basis):
    """The basis label to report for one activity - distinguishes pace from GAP."""
    if basis == "pace" and SPORT_DISPLAY.get(activity.get("type") or "") == "Run" \
            and activity.get("use_gap_zone_times") and activity.get("gap_zone_times"):
        return "gap"
    return basis


def _apply_bands(secs_by_zone, bands):
    """Fold {zone: secs} into the stated bands, as percentages of classified time.

    Zones ABOVE the highest stated band fold into that top band: intervals.icu reports
    Z6/Z7 for a bike row that only names Z4-5, and time spent harder than the top band
    is still time in the top band - dropping it would understate quality.
    """
    top = max((z for b in bands for z in b["zones"]), default=7)
    total = sum(secs_by_zone.values())
    out = []
    for i, b in enumerate(bands):
        zones = set(b["zones"])
        if b["zones"] and max(b["zones"]) >= top:
            zones |= {z for z in secs_by_zone if z > top}
        secs = sum(v for z, v in secs_by_zone.items() if z in zones)
        out.append({
            "label": b["label"],
            "target": b["target"],
            "minutes": round(secs / 60, 1),
            "actual": round(secs / total * 100, 1) if total else None,
        })
    return out


def current_phase(blueprint, on):
    """The phase containing `on` (a date), or None. Inclusive of both endpoints."""
    iso = on.isoformat()
    for p in (blueprint.get("phases") or []):
        if (p.get("start") or "9999") <= iso <= (p.get("end") or "0000"):
            return p
    return None


def build(activities, blueprint, today, week_start=None):
    """The published `zoneDistribution` block.

    activities   full intervals.icu activity objects (get_training_history output)
    blueprint    athletes/<slug>/reference/training-blueprint.json, parsed
    today        date
    week_start   Monday of the current week; derived when omitted
    """
    if week_start is None:
        week_start = today - timedelta(days=today.weekday())

    phase = current_phase(blueprint, today) or {}
    dist = phase.get("distribution") or {}

    # Phase-to-date, not the whole phase: comparing a week-old block against a target
    # meant for five weeks reads as a miss that has not happened yet.
    phase_start = phase.get("start")
    windows = {
        "week": {"from": week_start.isoformat(), "to": today.isoformat(),
                 "label": "this week"},
        "r4": {"from": (today - timedelta(days=27)).isoformat(), "to": today.isoformat(),
               "label": "rolling 4 weeks"},
    }
    if phase_start:
        windows["phase"] = {"from": phase_start, "to": today.isoformat(),
                            "label": f"{phase.get('name') or 'phase'} to date"}

    def _accumulate(acts, basis):
        """Fold one sport's activities on one basis. Returns the accumulator."""
        acc = {"secs": {}, "sessions": len(acts), "unclassified": 0,
               "moving": 0, "labels": set()}
        for a in acts:
            acc["moving"] += int(a.get("moving_time") or 0)
            zs = zone_seconds(a, basis)
            if not zs:
                acc["unclassified"] += 1
                continue
            acc["labels"].add(_basis_used(a, basis))
            for z, v in zs.items():
                acc["secs"][z] = acc["secs"].get(z, 0) + v
        return acc

    sports = {}
    for wid, w in windows.items():
        by_sport = {}
        for a in (activities or []):
            sport = SPORT_DISPLAY.get(a.get("type") or "")
            if not sport:
                continue
            day = (a.get("start_date_local") or "")[:10]
            if day and w["from"] <= day <= w["to"]:
                by_sport.setdefault(sport, []).append(a)

        out = {}
        for sport, acts in by_sport.items():
            basis = PREFERRED_BASIS.get(sport, "hr")
            acc = _accumulate(acts, basis)
            # Coverage against MOVING time, not session count: one classified 5h ride
            # among four unclassified 40-minute ones is better coverage than the count
            # suggests, and the reverse is the case that actually misleads.
            covered = (acc["moving"] and sum(acc["secs"].values()) / acc["moving"]) or 0
            if covered < MIN_COVERAGE and basis != "hr":
                alt = _accumulate(acts, "hr")
                alt_cov = (alt["moving"] and sum(alt["secs"].values()) / alt["moving"]) or 0
                if alt_cov > covered:
                    acc, basis, covered = alt, "hr", alt_cov

            bands = parse_bands(dist.get(sport))
            total = sum(acc["secs"].values())
            has = bool(bands and total)
            out[sport] = {
                "minutes": round(total / 60, 1),
                "sessions": acc["sessions"],
                "unclassified_sessions": acc["unclassified"],
                # What the split was ACTUALLY measured on, per sport - not the
                # preferred basis. Kathryn's runs fall back to heart rate and Jamie's
                # do not, so a single page-level label would be wrong for one of them.
                "basis": BASIS_LABEL.get(
                    sorted(acc["labels"])[0] if len(acc["labels"]) == 1 else basis, basis),
                # Share of moving time that could be classified at all. Published so
                # the app can withhold or caveat a split drawn from a thin slice
                # instead of presenting it as the whole window. CLAMPED to 100:
                # intervals.icu totals its zone seconds over timer time, which runs a
                # percent or two beyond moving_time, and "coverage 101.7%" reads as a
                # bug in the figure rather than the rounding artefact it is.
                "coverage": min(100.0, round(covered * 100, 1)),
                # Stated EXPLICITLY rather than left implicit in an empty `bands`,
                # because the publishing sanitiser's Records spec turns a None into [],
                # so "no target for this phase" and "target of nothing" would arrive at
                # the app indistinguishable. A taper states no distribution at all and
                # must not read as a target the athlete is missing.
                "target_stated": has,
                "bands": _apply_bands(acc["secs"], bands) if has else None,
                "zones": {f"Z{z}": round(v / 60, 1) for z, v in sorted(acc["secs"].items())},
            }
        sports[wid] = out

    return {
        "phase": phase.get("name"),
        # The PREFERRED basis per sport. What each sport actually used in each window
        # is on the sport itself, since it varies by athlete and by data coverage.
        "basis": {k: BASIS_LABEL[v] for k, v in PREFERRED_BASIS.items()},
        "min_coverage": round(MIN_COVERAGE * 100),
        "windows": windows,
        "sports": sports,
    }


def build_for_slug(base, slug, activities, today):
    """Convenience wrapper: reads the athlete's blueprint, returns None if absent."""
    try:
        bp = json.loads((base / f"athletes/{slug}/reference/training-blueprint.json").read_text())
    except Exception:
        return None
    return build(activities, bp, today)
