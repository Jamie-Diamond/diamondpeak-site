#!/usr/bin/env python3
"""Restart claudecoach-bot if its poll-loop heartbeat goes stale.

The bot touches .bot_heartbeat at the top of every getUpdates cycle (~every
30-60s in normal operation). If that file stops updating, the single-threaded
poll loop is wedged (the failure mode seen 16/17 Jun, where the loop sat silent
for hours and nothing auto-recovered). This watchdog — run every 5 min from cron
— bounces the service in that case. Deliberately conservative: it only acts when
the heartbeat EXISTS and is stale, never on a missing file (just-deployed /
starting), so it can never get into a restart loop if the heartbeat itself breaks.
"""
import subprocess
import sys
import time
from pathlib import Path

BASE       = Path(__file__).parent.parent      # ClaudeCoach/
HEARTBEAT  = BASE / ".bot_heartbeat"
SERVICE    = "claudecoach-bot"
STALE_SECS = 300   # 5 min — well above the ~30s long-poll cycle

sys.path.insert(0, str(BASE / "lib"))
try:
    import ops_log
except Exception:
    ops_log = None


def _service_active() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0


def main() -> None:
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
