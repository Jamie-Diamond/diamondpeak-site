"""Weekly-hours REPLY capture — the bot half of docs/weekly-hours-capture.md.

The ask has gone out on the Sunday card since fb3eb7f; nothing read the answer, so a
declaration had to be hand-written. These tests cover the detector/parser pair that
reads it, the two-tier safety rule that keeps an ambiguous number from silently becoming
a training ceiling, and the three fallback properties a no-reply must still have.
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import weekly_availability as wa          # noqa: E402
import races                              # noqa: E402

MON = date(2026, 8, 3)


class TestTierOneUnambiguous:
    """Messages that frame themselves as being about a week: safe to write on sight."""

    @pytest.mark.parametrize("msg,hours", [
        ("I've got about 14 hours this week", 14.0),
        ("14h next week", 14.0),
        ("20, big week", 20.0),
        ("I've got about 14 this week", 14.0),
        ("17.5 hours next week", 17.5),
        ("12 hours this week, nothing long Mon-Thu", 12.0),
    ])
    def test_declaration_detected_and_parsed(self, msg, hours):
        assert wa.looks_like_hours_declaration(msg), msg
        assert wa.parse_hours_message(msg)["hours"] == hours

    def test_range_takes_the_lower_bound(self):
        # Hedged range: building to the top of it would overshoot a week the athlete
        # was already unsure about.
        p = wa.parse_hours_message("maybe 12-13 hours next week")
        assert p["hours"] == 12.0

    def test_range_does_not_leave_a_stranded_unit_as_a_constraint(self):
        # The range regex removes "12-13" and used to leave the word "hours" behind,
        # which was then stored as the athlete's constraint prose.
        assert wa.parse_hours_message("maybe 12-13 hours next week")["constraints"] == ""

    def test_constraints_are_captured_verbatim(self):
        # Free prose reaches the Stage-1 planner as written, so it must not be paraphrased.
        p = wa.parse_hours_message("12 hours this week, nothing long Mon-Thu")
        assert p["constraints"] == "nothing long Mon-Thu"

    def test_the_coach_phrasing_from_the_ticket(self):
        p = wa.parse_hours_message("17.5 hours next week, away Thu-Fri, nothing long Mon-Thu")
        assert p["hours"] == 17.5
        assert "away Thu-Fri" in p["constraints"]
        assert "nothing long Mon-Thu" in p["constraints"]


class TestTierTwoNeedsConfirmation:
    """A figure with no weekly framing may be OFFERED to the athlete, never written."""

    @pytest.mark.parametrize("msg", ["12 max, nothing long midweek", "14", "about 14", "14h"])
    def test_reply_tier_but_not_declaration_tier(self, msg):
        assert wa.looks_like_hours_reply(msg), msg
        assert not wa.looks_like_hours_declaration(msg), msg

    def test_midweek_is_a_constraint_not_weekly_framing(self):
        # `\bweek\b` must not match inside "midweek", or "nothing long midweek" would be
        # read as self-framing and written without a confirmation tap.
        p = wa.parse_hours_message("12 max, nothing long midweek")
        assert p["framed"] is False
        assert p["constraints"] == "nothing long midweek"


class TestRefusesRatherThanGuessing:
    """The failure this whole module exists to prevent is a number the athlete never
    said quietly deciding how hard they train."""

    @pytest.mark.parametrize("msg", [
        "How many hours should I do next week?",
        "how many hours do I need",
        "what should next week look like",
        "should I do 14 hours next week?",
    ])
    def test_a_question_is_never_a_declaration(self, msg):
        # The lesson races.looks_like_race_statement learned: "Am I racing on Saturday?"
        # became a race called "?".
        assert not wa.looks_like_hours_declaration(msg), msg
        assert not wa.looks_like_hours_reply(msg), msg

    @pytest.mark.parametrize("msg", [
        "I did 14 hours last week",
        "slept 7 hours",
        "managed 12",
        "ran 14 hours this week",
    ])
    def test_a_report_of_hours_done_is_not_hours_available(self, msg):
        # Capping the coming week off the previous one is exactly the silent persistence
        # the dated-declaration design removes.
        assert not wa.looks_like_hours_declaration(msg), msg
        assert not wa.looks_like_hours_reply(msg), msg

    @pytest.mark.parametrize("msg", [
        "I've got a 2 hour ride tomorrow",
        "long run of 3 hours on Saturday",
        "90 min swim session later",
    ])
    def test_one_session_duration_is_not_a_week_budget(self, msg):
        assert not wa.looks_like_hours_declaration(msg), msg
        assert not wa.looks_like_hours_reply(msg), msg

    @pytest.mark.parametrize("msg", ["3", "0", "2"])
    def test_a_bare_low_number_reads_as_a_score_not_a_week(self, msg):
        # The card this ask rides on also asks "Ankle score this morning? (0-10)" and
        # "Injury pain score before heading out? (0-10)" (morning-checkin.py:60-72). A
        # bare 3 becoming a three-hour training week would halve a real ceiling silently.
        assert not wa.looks_like_hours_reply(msg), msg

    def test_the_overlap_band_is_offered_never_written(self):
        # 5-10 genuinely overlaps the pain-score range, so it cannot be resolved by
        # parsing alone. It stays tier 2, which by construction requires a tap.
        assert wa.looks_like_hours_reply("7")
        assert not wa.looks_like_hours_declaration("7")

    def test_out_of_band_figure_is_refused_at_write_time(self):
        with pytest.raises(ValueError):
            wa.record("nobody", MON, hours=99, base="/tmp/wa-does-not-exist")


class TestAskOutstandingGatesTheContextualTier:
    """The contextual tier must rest on a RECORDED send, not on "it is Sunday"."""

    def test_no_recorded_ask_means_not_outstanding(self, tmp_path):
        assert wa.ask_outstanding("jamie", MON, base=tmp_path) is False

    def test_recorded_ask_is_outstanding(self, tmp_path):
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        assert wa.ask_outstanding("jamie", MON, base=tmp_path) is True

    def test_answering_closes_it(self, tmp_path):
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        wa.record("jamie", MON, hours=14, source="telegram-reply", base=tmp_path)
        assert wa.ask_outstanding("jamie", MON, base=tmp_path) is False

    def test_a_stale_ask_expires(self, tmp_path):
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        later = datetime.now() + timedelta(hours=wa._ASK_WINDOW_HOURS + 1)
        assert wa.ask_outstanding("jamie", MON, now=later, base=tmp_path) is False

    def test_recording_an_ask_preserves_existing_declarations(self, tmp_path):
        wa.record("jamie", MON - timedelta(days=7), hours=11, base=tmp_path)
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        assert wa.hours_for_week("jamie", MON - timedelta(days=7), base=tmp_path) == 11.0

    def test_an_ask_never_supplies_hours(self, tmp_path):
        # Recording the SEND must not look like an answer to it.
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        assert wa.hours_for_week("jamie", MON, base=tmp_path) is None
        assert wa.has_declaration("jamie", MON, base=tmp_path) is False


class TestNoteAskSentDoesNotDisturbDayShape:
    """`asks` bookkeeping must not be mistaken for the athlete's day shape."""

    def test_day_shape_is_none_before_and_after_an_ask(self, tmp_path):
        # An EMPTY declarations list is falsy, so a file of pure `asks` bookkeeping used to
        # satisfy every clause of _is_legacy_flat and got handed to
        # session_library.reconcile_day_rules as though it were a day shape — for every
        # athlete, every Sunday, from the moment the ask went out.
        assert wa.day_shape("jamie", MON, base=tmp_path) is None
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        assert wa.day_shape("jamie", MON, base=tmp_path) is None

    def test_a_file_written_by_this_module_is_never_legacy_flat(self, tmp_path):
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        assert wa._is_legacy_flat(wa.load_raw("jamie", base=tmp_path)) is False

    def test_a_genuine_legacy_flat_file_is_still_honoured(self, tmp_path):
        # The pre-existing Phase 5a shape must keep working untouched.
        d = tmp_path / "athletes" / "jamie"
        d.mkdir(parents=True)
        (d / wa.FILENAME).write_text(json.dumps({"unavailable_days": ["Wed"]}))
        assert wa._is_legacy_flat(wa.load_raw("jamie", base=tmp_path)) is True
        assert wa.day_shape("jamie", MON, base=tmp_path) == {"unavailable_days": ["Wed"]}

    def test_a_legacy_file_survives_an_ask_being_recorded(self, tmp_path):
        d = tmp_path / "athletes" / "jamie"
        d.mkdir(parents=True)
        (d / wa.FILENAME).write_text(json.dumps({"unavailable_days": ["Wed"]}))
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        # Carried forward whole rather than discarded — deleting it would silently widen
        # the athlete's week.
        assert wa.load_raw("jamie", base=tmp_path)["legacy_day_shape"] == \
            {"unavailable_days": ["Wed"]}

    def test_a_declaration_still_supplies_its_own_day_shape(self, tmp_path):
        wa.record("jamie", MON, hours=14, unavailable_days=["Thu"], base=tmp_path)
        assert wa.day_shape("jamie", MON, base=tmp_path) == {"unavailable_days": ["Thu"]}


