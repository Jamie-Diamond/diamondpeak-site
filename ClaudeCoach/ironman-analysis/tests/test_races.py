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

    def test_pre_race_body_carries_no_numbers_of_its_own(self):
        # Named precisely: the TEMPLATE contributes no figures. block_fact is excluded
        # deliberately — §8.6's own worked example for "name the work behind it" is
        # "You've done sixteen weeks and four rides over four hours for this", so the
        # week count is sanctioned there. The focus points are held to digit-free
        # separately (TestDerivedFocusCannotInventANumber).
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie")
        assert not any(ch.isdigit() for ch in m)

    def test_focus_points_are_separate_sentences_not_a_welded_list(self):
        # Jamie read the first version - "Three things, all already done before: a; b; c" -
        # and said it did not make sense. It was a template talking. Points are now spoken
        # sentences, and no counted lead-in or semicolon welding may come back.
        r = races.normalise(A_RACE)
        m = races.render_pre_race(r, "Jamie", focus=["go easy early", "let it come to you",
                                                    "keep eating"])
        assert ";" not in m
        for banned in ("Three things", "Two things", "One thing", "already done before"):
            assert banned not in m
        assert "Go easy early." in m
        assert "Let it come to you." in m
        assert "And keep eating." in m          # a spoken list closes with "and"

    def test_a_single_focus_point_gets_no_stray_and(self):
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie", focus=["go easy early"])
        assert "Go easy early." in m
        assert "And" not in m

    def test_good_luck_leads_rather_than_signs_off(self):
        # It is the one thing the message exists to say; it used to be the last line.
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie", focus=["go easy early"])
        assert m.splitlines()[0].endswith("Good luck.")

    def test_the_work_behind_it_closes_the_message(self):
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie", focus=["go easy early"],
                                  block_fact="You have twenty weeks behind you for this one")
        assert m.rstrip().endswith("You have twenty weeks behind you for this one.")

    def test_pre_race_caps_focus_at_three_and_invents_none(self):
        # Labels deliberately free of number words — "bravo two" would be dropped by the
        # quantity guard, which is correct behaviour but tests the wrong thing here.
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie",
                                  focus=["swim steady", "ride patient", "run relaxed",
                                         "smile lots"])
        assert "Swim steady." in m and "Ride patient." in m and "And run relaxed." in m
        assert "smile" not in m.lower()          # the fourth point is dropped, not folded

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


class TestQuestionsAreNotRaceAnnouncements:
    """Asking about a race is not announcing one. These forms clear both the racing-verb
    and the date test, and the name-strip chain reduces them to a bare "?" — which was
    written as a race called "?" before the two guards below existed."""

    MON = date(2026, 7, 27)

    QUESTIONS = [
        "Am I racing on Saturday?",
        "Am I racing tomorrow?",
        "Should I be racing on Saturday?",
        "is my race on Saturday?",
        "when is my next race, Saturday?",
    ]

    def test_questions_are_not_captured(self):
        for q in self.QUESTIONS:
            assert not races.looks_like_race_statement(q, self.MON), q

    def test_punctuation_only_name_is_rejected_even_without_the_question_guard(self):
        # Belt and braces: if a question form ever slips past the '?' check, the parser
        # must still refuse to name a race after leftover punctuation.
        p = races.parse_race_message("Am I racing on Saturday?", self.MON)
        assert p["name"] is None
        assert "name" in p["missing"]

    def test_a_real_announcement_still_works(self):
        assert races.looks_like_race_statement("I'm racing Dorney on Saturday", self.MON)


class TestDerivedFocusCannotInventANumber:
    """The hard constraint. Three independent layers, each asserted separately, so the
    guarantee does not rest on any one of them holding."""

    def test_layer1_every_catalogue_line_is_digit_free(self):
        # Nothing is generated at run time: the lines are a fixed catalogue. If a future
        # edit puts a figure in one, this fails before it can ever reach an athlete.
        for cid, _rank, text, _why in races.FOCUS_CATALOGUE:
            assert not any(c.isdigit() for c in text), f"{cid} carries a figure: {text!r}"

    def test_layer1_no_catalogue_line_states_a_quantity_in_words_either(self):
        for cid, _rank, text, _why in races.FOCUS_CATALOGUE:
            assert not races._carries_a_figure(text), f"{cid} states a quantity: {text!r}"

    def test_layer1_catalogue_lines_are_composable_clauses(self):
        # They get capitalised and joined into sentences, so a leading capital or an
        # internal em-dash (the shape that produced the garbled first version) would show
        # up mid-message as broken punctuation.
        for cid, _rank, text, _why in races.FOCUS_CATALOGUE:
            assert text[0].islower(), f"{cid} should not be pre-capitalised: {text!r}"
            assert "—" not in text, f"{cid} carries an internal em-dash: {text!r}"
            assert not text.endswith("."), f"{cid} should not be pre-punctuated: {text!r}"

    def test_layer1_catalogue_ids_and_ranks_are_unique(self):
        ids = [c[0] for c in races.FOCUS_CATALOGUE]
        ranks = [c[1] for c in races.FOCUS_CATALOGUE]
        assert len(set(ids)) == len(ids)
        assert len(set(ranks)) == len(ranks)      # ranking must be total, not arbitrary

    def test_layer3_a_catalogue_line_with_a_figure_is_dropped_not_shipped(self, monkeypatch):
        # Simulate the future regression layer 3 exists to catch.
        poisoned = [("pace_first_half", 10, "hold 250 W for the first hour", "why")]
        monkeypatch.setattr(races, "FOCUS_CATALOGUE", poisoned)
        prof = {"prev_race": {"notes": "Lap 2 fade, decoupling 14.5%"}}
        sel, sup = races.derive_focus(prof, "")
        assert sel == []
        assert any(s["id"] == "pace_first_half" and "no numbers" in s["reason"]
                   for s in sup)

    def test_layer3_render_drops_a_curated_focus_point_carrying_a_figure(self):
        # The override path: a hand-written race_focus list comes through render, so the
        # guard has to sit there too.
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie",
                                  focus=["hold 250 W on the climb", "stay calm"])
        assert "250" not in m
        assert "Stay calm." in m                 # composed as a sentence, so capitalised

    def test_layer3_render_also_drops_a_figure_written_as_a_word(self):
        # A digit check alone would let "two hundred and fifty watts" straight through.
        m = races.render_pre_race(races.normalise(A_RACE), "Jamie",
                                  focus=["hold two hundred and fifty watts", "stay calm"])
        assert "hundred" not in m and "fifty" not in m
        assert "Stay calm." in m

    def test_no_derived_message_contains_a_digit_in_its_focus_sentence(self):
        prof = {"prev_race": {"notes": "fade, walk-breaks from km 13",
                              "t1t2_time": "~10:00"},
                "race_targets": {"t1t2_time": "5:30"}}
        sel, _ = races.derive_focus(prof, "", avg_g_hr=30, race_target_g_hr=90)
        for f in sel:
            assert not any(c.isdigit() for c in f["text"])


