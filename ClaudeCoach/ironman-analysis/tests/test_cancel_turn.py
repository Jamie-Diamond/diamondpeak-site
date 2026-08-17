"""Stopping a turn from Telegram, and being honest about what it managed to do first
(bug #30 part (a), 17 Aug 2026).

lib/engine.py already owns the kill and test_engine_cancel.py pins that contract. These
tests are the TRANSPORT half: the Stop button, the tap that reaches the engine without
queueing behind the reply it is meant to kill, and - the part Jamie actually asked for -
the calendar read-back afterwards. His words when asked how strong the guarantee had to
be: "It doesn't have to guarantee it, but it should be aware of what it hasn't changed so
it knows how to rectify it."

So the properties worth failing a build over:

  1. A cancelled turn NEVER sends "(no response)" and NEVER writes history. That string is
     the crash fallback. Posted after a Stop it reads as the bot having tried and failed,
     and a history entry would have the next turn answer the withdrawn question anyway.
  2. Stop before the run starts, during it, and after it has finished are all safe.
     stream_claude is a generator, so "before it starts" is a real window, not a theory.
  3. THE STALE TAP. A tap carrying a finished run's id must not touch the run that is live
     now. This is the transport-side version of test_engine_cancel's race test: it fails
     the moment anyone re-keys this by chat id, which is the tempting simplification.
  4. Nothing changed -> say so. Something changed -> name it and offer it back.
  5. The undo restores through icu_api's own push_workout/delete_workout, and REFUSES an
     edit rather than half-restoring it.
  6. The Stop button survives the ticker's 1/sec rewrite, and does not survive the turn.
  7. A turn nobody cancels is untouched. This ships ahead of guaranteed use.

Fake Telegram transport, fake ICU client, fake stream. Nothing here spawns a process or
opens a socket.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "telegram"))
sys.path.insert(0, str(REPO / "lib"))
import bot        # noqa: E402
import engine     # noqa: E402
import write_verify  # noqa: E402

BOT_SRC = (REPO / "telegram" / "bot.py").read_text()

CHAT = "4242"
SLUG = "kathryn"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTelegram:
    """Records every outbound call instead of making it. `post` mimics tg_post's return
    shape closely enough for the placeholder's message_id to come back."""

    def __init__(self, message_id=901):
        self.posts = []          # (method, payload)
        self.sends = []          # (text, reply_markup)
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


class FakeIcu:
    """The write endpoints the undo uses, recorded. Same names and argument shapes as
    lib/icu_api.IcuClient - if that drifts, these tests should be the thing that notices."""

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
        self.pushed.append({"sport": sport, "event_date": event_date, "name": name,
                            "description": description, "description_raw": description_raw,
                            "planned_training_load": planned_training_load, **kwargs})
        return {"id": 999}


def ev(eid, day, name="Endurance ride", etype="Ride", load=60, minutes=90, **extra):
    e = {"id": eid, "start_date_local": f"{day}T00:00:00", "type": etype,
         "name": name, "icu_training_load": load, "moving_time": minutes * 60}
    e.update(extra)
    return e


def fake_stream(events, spy=None):
    """A stand-in for engine.stream_claude that yields a scripted event list and records
    the run_id/run_owner it was handed."""
    def _stream(user_message, config, history, model=None, system_prompt_file=None,
                athlete_name="", context="", run_id=None, run_owner=None):
        if spy is not None:
            spy["run_id"] = run_id
            spy["run_owner"] = run_owner
        for e in events:
            yield e
    return _stream


@pytest.fixture(autouse=True)
def _clean_module_state():
    """The registries are module-level and this suite pokes them directly. Leaving one
    populated would leak a cancel request into the next test."""
    bot._RUN_PHASE.clear()
    bot._PENDING_UNDO.clear()
    bot._EVENTS_SNAPSHOT.clear()
    yield
    bot._RUN_PHASE.clear()
    bot._PENDING_UNDO.clear()
    bot._EVENTS_SNAPSHOT.clear()


# ---------------------------------------------------------------------------
# 1. The Stop button and its token
# ---------------------------------------------------------------------------

