"""Tests for lib/acknowledgement.py — the five §8.3 milestone triggers.

These exist to protect ONE property: every trigger fails closed. False praise is
worse than silence — congratulating an athlete on a week they missed, or calling a
ride their longest when it was not, discredits every other thing the coach says.
Each gate below is asserted directly, so a later edit cannot quietly remove one.

Nothing here touches a real athlete's files; every case uses tmp_path or a
literal feed.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import acknowledgement as ack        # noqa: E402

WEEK_END = date(2026, 8, 2)         # a Sunday

# A cfg with a defensible CTL basis, so required_tss returns week types.
CFG = {"plan_start": "2026-01-05", "race_date": "2026-09-19",
       "ctl_targets": {"phase_ctl": {"base": 70, "build": 90,
                                     "specific": 105, "peak": 110}},
       "phase_weeks": {"base": 12, "build": 8, "specific": 8, "peak": 3}}


def _boundary(level="pro", **kw):
    """First week end where a build-type week is followed by a step-down."""
    probe = date(2026, 1, 11)
    while probe < date(2026, 9, 20):
        hit = ack.evaluate_block_finished(CFG, 100.0, probe, compliance_pct=96,
                                          coaching_level=level, fired={}, **kw)
        if hit:
            return probe, hit
        probe += timedelta(days=7)
    raise AssertionError("no build->step-down boundary found in the plan")


def _feed(days_back, dates, sport="Ride", distance=50000.0, start_id=0):
    """A minimal ICU-shaped history feed. `days_back` seeds the earliest date so
    the span/coverage gates can be exercised."""
    out = [{"id": f"a{start_id}", "type": sport, "distance": distance,
            "start_date_local": f"{(date(2026, 8, 2) - timedelta(days=days_back)).isoformat()}T07:00:00"}]
    for i, d in enumerate(dates):
        out.append({"id": f"b{start_id}{i}", "type": sport, "distance": distance,
                    "start_date_local": f"{d}T07:00:00"})
    return out


class TestBlockFinished:
    def test_fires_on_a_build_to_stepdown_boundary(self):
        probe, hit = _boundary()
        assert hit["trigger"] == "block_finished"
        assert hit["key"] == f"block:{probe.isoformat()}"
        assert "Next week steps down." in hit["text"]

    def test_silent_when_the_week_missed_its_target(self):
        """W3 / §8.4 — praise on a week that missed reads as sarcasm."""
        probe, _ = _boundary()
        assert ack.evaluate_block_finished(CFG, 100.0, probe, compliance_pct=62,
                                          coaching_level="pro", fired={}) is None

    def test_silent_when_compliance_is_unknown(self):
        probe, _ = _boundary()
        assert ack.evaluate_block_finished(CFG, 100.0, probe, compliance_pct=None,
                                           coaching_level="pro", fired={}) is None

    def test_silent_without_a_defensible_ctl_basis(self):
        """required_tss returns {"error": ...} — no fabricated target, no praise."""
        probe, _ = _boundary()
        assert ack.evaluate_block_finished({}, 100.0, probe, compliance_pct=96,
                                           coaching_level="pro", fired={}) is None

    def test_silent_without_ctl(self):
        probe, _ = _boundary()
        assert ack.evaluate_block_finished(CFG, None, probe, compliance_pct=96,
                                           coaching_level="pro", fired={}) is None

    def test_silent_once_the_occurrence_is_marked(self):
        probe, hit = _boundary()
        assert ack.evaluate_block_finished(
            CFG, 100.0, probe, compliance_pct=96, coaching_level="pro",
            fired={hit["key"]: "2026-07-29"}) is None

    def test_beginner_gets_no_training_metrics(self):
        """Calum is `beginner`: coaching_levels.py bans Load and Fitness there, so
        §8.3's own example sentence cannot go to him as written."""
        _, hit = _boundary(level="beginner", block_load=2340, fitness_delta=11)
        assert not ack._BEGINNER_BANNED_RE.search(hit["text"])
        assert "Load" not in hit["text"] and "Fitness" not in hit["text"]

    def test_pro_carries_the_numbers_when_supplied(self):
        _, hit = _boundary(level="pro", block_load=2340, fitness_delta=11)
        assert "2,340 Load" in hit["text"]
        assert "Fitness up 11 points" in hit["text"]

    def test_numbers_are_omitted_not_invented_when_absent(self):
        _, hit = _boundary(level="pro")
        assert "Load" not in hit["text"]


