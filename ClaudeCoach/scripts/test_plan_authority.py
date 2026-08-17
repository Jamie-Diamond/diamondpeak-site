#!/usr/bin/env python3
"""Offline tests for plan authority, increments 1 and 3 (13 Aug 2026).
Run: python3 ClaudeCoach/scripts/test_plan_authority.py

Increment 2 — the pin record and the honoured build — is tested next door in
scripts/test_agreed_week.py. This file is increment 1 (stop the destruction) and increment 3
(the athlete-facing surface).

INCREMENT 3, and what each part is guarding against:

  4. lib/icu_fetch.caller_violation — the two watcher jobs hold Bash and could write the
     calendar with no reason to. They declare who they are and the write endpoints refuse
     them; reads, which are all either job does, keep working. Fail-open for anyone who
     passes no --caller, so hand-runs are unaffected.
  5. telegram/bot.replan_card — the card must NAME THE WEEK THE BUILD WILL PLAN and list
     what it will not touch. The old copy said "this week" while the build planned
     _next_monday(today), which on a Monday is the FOLLOWING Monday, so listing that week's
     protections against a rebuild of another week would have been a fresh instance of the
     rage class this work closes. Checked on all seven weekdays.
  6. stage1-plan.fallback_gate — a WRONG-SHAPED week beats an empty calendar (9 Aug 2026:
     three athletes, nothing planned, found because Jamie asked); an UNSAFE one does not.
     Driven on {code, msg} fixtures so the gate tests codes, not prose that will be
     reworded. An unrecognised code ALLOWS and is reported — failing closed on unknowns
     would let the next hard rule silently reinstate the empty week.
  7. stage1-plan.agreed_shortfall_clause — when the agreed days stop the week reaching its
     target, the athlete is told, and nothing agreed is quietly shortened to hide it.

WHAT INCREMENT 1 GUARDS. Three mechanisms that stop a plan build destroying an agreed week,
plus the retirement of the one that was doing the destroying:

  1. lib/plan_lock — one build per athlete. Two detached `stage1-plan --push` runs against
     one calendar interleave into 14 events drawn from two incoherent plans (the 22 Jun
     France week). The second must stand down with BUSY_EXIT.
  2. lib/icu_fetch.scope_violation — a chat session serving one athlete must not WRITE to
     another's calendar. Reads stay unrestricted.
  3. telegram/bot — _extract_plan_override / _write_plan_override are gone, and neither
     launch path passes --override-json. That scrape rebuilt whatever week a stale JSON
     blob named, against the numbers assembled for a different week.

No LLM, no network, no ICU call: every check drives a pure function or a real flock in a
tmpdir. The lock checks fail if the flock is removed — they take the lock in a genuinely
separate process, not just a second handle in this one.
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
BASE = _here.parent
sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "telegram"))

# Point the lock at a tmpdir BEFORE importing plan_lock: LOCK_DIR is read at import time,
# and /var/lock does not exist on macOS — left at the default, every acquisition here
# would fail-soft to UNLOCKED and these checks would prove nothing about the real path.
LOCK_DIR = tempfile.mkdtemp(prefix="planlock-test-")
os.environ["CC_PLAN_LOCK_DIR"] = LOCK_DIR

import plan_lock          # noqa: E402
import icu_fetch          # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


# --- 1) the build lock -------------------------------------------------------------------
SLUG = "testathlete"

check("lock_path lands in the configured dir, named per athlete",
      plan_lock.lock_path(SLUG) == Path(LOCK_DIR) / f"cc-plan-{SLUG}.lock")
check("BUSY_EXIT is 4 (distinct from 1 = built nothing and 3 = built, not pushed)",
      plan_lock.BUSY_EXIT == 4)

with plan_lock.PlanLock(SLUG) as first:
    check("a first acquisition is HELD", first.state == plan_lock.HELD)

    # A SECOND PROCESS, not a second handle. flock is per open file description, so two
    # handles in one process would both succeed on some platforms and this check would be
    # vacuous. The child reports the state it saw.
    probe = (
        "import os, sys, json;"
        f"os.environ['CC_PLAN_LOCK_DIR']={LOCK_DIR!r};"
        f"sys.path.insert(0, {str(BASE / 'lib')!r});"
        "import plan_lock;"
        f"lk=plan_lock.PlanLock({SLUG!r});lk.__enter__();"
        "print(lk.state);lk.__exit__()"
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    check("a concurrent acquisition from another process is BUSY",
          r.stdout.strip() == plan_lock.BUSY, )
    check("build_running() sees the held lock", plan_lock.build_running(SLUG) is True)
    check("build_running() is per athlete, not global",
          plan_lock.build_running("someone-else") is False)

check("the lock is released when the block exits",
      plan_lock.build_running(SLUG) is False)
with plan_lock.PlanLock(SLUG) as again:
    check("a released lock can be taken again", again.state == plan_lock.HELD)

# THE PROBE MUST NOT KEEP THE FD. If build_running left the handle open, the build the bot
# goes on to spawn would find the lock held by its own parent and exit 4 every time —
# replan would be permanently dead.
plan_lock.build_running(SLUG)
with plan_lock.PlanLock(SLUG) as after_probe:
    check("build_running leaves no handle behind (a real build can still lock)",
          after_probe.state == plan_lock.HELD)

# FAIL-SOFT. An unusable lock path must not stop the Sunday build: UNLOCKED, not BUSY.
_real_dir = plan_lock.LOCK_DIR
try:
    plan_lock.LOCK_DIR = str(Path(LOCK_DIR) / "no" / "such" / "dir")
    with plan_lock.PlanLock(SLUG) as broken:
        check("an unusable lock file is UNLOCKED (build proceeds), never BUSY",
              broken.state == plan_lock.UNLOCKED)
    check("build_running is False when the lock file is unusable (never blocks a replan)",
          plan_lock.build_running(SLUG) is False)
finally:
    plan_lock.LOCK_DIR = _real_dir


# --- 2) the cross-athlete scope assert ---------------------------------------------------
V = icu_fetch.scope_violation

check("the four mutating endpoints are the write set",
      icu_fetch.WRITE_ENDPOINTS == frozenset(
          {"push_workout", "edit_workout", "edit_activity", "delete_workout"}))

for ep in sorted(icu_fetch.WRITE_ENDPOINTS):
    check(f"{ep}: refused for another athlete when scoped",
          bool(V("kathryn", ep, scope="jamie")))
    check(f"{ep}: allowed for the scoped athlete",
          V("jamie", ep, scope="jamie") is None)
    check(f"{ep}: allowed when no scope is set",
          V("kathryn", ep, scope="") is None)

check("the refusal names both athletes so the model can relay it",
      "kathryn" in V("kathryn", "push_workout", scope="jamie")
      and "jamie" in V("kathryn", "push_workout", scope="jamie"))

# READS ARE NEVER REFUSED. Comparing athletes is legitimate coaching work and a read
# cannot damage anyone; the scope exists to stop writes only.
for ep in ("profile", "fitness", "wellness", "events", "history", "activity_detail",
           "streams", "best_efforts", "power_curves", "training_summary",
           "sport_settings", "extended_metrics"):
    check(f"read endpoint {ep} is allowed cross-athlete even when scoped",
          V("kathryn", ep, scope="jamie") is None)

# An EMPTY variable means unscoped, not "a scope matching nobody" — otherwise any
# hand-run that exports it blank refuses every write.
check("an empty scope is treated as absent, not as a mismatch",
      V("kathryn", "push_workout", scope="   ") is None)
check("scope defaults to CC_ATHLETE_SCOPE from the environment", (
    os.environ.__setitem__("CC_ATHLETE_SCOPE", "jamie"),
    bool(V("kathryn", "push_workout")) and V("jamie", "push_workout") is None,
    os.environ.pop("CC_ATHLETE_SCOPE", None),
)[1])
check("with no CC_ATHLETE_SCOPE in the environment, nothing is refused",
      V("kathryn", "push_workout") is None)

# The scope must be refused BEFORE a client is built, so no other athlete's API key is
# ever loaded. Asserted against the source: main() has no test seam and the alternative
# needs a live config.
_fetch_src = (BASE / "lib" / "icu_fetch.py").read_text()
check("icu_fetch refuses before load_client, not after",
      _fetch_src.index("scope_violation(args.endpoint"
                       if "scope_violation(args.endpoint" in _fetch_src
                       else "_viol = scope_violation(")
      < _fetch_src.index("client = load_client("))


# --- 3) env plumbing: scoped, and per spawn ----------------------------------------------
import engine  # noqa: E402

_env = engine.scoped_env(BASE / "athletes" / "kathryn" / "system_prompt.txt")
check("scoped_env derives the slug from the system-prompt path",
      _env["CC_ATHLETE_SCOPE"] == "kathryn")
check("scoped_env carries the rest of the environment through (PATH etc.)",
      _env.get("PATH") == os.environ.get("PATH") and len(_env) >= len(os.environ))
check("scoped_env does NOT mutate os.environ (bot.py is multi-athlete/multi-thread)",
      "CC_ATHLETE_SCOPE" not in os.environ)
check("two athletes get two different scopes from one process",
      engine.scoped_env(BASE / "athletes" / "jamie" / "system_prompt.txt")
      ["CC_ATHLETE_SCOPE"] == "jamie" and _env["CC_ATHLETE_SCOPE"] == "kathryn")

# EVERY spawn that can reach icu_fetch must be scoped, including the rate-limit fallback
# retries — a capped turn falling Opus -> Sonnet must not run unscoped. Checked over the
# AST, not the text: these calls span lines, so a grep for "env=" on the line holding the
# call name misses the ones that matter. Source-level because spawning a real claude here
# is neither offline nor cheap.
_eng_src = (BASE / "lib" / "engine.py").read_text()
_eng_ast = ast.parse(_eng_src)


def _calls_named(tree, *names):
    """Every Call node in `tree` whose callee spells one of `names` (bare or dotted)."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        spelled = (f.id if isinstance(f, ast.Name)
                   else f"{getattr(f.value, 'id', '')}.{f.attr}" if isinstance(f, ast.Attribute)
                   else "")
        if spelled in names:
            out.append(node)
    return out


