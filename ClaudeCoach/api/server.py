#!/usr/bin/env python3
"""ClaudeCoach web API — the backend behind Peak at coach.diamondpeak.uk.

Phase 0 of docs/app-transition-plan.md. Serves the Peak app (the same files GitHub
Pages serves at /coach/) plus each athlete's FULL PRIVATE data, to that athlete only.

Who is asking is decided by Cloudflare Access, never by the app:

  production  CF_ACCESS_TEAM + CF_ACCESS_AUD set. Every request must carry a
              Cf-Access-Jwt-Assertion JWT signed by the team's Access keys, with our
              app's audience. The email inside it is the identity.
  private     CC_API_DEV_EMAIL set, CF vars not. For testing on the VM over an SSH
              tunnel only: any request that arrived through Cloudflare is refused, so a
              tunnel switched on too early cannot serve data under a fake identity.
  neither     every data request is refused (fail closed).

Email -> athlete comes from web-access.json (VM-only, outside the repo):
  {"users": {"someone@example.com": {"slug": "jamie", "coach": true},
             "new@example.com":     {"chat_id": "web-3f9a1c2b7d", "invited": "..."}}}
A coach may read every active athlete; anyone else reads exactly one slug. A user
invited from Peak has a web chat id instead of a slug: they sign up by chatting
(the bot's own onboarding), and once the coach approves them the slug is found
from athletes.json by that chat id.

Paths mirror the GitHub Pages layout so Peak runs unchanged:
  /coach/...                                  the app itself
  /ClaudeCoach/public/training-data-<slug>.json  PRIVATE file, not the public subset
  /ClaudeCoach/public/nutrition-<slug>.json    PRIVATE athletes/<slug>/nutrition-app.json
  /ClaudeCoach/public/session-library.json
  /api/me                                     who you are, which athletes you may see
  POST /api/refresh/<slug>                    rebuild that athlete's data from Intervals.icu now
  POST /api/chat                              talk to the coach as YOURSELF (see chat.py), SSE
  POST /api/chat/voice                        a voice recording; transcribed, reply spoken back
  POST /api/chat/photo?caption=               a photo, read by the bot's own image path
  POST /api/chat/button                       a tapped button (its callback data)
  GET  /api/chat/history                      your chat: conversation + scheduled messages
  GET  /api/media/<name>                      a photo/chart from your chat
  GET  /api/push/key, POST /api/push/(un)subscribe, POST /api/push/test
  /api/admin/...                              coach only: athletes, Telegram switch, invites
Anything else (the site's other tools) redirects to https://diamondpeak.uk.

Run: uvicorn server:app --host 127.0.0.1 --port 8787  (see system/claudecoach-api.service)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

import chat
import push

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

# CC_HOME / CC_APP_DIR let a test copy run against the live data without being inside
# the live checkout (which cc-gitpull owns).
CC = Path(os.environ.get("CC_HOME") or Path(__file__).resolve().parent.parent)  # ClaudeCoach/
SITE = CC.parent                                     # diamondpeak-site/
APP_DIR = Path(os.environ.get("CC_APP_DIR") or SITE / "coach")
PUBLIC_DIR = CC / "public"
ATHLETES_CONFIG = CC / "config" / "athletes.json"
ACCESS_CONFIG = Path(os.environ.get("CC_WEB_ACCESS", CC / "config" / "web-access.json"))
PENDING_FILE = CC / "config" / "pending.json"
PUBLIC_SITE = "https://diamondpeak.uk"

SLUG_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


def private_training_file(slug: str) -> Path:
    """Where refresh-site-data.py writes the full private payload for `slug`.
    Jamie's lives under athletes/, everyone else's at the ClaudeCoach root."""
    for p in (CC / "athletes" / slug / "training-data.json",
              CC / f"training-data-{slug}.json"):
        if p.exists():
            return p
    return CC / f"training-data-{slug}.json"


# ── identity ──────────────────────────────────────────────────────────────

class _Jwks:
    """Cloudflare Access signing keys, cached for an hour."""

    def __init__(self):
        self._client = None
        self._at = 0.0

    def key_for(self, token: str, team: str):
        import jwt  # PyJWT, only needed in production mode
        if self._client is None or time.time() - self._at > 3600:
            self._client = jwt.PyJWKClient(f"https://{team}.cloudflareaccess.com/cdn-cgi/access/certs")
            self._at = time.time()
        return self._client.get_signing_key_from_jwt(token).key


_jwks = _Jwks()


def request_email(request: Request) -> str:
    team = os.environ.get("CF_ACCESS_TEAM", "").strip()
    aud = os.environ.get("CF_ACCESS_AUD", "").strip()
    if team and aud:
        import jwt
        token = request.headers.get("cf-access-jwt-assertion", "")
        if not token:
            raise HTTPException(401, "not signed in")
        try:
            claims = jwt.decode(token, _jwks.key_for(token, team), algorithms=["RS256"],
                                audience=aud, issuer=f"https://{team}.cloudflareaccess.com")
        except Exception:
            raise HTTPException(401, "sign-in not valid")
        email = (claims.get("email") or "").strip().lower()
        if not email:
            raise HTTPException(401, "sign-in has no email")
        return email

    dev = os.environ.get("CC_API_DEV_EMAIL", "").strip().lower()
    if dev:
        if request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip"):
            raise HTTPException(503, "private test mode: not reachable through Cloudflare")
        return dev

    raise HTTPException(503, "sign-in not configured")


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _users() -> dict:
    return {k.strip().lower(): v for k, v in (_load(ACCESS_CONFIG).get("users") or {}).items()}


def _user(email: str) -> dict:
    return _users().get(email) or {}


def _slug_of(user: dict, athletes: dict) -> str | None:
    """The athlete a user IS: named directly, or found by their web chat id."""
    if user.get("slug"):
        return user["slug"]
    cid = str(user.get("chat_id") or "")
    if cid:
        for s, a in athletes.items():
            if isinstance(a, dict) and str(a.get("chat_id") or "") == cid:
                return s
    return None


def is_coach(email: str) -> bool:
    return bool(_user(email).get("coach"))


def allowed_athletes(email: str) -> list[dict]:
    """Athletes this email may read, as [{slug, name}], in athletes.json order."""
    user = _user(email)
    if not user:
        return []
    athletes = _load(ATHLETES_CONFIG)
    active = [(s, v) for s, v in athletes.items() if isinstance(v, dict) and v.get("active")]
    if not user.get("coach"):
        mine = _slug_of(user, athletes)
        active = [(s, v) for s, v in active if s == mine]
    return [{"slug": s, "name": (v.get("name") or s).split()[0]} for s, v in active]


def own_slug(email: str) -> str | None:
    """The athlete this email IS - who a chat message is from. A coach can VIEW
    everyone, but always talks to the coach as themself."""
    slug = _slug_of(_user(email), _load(ATHLETES_CONFIG))
    return slug if slug in {a["slug"] for a in allowed_athletes(email)} else None


def own_chat(email: str) -> tuple[str | None, str | None, str | None]:
    """(chat_id, slug, state) for the person signed in:
      active      an active athlete - chat, voice, photos, buttons
      waiting     signed up, not yet approved by the coach
      onboarding  invited from Peak, sign-up questions not finished
    or (None, None, None) for an email with no athlete at all."""
    user = _user(email)
    athletes = _load(ATHLETES_CONFIG)
    slug = _slug_of(user, athletes)
    if slug and isinstance(athletes.get(slug), dict):
        a = athletes[slug]
        cid = str(a.get("chat_id") or "")
        if cid:
            return cid, slug, ("active" if a.get("active") else "waiting")
    cid = str(user.get("chat_id") or "")
    if cid and cid in [str(x) for x in (_load_list(PENDING_FILE))]:
        return cid, None, "onboarding"
    return None, None, None


def _load_list(path: Path) -> list:
    try:
        v = json.loads(path.read_text())
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _slug_for_chat(chat_id: str) -> str | None:
    for s, a in _load(ATHLETES_CONFIG).items():
        if isinstance(a, dict) and str(a.get("chat_id") or "") == str(chat_id):
            return s
    return None


def _write_json(path: Path, data) -> None:
    """Atomic, and keeps the file's permissions (web-access.json is 600)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(tmp, path.stat().st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o600)
    tmp.replace(path)


