"""Cost and messages per model for Peak's Athletes & usage page (16 Oct 2026, Jamie:
"track costs and messages per model ... for this to be viable this is key")."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import usage_ledger as ul  # noqa: E402

MTOK = {"input_tokens": 1_000_000, "output_tokens": 0}       # $2 on Sonnet 5.5, $0.10 on Haiku


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _user(text, ts):
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


def _tool_result(ts):
    return {"type": "user", "timestamp": ts, "message": {"content": [{"type": "tool_result"}]}}


def _asst(mid, model, ts):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "model": model, "usage": MTOK}}


def test_a_chat_message_is_one_turn_on_its_first_model(tmp_path):
    f = tmp_path / "chat.jsonl"
    _write(f, [_user("You are ClaudeCoach, Fred. How was my ride?", "2026-10-16T08:00:00Z"),
               _asst("a1", "claude-opus-5-5", "2026-10-16T08:00:05Z"),     # tool call
               _tool_result("2026-10-16T08:00:06Z"),
               _asst("a2", "claude-opus-5-5", "2026-10-16T08:00:09Z"),     # the answer
               _user("thanks", "2026-10-16T09:00:00Z"),
               _asst("a3", "claude-sonnet-5-5", "2026-10-16T09:00:03Z")])
    models = ul._price_file(str(f), {"fred": {"name": "Fred Hayes"}}, set())[5]
    assert models[("2026-10-16", "chat", "opus")][1] == 1        # two calls, one message
    assert models[("2026-10-16", "chat", "sonnet")] == (pytest.approx(2.0), 1)


def test_a_scheduled_run_is_one_message_on_the_model_that_cost_most(tmp_path):
    f = tmp_path / "job.jsonl"
    _write(f, [_user("Morning card for athletes/fred/", "2026-10-16T06:00:00Z"),
               _asst("b1", "claude-haiku-5-5", "2026-10-16T06:00:02Z"),
               _asst("b2", "claude-sonnet-5-5", "2026-10-16T06:00:05Z")])
    models = ul._price_file(str(f), {"fred": {"name": "Fred Hayes"}}, set())[5]
    assert models[("2026-10-16", "scheduled", "sonnet")][1] == 1
    assert models[("2026-10-16", "scheduled", "haiku")][1] == 0


def _d(rows_by_athlete):
    from collections import defaultdict
    out = {}
    for s, rows in rows_by_athlete.items():
        v = {"all": defaultdict(float), "models": defaultdict(lambda: [0.0, 0])}
        for day, kind, model, usd, n in rows:
            v["all"][day] += usd
            v["models"][(day, kind, model)][0] += usd
            v["models"][(day, kind, model)][1] += n
        out[s] = v
    return out


def test_model_view_averages_coached_athletes_and_gives_the_margin(monkeypatch, tmp_path):
    monkeypatch.setattr(ul, "_pricing", lambda: {"price_gbp_incl_vat": 45, "vat_rate": 0.2,
                                                  "usd_per_gbp": 1.325})
    import planning_pause
    monkeypatch.setattr(planning_pause, "is_paused", lambda s, cfg=None, path=None: s == "calum")
    today = date(2026, 10, 9)
    d = _d({"fred":  [("2026-10-03", "chat", "opus", 9.0, 30), ("2026-10-03", "scheduled", "haiku", 1.0, 100)],
            "james": [("2026-10-03", "chat", "sonnet", 4.0, 30), ("2026-10-03", "scheduled", "haiku", 1.0, 100)],
            "jamie": [("2026-10-03", "chat", "opus", 99.0, 300)],     # the coach: left out
            "calum": [("2026-10-03", "scheduled", "haiku", 0.5, 10)]})  # tracking only: left out
    v = ul.model_view(d, {s: {"name": s.title()} for s in d}, "jamie", today)
    assert v["athletes"] == ["Fred", "James"]
    k = ul.DAYS_PER_MONTH / ul.MODEL_WINDOW_DAYS                   # the window, to a month
    assert v["avg_month"] == pytest.approx((10 + 5) / 2 * k, abs=0.01)
    assert v["margin"] == pytest.approx(1 - v["avg_month"] / (45 / 1.2 * 1.325), abs=0.001)
    rows = {(r["kind"], r["model"]): r for r in v["rows"]}
    assert list(rows) == [("chat", "sonnet"), ("chat", "opus"), ("scheduled", "haiku")]
    assert rows[("chat", "opus")]["per_msg"] == pytest.approx(0.3)
    assert rows[("scheduled", "haiku")]["msgs"] == pytest.approx(100 * k, abs=0.1)


def test_the_repo_has_a_price():
    p = json.loads((REPO / "config" / "pricing.json").read_text())
    assert p["price_gbp_incl_vat"] > 0 and 0 <= p["vat_rate"] < 1 and p["usd_per_gbp"] > 0
