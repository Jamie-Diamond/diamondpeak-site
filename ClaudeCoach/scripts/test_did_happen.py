"""Yes / No buttons on the evening check-in's "Did the [session] happen today?" (2 Oct 2026).

Drives telegram/bot.py's real dispatch_callback with Telegram and Intervals.icu faked,
tap by tap: Yes -> RPE ask; No -> Reschedule / Skip; Reschedule -> six days; a day ->
the event moves and the day is pinned; Skip -> logged as missed. Plus lib/did_happen.py's
matching and callback parsing. Run: python3 -m pytest ClaudeCoach/scripts/test_did_happen.py
"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC / "lib"))
sys.path.insert(0, str(CC / "telegram"))
import bot as B          # noqa: E402
import did_happen as D   # noqa: E402

EV = {"id": 139396103, "name": "Strength B — split squat + hip thrust (Tier C)",
      "start_date_local": "2026-10-02T00:00:00", "type": "WeightTraining",
      "moving_time": 2400, "load_target": 20}
ASK = "Did the Strength B — split squat + hip thrust (Tier C) happen today?"


# ── lib/did_happen.py ──

def test_finds_the_named_session_not_a_shorter_namesake():
    short = dict(EV, id=1, name="Strength B")
    ride = {"id": 2, "name": "Aerobic ride 2-3h", "paired_activity_id": "i9"}
    assert D.find_event(ASK, [short, EV, ride])["id"] == EV["id"]


def test_a_paraphrased_name_falls_back_to_the_only_open_session():
    ride = {"id": 2, "name": "Aerobic ride 2-3h", "paired_activity_id": "i9"}
    assert D.find_event("Did your strength session happen today?", [EV, ride])["id"] == EV["id"]


def test_two_open_sessions_and_no_name_match_means_no_buttons():
    other = dict(EV, id=5, name="Easy swim")
    assert D.find_event("Did your session happen today?", [EV, other]) is None


def test_is_ask_only_matches_the_did_it_happen_question():
    assert D.is_ask(ASK)
    assert not D.is_ask("Strength session done. RPE and main focus?")


def test_callback_data_round_trips_and_fits_telegram():
    for verb in ("y", "n", "r", "s"):
        data = D._cb(verb, "jmccabe", EV["id"])
        assert D.parse(data) == (verb, "jmccabe", str(EV["id"]), None)
    data = D._cb("m", "jmccabe", EV["id"], "2026-10-08")
    assert D.parse(data) == ("m", "jmccabe", str(EV["id"]), "2026-10-08")
    assert len(data.encode()) <= 64
    for bad in ("did:m:jamie:1", "did:y:jamie:1:2026-10-03", "did:x:jamie:1",
                "did:m:jamie:1:notadate", "dup:del:jamie:1:2"):
        assert D.parse(bad) is None


def test_days_start_after_the_session_or_today_if_tapped_late():
    fri = date(2026, 10, 2)
    assert D.day_options(fri, today=fri)[0] == date(2026, 10, 3)
    assert len(D.day_options(fri, today=fri)) == 6
    assert D.day_options(fri, today=date(2026, 10, 4))[0] == date(2026, 10, 4)


def test_move_keeps_the_time_of_day():
    assert D.moved_start(dict(EV, start_date_local="2026-10-02T18:30:00"), "2026-10-04") \
        == "2026-10-04T18:30:00"


def test_skip_is_logged_once(tmp_path):
    (tmp_path / "current-state.json").write_text(json.dumps({"ankle": {"pain": 0}}))
    assert D.record_skip(tmp_path, EV) and D.record_skip(tmp_path, EV)
    st = json.loads((tmp_path / "current-state.json").read_text())
    assert st["ankle"] == {"pain": 0}
    assert [m["event_id"] for m in st["missed_sessions"]] == [str(EV["id"])]


# ── telegram/bot.py _handle_did_happen, through dispatch_callback ──

class FakeIcu:
    def __init__(self):
        self.moves = []

    def get_event(self, eid):
        return dict(EV)

    def edit_workout(self, eid, **fields):
        self.moves.append((str(eid), fields))
        return dict(EV, **fields)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    adir = tmp_path / "athletes" / "jamie"
    (adir / "telegram").mkdir(parents=True)
    (adir / "current-state.json").write_text("{}")
    icu, posts, hist = FakeIcu(), [], []
    monkeypatch.setattr(B, "_icu_client", lambda slug: icu)
    monkeypatch.setattr(B, "_athlete_dir", lambda slug: adir)
    monkeypatch.setattr(B, "tg_post", lambda token, method, payload: posts.append((method, payload)) or {})
    monkeypatch.setattr(B, "_append_capture_history",
                        lambda chat_id, slug, user, assistant, kind="capture": hist.append((user, assistant)))
    pins = []
    monkeypatch.setattr(B.agreed_week, "pinned_dates_span", lambda slug, a, b: {})
    monkeypatch.setattr(B.agreed_week, "pin", lambda slug, day, **kw: pins.append((day, kw)))
    athletes = {"42": {"slug": "jamie", "name": "Jamie"}}

    def tap(data):
        posts.clear()
        return B.dispatch_callback("tok", "42", data, 900, athletes, {})
    return {"tap": tap, "posts": posts, "hist": hist, "icu": icu, "pins": pins, "adir": adir}


def _card(posts):
    return next(p for m, p in posts if m == "editMessageText" and p["message_id"] == 900)


def _buttons(card):
    return [b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row]


def test_no_offers_reschedule_or_skip(rig):
    assert rig["tap"](f"did:n:jamie:{EV['id']}")
    card = _card(rig["posts"])
    assert "❌ No" in card["text"]
    assert _buttons(card) == [f"did:r:jamie:{EV['id']}", f"did:s:jamie:{EV['id']}"]
    assert rig["hist"] == []                     # nothing settled yet


def test_reschedule_offers_six_days_and_skip(rig):
    rig["tap"](f"did:r:jamie:{EV['id']}")
    cbs = _buttons(_card(rig["posts"]))
    assert len([c for c in cbs if c.startswith("did:m:")]) == 6
    assert cbs[-1] == f"did:s:jamie:{EV['id']}"


def test_a_day_moves_the_session_pins_the_day_and_tells_the_coach(rig):
    rig["tap"](f"did:m:jamie:{EV['id']}:2026-10-04")
    assert rig["icu"].moves == [(str(EV["id"]), {"start_date_local": "2026-10-04T00:00:00"})]
    assert rig["pins"][0][0] == "2026-10-04"
    card = _card(rig["posts"])
    assert "Moved to Sun 4 Oct" in card["text"] and _buttons(card) == []
    assert rig["hist"] == [("", card["text"])]   # same text as the card: Peak shows it once


def test_skip_logs_missed_and_tells_the_coach(rig):
    rig["tap"](f"did:s:jamie:{EV['id']}")
    st = json.loads((rig["adir"] / "current-state.json").read_text())
    assert st["missed_sessions"][0]["event_id"] == str(EV["id"])
    card = _card(rig["posts"])
    assert "Skipped" in card["text"] and rig["hist"] == [("", card["text"])]


def test_yes_asks_how_hard(rig):
    rig["tap"](f"did:y:jamie:{EV['id']}")
    assert "✅ Yes" in _card(rig["posts"])["text"]
    sent = [p["text"] for m, p in rig["posts"] if m == "sendMessage"]
    assert sent and "out of 10" in sent[0]
    assert rig["hist"] == [("", sent[0])]


def test_another_athletes_button_is_not_handled(rig):
    assert B._handle_did_happen("tok", "42", f"did:s:kathryn:{EV['id']}", 900,
                                {"42": {"slug": "jamie"}}) is False