def _passes_env(call):
    return any(k.arg == "env" for k in call.keywords)


for site, expected in (("_run_once", 3), ("_stream_once", 3)):
    calls = _calls_named(_eng_ast, site)
    check(f"all {len(calls)} {site} call sites pass env= (expected >= {expected}, "
          "including the Opus->Sonnet fallback retry)",
          len(calls) >= expected and all(_passes_env(c) for c in calls))

# The raw spawns underneath, including call_claude_with_image's — it holds the same TOOLS
# and can therefore reach icu_fetch too.
_spawns = _calls_named(_eng_ast, "subprocess.run", "subprocess.Popen")
check(f"every subprocess spawn in engine.py passes env= ({len(_spawns)} found)",
      len(_spawns) == 3 and all(_passes_env(c) for c in _spawns))
# Matched WITHOUT the closing paren (17 Aug 2026): what this is guarding is that both
# helpers take env=, and _stream_once legitimately grew a `run=None` handle when
# cancellation landed (bug #30). Pinning the exact spelling of the whole signature made
# an unrelated, correct change look like a scoping regression; the env= claim is intact.
check("_run_once and _stream_once both take env",
      "def _run_once(prompt, model, extra_args, cwd, timeout=300, env=None" in _eng_src
      and "def _stream_once(prompt, model, extra_args, cwd, env=None" in _eng_src)
