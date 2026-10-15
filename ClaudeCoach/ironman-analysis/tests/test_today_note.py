"""lib/today_note.py: the morning card on Peak's Today tab (Jamie, 15 Oct 2026)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import outbox  # noqa: E402
import today_note as tn  # noqa: E402


def test_note_is_todays_only_and_opens_once_per_version(tmp_path, monkeypatch):
    monkeypatch.setattr(tn, "BASE", tmp_path)
    assert tn.read("tess") is None
    tn.write("tess", "Fresh after the week. Hold the first hour back.", "plan")
    assert tn.read("tess")["stage"] == "plan"
    assert tn.mark_opened("tess")["text"].startswith("Fresh")
    assert tn.mark_opened("tess") is None                 # already in the chat
    tn.write("tess", "Slept well. Go for it.", "wellness")
    assert tn.mark_opened("tess")["stage"] == "wellness"  # a new version opens again
    from datetime import date
    assert tn.read("tess", date(2020, 1, 1)) is None


def test_headline_is_the_first_sentence_plain():
    assert tn.headline("*Big ride* today: hold back. ⚠️ HRV down.") == "Big ride today: hold back."


def test_coach_sees_the_note_and_never_the_ping(tmp_path, monkeypatch):
    monkeypatch.setattr(tn, "BASE", tmp_path)
    monkeypatch.setattr(outbox, "BASE", tmp_path)
    d = tmp_path / "athletes" / "tess"
    d.mkdir(parents=True)
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    (d / outbox.OUTBOX_NAME).write_text(json.dumps({"ts": now, "text": "Big ride today.",
                                                    "source": tn.PING}) + "\n")
    tn.write("tess", "Big ride today. Fuel from the start.", "wellness")
    texts = [h["assistant"] for h in outbox.with_sent([], "tess")]
    assert texts == ["Big ride today. Fuel from the start."]
