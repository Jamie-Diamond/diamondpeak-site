"""Scope confirmation after a turn that ran to completion (bug #30 part (b), 18 Aug 2026).

Part (a) - test_cancel_turn.py - covers the turn the athlete STOPS. This is the other
half of the same incident. Kathryn, 16 Aug: "2.5 hours". She meant one day. The coach read
it as a week-wide constraint and rebuilt the week, and because nothing went wrong from the
bot's point of view she got no Stop, no diff and no acknowledgement that the scope had been
misread. The turn ran to completion. That is the case here.

Jamie's design constraint, said in as many words: "it needs to be discretional". A list of
ambiguous phrases was rejected - the next unanticipated wording slips through exactly the
way "2.5 hours" did. So the properties worth failing a build over are all about the
ARITHMETIC of scope, never about the wording:

  1. Days the MESSAGE named vs days the DIFF touched. Nothing else decides.
  2. A turn touching zero or one day, with no day named, is the ordinary turn and must be
     observably unchanged. The check exists for a change that SPREADS.
  3. A whole-week request is not overreach, whichever of this bot's existing whole-week
     signals it arrives by.
  4. Silence applies the NARROW default (Jamie: "always default to the narrower,
     non-bespoke scope"), so the card must SAY that, and the auto-apply must go through the
     same _undo_worker a tap does - there is one writer to that calendar or there is none.
  5. The stale-snapshot defence. A "before" from an earlier turn would make the PREVIOUS
     turn's legitimate push look like this turn's overreach, and here that is not a false
     accusation, it is a timer deleting a session the athlete asked for.
  6. This path and the cancellation path are mutually exclusive for a given turn.

Fake Telegram transport, fake ICU client, no subprocess, no socket, and no timer left
running past the test that armed it.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "telegram"))
sys.path.insert(0, str(REPO / "lib"))
import bot                # noqa: E402
import day_overrides      # noqa: E402
import weekly_availability  # noqa: E402
import write_verify       # noqa: E402

BOT_SRC = (REPO / "telegram" / "bot.py").read_text()

CHAT = "4242"
SLUG = "kathryn"

# A fixed Monday, so "Thursday" and "Friday" mean the same thing every time this runs.
MON = date(2026, 8, 17)
TUE, WED, THU, FRI = (MON + timedelta(days=n) for n in (1, 2, 3, 4))


class FakeTelegram:
    """Records every outbound call instead of making it. Same shape as
    test_cancel_turn.FakeTelegram - deliberately, so the two suites read alike."""

    def __init__(self, message_id=901):
        self.posts = []
        self.sends = []
        self.message_id = message_id

    def post(self, token, method, payload):
        self.posts.append((method, payload))
        if method == "sendMessage":
            return {"result": {"message_id": self.message_id}}
        return {"result": True}

    def send(self, token, chat_id, text, parse_mode="Markdown", reply_markup=None,
             disable_notification=False):
        self.sends.append((text, reply_markup))
        return self.message_id + 1

    def install(self, monkeypatch):
        monkeypatch.setattr(bot, "tg_post", self.post)
        monkeypatch.setattr(bot, "send", self.send)
        monkeypatch.setattr(bot, "log", lambda msg: None)
        return self

    def edits(self):
        return [p for m, p in self.posts if m == "editMessageText"]

    def sent_text(self):
        return "\n".join(t for t, _ in self.sends)

    def cards(self):
        return [(t, kb) for t, kb in self.sends if kb and kb.get("inline_keyboard")
                and any(b.get("callback_data", "").startswith(("undo:", "keep:"))
                        for row in kb["inline_keyboard"] for b in row)]


class FakeIcu:
    def __init__(self, fail_on=()):
        self.deleted = []
        self.pushed = []
        self.fail_on = fail_on

    def delete_workout(self, event_id):
        if "delete" in self.fail_on:
            raise RuntimeError("icu said no")
        self.deleted.append(str(event_id))

    def push_workout(self, sport, event_date, name, description="", description_raw="",
                     planned_training_load=0, **kwargs):
        if "push" in self.fail_on:
            raise RuntimeError("icu said no")
        self.pushed.append({"sport": sport, "event_date": event_date, "name": name})
        return {"id": 999}


def ev(eid, day, name="Endurance ride", etype="Ride", load=60, minutes=90, **extra):
    d = day.isoformat() if hasattr(day, "isoformat") else day
    e = {"id": eid, "start_date_local": f"{d}T00:00:00", "type": etype,
         "name": name, "icu_training_load": load, "moving_time": minutes * 60}
    e.update(extra)
    return e


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Module-level registries, poked directly by this suite. A leaked pending undo would
    be popped by the next test's timer; a leaked TIMER would fire mid-suite and mutate
    another test's FakeIcu, which is the flake nobody would ever reproduce."""
    for t in list(bot._SCOPE_TIMERS.values()):
        t.cancel()
    bot._SCOPE_TIMERS.clear()
    bot._PENDING_UNDO.clear()
    bot._EVENTS_SNAPSHOT.clear()
    yield
    for t in list(bot._SCOPE_TIMERS.values()):
        t.cancel()
        t.join(timeout=2)
    bot._SCOPE_TIMERS.clear()
    bot._PENDING_UNDO.clear()
    bot._EVENTS_SNAPSHOT.clear()


