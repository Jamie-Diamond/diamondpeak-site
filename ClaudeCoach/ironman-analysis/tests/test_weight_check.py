"""lib/weight_check.py: weigh-ins that can't be the athlete (Jamie, 15 Oct 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import weight_check as W  # noqa: E402

FRED = [{"date": "2026-08-03", "kg": 87.6}, {"date": "2026-09-25", "kg": 79.5},
        {"date": "2026-09-26", "kg": 79.5}, {"date": "2026-10-01", "kg": 86.5}]


def test_freds_two_low_mornings_are_suspect_together():
    s = W.suspects(FRED)
    assert len(s) == 1 and s[0]["dates"] == ["2026-09-25", "2026-09-26"] and s[0]["kg"] == 79.5


def test_ten_percent_over_three_months_is_fine():
    r = [{"date": "2026-01-01", "kg": 90}, {"date": "2026-04-01", "kg": 81},
         {"date": "2026-07-01", "kg": 80}]
    assert W.suspects(r) == []


def test_normal_daily_noise_is_fine():
    r = [{"date": f"2026-10-{d:02d}", "kg": 82 + (1.2 if d % 2 else -0.8)} for d in range(1, 20)]
    assert W.suspects(r) == []


def test_approval_puts_them_back(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "BASE", tmp_path)
    (tmp_path / "athletes" / "x").mkdir(parents=True)
    kept, held = W.keep("x", FRED)
    assert len(kept) == 2 and len(held) == 1
    assert W.to_ask("x", FRED) and not (W.mark_asked("x", ["2026-09-25", "2026-09-26"]) or W.to_ask("x", FRED))
    W.answer("x", ["2026-09-25", "2026-09-26"], True)
    kept, held = W.keep("x", FRED)
    assert len(kept) == 4 and held == []
    assert "79.5 kg on 25 Sep to 26 Sep" in W.question(W.suspects(FRED)[0])
