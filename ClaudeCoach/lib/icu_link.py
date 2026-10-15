"""Intervals.icu OAuth: the athlete links their own Intervals.icu to Peak, no API key.

Peak's Connect Intervals.icu (api/server.py, /api/icu/connect -> Intervals.icu's approval
page -> /api/icu/callback) stores the athlete's token where IcuClient finds it, so the
rest of ClaudeCoach reads and writes their Intervals.icu exactly as it does with a key.

The app is "Diamond Peak Coach", client id 1374, approved by Intervals.icu 15 Oct 2026
and managed by Jamie at intervals.icu -> Settings -> Manage App. Its secrets live on the
VM only, in /etc/claudecoach/icu-app.json (never in this public repo).

Facts from Intervals.icu's own forum (thread 2759 and the API thread 609), checked 15 Oct:
  - Tokens never expire and there is no refresh token. A token stops working only when
    the athlete revokes it, or authorises the app again (a new token replaces the old).
  - The code from the callback must be swapped for a token within 2 minutes.
  - "0" as the athlete id means the token's own athlete.
  - A token reaches only the athlete who authorised it, never the people they coach.
  - Rate limits (posted 24 Jun 2026 as a draft, not live on 15 Oct: no X-RateLimit
    headers came back): an OAuth app gets 100 calls per athlete per day POOLED across
    the app, minimum 5000/day, and 1/8 of that per rolling 15 minutes (minimum 2500).
    An API key gets 5000/day of its own. Both share 10 calls/second per IP address.

Tokens: athletes/<slug>/icu_tokens.json, or config/web-signup/<chat id>/ while the
athlete is still signing up (moved into their folder by outbox.adopt()). Mode 600.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import outbox

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
APP_CONFIG = Path(os.environ.get("CC_ICU_APP", "/etc/claudecoach/icu-app.json"))
TOKENS_NAME = "icu_tokens.json"
AUTH_URL = "https://intervals.icu/oauth/authorize"
TOKEN_URL = "https://intervals.icu/api/oauth/token"
DISCONNECT_URL = "https://intervals.icu/api/v1/disconnect-app"
PEAK = "https://coach.diamondpeak.uk"
CONNECT_URL = PEAK + "/api/icu/connect"
REDIRECT_URI = PEAK + "/api/icu/callback"
# Comma-separated (Intervals.icu's format, not spaces). WRITE includes READ.
# ACTIVITY: manual gym sessions, Strava copies, HR fixes. CALENDAR: the plan.
# SETTINGS: FTP / thresholds (lib/thresholds.py, lib/baseline.py). WELLNESS: read only.
SCOPE = "ACTIVITY:WRITE,WELLNESS:READ,CALENDAR:WRITE,SETTINGS:WRITE"
NEEDED = ("ACTIVITY", "WELLNESS", "CALENDAR", "SETTINGS")
STATE_TTL = 30 * 60
UA = "ClaudeCoach (coach.diamondpeak.uk)"


def _app() -> dict:
    return json.loads(APP_CONFIG.read_text())


def configured() -> bool:
    try:
        a = _app()
        return bool(a.get("client_id") and a.get("client_secret"))
    except (OSError, ValueError):
        return False


def _sign(msg: str) -> str:
    key = str(_app()["client_secret"]).encode()
    return hmac.new(key, ("icu:" + msg).encode(), hashlib.sha256).hexdigest()[:32]


def make_state(chat_id: str, now: float | None = None) -> str:
    """Ties Intervals.icu's reply to the Peak user who asked: chat id, time, signature."""
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
    app = _app()
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": app["client_id"], "redirect_uri": app.get("redirect_uri") or REDIRECT_URI,
        "scope": SCOPE, "state": make_state(chat_id)})


def exchange(code: str) -> dict:
    """The callback's code -> {"token_type", "access_token", "scope", "athlete": {id, name}}."""
    app = _app()
    data = urllib.parse.urlencode({"client_id": app["client_id"],
                                   "client_secret": app["client_secret"], "code": code}).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def missing_scopes(granted: str) -> list[str]:
    """The areas the athlete unticked on Intervals.icu's approval page."""
    have = {s.split(":")[0].strip().upper() for s in (granted or "").split(",") if s.strip()}
    return [s for s in NEEDED if s not in have]


