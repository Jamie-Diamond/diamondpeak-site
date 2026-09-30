#!/usr/bin/env python3
"""Telegram inbox, now the coaching bot is off (30 Sep 2026).

claudecoach-bot.service is stopped: coaching runs in Peak. Anyone who still messages
the old Telegram coach would get no reply, and Telegram keeps unread messages for only
about a day. This picks them up (VM crontab, every 10 min) and puts each one in the
coach's Peak chat, e.g. "📨 Calum on Telegram: ...", which sends Jamie a notification.
It never replies to the athlete.

The food bot has its own token and is untouched.

    python3 ClaudeCoach/scripts/telegram-inbox.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC / "lib"))
import outbox  # noqa: E402
import tg      # noqa: E402

CONFIG = CC / "telegram" / "config.json"
ATHLETES = CC / "config" / "athletes.json"
STATE = Path("/root/.claudecoach-telegram-inbox.json")


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def describe(update: dict, names: dict) -> tuple[str, str] | None:
    """(who, what) for a message worth passing on; None for anything else."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    chat = msg.get("chat") or {}
    who = names.get(str(chat.get("id"))) or chat.get("first_name") or "Someone"
    what = (msg.get("text") or msg.get("caption") or "").strip()
    if msg.get("photo"):
        what = ("(a photo) " + what).strip()
    elif msg.get("voice") or msg.get("audio"):
        what = "(a voice note)"
    elif msg.get("document"):
        what = ("(a file) " + what).strip()
    return (who, what) if what else None


def main() -> int:
    config = json.loads(CONFIG.read_text())
    token = config["bot_token"]
    admin = str(config.get("admin_chat_id") or config.get("chat_id") or "")
    athletes = json.loads(ATHLETES.read_text())
    names = {str(a.get("chat_id")): a.get("name", slug).split()[0]
             for slug, a in athletes.items() if isinstance(a, dict) and a.get("chat_id")}
    try:
        offset = int(json.loads(STATE.read_text()).get("offset", 0))
    except (OSError, ValueError):
        offset = 0
    r = tg.post(token, "getUpdates", {"offset": offset, "timeout": 0,
                                      "allowed_updates": ["message", "edited_message"]}, log=log)
    if not r.get("ok"):
        log(f"getUpdates failed: {r}")
        return 1
    passed = 0
    for u in r.get("result") or []:
        offset = max(offset, int(u["update_id"]) + 1)
        d = describe(u, names)
        if not d:
            continue
        who, what = d
        outbox.record(admin, f"📨 *{who} on Telegram:* {what}\n\n_Telegram coaching is off, "
                             "so they got no reply there._", source="telegram-inbox")
        passed += 1
    STATE.write_text(json.dumps({"offset": offset, "checked": datetime.now().isoformat()}))
    if passed:
        log(f"passed on {passed} Telegram message(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
