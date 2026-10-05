#!/opt/peak-browser/bin/python3
"""Screenshot Peak as an athlete sees it, from the VM (Jamie, 5 Oct 2026: "Install a
browser to the VM", so a layout change is looked at before it is called done).

Starts a private, read-only copy of the API on localhost (CC_API_DEV_EMAIL sign-in, no
push watcher), loads https://coach.diamondpeak.uk/coach/app.html in headless Chromium
(/opt/peak-browser, Playwright) with the app's files served from THIS checkout and /api
sent to that copy, then saves a phone-sized PNG. Nothing goes through Cloudflare and no
message is sent: only GETs are forwarded.

    peak-screenshot.py --email jamie@diamondpeak.uk --hash chat --dev --out /tmp/dev.png
    peak-screenshot.py --hash chat --scroll top      # top of the chat instead of the end
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SITE = Path(__file__).resolve().parents[2]              # diamondpeak-site/
API_DIR = SITE / "ClaudeCoach" / "api"
UVICORN = "/opt/claudecoach-api/venv/bin/uvicorn"
HOST = "https://coach.diamondpeak.uk"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_api(email: str, port: int) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CF_ACCESS")}
    for f in ("/etc/claudecoach-api.env", "/root/.claude/cc.env"):
        try:
            for line in Path(f).read_text().splitlines():
                k, sep, v = line.partition("=")
                if sep and not line.lstrip().startswith("#") and not k.startswith("CF_ACCESS"):
                    env.setdefault(k.strip(), v.strip().strip('"'))
        except OSError:
            pass
    env.update(CC_API_DEV_EMAIL=email, CC_PUSH_WATCH="0", HOME="/root")
    p = subprocess.Popen([UVICORN, "server:app", "--host", "127.0.0.1", "--port", str(port)],
                         cwd=API_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/me", timeout=1)
            return p
        except urllib.error.HTTPError:
            return p                                     # up, just not happy with the path
        except Exception:
            time.sleep(0.5)
    p.kill()
    raise SystemExit("private API did not start: " + (p.stderr.read() or b"").decode()[-800:])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="jamie@diamondpeak.uk")
    ap.add_argument("--athlete", default="jamie", help="profile to open (skips the picker)")
    ap.add_argument("--hash", default="chat", help="tab to open, e.g. chat, today, calendar")
    ap.add_argument("--dev", action="store_true", help="switch the chat to the Dev tab")
    ap.add_argument("--scroll", default="end", help="end | top | a pixel offset")
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--full", action="store_true", help="whole page, not one screen")
    ap.add_argument("--out", default="/tmp/peak.png")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    port = _free_port()
    api = _start_api(a.email, port)
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            ctx = b.new_context(viewport={"width": a.width, "height": a.height},
                                device_scale_factor=2, is_mobile=True, has_touch=True,
                                service_workers="block")
            ctx.add_init_script(f"localStorage.setItem('cc.athlete', {a.athlete!r})")
            if a.dev:
                ctx.add_init_script("localStorage.setItem('cc.chatTab','dev')")
            page = ctx.new_page()

            def handle(route):
                req = route.request
                path = req.url[len(HOST):].split("#")[0]
                if path.startswith("/api/"):
                    if req.method != "GET":
                        return route.fulfill(status=403, body="screenshot mode: read only")
                    try:
                        r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30)
                        return route.fulfill(status=r.status, body=r.read(),
                                             headers={"content-type": r.headers.get("content-type", "")})
                    except urllib.error.HTTPError as e:
                        return route.fulfill(status=e.code, body=e.read())
                f = SITE / path.split("?")[0].lstrip("/")
                if f.is_file():
                    return route.fulfill(status=200, body=f.read_bytes(),
                                         content_type=mimetypes.guess_type(f.name)[0] or "text/plain")
                return route.fulfill(status=404, body="")

            page.route(f"{HOST}/**", handle)
            page.goto(f"{HOST}/coach/app.html#{a.hash}")
            page.wait_for_timeout(4000)
            if a.scroll == "top":
                page.evaluate("window.scrollTo(0, 0)")
            elif a.scroll == "end":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            else:
                page.evaluate(f"window.scrollTo(0, {int(a.scroll)})")
            page.wait_for_timeout(800)
            page.screenshot(path=a.out, full_page=a.full)
            b.close()
    finally:
        api.terminate()
    print(a.out)


if __name__ == "__main__":
    sys.exit(main())
