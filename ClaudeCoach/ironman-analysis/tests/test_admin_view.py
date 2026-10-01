"""Peak's Athletes & usage page (lib/admin_view.py, lib/usage_ledger.py; Jamie, 1 Oct 2026).

Pinned: every person gets a row with the right sign-up stage (invited, signing up at a
named question, waiting for approval, test week, coached with race or goal, tracking
only); chat comes from the athlete's ledger with the allowance or "exempt"; and costs are
priced per day from transcripts, chat separately, from 27 Sep only.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import admin_view  # noqa: E402
import goals  # noqa: E402
import usage_ledger  # noqa: E402

TODAY = date(2026, 10, 1)


def _base(tmp_path):
    for slug in ("jamie", "tess", "kat", "newbie", "bea"):
        (tmp_path / "athletes" / slug / "telegram").mkdir(parents=True)
    (tmp_path / "athletes" / "jamie" / "telegram" / "history.json").write_text(json.dumps(
        [{"user": "hi", "assistant": "hello", "ts": "2026-10-01T09:43:10"},
         {"user": "", "assistant": "scheduled", "ts": "2026-10-01T18:00:00"}]))
    (tmp_path / "athletes" / "jamie" / "chat-usage.json").write_text(json.dumps(
        {"2026-10": {"usd": 8.9, "replies": 12}, "2026-09": {"usd": 52.5, "replies": 37}}))
    (tmp_path / "athletes" / "bea" / "baseline.json").write_text(json.dumps(
        {"status": "active", "window": {"start": "2026-09-28", "end": "2026-10-04"}}))
    (tmp_path / "config" / "web-signup" / "web-s1").mkdir(parents=True)
    (tmp_path / "config" / "web-signup" / "web-s1" / "web-outbox.jsonl").write_text("{}\n")
    return tmp_path


ATHLETES = {
    "jamie": {"name": "Jamie Diamond", "chat_id": "100", "active": True,
              "race_date": "2027-04-04", "race_name": "Brighton Marathon"},
    "tess": {"name": "Tess Goal", "chat_id": "web-t", "active": True,
             "goal": goals.make("ftp", today=date(2026, 9, 1), start=date(2026, 9, 7))},
    "kat": {"name": "Kat", "chat_id": "200", "active": True, "planning_paused": True},
    "newbie": {"name": "New Person", "chat_id": "web-n", "active": False},
    "bea": {"name": "Bea", "chat_id": "web-b", "active": True},
}
USERS = {
    "jamie@x.uk": {"slug": "jamie", "coach": True},
    "tess@x.uk": {"chat_id": "web-t", "invited": "2026-09-01T10:00:00"},
    "kat@x.uk": {"slug": "kat"},
    "newbie@x.uk": {"chat_id": "web-n", "invited": "2026-09-30T10:00:00"},
    "bea@x.uk": {"chat_id": "web-b", "invited": "2026-09-27T10:00:00"},
    "sam@x.uk": {"chat_id": "web-s1", "invited": "2026-09-30T16:17:35"},
    "duncan@x.uk": {"chat_id": "web-d", "invited": "2026-09-30T17:34:43"},
}
PENDING = ["web-s1", "web-d"]
ONBOARDING = {"web-s1": {"current_key": "icu_key", "answers": {"name": "Sam Test"}}}
COSTS = {"jamie": {"month": 20.0, "rate_month": 316.4, "total": 90.1},
         "kat": {"month": 2.0, "rate_month": 45.5, "total": 12.0}}


def _rows(tmp_path):
    return {r["name"]: r for r in admin_view.rows(USERS, ATHLETES, PENDING, ONBOARDING, COSTS,
                                                  today=TODAY, admin_chat_id="100",
                                                  base=_base(tmp_path))}


def test_every_person_has_the_right_stage(tmp_path):
    r = _rows(tmp_path)
    assert r["Jamie Diamond"]["stage"] == "Coached · Brighton Marathon"
    assert r["Tess Goal"]["stage"] == "Coached · goal: Raise my FTP"
    assert r["Kat"]["stage"] == "Tracking only"
    assert r["New Person"]["stage"] == "Signed up · waiting for your approval"
    assert r["Bea"]["stage"] == "Test week · ends 2026-10-04"
    assert r["Sam Test"]["stage"] == "Signing up · at Intervals.icu key"
    assert r["duncan"]["stage"] == "Invited 2026-09-30 · not started"


def test_order_is_coached_first_invited_last(tmp_path):
    kinds = [x["stage_kind"] for x in admin_view.rows(
        USERS, ATHLETES, PENDING, ONBOARDING, COSTS, today=TODAY, base=_base(tmp_path))]
    assert kinds == sorted(kinds, key=lambda k: admin_view.ORDER[k])
    assert kinds[0] == "coached" and kinds[-1] == "invited"


def test_chat_and_cost_columns(tmp_path):
    r = _rows(tmp_path)
    j = r["Jamie Diamond"]
    assert j["chat"] == {"replies": 12, "usd": 8.9, "allowance": 20.0, "exempt": True}
    assert j["cost"] == {"month": 20.0, "rate_month": 316.4, "total": 90.1}
    assert j["last_message"] == "2026-10-01T09:43"          # the athlete's line, not the 18:00 card
    assert r["Kat"]["chat"]["exempt"] is False and r["Kat"]["chat"]["replies"] == 0
    assert r["duncan"]["chat"] is None and r["duncan"]["cost"] is None
    assert r["Sam Test"]["last_message"]                      # sign-up chat mtime


def test_totals_add_the_shared_jobs():
    t = admin_view.totals(dict(COSTS, system={"month": 1, "rate_month": 10, "total": 3}))
    assert t == {"month": 23.0, "rate_month": 371.9, "total": 105.1}


# ── usage_ledger: per-day pricing from transcripts ─────────────────────────────

def _transcript(path, first_prompt, msgs):
    rows = [{"type": "user", "message": {"content": first_prompt}}]
    for i, (ts, out_tok) in enumerate(msgs):
        rows.append({"type": "assistant", "timestamp": ts, "message": {
            "id": f"m{i}", "model": "claude-sonnet-5-5",
            "usage": {"input_tokens": 0, "output_tokens": out_tok,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
        rows.append(rows[-1])                    # the CLI writes a response on several rows
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_ledger_prices_per_day_chat_separately_from_27_sep(tmp_path, monkeypatch):
    ur = usage_ledger._report()
    proj = tmp_path / "-Users-diamondpeakconsulting-diamondpeak-site"
    proj.mkdir()
    monkeypatch.setattr(ur, "PROJECTS", tmp_path)
    monkeypatch.setattr(ur, "PROJECT_GLOBS", ("-Users-diamondpeakconsulting-diamondpeak-site",))
    usage_ledger._cache.clear()
    ath = {"jamie": {"name": "Jamie Diamond"}}
    # 100k output tokens on Sonnet 5.5 = $1.00
    _transcript(proj / "a.jsonl", "You are ClaudeCoach, Jamie's coach",
                [("2026-09-20T10:00:00Z", 100000),      # before logging began: not counted
                 ("2026-09-30T10:00:00Z", 100000), ("2026-10-01T10:00:00Z", 200000)])
    _transcript(proj / "b.jsonl", "Session sync for athletes/jamie/ ...",
                [("2026-10-01T05:00:00Z", 50000)])
    s = usage_ledger.summary(ath, today=TODAY)["athletes"]["jamie"]
    assert s["total"] == 3.5 and s["month"] == 2.5 and s["chat_month"] == 2.0
    assert s["chat_by_month"] == {"2026-09": 1.0, "2026-10": 2.0}
    assert s["rate_month"] == 3.5                            # actual, not scaled
    usage_ledger._cache.clear()
