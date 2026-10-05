"""What an athlete's training fuelling climbs toward (Jamie, 5 Oct 2026: "Non race
athletes shouldn't get feedback about bonking in races. Nutrition suggestions should be
relative to their training ... they need to work out their nutrition, it shouldn't just
be my nutrition pushed onto them").

Every fuelling prescription used `nutrition_target_g_hr or 90`, so an athlete with no
figure of their own got Jamie's Ironman race rate. Fred's FTP-goal weekly summary told
him he was 40 g/hr short of 90 and risked bonking in a race he does not have.

    race athlete       A race ahead, or no goal block (a racer between races): ramps
                       toward their race figure, `nutrition_target_g_hr`, as before
    non-race athlete   a goal block (lib/goals.py) and no A race ahead: ramps toward
                       the figure they agreed with the coach, `training_fuel_g_hr`
                       (plan_tools.py training-fuel --save), else TRAINING_CAP_G_HR

A non-race athlete's case for eating is training quality and recovery, never race day:
`non_race()` is what the weekly summary, check-ins and session notes branch on.
"""
from __future__ import annotations

from datetime import date

TRAINING_CAP_G_HR = 60      # a training level, not a race level, until they agree their own
RACE_DEFAULT_G_HR = 90      # unchanged default for a racer with no figure set


def non_race(cfg: dict, today: date | None = None) -> bool:
    """True for an athlete training toward a goal with no A race ahead."""
    import goals
    import plan_tools               # lazy: plan_tools imports this module's callers
    return bool(goals.goal_cfg(cfg)) and not plan_tools.a_race_ahead(cfg or {}, today)


def agreed(cfg: dict) -> int | None:
    """The training figure the athlete agreed with the coach, else None."""
    try:
        v = int((cfg or {}).get("training_fuel_g_hr") or 0)
    except (TypeError, ValueError):
        return None
    return v or None


def ceiling(cfg: dict, today: date | None = None) -> int:
    """g/hr the ride prescription ramps toward."""
    if non_race(cfg, today):
        return agreed(cfg) or TRAINING_CAP_G_HR
    return int((cfg or {}).get("nutrition_target_g_hr") or RACE_DEFAULT_G_HR)


def run_ceiling(cfg: dict, today: date | None = None) -> int | None:
    """g/hr the long-run prescription ramps toward; None = the run training default."""
    if non_race(cfg, today):
        return agreed(cfg) or TRAINING_CAP_G_HR
    return (cfg or {}).get("nutrition_run_target_g_hr")


def label(cfg: dict, today: date | None = None) -> str:
    """How to name the ceiling to the athlete."""
    if not non_race(cfg, today):
        return "race target"
    return "your agreed training figure" if agreed(cfg) else "training level"
