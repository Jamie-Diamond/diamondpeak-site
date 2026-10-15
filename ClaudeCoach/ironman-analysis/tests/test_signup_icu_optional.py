"""Sign-up stops for Intervals.icu set-up (watch, sleep, workouts) but never for history
(Jamie, 15 Oct 2026: people waited days for Garmin's export email). History is optional
and asked for after sign-up."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "telegram"))
sys.path.insert(0, str(REPO / "lib"))
import bot  # noqa: E402


def _acts(days_back):
    from datetime import date, timedelta
    d = (date.today() - timedelta(days=days_back)).isoformat()
    return [{"source": "GARMIN_CONNECT", "start_date_local": d + "T08:00:00"}]


def test_short_history_is_optional():
    profile = {"icu_garmin_health": True, "garmin_download_wellness": True,
               "garmin_upload_workouts": True}
    checks = bot._icu_checks(profile, _acts(20), [{"hrv": 40}])
    h = [c for c in checks if c["key"] == "history"][0]
    assert not h["ok"] and h.get("optional")


def test_setup_issues_never_include_history(monkeypatch):
    monkeypatch.setattr(bot, "_strava_linked", lambda cid: False)

    class Fake:
        def __init__(self, *a, **k): pass
        def fetch_all(self, *a):
            return ({"garmin_download_wellness": True}, _acts(20), [{"hrv": 40}])
    import icu_api
    monkeypatch.setattr(icu_api, "IcuClient", Fake)
    assert not any("history" in i.lower() or "old data" in i
                   for i in bot._icu_setup_issues("i1", "", "web-x"))


def _linked(monkeypatch, issues):
    asked, after = [], []
    monkeypatch.setattr(bot, "_fetch_icu_data", lambda i, k: ({}, "summary"))
    monkeypatch.setattr(bot, "_icu_setup_issues", lambda *a: issues)
    monkeypatch.setattr(bot, "_ob_ask", lambda t, c, key, q, **k: asked.append(key))
    monkeypatch.setattr(bot, "save_onboarding_state", lambda s: None)
    monkeypatch.setattr(bot, "_ob_after_icu", lambda *a: after.append(1) or True)
    session = {"answers": {}}
    bot._ob_icu_linked("t", "web-x", {"web-x": session}, session, "i1", "")
    return asked, after


def test_sign_up_stops_for_the_watch(monkeypatch):
    assert _linked(monkeypatch, ["I can't see any activities yet."]) == (["icu_fix"], [])


def test_sign_up_goes_on_when_only_history_is_short(monkeypatch):
    assert _linked(monkeypatch, []) == ([], [1])   # history never reaches the issues


def test_after_sign_up_nothing_sent_when_all_in(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "_icu_checklist", lambda *a: [{"ok": True, "fix": ""}])
    monkeypatch.setattr(bot, "send", lambda *a, **k: sent.append(a))
    bot._icu_after_signup("t", "web-x", "nobody")
    assert sent == []


def test_after_sign_up_history_only_is_worded_optional(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "_icu_checklist", lambda *a: [
        {"ok": True, "fix": ""}, {"ok": False, "optional": True, "fix": "Download old data"}])
    monkeypatch.setattr(bot, "send", lambda t, c, text, **k: sent.append((text, k)))
    bot._icu_after_signup("t", "web-x", "nobody")
    text, k = sent[0]
    assert "optional" in text and "no need to wait" in text
    assert k["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == bot.ICU_RECHECK