check("run_claude takes env and passes it to subprocess.run",
      "env=None" in (BASE / "lib" / "claude_call.py").read_text()
      and "timeout=timeout, env=env" in (BASE / "lib" / "claude_call.py").read_text())
check("daily-prescription scopes its own spawn (its prompt instructs a push_workout)",
      'CC_ATHLETE_SCOPE": slug' in (BASE / "scripts" / "daily-prescription.py").read_text())


# --- 4) the plan-override scrape is retired ----------------------------------------------
import bot as B  # noqa: E402

check("_extract_plan_override is gone from bot.py",
      not hasattr(B, "_extract_plan_override"))
check("_write_plan_override is gone from bot.py",
      not hasattr(B, "_write_plan_override"))
_bot_src = (BASE / "telegram" / "bot.py").read_text()
# The quoted argv form, not the bare flag name: the retirement comment explains what was
# removed and names the flag in prose, which is documentation, not a live call.
check("no launch path in bot.py passes --override-json any more",
      '"--override-json"' not in _bot_src and "'--override-json'" not in _bot_src)
check("stage1-plan KEEPS --override-json (hand-runs and plan_builder.main still want it)",
      "--override-json" in (BASE / "scripts" / "stage1-plan.py").read_text())
check("bot.py maps the busy exit code rather than hard-coding 4",
      "plan_lock.BUSY_EXIT" in _bot_src)
check("the busy exit is NOT in _REPLAN_SELF_REPORTS (it tells the athlete nothing)",
      plan_lock.BUSY_EXIT not in B._REPLAN_SELF_REPORTS)


# --- 5) the swallowed delete failure is logged -------------------------------------------
_pb_src = (BASE / "lib" / "plan_builder.py").read_text()
check("plan_builder.push still swallows a failed delete (a duplicate is benign)",
      "except Exception as e:" in _pb_src and "failed.append(eid)" in _pb_src)
check("plan_builder.push now names the event it could not delete",
      "delete of old event" in _pb_src)
check("push() reports the failures to its caller",
      '"delete_failed": failed' in _pb_src)


# --- 6) INCREMENT 3: the caller allowlist ------------------------------------------------
# Watchers hold Bash and could therefore write the calendar with no reason to. They now
# declare who they are and the four WRITE endpoints refuse them. READS must keep working —
# both jobs do nothing else through this CLI.
C = icu_fetch.caller_violation

check("the two watchers are the no-write callers",
      icu_fetch.NO_WRITE_CALLERS == frozenset({"activity-watcher", "morning-checkin"}))

for caller in sorted(icu_fetch.NO_WRITE_CALLERS):
    for ep in sorted(icu_fetch.WRITE_ENDPOINTS):
        check(f"{caller} is refused {ep}", bool(C(ep, caller)))
    for ep in ("events", "history", "profile", "activity_detail", "extended_metrics",
               "fitness", "wellness", "streams"):
        check(f"{caller} may still READ {ep} (it is all either job does)",
              C(ep, caller) is None)

# FAIL-OPEN for anyone who does not volunteer a name, so every hand-run keeps working. This
# is an allowlist over jobs that identify themselves, not an authentication boundary.
for absent in (None, "", "   "):
    check(f"a caller of {absent!r} is unrestricted (hand-run compatibility)",
          C("push_workout", absent) is None)
for allowed in ("chat", "daily-prescription", "stage1-plan", "cli"):
    check(f"caller {allowed} is not refused a write", C("push_workout", allowed) is None)

check("the refusal names the caller and points at whose job writing is",
      "activity-watcher" in C("push_workout", "activity-watcher")
      and "stage1-plan" in C("push_workout", "activity-watcher"))

# Refused BEFORE load_client, same ordering and same reason as the scope check: a job that
# may not write must not get as far as loading anyone's API key.
check("icu_fetch refuses the caller before load_client, not after",
      _fetch_src.index("_cviol = caller_violation(") < _fetch_src.index("client = load_client("))