def _check(monkeypatch, message, before, after, tg=None, today=MON):
    """Run the completed-turn scope check with a usable before and a scripted after."""
    tg = tg or FakeTelegram().install(monkeypatch)
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    monkeypatch.setattr(bot, "_authorised_dates",
                        lambda text, t=None: day_overrides.named_dates(text, today))
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: after)
    bot._check_reply_scope("tok", CHAT, SLUG, message, before)
    return tg


# ---------------------------------------------------------------------------
# 1. Reading the message: which days did it name?
# ---------------------------------------------------------------------------

def test_named_dates_reads_one_day_the_directed_day_parser_refuses():
    """The literal incident message. parse_directed_day refuses it ("no sport named"), so
    reusing that would have read "2.5 hours on Thursday" as authorising nothing at all -
    and a turn touching only Thursday would then have been carded and auto-undone."""
    assert day_overrides.parse_directed_day("2.5 hours on Thursday", MON)["date"] is None
    assert day_overrides.named_dates("2.5 hours on Thursday", MON) == {THU.isoformat()}


def test_named_dates_collects_every_day_a_message_names():
    """resolve_directed_date returns None the moment a second weekday appears, because it
    writes a permission. Here that would read "move Wed and Fri" as authorising nothing."""
    assert day_overrides.resolve_directed_date("move Wed and Fri", MON) is None
    assert day_overrides.named_dates("move Wed and Fri", MON) == {
        WED.isoformat(), FRI.isoformat()}


def test_named_dates_takes_both_readings_of_a_weekday_already_gone_by():
    """Said on Thursday, "Tuesday" is either this Tuesday or next. Both, never neither:
    neither would hand a timer permission to delete whatever landed on the day they meant."""
    said_on_thu = MON + timedelta(days=3)
    assert day_overrides.named_dates("shift Tuesday's swim", said_on_thu) == {
        TUE.isoformat(), (TUE + timedelta(days=7)).isoformat()}


def test_named_dates_handles_iso_dates_relative_days_and_next_week():
    assert day_overrides.named_dates("2026-09-01", MON) == {"2026-09-01"}
    assert day_overrides.named_dates("bin tomorrow's run", MON) == {TUE.isoformat()}
    assert day_overrides.named_dates("Thursday next week", MON) == {
        (THU + timedelta(days=7)).isoformat()}
    assert day_overrides.named_dates("", MON) == set()


# ---------------------------------------------------------------------------
# 2. The whole-week signal, from what this bot already recognises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg", [
    "/replan", "replan week", "plan next 2 weeks",
    "I've got 12 hours this week", "big week please",
    "Monday rest Tuesday swim Wednesday run Thursday long ride Friday rest",
    "no cycling this week",
])
def test_a_whole_week_request_is_recognised(msg):
    assert bot._week_scope_signalled(msg) is True


@pytest.mark.parametrize("msg", [
    "2.5 hours", "move Thursday's ride to Friday", "make today easier",
    "12 max, nothing long midweek",
])
def test_a_day_sized_request_is_not_a_week_request(msg):
    assert bot._week_scope_signalled(msg) is False


# ---------------------------------------------------------------------------
# 3. The turns that must stay exactly as they are today
# ---------------------------------------------------------------------------

def test_one_day_changed_matching_the_day_named_says_nothing(monkeypatch):
    before = [ev(1, THU)]
    after = [ev(1, THU, name="Easier ride", load=40)]
    tg = _check(monkeypatch, "make Thursday 2.5 hours", before, after)
    assert tg.sends == [], "the day they named is the day that changed - nothing to ask"


