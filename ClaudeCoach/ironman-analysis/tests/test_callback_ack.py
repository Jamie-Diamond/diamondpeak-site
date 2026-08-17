"""A tap must be acknowledged on RECEIPT of the batch, in both bots.

Telegram expires a callback query id about 15 seconds after the button is pressed. Both
poll loops already acked as the first statement of their callback branch, which looked
correct and was not: getUpdates returns a BATCH, the loop walks it one update at a time,
and a tap queued behind a slow update in the same batch waited for that update to finish
before it was acked. Coach turns measured 100-500s on 17 Aug 2026, so the ack was refused
("query is too old"), the button span on and the athlete re-tapped.

The fix is a pre-pass over the batch. These tests pin the two things that make it work:
the pre-pass runs before the batch is walked (asserted against the source, the idiom
test_day_overrides_capture.py already uses for dispatch order), and a failing ack cannot
take the batch down with it - it runs ahead of real work, so a raise there would drop
updates that have nothing to do with the tap.

NOT covered here, and not fixable at this seam: a tap made while the loop is ALREADY
blocked inside a handler. No getUpdates call happens until that returns, so the query can
be minutes old before either bot sees it. That is the blocking poll loop (open task #18).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "telegram"))
sys.path.insert(0, str(REPO / "lib"))
import bot  # noqa: E402

BOT_SRC = (REPO / "telegram" / "bot.py").read_text()
NUT_PATH = REPO / "telegram" / "nutrition_bot.py"
NUT_SRC = NUT_PATH.read_text()


def _nutrition_bot():
    spec = importlib.util.spec_from_file_location("nb_ack", NUT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _main_body(src: str) -> str:
    """Only the poll loop. `answer_callback` is DEFINED above main() in both files, so a
    naive index() over the whole source finds the def, not a call site."""
    return src[src.index("def main():"):]


# --- source order ------------------------------------------------------------

@pytest.mark.parametrize("src,call,walk", [
    (BOT_SRC, "ack_callbacks(token, updates)", "for update in updates:"),
    (NUT_SRC, "ack_callbacks(token, updates, allowed)", "for upd in updates:"),
])
def test_the_batch_is_acked_before_it_is_walked(src, call, walk):
    body = _main_body(src)
    assert body.index(call) < body.index(walk), \
        "the ack pre-pass must run before any update in the batch is handled"


def test_coach_bot_acks_before_any_handler_is_dispatched():
    body = _main_body(BOT_SRC)
    assert body.index("ack_callbacks(token, updates)") < body.index("_handle_"), \
        "no callback handler may run before the batch has been acked"


def test_nutrition_bot_acks_before_the_pending_commit():
    body = _main_body(NUT_SRC)
    assert body.index("ack_callbacks(token, updates, allowed)") < body.index("commit_pending("), \
        "the commit is the slow work; the ack must not be behind it"


@pytest.mark.parametrize("body,call", [
    (_main_body(BOT_SRC), "answer_callback("),
    (_main_body(NUT_SRC), "tg.answer_callback("),
])
def test_the_dispatch_branch_does_not_ack_a_second_time(body, call):
    # A re-ack is refused as an already-answered query, and is only quiet because the
    # transport string-matches Telegram's error text. One ack per tap.
    assert call not in body


# --- behaviour: coach bot ----------------------------------------------------

def test_coach_bot_acks_every_tap_in_the_batch(monkeypatch):
    seen = []
    monkeypatch.setattr(bot, "answer_callback", lambda token, cbid: seen.append(cbid))
    bot.ack_callbacks("tok", [
        {"update_id": 1, "callback_query": {"id": "a", "data": "drill:x"}},
        {"update_id": 2, "message": {"text": "hello"}},
        {"update_id": 3, "callback_query": {"id": "b", "data": "test:y"}},
    ])
    assert seen == ["a", "b"]


def test_coach_bot_survives_an_ack_that_raises(monkeypatch):
    seen = []

    def flaky(token, cbid):
        if cbid == "a":
            raise RuntimeError("socket died")
        seen.append(cbid)

    monkeypatch.setattr(bot, "answer_callback", flaky)
    monkeypatch.setattr(bot, "log", lambda msg: None)
    # No raise: this runs ahead of the real work, so an escaping error would drop the batch.
    bot.ack_callbacks("tok", [
        {"update_id": 1, "callback_query": {"id": "a"}},
        {"update_id": 2, "callback_query": {"id": "b"}},
    ])
    assert seen == ["b"], "one bad ack must not stop the rest of the batch being acked"


def test_coach_bot_ignores_a_malformed_update(monkeypatch):
    monkeypatch.setattr(bot, "answer_callback",
                        lambda token, cbid: pytest.fail("nothing to ack here"))
    monkeypatch.setattr(bot, "log", lambda msg: None)
    bot.ack_callbacks("tok", [None, {}, {"callback_query": {}}, "junk"])


# --- behaviour: nutrition bot ------------------------------------------------

def test_nutrition_bot_acks_the_allowed_chat_only(monkeypatch):
    nb = _nutrition_bot()
    seen = []
    monkeypatch.setattr(nb.tg, "answer_callback",
                        lambda token, cbid, log=None: seen.append(cbid))
    monkeypatch.setattr(nb, "log", lambda msg: None)
    nb.ack_callbacks("tok", [
        {"callback_query": {"id": "mine", "message": {"chat": {"id": 4242}}}},
        {"callback_query": {"id": "stranger", "message": {"chat": {"id": 9999}}}},
        {"callback_query": {"id": "no-message-key"}},
        {"message": {"text": "two eggs"}},
    ], "4242")
    # The dispatch branch drops a foreign chat before acking; the pre-pass must not
    # quietly move that boundary.
    assert seen == ["mine"]


def test_nutrition_bot_survives_an_ack_that_raises(monkeypatch):
    nb = _nutrition_bot()
    seen = []

    def flaky(token, cbid, log=None):
        if cbid == "a":
            raise RuntimeError("socket died")
        seen.append(cbid)

    monkeypatch.setattr(nb.tg, "answer_callback", flaky)
    monkeypatch.setattr(nb, "log", lambda msg: None)
    nb.ack_callbacks("tok", [
        {"callback_query": {"id": "a", "message": {"chat": {"id": 1}}}},
        {"callback_query": {"id": "b", "message": {"chat": {"id": 1}}}},
    ], "1")
    assert seen == ["b"]