class TestStreak:
    def _weeks(self, *pcts):
        return {(WEEK_END - timedelta(days=7 * (i + 1))).isoformat(): p
                for i, p in enumerate(pcts)}

    def test_fires_on_three_consecutive_weeks(self):
        hit = ack.evaluate_streak(WEEK_END, 97, weeks=self._weeks(94, 91),
                                  coaching_level="pro", fired={})
        assert hit and hit["trigger"] == "streak"
        assert hit["text"].startswith("Third week running")
        assert "91%" in hit["text"]        # the LOWEST of the three, not the best

    def test_silent_on_two_weeks(self):
        assert ack.evaluate_streak(WEEK_END, 97, weeks=self._weeks(94),
                                   coaching_level="pro", fired={}) is None

    def test_a_missing_week_breaks_it_rather_than_being_read_over(self):
        """An unrecorded week is unknown, and an unknown week cannot support a
        claim about three consecutive ones."""
        weeks = {(WEEK_END - timedelta(days=14)).isoformat(): 91,
                 (WEEK_END - timedelta(days=21)).isoformat(): 95}
        assert ack.evaluate_streak(WEEK_END, 97, weeks=weeks,
                                   coaching_level="pro", fired={}) is None

    def test_a_week_with_no_plan_breaks_it(self):
        weeks = self._weeks(None, 91)
        assert ack.evaluate_streak(WEEK_END, 97, weeks=weeks,
                                   coaching_level="pro", fired={}) is None

    def test_silent_when_this_week_is_under_the_threshold(self):
        assert ack.evaluate_streak(WEEK_END, 84, weeks=self._weeks(94, 91),
                                   coaching_level="pro", fired={}) is None

    def test_extending_a_streak_does_not_re_fire(self):
        """Key is the streak's START, so week four carries week three's key."""
        three = ack.evaluate_streak(
            WEEK_END - timedelta(days=7), 93,
            weeks={(WEEK_END - timedelta(days=14)).isoformat(): 94,
                   (WEEK_END - timedelta(days=21)).isoformat(): 91},
            coaching_level="pro", fired={})
        assert three
        four = ack.evaluate_streak(
            WEEK_END, 97,
            weeks={(WEEK_END - timedelta(days=7)).isoformat(): 93,
                   (WEEK_END - timedelta(days=14)).isoformat(): 94,
                   (WEEK_END - timedelta(days=21)).isoformat(): 91},
            coaching_level="pro", fired={three["key"]: "2026-07-26"})
        assert four is None

    def test_beginner_wording_avoids_the_percentage(self):
        hit = ack.evaluate_streak(WEEK_END, 97, weeks=self._weeks(94, 91),
                                  coaching_level="beginner", fired={})
        assert hit and "%" not in hit["text"]


