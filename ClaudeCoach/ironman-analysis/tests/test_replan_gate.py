"""lib/replan_gate.py and lib/icu_fetch.recompute_violation — the ad-hoc-replan fix
(24 Aug 2026).

WHAT BROKE, repeatedly. Asked for a same/next-day replan, the chat coach edited around
whatever session was already on the calendar instead of rebuilding it from Fitness/
Fatigue/Form and week-load-to-date. Written up as a "[perm] rule" in Jamie's standing
rules eight times after the Sun 23 Aug incident (entries 109-114,117,120) — a rule the
model can read and still bypass is not a fix. This is the code precondition instead:
lib/icu_fetch.py's push_workout/edit_workout now refuse a load-bearing write unless
plan_tools.py (tss / session-for-load / required-tss) ran first, in the SAME chat turn,
for the SAME athlete. Turn identity travels as CC_TURN_ID, stamped once per turn by
engine.scoped_env and inherited by every Bash child of that one `claude` process.

No LLM, no network. replan_gate is pure file I/O against a tmpdir; recompute_violation
is a pure function once the "did it recompute" bool is supplied.
"""
import sys
import tempfile
import time
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import replan_gate as RG        # noqa: E402
import icu_fetch as F            # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_marker_dir(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(RG, "MARKER_DIR", d)
    yield d


# ---------------------------------------------------------------------------
# 1. replan_gate — the marker itself
# ---------------------------------------------------------------------------

def test_no_marker_is_not_recomputed():
    assert RG.recomputed_this_turn("jamie", "turn-1") is False


def test_mark_then_check_same_turn_is_recomputed():
    RG.mark_recomputed("jamie", "turn-1")
    assert RG.recomputed_this_turn("jamie", "turn-1") is True


def test_a_different_turn_id_is_not_authorised():
    # The exact shape of the bug this closes: evidence from an EARLIER turn must not
    # authorise a write in a later one just because it is the same athlete.
    RG.mark_recomputed("jamie", "turn-1")
    assert RG.recomputed_this_turn("jamie", "turn-2") is False


def test_a_different_athletes_marker_does_not_leak():
    RG.mark_recomputed("kathryn", "turn-1")
    assert RG.recomputed_this_turn("jamie", "turn-1") is False


def test_stale_marker_expires():
    RG.mark_recomputed("jamie", "turn-1")
    assert RG.recomputed_this_turn("jamie", "turn-1", max_age_s=0.0) is False


def test_a_later_recompute_overwrites_the_earlier_one():
    RG.mark_recomputed("jamie", "turn-1")
    RG.mark_recomputed("jamie", "turn-2")
    assert RG.recomputed_this_turn("jamie", "turn-1") is False
    assert RG.recomputed_this_turn("jamie", "turn-2") is True


def test_mark_recomputed_never_raises_with_no_slug_or_turn():
    RG.mark_recomputed("", "turn-1")
    RG.mark_recomputed("jamie", "")
    assert RG.recomputed_this_turn("", "turn-1") is False
    assert RG.recomputed_this_turn("jamie", "") is False


def test_mark_recomputed_fails_soft_on_an_unwritable_dir(monkeypatch):
    monkeypatch.setattr(RG, "MARKER_DIR", "/nonexistent/does/not/exist/at/all")
    RG.mark_recomputed("jamie", "turn-1")   # must not raise


# ---------------------------------------------------------------------------
# 2. icu_fetch.recompute_violation — who the gate applies to
# ---------------------------------------------------------------------------

def test_load_bearing_true_for_a_load_target():
    assert F.load_bearing({"load_target": 80}) is True


def test_load_bearing_false_for_name_and_description_only():
    assert F.load_bearing({"name": "Easy spin", "description": "steady"}) is False


def test_load_bearing_false_for_empty_payload():
    assert F.load_bearing({}) is False


def test_blocks_a_load_bearing_edit_with_no_recompute():
    msg = F.recompute_violation("edit_workout", "agreed", {"load_target": 80}, False)
    assert msg is not None and msg.startswith("ERROR")


def test_allows_the_same_edit_once_recomputed():
    msg = F.recompute_violation("edit_workout", "agreed", {"load_target": 80}, True)
    assert msg is None


def test_allows_a_cosmetic_only_edit_with_no_recompute():
    # Renaming a session already on the calendar is not the replan this gate exists for.
    msg = F.recompute_violation("edit_workout", "agreed",
                                {"name": "Renamed"}, False)
    assert msg is None


def test_allows_delete_workout_with_no_recompute():
    # Nothing here to get wrong the way a load/duration figure can be got wrong.
    msg = F.recompute_violation("delete_workout", "agreed", {}, False)
    assert msg is None


def test_allows_edit_activity_with_no_recompute():
    # A past activity, not a planned session — not what a replan touches.
    msg = F.recompute_violation("edit_activity", "agreed",
                                {"name": "Tuesday ride"}, False)
    assert msg is None


def test_exempts_coach_auto_same_day_modulation():
    # daily-prescription's readiness adjustment is not an ad-hoc replan, and
    # authority_violation already bounds it to today / no deletes.
    msg = F.recompute_violation("push_workout", "coach-auto", {"load_target": 80}, False)
    assert msg is None


def test_recomputed_none_means_not_a_chat_turn_and_is_not_gated():
    # A hand-run or a job that never went through engine.scoped_env carries no
    # CC_TURN_ID at all, so the caller passes recomputed=None — fail-open, exactly
    # like scope_violation and caller_violation on an unset env var.
    msg = F.recompute_violation("push_workout", "agreed", {"load_target": 80}, None)
    assert msg is None


# ---------------------------------------------------------------------------
# 3. End-to-end: plan_tools' side of the handshake, through replan_gate directly
#    (plan_tools.py itself shells out to primitives that need live athlete config,
#    so this pins the CONTRACT — mark_recomputed(slug, turn_id) — rather than
#    spawning the CLI).
# ---------------------------------------------------------------------------

def test_full_handshake_recompute_then_write():
    turn_id = "turn-abc123"
    slug = "jamie"
    assert F.recompute_violation("push_workout", "agreed",
                                 {"load_target": 90}, RG.recomputed_this_turn(slug, turn_id)) \
        is not None
    RG.mark_recomputed(slug, turn_id)   # what plan_tools.py's dispatcher does
    assert F.recompute_violation("push_workout", "agreed",
                                 {"load_target": 90}, RG.recomputed_this_turn(slug, turn_id)) \
        is None
