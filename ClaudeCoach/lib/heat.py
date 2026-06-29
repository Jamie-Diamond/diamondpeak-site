"""Heat-protocol state and dose rules, shared by the cron scripts.

The protocol has three layers:
  - profile.json `heat_protocol: false` — athlete-level kill switch (off entirely)
  - blueprint sidecar env_protocols.heat {active, starts} — active when the race
    is hot; `starts` (race − 4 weeks) is when the formal sauna block begins
  - before `starts` the protocol is PAUSED on ambient exposure, not off: outdoor
    sessions in heat are auto-credited as dose, and the watchdog checks the
    14-day dose against a maintenance floor so the pause stays honest

Dose model (standard heat-acclimation guidance — adaptations decay over days,
roughly one exposure per 4–5 days maintains; confirm values with the coach):
  sauna / hot bath entry                       = 1.0 (entries without a dose field)
  outdoor session ≥60 min at ≥25°C ambient     = 1.0 base
  outdoor session 30–60 min at ≥25°C ambient   = 0.5 base

The base dose (duration gate) is then weighted by two bounded multipliers so a
brutal hot/hard day scores higher than a mild easy one — heat adaptation tracks
thermal strain, which rises with both environmental heat and metabolic heat
production:
  temperature multiplier  — relative to a 30°C reference (see DOSE_TEMP_*)
  intensity   multiplier  — relative to a moderate-aerobic load rate ~60 TSS/hr
                            ≈ IF 0.77 (see DOSE_INT_*)
Both are centred so a typical maintenance session (~30°C, moderate) ≈ 1.0, which
preserves the score calibration; they only differentiate hotter/harder days.
"""
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/

HEAT_AMBIENT_C       = 25.0  # blueprint: outdoor heat sessions = peak ambient ≥ 25°C
HEAT_FULL_DOSE_MIN   = 60    # ≥60 min at temperature = full dose
HEAT_HALF_DOSE_MIN   = 30    # 30–60 min = half dose
MAINTENANCE_DOSE_14D = 2.0   # pre-`starts` floor that keeps the ambient-exposure pause honest
PROTOCOL_DOSE_14D    = 3.0   # race-proximal target once the formal block starts
SENSOR_SUSPECT_C     = 22.0  # Garmin wrist sensors read 3–8°C high; look up external weather at or above this
LONG_SESSION_MIN     = 90    # for long sessions report both peak and mean ambient

ACCL_TAU_DAYS = 21.0   # exponential decay time constant (3-week half-life)
ACCL_SCALE    = 10.5   # maps raw score → %; 3×/week full-dose steady-state ≈ 100%

# Dose weighting — temperature multiplier (relative to 30°C reference)
DOSE_TEMP_REF_C   = 30.0    # ambient at which temp_mult == 1.0
DOSE_TEMP_SLOPE   = 0.05    # per °C above/below the reference
DOSE_TEMP_MIN     = 0.7     # floor (a 25°C session still counts, just less)
DOSE_TEMP_MAX     = 1.6     # ceiling (≥42°C is brutal but capped)

# Dose weighting — intensity multiplier (relative to ~60 TSS/hr ≈ IF 0.77)
DOSE_INT_REF_TSS_HR = 60.0  # load rate at which int_mult == 1.0
DOSE_INT_SLOPE      = 0.004 # per TSS/hr above/below the reference
DOSE_INT_MIN        = 0.8   # floor (very easy hot session)
DOSE_INT_MAX        = 1.3   # ceiling (threshold/VO2 in the heat)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def dose_multipliers(temp_c: float, tss: float | None, mins: float | None) -> tuple[float, float]:
    """(temp_mult, int_mult) for a heat dose.

    temp_mult scales the environmental stimulus relative to DOSE_TEMP_REF_C;
    int_mult scales the metabolic-heat stimulus from the session's load rate
    (TSS/hr). Both are bounded so a single session can neither dominate nor
    zero out the dose. int_mult falls back to 1.0 when load or duration is
    missing.
    """
    temp_mult = _clamp(1 + (float(temp_c) - DOSE_TEMP_REF_C) * DOSE_TEMP_SLOPE,
                       DOSE_TEMP_MIN, DOSE_TEMP_MAX)
    if tss and mins and mins > 0:
        tss_hr = float(tss) / (float(mins) / 60.0)
        int_mult = _clamp(1 + (tss_hr - DOSE_INT_REF_TSS_HR) * DOSE_INT_SLOPE,
                          DOSE_INT_MIN, DOSE_INT_MAX)
    else:
        int_mult = 1.0
    return round(temp_mult, 3), round(int_mult, 3)


def state(slug: str, profile: dict | None = None) -> dict:
    """{active, starts, in_protocol_window, maintenance} for an athlete.

    `active` False means no heat prep for this race (or athlete kill switch);
    `in_protocol_window` True means the formal race−4wk block has begun;
    `maintenance` True means the athlete opted in (profile `heat_maintenance`)
    to having their ambient-exposure dose policed BEFORE the window — for an
    athlete who deliberately paused formal heat work on "I'm in hot venues
    enough". Without it, nothing nags before `starts`.
    """
    profile = profile or {}
    if profile.get("heat_protocol") is False:
        return {"active": False, "starts": None,
                "in_protocol_window": False, "maintenance": False}
    f = BASE / "athletes" / slug / "reference/training-blueprint.json"
    try:
        h = (json.loads(f.read_text()).get("env_protocols") or {}).get("heat") or {}
    except Exception:
        h = {}
    active = bool(h.get("active"))
    starts = h.get("starts")
    in_window = False
    if active:
        try:
            in_window = starts is None or date.fromisoformat(starts) <= date.today()
        except (ValueError, TypeError):
            in_window = True
    return {"active": active, "starts": starts, "in_protocol_window": in_window,
            "maintenance": active and bool(profile.get("heat_maintenance"))}


