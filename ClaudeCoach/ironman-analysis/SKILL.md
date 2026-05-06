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
| `wind_time_tax_min` | `headwind_ms`, `distance_km`, `base_speed_ms` | Minutes added by wind on a segment |
| `format_pace` | `sec_per_km` | String e.g. "5:06/km" |
| `format_if` | `intensity_factor` | String e.g. "0.692" |
| `modulate_session` | `planned: dict`, `readiness: dict` | `SessionPrescription` — adjusted session + fired rules + L2 trails + summary |
| `classify_gap` | `planned_tss`, `actual_tss`, `planned_duration_min`, `actual_duration_min`, `rpe` | Gap classification string (see below) |
| `tss_gap_series` | `planned_events`, `actual_activities`, `session_log` | `list[ComplianceRecord]` — matched planned vs actual with classifications |
| `rolling_compliance` | `list[ComplianceRecord]` | `dict` — compliance_rate, completion_rate, dominant_gap_type, classification_counts |
| `forward_correction_factor` | `compliance_rate: float` | Multiplier for quality TSS targets (1.0 if no correction needed; only valid when dominant gap = intensity_short_soft) |
| `compliance_recommendations` | compliance metrics dict | `list[str]` — actionable fix per dominant gap type |
| `assess_week_debt` | `planned_sessions`, `actual_sessions`, `today` | `WeekDebt` — debt TSS, redistributable flag, reason |
| `ramp_headroom` | `current_ctl`, `weekly_planned_tss`, `ankle_in_rehab` | Max additional TSS available this week without breaching ramp cap |
| `apply_compliance_correction` | `sessions`, `correction_factor` | Sessions list with quality TSS targets scaled (Z2/swim/strength unchanged) |
| `quality_session_spacing_ok` | `new_session_date`, `existing_sessions` | Bool — False if placing a quality session here creates back-to-back quality days |
| `build_debrief` | `activity`, `raw_laps`, `ftp`, `planned_tss` | `DebriefResult` — drift, decoupling, zone distribution, quality label, flags |
| `lap_drift` | `laps`, `attr` | % change first→last lap for avg_watts / avg_hr / avg_pace_s_per_km |
| `hr_power_decoupling` | `laps` | % change in HR:power ratio first→second half (>5% = aerobic stress flag) |
| `power_zone_distribution` | `laps`, `ftp` | Dict of Coggan zone → seconds (lap-average approximation) |
| `session_quality_label` | `execution_pct`, `decoupling_pct` | "executed_well" \| "adequate" \| "undercooked" \| "overdone" |

### `DebriefResult` fields

| Field | Type | Notes |
|---|---|---|
| `session_name` | str | From activity name or workout_name |
| `sport` | str | bike \| run \| swim |
| `actual_tss` | float | |
| `planned_tss` | float or None | From get_events; None if not set |
| `execution_pct` | float or None | actual/planned; None if no planned_tss |
| `hr_drift_pct` | float or None | % change first→last lap HR |
| `power_drift_pct` | float or None | % change first→last lap power |
| `pace_drift_pct` | float or None | % change first→last lap pace (+ = slower) |
| `decoupling_pct` | float or None | HR:power ratio drift >5% = flag |
| `power_zone_distribution` | dict[str, float] | Zone → seconds; empty for non-bike |
| `quality_label` | str | executed_well \| adequate \| undercooked \| overdone |
| `flags` | list[str] | Auto-generated concern strings (decoupling, power drop, execution gap) |

Power zones (Coggan): Z1 <55% FTP, Z2 55–75%, Z3 75–87%, Z4 87–95%, Z5 95–105%, Z6 >105%.

Lap data comes from `get_activity_detail` → `laps` key. Pace field `avg_pace` in Intervals.icu is seconds/metre → `build_debrief` converts to s/km automatically.

### Gap classifications (`classify_gap` output)

| Classification | Meaning | Fix |
|---|---|---|
| `completed` | Within 12% of planned TSS | None |
| `skipped` | No activity logged | Adherence — protect calendar time |
| `duration_short` | < 80% planned duration | Scheduling / time pressure |
| `intensity_short_fatigued` | Full duration, TSS gap, RPE ≥ 7 | Load too ambitious — reduce targets or add recovery |
| `intensity_short_soft` | Full duration, TSS gap, RPE < 7 | Execution gap — add session cues; apply correction factor |
| `intensity_short_unknown` | Full duration, TSS gap, no RPE | Log RPE to enable root-cause analysis |