# The two scripts must actually PASS it, in their prompts (the LLM's own Bash calls, which
# are the risk) and on their own subprocess reads.
_aw_src = (BASE / "scripts" / "activity-watcher.py").read_text()
_mc_src = (BASE / "scripts" / "morning-checkin.py").read_text()
check("activity-watcher declares its caller and tells the model to pass it every time",
      'CALLER = "activity-watcher"' in _aw_src
      and "--caller activity-watcher --endpoint profile" in _aw_src
      and "EVERY icu_fetch.py call you make must carry" in _aw_src)
check("morning-checkin declares its caller and tells the model to pass it every time",
      'CALLER = "morning-checkin"' in _mc_src
      and "--caller morning-checkin --endpoint events" in _mc_src
      and "EVERY icu_fetch.py call you make must carry" in _mc_src)
check("every icu_fetch subprocess in activity-watcher passes --caller",
      _aw_src.count('"--caller", CALLER') == 4)
# Both spawns are also SCOPED. The design's version of this gate was ANDed with
# CC_ATHLETE_SCOPE, but neither script set it, so such a gate could never have fired. The
# refusal is on --caller alone; the scope is added because it is the right bound anyway.
for name, src in (("activity-watcher", _aw_src), ("morning-checkin", _mc_src)):
    check(f"{name} scopes its claude spawn to one athlete",
          '"CC_ATHLETE_SCOPE": slug' in src)


# --- 7) INCREMENT 3: the replan card -----------------------------------------------------
# The card's job is to be TRUE about which week it rebuilds and what it will not touch.
_MON = date(2026, 8, 17)          # a Monday
_TUE, _THU = "2026-08-18", "2026-08-20"
_PINS = {
    _TUE: {"why": "agreed in chat", "at": "2026-08-11T19:04:11", "by": "chat",
           "session": {"sport": "Run", "name": "Long run 35km", "minutes": 210,
                       "load_target": 190, "coarse": False, "segments": []}},
    _THU: {"why": "you said no training Thursday", "at": "2026-08-11T19:05:02",
           "by": "chat", "session": None},
}
_PROT = {_TUE: "agreed in chat", _THU: "you said no training Thursday"}

_text, _rows = B.replan_card(_MON, _PINS, _PROT)
print("\n--- replan card, WITH pins ---\n" + _text + "\n" +
      "\n".join("  " + " | ".join(b["text"] for b in r) for r in _rows) + "\n")

check("the card names the week it will actually plan", "w/c Mon 17 Aug" in _text)
check("the card does not say 'this week'", "this week" not in _text.lower())
check("the protected block names the agreed run with its size and when we agreed it",
      "Tue 18 — Long run 35km (agreed in chat, 11 Aug)" in _text)
check("a rest-day pin reads as nothing, with the athlete's own reason",
      "Thu 20 — nothing (you said no training Thursday" in _text)
check("the card states which days WILL be rebuilt",
      "I will rebuild: Mon, Wed, Fri, Sat, Sun." in _text)
_labels = [b["text"] for r in _rows for b in r]
check("three buttons, and the first counts the days it will rebuild",
      _labels == ["✅ Rebuild those 5 days",
                  "🔓 Rebuild the whole week (drops what we agreed)",
                  "❌ Cancel"])
check("the release button carries the new callback token",
      [b["callback_data"] for r in _rows for b in r]
      == ["__REPLAN_CONFIRM__", "__REPLAN_CONFIRM_RELEASE__", "__REPLAN_CANCEL__"])

# NO PINS: degrade to one confirm button and no protected block — no worse than the card
# this replaced.
_t0, _r0 = B.replan_card(_MON, {}, {})
print("--- replan card, NO pins ---\n" + _t0 + "\n" +
      "\n".join("  " + " | ".join(b["text"] for b in r) for r in _r0) + "\n")
check("with nothing protected the card still names the real week",
      "w/c Mon 17 Aug" in _t0 and "this week" not in _t0.lower())
check("with nothing protected there is no protected block", "will *not* touch" not in _t0)
check("with nothing protected there are two buttons and no release",
      [b["text"] for r in _r0 for b in r] == ["✅ Rebuild the week", "❌ Cancel"])

# An availability-only week: the day is protected (the build plans nothing there) but there
# is no PIN, and release() releases pins — so offering "drops what we agreed" would promise
# something the button cannot do.
_t1, _r1 = B.replan_card(_MON, {}, {"2026-08-19": "you said you are unavailable"})
check("an availability-only protected day is listed but offers NO release button",
      "Wed 19 — nothing (you said you are unavailable)" in _t1
      and "__REPLAN_CONFIRM_RELEASE__" not in json.dumps(_r1))
check("an availability-only week still offers the 6-day rebuild",
      "✅ Rebuild those 6 days" in json.dumps(_r1, ensure_ascii=False))

# Every day protected: "rebuild those 0 days" would be a button that does nothing.
_allp = {(_MON + timedelta(days=i)).isoformat(): "agreed in chat" for i in range(7)}
_t2, _r2 = B.replan_card(_MON, {d: {"why": "agreed in chat", "at": "", "session": None}
                                for d in _allp}, _allp)
check("with all seven days agreed there is no confirm button, only release + cancel",
      [b["callback_data"] for r in _r2 for b in r]
      == ["__REPLAN_CONFIRM_RELEASE__", "__REPLAN_CANCEL__"])
check("and it says there is nothing left to rebuild",
      "nothing left for me to rebuild" in _t2)