def test_two_days_changed_when_the_message_named_both_says_nothing(monkeypatch):
    """The payoff for named_dates returning a SET. resolve_directed_date gives up on the
    second weekday, so a single-date reading would authorise nothing here - and ten
    minutes later a timer would delete a two-day change she asked for in so many words."""
    tg = _check(monkeypatch, "move Wed and Fri to easy", [], [ev(2, WED), ev(3, FRI)])
    assert tg.sends == []
    assert bot._PENDING_UNDO == {} and bot._SCOPE_TIMERS == {}


def test_one_day_changed_with_no_day_named_says_nothing(monkeypatch):
    """The ordinary turn, and the overwhelming majority of everything that gets here. A
    card would mean auto-undoing a single session the athlete plainly asked for, on nothing
    stronger than a parser that could not find a weekday."""
    tg = _check(monkeypatch, "add a swim please", [], [ev(7, WED)])
    assert tg.sends == []


def test_a_calendar_that_did_not_move_says_nothing(monkeypatch):
    week = [ev(1, TUE), ev(2, THU)]
    tg = _check(monkeypatch, "how am I looking?", week, list(week))
    assert tg.sends == []


def test_an_explicit_whole_week_request_is_never_carded(monkeypatch):
    """And it costs nothing: the week signal is checked before the calendar is read."""
    def _explode(slug):
        pytest.fail("a whole-week request must not even buy the read-back")
    tg = FakeTelegram().install(monkeypatch)
    monkeypatch.setattr(bot, "_read_planned_window", _explode)
    bot._check_reply_scope("tok", CHAT, SLUG, "replan week", [ev(1, TUE)])
    assert tg.sends == []


def test_a_failed_read_back_says_nothing(monkeypatch):
    tg = _check(monkeypatch, "2.5 hours", [ev(1, TUE)], None)
    assert tg.sends == [], "'unknown' has never been grounds for saying anything here"


# ---------------------------------------------------------------------------
# 4. The overreach itself
# ---------------------------------------------------------------------------

def _week_replan_after():
    """What the 16 Aug turn actually did: three days rewritten off one day's remark."""
    return [ev(11, WED, name="Recovery spin", load=30),
            ev(12, THU, name="Short ride", load=35),
            ev(13, FRI, name="Easy run", etype="Run", load=25)]


def test_a_multi_day_change_with_no_day_named_is_carded(monkeypatch):
    """The incident, replayed. "2.5 hours" named no day; three days changed."""
    tg = _check(monkeypatch, "2.5 hours", [], _week_replan_after())
    cards = tg.cards()
    assert len(cards) == 1
    text, kb = cards[0]
    assert text.startswith("Your message didn't name a day, and this changed "
                           "Wed 19 Aug, Thu 20 Aug and Fri 21 Aug:")
    assert "• added Wed 19 Aug: Recovery spin (30 TSS)" in text
    assert "• added Fri 21 Aug: Easy run (25 TSS)" in text
    # The destructive default is stated on the card. Silence is only a fair default if the
    # athlete was told it was one.
    assert "No reply in 10 minutes and I'll undo all of it." in text
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert labels == ["↩️ Undo all of it", "✅ Keep it all"]


def test_a_change_that_spreads_beyond_the_named_day_is_carded(monkeypatch):
    before = [ev(1, THU)]
    after = [ev(1, THU, name="Shorter ride", load=40), ev(2, WED), ev(3, FRI)]
    tg = _check(monkeypatch, "make Thursday 2.5 hours", before, after)
    text, kb = tg.cards()[0]
    assert text.startswith("You asked about Thu 20 Aug. "
                           "This also changed Wed 19 Aug and Fri 21 Aug:")
    assert "No reply in 10 minutes and I'll keep Thu 20 Aug and undo the rest." in text
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert labels == ["↩️ Just Thu 20 Aug", "✅ Keep it all"]
    # Thursday's own edit is IN scope, so it is not in the card and not in the undo.
    assert "Thu 20 Aug" not in text.split(":", 1)[1].split("No reply")[0]


def test_a_change_on_a_different_day_than_the_one_named_is_carded(monkeypatch):
    """One day changed, so the count alone would let this through - but it is not the day
    they named, and "also changed" would be a lie because nothing landed on Thursday."""
    tg = _check(monkeypatch, "2.5 hours on Thursday", [], [ev(9, FRI)])
    text, kb = tg.cards()[0]
    assert text.startswith(
        "You asked about Thu 20 Aug, but nothing changed there. "
        "What did change was Fri 21 Aug:")
    assert "No reply in 10 minutes and I'll undo it and leave Thu 20 Aug as it was." in text
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    # NOT "Just Thu 20 Aug". Nothing landed on Thursday, so that button would be offering
    # to keep nothing at all.
    assert labels == ["↩️ Undo that", "✅ Keep it all"]


