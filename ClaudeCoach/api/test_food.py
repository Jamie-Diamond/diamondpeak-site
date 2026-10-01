"""Tests for api/food.py: the food bot's own handlers, its Telegram calls redirected to
the web turn. Run on the VM (imports telegram/nutrition_bot.py and its lib/)."""
import json
import time

import food


def run(sink):
    out = []
    while True:
        kind, text, extra = sink.q.get(timeout=10)
        out.append((kind, text, extra))
        if kind == "done":
            return out


def test_a_web_turn_runs_the_food_bot_and_keeps_both_sides(tmp_path, monkeypatch):
    monkeypatch.setattr(food, "CC", tmp_path)
    (tmp_path / "athletes" / "jamie").mkdir(parents=True)
    (tmp_path / "athletes" / "jamie" / "profile.json").write_text(json.dumps({"nutrition_tracker": True}))
    assert food.enabled("jamie")
    m = food.nb()
    sent_to_telegram = []
    monkeypatch.setattr(food, "_context", lambda slug: (object(), "TOKEN"))

    def fake_handle_text(ctx, text, token, chat_id):
        m.tg.send(token, chat_id, f"2 eggs, 140 kcal. Log it?",
                  reply_markup={"inline_keyboard": [[{"text": "Log it", "callback_data": "confirm"},
                                                     {"text": "No", "callback_data": "cancel"}]]})
    monkeypatch.setattr(m, "handle_text", fake_handle_text)
    events = run(food.start_turn("jamie", text="2 eggs"))
    msgs = [(t, e.get("buttons")) for k, t, e in events if k == "message"]
    assert msgs == [("2 eggs, 140 kcal. Log it?", [[{"text": "Log it", "data": "confirm"},
                                                    {"text": "No", "data": "cancel"}]])]
    h = food.history("jamie")
    assert [(x["who"], x["text"]) for x in h] == [("me", "2 eggs"), ("bot", "2 eggs, 140 kcal. Log it?")]
    assert h[1]["buttons"]

    # outside a web turn the real Telegram send is untouched
    calls = []
    real = m.tg.send
    assert getattr(food._TURN, "sink", None) is None


def test_tapping_no_drops_the_offer_and_clears_old_buttons(tmp_path, monkeypatch):
    monkeypatch.setattr(food, "CC", tmp_path)
    (tmp_path / "athletes" / "jamie").mkdir(parents=True)
    m = food.nb()
    monkeypatch.setattr(food, "_context", lambda slug: (type("C", (), {"store": None})(), "TOKEN"))
    cleared = []
    monkeypatch.setattr(m, "get_pending", lambda store: {"items": []})
    monkeypatch.setattr(m, "set_inbound", lambda ctx, t: None)
    monkeypatch.setattr(m, "clear_pending", lambda store: cleared.append(1))
    food._record("jamie", "bot", "2 eggs. Log it?", [[{"text": "No", "data": "cancel"}]])
    events = run(food.start_turn("jamie", button="cancel"))
    assert cleared and [t for k, t, e in events if k == "message"] == ["Dropped it."]
    h = food.history("jamie")
    assert h[0]["buttons"] == [] and [x["text"] for x in h[1:]] == ["No", "Dropped it."]


def test_one_turn_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(food, "CC", tmp_path)
    (tmp_path / "athletes" / "jamie").mkdir(parents=True)
    m = food.nb()
    monkeypatch.setattr(food, "_context", lambda slug: (object(), "TOKEN"))
    monkeypatch.setattr(m, "handle_text", lambda *a: time.sleep(0.5))
    s1 = food.start_turn("jamie", text="slow")
    try:
        food.start_turn("jamie", text="again")
        assert False, "second turn should be refused"
    except food.Busy:
        pass
    run(s1)


def test_edit_rescales_moves_and_deletes_a_logged_item(tmp_path, monkeypatch):
    import sys
    from datetime import date
    sys.path.insert(0, str(food.CODE / "lib"))
    from nutrition_store import NutritionStore
    monkeypatch.setattr(food, "CC", tmp_path)
    adir = tmp_path / "athletes" / "jamie"
    adir.mkdir(parents=True)
    store = NutritionStore(adir)
    m = food.nb()
    e = store.add_entry(date(2026, 10, 1), raw_text="honey", resolved_name="Honey", kcal=28.8,
                        protein_g=0.0, carb_g=7.6, fat_g=0.0, portion_g=10.0, portion_used_g=10.0,
                        logged_at="2026-10-01T10:26", meal="breakfast",
                        per_100g={"kcal": 288, "protein_g": 0.0, "carb_g": 76, "fat_g": 0.0})
    eid = e["id"] if isinstance(e, dict) else e
    ctx = type("C", (), {"store": store})()
    monkeypatch.setattr(food, "_context", lambda slug: (ctx, "TOKEN"))
    monkeypatch.setattr(m, "record_action", lambda c, t: None)
    published = []
    monkeypatch.setattr(food, "_publish", lambda slug: published.append(slug))
    said = food.edit_entry("jamie", "2026-10-01", eid, grams=20, meal="snacks", time="11:00")
    got = next(x for x in store.get_day("2026-10-01")["entries"] if x["id"] == eid)
    assert round(got["kcal"]) == 58 and got["portion_used_g"] == 20 and got["meal"] == "snacks"
    assert got["logged_at"] == "2026-10-01T11:00" and "20 g" in said and published == ["jamie"]
    assert food.history("jamie")[-1]["text"].startswith("\u270f")
    food.edit_entry("jamie", "2026-10-01", eid, delete=True)
    assert not store.get_day("2026-10-01")["entries"]
    try:
        food.edit_entry("jamie", "2026-10-01", eid, delete=True)
        assert False
    except LookupError:
        pass
