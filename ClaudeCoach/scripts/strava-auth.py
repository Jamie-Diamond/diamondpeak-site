#!/usr/bin/env python3
"""
One-time Strava OAuth setup per athlete.
Usage: python3 ClaudeCoach/scripts/strava-auth.py --athlete <slug>

Prerequisites:
- Register a Strava API app at https://www.strava.com/settings/api
- Set redirect_uri to: http://localhost
- Add client_id + client_secret to ClaudeCoach/config/strava_app.json
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import ssl
from pathlib import Path

BASE = Path(__file__).parent.parent  # ClaudeCoach/
APP_CONFIG = BASE / "config/strava_app.json"


def _ssl_ctx():
    cafile = "/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None
    return ssl.create_default_context(cafile=cafile)


def main():
    parser = argparse.ArgumentParser(description="Strava OAuth setup for a ClaudeCoach athlete")
    parser.add_argument("--athlete", required=True, help="Athlete slug (e.g. jamie)")
    args = parser.parse_args()
    slug = args.athlete

    if not APP_CONFIG.exists():
        print(f"ERROR: {APP_CONFIG} not found.")
        print("Create it with:")
        print('  {"client_id": 12345, "client_secret": "abc123..."}')
        sys.exit(1)

    app = json.loads(APP_CONFIG.read_text())
    client_id = str(app["client_id"])
    client_secret = app["client_secret"]

    scope = "read,activity:read_all,activity:write"
    auth_url = (
        "https://www.strava.com/oauth/authorize?"
        + urllib.parse.urlencode({
            "client_id":     client_id,
            "redirect_uri":  "http://localhost",
            "response_type": "code",
            "approval_prompt": "force",
            "scope":         scope,
        })
    )

    print(f"\nStrava OAuth for athlete: {slug}")
    print("=" * 60)
    print("\n1. Open this URL in your browser:\n")
    print(f"   {auth_url}\n")
    print("2. Approve the app in Strava.")
    print("3. Your browser will redirect to http://localhost?code=...&scope=...")
    print("   (the page may show 'Connection Refused' — that's fine)")
    print("\n4. Paste the FULL redirect URL (or just the 'code' parameter value):\n")

    raw = input("   > ").strip()
    if not raw:
        print("ERROR: No input received.")
        sys.exit(1)

    # Accept either the full URL or just the code value
    if raw.startswith("http"):
        parsed = urllib.parse.urlparse(raw)
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        granted_scope = (params.get("scope") or [""])[0]
    else:
        code = raw
        granted_scope = ""

    if not code:
        print("ERROR: Could not extract code from input.")
        sys.exit(1)

    if granted_scope and "activity:write" not in granted_scope:
        print(f"WARNING: activity:write not in granted scope ({granted_scope}). Description updates will fail.")

    # Exchange code for tokens
    payload = urllib.parse.urlencode({
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
        "grant_type":    "authorization_code",
    }).encode()
    req = urllib.request.Request(
        "https://www.strava.com/oauth/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ERROR: Token exchange failed ({e.code}): {body}")
        sys.exit(1)

    tokens = {
        "access_token":  resp["access_token"],
        "refresh_token": resp["refresh_token"],
        "expires_at":    resp["expires_at"],
    }

    token_file = BASE / "athletes" / slug / "strava_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(tokens, indent=2))

    athlete_name = resp.get("athlete", {}).get("firstname", slug)
    expiry = time.strftime("%Y-%m-%d %H:%M", time.localtime(tokens["expires_at"]))
    print(f"\nSuccess! Tokens saved for {athlete_name} → {token_file}")
    print(f"Access token expires: {expiry} (auto-refreshed on each use)")


if __name__ == "__main__":
    main()