### `WeekDebt` fields

| Field | Type | Notes |
|---|---|---|
| `week_start` | str | YYYY-MM-DD Monday |
| `planned_tss` | float | Total planned TSS for the week |
| `actual_tss_to_date` | float | Accumulated so far |
| `debt_tss` | float | Planned-to-date minus actual (positive = missed) |
| `debt_pct` | float | debt_tss / planned_tss |
| `days_elapsed` | int | Days since Monday (0=Mon) |
| `days_remaining` | int | Days left including today |
| `days_missed` | int | Days where TSS was planned but zero logged |
| `redistributable` | bool | Whether debt can be safely spread into remaining days |
| `reason` | str | Empty when redistributable=True; explains constraint otherwise |

Redistribution is blocked when: it's the last day of the week, >3 days were missed, or >40% of planned weekly TSS was not completed.

### `modulate_session` — planned dict keys

| Key | Type | Notes |
|---|---|---|
| `session_type` | str | `bike_threshold`, `bike_z2`, `bike_vo2`, `bike_race_pace`, `run_quality`, `run_easy`, `run_long`, `brick`, `swim`, `strength` |
| `target_intensity` | float | IF as fraction of FTP (1.0 = FTP) |
| `interval_count` | int or None | None for non-interval sessions |
| `interval_duration_min` | float or None | |
| `recovery_min` | float or None | |
| `total_duration_min` | int | |

### `modulate_session` — readiness dict keys

| Key | Type | Notes |
|---|---|---|
| `atl` | float | From get_fitness |
| `ctl` | float | From get_fitness |
| `hrv_trend_pct` | float | (today − 7d avg) / 7d avg × 100 |
| `sleep_h_last_night` | float | From get_wellness |
| `last_session_rpe` | int or None | From session-log.json; None if no prior entry |
| `ankle_pain_score` | int | 0–10 |
| `ankle_quality_cleared` | bool | True once 4 consecutive pain-free quality sessions done |
| `temp_c` | float | Today's forecast ambient temp |
| `dew_point_c` | float | Today's forecast dew point |

### `SessionPrescription` fields

| Field | Type | Notes |
|---|---|---|
| `session_type` | str | May differ from planned if swapped |
| `go` | bool | False = don't train today |
| `swapped_to_z2` | bool | R2 or R6 fired and replaced quality session |
| `modified` | bool | Any parameter changed |
| `target_intensity` | float | Final prescribed intensity |
| `interval_count` | int or None | Final prescribed count |
| `interval_duration_min` | float or None | |
| `recovery_min` | float or None | |
| `total_duration_min` | int | Duration preserved on swap |
| `applied_rules` | list[str] | e.g. `["R3", "R7"]` |
| `reasoning_trails` | list[str] | One L2 trail string per fired rule |
| `summary` | str | Human-readable one-liner |

### Modulation rules (R1–R7)

| Rule | Signal | Action |
|---|---|---|
| R1 | ankle_pain_score ≥ 3, or ankle_quality_cleared False for run sessions | Hard stop — go=False, early return |
| R2 | ATL − CTL > 25 | Swap quality → Z2/easy, preserve duration |
| R3 | hrv_trend_pct < −7% | Drop intensity 0.05, intervals −1 |
| R4 | 15 ≤ ATL − CTL ≤ 25 | Cap intensity at 0.95 |
| R5 | last_session_rpe ≥ 8 | Drop intensity 0.05 |
| R6 | sleep < 6h + hrv < −5% | Swap to Z2 (two-signal); or sleep 6–7h alone → intervals −1 |
| R7 | heat/humidity correction via env_pacing | Reduce intensity by combined fraction |

Intensity floor across all rules: 0.65. R1 exits immediately; R2 exits after swap (no soft rules applied on top).

## CLI wrapper

```bash
python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/ironman-analysis/scripts/modulate.py '<json>'
```

JSON must have `"planned"` and `"readiness"` keys. Outputs full `SessionPrescription` as JSON.

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