def test_only_the_excess_is_parked_for_the_undo(monkeypatch):
    before = [ev(1, THU)]
    after = [ev(1, THU, name="Shorter", load=40), ev(2, WED), ev(3, FRI)]
    _check(monkeypatch, "make Thursday 2.5 hours", before, after)
    assert len(bot._PENDING_UNDO) == 1
    parked = list(bot._PENDING_UNDO.values())[0]["diff"]
    assert [e["id"] for e in parked["created"]] == [2, 3]
    assert parked["deleted"] == [] and parked["edited"] == [], \
        "Thursday was authorised, so Thursday's edit is not the excess"


# ---------------------------------------------------------------------------
# 5. What the undo cannot safely do, it says instead
# ---------------------------------------------------------------------------

def test_an_edit_only_overreach_is_stated_and_not_offered(monkeypatch):
    """_reversible refuses an edit: the snapshot carries five fields, so "restoring" one
    would rewrite those and silently keep whatever else changed. Same refusal here."""
    before = [ev(2, WED), ev(3, FRI)]
    after = [ev(2, WED, name="Recovery spin", load=30),
             ev(3, FRI, name="Easy run", load=25)]
    tg = _check(monkeypatch, "2.5 hours", before, after)
    assert tg.cards() == [], "no button that would do the wrong thing"
    assert bot._PENDING_UNDO == {} and bot._SCOPE_TIMERS == {}
    text = tg.sent_text()
    assert "I can't safely reverse these, so check them yourself:" in text
    assert "No reply in 10 minutes" not in text, "nothing is going to happen on silence"


def test_a_removed_note_is_stated_and_not_offered(monkeypatch):
    """push_workout hardcodes category=WORKOUT, so a deleted NOTE would come back as the
    wrong kind of thing. It is named, never restored."""
    before = [ev(2, WED, category="NOTE"), ev(3, FRI, category="NOTE")]
    tg = _check(monkeypatch, "2.5 hours", before, [])
    assert tg.cards() == []
    assert "I can't safely reverse these" in tg.sent_text()


# ---------------------------------------------------------------------------
# 6. THE STALE SNAPSHOT - the negative control's permanent home
# ---------------------------------------------------------------------------

def test_a_snapshot_from_an_earlier_turn_is_refused():
    turn_started = time.time()
    bot._set_events_snapshot(SLUG, turn_started - 30, [ev(1, TUE)])
    assert bot._turn_before_events(SLUG, turn_started) is None, \
        "a before that predates this turn is missing the PREVIOUS turn's legitimate push"
    bot._set_events_snapshot(SLUG, turn_started + 0.01, [ev(1, TUE)])
    assert bot._turn_before_events(SLUG, turn_started) == [ev(1, TUE)]


def test_a_pre_widening_two_tuple_is_refused():
    bot._EVENTS_SNAPSHOT[SLUG] = (time.time() + 1, frozenset())
    assert bot._turn_before_events(SLUG, time.time()) is None


def test_a_stale_before_suppresses_the_card_entirely(monkeypatch):
    """The negative control, as behaviour rather than as a unit.

    The previous turn legitimately pushed Wed, Thu and Fri because the athlete asked for
    them. THIS turn is a question that writes nothing. prefetch_context hit its 150s cache
    and did not retake the snapshot, so the only "before" on file predates the earlier
    push. Diffed naively that push is three days of overreach, the card offers to undo it,
    and ten minutes later a timer deletes three sessions she asked for.

    Delete the `snap[0] < turn_started` line in _turn_before_events and this test fails.
    """
    tg = FakeTelegram().install(monkeypatch)
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    turn_started = time.time()
    bot._set_events_snapshot(SLUG, turn_started - 120, [])       # before the earlier push
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: _week_replan_after())
    before_events = bot._turn_before_events(SLUG, turn_started)
    bot._check_reply_scope("tok", CHAT, SLUG, "how am I looking?", before_events)
    assert tg.sends == [], "a stale before must buy silence, never a destructive offer"
    assert bot._PENDING_UNDO == {} and bot._SCOPE_TIMERS == {}


# ---------------------------------------------------------------------------
# 7. The two buttons
# ---------------------------------------------------------------------------