def fetch_ambient_weather(lat: float, lon: float, start_utc: str, end_utc: str) -> list[float]:
    """Hourly ambient temperatures (°C) from Open-Meteo historical archive.

    start_utc / end_utc: ISO datetime strings "YYYY-MM-DDTHH:MM:SS" in UTC.
    """
    import urllib.request
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={start_utc[:10]}&end_date={end_utc[:10]}"
        "&hourly=temperature_2m&timezone=UTC"
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    times = data["hourly"]["time"]           # "YYYY-MM-DDTHH:MM"
    temps = data["hourly"]["temperature_2m"]
    start_h, end_h = start_utc[:13], end_utc[:13]  # "YYYY-MM-DDTHH"
    return [t for ts, t in zip(times, temps) if start_h <= ts[:13] <= end_h and t is not None]


def exposure_entry(act: dict) -> dict | None:
    """heat-log entry for an outdoor activity with ambient ≥25°C, or None.

    Indoor sessions are excluded even if the room reads warm-ish (trainer flag,
    Virtual* types); swims carry no temperature and fall out on the None check.

    Device temp is used as the initial filter but Garmin wrist sensors read 3–8°C
    high in sunlight, so any reading ≥ SENSOR_SUSPECT_C triggers an Open-Meteo
    lookup using the activity's GPS coordinates.  The external peak temp then
    drives both dose eligibility and the log context.  Falls back to device temp
    if the network call fails, flagged in context.
    """
    temp = act.get("average_temp")
    mins = (act.get("moving_time") or 0) / 60
    if (temp is None or float(temp) < SENSOR_SUSPECT_C or mins < HEAT_HALF_DOSE_MIN
            or act.get("trainer") or str(act.get("type") or "").startswith("Virtual")):
        return None

    temp        = float(temp)
    temp_source = "device"
    temp_peak   = None
    temp_mean   = None

    latlng    = act.get("start_lat_lng")
    start_raw = (act.get("start_date") or act.get("start_date_local") or "").replace("Z", "")
    if latlng and len(latlng) == 2 and start_raw:
        try:
            lat, lon   = float(latlng[0]), float(latlng[1])
            start_dt   = datetime.fromisoformat(start_raw)
            end_dt     = start_dt + timedelta(seconds=int(act.get("moving_time") or 0))
            ambient    = fetch_ambient_weather(
                lat, lon,
                start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            if ambient:
                temp_peak   = max(ambient)
                temp_mean   = round(sum(ambient) / len(ambient), 1)
                temp        = temp_peak
                temp_source = "external"
        except Exception:
            temp_source = "device (external fetch failed)"

    if temp < HEAT_AMBIENT_C:
        return None

    base_dose = 1.0 if mins >= HEAT_FULL_DOSE_MIN else 0.5
    tss = act.get("icu_training_load")
    temp_mult, int_mult = dose_multipliers(temp, tss, mins)
    dose = round(base_dose * temp_mult * int_mult, 2)

    ctx = f"{act.get('type', '')} — ambient {round(temp, 1)}°C ({temp_source})"
    if mins >= LONG_SESSION_MIN and temp_peak is not None and temp_mean is not None:
        ctx += f"; peak {round(temp_peak, 1)}°C / mean {temp_mean}°C"
    ctx += f"; dose {base_dose}×T{temp_mult}×I{int_mult}={dose}"

    return {
        "date": str(act.get("start_date_local") or "")[:10],
        "method": "outdoor session (auto)",
        "activity_id": str(act.get("id") or ""),
        "duration_min": round(mins),
        "temperature_c": round(temp, 1),
        "base_dose": base_dose,
        "temp_mult": temp_mult,
        "int_mult": int_mult,
        "dose": dose,
        "context": ctx,
        "logged_at": date.today().isoformat(),
    }


def acclimation_score(slug: str, ref_date: date | None = None) -> float:
    """0–100 heat acclimation percentage with a 21-day exponential decay.

    Each heat-log entry contributes its dose × exp(−days_since/21) to a raw
    total; ACCL_SCALE maps the raw total so sustained 3×/week full-dose
    exposure saturates at ~100%.  Mirrors the Garmin heat-acclimation model.
    """
    if ref_date is None:
        ref_date = date.today()
    log_file = BASE / "athletes" / slug / "heat-log.json"
    try:
        entries = json.loads(log_file.read_text())
    except Exception:
        return 0.0
    raw = 0.0
    for entry in entries:
        try:
            d = date.fromisoformat(str(entry.get("date") or "")[:10])
        except (ValueError, TypeError):
            continue
        days_since = (ref_date - d).days
        if days_since < 0:
            continue
        raw += float(entry.get("dose") or 1.0) * math.exp(-days_since / ACCL_TAU_DAYS)
    return min(100.0, raw * ACCL_SCALE)
