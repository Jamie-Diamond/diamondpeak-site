# IcuSync MCP — observed response shapes

Captured 2026-04-25 against athlete `i196362` (Jamie Diamond, Europe/London tz).
Update this file whenever schemas drift.

## get_athlete_profile

Top-level keys:

```
id, name, email, timezone, current_date_local,
weight, height, sex, date_of_birth, measurement_preference,
resting_hr, run, ride, warnings
```

Critical for anchoring:
- `current_date_local` — string `YYYY-MM-DD` in athlete tz. Use this, never system date.
- `timezone` — IANA, e.g. `Europe/London`.

Sport blocks (`run`, `ride`):
- `ftp` (run.ftp may be `null`), `lthr`, `max_hr`, `threshold_pace`, `power_zones`, `hr_zones`.

## get_fitness

Returns `list[dict]`, one row per day (inclusive of `oldest` and `newest`).

```json
{
  "date": "2026-04-25",     // string YYYY-MM-DD, athlete-local
  "ctl": 73.4,              // float
  "atl": 89.8,              // float
  "tsb": -16.4,             // float, absolute (NOT percentage)
  "load": null              // always null, do not use
}
```

Future dates **return zero-training projections**, not plans. Always label these.

## get_training_history

Returns:

```json
{
  "fitness_metrics": {},
  "activities": [ ... ],
  "summary": { ... }
}
```

### activity dict (relevant fields for load.py)

```json
{
  "id": "i142777471",                       // string, primary dedup key
  "date": "2026-04-25T07:27:16",            // ISO datetime, athlete-local (no tz suffix)
  "name": "T-147",
  "type": "Ride",                           // also: GravelRide, VirtualRide, Run, Swim, OpenWaterSwim, WeightTraining, Hike, Kayaking, Elliptical
  "device_name": "Garmin Edge 830",
  "garmin_attribution": "Data provided by Garmin®",
  "duration_minutes": 237,
  "distance_km": "129.94",                  // STRING not float — coerce
  "average_pace": "1:49/km",
  "average_cadence": 81,
  "average_power": 202,
  "normalized_power": 227,
  "average_hr": 136,
  "tss": 204,
  "calories": 3271,
  "description": "..."
}
```

### Known data-quality issues

- **Duplicates with distinct ids**: e.g. 14 Apr 2026 — two `VirtualRide` rows, ids `i139909103` and `i139908846`, both `2026-04-14T23:36:47`, same NP/duration/TSS, names "Cycling" and "Night Virtual Ride". Garmin-side artifact. Dedup must use a secondary composite key, not just `id`.
- **Volume**: 90 days of activities can exceed the MCP token cap. Fetch in <=30-day windows or sport-filtered.
- **Strava-only flag**: `strava_only: true` on imported activities. Not currently relevant — athlete syncs Garmin direct.

## get_wellness

Returns `list[dict]`, one per day:

```json
{
  "date": "2026-04-25",
  "hrv": 32,                  // rMSSD; lnRMSSD = ln(hrv) for analysis
  "rhr": 56,
  "sleep_secs": 23040,
  "sleep_quality": null,
  "fatigue": null,            // 1-5, currently null for this athlete
  "soreness": null,
  "mood": null,
  "motivation": null,
  "stress": null,
  "readiness": null,
  "weight": 82.05,
  "vo2max": null,
  "steps": null
}
```

Subjective fields **ignored** by skill design — athlete does not log them.

## get_activity_detail

Triggers Garmin attribution requirement. Includes per-interval streams, decoupling, EF, weather. Used in Phase 4 (`session.py`).

## Garmin attribution rule

Any output that displays activity-detail content **must** end with:

> Data provided by Garmin®

Rendered as muted/secondary text, on its own line.