class TestFirstAtDistance:
    def _hist(self, n=12, distance=50000.0, span_days=400):
        dates = [(date(2026, 8, 2) - timedelta(days=span_days - i * 20)).isoformat()
                 for i in range(n)]
        return [{"id": f"h{i}", "type": "Ride", "distance": distance,
                 "start_date_local": f"{d}T07:00:00"} for i, d in enumerate(dates)]

    def _act(self, distance=80000.0):
        return {"id": "new", "type": "Ride", "distance": distance,
                "start_date_local": "2026-08-02T07:00:00"}

    def test_fires_on_a_genuine_longest(self):
        hit = ack.evaluate_first_at_distance(self._act(), self._hist(),
                                             coaching_level="pro", fired={})
        assert hit and hit["trigger"] == "first_at_distance"
        assert "Longest ride" in hit["text"]
        assert "80.0 km" in hit["text"]

    def test_span_is_named_from_the_feed_not_from_the_request(self):
        """`get_training_history(365)` is a ROLLING window that may return three
        months, so "this year" would be a false claim. 150 days must not read as
        twelve months."""
        hit = ack.evaluate_first_at_distance(self._act(), self._hist(span_days=150),
                                             coaching_level="pro", fired={})
        assert hit and "twelve months" not in hit["text"]
        assert "months" in hit["text"]

    def test_silent_on_too_short_a_span(self):
        assert ack.evaluate_first_at_distance(
            self._act(), self._hist(span_days=60),
            coaching_level="pro", fired={}) is None

    def test_silent_on_too_shallow_a_history(self):
        assert ack.evaluate_first_at_distance(
            self._act(), self._hist(n=4), coaching_level="pro", fired={}) is None

    def test_one_unknown_distance_means_an_unknown_maximum(self):
        h = self._hist()
        h[3]["distance"] = None
        assert ack.evaluate_first_at_distance(self._act(), h,
                                              coaching_level="pro", fired={}) is None

    def test_silent_inside_the_margin(self):
        assert ack.evaluate_first_at_distance(
            self._act(distance=50000.0 * 1.005), self._hist(),
            coaching_level="pro", fired={}) is None

    def test_silent_for_a_sport_the_trigger_does_not_cover(self):
        act = dict(self._act())
        act["type"] = "Golf"
        assert ack.evaluate_first_at_distance(act, self._hist(),
                                              coaching_level="pro", fired={}) is None

    def test_silent_without_a_distance(self):
        act = dict(self._act())
        act["distance"] = None
        assert ack.evaluate_first_at_distance(act, self._hist(),
                                              coaching_level="pro", fired={}) is None

    def test_silent_on_an_empty_feed(self):
        assert ack.evaluate_first_at_distance(self._act(), [],
                                              coaching_level="pro", fired={}) is None

    def test_does_not_re_fire_for_the_same_distance(self):
        hit = ack.evaluate_first_at_distance(self._act(), self._hist(),
                                             coaching_level="pro", fired={})
        assert ack.evaluate_first_at_distance(
            self._act(), self._hist(), coaching_level="pro",
            fired={hit["key"]: "2026-08-02"}) is None

    def test_swim_renders_in_metres(self):
        act = {"id": "s", "type": "Swim", "distance": 5400.0,
               "start_date_local": "2026-08-02T07:00:00"}
        hist = [{"id": f"s{i}", "type": "Swim", "distance": 2000.0,
                 "start_date_local":
                     f"{(date(2026, 8, 2) - timedelta(days=400 - i * 20)).isoformat()}T07:00:00"}
                for i in range(12)]
        hit = ack.evaluate_first_at_distance(act, hist, coaching_level="mid", fired={})
        assert hit and "5400 m" in hit["text"]


class TestComeback:
    def _act(self, d="2026-08-02"):
        return {"id": "new", "type": "Run", "distance": 10000.0,
                "start_date_local": f"{d}T07:00:00"}

    def _hist(self, dates, earliest_days=60):
        out = [{"id": "old", "type": "Run", "distance": 10000.0,
                "start_date_local":
                    f"{(date(2026, 8, 2) - timedelta(days=earliest_days)).isoformat()}T07:00:00"}]
        for i, d in enumerate(dates):
            out.append({"id": f"p{i}", "type": "Run", "distance": 10000.0,
                        "start_date_local": f"{d}T07:00:00"})
        return out

    def test_fires_after_a_real_gap(self):
        hit = ack.evaluate_comeback(self._act(), self._hist(["2026-07-24"]), fired={})
        assert hit and hit["trigger"] == "comeback"
        assert "9 days" in hit["text"]
        assert hit["key"] == "comeback:2026-08-02"

    def test_silent_below_the_threshold(self):
        assert ack.evaluate_comeback(self._act(),
                                     self._hist(["2026-07-30"]), fired={}) is None

    def test_silent_when_the_feed_cannot_prove_the_gap(self):
        """A feed that starts inside the claimed gap is indistinguishable from a
        real break — the single most dangerous false positive here."""
        short = [{"id": "p", "type": "Run", "distance": 10000.0,
                  "start_date_local": "2026-07-31T07:00:00"}]
        assert ack.evaluate_comeback(self._act(), short, fired={}) is None

    def test_silent_without_any_prior_activity(self):
        assert ack.evaluate_comeback(self._act(), [self._act()], fired={}) is None

    def test_silent_on_an_empty_feed(self):
        assert ack.evaluate_comeback(self._act(), [], fired={}) is None

    def test_does_not_fire_twice_for_the_same_return(self):
        h = self._hist(["2026-07-24"])
        assert ack.evaluate_comeback(
            self._act(), h, fired={"comeback:2026-08-02": "2026-08-02"}) is None

    def test_a_second_session_in_the_same_week_is_not_a_second_comeback(self):
        """The key is the gap-end date, so only the FIRST session back carries it."""
        first = ack.evaluate_comeback(self._act("2026-08-01"),
                                      self._hist(["2026-07-24"]), fired={})
        assert first
        second = ack.evaluate_comeback(
            self._act("2026-08-02"),
            self._hist(["2026-07-24", "2026-08-01"]), fired={first["key"]: "x"})
        assert second is None