# THE CARD MUST NAME THE WEEK THE BUILD PLANS — on every weekday, Monday included. This is
# the check that matters: the design's whole point is that the card and the build cannot
# disagree. Monday is the trap (`(7 - 0) % 7 or 7` = 7, so replan on a Monday plans NEXT
# Monday), and it is why the label "Rebuild this week's plan" was already wrong.
for _off in range(7):
    _today = date(2026, 8, 17) + timedelta(days=_off)
    _ws = B.next_plan_monday(_today)
    check(f"next_plan_monday({_today} {_today.strftime('%a')}) is a future Monday",
          _ws.weekday() == 0 and _ws > _today)
    _txt, _ = B.replan_card(_ws, {}, {})
    check(f"the card built for {_today.strftime('%a')} names {_ws} and no other week",
          f"w/c {_ws.strftime('%a')} {_ws.day} {_ws.strftime('%b')}" in _txt)
check("replan on a MONDAY plans the FOLLOWING Monday (the product question stays open, "
      "but the copy is now truthful about it)",
      B.next_plan_monday(date(2026, 8, 17)) == date(2026, 8, 24))

# THE CONFIRM PATH. The card's week is carried on the pending and passed to the build as
# --week-start, so a tap cannot retarget the week the athlete read about.
check("the pending replan carries the week, not just an expiry",
      "\"week_start\": _ws.isoformat()" in _bot_src)
check("the confirm passes --week-start from the pending",
      '"--week-start", week_start' in _bot_src)
check("the card TTL is generous enough to read a protected list",
      B._REPLAN_CARD_TTL >= 180)
check("all three tokens are handled", B._REPLAN_TOKENS == (
      "__REPLAN_CONFIRM__", "__REPLAN_CONFIRM_RELEASE__", "__REPLAN_CANCEL__"))
# The release must happen BEFORE the build is launched, and a FAILED release must abort it:
# rebuilding with the pins still in place delivers the opposite of what was tapped.
check("the release runs before the build is spawned",
      _bot_src.index("agreed_week.release(slug, week_start") < _bot_src.index('"--week-start", week_start'))
check("a failed release aborts rather than rebuilding with the pins still standing",
      "rebuilding now would have kept those days anyway" in _bot_src)
check("the release is attributed in the store so 'who dropped it' is answerable",
      "athlete tapped " in _bot_src)
check("the /replan menu label no longer claims to rebuild THIS week",
      not any("this week" in d.lower() for c, d in B.BOT_COMMANDS if c == "replan"))


# --- 8) INCREMENT 3: blocking codes + the empty-week fallback gate -----------------------
# stage1-plan cannot be imported by name (the hyphen is a syntax error), so it is loaded by
# path. Its pure helpers are driven directly on {code, msg} fixtures — no LLM, no ICU.
import importlib.util  # noqa: E402
sys.path.insert(0, str(BASE / "ironman-analysis"))
_s1_spec = importlib.util.spec_from_file_location("stage1_plan", BASE / "scripts" / "stage1-plan.py")
S1 = importlib.util.module_from_spec(_s1_spec)
_s1_spec.loader.exec_module(S1)


def _blk(*codes):
    return [{"code": c, "msg": f"prose for {c}"} for c in codes]


# THE GATE. A wrong-SHAPED week beats an empty calendar; an UNSAFE week does not.
check("a load/shape blocker alone allows the fallback on a confirmed-empty week",
      S1.fallback_gate(_blk("weekly_tss_floor"), True)[0] is True)
check("a missing long ride is structure, not safety — fallback allowed",
      S1.fallback_gate(_blk("long_ride_missing"), True)[0] is True)
check("a wrong-day rule is shape, not safety — fallback allowed",
      S1.fallback_gate(_blk("Run_forbidden_day"), True)[0] is True)
for _safe in sorted(S1._SAFETY_BLOCKER_CODES):
    ok, safety = S1.fallback_gate(_blk(_safe), True)
    check(f"safety code {_safe} BLOCKS the fallback and is named back to the caller",
          ok is False and [e["code"] for e in safety] == [_safe])
check("one safety code among several shape ones is enough to block",
      S1.fallback_gate(_blk("weekly_tss_floor", "ctl_ramp", "no_rest_day"), True)[0] is False)

# The four safety codes the design names by hand must all be in the set, including the two
# that arrive via built["hard"] rather than as bare appends.
for _named in ("run_mileage_cap", "run_quality_not_cleared", "physio_not_cleared",
               "ctl_ramp", "run_weekly_volume", "run_long_volume"):
    check(f"{_named} is classified as a safety blocker",
          _named in S1._SAFETY_BLOCKER_CODES)
check("weekly_tss_floor is NOT a safety blocker (a light week is the complaint the "
      "fallback answers, not a reason to deliver nothing)",
      "weekly_tss_floor" not in S1._SAFETY_BLOCKER_CODES)

# A NON-EMPTY week never takes the fallback: the athlete's plan stands, which is today's
# behaviour and correct. And UNKNOWN is not empty — conflating "could not read the calendar"
# with "there is nothing there" is how a failed build overwrites a week that was fine.
for _empty, _label in ((False, "a week that already has sessions"),
                       (None, "a week whose calendar could NOT be read")):
    check(f"{_label} never takes the fallback",
          S1.fallback_gate(_blk("weekly_tss_floor"), _empty)[0] is False)

