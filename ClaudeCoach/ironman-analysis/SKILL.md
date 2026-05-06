# Ironman analysis — invocation contract

When the athlete asks for training analysis, use this package. It encodes the locked methodology so reviews stay consistent across the 21-week build.

## When to use

- Weekly reviews ("review last week's training")
- Trajectory checks ("am I on track for the build-table CTL targets?")
- Flag evaluations (ramp too high, ATL-CTL gap sustained, fatigue convergence)
- Any time an arithmetic answer is needed about training load, volume, heat acclimation, HRV trend, or session quality — let the code answer, not vibes

## Data contract

Primitives are pure: dicts/lists in, dicts/lists out. No MCP coupling.

| Function | Inputs | Output |
| --- | --- | --- |
| `dedupe_activities` | `list[dict]` from `get_training_history` | Deduplicated list. Primary key: `id`. Secondary key: `(date, duration_minutes, normalized_power, tss)` to catch Garmin-side duplicates with distinct ids. |
| `daily_tss` | activities | `dict[date, float]` — TSS summed by athlete-local date |
| `banister_series` | `daily_tss`, seed CTL/ATL, date range | `list[LoadPoint]` — date, TSS, CTL, ATL, TSB, TSB%, is_projection |
| `weekly_ramp` | `list[LoadPoint]` | `list[(date, ΔCTL_7d)]` |
| `atl_ctl_gap_streak` | `list[LoadPoint]` (or fitness rows) | Longest run of consecutive days where `ATL - CTL > 25`, plus current streak |
| `trajectory_check` | `list[LoadPoint]`, `dict[date, float]` build targets | Per-target days-ahead/behind in CTL terms |
| `flag_conditions` | `list[LoadPoint]`, ankle_in_rehab=True | `list[Flag]` per project rules |
| `separate_actual_projection` | fitness rows, today | `(actual, projection)` tuple |
| `adjust_bike_if` | `target_if`, `temp_c`, `dew_point_c`, `segment`, `wind_speed_ms`, `wind_from_deg` | `EnvAdjustment` — adjusted IF, component corrections, wind time tax, L2 reasoning trail |
| `adjust_run_pace` | `target_pace_sec_per_km`, `temp_c`, `dew_point_c`, `segment`, wind args | `EnvAdjustment` — adjusted pace (sec/km), corrections, L2 trail |
| `race_day_targets` | `bike_target_if`, `run_target_pace_sec_per_km`, forecast args | All-segment dict + summary (conservative IF, adjusted pace, correction %) |
| `heat_correction_fraction` | `temp_c` | Fraction (e.g. -0.02 at 28°C) |
| `humidity_correction_fraction` | `dew_point_c` | Fraction (e.g. -0.006 at 18°C DP) |
| `headwind_component` | `wind_speed_ms`, `wind_from_deg`, `course_heading_deg` | Signed m/s — positive = headwind |
| `format_pace` | `sec_per_km` | String e.g. "5:06/km" |

## Required MCP calls (when running live)

1. `get_athlete_profile` — anchors `current_date_local`, timezone, FTP. Always first.
2. `get_fitness(oldest, newest)` — historical CTL/ATL/TSB to seed and cross-check our recomputation.
3. `get_training_history(days)` — activities for `daily_tss`. Volume can overflow context; for >30 days, fetch in chunks or filter by sport.
4. `get_wellness(oldest, newest)` — HRV/RHR/sleep for HRV phase (Phase 3).

## Reporting conventions

- TSB always reported absolute *and* percentage, e.g. "TSB -16 / -22%".
- Future-dated fitness rows clearly labelled as zero-training projections.
- Flag conditions explicit: ramp >4 CTL/wk while ankle in rehab, ATL-CTL gap >25 for >5 consecutive days, weekly run-km increase >10%.
- Garmin attribution at the foot of any output that includes activity-detail data: "Data provided by Garmin®"

## Methodological commitments

- Banister formulation: `X_t = X_{t-1} + (TSS_t - X_{t-1}) / TC` with TC=42 (CTL), TC=7 (ATL).
- Day-of-activity TSS bucketed by **athlete-local date** (parsed from the ISO datetime string returned by IcuSync — already in athlete tz). Never use system date.
- Multi-signal corroboration required before suggesting load reduction. HRV alone is never the trigger.

## Athlete-specific rules baked in

- Ankle return-to-run ongoing — hard flag at >10% weekly run-km while ankle unconfirmed.
- FTP authority: Intervals.icu profile (currently 316 W). Never override.
- Subjective wellness fields: ignored. The athlete does not log them; HRV + RHR + sleep are sufficient.
