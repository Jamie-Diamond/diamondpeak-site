"""Guard against cross-athlete injury contamination.

Injury state is athlete-scoped by construction: every consumer reads
``profile['injuries']`` and the structured injury block in ``current-state.json``
(e.g. ``ankle``) for ONE athlete at a time (see lib/injury.py, lib/session_library.py,
scripts/daily-prescription.py, scripts/*-checkin.py). There is no shared/global
injury state, so the structured path cannot leak between athletes.

The 2026-07-04 bug was a DATA contamination: Jamie's ankle / quality-run block was
written into Kathryn's current-state, so she was falsely R1-gated for weeks though
her profile lists no injury. This module makes that class of contamination
detectable and loud.

Invariant enforced
------------------
A structured injury block in an athlete's ``current-state.json`` (e.g. ``ankle``)
MUST be backed by a matching injury in that SAME athlete's ``profile.json``
``injuries[]`` (matched on ``location``). A tracked injury block with no backing
profile injury is the contamination signature.

``check_injury_scope`` reads STRUCTURED fields only, so it never false-positives on
the historical ankle prose that (correctly, flagged "disregard") still sits in
Kathryn's current-state.md dated log.
"""
from __future__ import annotations

import json
from pathlib import Path

# Structured injury blocks that may appear in current-state.json, each mapped to
# the substring expected in a backing profile injuries[].location. Extend as new
# structured body-part blocks are introduced.
INJURY_BLOCKS = {"ankle": "ankle"}


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def check_injury_scope(slug: str, base_dir) -> list[str]:
    """Return a list of scope violations for one athlete (empty list = clean)."""
    ath = Path(base_dir) / "athletes" / slug
    profile = _load_json(ath / "profile.json")
    cstate = _load_json(ath / "current-state.json")
    locations = " ".join(
        (inj.get("location") or "").lower()
        for inj in (profile.get("injuries") or [])
    )
    violations: list[str] = []
    for block, needle in INJURY_BLOCKS.items():
        if cstate.get(block) and needle not in locations:
            violations.append(
                f"{slug}: current-state.json carries a '{block}' injury block but "
                f"profile.json injuries[] has no matching '{needle}' entry — "
                f"possible cross-athlete injury contamination"
            )
    return violations


def assert_injury_scope(slug: str, base_dir) -> bool:
    """Raise AssertionError (loudly) on any scope violation. For callers that must
    fail closed rather than silently prescribe off contaminated state."""
    v = check_injury_scope(slug, base_dir)
    if v:
        raise AssertionError("; ".join(v))
    return True


def check_all(base_dir, slugs: list[str] | None = None) -> dict:
    """Check every athlete. Returns {slug: [violations]} for slugs with violations."""
    base = Path(base_dir)
    if slugs is None:
        cfg = _load_json(base / "config" / "athletes.json")
        slugs = list(cfg.keys())
    return {s: v for s in slugs if (v := check_injury_scope(s, base))}


if __name__ == "__main__":
    import sys
    base = sys.argv[1] if len(sys.argv) > 1 else "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach"
    bad = check_all(base)
    if bad:
        print("INJURY-SCOPE VIOLATIONS:")
        for s, vs in bad.items():
            for v in vs:
                print(f"  ✗ {v}")
        sys.exit(1)
    print("injury scope OK — every tracked injury block is backed by a profile injury")
