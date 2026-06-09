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
  outdoor session ≥60 min at ≥25°C ambient     = 1.0
  outdoor session 30–60 min at ≥25°C ambient   = 0.5
"""
import json
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/

HEAT_AMBIENT_C       = 25.0  # blueprint: outdoor heat sessions = peak ambient ≥ 25°C
HEAT_FULL_DOSE_MIN   = 60    # ≥60 min at temperature = full dose
HEAT_HALF_DOSE_MIN   = 30    # 30–60 min = half dose
MAINTENANCE_DOSE_14D = 2.0   # pre-`starts` floor that keeps the ambient-exposure pause honest
PROTOCOL_DOSE_14D    = 3.0   # race-proximal target once the formal block starts


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


def exposure_entry(act: dict) -> dict | None:
    """heat-log entry for an outdoor activity with device ambient ≥25°C, or None.

    Indoor sessions are excluded even if the room reads warm-ish (trainer flag,
    Virtual* types); swims carry no temperature and fall out on the None check.
    """
    temp = act.get("average_temp")
    mins = (act.get("moving_time") or 0) / 60
    if (temp is None or float(temp) < HEAT_AMBIENT_C or mins < HEAT_HALF_DOSE_MIN
            or act.get("trainer") or str(act.get("type") or "").startswith("Virtual")):
        return None
    return {
        "date": str(act.get("start_date_local") or "")[:10],
        "method": "outdoor session (auto)",
        "activity_id": str(act.get("id") or ""),
        "duration_min": round(mins),
        "temperature_c": round(float(temp), 1),
        "dose": 1.0 if mins >= HEAT_FULL_DOSE_MIN else 0.5,
        "context": f"{act.get('type', '')} — ambient {round(float(temp), 1)}°C from device",
        "logged_at": date.today().isoformat(),
    }
