"""Tests for lib/icu_link.py and IcuClient's OAuth path. Run: python3 -m pytest ClaudeCoach/lib/test_icu_link.py"""
import json

import pytest

import icu_link
import outbox
from icu_api import IcuClient


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "athletes" / "jamie").mkdir(parents=True)
    (tmp_path / "config" / "athletes.json").write_text(json.dumps(
        {"jamie": {"chat_id": "111", "icu_athlete_id": "i196362", "icu_api_key": "k"}}))
    app = tmp_path / "icu-app.json"
    app.write_text(json.dumps({"client_id": "1374", "client_secret": "s3cret",
                               "webhook_secret": "wh", "redirect_uri": "https://x/cb"}))
    monkeypatch.setattr(icu_link, "BASE", tmp_path)
    monkeypatch.setattr(icu_link, "APP_CONFIG", app)
    monkeypatch.setattr(outbox, "BASE", tmp_path)
    monkeypatch.setattr(outbox, "ATHLETES_CONFIG", tmp_path / "config" / "athletes.json")
    monkeypatch.setattr(outbox, "SIGNUP_DIR", tmp_path / "config" / "web-signup")
    outbox._CACHE.update(mtime=None, data={})
    icu_link._INDEX["sig"] = None
    return tmp_path


def test_state_is_tied_to_the_chat_and_expires(cc):
    s = icu_link.make_state("web-abc123", now=1000)
    assert icu_link.check_state(s, "web-abc123", now=1010)
    assert not icu_link.check_state(s, "web-other1", now=1010)
    assert not icu_link.check_state(s, "web-abc123", now=1000 + icu_link.STATE_TTL + 1)
    assert not icu_link.check_state(s[:-1] + ("1" if s[-1] == "0" else "0"), "web-abc123", now=1010)
    assert not icu_link.check_state("junk", "web-abc123")


def test_authorize_url_asks_for_comma_separated_scopes_and_the_registered_redirect(cc):
    u = icu_link.authorize_url("web-abc123")
    assert u.startswith("https://intervals.icu/oauth/authorize?client_id=1374")
    assert "redirect_uri=https%3A%2F%2Fx%2Fcb" in u
    assert "scope=ACTIVITY%3AWRITE%2CWELLNESS%3AREAD%2CCALENDAR%3AWRITE%2CSETTINGS%3AWRITE" in u


def test_missing_scopes_and_athlete_ids():
    assert icu_link.missing_scopes("ACTIVITY:WRITE,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:WRITE") == []
    assert icu_link.missing_scopes("ACTIVITY:READ,CALENDAR:WRITE") == ["WELLNESS", "SETTINGS"]
    assert icu_link.same_athlete("i196362", "196362") and icu_link.same_athlete("I196362", "i196362")
    assert not icu_link.same_athlete("i196362", "i19636") and not icu_link.same_athlete("", "")


def test_a_saved_token_is_found_by_athlete_id_for_signups_and_athletes(cc):
    icu_link.save_tokens("web-abc123", {"access_token": "tok-new", "scope": "ACTIVITY:WRITE",
                                        "athlete": {"id": "i777", "name": "Robin"}})
    f = cc / "config" / "web-signup" / "web-abc123" / "icu_tokens.json"
    assert f.exists() and oct(f.stat().st_mode & 0o777) == "0o600"
    assert icu_link.token_for_athlete("i777") == "tok-new" and icu_link.token_for_athlete("777") == "tok-new"
    icu_link.save_tokens("111", {"access_token": "tok-j", "athlete": {"id": "196362"}})
    assert (cc / "athletes" / "jamie" / "icu_tokens.json").exists()
    assert icu_link.token_for_athlete("i196362") == "tok-j"
    assert icu_link.token_for_athlete("i999") is None
    assert icu_link.connected(slug="jamie") and icu_link.load("web-abc123")["icu_name"] == "Robin"


def test_icu_client_uses_a_key_when_there_is_one_else_the_stored_token(cc):
    icu_link.save_tokens("111", {"access_token": "tok-j", "athlete": {"id": "i196362"}})
    keyed = IcuClient("i196362", "k")
    assert keyed.auth == "key" and keyed.session.auth == ("API_KEY", "k")
    keyless = IcuClient("i196362", "")
    assert keyless.auth == "oauth" and keyless.session.headers["Authorization"] == "Bearer tok-j"
    explicit = IcuClient("0", "k", token="t2")
    assert explicit.auth == "oauth" and explicit.session.headers["Authorization"] == "Bearer t2"
    nothing = IcuClient("i5", "")                      # no key, no token: 401 at call time, as before
    assert nothing.auth == "key"


def test_adopt_moves_the_signup_token_into_the_athlete_folder(cc):
    icu_link.save_tokens("web-abc123", {"access_token": "tok-r", "athlete": {"id": "i777"}})
    outbox.adopt("web-abc123", "robin")
    assert (cc / "athletes" / "robin" / "icu_tokens.json").exists()
    assert icu_link.token_for_athlete("i777") == "tok-r"


def test_revoke_forgets_the_token_even_when_intervals_does_not_answer(cc, monkeypatch):
    icu_link.save_tokens("111", {"access_token": "tok-j", "athlete": {"id": "i196362"}})
    sent = []
    monkeypatch.setattr(icu_link, "revoke_token", lambda t: sent.append(t) or False)
    assert icu_link.revoke(slug="jamie") is False
    assert sent == ["tok-j"] and not icu_link.connected(slug="jamie")
    assert icu_link.token_for_athlete("i196362") is None
    assert icu_link.revoke(slug="jamie") is True       # nothing left to revoke


def test_webhook_secret_must_match(cc):
    assert icu_link.webhook_ok({"secret": "wh", "events": []})
    assert not icu_link.webhook_ok({"secret": "nope"}) and not icu_link.webhook_ok({})
    app = json.loads(icu_link.APP_CONFIG.read_text())
    icu_link.APP_CONFIG.write_text(json.dumps({**app, "webhook_auth_header": "Bearer h"}))
    assert not icu_link.webhook_ok({"secret": "wh"}, "")
    assert icu_link.webhook_ok({"secret": "wh"}, "Bearer h")


def test_every_call_is_counted_per_athlete(cc, monkeypatch, tmp_path):
    import icu_api
    logs = tmp_path / "logs" / "icu-requests"
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(icu_api, "_COUNT_DIR", str(logs))
    monkeypatch.delenv("CC_ICU_COUNT", raising=False)

    class R:
        status_code = 200
        headers = {"X-RateLimit-Remaining": "2499,4990"}

        class request:
            method = "GET"
            path_url = "/api/v1/athlete/i1/wellness?oldest=x"
    icu_api.count_request("i1", "oauth", R)
    line = next(logs.iterdir()).read_text().split()
    assert line[1:] == ["i1", "oauth", line[3], "GET", "/api/v1/athlete/i1/wellness", "200", "2499,4990"]
