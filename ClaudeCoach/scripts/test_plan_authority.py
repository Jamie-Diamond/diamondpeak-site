#!/usr/bin/env python3
"""Offline tests for plan authority, increment 1 (13 Aug 2026).
Run: python3 ClaudeCoach/scripts/test_plan_authority.py

WHAT THIS GUARDS. Three mechanisms that stop a plan build destroying an agreed week, plus
the retirement of the one that was doing the destroying:

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
check("_run_once and _stream_once both take env",
      "def _run_once(prompt, model, extra_args, cwd, timeout=300, env=None)" in _eng_src
      and "def _stream_once(prompt, model, extra_args, cwd, env=None)" in _eng_src)
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


# --- 6) every touched file still parses --------------------------------------------------
for rel in ("lib/icu_fetch.py", "lib/engine.py", "lib/claude_call.py",
            "lib/plan_builder.py", "lib/plan_lock.py", "scripts/stage1-plan.py",
            "scripts/daily-prescription.py", "telegram/bot.py",
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
