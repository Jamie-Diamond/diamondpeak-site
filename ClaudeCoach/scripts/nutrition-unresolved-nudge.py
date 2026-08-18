#!/usr/bin/env python3
"""nutrition-unresolved-nudge.py - tell the athlete what the nutrition bot still owes him.

Runs on the VM via crontab, once a morning. Not installed by this commit - see the
crontab line at the bottom of this docstring.

WHAT IT IS FOR. lib/nutrition_store.log_unresolved queues every food string the
resolution ladder could not map, and until 18 Aug 2026 nothing on earth read that file
back. Ten rows had accumulated over more than a week, including a Co-op item that sat
open most of a day: the bot had asked once, in a reply that scrolled away, and then
never mentioned it again. He had no way of knowing an entry was still missing. This
script is the reader.

WHY IT IS ITS OWN SCRIPT ON THE NUTRITION BOT'S TOKEN.
  * The nutrition bot (telegram/nutrition_bot.py) is a pure long-poll daemon under
    claudecoach-nutrition.service. It has no proactive path of its own and should not
    grow a second mode: a cron invocation of the same file that systemd is running as a
    daemon is one typo away from two poll loops on one token.
  * It cannot ride the coach's evening-checkin either, and that is the load-bearing
    reason. telegram/notify.py carries the COACH's token, so the nudge would arrive in
    the coach chat - and the reply to it is food, which only the nutrition bot can parse
    and log. He would answer the question and STILL not be logged, which is the original
    defect wearing a different hat.
So: a small script in scripts/, invoked by cron, exactly as morning-checkin.py,
evening-checkin.py and night-before-brief.py already are. All the judgement (which rows
are due, how the message reads) lives in nutrition_bot.due_unresolved and
nutrition_bot.fmt_unresolved_nudge, where test_nutrition_bot.py can reach it; this file
is plumbing only.

SINGLE-ATHLETE, ON PURPOSE. The coach crons loop over config/athletes.json. The
nutrition bot has one token, one chat and one athlete (telegram/nutrition_config.json),
so this reads that config and nothing else - the same boundary the bot itself keeps.

date.today(), NOT the ICU local date. Every other cron script here does the same
(evening-checkin._sports_logged_today). The alternative, Context.local_today(), needs
ICU auth and a whole Context built with fetchers and the CoFID table, which is a lot of
failure surface for a nudge - and a morning cron slot is nowhere near the 23:00-01:00
window where a UTC server date and the London date disagree.

  # crontab -e on the VM, alongside the coach's pushes at 06:00 / 20:30 / 21:00:
  30 9 * * * /usr/bin/python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/scripts/nutrition-unresolved-nudge.py >> /root/Library/Logs/ClaudeCoach/nutrition-unresolved-nudge.log 2>&1

09:30 is deliberately clear of all three of those, so a stuck-food ask never lands in
the same handful of minutes as the morning card or the evening question.
"""
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
sys.path.insert(0, str(BASE / "lib"))

import ops_log                                          # noqa: E402
from nutrition_store import NutritionStore              # noqa: E402

# Loaded by path, not by import: the module lives in telegram/, which is not a package,
# and this is the same loader test_nutrition_bot.py uses. Importing it does not start
# the poll loop - main() is behind an __main__ guard.
_spec = importlib.util.spec_from_file_location(
    "nutrition_bot", BASE / "telegram" / "nutrition_bot.py")
NB = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(NB)

SCRIPT = "nutrition-unresolved-nudge"


def main(argv=None):
    argv = argv or sys.argv[1:]
    today = date.fromisoformat(argv[0]) if argv else date.today()
    try:
        cfg = NB.load_config()
    except SystemExit as exc:
        # The bot raises SystemExit when nutrition_config.json is absent, which on a dev
        # box is normal and on the VM is an outage. Say which file, and page.
        ops_log.alert(SCRIPT, f"no nutrition config, nudge not sent: {exc}")
        return 1
    slug = cfg.get("athlete") or ""
    store = NutritionStore(BASE / "athletes" / slug)
    try:
        sent = NB.send_unresolved_nudge(store, cfg["bot_token"], cfg["chat_id"], today)
    except Exception as exc:
        ops_log.alert(SCRIPT, f"exception: {type(exc).__name__}: {exc}", athlete=slug)
        raise
    open_rows = len(store.read_unresolved())
    if sent:
        # VERBATIM, whether or not it went out - ops_log.log_outbound exists precisely so
        # that what the athlete was actually told is recoverable from the ops record and
        # not only from a Telegram chat nobody can grep.
        ops_log.log_outbound(SCRIPT, sent, sent=True, athlete=slug)
        detail = f"nudged; {open_rows} row(s) still queued"
    else:
        detail = f"nothing due; {open_rows} row(s) queued"
    ops_log.record_run(SCRIPT, athlete=slug, ok=True, detail=detail)
    print(f"{slug}: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
