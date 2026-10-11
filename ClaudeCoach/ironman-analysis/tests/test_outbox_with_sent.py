"""lib/outbox.with_sent: the coach sees every message Peak showed the athlete (11 Oct 2026:
a rule check and the weekly summaries never reached history.json)."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import outbox  # noqa: E402


def test_unlogged_sent_messages_join_history_in_time_order(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "BASE", tmp_path)
    d = tmp_path / "athletes" / "tess"
    d.mkdir(parents=True)
    t = lambda h: (datetime.now() - timedelta(hours=h)).isoformat(timespec="seconds")
    rows = [{"ts": t(5), "text": "Good morning", "source": "notify"},           # in history
            {"ts": t(3), "text": "Quick check: still right?", "source": "rule-check"},
            {"ts": t(2), "text": "dev talk", "source": "dev-session"},
            {"ts": t(2), "text": "a tap in Dev", "source": "web-turn", "tab": "dev"},
            {"ts": t(1), "text": "Week ending - MIXED", "source": "weekly-summary"}]
    (d / "web-outbox.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    hist = [{"user": "", "assistant": "Good morning", "ts": t(5)},
            {"user": "hi", "assistant": "hello", "ts": t(2.5)}]
    out = outbox.with_sent(hist, "tess")
    assert [h["assistant"] for h in out] == [
        "Good morning", "Quick check: still right?", "hello", "Week ending - MIXED"]
    assert len(hist) == 2                                        # the caller's list untouched