def require_slug(request: Request, slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise HTTPException(404)
    if slug not in {a["slug"] for a in allowed_athletes(request_email(request))}:
        raise HTTPException(403, "not your data")


# ── app ───────────────────────────────────────────────────────────────────

app = FastAPI(title="ClaudeCoach API", docs_url=None, redoc_url=None, openapi_url=None)

NO_STORE = {"Cache-Control": "private, no-cache"}


@app.on_event("startup")
def _start_push():
    if os.environ.get("CC_PUSH_WATCH", "1") == "0":
        return
    try:
        push.start(CC, own_slug, ATHLETES_CONFIG)
    except Exception as e:
        print(f"[push] not started: {e}")


def _reply_while_away(chat_id, text):
    slug = _slug_for_chat(chat_id)
    if slug:
        push.notify(slug, push.plain(text), kind="reply")


chat.on_reply_while_away = _reply_while_away


@app.get("/api/me")
def me(request: Request):
    email = request_email(request)
    athletes = allowed_athletes(email)
    _, own, state = own_chat(email)
    if not athletes and not state:
        raise HTTPException(403, "this email has no athlete")
    return JSONResponse({"email": email, "athletes": athletes, "own": own, "state": state,
                         "coach": is_coach(email), "push": push.subscribed(email),
                         "strava": _strava_state(email)},
                        headers=NO_STORE)


@app.get("/ClaudeCoach/public/training-data-{slug}.json")
def training_data(slug: str, request: Request):
    require_slug(request, slug)
    path = private_training_file(slug)
    if not path.exists():
        raise HTTPException(404, "no data yet")
    return FileResponse(path, media_type="application/json", headers=NO_STORE)


@app.get("/ClaudeCoach/public/nutrition-{slug}.json")
def nutrition(slug: str, request: Request):
    require_slug(request, slug)
    path = CC / "athletes" / slug / "nutrition-app.json"
    if not path.exists():
        raise HTTPException(404)  # opt-in per athlete; Peak treats 404 as "not enabled"
    return FileResponse(path, media_type="application/json", headers=NO_STORE)


# ── pull-to-refresh ──

REFRESH_MIN_GAP_S = 60
REFRESH_TIMEOUT_S = 180
_refresh_lock = threading.Lock()
_last_refresh: dict[str, float] = {}


def run_refresh(slug: str) -> int:
    """refresh-site-data.py --athlete: pure Python ICU pulls, no Claude call."""
    r = subprocess.run([os.environ.get("CC_PYTHON", "/usr/bin/python3"),
                        str(CC / "scripts" / "refresh-site-data.py"), "--athlete", slug],
                       cwd=str(SITE), capture_output=True, text=True, timeout=REFRESH_TIMEOUT_S)
    return r.returncode


@app.post("/api/refresh/{slug}")
def refresh(slug: str, request: Request):
    # A custom header forces a CORS preflight, so another site can't trigger this
    # with the athlete's Cloudflare cookie.
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    require_slug(request, slug)
    with _refresh_lock:
        wait = REFRESH_MIN_GAP_S - (time.time() - _last_refresh.get(slug, 0))
        if wait > 0:
            raise HTTPException(429, f"refreshed moments ago - try again in {int(wait) + 1}s")
        _last_refresh[slug] = time.time()
    try:
        code = run_refresh(slug)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Intervals.icu was too slow - the last data is still shown")
    if code == 3:
        raise HTTPException(409, "a refresh is already running - try again shortly")
    if code != 0:
        raise HTTPException(502, "refresh failed - the last data is still shown")
    return JSONResponse({"ok": True}, headers=NO_STORE)


# ── chat ──

def _sse(kind: str, text: str, extra: dict | None = None) -> str:
    return f"event: {kind}\ndata: {json.dumps({'text': text, **(extra or {})})}\n\n"


def _chat_user(request: Request, need_active: bool = False) -> tuple[str, str]:
    """(chat_id, label) for the signed-in person, or an HTTP error."""
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    email = request_email(request)
    cid, slug, state = own_chat(email)
    if not cid:
        raise HTTPException(403, "this email has no athlete")
    if state == "waiting":
        raise HTTPException(409, "your coach is activating your account - you'll get a message here")
    if need_active and state != "active":
        raise HTTPException(409, "that works once your account is active")
    return cid, slug or email


@app.post("/api/chat")
async def chat_send(request: Request):
    cid, label = _chat_user(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    text = str((body or {}).get("text") or "").strip()[:4000]
    if not text:
        raise HTTPException(400, "empty message")
    return _turn_stream(cid, label, text=text)


@app.post("/api/chat/button")
async def chat_button(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    data = str((body or {}).get("data") or "").strip()[:200]
    # Sign-up question buttons ("ob:...") work before the account is active.
    cid, label = _chat_user(request, need_active=not data.startswith("ob:"))
    if not data:
        raise HTTPException(400, "no button")
    item = str((body or {}).get("item") or "")
    item = item if ITEM_RE.match(item) else None
    return _turn_stream(cid, label, button=data, item=item)


ITEM_RE = re.compile(r"^[0-9]{14}-[0-9a-f]{6}$")
LOG_RANGES = {"r": (1, 10), "p": (0, 10), "c": (0, 150)}


@app.post("/api/chat/log")
async def chat_log(request: Request):
    """Save a Log it card (RPE / pain / carbs for one session) in one go."""
    cid, label = _chat_user(request, need_active=True)
    try:
        body = await request.json() or {}
    except ValueError:
        body = {}
    item = str(body.get("item") or "")
    if not ITEM_RE.match(item):
        raise HTTPException(400, "which session?")
    values = {}
    for k, v in (body.get("values") or {}).items():
        if k in LOG_RANGES and v is not None:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"bad value for {k}")
            lo, hi = LOG_RANGES[k]
            if not lo <= v <= hi:
                raise HTTPException(400, f"{k} must be {lo}-{hi}")
            values[k] = v
    if not values:
        raise HTTPException(400, "nothing to save")
    try:
        return JSONResponse(chat.log_session(cid, item, values))
    except chat.Busy:
        raise HTTPException(409, "the coach is busy with your last message - try again in a moment")
    except LookupError as e:
        raise HTTPException(404, str(e))


async def _upload(request: Request, max_mb: int) -> bytes:
    data = await request.body()
    if len(data) < 500:
        raise HTTPException(400, "nothing recorded")
    if len(data) > max_mb * 1024 * 1024:
        raise HTTPException(413, f"too large - {max_mb} MB at most")
    return data


@app.post("/api/chat/voice")
async def chat_voice(request: Request):
    cid, label = _chat_user(request)
    return _turn_stream(cid, label, audio=await _upload(request, 15))


@app.post("/api/chat/photo")
async def chat_photo(request: Request, caption: str = ""):
    cid, label = _chat_user(request, need_active=True)
    return _turn_stream(cid, label, text=caption.strip()[:1000], image=await _upload(request, 12))


def _turn_stream(chat_id: str, label: str, **turn) -> StreamingResponse:
    try:
        sink = chat.start_turn(chat_id, label, **turn)
    except chat.Busy:
        raise HTTPException(409, "the coach is still answering your last message")

    def events():
        done = False
        try:
            # Comments every 10s keep Cloudflare from closing a long, quiet turn.
            while True:
                try:
                    kind, text, extra = sink.q.get(timeout=10)
                except Exception:
                    yield ": ping\n\n"
                    continue
                yield _sse(kind, text, extra)
                if kind == "done":
                    done = True
                    return
        finally:
            if not done:
                sink.consumer_gone = True     # left mid-turn: the reply becomes a notification

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chat/history")
def chat_history(request: Request):
    email = request_email(request)
    cid, slug, state = own_chat(email)
    if not cid:
        raise HTTPException(403, "this email has no athlete")
    return JSONResponse({"slug": slug, "state": state, "history": chat.timeline(slug, chat_id=cid)},
                        headers=NO_STORE)


MEDIA_RE = re.compile(r"^[0-9a-f-]{8,40}\.png$")


@app.get("/api/media/{name}")
def media(name: str, request: Request):
    email = request_email(request)
    if not MEDIA_RE.match(name):
        raise HTTPException(404)
    slug = own_slug(email)
    if slug:
        path = CC / "athletes" / slug / "web-media" / name
    else:                           # still signing up: the sign-up screenshots
        cid, _, state = own_chat(email)
        if not cid or state != "onboarding":
            raise HTTPException(404)
        path = CC / "config" / "web-signup" / cid / "web-media" / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers=NO_STORE)


# ── Intervals.icu link, for Settings → Connections ──

_ICU_STATUS: dict = {}          # slug -> (checked at, key tail, state)


def _icu_check(athlete_id: str, key: str) -> str:
    """"ok" when the key opens this athlete's Intervals.icu, "rejected" when it doesn't
    (changed, cleared, or someone else's), "unknown" when Intervals.icu didn't answer."""
    import base64
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        "https://intervals.icu/api/v1/athlete/0",
        headers={"Authorization": "Basic " + base64.b64encode(f"API_KEY:{key}".encode()).decode(),
                 "User-Agent": "ClaudeCoach"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            got = str(json.loads(r.read()).get("id") or "")
        return "ok" if got == str(athlete_id) else "rejected"
    except urllib.error.HTTPError as e:
        return "rejected" if e.code in (401, 403) else "unknown"
    except Exception:
        return "unknown"


def _icu_state(slug: str | None) -> str:
    a = _load(ATHLETES_CONFIG).get(slug or "") or {}
    key, aid = str(a.get("icu_api_key") or ""), str(a.get("icu_athlete_id") or "")
    if not slug or not key or not aid:
        return "missing"
    hit = _ICU_STATUS.get(slug)
    if hit and hit[1] == key[-4:] and time.time() - hit[0] < (600 if hit[2] == "ok" else 60):
        return hit[2]
    state = _icu_check(aid, key)
    _ICU_STATUS[slug] = (time.time(), key[-4:], state)
    return state


@app.get("/api/icu/status")
def icu_status(request: Request):
    return JSONResponse({"state": _icu_state(own_slug(request_email(request)))}, headers=NO_STORE)


# ── Settings for the athlete on screen (Jamie, 30 Sep 2026: the coach viewing Kathryn
#    must see HER connections and switches, not his own) ──

def _slug_strava(slug: str) -> str | None:
    """"copying" (Peak copies their Strava), "linked" (read another way: Jamie), None."""
    if not (CC / "athletes" / slug / "strava_tokens.json").exists():
        return None
    return "copying" if (_load(ATHLETES_CONFIG).get(slug) or {}).get("strava_bridge") else "linked"


def _settings(slug: str) -> dict:
    import coaching_prefs
    import planning_pause
    return {"slug": slug, "icu": _icu_state(slug), "strava": _slug_strava(slug),
            "prefs": coaching_prefs.prefs(slug),
            "tracking_only": planning_pause.is_paused(slug)}


@app.get("/api/settings/{slug}")
def athlete_settings(slug: str, request: Request):
    require_slug(request, slug)
    return JSONResponse(_settings(slug), headers=NO_STORE)


@app.post("/api/settings/{slug}")
async def athlete_settings_set(slug: str, request: Request):
    """Heat / fuelling switches: the athlete, or the coach. Tracking only: coach only."""
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    require_slug(request, slug)
    body = await request.json() or {}
    email = request_email(request)
    import coaching_prefs
    heat, fuel = body.get("heat"), body.get("fuelling")
    if heat is not None or fuel is not None:
        coaching_prefs.set_prefs(slug, heat=None if heat is None else bool(heat),
                                 fuelling=None if fuel is None else bool(fuel))
    if "tracking_only" in body:
        if not is_coach(email):
            raise HTTPException(403, "only your coach can change that")
        athletes = _load(ATHLETES_CONFIG)
        if not isinstance(athletes.get(slug), dict):
            raise HTTPException(404, "no such athlete")
        athletes[slug]["planning_paused"] = bool(body["tracking_only"])
        if body["tracking_only"]:
            athletes[slug]["planning_paused_reason"] = f"coach-set in Peak on {time.strftime('%Y-%m-%d')}: tracking only"
        _write_json(ATHLETES_CONFIG, athletes)
    return JSONResponse(_settings(slug), headers=NO_STORE)


# ── Strava, for watches with no direct Intervals.icu link (lib/strava_link.py) ──

def _strava_state(email: str) -> str | None:
    """"copying": Peak copies their Strava into Intervals.icu; "linked": the coach
    already reads their Strava another way (Jamie); None: not connected."""
    try:
        import strava_link
        cid, slug, _ = own_chat(email)
        if not cid or not strava_link.connected(cid):
            return None
        return "copying" if not slug or _load(ATHLETES_CONFIG).get(slug, {}).get("strava_bridge") else "linked"
    except Exception:
        return None


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{title} - Peak</title><body style="font-family:-apple-system,system-ui,sans-serif;'
        'background:#f8f5ef;color:#18160f;padding:48px 24px;max-width:420px;margin:auto;line-height:1.5">'
        f'<h2 style="font-family:Georgia,serif;font-weight:400">{title}</h2><p>{body}</p>'
        '<p><a href="/coach/app.html" style="color:#1d6840">Back to Peak</a></p></body>',
        status_code=status, headers=NO_STORE)


@app.get("/api/strava/connect")
def strava_connect(request: Request):
    """Peak's Connect Strava button: off to Strava's approval page."""
    import strava_link
    email = request_email(request)
    cid, slug, _ = own_chat(email)
    if not cid:
        raise HTTPException(403, "this email has no athlete")
    if _strava_state(email) == "linked":        # never swap Jamie's write-scope token
        return _page("Strava is already connected", "Your coach already reads your Strava.")
    if not strava_link.connected(cid) and strava_link.connected_count() >= strava_link.CAPACITY:
        return _page("Strava is full", "Peak can't connect any more Strava accounts just now. "
                     "Ask your coach.")
    return RedirectResponse(strava_link.authorize_url(cid))


@app.get("/api/strava/callback")
def strava_callback(request: Request, code: str = "", state: str = "", scope: str = "",
                    error: str = ""):
    """Strava sends the athlete back here after they approve (or don't)."""
    import outbox
    import strava_link
    email = request_email(request)
    cid, slug, st = own_chat(email)
    if not cid or not strava_link.check_state(state, cid):
        return _page("That link has expired", "Go back to Peak and tap <b>Connect Strava</b> again.", 400)
    if error or not code:
        return _page("Strava not connected", "No problem. You can connect it any time from Peak.")
    if "activity:read" not in scope:
        return _page("One more tick needed", "Strava needs permission to <b>view data about your "
                     "activities</b>. <a href=\"/api/strava/connect\">Connect Strava</a> again and "
                     "leave that box ticked.")
    if _strava_state(email) == "linked":
        return _page("Strava is already connected", "Your coach already reads your Strava.")
    try:
        tok = strava_link.exchange(code)
        strava_link.save_tokens(cid, tok, scope)
    except Exception as e:
        print(f"[strava] connect failed for {cid}: {e}", flush=True)
        return _page("That didn't work", "Strava didn't accept the connection. Try again from "
                     "Peak in a minute.", 502)
    if slug:
        athletes = _load(ATHLETES_CONFIG)
        if isinstance(athletes.get(slug), dict):
            athletes[slug]["strava_bridge"] = True
            _write_json(ATHLETES_CONFIG, athletes)
    outbox.record(cid, "✓ *Strava connected.* I'll copy your workouts into Intervals.icu from now "
                  "on, and your history too -- it can take a few hours to all arrive."
                  + (" Tap *Check again* above to carry on." if st == "onboarding" else ""),
                  source="strava-link")
    return _page("✓ Strava connected", "You can close this and go back to Peak.")


# ── notifications ──

@app.get("/api/push/key")
def push_key(request: Request):
    request_email(request)
    return JSONResponse({"key": push.public_key()})


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    email = request_email(request)
    if not allowed_athletes(email) and not own_chat(email)[2]:
        raise HTTPException(403, "this email has no athlete")   # Access lets anyone sign in
    try:
        push.subscribe(email, (await request.json() or {}).get("subscription"))
    except (ValueError, AttributeError):
        raise HTTPException(400, "not a push subscription")
    return JSONResponse({"ok": True, "count": push.subscribed(email)})


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    email = request_email(request)
    push.unsubscribe(email, str((await request.json() or {}).get("endpoint") or ""))
    return JSONResponse({"ok": True})


@app.post("/api/push/test")
def push_test(request: Request):
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    slug = own_slug(request_email(request))
    if not slug:
        raise HTTPException(409, "notifications work once your account is active")
    sent = push.notify(slug, "Notifications are working.")
    return JSONResponse({"sent": sent, "errors": list(push.LAST_ERRORS)})


# ── coach admin ──

def _require_coach(request: Request) -> str:
    email = request_email(request)
    if not is_coach(email):
        raise HTTPException(403, "coach only")
    return email


@app.get("/api/admin/athletes")
def admin_athletes(request: Request):
    _require_coach(request)
    athletes, users = _load(ATHLETES_CONFIG), _users()
    pending = [str(x) for x in _load_list(PENDING_FILE)]
    rows = []
    for s, a in athletes.items():
        if not isinstance(a, dict):
            continue
        emails = [e for e, u in users.items() if _slug_of(u, athletes) == s]
        cid = str(a.get("chat_id") or "")
        rows.append({"slug": s, "name": a.get("name") or s, "active": bool(a.get("active")),
                     "telegram": not cid.startswith("web-") and a.get("telegram", True) is not False,
                     "web_only": cid.startswith("web-"), "emails": emails})
    invites = [{"email": e, "chat_id": u.get("chat_id"), "invited": u.get("invited"),
                "state": "onboarding" if str(u.get("chat_id")) in pending else "signed up"}
               for e, u in users.items()
               if u.get("chat_id") and not _slug_of(u, athletes)]
    return JSONResponse({"athletes": rows, "invites": invites}, headers=NO_STORE)


@app.post("/api/admin/telegram")
async def admin_telegram(request: Request):
    """Move an athlete off Telegram (on=false) or back. Off: every coach message goes
    to Peak only, with a notification (lib/outbox.py)."""
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    _require_coach(request)
    body = await request.json() or {}
    slug, on = str(body.get("slug") or ""), bool(body.get("on"))
    athletes = _load(ATHLETES_CONFIG)
    if slug not in athletes:
        raise HTTPException(404, "no such athlete")
    if str(athletes[slug].get("chat_id") or "").startswith("web-"):
        raise HTTPException(409, "this athlete signed up on the web and has no Telegram")
    if on:
        athletes[slug].pop("telegram", None)
    else:
        athletes[slug]["telegram"] = False
    _write_json(ATHLETES_CONFIG, athletes)
    return JSONResponse({"slug": slug, "telegram": on})


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/api/admin/invite")
async def admin_invite(request: Request):
    """Invite a new athlete by email. They sign in at coach.diamondpeak.uk and the
    bot's own sign-up questions run in Peak's chat; the coach approves at the end."""
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    _require_coach(request)
    import secrets
    body = await request.json() or {}
    email = str(body.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "that doesn't look like an email address")
    data = _load(ACCESS_CONFIG)
    users = data.setdefault("users", {})
    if email in {k.strip().lower() for k in users}:
        raise HTTPException(409, "that email already has access")
    cid = "web-" + secrets.token_hex(5)
    users[email] = {"chat_id": cid, "invited": time.strftime("%Y-%m-%dT%H:%M:%S")}
    pending = _load_list(PENDING_FILE)
    pending.append(cid)
    _write_json(PENDING_FILE, pending)
    _write_json(ACCESS_CONFIG, data)
    return JSONResponse({"email": email, "chat_id": cid})


@app.post("/api/admin/link")
async def admin_link(request: Request):
    """Give an existing athlete web access: this email signs in as them."""
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    _require_coach(request)
    body = await request.json() or {}
    email = str(body.get("email") or "").strip().lower()
    slug = str(body.get("slug") or "")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "that doesn't look like an email address")
    if slug not in _load(ATHLETES_CONFIG):
        raise HTTPException(404, "no such athlete")
    data = _load(ACCESS_CONFIG)
    users = data.setdefault("users", {})
    if email in users and users[email].get("coach"):
        raise HTTPException(409, "that is a coach login")
    users[email] = {"slug": slug}
    _write_json(ACCESS_CONFIG, data)
    return JSONResponse({"email": email, "slug": slug})


@app.get("/ClaudeCoach/public/session-library.json")
def session_library():
    return FileResponse(PUBLIC_DIR / "session-library.json", media_type="application/json")


@app.get("/")
def root():
    return RedirectResponse("/coach/app.html")


@app.get("/coach/{path:path}")
def app_files(path: str, request: Request):
    request_email(request)  # the shell is harmless, but nothing is served signed-out
    target = (APP_DIR / (path or "app.html")).resolve()
    if APP_DIR.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404)
    headers = {"Service-Worker-Allowed": "/"} if target.name == "sw.js" else None
    return FileResponse(target, headers=headers)


@app.get("/api/{path:path}")
@app.get("/ClaudeCoach/{path:path}")
def not_here(path: str):
    """Data and API paths never fall through to the public-site redirect."""
    raise HTTPException(404)


@app.get("/{path:path}")
def elsewhere(path: str, request: Request):
    """The site's other tools (Peak links to ../cycling/...) live on GitHub Pages."""
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"{PUBLIC_SITE}/{path}{qs}")
