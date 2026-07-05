#!/usr/bin/env python3
"""Restart claudecoach-bot if its poll-loop heartbeat goes stale, and alert if an
athlete's text message went unanswered.

The bot touches .bot_heartbeat at the top of every getUpdates cycle (~every
30-60s in normal operation). If that file stops updating, the single-threaded
poll loop is wedged (the failure mode seen 16/17 Jun, where the loop sat silent
for hours and nothing auto-recovered). This watchdog — run every 5 min from cron
— bounces the service in that case. Deliberately conservative: it only acts when
the heartbeat EXISTS and is stale, never on a missing file (just-deployed /
starting), so it can never get into a restart loop if the heartbeat itself breaks.

Separately, the poll loop can stay alive while a single reply still goes missing
(the swim-splits message that got no reply and was only found by trawling history
after the athlete complained). bot.py logs "[slug] In: ..." for every text message
it routes and "[slug] Out..." once a reply (or an apology) has been sent for it.
check_missed_replies() pairs those lines per athlete and alerts if an In has no
later Out within REPLY_STALE_MIN minutes, so a dropped reply surfaces on its own
instead of waiting for the athlete to notice.
"""
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE        = Path(__file__).parent.parent      # ClaudeCoach/
HEARTBEAT   = BASE / ".bot_heartbeat"
SERVICE     = "claudecoach-bot"
STALE_SECS  = 300   # 5 min — well above the ~30s long-poll cycle

BOT_LOG         = BASE / "telegram/bot.log"
REPLY_STALE_MIN = 10   # generation + delivery normally takes well under this
REPLY_STATE     = BASE / ".reply_watchdog_state.json"
_IN_RE  = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] In: ")
_OUT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] Out")

sys.path.insert(0, str(BASE / "lib"))
try:
    import ops_log
except Exception:
    ops_log = None


def _service_active() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0


def _load_reply_state() -> dict:
    try:
        import json
        return json.loads(REPLY_STATE.read_text())
    except Exception:
        return {}


def _save_reply_state(state: dict) -> None:
    try:
        import json
        REPLY_STATE.write_text(json.dumps(state))
    except Exception:
        pass


def check_missed_replies() -> None:
    """Alert once per stuck message if an athlete's text has had no reply logged
    within REPLY_STALE_MIN minutes. Only looks at the log tail so a huge bot.log
    doesn't slow the check down."""
    try:
        lines = BOT_LOG.read_text().splitlines()[-2000:]
    except Exception:
        return
    last_in = {}
    for line in lines:
        m = _IN_RE.match(line)
        if m:
            last_in[m.group(2)] = m.group(1)
            continue
        m = _OUT_RE.match(line)
        if m and m.group(2) in last_in and m.group(1) >= last_in[m.group(2)]:
            del last_in[m.group(2)]
    if not last_in:
        return
    now = datetime.now()
    state = _load_reply_state()
    changed = False
    for slug, ts in last_in.items():
        try:
            age_min = (now - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
        except Exception:
            continue
        if age_min < REPLY_STALE_MIN or state.get(slug) == ts:
            continue  # too recent, or already alerted for this exact message
        detail = f"no reply logged for {slug}'s message sent {ts} (>{REPLY_STALE_MIN} min ago)"
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {detail}", file=sys.stderr)
        if ops_log:
            try:
                ops_log.alert("bot-watchdog", detail)
            except Exception:
                pass
        state[slug] = ts
        changed = True
    if changed:
        _save_reply_state(state)


def main() -> None:
    check_missed_replies()
    if not _service_active():
        return  # stopped on purpose — not the watchdog's job to start it
    if not HEARTBEAT.exists():
        return  # no heartbeat yet (just deployed / starting) — nothing to judge
    age = time.time() - HEARTBEAT.stat().st_mtime
    if age <= STALE_SECS:
        return
    detail = f"heartbeat stale ({int(age)}s > {STALE_SECS}s) — restarting {SERVICE}"
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {detail}", file=sys.stderr)
    if ops_log:
        try:
            ops_log.alert("bot-watchdog", detail)
        except Exception:
            pass
    subprocess.run(["systemctl", "restart", SERVICE])


if __name__ == "__main__":
    main()