def test_the_narrow_button_is_the_existing_undo_pipeline(monkeypatch):
    """Not a parallel implementation. The label changes; the callback, the parked diff and
    the worker are #30(a)'s, so there is exactly one thing that reverses a diff."""
    tg = _check(monkeypatch, "make Thursday 2.5 hours",
                [ev(1, THU)], [ev(1, THU, load=40), ev(2, WED), ev(3, FRI)])
    _text, kb = tg.cards()[0]
    tok = kb["inline_keyboard"][0][0]["callback_data"]
    assert tok.startswith("undo:")

    submitted = []
    monkeypatch.setattr(bot, "_submit",
                        lambda worker, chat_id, *a: submitted.append((worker, a)))
    assert bot._handle_undo("tok", CHAT, tok, 902, {CHAT: {"slug": SLUG}}) is True
    assert submitted and submitted[0][0] is bot._undo_worker
    assert bot._PENDING_UNDO == {}, "single use - the tap consumed it"


def test_the_narrow_tap_deletes_only_the_excess(monkeypatch):
    tg = _check(monkeypatch, "make Thursday 2.5 hours",
                [ev(1, THU)], [ev(1, THU, load=40), ev(2, WED), ev(3, FRI)])
    tok = tg.cards()[0][1]["inline_keyboard"][0][0]["callback_data"][len("undo:"):]
    icu = FakeIcu()
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: [])
    monkeypatch.setattr(bot, "_invalidate_prefetch", lambda slug: None)
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    pending = bot._take_undo(tok, CHAT)
    bot._undo_worker("tok", CHAT, SLUG, pending, None)
    assert sorted(icu.deleted) == ["2", "3"], "Thursday was authorised and must survive"
    assert icu.pushed == []


def test_keep_it_all_is_a_clean_no_op(monkeypatch):
    tg = _check(monkeypatch, "2.5 hours", [], _week_replan_after())
    keep = tg.cards()[0][1]["inline_keyboard"][0][1]["callback_data"]
    assert keep.startswith("keep:")
    monkeypatch.setattr(bot, "_icu_client",
                        lambda slug: pytest.fail("keeping it writes nothing"))
    monkeypatch.setattr(bot, "_submit",
                        lambda *a: pytest.fail("keeping it queues nothing"))
    assert bot._handle_scope_keep("tok", CHAT, keep, 902) is True
    assert bot._PENDING_UNDO == {}, "and the timer must now find nothing to apply"
    assert tg.edits()[-1]["text"] == "✅ Kept as it is."


def test_the_keep_handler_ignores_callbacks_that_are_not_its_own(monkeypatch):
    FakeTelegram().install(monkeypatch)
    assert bot._handle_scope_keep("tok", CHAT, "undo:abc", 902) is False
    assert bot._handle_scope_keep("tok", CHAT, "__SPEAK_LAST__", 902) is False


def test_a_second_tap_on_a_settled_card_is_answered_honestly(monkeypatch):
    tg = _check(monkeypatch, "2.5 hours", [], _week_replan_after())
    keep = tg.cards()[0][1]["inline_keyboard"][0][1]["callback_data"]
    bot._handle_scope_keep("tok", CHAT, keep, 902)
    bot._handle_scope_keep("tok", CHAT, keep, 902)
    assert "already been settled" in tg.edits()[-1]["text"]


# ---------------------------------------------------------------------------
# 8. Silence applies the narrow default
# ---------------------------------------------------------------------------

def test_the_timeout_must_stay_inside_the_pending_undo_ttl():
    """_take_undo refuses an expired token. A timeout at or above the TTL would fire, find
    nothing, and silently leave the wider change standing - the one outcome Jamie ruled
    out. This is the assertion that fails if anybody retunes either number."""
    assert bot._SCOPE_CONFIRM_S < bot._PENDING_UNDO_TTL


def test_no_reply_applies_the_undo_for_the_excess(monkeypatch):
    tg = _check(monkeypatch, "make Thursday 2.5 hours",
                [ev(1, THU)], [ev(1, THU, load=40), ev(2, WED), ev(3, FRI)])
    tok = tg.cards()[0][1]["inline_keyboard"][0][0]["callback_data"][len("undo:"):]
    assert tok in bot._SCOPE_TIMERS

    icu = FakeIcu()
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    # The calendar is still exactly as the card described it.
    monkeypatch.setattr(bot, "_read_planned_window",
                        lambda slug: [ev(1, THU, load=40), ev(2, WED), ev(3, FRI)])
    monkeypatch.setattr(bot, "_invalidate_prefetch", lambda slug: None)
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    ran = []
    monkeypatch.setattr(bot, "_submit",
                        lambda worker, chat_id, *a: (ran.append(worker), worker(*a)))
    monkeypatch.setattr(bot, "_SCOPE_CONFIRM_S", 0.01)

    # Re-arm at the tiny timeout rather than sleeping ten minutes.
    bot._SCOPE_TIMERS.pop(tok).cancel()
    t = bot._arm_scope_auto_undo("tok", CHAT, SLUG, tok, 902)
    t.join(timeout=5)
    assert ran == [bot._undo_worker], \
        "the auto-apply must be the SAME writer a tap uses, not a parallel one"
    assert sorted(icu.deleted) == ["2", "3"]
    assert bot._PENDING_UNDO == {}
    assert "undoing the extra now" in " ".join(e["text"] for e in tg.edits())