# UNKNOWN CODES ALLOW, AND ARE REPORTED. Failing closed on an unrecognised code means the
# next hard rule anyone adds silently reinstates the 9 Aug empty week.
check("an unclassified code does not block the fallback",
      S1.fallback_gate(_blk("some_new_rule_2027"), True)[0] is True)
check("an unclassified code is reported so the list can be audited",
      S1.unknown_blocker_codes(_blk("some_new_rule_2027", "ctl_ramp"))
      == ["some_new_rule_2027"])
check("per-sport day codes are recognised by suffix, not enumerated",
      S1.unknown_blocker_codes(_blk("Swim_forbidden_day", "Ride_directed_day")) == [])

# THE SHAPES THEMSELVES. Blocking entries are dicts; every consumer that shows prose goes
# through _msgs. A dict reaching the athlete's Telegram message is the 5 Jul 2026 bug.
check("_msgs renders {code,msg} dicts to prose",
      S1._msgs(_blk("ctl_ramp")) == ["prose for ctl_ramp"])
check("_msgs tolerates a bare string, so a missed path is not fatal",
      S1._msgs(["bare prose"]) == ["bare prose"])
_s1_src = (BASE / "scripts" / "stage1-plan.py").read_text()
check("the athlete-facing failure reason is RENDERED, never a raw dict",
      "why = (_msgs(blocking)[0] if blocking" in _s1_src)
check("the proposer feedback is rendered too", "all_issues = _msgs(blocking) + advisory" in _s1_src)
check("built['hard'] carries its code through instead of throwing it away",
      '{"code": v.get("code"), "msg": f"rule(hard): {v[\'msg\']}"}' in _s1_src)
check("all five bare-prose blocking appends now carry codes",
      all(f'"code": "{c}"' in _s1_src for c in
          ("run_mileage_cap", "run_quality_not_cleared", "long_run_cap",
           "long_ride_missing", "physio_not_cleared")))
check("the fallback push cannot delete (replace=False)",
      "pb.push(args.athlete, built, replace=False)" in _s1_src)
check("the fallback banks the zones for next week's rolling balance",
      _s1_src.index("summary[\"push_result\"] = pb.push(args.athlete, built, replace=False)")
      < _s1_src.index("_write_prior_zones(args.athlete, week_start, proposal)\n                if args.notify"))
check("the fallback does NOT advance the injury ramp off a week that failed its own audit",
      _s1_src.count("_advance_injury_ramp(args.athlete") == 1)
# THE EXIT CONTRACT. This block replaces "the fallback still exits 3 so weekly-plan.sh
# sees 'not clean'", which encoded the 16 Aug 2026 bug as the requirement: the fallback
# pushed calum three events and exited 3 anyway, so weekly-plan.sh alerted "NO WEEK PUSHED
# for calum" about a week that was on his calendar. 3 means NOTHING WAS PUSHED, and the
# only thing allowed to decide it is whether anything was pushed. Driven on the real
# function rather than the source text, because a text assertion cannot catch a helper
# that returns the wrong number.
check("a --push run that pushed a clean week exits 0",
      S1.exit_code_for({"pushed": True, "push_result": {"pushed": [1, 2, 3]}}, True) == 0)
check("the empty-week FALLBACK pushed a week, so it exits 0 too (16 Aug 2026)",
      S1.exit_code_for({"pushed": True, "week_was_empty": True,
                        "push_result": {"pushed": [129564388, 129564389, 129564390]}},
                       True) == 0)
check("a --push run that genuinely pushed nothing still exits 3",
      S1.exit_code_for({"pushed": False, "reason": "not ready"}, True) == 3)
# A dry run writes no "pushed" key at all (summary["pushed"] is only set under --push), so
# reading the field without the args.push gate would make every hand-run and every
# scripts/shadow-week-check.sh run exit 3 for doing exactly what it is meant to do.
check("a DRY RUN exits 0, having pushed nothing on purpose",
      S1.exit_code_for({"ready_to_push": False}, False) == 0)
check("the exit code is derived from the same field the summary prints, not a parallel flag",
      "not_pushed" not in _s1_src)
# THE HEARTBEAT half of the same false report. `stage1-plan` defaults to FAILURE in
# coach_alert.OUTCOME_CLASS, ops-digest._saw() treats a FAILURE heartbeat as "the
# deliverable did not happen", and the weekly plan is telegram=True, so the fallback's
# ok=False beat told Jamie there was no week for calum, on the one channel that reaches
# him. FINDING keeps the digest line and drops the false claim.
import coach_alert  # noqa: E402
import ops_log      # noqa: E402
check("an unstamped stage1-plan ok=False line is a FAILURE (why the stamp is needed)",
      coach_alert.classify({"script": "stage1-plan", "ok": False, "detail": "x"})
      == coach_alert.FAILURE)
# Stamped with ops_log.FINDING, which is the value stage1-plan actually writes, not with
# coach_alert's own name for it. classify() only honours a stamp that is IN (FAILURE,
# FINDING) as coach_alert spells them, so if the two ever stop being the same string the
# stamp goes inert, the line classifies back to FAILURE and the false "no weekly plan for
# calum" alert returns with every test still green. Asserting the shipped value is what
# makes that impossible.
check("ops_log and coach_alert agree on what FINDING is spelled",
      ops_log.FINDING == coach_alert.FINDING)
