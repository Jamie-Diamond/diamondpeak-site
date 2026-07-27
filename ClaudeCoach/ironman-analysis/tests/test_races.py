"""Race registry and the race-day branching (lib/races.py).

The point of these: `race_date` was structured data that nothing branched on, so every
one of these assertions was previously unrepresentable. The phase tests pin `today`
rather than reading a clock, which is why the race-day path is testable at all.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[2]        # ClaudeCoach/
sys.path.insert(0, str(_BASE / "lib"))
import races  # noqa: E402


A_RACE = {"name": "IM Cervia", "date": "2026-09-19", "priority": "A",
          "distance": "Full Ironman", "status": "upcoming"}
B_RACE = {"name": "Dorney Olympic tri", "date": "2026-07-26", "priority": "B",
          "status": "completed"}


@pytest.fixture
def reg():
    return sorted([races.normalise(A_RACE), races.normalise(B_RACE)],
                  key=lambda r: r["date"])


class TestNormalise:
    def test_unknown_priority_stays_none_not_defaulted(self):
        # The whole registry rests on this: a guessed A/B/C is worse than an absent one.
        assert races.normalise({"name": "x", "date": "2026-01-01"})["priority"] is None
        assert races.normalise({"name": "x", "date": "2026-01-01",
                                "priority": "important"})["priority"] is None

    def test_priority_case_and_whitespace_tolerated(self):
        assert races.normalise({"name": "x", "date": "2026-01-01",
                                "priority": " b "})["priority"] == "B"

    def test_status_implied_from_date_when_absent(self):
        past = races.normalise({"name": "x", "date": "2020-01-01"})
        assert past["status"] == "completed"
        future = races.normalise({"name": "x", "date": "2099-01-01"})
        assert future["status"] == "upcoming"

    def test_explicit_status_beats_the_date(self):
        # A future race can legitimately be marked completed/withdrawn by a human.
        r = races.normalise({"name": "x", "date": "2099-01-01", "status": "completed"})
        assert r["status"] == "completed"


class TestPhase:
    """The branch that did not exist. Every case pins `today`."""

    def test_race_day(self, reg):
        ph = races.race_phase(reg, date(2026, 9, 19))
        assert ph["phase"] == "race_day" and ph["days_to"] == 0
        assert ph["race"]["name"] == "IM Cervia"

    def test_race_eve(self, reg):
        assert races.race_phase(reg, date(2026, 9, 18))["phase"] == "race_eve"

    def test_race_week(self, reg):
        assert races.race_phase(reg, date(2026, 9, 14))["phase"] == "race_week"

    def test_race_completed_the_morning_after(self, reg):
        ph = races.race_phase(reg, date(2026, 9, 20))
        assert ph["phase"] == "race_completed" and ph["days_to"] == -1

    def test_ordinary_day_is_none_so_callers_fall_through(self, reg):
        assert races.race_phase(reg, date(2026, 6, 1))["phase"] is None

    def test_eight_days_out_is_not_race_week(self, reg):
        assert races.race_phase(reg, date(2026, 9, 11))["phase"] is None

    def test_completed_race_does_not_trigger_race_week(self, reg):
        # Five days before the already-completed B-race: it must not open a race week.
        assert races.race_phase(reg, date(2026, 7, 21))["phase"] is None

    def test_upcoming_race_tomorrow_beats_race_completed_yesterday(self):
        # Back-to-back race weekends: the pre-race message matters more than a 2nd debrief.
        reg = [races.normalise({"name": "sat", "date": "2026-05-02"}),
               races.normalise({"name": "sun", "date": "2026-05-04"})]
        ph = races.race_phase(reg, date(2026, 5, 3))
        assert ph["phase"] == "race_eve" and ph["race"]["name"] == "sun"

    def test_undated_race_never_fires_a_branch(self):
        reg = [races.normalise({"name": "someday", "date": None})]
        assert races.race_phase(reg, date(2026, 5, 3))["phase"] is None


class TestARace:
    def test_a_race_selected_over_b(self, reg):
        assert races.a_race(reg)["priority"] == "A"

    def test_earliest_upcoming_a_race_wins_across_seasons(self):
        reg = [races.normalise({"name": "2027", "date": "2027-09-01", "priority": "A"}),
               races.normalise({"name": "2026", "date": "2026-09-01", "priority": "A"})]
        assert races.a_race(sorted(reg, key=lambda r: r["date"]))["name"] == "2026"

    def test_no_a_race_returns_none_rather_than_picking_one(self, reg):
        only_b = [r for r in reg if r["priority"] == "B"]
        assert races.a_race(only_b) is None


class TestLegacyFields:
    """The existing `race_date` consumers must not notice the registry landing."""

    def test_race_date_derived_from_the_a_race(self):
        e = {"race_date": "2026-09-19", "race_name": "IM Italy Emilia-Romagna",
             "races": [A_RACE, B_RACE]}
        races.sync_legacy_fields(e)
        assert e["race_date"] == "2026-09-19"

    def test_existing_race_name_is_not_restyled(self):
        # bot.py:329 builds a countdown-stripping regex from race_name, so a tidier
        # registry spelling must not silently replace the configured one.
        e = {"race_date": "2026-08-29", "race_name": "Tour de stations, marmottes",
             "races": [{"name": "Tour de Stations / Marmottes", "date": "2026-08-29",
                        "priority": "A"}]}
        races.sync_legacy_fields(e)
        assert e["race_name"] == "Tour de stations, marmottes"

    def test_race_name_follows_when_the_a_race_moves(self):
        e = {"race_date": "2026-01-01", "race_name": "Old Race",
             "races": [A_RACE]}
        races.sync_legacy_fields(e)
        assert (e["race_date"], e["race_name"]) == ("2026-09-19", "IM Cervia")

    def test_no_a_race_leaves_legacy_fields_untouched(self):
        e = {"race_date": "2026-09-19", "race_name": "Keep Me", "races": [B_RACE]}
        races.sync_legacy_fields(e)
        assert (e["race_date"], e["race_name"]) == ("2026-09-19", "Keep Me")

    def test_load_races_falls_back_to_legacy_race_date(self):
        # An athlete configured before the registry existed still gets a race-aware path.
        cfg = {"x": {"race_date": "2026-09-19", "race_name": "IM Cervia"}}
        rs = races.load_races("x", cfg)
        assert len(rs) == 1 and rs[0]["priority"] == "A"


class TestParsing:
    MON = date(2026, 7, 27)          # a Monday

    def test_weekday_resolves_forward(self):
        assert races.resolve_date("on Saturday", self.MON) == date(2026, 8, 1)

    def test_next_weekday_is_a_week_further(self):
        assert races.resolve_date("next Saturday", self.MON) == date(2026, 8, 8)

    def test_same_weekday_means_next_week_not_today(self):
        assert races.resolve_date("on Monday", self.MON) == date(2026, 8, 3)

    def test_iso_and_day_month_and_month_day(self):
        assert races.resolve_date("2026-09-19", self.MON) == date(2026, 9, 19)
        assert races.resolve_date("19 September", self.MON) == date(2026, 9, 19)
        assert races.resolve_date("Sept 19", self.MON) == date(2026, 9, 19)

    def test_priority_is_reported_missing_so_the_caller_asks(self):
        p = races.parse_race_message("I'm racing Dorney on Saturday", self.MON)
        assert p["name"] == "Dorney"
        assert p["date"] == "2026-08-01"
        assert p["priority"] is None
        assert "priority" in p["missing"]

    def test_stated_priority_is_read(self):
        p = races.parse_race_message(
            "I'm racing the South Bucks Sprint on 15 August as a B race", self.MON)
        assert (p["name"], p["priority"], p["date"]) == ("South Bucks Sprint", "B", "2026-08-15")
        assert p["missing"] == []

    def test_detector_ignores_ordinary_training_talk(self):
        # The failure mode that would matter: hijacking normal chat.
        for msg in ["I'm doing my long run tomorrow",
                    "shall I move Saturday's ride?",
                    "felt rough on Tuesday",
                    "I'm racing fit at the moment"]:
            assert not races.looks_like_race_statement(msg, self.MON), msg

    def test_detector_needs_a_date_as_well_as_a_verb(self):
        assert not races.looks_like_race_statement("I'm racing Dorney", self.MON)
        assert races.looks_like_race_statement("I'm racing Dorney on Saturday", self.MON)


class TestWriting:
    def _cfg(self, tmp_path):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"jamie": {"race_date": "2026-09-19",
                                           "race_name": "IM Cervia"}}))
        return p

    def test_add_race_writes_and_syncs(self, tmp_path):
        p = self._cfg(tmp_path)
        races.add_race("jamie", "IM Cervia", "2026-09-19", priority="A", path=p)
        cfg = json.loads(p.read_text())
        assert cfg["jamie"]["races"][0]["priority"] == "A"
        assert cfg["jamie"]["race_date"] == "2026-09-19"

    def test_same_date_updates_in_place_rather_than_duplicating(self, tmp_path):
        p = self._cfg(tmp_path)
        races.add_race("jamie", "Dorney", "2026-07-26", path=p)
        races.add_race("jamie", "Dorney", "2026-07-26", priority="B", path=p)
        rs = json.loads(p.read_text())["jamie"]["races"]
        assert len(rs) == 1 and rs[0]["priority"] == "B"

    def test_refuses_an_unparseable_date(self, tmp_path):
        p = self._cfg(tmp_path)
        with pytest.raises(ValueError):
            races.add_race("jamie", "Mystery", "sometime in spring", path=p)

    def test_refuses_a_nameless_race(self, tmp_path):
        p = self._cfg(tmp_path)
        with pytest.raises(ValueError):
            races.add_race("jamie", "  ", "2026-09-19", path=p)

    def test_unknown_athlete_refused(self, tmp_path):
        p = self._cfg(tmp_path)
        with pytest.raises(KeyError):
            races.add_race("nobody", "X", "2026-09-19", path=p)


class TestIcuRaceEvents:
    def test_race_category_is_read_not_discarded(self):
        class _C:
            def get_events(self, a, b):
                return [{"category": "WORKOUT", "name": "Z2 ride",
                         "start_date_local": "2026-09-01T00:00:00"},
                        {"category": "RACE", "name": "IM Cervia", "id": 42,
                         "start_date_local": "2026-09-19T00:00:00"}]
        out = races.icu_race_events(_C(), "2026-01-01", "2026-12-31")
        assert len(out) == 1
        assert out[0]["name"] == "IM Cervia" and out[0]["date"] == "2026-09-19"
        # Priority is never inferred from ICU — the API has no such field.
        assert out[0]["priority"] is None

    def test_api_failure_is_not_fatal(self):
        class _C:
            def get_events(self, a, b):
                raise RuntimeError("intervals.icu down")
        assert races.icu_race_events(_C(), "2026-01-01", "2026-12-31") == []


class TestWording:
    """docs/tone-of-voice-guide.md §8.6 as assertions."""

    def test_pre_race_is_short_and_wishes_luck(self):
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie",
                                  block_fact="You have 20 weeks of work behind you")
        assert "Good luck" in m
        assert len(m.split()) < 80

    def test_pre_race_introduces_no_numbers(self):
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie")
        assert not any(ch.isdigit() for ch in m)

    def test_pre_race_caps_focus_at_three_and_invents_none(self):
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie",
                                  focus=["a", "b", "c", "d"])
        assert "d" not in m.split(": ")[-1].split("; ")
        assert m.count(";") == 2

    def test_post_race_leads_on_the_result_not_the_analysis(self):
        m = races.render_post_race(races.normalise(B_RACE), "Jamie", good_day=True)
        assert m.split("\n")[0].startswith("Dorney Olympic tri done")

    def test_bad_day_carries_no_diagnosis(self):
        m = races.render_post_race(races.normalise(B_RACE), "Jamie", good_day=False)
        assert "No analysis today" in m
        assert not any(ch.isdigit() for ch in m)

    def test_unknown_result_asks_rather_than_assuming(self):
        m = races.render_post_race(races.normalise(B_RACE), "Jamie")
        assert "How did it go?" in m

    def test_prompt_block_empty_on_an_ordinary_day(self, reg):
        assert races.prompt_block(reg, date(2026, 6, 1)) == ""

    def test_prompt_block_names_the_phase(self, reg):
        assert "IS TODAY" in races.prompt_block(reg, date(2026, 9, 19))
        assert "TOMORROW" in races.prompt_block(reg, date(2026, 9, 18))

    def test_prompt_block_says_priority_unconfirmed_rather_than_guessing(self):
        reg = [races.normalise({"name": "Mystery", "date": "2026-09-19"})]
        assert "priority not confirmed" in races.prompt_block(reg, date(2026, 9, 19))


class TestPostRaceFiresOnce:
    """POST_RACE_DAYS keeps the window open for a couple of days so a Sunday race is
    still caught by a Tuesday card, but the message itself must be a one-off."""

    def _reg(self, sent=False):
        r = {"name": "Dorney Olympic tri", "date": "2026-07-26", "priority": "B",
             "status": "completed"}
        if sent:
            r["post_race_sent"] = True
        return [races.normalise(r)]

    def test_fires_while_unacknowledged(self):
        assert races.race_phase(self._reg(), date(2026, 7, 27))["phase"] == "race_completed"

    def test_does_not_fire_again_once_acknowledged(self):
        assert races.race_phase(self._reg(sent=True), date(2026, 7, 27))["phase"] is None
        assert races.race_phase(self._reg(sent=True), date(2026, 7, 28))["phase"] is None

    def test_mark_persists_and_survives_normalise(self, tmp_path):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"jamie": {"races": [
            {"name": "Dorney", "date": "2026-07-26", "priority": "B"}]}}))
        assert races.mark_post_race_sent("jamie", "2026-07-26", path=p) is True
        rs = races.load_races("jamie", json.loads(p.read_text()))
        assert rs[0]["post_race_sent"] is True

    def test_mark_reports_false_when_no_such_race(self, tmp_path):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"jamie": {"races": []}}))
        assert races.mark_post_race_sent("jamie", "2026-07-26", path=p) is False
