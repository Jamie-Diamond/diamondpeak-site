"""Per-athlete evidence that THIS chat turn recomputed load before writing the calendar.

WHY (24 Aug 2026). The ad-hoc-replan bug: asked for a same/next-day replan, the chat
coach edited around whatever session was already on the calendar instead of rebuilding
it from Fitness/Fatigue/Form and week-load-to-date. Written up as a "[perm] rule" in
Jamie's standing rules EIGHT times after the Sun 23 Aug incident (entries 109-114,117,120)
because a prose rule is not a precondition — the model can read it, agree with it, and
still reach for the stale event next time. `_ACCURACY_RULE` in lib/engine.py already
solved the sibling problem (hand-converting TSS to minutes) by pointing at a tool
instead of asking nicely; this module is the enforcement half that rule never got:
lib/icu_fetch.py's push_workout/edit_workout refuse a load-bearing write unless a
plan_tools.py load command (tss / session-for-load / required-tss) ran FIRST, in the
SAME turn, for the SAME athlete.

TURN SCOPE. Both plan_tools.py and icu_fetch.py are separate processes spawned via
Bash from one `claude` CLI turn, so "this turn" has to be established through
something both inherit: engine.scoped_env stamps CC_TURN_ID (a fresh id per spawned
turn) alongside the existing CC_ATHLETE_SCOPE, and every Bash child of that CLI
process inherits both. Neither variable exists outside a chat turn (hand runs, cron
jobs), so this gate is a no-op everywhere else — the same fail-open shape as
CC_ATHLETE_SCOPE and --caller in icu_fetch.py.

ONE MARKER PER ATHLETE, not one per turn: each recompute OVERWRITES the previous
marker rather than adding to it, so nothing accumulates and there is nothing to prune.
The turn id inside it is what makes an old turn's marker unable to authorise a new
one's write.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

# Overridable so tests don't touch the real path. /tmp (not the athlete's own
# directory): this is transient per-turn evidence, not athlete data, and does not
# need to survive a reboot.
MARKER_DIR = os.environ.get("CC_REPLAN_MARKER_DIR", "/tmp/claudecoach-replan-gate")

# Generous for one turn, including a resume-fallback or model-fallback retry (both
# reuse the same env/turn id) — short enough that a marker from an much earlier,
# unrelated turn cannot authorise a write minutes later.
MAX_AGE_S = 1800.0


def _marker_path(slug: str) -> Path:
    return Path(MARKER_DIR) / f"{slug}.json"


def mark_recomputed(slug: str, turn_id: str) -> None:
    """Record that a load command ran for `slug` under `turn_id`. Never raises: a
    marker that fails to write must not fail the maths command that computed it —
    it only means the NEXT write refuses and asks for the recompute again, which is
    the safe direction to fail in."""
    if not slug or not turn_id:
        return
    try:
        Path(MARKER_DIR).mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=MARKER_DIR, prefix=".rg-", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump({"turn_id": turn_id, "ts": time.time()}, fh)
        os.replace(tmp, _marker_path(slug))
    except Exception:
        pass


def recomputed_this_turn(slug: str, turn_id: str, max_age_s: float = MAX_AGE_S) -> bool:
    """True iff `slug`'s marker names THIS `turn_id` and is not stale.

    Fails CLOSED (False) on anything unreadable — missing file, bad JSON, wrong or
    absent turn id, or a marker older than `max_age_s`. This is the one place in the
    gate that fails closed rather than open: the caller only reaches here once it has
    already established that this IS a chat turn subject to the gate, and "cannot
    prove it recomputed" is exactly the case the gate exists to catch."""
    if not slug or not turn_id:
        return False
    try:
        data = json.loads(_marker_path(slug).read_text())
    except Exception:
        return False
    if data.get("turn_id") != turn_id:
        return False
    age = time.time() - float(data.get("ts") or 0)
    return 0 <= age <= max_age_s
