"""Tests for lib/outbox.py. Run: python3 -m pytest ClaudeCoach/lib/test_outbox.py"""
import json

import outbox


def setup(tmp_path, monkeypatch, athletes):
    cfg = tmp_path / "athletes.json"
    cfg.write_text(json.dumps(athletes))
    monkeypatch.setattr(outbox, "BASE", tmp_path)
    monkeypatch.setattr(outbox, "ATHLETES_CONFIG", cfg)
    monkeypatch.setattr(outbox, "SIGNUP_DIR", tmp_path / "config" / "web-signup")
    outbox._CACHE.update(mtime=None, data={})
    return cfg


def test_record_keeps_text_buttons_and_photo(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"kat": {"chat_id": "222", "active": True}})
    kb = {"inline_keyboard": [[{"text": "RPE 7", "callback_data": "r:1:kat:7"},
                               {"text": "Site", "url": "https://x"}, {"text": "?"}]]}
    mid = outbox.record("222", "Morning card", kb, source="notify")
    outbox.record("222", "", photo=b"PNG")
    rows = outbox.read("kat")
    assert rows[0]["id"] == mid and rows[0]["text"] == "Morning card"
    assert rows[0]["buttons"] == [[{"text": "RPE 7", "data": "r:1:kat:7"}, {"text": "Site", "url": "https://x"}]]
    assert (tmp_path / "athletes" / "kat" / "web-media" / rows[1]["photo"]).read_bytes() == b"PNG"


def test_unknown_chat_records_nothing_and_keeps_telegram(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"kat": {"chat_id": "222"}})
    assert outbox.record("999", "hello") is None
    assert outbox.telegram_on("999") is True


def test_switch_and_web_only_chats(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"kat": {"chat_id": "222", "telegram": False},
                                  "jam": {"chat_id": "111"},
                                  "new": {"chat_id": "web-abc"}})
    assert outbox.telegram_on("222") is False
    assert outbox.telegram_on("111") is True
    assert outbox.telegram_on("web-abc") is False


def test_html_cards_become_markdown(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"jam": {"chat_id": "111"}})
    outbox.record("111", "🛠 <b>Bug fix ready</b>\n<i>a &amp; b</i>", parse_mode="HTML")
    assert outbox.read("jam")[0]["text"] == "🛠 *Bug fix ready*\n_a & b_"


def test_trim(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"jam": {"chat_id": "111"}})
    monkeypatch.setattr(outbox, "KEEP_LINES", 3)
    for i in range(7):
        outbox.record("111", f"m{i}")
    assert [r["text"] for r in outbox.read("jam")] == ["m4", "m5", "m6"]


def test_update_rewrites_one_message_and_keeps_the_original_text(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {"jam": {"chat_id": "111"}})
    kb = {"inline_keyboard": [[{"text": "0", "callback_data": "p:1:jam:0"}]]}
    a = outbox.record("111", "Injury pain during (0-10)", kb)
    b = outbox.record("111", "other")
    drill = {"inline_keyboard": [[{"text": "📊 Intervals", "callback_data": "drill:intervals:1:jam"}]]}
    assert outbox.update("111", a, "✓ Pain 0/10 logged", drill) is True
    rows = {r["id"]: r for r in outbox.read("jam")}
    assert rows[a]["text"] == "✓ Pain 0/10 logged" and rows[a]["orig_text"] == "Injury pain during (0-10)"
    assert rows[a]["buttons"] == [[{"text": "📊 Intervals", "data": "drill:intervals:1:jam"}]]
    assert rows[b]["text"] == "other"
    assert outbox.update("111", "nope", "x") is False


def test_signup_chat_is_kept_then_moves_into_the_athlete_folder(tmp_path, monkeypatch):
    cfg = setup(tmp_path, monkeypatch, {"jam": {"chat_id": "111", "active": True}})
    cid = "web-0a1b2c3d4e"
    kb = {"inline_keyboard": [[{"text": "Yes", "callback_data": "ob:icu_has:yes"}]]}
    assert outbox.signing_up(cid) is True and outbox.signing_up("111") is False
    q = outbox.record(cid, "Do you use Intervals.icu?", kb)
    outbox.record(cid, "", photo=b"PNG")
    outbox.record_answer(cid, "Yes, I use it")
    outbox.clear_buttons(cid)
    rows = outbox.read_chat(cid)
    assert [r.get("who") for r in rows] == [None, None, "me"]
    assert rows[0]["id"] == q and rows[0]["buttons"] == []
    assert outbox.media_path(cid, rows[1]["photo"]).read_bytes() == b"PNG"
    assert outbox.record_answer("111", "x") is None          # an athlete's answers live in history.json

    # sign-up finished: the athlete exists (not active yet), the chat moves in
    cfg.write_text(json.dumps({"jam": {"chat_id": "111"}, "sam": {"chat_id": cid, "active": False}}))
    outbox._CACHE.update(mtime=None, data={})
    outbox.adopt(cid, "sam")
    moved = outbox.read("sam")
    assert [r["text"] for r in moved] == ["Do you use Intervals.icu?", "", "Yes, I use it"]
    assert (tmp_path / "athletes" / "sam" / "web-media" / moved[1]["photo"]).read_bytes() == b"PNG"
    assert not (tmp_path / "config" / "web-signup" / cid).exists()
    assert outbox.signing_up(cid) is True                     # waiting for approval


def test_only_web_chat_ids_get_a_signup_folder(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, {})
    assert outbox.record("web-../../etc", "x") is None
    assert outbox.record("12345", "x") is None
