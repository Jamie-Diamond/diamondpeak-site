#!/usr/bin/env python3
"""
Multi-signal recovery score.

Usage:
  python3 recovery_score.py --athlete jamie
  python3 recovery_score.py --athlete jamie --wellness '[...]' --pain 2

Outputs JSON:
  {
    "score": 74,
    "label": "AMBER",
    "colour": "warn",
    "recommendation": "Proceed — monitor effort cap.",
    "signals": {
      "hrv":   {"value": 38, "baseline": 42, "ratio": 0.90, "score": 62, "weight": 0.35},
      "tsb":   {"value": -14.2, "score": 72, "weight": 0.30},
      "sleep": {"value": 7.1, "score": 82, "weight": 0.25},
      "pain":  {"value": 2, "score": 80, "weight": 0.10}
    },
    "available_signals": ["hrv", "tsb", "sleep", "pain"],
    "missing_signals": []
  }
"""
import argparse, json, sys
from pathlib import Path
from datetime import date, timedelta
from statistics import mean

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "lib"))


# ── Signal scorers ────────────────────────────────────────────────────────────

def _score_hrv(today_hrv, baseline_hrv):
    """HRV ratio vs 7-day baseline → 0-100."""
    if today_hrv is None or baseline_hrv is None or baseline_hrv == 0:
        return None
    ratio = today_hrv / baseline_hrv
    if ratio >= 1.05:   return 100
    if ratio >= 1.00:   return 90
    if ratio >= 0.95:   return 80
    if ratio >= 0.90:   return 70
    if ratio >= 0.85:   return 55
    if ratio >= 0.80:   return 35
    return 15


def _score_tsb(tsb):
    """TSB (form) → 0-100. Optimal window is −10 to +5."""
    if tsb is None:
        return None
    if tsb >= 10:    return 65   # too fresh → detraining risk
    if tsb >= 5:     return 80
    if tsb >= -5:    return 95
    if tsb >= -15:   return 85
    if tsb >= -25:   return 65
    if tsb >= -35:   return 40
    return 15


def _score_sleep(hrs):
    """Sleep hours → 0-100. Target ≥ 7h."""
    if hrs is None:
        return None
    if hrs >= 8.5:   return 100
    if hrs >= 8.0:   return 95
    if hrs >= 7.5:   return 88
    if hrs >= 7.0:   return 80
    if hrs >= 6.5:   return 65
    if hrs >= 6.0:   return 45
    return 25


def _score_pain(pain):
    """Injury pain score (0-10) → 0-100."""
    if pain is None:
        return None
    if pain == 0:    return 100
    if pain <= 1:    return 90
    if pain <= 2:    return 78
    if pain <= 3:    return 60
    if pain <= 4:    return 40
    return 15


# ── Composite score ───────────────────────────────────────────────────────────

_WEIGHTS = {"hrv": 0.35, "tsb": 0.30, "sleep": 0.25, "pain": 0.10}

_LABELS = [
    (80, "GREEN",  "good",   "All signals clear — train as planned."),
    (65, "AMBER",  "warn",   "Proceed — monitor effort; ease back if RPE spikes early."),
    (50, "ORANGE", "warn",   "Reduced capacity — consider dropping intensity or volume 20%."),
    (0,  "RED",    "bad",    "Recovery priority — easy movement only; protect adaptation."),
]