def test_the_stop_token_carries_the_run_id_and_fits_telegrams_limit():
    rid = engine.new_run_id()
    kb = bot._stop_keyboard(rid)
    data = kb["inline_keyboard"][0][0]["callback_data"]
    assert data == f"stop:{rid}"
    # Telegram rejects callback_data over 64 bytes, and a rejected sendMessage means no
    # button at all. uuid4().hex is 32 chars, so this has ~27 bytes of headroom.
    assert len(data.encode()) <= 64


def test_the_placeholder_is_sent_with_the_stop_button(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    _run_worker(monkeypatch, tg, stream_result=("An answer", "Thought for 2s", False))
    sends = [p for m, p in tg.posts if m == "sendMessage"]
    assert sends, "the placeholder must still be sent"
    kb = sends[0].get("reply_markup") or {}
    assert kb["inline_keyboard"][0][0]["callback_data"].startswith("stop:")


# ---------------------------------------------------------------------------
# 2. The ticker must not strip the button, and must not resurrect it
# ---------------------------------------------------------------------------

def _idle_ticker(monkeypatch, reply_markup):
    """A ticker whose background thread will not fire during the test (not_before is far
    in the future), so _edit can be driven deterministically."""
    return bot._StatusTicker("tok", CHAT, 901, time.time() + 3600,
                             reply_markup=reply_markup)


def test_the_ticker_carries_the_keyboard_on_every_edit(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    kb = bot._stop_keyboard("run-1")
    ticker = _idle_ticker(monkeypatch, kb)
    try:
        ticker._edit("Thinking... (1s)")
        ticker._edit("Checking intervals.icu (2s)")
        ticker._edit("Checking intervals.icu (3s)")
    finally:
        ticker.stop()
    edits = tg.edits()
    assert len(edits) == 3
    # Telegram REMOVES an inline keyboard on any editMessageText that omits reply_markup,
    # so an omission here deletes the Stop button one second into every turn.
    assert all(e.get("reply_markup") == kb for e in edits)


def test_a_ticker_without_a_keyboard_edits_exactly_as_it_used_to(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    ticker = _idle_ticker(monkeypatch, None)
    try:
        ticker._edit("Thinking... (1s)")
    finally:
        ticker.stop()
    assert tg.edits() == [{"chat_id": CHAT, "message_id": 901, "text": "Thinking... (1s)"}]


def test_a_ticker_edit_that_wakes_after_teardown_is_dropped(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    ticker = _idle_ticker(monkeypatch, bot._stop_keyboard("run-1"))
    ticker.stop()
    ticker._edit("Checking intervals.icu (9s)")
    assert tg.edits() == [], \
        "a late tick must not put the status line - or the Stop button - back on a finished turn"


# ---------------------------------------------------------------------------
# 3. The tap: before, during, after
# ---------------------------------------------------------------------------

def test_a_tap_during_the_run_reaches_the_engine(monkeypatch):
    FakeTelegram().install(monkeypatch)
    rid = engine.new_run_id()
    run = engine._register_run(rid, CHAT)
    try:
        assert bot._handle_stop("tok", CHAT, f"stop:{rid}", 901) is True
        assert run.cancelled is True
    finally:
        engine._deregister_run(run)


def test_a_tap_before_the_run_starts_is_parked_and_honoured(monkeypatch):
    FakeTelegram().install(monkeypatch)
    rid = "not-registered-yet"
    assert bot._handle_stop("tok", CHAT, f"stop:{rid}", 901) is True
    assert bot._RUN_PHASE[rid][0] == "want-cancel"
    # And the worker honours it WITHOUT spawning: stream_claude must never be reached.
    def _explode(*a, **k):
        pytest.fail("a parked cancel must stop the CLI being spawned at all")
    monkeypatch.setattr(bot, "stream_claude", _explode)
    response, summary, cancelled = bot.call_claude_streaming(
        "tok", CHAT, 901, "replan my week", {}, [], run_id=rid)
    assert (response, cancelled) == (None, True)
    assert summary is None, "nothing ran, so msg1 must not collapse to invented work"
    assert rid not in bot._RUN_PHASE, "the parked request must be consumed, not left to fire twice"


def test_a_tap_after_the_turn_finished_is_a_harmless_no_op(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    rid = engine.new_run_id()
    bot._mark_run_done(rid)
    assert bot._handle_stop("tok", CHAT, f"stop:{rid}", 901) is True
    # Not parked - a "done" run must never acquire a pending cancel that a later turn
    # could inherit.
    assert bot._RUN_PHASE[rid][0] == "done"
    assert ("editMessageReplyMarkup", {"chat_id": CHAT, "message_id": 901,
                                       "reply_markup": {"inline_keyboard": []}}) in tg.posts
    assert "already finished" in tg.sent_text()


def test_the_stop_handler_ignores_callbacks_that_are_not_its_own(monkeypatch):
    FakeTelegram().install(monkeypatch)
    assert bot._handle_stop("tok", CHAT, "__SPEAK_LAST__", 901) is False
    assert bot._handle_stop("tok", CHAT, "bf:12", 901) is False


def test_a_malformed_stop_token_does_not_raise(monkeypatch):
    FakeTelegram().install(monkeypatch)
    assert bot._handle_stop("tok", CHAT, "stop:", 901) is True
    assert bot._RUN_PHASE == {}


# ---------------------------------------------------------------------------
# 4. THE STALE TAP - the negative control's permanent home
# ---------------------------------------------------------------------------

def test_a_stale_tap_cannot_kill_the_run_that_is_live_now(monkeypatch):
    """Run A finishes. Run B starts for the SAME chat. The athlete's tap on A's button,
    delayed in a batch, lands now. B must survive.

    This is the test that fails if anyone re-keys cancellation by chat id - e.g. by
    reaching for engine.active_run_ids(owner=chat_id), whose own docstring forbids exactly
    that. Killing the athlete's fresh, correct question is worse than the bug being fixed.
    """
    FakeTelegram().install(monkeypatch)
    rid_a = engine.new_run_id()
    run_a = engine._register_run(rid_a, CHAT)
    engine._deregister_run(run_a)            # A finished on its own
    bot._mark_run_done(rid_a)

    rid_b = engine.new_run_id()
    run_b = engine._register_run(rid_b, CHAT)
    try:
        bot._handle_stop("tok", CHAT, f"stop:{rid_a}", 901)
        assert run_b.cancelled is False, "a tap naming run A must never touch run B"
    finally:
        engine._deregister_run(run_b)


def test_a_tap_from_another_chat_is_refused(monkeypatch):
    FakeTelegram().install(monkeypatch)
    rid = engine.new_run_id()
    run = engine._register_run(rid, CHAT)
    try:
        bot._handle_stop("tok", "9999", f"stop:{rid}", 901)
        assert run.cancelled is False
    finally:
        engine._deregister_run(run)


# ---------------------------------------------------------------------------
# 5. call_claude_streaming's contract
# ---------------------------------------------------------------------------

def test_a_turn_nobody_cancels_is_unchanged(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    spy = {}
    monkeypatch.setattr(bot, "stream_claude", fake_stream([
        ("status", "Bash", "icu_fetch.py --athlete kathryn get_events"),
        ("chunk", "partial"),
        ("final", "Here is your week."),
    ], spy))
    response, summary, cancelled = bot.call_claude_streaming(
        "tok", CHAT, 901, "how am I doing?", {}, [], run_id="run-x")
    assert (response, cancelled) == ("Here is your week.", False)
    assert summary and summary != "Stopped"
    assert (spy["run_id"], spy["run_owner"]) == ("run-x", CHAT)


def test_no_run_id_leaves_the_stream_call_exactly_as_it_was(monkeypatch):
    FakeTelegram().install(monkeypatch)
    spy = {}
    monkeypatch.setattr(bot, "stream_claude",
                        fake_stream([("final", "Here is your week.")], spy))
    response, _summary, cancelled = bot.call_claude_streaming(
        "tok", CHAT, 901, "how am I doing?", {}, [])
    assert (response, cancelled) == ("Here is your week.", False)
    assert (spy["run_id"], spy["run_owner"]) == (None, None)


def test_a_cancelled_stream_never_returns_the_no_response_fallback(monkeypatch):
    FakeTelegram().install(monkeypatch)
    monkeypatch.setattr(bot, "stream_claude", fake_stream([
        ("status", "Bash", "icu_fetch.py push_workout"),
        ("chunk", "I've moved Tuesday"),
        ("cancelled", "I've moved Tuesday"),
    ]))
    response, _summary, cancelled = bot.call_claude_streaming(
        "tok", CHAT, 901, "2.5 hours", {}, [], run_id="run-x")
    assert cancelled is True
    assert response is None
    assert response != "(no response)"


def test_a_crash_still_takes_the_old_fallback(monkeypatch):
    """A stream that ends without a final is a CRASH, not a cancel, and keeps its existing
    error path. Confusing the two is the whole reason `cancelled` is a separate value."""
    FakeTelegram().install(monkeypatch)
    monkeypatch.setattr(bot, "stream_claude", fake_stream([("chunk", "half")]))
    response, _summary, cancelled = bot.call_claude_streaming(
        "tok", CHAT, 901, "hello", {}, [], run_id="run-x")
    assert (response, cancelled) == ("(no response)", False)


# ---------------------------------------------------------------------------
# 6. The worker: no reply, no history
# ---------------------------------------------------------------------------

def _run_worker(monkeypatch, tg, stream_result, capture_history=None):
    """Drive _chat_reply_worker with every slow/IO collaborator stubbed. Returns the
    _report_cancelled_turn calls it made."""
    reports = []
    saved = []
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
    monkeypatch.setattr(bot, "_verify_logged_reply",
                        lambda slug, ts, clean, **kw: clean)
    monkeypatch.setattr(bot, "_verify_session_preview", lambda slug, clean: clean)
    monkeypatch.setattr(bot, "_verify_external_writes", lambda *a, **k: "")
    monkeypatch.setattr(bot, "_make_capture_retry", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_make_calendar_retry", lambda *a, **k: None)
    monkeypatch.setattr(bot, "response_footer", lambda model, slug="", athlete_cfg=None: "")
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    monkeypatch.setattr(bot, "save_history", lambda h, f=None: saved.append(list(h)))
    monkeypatch.setattr(bot, "_report_cancelled_turn",
                        lambda *a, **k: reports.append(a))
    monkeypatch.setattr(bot, "call_claude_streaming", lambda *a, **k: stream_result)
    monkeypatch.setattr(bot, "call_claude",
                        lambda *a, **k: pytest.fail("the streaming path must be used here"))
    bot._chat_reply_worker("tok", CHAT, {}, {"race_name": "Cervia"},
                           {"history": "h.json", "system_prompt": "sp.txt"},
                           "Kathryn", SLUG, "2.5 hours", capture_history)
    return reports, saved


def test_a_cancelled_turn_sends_no_reply_and_writes_no_history(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    reports, saved = _run_worker(monkeypatch, tg,
                                 stream_result=(None, "Updated intervals.icu", True))
    assert saved == [], "a withdrawn question must not enter history"
    assert tg.sends == [], "the report is _report_cancelled_turn's job, not a reply"
    assert "(no response)" not in tg.sent_text()
    assert len(reports) == 1
    assert reports[0][2] == SLUG


def test_an_uncancelled_turn_still_replies_and_saves(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    reports, saved = _run_worker(monkeypatch, tg,
                                 stream_result=("Here is your week.", "Thought for 2s", False))
    assert reports == []
    assert len(saved) == 1
    assert tg.sends and tg.sends[0][0] == "Here is your week."


def test_the_stop_button_is_removed_when_the_turn_ends_normally(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    _run_worker(monkeypatch, tg,
                stream_result=("Here is your week.", "Thought for 2s", False))
    collapse = tg.edits()[-1]
    assert collapse["text"] == "Thought for 2s"
    # Explicit, not incidental. Omitting reply_markup would also strip it today, but that
    # is the accident this feature had to fix on the ticker - it must not be the mechanism
    # anything relies on.
    assert collapse["reply_markup"] == {"inline_keyboard": []}


def test_the_run_is_marked_done_so_a_later_tap_is_answered(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    _run_worker(monkeypatch, tg,
                stream_result=("Here is your week.", "Thought for 2s", False))
    assert [p for p, _ts in bot._RUN_PHASE.values()] == ["done"]


# ---------------------------------------------------------------------------
# 7. The report: diff the calendar and say what you find
# ---------------------------------------------------------------------------

def _report(monkeypatch, before, after, tg=None):
    tg = tg or FakeTelegram().install(monkeypatch)
    monkeypatch.setattr(bot, "_CANCEL_SETTLE_S", 0)
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    if before is not None:
        bot._set_events_snapshot(SLUG, time.time(), before)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: after)
    bot._report_cancelled_turn("tok", CHAT, SLUG, 901, "Updated intervals.icu")
    return tg


def test_nothing_changed_is_said_plainly(monkeypatch):
    week = [ev(1, "2026-08-18"), ev(2, "2026-08-20")]
    tg = _report(monkeypatch, week, list(week))
    assert tg.sent_text() == "Stopped. Nothing reached your calendar."
    assert tg.sends[0][1] == {"inline_keyboard": []}, "no undo to offer"


def test_a_write_that_landed_is_named_and_offered_back(monkeypatch):
    before = [ev(1, "2026-08-18")]
    after = before + [ev(7, "2026-08-19", name="Threshold 3x12", load=88)]
    tg = _report(monkeypatch, before, after)
    text = tg.sent_text()
    assert "Stopped" in text
    assert "Threshold 3x12" in text
    assert "Wed 19 Aug" in text, "the DAY is what the athlete recognises"
    assert "88 TSS" in text
    kb = tg.sends[0][1]
    assert kb["inline_keyboard"][0][0]["callback_data"].startswith("undo:")


def test_a_removed_session_is_named_and_offered_back(monkeypatch):
    before = [ev(1, "2026-08-18", name="Long ride"), ev(2, "2026-08-20")]
    after = [ev(2, "2026-08-20")]
    tg = _report(monkeypatch, before, after)
    assert "removed" in tg.sent_text()
    assert "Long ride" in tg.sent_text()
    assert tg.sends[0][1]["inline_keyboard"][0][0]["callback_data"].startswith("undo:")


def test_an_edit_is_reported_but_never_offered_as_an_undo(monkeypatch):
    before = [ev(1, "2026-08-18", name="Long ride", load=200)]
    after = [ev(1, "2026-08-18", name="Long ride", load=120)]
    tg = _report(monkeypatch, before, after)
    text = tg.sent_text()
    assert "changed" in text
    assert "can't safely reverse" in text
    assert tg.sends[0][1] == {"inline_keyboard": []}, \
        "an edit is not reversible from a fingerprint, so no button may promise it is"


def test_a_missing_snapshot_produces_honesty_not_reassurance(monkeypatch):
    tg = _report(monkeypatch, None, [ev(1, "2026-08-18")])
    text = tg.sent_text()
    assert "couldn't check your calendar" in text
    # The one thing it must never do on no evidence is claim the calendar is clean.
    assert "Nothing reached your calendar" not in text


def test_a_failed_read_back_produces_honesty_not_reassurance(monkeypatch):
    tg = _report(monkeypatch, [ev(1, "2026-08-18")], None)
    assert "couldn't check your calendar" in tg.sent_text()


def test_the_report_collapses_msg1_and_takes_the_button_with_it(monkeypatch):
    week = [ev(1, "2026-08-18")]
    tg = _report(monkeypatch, week, list(week))
    collapse = tg.edits()[-1]
    assert collapse["reply_markup"] == {"inline_keyboard": []}
    assert "stopped" in collapse["text"]


# ---------------------------------------------------------------------------
# 8. Undo
# ---------------------------------------------------------------------------

def test_undo_deletes_what_the_stopped_turn_added(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    icu = FakeIcu()
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: [])
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    diff = write_verify.events_diff([ev(1, "2026-08-18")],
                                    [ev(1, "2026-08-18"), ev(7, "2026-08-19")])
    bot._undo_worker("tok", CHAT, SLUG, {"diff": diff}, 901)
    assert icu.deleted == ["7"]
    assert icu.pushed == []
    assert "Put back:" in tg.sent_text()


def test_undo_recreates_what_the_stopped_turn_removed(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    icu = FakeIcu()
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: [])
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    gone = ev(3, "2026-08-21", name="Brick", etype="Ride", load=110, minutes=150,
              description="Main Set 3x\n- 20m Z3", description_raw="Steady off the bike")
    diff = write_verify.events_diff([gone], [])
    bot._undo_worker("tok", CHAT, SLUG, {"diff": diff}, 901)
    assert icu.deleted == []
    assert len(icu.pushed) == 1
    p = icu.pushed[0]
    # The exact icu_api.push_workout call shape - a rebuild that loses the load or the
    # structured description is not a restore.
    assert p["sport"] == "Ride"
    assert p["event_date"] == "2026-08-21"
    assert p["name"] == "Brick"
    assert p["planned_training_load"] == 110
    assert p["description"] == "Main Set 3x\n- 20m Z3"
    assert p["description_raw"] == "Steady off the bike"
    assert p["moving_time"] == 150 * 60


def test_undo_refuses_a_non_workout_event_rather_than_recreating_it_wrong(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    icu = FakeIcu()
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: [])
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    # push_workout hardcodes category=WORKOUT, so a deleted NOTE would come back as a
    # session. Say so instead.
    note = ev(5, "2026-08-22", name="Travel day", category="NOTE")
    diff = write_verify.events_diff([note], [])
    bot._undo_worker("tok", CHAT, SLUG, {"diff": diff}, 901)
    assert icu.pushed == []
    assert "Travel day" in tg.sent_text()


def test_undo_reports_a_write_that_would_not_go_back(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    icu = FakeIcu(fail_on=("delete",))
    monkeypatch.setattr(bot, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: [])
    monkeypatch.setattr(bot, "_reply_inline", lambda slug=None: {"inline_keyboard": []})
    diff = write_verify.events_diff([], [ev(7, "2026-08-19", name="Threshold 3x12")])
    bot._undo_worker("tok", CHAT, SLUG, {"diff": diff}, 901)
    text = tg.sent_text()
    assert "couldn't undo any of that" in text
    assert "Threshold 3x12" in text
    # And the card must not claim otherwise: "Undone" over a restore that wrote nothing is
    # the same lie as a claimed calendar push that never landed.
    assert "Couldn't undo that" in tg.edits()[-1]["text"]


def test_an_undo_token_is_single_use_and_chat_scoped(monkeypatch):
    diff = write_verify.events_diff([], [ev(7, "2026-08-19")])
    tok = bot._park_undo(CHAT, SLUG, diff)
    assert bot._take_undo(tok, "9999") is None, "another chat must not be able to fire it"
    assert bot._take_undo(tok, CHAT) is not None
    assert bot._take_undo(tok, CHAT) is None, "a restore already run must not run again"


def test_an_expired_undo_says_so_and_writes_nothing(monkeypatch):
    tg = FakeTelegram().install(monkeypatch)
    monkeypatch.setattr(bot, "_submit",
                        lambda *a, **k: pytest.fail("nothing to undo, so nothing to submit"))
    handled = bot._handle_undo("tok", CHAT, "undo:deadbeef", 901,
                               {CHAT: {"slug": SLUG}})
    assert handled is True
    edits = tg.edits()
    assert "expired" in edits[-1]["text"]


def test_the_undo_tap_goes_through_the_reply_pool(monkeypatch):
    """Unlike the Stop tap. The turn is over so the per-chat lock is free, and a calendar
    write must be serialised against the athlete's next message."""
    tg = FakeTelegram().install(monkeypatch)
    submitted = []
    monkeypatch.setattr(bot, "_submit", lambda worker, chat_id, *a: submitted.append(worker))
    diff = write_verify.events_diff([], [ev(7, "2026-08-19")])
    tok = bot._park_undo(CHAT, SLUG, diff)
    assert bot._handle_undo("tok", CHAT, f"undo:{tok}", 901, {CHAT: {"slug": SLUG}}) is True
    assert submitted == [bot._undo_worker]


# ---------------------------------------------------------------------------
# 9. The snapshot has to carry the events, not just the fingerprint
# ---------------------------------------------------------------------------

def test_the_snapshot_keeps_the_events_an_undo_restores_from():
    week = [ev(1, "2026-08-18"), ev(2, "2026-08-20")]
    bot._set_events_snapshot(SLUG, 1000.0, week)
    epoch, fp, events = bot._events_snapshot(SLUG)
    assert epoch == 1000.0
    assert fp == write_verify.events_fingerprint(week)
    assert events == week


def test_the_snapshot_is_copied_not_aliased():
    week = [ev(1, "2026-08-18")]
    bot._set_events_snapshot(SLUG, 1000.0, week)
    week.append(ev(9, "2026-08-25"))
    assert len(bot._events_snapshot(SLUG)[2]) == 1, \
        "the 'before' must not follow the caller's list as it is mutated"


def test_a_pre_widening_two_tuple_disables_undo_rather_than_crashing():
    """A process reloaded mid-turn can hold an old 2-tuple. It must read as "no events to
    restore from", never as "the window was empty" - the second would have an undo delete
    every session the athlete has."""
    bot._EVENTS_SNAPSHOT[SLUG] = (1000.0, frozenset())
    epoch, fp, events = bot._events_snapshot(SLUG)
    assert (epoch, fp, events) == (1000.0, frozenset(), None)


def test_the_verifier_still_returns_its_old_verdicts(monkeypatch):
    """The snapshot widening must not move icu_events_verdict's answers."""
    monkeypatch.setattr(bot, "log", lambda msg: None)
    week = [ev(1, "2026-08-18")]
    bot._set_events_snapshot(SLUG, time.time(), week)
    monkeypatch.setattr(bot, "_read_planned_window", lambda slug: list(week))
    assert bot._verify_icu_calendar_claim(SLUG) == "absent"

    bot._set_events_snapshot(SLUG, time.time(), week)
    monkeypatch.setattr(bot, "_read_planned_window",
                        lambda slug: week + [ev(7, "2026-08-19")])
    assert bot._verify_icu_calendar_claim(SLUG) == "ok"

    bot._EVENTS_SNAPSHOT.clear()
    assert bot._verify_icu_calendar_claim(SLUG) == "unknown"


# ---------------------------------------------------------------------------
# 10. Source-level wiring the behaviour tests cannot see
# ---------------------------------------------------------------------------

def _main_body():
    return BOT_SRC[BOT_SRC.index("def main():"):]


def test_the_stop_tap_is_handled_inline_and_never_submitted():
    """_submit takes the per-chat lock, which is held by the reply the tap is cancelling.
    A queued cancel runs only after its target has finished, i.e. never usefully."""
    src = BOT_SRC[BOT_SRC.index("def _handle_stop("):]
    src = src[:src.index("\n# --- Undo")]
    assert "_submit(" not in src


def test_the_stop_branch_runs_before_every_other_callback_handler():
    body = _main_body()
    assert body.index("_handle_stop(") < body.index("_handle_bugfix("), \
        "the athlete is watching a wrong answer being built; nothing may queue in front"


def test_the_stop_branch_does_not_ack_a_second_time():
    # ack_callbacks already acked the whole batch on receipt (17 Aug 2026). A re-ack is
    # refused as an already-answered query.
    src = BOT_SRC[BOT_SRC.index("def _handle_stop("):]
    src = src[:src.index("\n# --- Undo")]
    assert "answer_callback(" not in src


def test_cancellation_is_never_looked_up_by_chat_id():
    """engine.active_run_ids exists for diagnostics and its docstring forbids this use.
    A lookup by owner is the API that kills the athlete's next, correct question."""
    assert "active_run_ids" not in BOT_SRC