check("a FINDING-stamped one is not, so the gap check still counts the run as delivered",
      coach_alert.classify({"script": "stage1-plan", "ok": False, "detail": "x",
                            "outcome": ops_log.FINDING}) == coach_alert.FINDING)
check("the fallback beat is FINDING-stamped, and it is the ONLY beat that is",
      _s1_src.count("outcome=ops_log.FINDING") == 1
      and "empty calendar ({why})\", outcome=ops_log.FINDING)" in _s1_src)

# EVERY CODE THE SCRIPT CAN EMIT HAS A WORD IN weekly-plan.sh. The wrapper's `what` lookup
# falls back to f"exited {rc}", which is not a lie but tells the reader nothing: 4 (stood
# down, another build holds the lock) sat in that fallback until 17 Aug 2026. Codes are
# read out of the AST, so adding a fifth exit to stage1-plan.py fails this check instead of
# quietly landing in the digest as "exited 5".
def _emittable_codes(path):
    """Non-zero exit codes stage1-plan.py can produce. `unhandled` is any sys.exit()
    argument shape this reader does not understand. Never silently ignored, because an
    unread exit is exactly the gap this check exists to close."""
    tree = ast.parse(path.read_text(), filename=str(path))
    codes, unhandled = set(), []
    # Every int anywhere inside a `return` in exit_code_for, not just a bare
    # `return <int>`: a `return 0 if x else 3` hides the 3 one node down, and missing it
    # would have this check pass while claiming 3 is unreachable.
    returned = {c.value
                for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == "exit_code_for"
                for n in ast.walk(f) if isinstance(n, ast.Return) and n.value is not None
                for c in ast.walk(n.value)
                if isinstance(c, ast.Constant) and isinstance(c.value, int)}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "exit"
                and node.args):
            continue
        a = node.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, int):
            codes.add(a.value)
        elif isinstance(a, ast.Attribute) and a.attr == "BUSY_EXIT":
            codes.add(plan_lock.BUSY_EXIT)
        elif isinstance(a, ast.Name) and a.id == "rc":
            codes |= returned          # sys.exit(rc), rc from exit_code_for
        else:
            unhandled.append(ast.dump(a))
    return {c for c in codes if c}, unhandled


_codes, _unread = _emittable_codes(BASE / "scripts" / "stage1-plan.py")
check("every sys.exit() in stage1-plan.py is readable by this check", not _unread)
check("the exit codes are the documented four (1 built nothing, 3 pushed nothing, "
      "4 stood down; 124 comes from `timeout`, not the script)",
      _codes == {1, 3, 4})
import re  # noqa: E402

_wp_src = (BASE / "scripts" / "weekly-plan.sh").read_text()
_mapped = {int(c) for c in re.findall(r'"(\d+)":', _wp_src)}
check(f"weekly-plan.sh maps every emittable code {sorted(_codes)} plus 124, mapped="
      f"{sorted(_mapped)}", _codes | {124} <= _mapped)
check("weekly-plan.sh does NOT tell Jamie the calendar is empty when the build stood down "
      "(rc=4: the build holding the lock delivers the week)",
      'tail = ("" if rc == "4" else' in _wp_src)
check("the fallback message does NOT claim the calendar was left alone",
      "Your calendar has NOT been updated" not in S1._fallback_message(
          {"phase": "build"},
          {"week_start": "2026-08-17", "total_tss": 610,
           "sessions": [{"date": "2026-08-17", "name": "Endurance ride",
                         "duration_min": 120}]},
          "rule(hard): week TSS 610 under the floor"))
_fb = S1._fallback_message(
    {"phase": "build"},
    {"week_start": "2026-08-17", "total_tss": 610,
     "sessions": [{"date": "2026-08-17", "name": "Endurance ride", "duration_min": 120}]},
    "rule(hard): week TSS 610 under the floor")
print("--- fallback message ---\n" + _fb + "\n")
check("the fallback message says it is a starting point and asks for the correction",
      "starting point" in _fb and "Tell me what to change" in _fb)
check("the fallback message strips the rule() prefix from the reason the athlete reads",
      "rule(hard)" not in _fb and "under the floor" in _fb)


# --- 9) INCREMENT 3: 'I could not honour everything' -------------------------------------
# Agreed days are fixed, so the remaining days may not reach the week's target — and flex()
# deliberately refuses to shrink a pinned session. That is right, and hiding it reads to the
# athlete as the coach getting the week wrong rather than keeping their word.
_brief = {"weekly_tss_target": 820, "phase": "build"}
_built_short = {"week_start": "2026-08-17", "total_tss": 742, "sessions": [
    {"date": _TUE, "sport": "Ride", "name": "Long ride", "duration_min": 240,
     "pinned": True, "load_target": 212},
    {"date": "2026-08-19", "sport": "Swim", "name": "Threshold swim", "duration_min": 60,
     "pinned": False, "load_target": 60}]}
_clause = S1.agreed_shortfall_clause(_brief, _built_short, _PINS)
print("--- shortfall clause ---\n" + _clause + "\n")
check("the shortfall clause names the agreed days in the athlete's terms",
      "Tue's ride 4h" in _clause and "Thu off" in _clause)
