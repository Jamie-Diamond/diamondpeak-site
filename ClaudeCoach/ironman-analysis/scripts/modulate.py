#!/usr/bin/env python3
"""CLI wrapper for the W2 session modulation engine.

Usage:
    python3 scripts/modulate.py '<json>'

    JSON must contain "planned" and "readiness" keys matching the
    modulate_session contract in primitives/modulation.py.

Output: JSON prescription to stdout.

Example:
    python3 scripts/modulate.py '{
      "planned": {
        "session_type": "bike_threshold",
        "target_intensity": 1.0,
        "interval_count": 4,
        "interval_duration_min": 10,
        "recovery_min": 3,
        "total_duration_min": 75
      },
      "readiness": {
        "atl": 138, "ctl": 120,
        "hrv_trend_pct": -8.5,
        "sleep_h_last_night": 7.2,
        "last_session_rpe": 7,
        "ankle_pain_score": 0,
        "ankle_quality_cleared": true,
        "temp_c": 22.0,
        "dew_point_c": 14.0
      }
    }'
"""

import json
import sys
from pathlib import Path

# Ensure the package root is importable when run from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from primitives.modulation import modulate_session, SessionPrescription


def _prescription_to_dict(p: SessionPrescription) -> dict:
    return {
        "session_type": p.session_type,
        "go": p.go,
        "swapped_to_z2": p.swapped_to_z2,
        "modified": p.modified,
        "target_intensity": p.target_intensity,
        "interval_count": p.interval_count,
        "interval_duration_min": p.interval_duration_min,
        "recovery_min": p.recovery_min,
        "total_duration_min": p.total_duration_min,
        "applied_rules": p.applied_rules,
        "reasoning_trails": p.reasoning_trails,
        "summary": p.summary,
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: modulate.py '<json>'", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    planned = data.get("planned")
    readiness = data.get("readiness")

    if not planned or not readiness:
        print("JSON must contain 'planned' and 'readiness' keys.", file=sys.stderr)
        sys.exit(1)

    prescription = modulate_session(planned, readiness)
    print(json.dumps(_prescription_to_dict(prescription), indent=2))


if __name__ == "__main__":
    main()
