"""Tests for lib/chat_context.py. Run: python3 -m pytest ClaudeCoach/lib/test_chat_context.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import chat_context as cc


def test_activity_lines_carry_ids_and_the_numbers_asked_about():
    a = {"id": "i1", "start_date_local": "2026-10-01T12:32:13", "type": "Run", "moving_time": 3900,
         "distance": 13000, "icu_training_load": 92, "average_heartrate": 148.4,
         "icu_intensity": 82, "decoupling": 3.14, "name": "Wandsworth Running"}
    line = cc.activity_lines([a])[0]
    for bit in ("2026-10-01", "65min", "13.0km", "Load=92", "HR 148", "IF 0.82", "dec 3.1%",
                "id=i1", '"Wandsworth Running"'):
        assert bit in line


def test_last_week_marks_done_by_pairing_or_same_sport_and_not_done():
    past = [{"id": 1, "start_date_local": "2026-09-29T00:00:00", "type": "Run", "name": "Easy run"},
            {"id": 2, "start_date_local": "2026-09-29T00:00:00", "type": "Swim", "name": "Technique"},
            {"id": 3, "start_date_local": "2026-09-27T00:00:00", "type": "Ride", "name": "Z2",
             "paired_activity_id": "i9"}]
    acts = [{"id": "i5", "start_date_local": "2026-09-29T07:27:23", "type": "VirtualRun"}]
    lines = cc.planned_last_week(past, acts)
    assert "done (i9)" in lines[0] and "done (i5)" in lines[1] and "NOT DONE" in lines[2]
    assert all("id=" in ln for ln in lines)


def test_upcoming_lists_every_event_with_id_and_load_up_to_the_cap():
    evs = [{"id": n, "start_date_local": f"2026-10-{n:02d}T00:00:00", "type": "Ride",
            "name": f"S{n}", "load_target": 50} for n in range(1, 46)]
    lines = cc.upcoming(evs)
    assert len(lines) == 41 and "id=1" in lines[0] and "load 50" in lines[0]
    assert lines[-1].startswith("  (+5 more")