class TestTargetWeekIsTheWeekThatWasASKED:
    """Which week a reply belongs to. Read, not recomputed from today's date."""

    SUN = date(2026, 8, 2)          # the Sunday the 18:00 build runs
    WED = date(2026, 8, 5)

    def test_on_sunday_it_is_tomorrows_week(self, tmp_path):
        assert wa.target_week("jamie", "14h", today=self.SUN, base=tmp_path) == MON

    def test_an_outstanding_ask_wins_over_the_calendar(self, tmp_path):
        # The bug this fixes: an athlete answering MONDAY morning is inside the documented
        # answer window, but "the Monday after today" is then the FOLLOWING week, so the
        # figure landed on a week nobody asked about while the week they were asked about
        # stayed on the config fallback.
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        assert wa.target_week("jamie", "14", today=MON, base=tmp_path) == MON
        assert wa.target_week("jamie", "14", today=date(2026, 8, 4), base=tmp_path) == MON

    def test_without_an_ask_a_monday_reply_targets_the_next_week(self, tmp_path):
        assert wa.target_week("jamie", "14h", today=MON,
                              base=tmp_path) == MON + timedelta(days=7)

    def test_this_week_midweek_means_the_week_in_progress(self, tmp_path):
        assert wa.target_week("jamie", "14 hours this week", today=self.WED,
                              base=tmp_path) == MON

    def test_next_week_midweek_means_the_following_week(self, tmp_path):
        assert wa.target_week("jamie", "14 hours next week", today=self.WED,
                              base=tmp_path) == MON + timedelta(days=7)

    def test_an_answered_ask_no_longer_steers_the_week(self, tmp_path):
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        wa.record("jamie", MON, hours=14, base=tmp_path)
        assert wa.target_week("jamie", "16h", today=MON,
                              base=tmp_path) == MON + timedelta(days=7)

    def test_a_stale_ask_no_longer_steers_the_week(self, tmp_path):
        wa.note_ask_sent("jamie", MON, base=tmp_path)
        stale = datetime.now() + timedelta(hours=wa._ASK_WINDOW_HOURS + 1)
        assert wa.target_week("jamie", "14", today=MON, now=stale,
                              base=tmp_path) == MON + timedelta(days=7)

    def test_the_already_built_check_is_keyed_on_the_TARGET_week(self):
        # Loaded by path because telegram/bot.py is not importable in the test env (it
        # opens network/voice deps at import). The invariant under test is that the check
        # takes the target week as an argument at all: keyed only on "is it Sunday
        # evening", a mid-week "14 hours this week" — which target_week resolves to the
        # CURRENT Monday, a week built the previous Sunday — would be answered with
        # "I build this evening" about a week that already exists.
        bot = (Path(__file__).resolve().parents[2] / "telegram/bot.py").read_text()
        assert "def _hours_week_is_built(week_start, now=None)" in bot
        assert "after_build=_hours_week_is_built(week_start)" in bot

    def test_the_bot_handler_does_not_compute_the_week_itself(self):
        # The handler must go through target_week; a local "Monday after today" is the
        # defect above.
        bot = (Path(__file__).resolve().parents[2] / "telegram/bot.py").read_text()
        assert "weekly_availability.target_week(slug, text)" in bot
        assert "def _next_monday(" not in bot


