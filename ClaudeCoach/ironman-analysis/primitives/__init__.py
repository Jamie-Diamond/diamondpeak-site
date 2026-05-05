"""Pure-function training analytics primitives.

Each module owns one analytical concern. Functions take dicts/lists matching
observed IcuSync MCP shapes (see ../schemas/) and return dataclasses or
plain dicts. No MCP coupling, no IO, no globals.
"""

from primitives.load import (
    LoadPoint,
    Flag,
    BUILD_TABLE,
    dedupe_activities,
    daily_tss,
    banister_series,
    weekly_ramp,
    atl_ctl_gap_streak,
    trajectory_check,
    flag_conditions,
    separate_actual_projection,
)

__all__ = [
    "LoadPoint",
    "Flag",
    "BUILD_TABLE",
    "dedupe_activities",
    "daily_tss",
    "banister_series",
    "weekly_ramp",
    "atl_ctl_gap_streak",
    "trajectory_check",
    "flag_conditions",
    "separate_actual_projection",
]
