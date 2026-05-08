#!/usr/bin/env python3
"""Send a message to the ClaudeCoach Telegram chat. Usage: notify.py <message> or pipe via stdin."""
import json, sys, ssl, urllib.request
from pathlib import Path

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)

config = json.loads((Path(__file__).parent / "config.json").read_text())
token = config["bot_token"]
chat_id = config["chat_id"]

message = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else sys.stdin.read().strip()
if not message:
    sys.exit(0)

for chunk in [message[i:i+4096] for i in range(0, len(message), 4096)]:
    payload = json.dumps({"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT)
    except Exception as e:
        print(f"Telegram error: {e}", file=sys.stderr)
        sys.exit(1)
