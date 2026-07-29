"""Closing an open action from Telegram — the capture half of lib/open_actions.py.

The store has computed honest state since 28 Jul and its only writer was a CLI on the VM,
which is why every status cell in the markdown table sat as a template placeholder for three
months. These tests cover the detector/parser pair, the candidate matcher, and the
closed_by/closed provenance that distinguishes a decision from an omission.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import open_actions as oa      # noqa: E402
import races                   # noqa: E402
import weekly_availability as wa   # noqa: E402

TODAY = date(2026, 7, 29)

# A stand-in for the real store: the shapes that matter (overdue, gating, open-ended).
STATE = {
    "open_actions": [
        {"action": "Book Precision Hydration sweat-sodium test", "due": "2026-05-31",
         "status": "pending", "gates_race_decision": True},
        {"action": "TT bike fit appointment", "due": "2026-05-31", "status": "pending"},
        {"action": "Aero helmet", "due": "2026-06-30", "status": "pending"},
        {"action": "ISM saddle order", "due": "2026-08-06", "status": "pending"},
        {"action": "5+ hr ice retention test", "due": "2026-09-01", "status": "pending"},
    ]
}


@pytest.fixture()
def adir(tmp_path):
    d = tmp_path / "athletes" / "jamie"
    d.mkdir(parents=True)
    (d / "current-state.json").write_text(json.dumps(STATE, indent=1))
    return d


class TestInstructionDetection:
    @pytest.mark.parametrize("msg", [
        "sweat test is booked",
        "had the sweat sodium test done",
        "bike fit done",
        "aero helmet ordered",
        "drop the ISM saddle order",
        "push the ice retention test to next week",
    ])
    def test_detected(self, msg):
        assert oa.looks_like_action_instruction(msg), msg

    @pytest.mark.parametrize("msg", [
        "Is the sweat test done?",
        "is the bike fit still open",
        "what actions are outstanding",
        "how many are overdue",
        "any news on the saddle",
        "should I drop the aero helmet?",
    ])
    def test_a_question_is_never_an_instruction(self, msg):
        # "Is the sweat test done?" must not close the sweat test.
        assert not oa.looks_like_action_instruction(msg), msg

    @pytest.mark.parametrize("msg", [
        "I did my long run",
        "morning, feeling good",
        "swim Wednesday this week",
    ])
    def test_ordinary_chat_selects_no_action(self, msg, adir):
        # Against the REAL store, not an empty one: candidates() is trivially [] for a
        # missing directory, so the earlier form of this test proved nothing whatever the
        # detector did.
        items = oa.evaluate_from_dir(adir, TODAY)
        assert oa.open_items(items), "fixture must have open items or this proves nothing"
        p = oa.parse_action_message(msg)
        assert not (p["status"] and oa.candidates(items, p["subject"])), msg


class TestParsedIntent:
    @pytest.mark.parametrize("msg,status", [
        ("sweat test is booked", "booked"),
        ("aero helmet ordered", "ordered"),
        ("bike fit done", "done"),
        ("drop the ISM saddle order", "dropped"),
        ("the running FTP is sorted", "done"),
        ("bike fit is in the diary", "scheduled"),
    ])
    def test_status_intent(self, msg, status):
        assert oa.parse_action_message(msg)["status"] == status

    def test_defer_resolves_a_new_date(self):
        p = oa.parse_action_message("push the ice retention test to next week", TODAY)
        assert p["status"] == "defer"
        assert p["defer_to"] == "2026-08-05"

    def test_defer_reads_an_iso_date(self):
        p = oa.parse_action_message("push the sweat test to 2026-09-15", TODAY)
        assert p["defer_to"] == "2026-09-15"

    def test_defer_with_no_date_is_refused(self):
        # A deferral with no new date is how an item slips forever with nobody noticing —
        # exactly what the escalation bands exist to expose.
        p = oa.parse_action_message("defer the aero helmet", TODAY)
        assert p["status"] == ""
        assert p["refused"] == "deferral with no new date"

    def test_the_whole_message_is_kept_as_the_note(self):
        p = oa.parse_action_message("sweat test is booked for the 12th")
        assert p["note"] == "sweat test is booked for the 12th"


class TestCandidateMatching:
    def test_matches_the_intended_item(self, adir):
        items = oa.evaluate_from_dir(adir, TODAY)
        p = oa.parse_action_message("had the sweat sodium test done")
        c = oa.candidates(items, p["subject"])
        assert c and c[0]["action"] == "Book Precision Hydration sweat-sodium test"

    def test_no_overlap_yields_no_candidates(self, adir):
        # Must return nothing rather than the closest of five on no evidence.
        items = oa.evaluate_from_dir(adir, TODAY)
        assert oa.candidates(items, "swimming goggles") == []

    def test_closed_items_are_never_candidates(self, adir):
        oa.set_status("jamie", "Aero helmet", "done", athlete_dir=adir,
                      closed_by="jamie via telegram", today=TODAY)
        items = oa.evaluate_from_dir(adir, TODAY)
        assert oa.candidates(items, "aero helmet") == []

    def test_generic_words_do_not_select(self, adir):
        # `test` appears in three labels, so it must not look like a confident match.
        items = oa.evaluate_from_dir(adir, TODAY)
        assert oa.candidates(items, "test") == []


class TestWritesRecordWhoAndWhen:
    def test_close_records_who_and_when(self, adir):
        e = oa.set_status("jamie", "sweat-sodium", "done", athlete_dir=adir,
                          closed_by="jamie via telegram", today=TODAY)
        assert e["status"] == "done"
        assert e["closed"] == "2026-07-29"
        assert e["closed_by"] == "jamie via telegram"

    def test_a_closed_item_is_distinguishable_from_one_never_captured(self, adir):
        oa.set_status("jamie", "sweat-sodium", "dropped", athlete_dir=adir,
                      closed_by="jamie via telegram", today=TODAY)
        items = oa.evaluate_from_dir(adir, TODAY)
        closed = [i for i in items if not i["open"]]
        assert len(closed) == 1
        # Both a dropped item and one that was never captured are absent from the open
        # list; only this field says which happened.
        assert closed[0]["closed_by"] == "jamie via telegram"
        assert closed[0]["closed"] == "2026-07-29"
        assert "aero helmet order that never existed" not in [i["action"] for i in items]

    def test_reopening_clears_the_provenance(self, adir):
        oa.set_status("jamie", "sweat-sodium", "done", athlete_dir=adir,
                      closed_by="jamie via telegram", today=TODAY)
        e = oa.set_status("jamie", "sweat-sodium", "pending", athlete_dir=adir)
        assert "closed" not in e and "closed_by" not in e

    def test_a_defer_keeps_the_item_open(self, adir):
        e = oa.set_status("jamie", "ice retention", "pending", athlete_dir=adir,
                          due="2026-10-01")
        assert e["due"] == "2026-10-01"
        assert "closed" not in e
        items = oa.evaluate_from_dir(adir, TODAY)
        assert any(i["action"].startswith("5+ hr") and i["open"] for i in items)

    def test_an_ambiguous_match_raises_rather_than_closing_the_wrong_thing(self, adir):
        with pytest.raises(ValueError, match="matches"):
            oa.set_status("jamie", "test", "done", athlete_dir=adir)

    def test_a_missing_match_raises(self, adir):
        with pytest.raises(ValueError, match="no action matching"):
            oa.set_status("jamie", "swimming goggles", "done", athlete_dir=adir)

    def test_the_store_stays_valid_json_after_a_write(self, adir):
        oa.set_status("jamie", "sweat-sodium", "done", athlete_dir=adir,
                      closed_by="jamie via telegram", today=TODAY)
        blob = json.loads((adir / "current-state.json").read_text())
        assert len(blob["open_actions"]) == len(STATE["open_actions"])


class TestCrossFireWithOtherCaptures:
    @pytest.mark.parametrize("msg", [
        "sweat test is booked",
        "drop the ISM saddle order",
        "bike fit done",
        "push the ice retention test to next week",
    ])
    def test_an_action_instruction_is_not_read_as_hours(self, msg):
        assert not wa.looks_like_hours_declaration(msg), msg
        assert not wa.looks_like_hours_reply(msg), msg

    @pytest.mark.parametrize("msg", [
        "sweat test is booked",
        "drop the ISM saddle order",
    ])
    def test_an_action_instruction_is_not_read_as_a_race(self, msg):
        assert not races.looks_like_race_statement(msg, TODAY), msg

    def test_an_action_phrase_naming_a_race_does_not_become_a_race(self, msg=None):
        # "the sweat test gates the race decision" mentions a race but announces none.
        for m in ("drop the Dorney entry", "the sweat test gates the race decision"):
            assert not races.looks_like_race_statement(m, TODAY), m

    @pytest.mark.parametrize("msg", ["14h next week", "20, big week"])
    def test_an_hours_declaration_is_not_read_as_an_action(self, msg):
        assert not oa.looks_like_action_instruction(msg), msg