def compute(hrv_today=None, hrv_baseline=None, tsb=None, sleep_hrs=None, pain=0):
    """
    Returns the recovery dict. Any input may be None — missing signals are
    excluded and remaining weights are rescaled proportionally.
    """
    raw = {
        "hrv":   _score_hrv(hrv_today, hrv_baseline),
        "tsb":   _score_tsb(tsb),
        "sleep": _score_sleep(sleep_hrs),
        "pain":  _score_pain(pain if pain is not None else 0),
    }

    available   = {k: v for k, v in raw.items() if v is not None}
    missing     = [k for k, v in raw.items() if v is None]

    if not available:
        return {
            "score": 50, "label": "UNKNOWN", "colour": "muted",
            "recommendation": "Insufficient signal data — train to feel.",
            "signals": {}, "available_signals": [], "missing_signals": list(_WEIGHTS),
        }

    # Rescale weights for available signals only
    total_w = sum(_WEIGHTS[k] for k in available)
    score   = sum(available[k] * (_WEIGHTS[k] / total_w) for k in available)
    score   = round(score)

    label, colour, recommendation = "UNKNOWN", "muted", "Train to feel."
    for threshold, lbl, col, rec in _LABELS:
        if score >= threshold:
            label, colour, recommendation = lbl, col, rec
            break

    signals = {}
    for k, v in raw.items():
        entry = {"score": v, "weight": _WEIGHTS[k]}
        if k == "hrv":
            entry.update({"value": hrv_today, "baseline": hrv_baseline,
                          "ratio": round(hrv_today/hrv_baseline, 3) if hrv_baseline else None})
        elif k == "tsb":
            entry["value"] = tsb
        elif k == "sleep":
            entry["value"] = sleep_hrs
        elif k == "pain":
            entry["value"] = pain
        signals[k] = entry

    return {
        "score":             score,
        "label":             label,
        "colour":            colour,
        "recommendation":    recommendation,
        "signals":           signals,
        "available_signals": list(available),
        "missing_signals":   missing,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_wellness(rows):
    """Extract HRV baseline (7d), today's HRV, TSB, sleep from wellness list."""
    if not rows:
        return None, None, None, None

    # Sort by date ascending; last entry = most recent
    sorted_rows = sorted(rows, key=lambda r: r.get("date") or r.get("id") or "")
    today_row   = sorted_rows[-1]

    hrv_today    = today_row.get("hrv") or today_row.get("hrvSDNN")
    tsb_val      = today_row.get("form") or today_row.get("atl_form")
    sleep_val    = today_row.get("hrsSleep") or today_row.get("sleepSecs")
    if sleep_val and sleep_val > 24:      # stored as seconds
        sleep_val = sleep_val / 3600

    # 7-day HRV baseline (exclude today)
    hrv_vals = [
        r.get("hrv") or r.get("hrvSDNN")
        for r in sorted_rows[:-1]
        if (r.get("hrv") or r.get("hrvSDNN")) is not None
    ]
    hrv_baseline = round(mean(hrv_vals), 1) if hrv_vals else None

    return hrv_today, hrv_baseline, tsb_val, sleep_val


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--athlete",  default="jamie")
    p.add_argument("--wellness", default=None, help="Pre-fetched wellness JSON string")
    p.add_argument("--pain",     type=float, default=None, help="Injury pain score 0-10")
    args = p.parse_args()

    # Load athlete config
    cfg_path = ROOT / "config/athletes.json"
    athletes = json.loads(cfg_path.read_text())
    if args.athlete not in athletes:
        print(json.dumps({"error": f"athlete {args.athlete!r} not found"}))
        sys.exit(1)

    # Wellness data
    if args.wellness:
        wellness_rows = json.loads(args.wellness)
    else:
        from icu_api import IcuClient
        a = athletes[args.athlete]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        wellness_rows = client.get_wellness(8)

    hrv_today, hrv_baseline, tsb, sleep_hrs = _parse_wellness(wellness_rows)

    # Pain score
    pain = args.pain
    if pain is None:
        state_f = ROOT / f"athletes/{args.athlete}/current-state.json"
        if state_f.exists():
            try:
                state = json.loads(state_f.read_text())
                pain = state.get("ankle", {}).get("pain_during", 0) or 0
            except Exception:
                pain = 0
        else:
            pain = 0

    result = compute(hrv_today, hrv_baseline, tsb, sleep_hrs, pain)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
