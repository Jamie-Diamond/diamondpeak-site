#!/usr/bin/env python3
"""plan_distribution.py — two-directional intensity-distribution gate (chat path).

Pure functions, no IO. Given a training week expressed as zoned session segments
and the blueprint phase's per-sport intensity distribution
("78% Z1–2 / 12% Z3 / 10% Z4–5"), this checks the planned week against that
distribution in BOTH directions:

  EXCESS      — too much quality (easy share far below the Z1–2 target). This is
                what the generator-side validator (primitives.validate_plan
                ._check_distribution) already catches.
  INSUFFICIENT — a required quality slice is missing or far under-dosed (e.g. the
                phase calls for 10% Z4–5 but the stated plan has none). This is the
                gap that let the 22 Jul improvised week zero out Kathryn's Z4–5 run
                slice while asserting it was spec-compliant (deferred action-plan
                line 71). It is NEW arithmetic, deliberately kept out of the shared
                gated generator modules so concurrent promotion stays byte-safe.

WHY THIS MODULE STILL EXISTS (2026-07-28 consolidation):
  the original reason - "validate_plan._check_distribution is session-level and
  one-directional" - is GONE: that check is now segment-minute level too. What is
  left that is genuinely only here is the INSUFFICIENT direction (a required quality
  slice missing/under-dosed), which the audit path deliberately does not assert
  because it cannot tell a deload week from a normal one. So this is no longer a
  second classifier: the zone-name -> bucket taxonomy is IMPORTED from
  primitives.validate_plan (DIST_ZONE_BUCKET / bucket_for_zone_name) and the
  tolerance matches SEGMENT_TOLERANCE_PP. One taxonomy, one set of tolerances, two
  surfaces - a generation/audit surface reading calendar step bands, and this chat
  surface reading engine-proposal zone names.

  KNOWN GAP, NOT FIXED HERE: this module's only non-test callers are PROMPT STRINGS
  telling the model to shell out to its CLI (lib/engine.py:234, telegram/bot.py:3007).
  A gate whose invocation depends on a model choosing to run a command is not a gate.
  Both files are out of scope for this commit; the follow-up is to call
  audit_distribution() directly from the engine's plan path.

SCOPE — Run and Bike only, matching the generator-side validator: zone detection is
unreliable for swims (a "CSS" swim is a threshold set but the whole session
integrates low) and bricks are mixed by definition. Swim/Brick sessions are ignored,
not mis-bucketed.

ONE TAXONOMY NOTE: run "steady" now buckets to Z3, not easy. It used to be easy here
only — planned_tss gives run steady and run z3 the SAME band (80-86% pace) and the
same IF, so calling it easy here while the planner rendered it as Z3 was the drift
this consolidation removes.

INPUT FORM — the deterministic engine's proposal shape:
    [{"sport": "Run", "segments": [{"minutes": 40, "zone": "z2"}, ...]}, ...]
Any session whose sport is Run/Bike (Ride) is bucketed by the ZONE NAME of each
segment (see _ZONE_BUCKET). Zone names are the engine's own vocabulary; IF-based
bucketing is deliberately NOT used because a single IF band cannot separate a bike
Z3 (~0.80) from an easy run (~0.83).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Zone-name -> distribution bucket, per sport: IMPORTED, never redefined. This module
# used to carry its own near-identical copy, which is how the estate ended up with
# three intensity classifiers giving three answers. The canonical table lives with the
# gated validator so the audit path and this path cannot drift apart.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "ironman-analysis"))
from primitives.validate_plan import (           # noqa: E402
    DIST_ZONE_BUCKET as _ZONE_BUCKET,
    SEGMENT_TOLERANCE_PP,
    bucket_for_zone_name,
    norm_zone_name as _norm_zone,
)

# intervals.icu / proposal sport → the bucket key above.
_SPORT_KEY = {
    "run": "run", "trailrun": "run", "virtualrun": "run",
    "bike": "bike", "ride": "bike", "virtualride": "bike", "gravelride": "bike",
}

DEFAULT_TOL_PP = SEGMENT_TOLERANCE_PP   # shared with the audit path, not a local number
MIN_MINUTES = 90            # don't judge a sport on under 1.5h of planned work


def parse_distribution(row) -> dict | None:
    """Parse '78% Z1–2 / 12% Z3 / 10% Z4–5' -> {'easy':78,'z3':12,'z45':10}.

    Returns None when the row has no parseable Z1–2 leading figure (e.g. a blank
    taper row). Missing Z3/Z4–5 figures default to 0.
    """
    s = str(row or "")
    easy = re.search(r"(\d+(?:\.\d+)?)\s*%\s*z\s*1", s, re.I)
    if not easy:
        return None
    z3 = re.search(r"(\d+(?:\.\d+)?)\s*%\s*z\s*3", s, re.I)
    z45 = re.search(r"(\d+(?:\.\d+)?)\s*%\s*z\s*4", s, re.I)
    return {
        "easy": float(easy.group(1)),
        "z3": float(z3.group(1)) if z3 else 0.0,
        "z45": float(z45.group(1)) if z45 else 0.0,
    }


@dataclass
class SportFinding:
    sport: str                 # "Run" / "Bike"
    ok: bool
    target: dict               # {easy,z3,z45} pct
    actual: dict               # {easy,z3,z45} pct
    minutes: float
    violations: list           # list[str] — human-readable, both directions
    unknown_min: float = 0.0   # minutes whose zone could not be bucketed


def _bucket_minutes(sessions: list[dict]) -> dict[str, dict]:
    """sport-key -> {easy,z3,z45,unknown,total} minutes across Run/Bike sessions."""
    out: dict[str, dict] = {}
    for s in sessions or []:
        sport = _SPORT_KEY.get(str(s.get("sport") or "").strip().lower())
        if not sport:
            continue
        b = out.setdefault(sport, {"easy": 0.0, "z3": 0.0, "z45": 0.0,
                                   "unknown": 0.0, "total": 0.0})
        for seg in s.get("segments") or []:
            mins = float(seg.get("minutes") or 0)
            if mins <= 0:
                continue
            bucket = bucket_for_zone_name(sport, seg.get("zone"))
            b["total"] += mins
            b[bucket if bucket else "unknown"] += mins
    return out


def audit_distribution(distribution: dict, sessions: list[dict],
                       tol_pp: float = DEFAULT_TOL_PP,
                       min_minutes: float = MIN_MINUTES) -> list[SportFinding]:
    """Check a zoned week against the phase distribution BOTH directions.

    distribution: {"Run": "78% Z1–2 / 12% Z3 / 10% Z4–5", "Bike": "...", ...}
    sessions:     engine-proposal shape (see module docstring).

    Returns one SportFinding per Run/Bike sport that has a parseable target and
    at least `min_minutes` of planned work. A finding is `ok=False` when either:
      - EXCESS: easy share is more than tol_pp BELOW the Z1–2 target
                (too much quality), or
      - INSUFFICIENT: the Z3 or Z4–5 share is more than tol_pp BELOW its target
                (a required quality slice missing/under-dosed).
    A slice being ABOVE target is not itself flagged here (that is caught by the
    complementary excess check on the easy bucket) — the two together bound the
    week on both sides.
    """
    display = {"run": "Run", "bike": "Bike"}
    buckets = _bucket_minutes(sessions)
    findings: list[SportFinding] = []
    for sport_key, b in buckets.items():
        target = parse_distribution((distribution or {}).get(display[sport_key])
                                    or (distribution or {}).get(sport_key))
        total = b["total"]
        if target is None or total < min_minutes:
            continue
        actual = {k: (b[k] / total * 100.0) for k in ("easy", "z3", "z45")}
        viols: list[str] = []
        # EXCESS quality — easy share too far below target.
        if actual["easy"] < target["easy"] - tol_pp:
            viols.append(
                f"{display[sport_key]}: {actual['easy']:.0f}% Z1–2 vs target "
                f"{target['easy']:.0f}% (−{tol_pp:.0f}pp tol) — too much quality "
                f"planned (excess intensity)")
        # INSUFFICIENT quality — a required Z3 / Z4–5 slice missing or under-dosed.
        for key, label in (("z3", "Z3"), ("z45", "Z4–5")):
            if target[key] > 0 and actual[key] < target[key] - tol_pp:
                viols.append(
                    f"{display[sport_key]}: {actual[key]:.0f}% {label} vs target "
                    f"{target[key]:.0f}% (−{tol_pp:.0f}pp tol) — "
                    f"{'no' if actual[key] == 0 else 'insufficient'} {label} work "
                    f"planned (the phase requires this slice)")
        findings.append(SportFinding(
            sport=display[sport_key], ok=not viols, target=target, actual=actual,
            minutes=round(total, 1), violations=viols, unknown_min=round(b["unknown"], 1)))
    return findings


def summarise(findings: list[SportFinding]) -> str:
    """One-line-per-sport human summary for the chat context block."""
    if not findings:
        return "distribution: no Run/Bike volume to assess"
    out = []
    for f in findings:
        verdict = "OK" if f.ok else "OFF-SPEC"
        out.append(
            f"{f.sport} [{verdict}] planned {f.actual['easy']:.0f}/{f.actual['z3']:.0f}/"
            f"{f.actual['z45']:.0f} vs spec {f.target['easy']:.0f}/{f.target['z3']:.0f}/"
            f"{f.target['z45']:.0f} (Z1–2/Z3/Z4–5)"
            + ("" if f.ok else " — " + "; ".join(f.violations)))
    return "\n".join(out)


def any_offspec(findings: list[SportFinding]) -> bool:
    return any(not f.ok for f in findings)


# -- Blueprint lookup + CLI ----------------------------------------------------
# This module's input is the deterministic engine's PROPOSAL form (named zoned
# segments) — the same shape stage1-plan.py builds and the bot uses when it
# generates/replans/describes a week in zone terms. To check a proposed week, express
# it as zoned segments and pass --sessions.
#
# CORRECTED 2026-07-28: this comment used to claim that reverse-mapping intervals.icu
# %FTP/%pace step bands was unsafe "because the run bands overlap (easy 78–88% vs z3
# 80–86%)". The overlap is real but the conclusion was wrong: the RANGES overlap and
# the midpoints are identical (both 83), yet the TUPLES are distinct, and the planner
# renders every step from one table (planned_tss._ZONE_BAND). Inverting that table on
# the exact tuple is therefore exact, not a heuristic — which is what
# validate_plan.bucket_for_step now does, so a LIVE calendar week can be measured by
# segment minutes without being re-expressed by hand.

def load_phase_distribution(slug: str, week_start):
    """Blueprint per-sport distribution for the phase containing week_start.

    Imports are local so the arithmetic above stays dependency-free/testable.
    Returns {} on any failure (caller treats as 'no spec to check against').
    """
    import json
    import sys
    from datetime import date
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(base / "ironman-analysis"))
    from primitives.blueprint import current_phase  # noqa: E402

    if isinstance(week_start, str):
        week_start = date.fromisoformat(week_start)
    bp_path = base / "athletes" / slug / "reference" / "training-blueprint.json"
    if not bp_path.exists():
        return {}
    try:
        bp = json.loads(bp_path.read_text())
    except Exception:
        return {}
    phase = current_phase(bp, week_start) or {}
    return {"phase": phase.get("name"), "distribution": phase.get("distribution") or {}}


def _main():
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        description="Two-directional intensity-distribution check for a stated/"
                    "proposed training week (excess quality AND missing quality).")
    ap.add_argument("--athlete", help="load the phase distribution from this athlete's blueprint")
    ap.add_argument("--week-start", help="Monday YYYY-MM-DD (with --athlete) to pick the phase")
    ap.add_argument("--distribution", help="JSON per-sport distribution (overrides --athlete)")
    ap.add_argument("--sessions", required=True,
                    help='JSON list of zoned sessions: [{"sport":"Run",'
                         '"segments":[{"minutes":40,"zone":"z2"}]}]')
    ap.add_argument("--tol-pp", type=float, default=DEFAULT_TOL_PP)
    args = ap.parse_args()

    if args.distribution:
        dist = json.loads(args.distribution)
        phase = None
    elif args.athlete and args.week_start:
        info = load_phase_distribution(args.athlete, args.week_start)
        dist, phase = info.get("distribution", {}), info.get("phase")
    else:
        ap.error("provide --distribution, or --athlete with --week-start")

    sessions = json.loads(args.sessions)
    findings = audit_distribution(dist, sessions, tol_pp=args.tol_pp)
    out = {
        "phase": phase,
        "on_spec": not any_offspec(findings),
        "summary": summarise(findings),
        "findings": [
            {"sport": f.sport, "ok": f.ok, "target": f.target, "actual": f.actual,
             "minutes": f.minutes, "unknown_min": f.unknown_min,
             "violations": f.violations}
            for f in findings
        ],
    }
    print(json.dumps(out, indent=1, ensure_ascii=False))
    sys.exit(0 if out["on_spec"] else 1)


if __name__ == "__main__":
    _main()
