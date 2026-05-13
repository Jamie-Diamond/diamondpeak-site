#!/usr/bin/env python3
"""
Send a message or photo to the ClaudeCoach Telegram chat.

Usage:
  notify.py <message>                      # send text (defaults to config chat_id)
  notify.py --chat-id <id> <message>       # send to specific athlete
  notify.py --photo <path> [caption]       # send photo
  echo "text" | notify.py                  # pipe text
"""
import json, sys, ssl, urllib.request, urllib.error
from pathlib import Path

_cafile = "/etc/ssl/cert.pem" if __import__("os").path.exists("/etc/ssl/cert.pem") else None
SSL_CONTEXT = ssl.create_default_context(cafile=_cafile)

config  = json.loads((Path(__file__).parent / "config.json").read_text())
token   = config["bot_token"]

# Parse --chat-id before any other arg processing
_args = list(sys.argv[1:])
if "--chat-id" in _args:
    _idx = _args.index("--chat-id")
    if _idx + 1 < len(_args):
        chat_id = _args[_idx + 1]
        _args = _args[:_idx] + _args[_idx + 2:]
    else:
        chat_id = config["chat_id"]
else:
    chat_id = config["chat_id"]


def send_text(text):
    for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
        payload = json.dumps({"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                # Malformed Markdown — retry as plain text
                plain = json.dumps({"chat_id": chat_id, "text": chunk}).encode()
                req2 = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=plain,
                    headers={"Content-Type": "application/json"},
                )
                try:
                    urllib.request.urlopen(req2, timeout=10, context=SSL_CONTEXT)
                except Exception as e2:
                    print(f"Telegram error: {e2}", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"Telegram error: {e}", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"Telegram error: {e}", file=sys.stderr)
            sys.exit(1)


def send_photo(photo_bytes, caption=""):
    boundary = "CCbound"
    parts = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"chart.png\"\r\nContent-Type: image/png\r\n\r\n"
    ).encode() + photo_bytes + f"\r\n--{boundary}--\r\n".encode()
    if caption:
        parts = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"chart.png\"\r\nContent-Type: image/png\r\n\r\n"
        ).encode() + photo_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=parts,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT)
    except Exception as e:
        print(f"Telegram photo error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    args = _args  # already stripped of --chat-id
    if args and args[0] == "--photo":
        if len(args) < 2:
            print("Usage: notify.py --photo <path> [caption]", file=sys.stderr)
            sys.exit(1)
        photo_path = Path(args[1])
        caption = " ".join(args[2:]) if len(args) > 2 else ""
        send_photo(photo_path.read_bytes(), caption)
    else:
        message = " ".join(args).strip() if args else sys.stdin.read().strip()
        if not message:
            sys.exit(0)
        send_text(message)