class TestTrainedWhileIll:
    def _dir(self, tmp_path, **flag):
        (tmp_path / "current-state.json").write_text(json.dumps({"illness": flag}))
        return tmp_path

    def _act(self, mins=76, d="2026-07-29"):
        return {"id": "i1", "type": "Ride", "distance": 30000.0,
                "start_date_local": f"{d}T07:00:00", "moving_time": mins * 60}

    def test_fires_while_the_flag_is_active(self, tmp_path):
        adir = self._dir(tmp_path, condition="tonsillitis", status="active",
                         started="2026-07-26")
        hit = ack.evaluate_trained_while_ill(adir, self._act(), fired={})
        assert hit and hit["trigger"] == "trained_while_ill"
        assert hit["text"].startswith("76 minutes done")

    def test_the_sentence_carries_no_illness_handle(self, tmp_path):
        """illness._label() renders 'tonsillitis (active, day 4)' — a log handle.
        Dropped into a sentence it produces 'done while tonsillitis (active,
        day 4)', so the text is composed from the canonical fields instead."""
        adir = self._dir(tmp_path, condition="tonsillitis", status="active",
                         started="2026-07-26")
        hit = ack.evaluate_trained_while_ill(adir, self._act(), fired={})
        assert hit and "(" not in hit["text"]

    def test_recovering_reads_differently_from_active(self, tmp_path):
        adir = self._dir(tmp_path, condition="tonsillitis", status="recovering",
                         started="2026-07-26")
        hit = ack.evaluate_trained_while_ill(adir, self._act(), fired={})
        assert hit and "recovering" in hit["text"]

    def test_silent_with_no_flag(self, tmp_path):
        (tmp_path / "current-state.json").write_text(json.dumps({"last_updated": "x"}))
        assert ack.evaluate_trained_while_ill(tmp_path, self._act(), fired={}) is None

    def test_silent_when_the_session_predates_the_flag(self, tmp_path):
        adir = self._dir(tmp_path, condition="tonsillitis", status="active",
                         started="2026-07-26")
        assert ack.evaluate_trained_while_ill(
            adir, self._act(d="2026-07-01"), fired={}) is None

    def test_silent_with_no_duration(self, tmp_path):
        adir = self._dir(tmp_path, condition="tonsillitis", status="active",
                         started="2026-07-26")
        assert ack.evaluate_trained_while_ill(adir, self._act(mins=0), fired={}) is None

    def test_does_not_re_fire_for_the_same_session(self, tmp_path):
        adir = self._dir(tmp_path, condition="tonsillitis", status="active",
                         started="2026-07-26")
        assert ack.evaluate_trained_while_ill(
            adir, self._act(), fired={"ill:i1": "2026-07-29"}) is None


