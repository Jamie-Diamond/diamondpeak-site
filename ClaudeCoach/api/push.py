"""Phone notifications for Peak (Web Push).

- Keys: a VAPID key pair made on first use, kept outside the repo.
- Subscriptions: one per browser that turned notifications on, tied to the signed-in
  email; only that person's own athlete notifications go to it.
- What notifies: every new line in athletes/<slug>/web-outbox.jsonl (the coach's
  scheduled messages, see lib/outbox.py), found by a background thread that tails the
  files, and a chat reply that finished after the athlete left the app (chat.py).

Nothing here can block or break a request: sending is best-effort and a dead
subscription (404/410 from the push service) is dropped.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("CC_PUSH_DIR", "/etc/claudecoach"))
KEY_FILE = STATE_DIR / "vapid-private.pem"
SUBS_FILE = STATE_DIR / "push-subs.json"
OFFSETS_FILE = STATE_DIR / "push-offsets.json"
CLAIM_SUB = "https://coach.diamondpeak.uk"
QUIET_SOURCES = {"web-turn"}        # recorded during a live chat; the athlete is looking

_lock = threading.Lock()


def _log(msg):
    print(msg, flush=True)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ── keys ──

def _vapid():
    from py_vapid import Vapid
    if not KEY_FILE.exists():
        v = Vapid()
        v.generate_keys()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        v.save_key(str(KEY_FILE))
        os.chmod(KEY_FILE, 0o600)
    return Vapid.from_file(str(KEY_FILE))


def public_key() -> str:
    """The browser's applicationServerKey: base64url of the uncompressed P-256 point."""
    import base64
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw = _vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ── subscriptions ──

def subscribe(email: str, sub: dict) -> None:
    endpoint = (sub or {}).get("endpoint")
    keys = (sub or {}).get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("not a push subscription")
    with _lock:
        subs = _load(SUBS_FILE, {})
        subs[endpoint] = {"email": email, "sub": {"endpoint": endpoint, "keys": keys},
                          "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _save(SUBS_FILE, subs)


def unsubscribe(email: str, endpoint: str) -> None:
    with _lock:
        subs = _load(SUBS_FILE, {})
        if subs.get(endpoint, {}).get("email") == email:
            subs.pop(endpoint)
            _save(SUBS_FILE, subs)


def subscribed(email: str) -> int:
    return sum(1 for s in _load(SUBS_FILE, {}).values() if s.get("email") == email)


# ── sending ──

_MD = re.compile(r"[*_`]+")


def plain(text: str, limit: int = 140) -> str:
    t = re.sub(r"\n_[^\n]*_\s*$", "", text or "")           # the reply footer
    t = _MD.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


LAST_ERRORS: list[str] = []


def notify(slug: str, body: str, title: str = "Coach", url: str = "/coach/app.html#chat",
           kind: str = "message") -> int:
    """Send to every browser whose signed-in person IS this athlete. Returns the count.
    Every failure is logged with the push service's own answer (LAST_ERRORS keeps the
    latest for /api/push/test); only 410 Gone removes a subscription."""
    del LAST_ERRORS[:]
    if _own_slug is None:
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except Exception as e:
        _log(f"[push] pywebpush unavailable: {e}")
        LAST_ERRORS.append(f"pywebpush unavailable: {e}")
        return 0
    from urllib.parse import urlparse
    # kind "reply": a chat answer - the phone hides it if Peak is on screen right now.
    payload = json.dumps({"title": title, "body": body or "New message", "url": url, "kind": kind})
    sent, dead = 0, []
    for endpoint, rec in list(_load(SUBS_FILE, {}).items()):
        host = urlparse(endpoint).netloc
        try:
            if _own_slug(rec.get("email", "")) != slug:
                continue
            webpush(rec["sub"], payload, vapid_private_key=str(KEY_FILE),
                    vapid_claims={"sub": CLAIM_SUB}, ttl=6 * 3600)
            sent += 1
            _log(f"[push] {slug}: sent via {host}")
        except WebPushException as e:
            resp = getattr(e, "response", None)
            code = getattr(resp, "status_code", None)
            text = (getattr(resp, "text", "") or "")[:300]
            msg = f"{host} answered {code}: {text or e}"
            _log(f"[push] {slug}: {msg}")
            LAST_ERRORS.append(msg)
            if code == 410:
                dead.append(endpoint)
        except Exception as e:
            _log(f"[push] {slug}: send via {host} failed: {e}")
            LAST_ERRORS.append(f"{host}: {e}")
    if dead:
        with _lock:
            subs = _load(SUBS_FILE, {})
            for d in dead:
                subs.pop(d, None)
            _save(SUBS_FILE, subs)
    return sent


# ── the outbox watcher ──

_own_slug = None


def start(cc_home: Path, own_slug, athletes_file: Path, interval: float = 5.0) -> None:
    """Tail every athlete's web-outbox.jsonl and notify for each new line. Offsets are
    kept across restarts; an outbox never seen before starts at its end, so turning
    this on never replays history as a burst of notifications."""
    global _own_slug
    _own_slug = own_slug
    _vapid()

    def loop():
        offsets = _load(OFFSETS_FILE, {})
        while True:
            try:
                changed = False
                athletes = _load(athletes_file, {})
                for slug in athletes:
                    f = cc_home / "athletes" / slug / "web-outbox.jsonl"
                    if not f.exists():
                        continue
                    size = f.stat().st_size
                    if slug not in offsets or size < offsets[slug]:
                        offsets[slug] = size        # first sight, or trimmed: start at the end
                        changed = True
                        continue
                    if size == offsets[slug]:
                        continue
                    with open(f) as fh:
                        fh.seek(offsets[slug])
                        new = fh.read()
                    offsets[slug] = size
                    changed = True
                    for line in new.splitlines():
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if e.get("source") in QUIET_SOURCES:
                            continue
                        notify(slug, plain(e.get("text") or "") or ("📷 Photo" if e.get("photo") else ""))
                if changed:
                    _save(OFFSETS_FILE, offsets)
            except Exception as e:
                _log(f"[push] watcher: {e}")
            time.sleep(interval)

    threading.Thread(target=loop, name="push-outbox-watcher", daemon=True).start()
