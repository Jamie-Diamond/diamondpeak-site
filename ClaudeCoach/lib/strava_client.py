"""
Strava API client — token refresh + activity description update.
Tokens are stored per-athlete in athletes/{slug}/strava_tokens.json (gitignored).
App credentials (client_id, client_secret) live in config/strava_app.json (gitignored).
"""
import json
import time
import urllib.request
import urllib.parse
import ssl
from pathlib import Path

BASE = Path(__file__).parent.parent  # ClaudeCoach/
APP_CONFIG = BASE / "config/strava_app.json"
TOKEN_REFRESH_BUFFER = 300  # refresh if token expires within 5 minutes


def _ssl_ctx():
    cafile = "/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None
    return ssl.create_default_context(cafile=cafile)


def _load_app_config() -> dict:
    if not APP_CONFIG.exists():
        raise FileNotFoundError(f"Strava app config not found: {APP_CONFIG}")
    return json.loads(APP_CONFIG.read_text())


class StravaClient:
    def __init__(self, slug: str):
        self.slug = slug
        self.token_file = BASE / "athletes" / slug / "strava_tokens.json"
        app = _load_app_config()
        self.client_id = str(app["client_id"])
        self.client_secret = app["client_secret"]

    def _load_tokens(self) -> dict:
        if not self.token_file.exists():
            raise FileNotFoundError(
                f"No Strava tokens for {self.slug}. Run: python3 ClaudeCoach/scripts/strava-auth.py --athlete {self.slug}"
            )
        return json.loads(self.token_file.read_text())

    def _save_tokens(self, tokens: dict):
        self.token_file.write_text(json.dumps(tokens, indent=2))

    def _refresh(self, refresh_token: str) -> dict:
        payload = urllib.parse.urlencode({
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request(
            "https://www.strava.com/oauth/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()) as r:
            return json.loads(r.read())

    def access_token(self) -> str:
        tokens = self._load_tokens()
        if tokens.get("expires_at", 0) < time.time() + TOKEN_REFRESH_BUFFER:
            refreshed = self._refresh(tokens["refresh_token"])
            tokens.update({
                "access_token":  refreshed["access_token"],
                "refresh_token": refreshed["refresh_token"],
                "expires_at":    refreshed["expires_at"],
            })
            self._save_tokens(tokens)
        return tokens["access_token"]

    def get_activity_detail(self, strava_activity_id: int | str) -> dict:
        """Fetch full Strava activity detail (laps, splits_metric, segment_efforts, etc.)."""
        token = self.access_token()
        req = urllib.request.Request(
            f"https://www.strava.com/api/v3/activities/{strava_activity_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"Strava GET {strava_activity_id} → {e.code}: {body}") from e

    def update_description(self, strava_activity_id: int | str, text: str) -> bool:
        """Write text to a Strava activity description. Returns True on success."""
        token = self.access_token()
        payload = json.dumps({"description": text}).encode()
        req = urllib.request.Request(
            f"https://www.strava.com/api/v3/activities/{strava_activity_id}",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"Strava PUT {strava_activity_id} → {e.code}: {body}") from e