class TestSurfaces:
    def test_weekly_block_with_no_hits_forbids_inventing_one(self):
        out = ack.weekly_block([])
        assert "NO milestone fired" in out
        assert "Do NOT claim a streak" in out

    def test_weekly_block_says_the_finding_still_lands(self):
        """§8.5 / W5 — warmth never buys softness."""
        _, hit = _boundary()
        out = ack.weekly_block([hit])
        assert hit["text"] in out
        assert "the finding still lands in full" in out

    def test_activity_prefix_takes_at_most_one(self):
        hits = [{"trigger": "comeback", "key": "c", "text": "A."},
                {"trigger": "first_at_distance", "key": "f", "text": "B."}]
        out = ack.activity_prefix(hits)
        assert out.startswith("A.")
        assert "B." not in out

    def test_activity_prefix_is_empty_with_no_hits(self):
        assert ack.activity_prefix([]) == ""

    def test_prompt_note_forbids_a_second_acknowledgement(self):
        assert "DO NOT WRITE YOUR OWN" in ack.PROMPT_NOTE
        assert "never replaces or softens a finding" in ack.PROMPT_NOTE

    def test_register_filter_drops_a_metric_sentence_for_beginner(self):
        bad = [{"trigger": "block_finished", "key": "k",
                "text": "That's the build block done — 2,340 Load."}]
        assert ack.filter_register(bad, "beginner") == []
        assert len(ack.filter_register(bad, "pro")) == 1


class TestState:
    def test_missing_store_reads_as_empty(self, tmp_path):
        st = ack.load_state_from_dir(tmp_path)
        assert st == {"fired": {}, "weeks": {}}

    def test_corrupt_store_reads_as_empty(self, tmp_path):
        (tmp_path / ack.STATE_FILENAME).write_text("{not json")
        assert ack.load_state_from_dir(tmp_path) == {"fired": {}, "weeks": {}}

    def test_mark_fired_is_idempotent(self, tmp_path):
        ack.mark_fired_in(tmp_path, ["k1"], date(2026, 7, 29))
        ack.mark_fired_in(tmp_path, ["k1"], date(2026, 8, 5))
        st = ack.load_state_from_dir(tmp_path)
        assert st["fired"] == {"k1": "2026-07-29"}      # original date kept

    def test_record_week_stores_none_for_a_week_with_no_plan(self, tmp_path):
        ack.record_week_in(tmp_path, WEEK_END, None)
        assert ack.load_state_from_dir(tmp_path)["weeks"][WEEK_END.isoformat()] is None

    def test_the_record_does_not_accumulate(self, tmp_path):
        for i in range(ack._MAX_FIRED_KEYS + 40):
            ack.mark_fired_in(tmp_path, [f"k{i:04d}"],
                              date(2026, 1, 1) + timedelta(days=i))
        for i in range(ack._MAX_WEEKS + 10):
            ack.record_week_in(tmp_path, WEEK_END - timedelta(days=7 * i), 95)
        st = ack.load_state_from_dir(tmp_path)
        assert len(st["fired"]) <= ack._MAX_FIRED_KEYS
        assert len(st["weeks"]) <= ack._MAX_WEEKS

    def test_evaluation_never_creates_the_store(self, tmp_path):
        """The read-only audit path depends on this: evaluating must not write."""
        (tmp_path / "current-state.json").write_text(json.dumps({}))
        ack.evaluate_activity_from_dir(
            tmp_path, {"id": "x", "type": "Ride", "distance": 1000.0,
                       "start_date_local": "2026-08-02T07:00:00"},
            [], coaching_level="mid", fired={})
        assert not (tmp_path / ack.STATE_FILENAME).exists()


def test_no_trigger_fires_on_empty_inputs():
    """The blanket property: given nothing, say nothing."""
    assert ack.evaluate_block_finished({}, None, WEEK_END, compliance_pct=None,
                                       coaching_level="mid", fired={}) is None
    assert ack.evaluate_streak(WEEK_END, None, weeks={}, coaching_level="mid",
                               fired={}) is None
    assert ack.evaluate_first_at_distance({}, [], coaching_level="mid",
                                          fired={}) is None
    assert ack.evaluate_comeback({}, [], fired={}) is None
