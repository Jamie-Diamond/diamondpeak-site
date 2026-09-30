"""Tests for lib/strava_link.py. Run: python3 -m pytest ClaudeCoach/lib/test_strava_link.py"""
import json

import strava_link


def test_state_is_tied_to_the_chat_and_expires(tmp_path, monkeypatch):
    cfg = tmp_path / "strava_app.json"
    cfg.write_text(json.dumps({"client_id": 1, "client_secret": "x"}))
    monkeypatch.setattr(strava_link, "APP_CONFIG", cfg)
    s = strava_link.make_state("web-abc123", now=1000)
    assert strava_link.check_state(s, "web-abc123", now=1010)
    assert not strava_link.check_state(s, "web-other1", now=1010)
    assert not strava_link.check_state(s, "web-abc123", now=1000 + strava_link.STATE_TTL + 1)
    tampered = s[:-1] + ("1" if s[-1] == "0" else "0")
    assert not strava_link.check_state(tampered, "web-abc123", now=1010)
    assert not strava_link.check_state("junk", "web-abc123")