def test_the_auto_undo_refuses_when_the_calendar_has_moved(monkeypatch):
    """Ten minutes is long enough for another turn. If the days the card described are no
    longer as it described them, acting is a write nobody asked for."""
    tg = _check(monkeypatch, "2.5 hours", [], _week_replan_after())
    tok = tg.cards()[0][1]["inline_keyboard"][0][0]["callback_data"][len("undo:"):]
    icu = FakeIcu()
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(bot, "_submit",
                        lambda *a: pytest.fail("a moved calendar must not be written to"))
    # Wednesday's session has since been rescaled by a later turn.
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: [
        ev(11, WED, name="Recovery spin", load=95),
        ev(12, THU, name="Short ride", load=35),
        ev(13, FRI, name="Easy run", etype="Run", load=25)])
    monkeypatch.setattr(bot, "_SCOPE_CONFIRM_S", 0.01)
    bot._SCOPE_TIMERS.pop(tok).cancel()
    bot._arm_scope_auto_undo("tok", CHAT, SLUG, tok, 902).join(timeout=5)
    assert icu.deleted == [] and icu.pushed == []
    assert "left it alone" in " ".join(e["text"] for e in tg.edits())


def test_a_read_back_failure_stops_the_auto_undo(monkeypatch):
    tg = _check(monkeypatch, "2.5 hours", [], _week_replan_after())
    tok = tg.cards()[0][1]["inline_keyboard"][0][0]["callback_data"][len("undo:"):]
    monkeypatch.setattr(bot, "_submit",
                        lambda *a: pytest.fail("'unknown' is not permission to write"))
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: None)
    monkeypatch.setattr(bot, "_SCOPE_CONFIRM_S", 0.01)
    bot._SCOPE_TIMERS.pop(tok).cancel()
    bot._arm_scope_auto_undo("tok", CHAT, SLUG, tok, 902).join(timeout=5)
    assert "left it alone" in " ".join(e["text"] for e in tg.edits())


def test_a_tap_during_the_window_wins_and_the_timer_finds_nothing(monkeypatch):
    """The athlete replies before the deadline. The parked diff is single-use and popped
    under _PENDING_UNDO_GUARD, so the timer that wakes afterwards has nothing to apply and
    the undo can never run twice."""
    tg = _check(monkeypatch, "2.5 hours", [], _week_replan_after())
    tok = tg.cards()[0][1]["inline_keyboard"][0][0]["callback_data"][len("undo:"):]
    icu = FakeIcu()
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: _week_replan_after())
    monkeypatch.setattr(bot, "_invalidate_prefetch", lambda slug: None)
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    calls = []
    monkeypatch.setattr(bot, "_submit",
                        lambda worker, chat_id, *a: (calls.append(worker), worker(*a)))
    monkeypatch.setattr(bot, "_SCOPE_CONFIRM_S", 0.15)
    bot._SCOPE_TIMERS.pop(tok).cancel()
    t = bot._arm_scope_auto_undo("tok", CHAT, SLUG, tok, 902)
    assert bot._handle_undo("tok", CHAT, f"undo:{tok}", 902, {CHAT: {"slug": SLUG}}) is True
    t.join(timeout=5)
    assert calls == [bot._undo_worker], "exactly once - the tap, not the tap and the timer"
    assert sorted(icu.deleted) == ["11", "12", "13"]


def test_an_athlete_message_during_the_window_is_serialised_not_raced(monkeypatch):
    """The auto-apply writes through _submit, so it runs under the chat lock and can never
    interleave with the turn the athlete's next message starts. Asserted on the real lock:
    hold it, let the timer fire, and the undo must still be waiting when it is released."""
    tg = _check(monkeypatch, "2.5 hours", [], _week_replan_after())
    tok = tg.cards()[0][1]["inline_keyboard"][0][0]["callback_data"][len("undo:"):]
    icu = FakeIcu()
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: _week_replan_after())
    monkeypatch.setattr(bot, "_invalidate_prefetch", lambda slug: None)
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    monkeypatch.setattr(bot, "_SCOPE_CONFIRM_S", 0.01)
    done = threading.Event()
    real_submit = bot._submit

    def _watched(worker, chat_id, *a):
        def _wrapped(*wa):
            worker(*wa)
            done.set()
        real_submit(_wrapped, chat_id, *a)
    monkeypatch.setattr(bot, "_submit", _watched)

    with bot._chat_lock(CHAT):               # the athlete's next turn is in flight
        bot._SCOPE_TIMERS.pop(tok).cancel()
        bot._arm_scope_auto_undo("tok", CHAT, SLUG, tok, 902).join(timeout=5)
        assert done.wait(timeout=0.5) is False, \
            "the undo must not touch the calendar while a turn holds the chat lock"
        assert icu.deleted == []
    assert done.wait(timeout=5) is True
    assert sorted(icu.deleted) == ["11", "12", "13"]


