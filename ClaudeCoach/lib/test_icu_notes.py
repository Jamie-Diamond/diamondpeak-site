"""Notes reach the athlete: Intervals.icu drops description_raw (checked live 17 Oct 2026),
so IcuClient writes them into description after the steps (icu_api.with_notes)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import icu_api  # noqa: E402

STEPS = "Main Set 3x\n- 10m 88-94%\n- 5m 55%"


def test_notes_follow_the_steps_and_bullets_cannot_become_steps():
    out = icu_api.with_notes(STEPS, "Fuel 60 g CHO/hr\n- eat from 15 min")
    assert out == STEPS + "\n\nNotes\nFuel 60 g CHO/hr\n• eat from 15 min"
    assert [l for l in out.splitlines() if l.startswith("-")] == ["- 10m 88-94%", "- 5m 55%"]


def test_new_notes_replace_old_ones_and_empty_notes_leave_steps():
    once = icu_api.with_notes(STEPS, "old")
    assert icu_api.with_notes(once, "new") == STEPS + "\n\nNotes\nnew"
    assert icu_api.with_notes(STEPS, "  ") == STEPS
    assert icu_api.with_notes("", "only") == "Notes\nonly"


class _Client(icu_api.IcuClient):
    def __init__(self):            # no network
        self.athlete_id, self.sent, self.ev = "i1", [], {"description": STEPS}

    def _post(self, path, payload):
        self.sent.append(payload)
        return {"id": 1}

    def _put(self, path, payload):
        self.sent.append(payload)
        return {}

    def get_event(self, event_id):
        return self.ev


def test_push_and_edit_send_one_description():
    c = _Client()
    c.push_workout(sport="Ride", event_date="2099-01-01", name="x", description=STEPS, description_raw="Fuel 60 g")
    assert "description_raw" not in c.sent[0] and c.sent[0]["description"].endswith("Notes\nFuel 60 g")
    c.edit_workout(1, description_raw="Ride it easy")         # keeps the event's own steps
    assert c.sent[1] == {"description": STEPS + "\n\nNotes\nRide it easy"}