class TestNoSilentPersistence:
    """(c) of the no-reply contract: a declaration must not leak into the next week."""

    def test_a_declaration_binds_only_the_week_it_names(self, tmp_path):
        wa.record("jamie", MON, hours=17.5, source="telegram-reply", base=tmp_path)
        assert wa.hours_for_week("jamie", MON, base=tmp_path) == 17.5
        assert wa.hours_for_week("jamie", MON + timedelta(days=7), base=tmp_path) is None
        assert wa.hours_for_week("jamie", MON - timedelta(days=7), base=tmp_path) is None

    def test_none_week_start_resolves_to_none(self, tmp_path):
        # macro_projection's ceiling lambda discards its week; one real declaration must
        # not bound every projected week.
        wa.record("jamie", MON, hours=17.5, base=tmp_path)
        assert wa.hours_for_week("jamie", None, base=tmp_path) is None


class TestConfirmationCopy:
    """A mis-parse has to be visible in the read-back, not in next week's plan."""

    def test_states_the_figure(self):
        assert "14" in wa.confirmation(14.0, coaching_level="mid")

    def test_states_the_constraints_as_stored(self):
        msg = wa.confirmation(12.0, "nothing long Mon-Thu", coaching_level="mid")
        assert "nothing long Mon-Thu" in msg

    def test_beginner_copy_carries_no_load_jargon(self):
        # Calum is `beginner`. These are hardcoded strings, so coaching_levels.level_block
        # (which only shapes LLM prompts) cannot reach them.
        msg = wa.confirmation(8.0, coaching_level="beginner")
        low = msg.lower()
        for jargon in ("tss", "load", "if", "ceiling", "zone", "ctl", "atl"):
            assert jargon not in low.replace("i'll", ""), jargon

    def test_pro_copy_may_name_the_ceiling(self):
        assert "ceiling" in wa.confirmation(17.5, coaching_level="pro").lower()

    def test_after_build_offers_a_rebuild_rather_than_promising_one(self):
        # Spec item 5: recording a correction is right; silently rebuilding a week the
        # athlete has already been sent is not.
        late = wa.confirmation(14.0, coaching_level="mid", after_build=True)
        assert "already built" in late.lower()


class TestCrossFireWithOtherCaptures:
    """Only one handler fires per message — each returns True and routing stops. So the
    detectors must not overlap."""

    @pytest.mark.parametrize("msg", [
        "14h next week",
        "12 hours this week, nothing long Mon-Thu",
        "20, big week",
    ])
    def test_an_hours_declaration_is_not_read_as_a_race(self, msg):
        assert not races.looks_like_race_statement(msg, MON), msg

    @pytest.mark.parametrize("msg", [
        "I'm racing Dorney on Saturday",
        "I'm racing the Outlaw on 26 July as an A race",
    ])
    def test_a_race_statement_is_not_read_as_hours(self, msg):
        assert not wa.looks_like_hours_declaration(msg), msg
        assert not wa.looks_like_hours_reply(msg), msg

    def test_a_constraint_naming_days_does_not_need_a_day_override(self):
        # "nothing long Mon-Thu" is a CONSTRAINT on an hours declaration. It reaches the
        # planner as prose; it must not be mistaken for a directed-day instruction.
        p = wa.parse_hours_message("I've got 14 hours, nothing long Mon-Thu this week")
        assert p["hours"] == 14.0
        assert "Mon-Thu" in p["constraints"]
