#!/usr/bin/env python3
"""One-off reconciliation: write Kathryn's goal race-run pace = 5:45/km to every
stored location via the atomic set_run_pace_target helper, then tag the 5:00/km
run threshold as an unconfirmed/derived working estimate.

Run with --dry-run first. This is the *use* of the structural fix built in
lib/athlete_targets.py; future corrections should call that helper, not hand-edit.
"""
import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path("/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach")
sys.path.insert(0, str(BASE / "lib"))
from athlete_targets import set_run_pace_target, TargetWriteError  # noqa: E402

SLUG = "kathryn"

# Context-anchored, dash-free where possible. Each old phrase is unique to its
# file. 6:12/km is deliberately NOT in verify_absent: it legitimately survives in
# the correction-documentation lines (current-state L16, persistent-rules L27).
PACE_SUBS = {
    "run ~2:11 @ 6:12/km": "run ~2:01 @ 5:45/km",                              # system_prompt.txt
    "~2:11 | ~6:12/km": "~2:01 | ~5:45/km",                                   # rules.md target table
    "24% slower than threshold": "15% slower than threshold",                  # rules.md (5:45 vs 5:00)
    "6:10–6:15/km off a hard bike effort": "~5:45/km off a hard bike effort",  # rules.md (en-dash)
    "Run ~2:10 (~6:09/km)": "Run ~2:01 (~5:45/km)",                            # current-state.md
}
VERIFY_ABSENT = ["6:09/km", "6:10–6:15/km", "~2:11"]


def tag_threshold(dry_run: bool) -> dict:
    """Tag the 5:00/km run threshold as unconfirmed/derived (profile.json is
    authoritative; current-state.md mirrors the wording). Atomic + fail-loud."""
    profile_p = BASE / "athletes" / SLUG / "profile.json"
    cstate_p = BASE / "athletes" / SLUG / "current-state.md"

    prof = json.loads(profile_p.read_text(encoding="utf-8"))
    old_thr = prof.get("run_threshold_pace_per_km")
    new_thr = ("5:00/km — UNCONFIRMED working estimate (given by Kathryn 2026-07-22), "
               "NOT field-tested. Treat as derived/provisional: do NOT prescribe threshold-"
               "interval work as if this is a validated threshold. Pending a field test.")
    prof["run_threshold_pace_per_km"] = new_thr

    cstate = cstate_p.read_text(encoding="utf-8")
    old_phrase = "run threshold pace confirmed as working value 5:00/km pending a field test"
    new_phrase = ("run threshold pace is an UNCONFIRMED working estimate (5:00/km, not "
                  "field-tested) pending a field test")
    if cstate.count(old_phrase) != 1:
        raise TargetWriteError(f"threshold phrase count={cstate.count(old_phrase)} in {cstate_p}")
    cstate_new = cstate.replace(old_phrase, new_phrase)

    summary = {str(profile_p): f"run_threshold_pace_per_km {old_thr!r} -> tagged unconfirmed/derived",
               str(cstate_p): "current-state threshold wording softened to unconfirmed"}
    if dry_run:
        return {"dry_run": True, "changes": summary}

    for p, text in ((profile_p, json.dumps(prof, indent=2, ensure_ascii=False) + "\n"),
                    (cstate_p, cstate_new)):
        tmp = p.with_suffix(p.suffix + ".tmp-thr")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    return {"changes": summary}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=== 1. goal run-pace -> 5:45/km (via set_run_pace_target) ===")
    res = set_run_pace_target(SLUG, "5:45/km", prose_pace_subs=PACE_SUBS,
                              verify_absent=VERIFY_ABSENT, dry_run=args.dry_run)
    print(json.dumps(res, indent=2, ensure_ascii=False))

    print("\n=== 2. tag 5:00/km run threshold as unconfirmed/derived ===")
    print(json.dumps(tag_threshold(args.dry_run), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
