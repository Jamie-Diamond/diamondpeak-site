#!/usr/bin/env python3
"""Offline tests for the per-athlete monthly chat allowance (lib/chat_limits.py, 29 Sep 2026).
Run: python3 ClaudeCoach/scripts/test_chat_limits.py

Jamie's rules: $20/month default, overridable; at 80% chat steps down a model and the
athlete AND Jamie are warned once; at 100% it carries on at a daily cap (never a hard
stop), both told once; Jamie exempt; resets on the 1st. Plus the engine metering that
feeds it, and the bot's admin-chat fallback that makes /limits reachable at all.
No network, no model, no real athlete file.
"""
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
sys.path.insert(0, str(_here.parent / "telegram"))
import chat_limits as cl   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp(prefix="chat-limits-"))
cl.BASE = tmp
D = date(2026, 9, 29)
KATH = {"chat_id": "22", "name": "Kathryn"}

check("default allowance is $20", cl.allowance(KATH) == 20.0)
check("per-athlete override", cl.allowance({"chat_allowance_usd": 35}) == 35.0)
check("step-down: opus->sonnet, sonnet->haiku, haiku stays",
      (cl.step_down("opus"), cl.step_down("sonnet"), cl.step_down("haiku")) == ("sonnet", "haiku", "haiku"))

g = cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=D)
check("fresh month: no step-down, no notice", not g["step_down"] and not g["athlete_notice"])

for _ in range(15):
    cl.record("kathryn", 1.0, today=D)          # $15 = 75%
g = cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=D)
check("75%: still normal", not g["step_down"] and not g["athlete_notice"])

cl.record("kathryn", 1.2, today=D)              # $16.20 = 81%
g = cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=D)
check("80%: steps down a model", g["step_down"] and not g["blocked"])
check("80%: athlete warned", "80%" in g["athlete_notice"] and "lighter model" in g["athlete_notice"])
check("80%: Jamie warned, with the money", "Kathryn is at 80%" in g["admin_notice"] and "$16.20" in g["admin_notice"])
check("80%: says when it resets", "Thu 01 Oct" in g["athlete_notice"], g["athlete_notice"])
g2 = cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=D)
check("80% warning goes out once, not every message", not g2["athlete_notice"] and not g2["admin_notice"]
      and g2["step_down"])

cl.record("kathryn", 4.0, today=D)              # $20.20 = 101%
g = cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=D)
check("100%: told once (athlete + Jamie), still not blocked",
      "used this month's chat allowance" in g["athlete_notice"] and "100%" in g["admin_notice"]
      and not g["blocked"])
for _ in range(cl.DAILY_CAP_OVER - 1):
    cl.record("kathryn", 0.05, today=D)
check("the day the cap starts, the athlete still gets the full daily allowance",
      not cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=D)["blocked"])
cl.record("kathryn", 0.05, today=D)
g = cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=D)
check("100%: blocked once today's cap is used", g["blocked"] and "tomorrow" in g["block_text"])
check("  ...with no repeat notice", not g["athlete_notice"])
g = cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=date(2026, 9, 30))
check("next day: open again (still on the lighter model)", not g["blocked"] and g["step_down"])
g = cl.gate("kathryn", KATH, "Kathryn", admin_chat_id="11", today=date(2026, 10, 1))
check("1st of the month: reset to normal", not g["step_down"] and not g["blocked"])

cl.record("jamie", 500, today=D)
JAMIE = {"chat_id": "11", "name": "Jamie"}
g = cl.gate("jamie", JAMIE, "Jamie", admin_chat_id="11", today=D)
check("Jamie (admin chat) exempt at any spend", not g["step_down"] and not g["athlete_notice"])
check("exempt by flag too", cl.exempt({"chat_limit_exempt": True}))

rep = cl.report({"jamie": {**JAMIE, "active": True}, "kathryn": {**KATH, "active": True}},
                admin_chat_id="11", today=D)
check("/limits report shows spend, allowance and tier", "Kathryn: $" in rep and "(exempt)" in rep, rep)
check("no em-dashes in anything an athlete reads",
      all("—" not in t for t in (g2["block_text"], rep)))

# engine metering feeds the ledger
import engine  # noqa: E402
engine._chat_limits.BASE = tmp
sp = tmp / "athletes" / "calum" / "system_prompt.txt"
sp.parent.mkdir(parents=True, exist_ok=True)
engine._cost_reset()
engine._cost_add(0.5)
engine._cost_add("0.25")
engine._cost_add(None)
engine._meter(sp)
st = cl.status("calum", {}, date.today())
check("engine meters a reply's summed runs into the ledger", abs(st["spent"] - 0.75) < 1e-9, st)

# the bot's admin fallback
import bot as B  # noqa: E402
check("admin chat falls back to config chat_id", B.admin_chat_id({"chat_id": 11}) == "11")
check("explicit admin_chat_id still wins", B.admin_chat_id({"chat_id": 11, "admin_chat_id": 5}) == "5")

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
