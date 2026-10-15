"""The weekly Fitness (CTL) ramp cap - ONE rule, read by everything (Jamie, 15 Oct 2026:
"probably need alignment on this as it's fundamental to everything", then "scale it").

Before this, a cap missing from athletes.json meant 5 to the plan builder, plan check,
watchdog and Peak, but 3 to goal planning (lib/goals.DEFAULT_RAMP_CAP).

    an athlete's own max_ctl_ramp_per_week   wins (a deliberate, per-athlete choice)
    otherwise, scaled by current Fitness:    3 at Fitness 20 or under, 5 at 60 or over,
                                             straight line between

Why scale: +5 Fitness a week asks for about (Fitness + 30) Load a day whatever the
athlete's fitness, so at Fitness 10 it is four times their usual training and at 90 about
1.3 times. Heart and lungs cope with either; tendons, bones and joints adapt to the
relative jump, which is where beginners and returners get hurt.
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOW_CTL, HIGH_CTL = 20.0, 60.0
LOW_CAP, HIGH_CAP = 3.0, 5.0


def scaled(ctl: float | None) -> float:
    """The default cap for this Fitness (HIGH_CAP when Fitness is unknown)."""
    if ctl is None:
        return HIGH_CAP
    if ctl <= LOW_CTL:
        return LOW_CAP
    if ctl >= HIGH_CTL:
        return HIGH_CAP
    return round(LOW_CAP + (HIGH_CAP - LOW_CAP) * (ctl - LOW_CTL) / (HIGH_CTL - LOW_CTL), 1)


def _latest_ctl(slug: str | None) -> float | None:
    """Current Fitness from the athlete's last published refresh (no API call)."""
    if not slug:
        return None
    for p in (BASE / "athletes" / slug / "training-data.json", BASE / f"training-data-{slug}.json"):
        try:
            v = (json.loads(p.read_text()).get("kpi") or {}).get("ctl")
            if v is not None:
                return float(v)
        except Exception:
            continue
    return None


def _slug_for(cfg: dict | None) -> str | None:
    """Which athlete an athletes.json entry belongs to (by Intervals.icu id)."""
    aid = (cfg or {}).get("icu_athlete_id")
    if not aid:
        return None
    try:
        for slug, c in json.loads((BASE / "config" / "athletes.json").read_text()).items():
            if isinstance(c, dict) and c.get("icu_athlete_id") == aid:
                return slug
    except Exception:
        pass
    return None


def cap(cfg: dict | None, ctl: float | None = None, slug: str | None = None) -> float:
    """The athlete's weekly ramp cap. Pass `ctl` when you have it; else it is read from
    their last refresh via `slug`."""
    own = (cfg or {}).get("max_ctl_ramp_per_week")
    if own not in (None, ""):
        try:
            return float(own)
        except (TypeError, ValueError):
            pass
    return scaled(ctl if ctl is not None else _latest_ctl(slug or _slug_for(cfg)))


def source(cfg: dict | None) -> str:
    return "set for this athlete" if (cfg or {}).get("max_ctl_ramp_per_week") not in (None, "") \
        else "scaled to current Fitness"
