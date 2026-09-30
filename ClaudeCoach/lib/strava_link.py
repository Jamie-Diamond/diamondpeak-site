"""Strava for athletes whose watch can't talk to Intervals.icu directly (Apple Watch...).

Peak's Connect Strava button (api/server.py, /api/strava/connect -> Strava's approval
page -> /api/strava/callback) stores the athlete's Strava tokens where StravaClient
reads them, and scripts/strava-to-icu.py copies each Strava workout into their
Intervals.icu as a file upload, so the rest of ClaudeCoach reads it like any other
activity. Activities Intervals.icu pulls from Strava itself are hidden from its API;
uploaded copies are not.

Jamie decided this on 30 Sep 2026 knowing Strava's API terms bar its data from AI
use. The Strava app allows 10 connected athletes without Strava's review (Jamie is one).

Tokens: athletes/<slug>/strava_tokens.json, or config/web-signup/<chat id>/ while the
athlete is still signing up (moved into their folder by outbox.adopt()).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import outbox

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
APP_CONFIG = BASE / "config" / "strava_app.json"
TOKENS_NAME = "strava_tokens.json"
AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
PEAK = "https://coach.diamondpeak.uk"
CONNECT_URL = PEAK + "/api/strava/connect"
REDIRECT_URI = PEAK + "/api/strava/callback"
SCOPE = "read,activity:read_all"
CAPACITY = 10                                              # the Strava app's athlete limit
STATE_TTL = 30 * 60


def _app() -> dict:
    return json.loads(APP_CONFIG.read_text())


def _sign(msg: str) -> str:
    key = str(_app()["client_secret"]).encode()
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(chat_id: str, now: float | None = None) -> str:
    """Ties Strava's reply to the Peak user who asked: chat id, time, signature."""
    ts = str(int(now if now is not None else time.time()))
    return f"{chat_id}.{ts}.{_sign(chat_id + '.' + ts)}"


def check_state(state: str, chat_id: str, now: float | None = None) -> bool:
    try:
        cid, ts, sig = state.rsplit(".", 2)
    except ValueError:
        return False
    fresh = (now if now is not None else time.time()) - int(ts) < STATE_TTL if ts.isdigit() else False
    return cid == chat_id and fresh and hmac.compare_digest(sig, _sign(cid + "." + ts))


def authorize_url(chat_id: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": _app()["client_id"], "redirect_uri": REDIRECT_URI,
        "response_type": "code", "approval_prompt": "auto", "scope": SCOPE,
        "state": make_state(chat_id)})


def exchange(code: str) -> dict:
    """Strava's authorization code -> tokens (access, refresh, expires_at, athlete)."""
    app = _app()
    data = urllib.parse.urlencode({"client_id": app["client_id"], "client_secret": app["client_secret"],
                                   "code": code, "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def token_file(chat_id) -> Path | None:
    d = outbox._chat_dir(chat_id)
    return d / TOKENS_NAME if d else None


def connected(chat_id) -> bool:
    f = token_file(chat_id)
    return bool(f and f.exists())


def save_tokens(chat_id, tok: dict, scope: str = "") -> None:
    f = token_file(chat_id)
    if not f:
        raise LookupError("no athlete for this chat")
    f.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: tok.get(k) for k in ("access_token", "refresh_token", "expires_at")}
    keep["strava_athlete_id"] = (tok.get("athlete") or {}).get("id")
    keep["scope"] = scope
    keep["connected"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, indent=2))
    tmp.chmod(0o600)
    tmp.replace(f)


def connected_count() -> int:
    """Athletes holding a Strava connection, signed up or signing up (the app's cap is 10)."""
    n = len(list((BASE / "athletes").glob("*/" + TOKENS_NAME)))
    if outbox.SIGNUP_DIR.is_dir():
        n += len(list(outbox.SIGNUP_DIR.glob("*/" + TOKENS_NAME)))
    return n
