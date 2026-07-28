#!/usr/bin/env python3
"""day_overrides.py — the register of COACH-DIRECTED day-rule deviations.

WHY THIS EXISTS. `day_rules.{swim,bike,run}_days` describe an athlete's normal
weekly pattern, and the coach overrides them conversationally: "I told it this week
to swim on wed, so we swim on wed, rules are guidelines." Jamie's
`swim_days=["Tue","Thu"]` is a true description of behaviour (Tue x7, Thu x7, Wed x0
since 1 Jun), so it must NOT be widened to include Wed — that would throw away the
signal. But `validate_plan` classified the directed Wednesday swim as a HARD
invariant breach, which makes the audit permanently red on `day_rules`, and a
permanently red check is one nobody reads.

So: a deviation the coach DIRECTED is recorded here and downgrades to a soft
`{sport}_directed_day` advisory that still appears in the report. A deviation
NOBODY asked for stays a hard `{sport}_forbidden_day` — a generator quietly
drifting off the pattern is a real defect and is exactly what day_rules exist to
catch.

GRANULARITY: PER SESSION (sport family + calendar date). Argued against the two
alternatives:
  * per-week  — one entry excuses every deviation of that sport in the week, so a
                second, UNDIRECTED move hides behind the directed one.
  * standing  — an amendment with no end date is how Calum's Saturday exception
                became permanent (see `<key>_days_expires` in validate_plan). It
                recreates the problem it is meant to solve.
A dated key is also self-expiring: an entry for a past date can never excuse a
future deviation, so the register cannot silently accumulate permissions.

ANTI-DRIFT: overrides cannot be used to move the pattern by stealth.
`validate_plan` counts entries per sport+weekday over a rolling window and raises a
HARD `day_rules_drifted` once the same weekday has been directed
DRIFT_THRESHOLD times — "this is not an exception any more, amend the config."

SHAPE mirrors the existing reviewed-exception register
`athletes/<slug>/reference/rules-lint-accepted.json` (flat map, stable id -> prose
recording what was accepted and when it was reviewed):

    {"swim:2026-07-29": "Coach-directed ... Recorded 2026-07-28."}

Key = "<family>:<YYYY-MM-DD>", family in {swim, bike, run} (the day_rules key
without its `_days` suffix, so Ride/VirtualRide/GravelRide share one family).
Value = free prose. A non-string or empty value is IGNORED, i.e. a malformed
register fails CLOSED: the deviation stays hard. Nothing can silence a check by
being broken.

The register lives under `athletes/` which is GITIGNORED, so it never appears in a
diff — same as rules-lint-accepted.json. Schema is documented in
docs/rule-lifecycle.md.

HOW AN ENTRY GETS IN. Today: the coach hand-edits the JSON, or runs the CLI below.
The instructions themselves arrive in Telegram, so the durable fix is a call to
`record()` from the bot at the moment of instruction — see docs/rule-lifecycle.md
for the exact follow-up.

CLI (the only writer today):
  python3 lib/day_overrides.py --base-dir ClaudeCoach --slug jamie --list
  python3 lib/day_overrides.py --base-dir ClaudeCoach --slug jamie \
      --sport swim --date 2026-07-29 --note "Coach-directed in Telegram 27 Jul."
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

# day_rules key -> register family. Kept in step with validate_plan._SPORT_RULE:
# several ICU activity types collapse onto one family.
FAMILIES = ("swim", "bike", "run")
_KEY_RE = re.compile(r"^(swim|bike|run):(\d{4}-\d{2}-\d{2})$")


def register_path(slug: str, base) -> Path:
    return Path(base) / "athletes" / slug / "reference" / "day-rules-overrides.json"


def load(slug: str, base) -> dict[str, str]:
    """Valid entries only. A missing, unreadable or corrupt register yields {} —
    never an exception, and never a permission: every dropped entry means the
    deviation it would have excused stays a hard failure."""
    p = register_path(slug, base)
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(blob, dict):
        return {}
    out = {}
    for k, v in blob.items():
        if k.startswith("_"):          # allow a _note key, like the other registers
            continue
        if not _KEY_RE.match(str(k)):
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        out[str(k)] = v.strip()
    return out


def key(sport_or_family: str, on: date | str) -> str:
    fam = str(sport_or_family).strip().lower()
    fam = {"ride": "bike", "virtualride": "bike", "gravelride": "bike",
           "cycling": "bike"}.get(fam, fam)
    d = on if isinstance(on, str) else on.isoformat()
    return f"{fam}:{d[:10]}"


def record(slug: str, base, sport: str, on: date | str, note: str) -> str:
    """Add/replace one override. Returns the key written.

    This is the function the Telegram bot should call when the coach directs a
    session onto an off-pattern day; it is deliberately the whole write surface, so
    the follow-up is one call site and no new format."""
    k = key(sport, on)
    if not _KEY_RE.match(k):
        raise ValueError(f"bad override key {k!r} — family must be one of {FAMILIES}")
    if not (note or "").strip():
        raise ValueError("an override must carry a note saying who directed it and when")
    p = register_path(slug, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(blob, dict):
            blob = {}
    except Exception:
        blob = {}
    blob[k] = note.strip()
    p.write_text(json.dumps(blob, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return k


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-dir", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sport", help="swim | bike | run (or an ICU type: Ride, GravelRide…)")
    ap.add_argument("--date", help="YYYY-MM-DD — the session date being excused")
    ap.add_argument("--note", help="who directed it and when")
    a = ap.parse_args()
    if a.list or not (a.sport and a.date and a.note):
        reg = load(a.slug, a.base_dir)
        if not reg:
            print(f"{a.slug}: no day-rule overrides on record")
        for k, v in sorted(reg.items()):
            stale = "" if k.split(":")[1] >= date.today().isoformat() else "  (past)"
            print(f"{k}{stale}: {v}")
        return 0
    k = record(a.slug, a.base_dir, a.sport, a.date, a.note)
    print(f"recorded {k} for {a.slug} -> {register_path(a.slug, a.base_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