def same_athlete(a, b) -> bool:
    """Intervals.icu ids: "i196362" in athletes.json, sometimes bare digits elsewhere."""
    na, nb = str(a or "").strip().lower().lstrip("i"), str(b or "").strip().lower().lstrip("i")
    return bool(na) and na == nb


# ── where tokens live ──

def token_file(chat_id) -> Path | None:
    d = outbox._chat_dir(chat_id)
    return d / TOKENS_NAME if d else None


def save_tokens(chat_id, tok: dict) -> dict:
    f = token_file(chat_id)
    if not f:
        raise LookupError("no athlete for this chat")
    f.parent.mkdir(parents=True, exist_ok=True)
    athlete = tok.get("athlete") or {}
    keep = {"access_token": tok.get("access_token"), "scope": tok.get("scope") or "",
            "icu_athlete_id": str(athlete.get("id") or ""), "icu_name": athlete.get("name") or "",
            "connected": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, indent=2))
    tmp.chmod(0o600)
    tmp.replace(f)
    _INDEX["sig"] = None
    return keep


def load(chat_id=None, slug: str | None = None) -> dict | None:
    f = (BASE / "athletes" / slug / TOKENS_NAME) if slug else token_file(chat_id)
    try:
        d = json.loads(f.read_text()) if f else None
        return d if d and d.get("access_token") else None
    except (OSError, ValueError):
        return None


def connected(chat_id=None, slug: str | None = None) -> bool:
    return load(chat_id, slug) is not None


_INDEX: dict = {"sig": None, "map": {}}


def _token_files() -> list[Path]:
    return (list((BASE / "athletes").glob("*/" + TOKENS_NAME))
            + list((BASE / "config" / "web-signup").glob("*/" + TOKENS_NAME)))


def token_for_athlete(icu_athlete_id) -> str | None:
    """The OAuth token for an Intervals.icu athlete id, wherever it is stored. IcuClient
    uses this when it is given no API key, so every script works for a key-less athlete
    without knowing how they linked."""
    files = _token_files()
    sig = tuple(sorted((str(f), f.stat().st_mtime) for f in files if f.exists()))
    if sig != _INDEX["sig"]:
        m = {}
        for f in files:
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if d.get("access_token") and d.get("icu_athlete_id"):
                m[str(d["icu_athlete_id"]).lower().lstrip("i")] = d["access_token"]
        _INDEX.update(sig=sig, map=m)
    return _INDEX["map"].get(str(icu_athlete_id or "").lower().lstrip("i"))


# ── leaving ──

def revoke(slug: str | None = None, chat_id=None) -> bool:
    """Revoke Peak's access to this athlete's Intervals.icu and forget the token. For
    Settings -> Disconnect and for the GDPR delete script. Returns whether Intervals.icu
    confirmed (a token it already treats as gone, 401/403, counts as confirmed); the local
    token is removed either way. Also stops Intervals.icu's webhooks for the athlete."""
    f = (BASE / "athletes" / slug / TOKENS_NAME) if slug else token_file(chat_id)
    if not f or not f.exists():
        return True
    try:
        ok = revoke_token(json.loads(f.read_text()).get("access_token"))
    except (OSError, ValueError):
        ok = False
    f.unlink(missing_ok=True)
    _INDEX["sig"] = None
    return ok


def revoke_token(token: str | None) -> bool:
    if not token:
        return True
    try:
        req = urllib.request.Request(DISCONNECT_URL, method="DELETE", headers={
            "Authorization": f"Bearer {token}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20):
            return True
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)
    except Exception:
        return False


# ── webhooks (Manage App -> Webhooks; see api/server.py /api/icu/webhook) ──

def webhook_ok(payload: dict, auth_header: str = "") -> bool:
    """Intervals.icu puts the app's webhook secret in the body as "secret" (seen in
    community receivers; the docs don't spell out the shape). A custom Authorization
    header can also be set in Manage App: if one is configured here it must match too."""
    try:
        app = _app()
    except (OSError, ValueError):
        return False
    want = str(app.get("webhook_secret") or "")
    if not want or not hmac.compare_digest(str(payload.get("secret") or ""), want):
        return False
    hdr = str(app.get("webhook_auth_header") or "")
    return not hdr or hmac.compare_digest(auth_header or "", hdr)