check("the shortfall clause states both numbers", "742" in _clause and "820" in _clause)
check("the shortfall clause says the agreed session was NOT cut, and offers the trade back",
      "haven't shortened anything we agreed" in _clause and "rather I did" in _clause)
check("a week ON target says nothing (the clause is for the case that must not be silent)",
      S1.agreed_shortfall_clause(_brief, {**_built_short, "total_tss": 800}, _PINS) == "")
check("no pins, no clause — the ordinary week message is unchanged for everyone else",
      S1.agreed_shortfall_clause(_brief, _built_short, {}) == "")
check("no target, no clause",
      S1.agreed_shortfall_clause({}, _built_short, _PINS) == "")
_over = S1.agreed_shortfall_clause(_brief, {**_built_short, "total_tss": 910}, _PINS)
check("an OVER-target week gets the mirror clause, not the shortfall one",
      "910" in _over and "haven't trimmed anything we agreed" in _over)
# THE TWO SURFACES MUST SPELL A DURATION THE SAME WAY. The card and the week message
# describe the same agreed session minutes apart; "4h" on one and "4h00" on the other reads
# as two different sessions. They cannot share a helper (the hyphen in stage1-plan), so the
# agreement is asserted here instead of hoped for.
_s1_dur = S1._agreed_day_phrases.__wrapped__ if hasattr(S1._agreed_day_phrases, "__wrapped__") \
          else S1._agreed_day_phrases
for _m, _want in ((0, ""), (45, "45min"), (59, "59min"), (60, "1h"), (90, "1h30"),
                  (210, "3h30"), (240, "4h"), (300, "5h")):
    check(f"bot.duration_phrase({_m}) is {_want!r}", B.duration_phrase(_m) == _want)
    # The stage1 copy is a closure inside _agreed_day_phrases, so it is driven through its
    # caller on a one-session week rather than reached directly.
    _one = {"sessions": [{"date": _TUE, "sport": "Ride", "duration_min": _m,
                          "pinned": True}]}
    _got = _s1_dur(_one, {})[0]
    check(f"stage1 spells {_m}min the same way bot.py does",
          _got == ("Tue's ride" + (f" {_want}" if _want else "")))

# A ROUND duration on the card: the format the two helpers disagreed about before this.
_round = {_TUE: {"why": "agreed in chat", "at": "2026-08-11T19:04:11", "by": "chat",
                 "session": {"sport": "Ride", "name": "Long ride", "minutes": 240,
                             "load_target": 212, "coarse": False, "segments": []}}}
_tr, _ = B.replan_card(_MON, _round, {_TUE: "agreed in chat"})
check("a nameless-size pin gets its duration appended, in the shared format",
      "Tue 18 — Long ride 4h (agreed in chat, 11 Aug)" in _tr)
# ASSERTED ON THE RENDERED OUTPUT, not by grepping the source. "%-d" (no zero-pad) is a
# glibc/BSD extension that the rest of bot.py uses freely by long convention — the VM is
# Linux — so a source grep would fail on unrelated lines. It is avoided in _pin_line only,
# because a libc lacking it does not RAISE: it returns the directive verbatim, so the failure
# mode is silently wrong copy that no try/except catches.
check("the agreed date renders as a real date, not a leaked format directive",
      _tr.split("(agreed in chat, ")[1].startswith("11 Aug)")
      and "%" not in _tr and "-d" not in _tr.split("(agreed in chat, ")[1][:8])

check("_week_message takes the pins and appends the clause",
      "def _week_message(brief: dict, built: dict, pins: dict | None = None)" in _s1_src
      and "_week_message(brief, built, pins)" in _s1_src)


# --- 10) housekeeping: the June one-off is out of lib ------------------------------------
check("push_kathryn_plan.py is no longer in lib (it wrote six hard-coded weeks to one "
      "named athlete and any LLM job with Bash could run it)",
      not (BASE / "lib" / "push_kathryn_plan.py").exists())
check("it is kept in attic/ for reference",
      (BASE / "attic" / "push_kathryn_plan.py").exists())
check("attic says what it is for and that nothing imports from it",
      "not importable" in (BASE / "attic" / "README.md").read_text())
check("nothing imports push_kathryn_plan any more",
      not any("push_kathryn_plan" in p.read_text()
              for p in list((BASE / "lib").glob("*.py"))
              + [q for q in (BASE / "scripts").glob("*.py")
                 if not q.name.startswith("test_")]
              + [BASE / "telegram" / "bot.py"]))


# --- 11) every touched file still parses --------------------------------------------------
for rel in ("lib/icu_fetch.py", "lib/engine.py", "lib/claude_call.py",
            "lib/plan_builder.py", "lib/plan_lock.py", "scripts/stage1-plan.py",
            "scripts/daily-prescription.py", "scripts/activity-watcher.py",
            "scripts/morning-checkin.py", "telegram/bot.py",
            "scripts/test_plan_authority.py"):
    p = BASE / rel
    try:
        ast.parse(p.read_text(), filename=str(p))
        ok = True
    except SyntaxError as e:
        ok = False
        print(f"     {rel}: {e}")
    check(f"{rel} parses", ok)

check("scripts/daily-prescription.sh is deleted (dead, athlete-hardcoded, granted the "
      "single-account MCP push_workout)",
      not (BASE / "scripts" / "daily-prescription.sh").exists())


if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
