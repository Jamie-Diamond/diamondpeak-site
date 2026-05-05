"""load.py — daily training load primitives.

Pure functions. No MCP coupling, no IO. Take dicts/lists matching observed
IcuSync MCP shapes (see schemas/intervals_icu.md) and return dataclasses
or plain dicts.

Methodology — locked:
    * Banister formulation: X_t = X_{t-1} + (TSS_t - X_{t-1}) / TC
        - CTL: TC = 42 days  ("fitness")
        - ATL: TC =  7 days  ("fatigue")
        - TSB: CTL - ATL     ("form")
    * TSB reported absolute and as % of CTL (TSB / CTL * 100). API gives
      absolute; project doc uses %. Always render both.
    * Daily TSS bucketed by athlete-local date parsed from the activity's
      ISO datetime string (already in athlete tz; no tz suffix in IcuSync).
    * Future-dated rows (after today) with no activity data are
      zero-training projections, not plans. Flagged via is_projection.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CTL_TC = 42  # days — Banister long-term time constant
ATL_TC = 7   # days — Banister short-term time constant

# Build-table CTL targets from project knowledge.
# Each entry: (target_date, target_ctl_low, target_ctl_high, label).
# Race day 19 Sept 2026.
BUILD_TABLE: list[tuple[date, float, float, str]] = [
    (date(2026, 5, 31),  82.0,  88.0, "End base"),
    (date(2026, 6, 30),  92.0,  98.0, "End build"),
    (date(2026, 7, 31), 102.0, 108.0, "End specific"),
    (date(2026, 8, 15), 110.0, 115.0, "Peak"),
    (date(2026, 8, 31), 107.0, 113.0, "Pre-taper"),
    (date(2026, 9, 19),  95.0, 100.0, "Race day"),
]

# Flag thresholds (project standing rules).
RAMP_FLAG_THRESHOLD = 4.0       # CTL/week ramp considered too hot while ankle in rehab
ATL_CTL_GAP_THRESHOLD = 25.0    # absolute units; sustained >25 over CTL is the flag line
GAP_DAYS_FLAG = 5               # consecutive days at/above gap threshold = forced recovery


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoadPoint:
    """A single day in the load timeline."""
    date: date
    tss: float
    ctl: float
    atl: float
    tsb: float
    tsb_pct: float          # TSB as % of CTL (0 if CTL is 0)
    is_projection: bool     # True if this is a zero-training future row

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d


@dataclass(frozen=True)
class Flag:
    """A flagged condition with context."""
    code: str
    severity: str           # "warn" | "alert"
    message: str
    triggered_on: date


# ---------------------------------------------------------------------------
# Activity dedup + daily TSS
# ---------------------------------------------------------------------------

def _parse_activity_date(activity: dict) -> date | None:
    """Extract the local date from an activity dict.

    IcuSync returns ISO datetimes already in athlete-local time, with no tz
    suffix (e.g. '2026-04-25T07:27:16'). We trust that contract.
    """
    raw = activity.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        # Fall back to date-only string
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def dedupe_activities(activities: Iterable[dict]) -> list[dict]:
    """Remove duplicate activities.

    Two-pass strategy:
        1. Drop exact id collisions (last wins — ids should be unique anyway).
        2. Drop rows that share `(date, duration_minutes, normalized_power, tss)`
           with a previous row, even if their ids differ. This catches the
           Garmin-side duplication case (e.g. 14 Apr 2026: two ids,
           identical metrics, different names — counted twice in CTL/ATL
           if not deduped).

    The secondary key only fires when all components are non-null and the
    duration is positive. Walks/swims with null power are still safe — they
    only collide if their datetime AND duration AND TSS match exactly.
    """
    by_id: dict[str, dict] = {}
    for a in activities:
        aid = a.get("id")
        if aid is None:
            continue
        by_id[aid] = a

    seen: set[tuple] = set()
    out: list[dict] = []
    for a in by_id.values():
        key = (
            a.get("date"),
            a.get("duration_minutes"),
            a.get("normalized_power"),
            a.get("tss"),
        )
        # Only treat as dedup-collision if duration and tss are present
        if key[1] and key[3] is not None and key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def daily_tss(activities: Iterable[dict]) -> dict[date, float]:
    """Sum TSS per athlete-local date. Activities deduped first."""
    out: dict[date, float] = defaultdict(float)
    for a in dedupe_activities(activities):
        d = _parse_activity_date(a)
        tss = a.get("tss")
        if d is None or tss is None:
            continue
        out[d] += float(tss)
    return dict(out)


# ---------------------------------------------------------------------------
# Banister EWMA
# ---------------------------------------------------------------------------

def _safe_pct(numerator: float, denom: float) -> float:
    return (numerator / denom) * 100.0 if denom > 0 else 0.0


def banister_series(
    daily: dict[date, float],
    start: date,
    end: date,
    seed_ctl: float = 0.0,
    seed_atl: float = 0.0,
    today: date | None = None,
) -> list[LoadPoint]:
    """Compute CTL/ATL/TSB across [start, end] inclusive using Banister EWMA.

    Args:
        daily: dict[date, total_tss]. Missing dates = 0 TSS (true rest day).
        start: first date in the output series.
        end: last date in the output series (inclusive).
        seed_ctl/seed_atl: values just before `start`. If unknown, pass 0
            but allow ~6 weeks of warm-up before drawing conclusions.
        today: athlete-local "today". Days strictly after `today` with no
            entry in `daily` are flagged is_projection=True (zero-training
            future projection, not training plan).

    Returns:
        list[LoadPoint] in date order.
    """
    if today is None:
        today = max(daily.keys()) if daily else end

    points: list[LoadPoint] = []
    ctl, atl = float(seed_ctl), float(seed_atl)

    cur = start
    while cur <= end:
        tss = float(daily.get(cur, 0.0))
        ctl = ctl + (tss - ctl) / CTL_TC
        atl = atl + (tss - atl) / ATL_TC
        tsb = ctl - atl
        tsb_pct = _safe_pct(tsb, ctl)
        is_proj = cur > today and cur not in daily
        points.append(
            LoadPoint(
                date=cur,
                tss=round(tss, 1),
                ctl=round(ctl, 2),
                atl=round(atl, 2),
                tsb=round(tsb, 2),
                tsb_pct=round(tsb_pct, 1),
                is_projection=is_proj,
            )
        )
        cur += timedelta(days=1)
    return points


def fitness_rows_to_loadpoints(
    fitness: Iterable[dict],
    today: date,
    daily: dict[date, float] | None = None,
) -> list[LoadPoint]:
    """Adapt API `get_fitness` rows into LoadPoint objects.

    Use this when you want the API's authoritative CTL/ATL/TSB rather than
    the locally recomputed Banister values (cross-checking is still wise —
    see `compare_to_api` in tests).
    """
    daily = daily or {}
    out: list[LoadPoint] = []
    for row in fitness:
        d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        ctl = float(row.get("ctl") or 0.0)
        atl = float(row.get("atl") or 0.0)
        tsb = float(row.get("tsb") or (ctl - atl))
        tsb_pct = _safe_pct(tsb, ctl)
        # API future rows are zero-training projections by definition
        is_proj = d > today
        out.append(
            LoadPoint(
                date=d,
                tss=round(float(daily.get(d, 0.0)), 1),
                ctl=round(ctl, 2),
                atl=round(atl, 2),
                tsb=round(tsb, 2),
                tsb_pct=round(tsb_pct, 1),
                is_projection=is_proj,
            )
        )
    return sorted(out, key=lambda p: p.date)


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def weekly_ramp(points: Sequence[LoadPoint]) -> list[tuple[date, float]]:
    """Return list of (date, 7d ΔCTL) for each point with >=7 days of history.

    Ramp at day t = CTL_t - CTL_{t-7}.
    """
    by_date = {p.date: p for p in points}
    out: list[tuple[date, float]] = []
    for p in points:
        prev = by_date.get(p.date - timedelta(days=7))
        if prev is None:
            continue
        out.append((p.date, round(p.ctl - prev.ctl, 2)))
    return out


def atl_ctl_gap_streak(points: Sequence[LoadPoint], gap: float = ATL_CTL_GAP_THRESHOLD) -> dict:
    """Track consecutive days where ATL exceeds CTL by more than `gap`.

    Returns:
        {
            "current_streak_days": int,            # ending on the most recent point
            "longest_streak_days": int,
            "longest_streak_ended": date | None,
            "in_breach_today": bool,
        }

    Note: only inspects historical (non-projection) points.
    """
    actual = [p for p in points if not p.is_projection]
    longest = 0
    longest_end: date | None = None
    cur = 0
    for p in actual:
        if (p.atl - p.ctl) > gap:
            cur += 1
            if cur > longest:
                longest = cur
                longest_end = p.date
        else:
            cur = 0
    in_breach = bool(actual) and (actual[-1].atl - actual[-1].ctl) > gap
    return {
        "current_streak_days": cur,
        "longest_streak_days": longest,
        "longest_streak_ended": longest_end,
        "in_breach_today": in_breach,
    }


def trajectory_check(
    points: Sequence[LoadPoint],
    targets: Sequence[tuple[date, float, float, str]] = BUILD_TABLE,
) -> list[dict]:
    """For each build-table milestone, report the projected/actual CTL band match.

    For past targets, uses actual CTL on that date.
    For future targets, walks forward from today's CTL assuming a continued
    ramp equal to the average of the last 14 days' weekly ramps. This is a
    naive projection — it answers "where am I heading at current ramp" not
    "where will I be with the planned weeks ahead".

    Returns list of dicts:
        {
            "label": str,
            "target_date": date,
            "target_ctl_low": float,
            "target_ctl_high": float,
            "ctl_on_target_date": float,         # actual or projected
            "is_projected": bool,
            "delta_low": float,                  # ctl - low (negative = behind)
            "delta_high": float,                 # ctl - high (positive = ahead)
            "status": "below" | "on_track" | "above",
        }
    """
    if not points:
        return []

    by_date = {p.date: p for p in points if not p.is_projection}
    if not by_date:
        return []

    last_actual = max(by_date.keys())
    last_ctl = by_date[last_actual].ctl

    # Average weekly ramp across last 14 days, two windows
    ramps = weekly_ramp(points)
    recent_ramps = [r for d, r in ramps if d >= last_actual - timedelta(days=14)]
    avg_weekly_ramp = sum(recent_ramps) / len(recent_ramps) if recent_ramps else 0.0
    daily_ramp = avg_weekly_ramp / 7.0

    out: list[dict] = []
    for target_date, lo, hi, label in targets:
        if target_date <= last_actual:
            actual_pt = by_date.get(target_date)
            ctl_at = actual_pt.ctl if actual_pt else last_ctl
            projected = False
        else:
            days_ahead = (target_date - last_actual).days
            ctl_at = round(last_ctl + daily_ramp * days_ahead, 2)
            projected = True

        delta_low = round(ctl_at - lo, 2)
        delta_high = round(ctl_at - hi, 2)
        if delta_low < 0:
            status = "below"
        elif delta_high > 0:
            status = "above"
        else:
            status = "on_track"

        out.append(
            {
                "label": label,
                "target_date": target_date,
                "target_ctl_low": lo,
                "target_ctl_high": hi,
                "ctl_on_target_date": ctl_at,
                "is_projected": projected,
                "delta_low": delta_low,
                "delta_high": delta_high,
                "status": status,
            }
        )
    return out


def flag_conditions(
    points: Sequence[LoadPoint],
    *,
    ankle_in_rehab: bool = True,
    today: date | None = None,
) -> list[Flag]:
    """Evaluate project standing-rule flags.

    Rules encoded:
        * If `ankle_in_rehab`, ramp >4 CTL/wk is a hard flag.
        * ATL exceeds CTL by >25-30 for >5 consecutive days = forced recovery.
        * (Run-km >10% weekly increase: Phase 2, lives in volume.py.)
    """
    flags: list[Flag] = []
    actual = [p for p in points if not p.is_projection]
    if not actual:
        return flags
    today = today or actual[-1].date

    # 1. Ramp flag
    ramps = weekly_ramp(points)
    recent_ramps = [(d, r) for d, r in ramps if d >= today - timedelta(days=7)]
    if recent_ramps and ankle_in_rehab:
        max_recent = max(recent_ramps, key=lambda x: x[1])
        if max_recent[1] > RAMP_FLAG_THRESHOLD:
            flags.append(
                Flag(
                    code="ramp_too_hot_ankle",
                    severity="alert",
                    message=(
                        f"7-day ramp hit +{max_recent[1]:.1f} CTL on "
                        f"{max_recent[0].isoformat()}; cap is +{RAMP_FLAG_THRESHOLD:.1f} "
                        f"while ankle is in rehab. Pull bike volume, hold run flat."
                    ),
                    triggered_on=max_recent[0],
                )
            )

    # 2. ATL-CTL gap flag
    streak = atl_ctl_gap_streak(points)
    if streak["current_streak_days"] >= GAP_DAYS_FLAG:
        flags.append(
            Flag(
                code="atl_ctl_gap_sustained",
                severity="alert",
                message=(
                    f"ATL has exceeded CTL by >{ATL_CTL_GAP_THRESHOLD:.0f} for "
                    f"{streak['current_streak_days']} consecutive days. Force a recovery day."
                ),
                triggered_on=actual[-1].date,
            )
        )
    elif streak["in_breach_today"]:
        flags.append(
            Flag(
                code="atl_ctl_gap_today",
                severity="warn",
                message=(
                    f"ATL exceeds CTL by >{ATL_CTL_GAP_THRESHOLD:.0f} today "
                    f"({actual[-1].atl - actual[-1].ctl:+.1f}). Watch for {GAP_DAYS_FLAG}-day rule."
                ),
                triggered_on=actual[-1].date,
            )
        )

    return flags


# ---------------------------------------------------------------------------
# Actual vs projection split
# ---------------------------------------------------------------------------

def separate_actual_projection(
    points: Sequence[LoadPoint], today: date
) -> tuple[list[LoadPoint], list[LoadPoint]]:
    """Split a series into (historical_actual, future_projection).

    Future is anything strictly after `today`. The cut is by date, not by
    the `is_projection` flag, so this works on series built from the API
    where future rows are projection-only.
    """
    actual = [p for p in points if p.date <= today]
    projection = [p for p in points if p.date > today]
    return actual, projection