# ---------------------------------------------------------------------------
# 9. Wiring the behaviour tests cannot see
# ---------------------------------------------------------------------------

def _worker_body():
    body = BOT_SRC[BOT_SRC.index("def _chat_reply_worker("):]
    return body[:body.index("\ndef _image_reply_worker(")]


def test_the_before_is_captured_at_the_prefetch_not_at_report_time():
    """_verify_icu_calendar_claim refreshes _EVENTS_SNAPSHOT after the reply, so reading it
    at report time would diff the AFTER window against itself and silently disable the
    check on every turn that wrote to the calendar."""
    body = _worker_body()
    assert (body.index("before_events = _turn_before_events(slug, turn_started)")
            < body.index("call_claude_streaming("))
    assert body.index("context = prefetch_context(slug)") \
        < body.index("before_events = _turn_before_events(slug, turn_started)")


def test_the_scope_check_runs_after_the_reply_has_been_delivered():
    """It must never sit between generation and delivery. Its read-back costs a round trip
    and its card is a follow-up, not part of the answer."""
    body = _worker_body()
    assert body.index("msg2_id = send(") < body.index("_check_reply_scope(")
    assert body.index("save_history(history, files[\"history\"])") \
        < body.index("_check_reply_scope(")


def test_the_scope_check_buys_its_own_after_and_does_not_reuse_the_snapshot():
    """_verify_icu_calendar_claim leaves a refreshed _EVENTS_SNAPSHOT behind, and handing
    that over would save a read. It is not taken: whether that snapshot is pre- or
    post-retry depends on an invariant spread across two functions that nothing here
    holds, and a scope check reading a pre-retry window is blind on exactly the turns it
    exists for. It reads the calendar itself."""
    body = _worker_body()
    call = body[body.index("_check_reply_scope("):]
    call = call[:call.index("\n")]
    assert "after_events" not in call and "_after_snap" not in call
    src = BOT_SRC[BOT_SRC.index("def _check_reply_scope("):]
    src = src[:src.index("\nclass _StatusTicker")]
    assert "after = _read_planned_window(slug)" in src


def test_the_scope_check_and_the_cancel_report_are_mutually_exclusive():
    """The `if cancelled:` branch reports and RETURNS, so a stopped turn cannot also reach
    the scope check, and a completed turn cannot reach the cancel report. By construction,
    not by convention, and each is called from exactly one place so neither can fire twice
    for one turn."""
    body = _worker_body()
    cancel_at = body.index("_report_cancelled_turn(token, chat_id, slug, placeholder_id")
    scope_at = body.index("_check_reply_scope(")
    assert cancel_at < scope_at
    assert "\n            return\n" in body[cancel_at:scope_at], \
        "the cancelled branch must return before the completed-turn path can run"
    assert body.count("_check_reply_scope(") == 1
    assert body.count("_report_cancelled_turn(") == 1


