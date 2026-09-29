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

Email -> athlete comes from config/web-access.json (VM-only, gitignored):
  {"users": {"someone@example.com": {"slug": "jamie", "coach": true}}}
A coach may read every active athlete; anyone else reads exactly one slug.

Paths mirror the GitHub Pages layout so Peak runs unchanged:
  /coach/...                                  the app itself
  /ClaudeCoach/public/training-data-<slug>.json  PRIVATE file, not the public subset
  /ClaudeCoach/public/nutrition-<slug>.json    PRIVATE athletes/<slug>/nutrition-app.json
  /ClaudeCoach/public/session-library.json
  /api/me                                     who you are, which athletes you may see
  POST /api/refresh/<slug>                    rebuild that athlete's data from Intervals.icu now
  POST /api/chat                              talk to the coach as YOURSELF (see chat.py), SSE
  GET  /api/chat/history                      your shared Telegram/web conversation
Anything else (the site's other tools) redirects to https://diamondpeak.uk.

Run: uvicorn server:app --host 127.0.0.1 --port 8787  (see system/claudecoach-api.service)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

import chat

# CC_HOME / CC_APP_DIR let a test copy run against the live data without being inside
# the live checkout (which cc-gitpull owns).
CC = Path(os.environ.get("CC_HOME") or Path(__file__).resolve().parent.parent)  # ClaudeCoach/
SITE = CC.parent                                     # diamondpeak-site/
APP_DIR = Path(os.environ.get("CC_APP_DIR") or SITE / "coach")
PUBLIC_DIR = CC / "public"
ATHLETES_CONFIG = CC / "config" / "athletes.json"
ACCESS_CONFIG = Path(os.environ.get("CC_WEB_ACCESS", CC / "config" / "web-access.json"))
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


def allowed_athletes(email: str) -> list[dict]:
    """Athletes this email may read, as [{slug, name}], in athletes.json order."""
    users = {k.strip().lower(): v for k, v in (_load(ACCESS_CONFIG).get("users") or {}).items()}
    user = users.get(email)
    if not user:
        return []
    athletes = _load(ATHLETES_CONFIG)
    active = [(s, v) for s, v in athletes.items() if isinstance(v, dict) and v.get("active")]
    if not user.get("coach"):
        active = [(s, v) for s, v in active if s == user.get("slug")]
    return [{"slug": s, "name": (v.get("name") or s).split()[0]} for s, v in active]


def own_slug(email: str) -> str | None:
    """The athlete this email IS - who a chat message is from. A coach can VIEW
    everyone, but always talks to the coach as themself."""
    users = {k.strip().lower(): v for k, v in (_load(ACCESS_CONFIG).get("users") or {}).items()}
    slug = (users.get(email) or {}).get("slug")
    return slug if slug in {a["slug"] for a in allowed_athletes(email)} else None


def require_slug(request: Request, slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise HTTPException(404)
    if slug not in {a["slug"] for a in allowed_athletes(request_email(request))}:
        raise HTTPException(403, "not your data")


# ── app ───────────────────────────────────────────────────────────────────

app = FastAPI(title="ClaudeCoach API", docs_url=None, redoc_url=None, openapi_url=None)

NO_STORE = {"Cache-Control": "private, no-cache"}


@app.get("/api/me")
def me(request: Request):
    email = request_email(request)
    athletes = allowed_athletes(email)
    if not athletes:
        raise HTTPException(403, "this email has no athlete")
    return JSONResponse({"email": email, "athletes": athletes}, headers=NO_STORE)


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

def _sse(kind: str, text: str) -> str:
    return f"event: {kind}\ndata: {json.dumps({'text': text})}\n\n"


@app.post("/api/chat")
async def chat_send(request: Request):
    if request.headers.get("x-peak") != "1":
        raise HTTPException(400, "missing app header")
    slug = own_slug(request_email(request))
    if not slug:
        raise HTTPException(403, "this email has no athlete")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    text = str((body or {}).get("text") or "").strip()[:4000]
    if not text:
        raise HTTPException(400, "empty message")
    try:
        sink = chat.start_turn(slug, text)
    except chat.Busy:
        raise HTTPException(409, "the coach is still answering your last message")
    except LookupError:
        raise HTTPException(404, "no coaching chat set up for this athlete")

    def events():
        # Comments every 10s keep Cloudflare from closing a long, quiet turn.
        while True:
            try:
                kind, text = sink.q.get(timeout=10)
            except Exception:
                yield ": ping\n\n"
                continue
            yield _sse(kind, text)
            if kind == "done":
                return

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chat/history")
def chat_history(request: Request):
    slug = own_slug(request_email(request))
    if not slug:
        raise HTTPException(403, "this email has no athlete")
    return JSONResponse({"slug": slug, "history": chat.history(slug)}, headers=NO_STORE)


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
