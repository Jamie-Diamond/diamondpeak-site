"""Tests for lib/outbox.py. Run: python3 -m pytest ClaudeCoach/lib/test_outbox.py"""
import json

import outbox


def setup(tmp_path, monkeypatch, athletes):
    cfg = tmp_path / "athletes.json"
    cfg.write_text(json.dumps(athletes))
    monkeypatch.setattr(outbox, "BASE", tmp_path)
    monkeypatch.setattr(outbox, "ATHLETES_CONFIG", cfg)
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