def _run_worker(monkeypatch, tg, stream_result, message="2.5 hours"):
    """Drive the real _chat_reply_worker with every slow collaborator stubbed. Same idiom
    as test_cancel_turn._run_worker; returns (cancel reports, scope checks)."""
    reports, scoped = [], []
    monkeypatch.setattr(bot, "load_history", lambda f=None: [])
    monkeypatch.setattr(bot, "prefetch_context", lambda slug: "")
    monkeypatch.setattr(bot, "_with_facts", lambda ctx, slug: ctx)
    monkeypatch.setattr(bot, "select_model", lambda text, history: "claude-opus-5")
    monkeypatch.setattr(bot, "_snapshot_rules_text", lambda slug: "")
    monkeypatch.setattr(bot, "_seed_strava_baseline", lambda slug, text: None)
    monkeypatch.setattr(bot, "_voice_mode_on", lambda slug: False)
    monkeypatch.setattr(bot, "typing", lambda token, chat_id: None)
    monkeypatch.setattr(bot, "_apply_rule_capture_guard", lambda slug, before: [])
    monkeypatch.setattr(bot, "process_charts",
                        lambda token, chat_id, response, slug=None: response or "")
    monkeypatch.setattr(bot, "_verify_logged_reply", lambda slug, ts, clean, **kw: clean)
    monkeypatch.setattr(bot, "_verify_session_preview", lambda slug, clean: clean)
    monkeypatch.setattr(bot, "_strip_form_excuse", lambda slug, clean: clean)
    monkeypatch.setattr(bot, "_strip_model_countdown", lambda clean, athlete: clean)
    monkeypatch.setattr(bot, "_verify_external_writes", lambda *a, **k: "")
    monkeypatch.setattr(bot, "_make_capture_retry", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_make_calendar_retry", lambda *a, **k: None)
    monkeypatch.setattr(bot, "response_footer", lambda model, slug="", athlete_cfg=None: "")
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    monkeypatch.setattr(bot, "save_history", lambda h, f=None: None)
    monkeypatch.setattr(bot, "_report_cancelled_turn", lambda *a, **k: reports.append(a))
    monkeypatch.setattr(bot, "_check_reply_scope", lambda *a, **k: scoped.append(a))
    monkeypatch.setattr(bot, "call_claude_streaming", lambda *a, **k: stream_result)
    bot._chat_reply_worker("tok", CHAT, {}, {"race_name": "Cervia"},
                           {"history": "h.json", "system_prompt": "sp.txt"},
                           "Kathryn", SLUG, message, None)
    return reports, scoped


def test_a_cancelled_turn_never_reaches_the_scope_check(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    reports, scoped = _run_worker(monkeypatch, tg,
                                  stream_result=(None, "Updated intervals.icu", True))
    assert len(reports) == 1, "a stopped turn is still the cancel report's job"
    assert scoped == [], "and must not ALSO be carded for scope"


def test_a_completed_turn_reaches_the_scope_check_and_not_the_cancel_report(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    reports, scoped = _run_worker(
        monkeypatch, tg, stream_result=("Here is your week.", "Thought for 2s", False))
    assert reports == []
    assert len(scoped) == 1
    assert scoped[0][2] == SLUG and scoped[0][3] == "2.5 hours"
    assert tg.sends and tg.sends[0][0] == "Here is your week.", \
        "and the reply still goes out exactly as it did before"


def test_only_the_undo_worker_ever_reverses_a_diff():
    """One writer. The auto-apply gate reads; it does not write."""
    src = BOT_SRC[BOT_SRC.index("# --- Scope confirmation"):]
    src = src[:src.index("\nclass _StatusTicker")]
    # Call syntax, not the word - the prose in here explains at length WHY there is no
    # push_workout chokepoint to intercept, and that explanation is the point of the guard.
    assert "push_workout(" not in src and "delete_workout(" not in src


def test_the_scope_check_never_looks_at_the_message_for_suspicious_words():
    """Jamie rejected a keyword list of "ambiguous phrases" outright: the next unanticipated
    wording slips through exactly the way "2.5 hours" did. The message is asked which DAYS
    it names and whether it says "week", and nothing else - both by helpers that already
    existed for other reasons."""
    src = BOT_SRC[BOT_SRC.index("# --- Scope confirmation"):]
    src = src[:src.index("\nclass _StatusTicker")]
    assert "re.compile" not in src, "a new regex over the athlete's words is the rejected design"
    assert "_authorised_dates" in src and "_week_scope_signalled" in src


def test_the_week_signal_is_the_modules_own_and_not_a_new_vocabulary():
    src = (REPO / "lib" / "weekly_availability.py").read_text()
    body = src[src.index("def week_framed("):]
    body = body[:body.index("\ndef sport_exclusion_summary(")]
    assert "_WEEK_FRAMING_RE" in body and "_WEEK_FRAMED_RE" in body
    assert "re.compile" not in body


def test_the_keep_branch_sits_with_the_other_inline_callbacks():
    body = BOT_SRC[BOT_SRC.index("def main():"):]
    assert body.index("_handle_undo(") < body.index("_handle_scope_keep(")
    assert body.index("_handle_scope_keep(") < body.index("__SPEAK_LAST__")


def test_event_unchanged_is_the_public_form_of_the_diffs_own_comparison():
    a = ev(1, WED)
    assert write_verify.event_unchanged(a, dict(a, description="new prose")) is True
    assert write_verify.event_unchanged(a, dict(a, icu_training_load=95)) is False
