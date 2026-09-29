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
  /ClaudeCoach/public/nutrition-<slug>.json
  /ClaudeCoach/public/session-library.json
  /api/me                                     who you are, which athletes you may see
Anything else (the site's other tools) redirects to https://diamondpeak.uk.

Run: uvicorn server:app --host 127.0.0.1 --port 8787  (see system/claudecoach-api.service)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

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
    path = PUBLIC_DIR / f"nutrition-{slug}.json"
    if not path.exists():
        raise HTTPException(404)  # opt-in per athlete; Peak treats 404 as "not enabled"
    return FileResponse(path, media_type="application/json", headers=NO_STORE)


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
