#!/usr/bin/env python3
"""CLI wrapper for the W1 week re-optimiser.

Usage:
    python3 scripts/reoptimise.py '<json>'

JSON keys:
    planned_sessions   list[dict]  — from get_events for the week (date, planned_tss)
    actual_sessions    list[dict]  — from get_training_history for the week (date, tss)
    today              str         — YYYY-MM-DD (use get_athlete_profile current_date_local)
    current_ctl        float       — from get_fitness most recent row
    ankle_in_rehab     bool        — from current-state.md
    compliance_records list[dict]  — optional; each has planned_tss, actual_tss,
                                     planned_duration_min, actual_duration_min, rpe,
                                     gap_classification, gap_pct
                                     (pass last 28 days of compliance records for
                                     correction factor calculation)

Output: JSON with debt assessment, ramp headroom, compliance summary,
        and (if applicable) correction-adjusted remaining sessions.

Example:
    python3 scripts/reoptimise.py '{
      "planned_sessions": [
        {"date": "2026-05-04", "planned_tss": 100},
        {"date": "2026-05-05", "planned_tss": 80},
        {"date": "2026-05-07", "planned_tss": 120}
      ],
      "actual_sessions": [
        {"date": "2026-05-04", "tss": 95}
      ],
      "today": "2026-05-06",
      "current_ctl": 105.0,
      "ankle_in_rehab": true
    }'
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from primitives.compliance import (
    ComplianceRecord,
    compliance_recommendations,
    forward_correction_factor,
    rolling_compliance,
)
from primitives.reoptimise import (
    WeekDebt,
    assess_week_debt,
    ramp_headroom,
)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: reoptimise.py '<json>'", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    planned = data.get("planned_sessions", [])
    actual = data.get("actual_sessions", [])
    today = data.get("today")
    current_ctl = float(data.get("current_ctl", 0.0))
    ankle_in_rehab = bool(data.get("ankle_in_rehab", True))
    raw_compliance = data.get("compliance_records", [])

    if not today:
        print("'today' is required (YYYY-MM-DD).", file=sys.stderr)
        sys.exit(1)

    debt: WeekDebt = assess_week_debt(planned, actual, today)

    weekly_tss = debt.planned_tss
    headroom = ramp_headroom(current_ctl, weekly_tss, ankle_in_rehab)

    # Compliance summary from supplied historical records
    compliance_records: list[ComplianceRecord] = []
    for r in raw_compliance:
        try:
            compliance_records.append(ComplianceRecord(**r))
        except TypeError:
            pass  # skip malformed entries

    compliance_metrics = rolling_compliance(compliance_records)
    recommendations = compliance_recommendations(compliance_metrics)

    # Correction factor — only meaningful for intensity_short_soft dominant gap
    dominant = compliance_metrics.get("dominant_gap_type")
    correction_factor = (
        forward_correction_factor(compliance_metrics["compliance_rate"])
        if dominant == "intensity_short_soft"
        else 1.0
    )

    output = {
        "debt": asdict(debt),
        "ramp_headroom_tss": headroom,
        "compliance": compliance_metrics,
        "compliance_recommendations": recommendations,
        "correction_factor": correction_factor,
        "correction_factor_applies": dominant == "intensity_short_soft" and correction_factor != 1.0,
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