class TestFocusEvidenceGating:
    def test_no_evidence_means_no_points_rather_than_padding(self):
        sel, _ = races.derive_focus({}, "")
        assert sel == []

    def test_a_standing_rule_can_suppress_a_fuel_point(self):
        # Jamie's live case: his race target is explicitly not a training minimum, and his
        # bike capacity is separately recorded as proven, so an under-fuelling nudge built
        # from a Z2 training average must not fire.
        rules = ("[perm] Race fuelling = 90 g/hr race-day target (NOT a training minimum - "
                 "do not flag or compare easy/Z2 nutrition to it). "
                 "[perm] Fuelling habit: flag race-day nutrition risk when relevant.")
        sel, sup = races.derive_focus({}, rules, avg_g_hr=40, race_target_g_hr=90)
        assert all(f["id"] != "fuel_take_it_on" for f in sel)
        assert any(s["id"] == "fuel_take_it_on" and "standing rule forbids" in s["reason"]
                   for s in sup)

    def test_the_same_fuel_point_fires_when_no_rule_forbids_it(self):
        rules = "[perm] Fuelling habit: 0 g carbs/hour on rides - flag race-day nutrition risk when relevant."
        sel, _ = races.derive_focus({}, rules, avg_g_hr=10, race_target_g_hr=70)
        assert any(f["id"] == "fuel_take_it_on" for f in sel)

    def test_fuel_point_does_not_fire_when_intake_is_already_near_target(self):
        rules = "[perm] flag race-day nutrition risk when relevant."
        sel, _ = races.derive_focus({}, rules, avg_g_hr=68, race_target_g_hr=70)
        assert all(f["id"] != "fuel_take_it_on" for f in sel)

    def test_capped_at_three_with_the_overflow_recorded(self):
        prof = {"prev_race": {"notes": "fade; walk-breaks; lost the race number in transition",
                              "t1t2_time": "10:00"},
                "race_targets": {"t1t2_time": "5:30"}}
        rules = ("[perm] do not front-load all caffeine before the swim. "
                 "[perm] anchor race-day bike pacing to a HR ceiling. "
                 "[perm] the open focus is run fuelling.")
        sel, sup = races.derive_focus(prof, rules)
        assert len(sel) == 3
        assert any("three-point cap" in s["reason"] for s in sup)

    def test_ranked_by_what_it_cost_not_by_catalogue_order(self):
        prof = {"prev_race": {"notes": "fade", "t1t2_time": "10:00"},
                "race_targets": {"t1t2_time": "5:30"}}
        sel, _ = races.derive_focus(prof, "")
        assert [f["id"] for f in sel] == ["pace_first_half", "transitions"]

    def test_transition_target_written_as_bare_minutes_is_read(self):
        # "~5 min" vs "≤5:30": the two athletes write this field differently by hand.
        assert races._mmss_seconds("~5 min") == 300
        assert races._mmss_seconds("≤5:30") == 330
        assert races._mmss_seconds("no idea") is None

    def test_transitions_does_not_fire_when_the_past_race_beat_the_target(self):
        prof = {"prev_race": {"t1t2_time": "4:00"}, "race_targets": {"t1t2_time": "5:30"}}
        sel, _ = races.derive_focus(prof, "")
        assert all(f["id"] != "transitions" for f in sel)


class TestCuratedOverride:
    def test_curated_race_focus_wins_over_derivation(self):
        prof = {"race_focus": ["swim straight", "eat early"],
                "prev_race": {"notes": "fade"}}
        assert races.focus_for(prof, "") == ["swim straight", "eat early"]

    def test_derivation_used_when_nothing_is_curated(self):
        prof = {"prev_race": {"notes": "fade"}}
        assert races.focus_for(prof, "") == ["go out easier on the bike than feels right"]

    def test_curated_list_is_also_capped_at_three(self):
        prof = {"race_focus": ["a", "b", "c", "d"]}
        assert len(races.focus_for(prof, "")) == 3
