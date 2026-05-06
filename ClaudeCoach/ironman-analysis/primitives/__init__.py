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
from primitives.modulation import (
    SessionPrescription,
    modulate_session,
)
from primitives.env_pacing import (
    EnvAdjustment,
    heat_correction_fraction,
    humidity_correction_fraction,
    headwind_component,
    wind_time_tax_min,
    adjust_bike_if,
    adjust_run_pace,
    race_day_targets,
    format_pace,
    format_if,
)
from primitives.compliance import (
    ComplianceRecord,
    classify_gap,
    tss_gap_series,
    rolling_compliance,
    forward_correction_factor,
    compliance_recommendations,
)
from primitives.reoptimise import (
    WeekDebt,
    assess_week_debt,
    ramp_headroom,
    apply_compliance_correction,
    quality_session_spacing_ok,
)
from primitives.debrief import (
    LapMetrics,
    DebriefResult,
    POWER_ZONES,
    lap_drift,
    hr_power_decoupling,
    power_zone_distribution,
    session_quality_label,
    build_debrief,
)

__all__ = [
    # load
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
    # modulation
    "SessionPrescription",
    "modulate_session",
    # env_pacing
    "EnvAdjustment",
    "heat_correction_fraction",
    "humidity_correction_fraction",
    "headwind_component",
    "wind_time_tax_min",
    "adjust_bike_if",
    "adjust_run_pace",
    "race_day_targets",
    "format_pace",
    "format_if",
    # compliance
    "ComplianceRecord",
    "classify_gap",
    "tss_gap_series",
    "rolling_compliance",
    "forward_correction_factor",
    "compliance_recommendations",
    # reoptimise
    "WeekDebt",
    "assess_week_debt",
    "ramp_headroom",
    "apply_compliance_correction",
    "quality_session_spacing_ok",
    # debrief
    "LapMetrics",
    "DebriefResult",
    "POWER_ZONES",
    "lap_drift",
    "hr_power_decoupling",
    "power_zone_distribution",
    "session_quality_label",
    "build_debrief",
]
