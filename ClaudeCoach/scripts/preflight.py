#!/usr/bin/env python3
"""Pre-deployment sanity checks. Run from the repo root before pushing. Exits 1 on failure."""
import json, sys, py_compile
from collections import Counter
from pathlib import Path

BASE = Path(__file__).parent.parent
PASS, FAIL = "  OK", "FAIL"
failures = []

def check(label, ok, detail=""):
    tag = PASS if ok else FAIL
    line = f"[{tag}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    if not ok:
        failures.append(label)

# -- Python syntax ------------------------------------------------------------
py_files = [f for f in (BASE / "scripts").glob("*.py") if f.name != "preflight.py"] + [BASE / "telegram/bot.py"]
for f in py_files:
    try:
        py_compile.compile(str(f), doraise=True)
        check(f"Syntax: {f.name}", True)
    except py_compile.PyCompileError as e:
        check(f"Syntax: {f.name}", False, str(e))

# -- athletes.json ------------------------------------------------------------
athletes_f = BASE / "config/athletes.json"
athletes = {}
if athletes_f.exists():
    try:
        athletes = json.loads(athletes_f.read_text())
        check("athletes.json valid JSON", True)
    except json.JSONDecodeError as e:
        check("athletes.json valid JSON", False, str(e))
else:
    check("athletes.json exists", False, "file missing — create on VM, not tracked in git")

for slug, cfg in athletes.items():
    if cfg.get("active", True):
        has_chat_id = bool(cfg.get("chat_id"))
        check(f"athletes.json: {slug} has chat_id", has_chat_id,
              "" if has_chat_id else "add chat_id or bot cannot route messages to this athlete")

# -- config.json --------------------------------------------------------------
config_f = BASE / "telegram/config.json"
if config_f.exists():
    try:
        config = json.loads(config_f.read_text())
        check("config.json valid JSON", True)
        check("config.json has bot_token", bool(config.get("bot_token")))
        check("config.json has chat_id", bool(config.get("chat_id")))
    except json.JSONDecodeError as e:
        check("config.json valid JSON", False, str(e))

# -- per-athlete JSON files ---------------------------------------------------
athletes_dir = BASE / "athletes"
for slug_dir in sorted(athletes_dir.iterdir()) if athletes_dir.exists() else []:
    if not slug_dir.is_dir():
        continue
    slug = slug_dir.name
    for fname in ("session-log.json", "current-state.json"):
        f = slug_dir / fname
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
            check(f"{slug}/{fname} valid JSON", True)
            if fname == "session-log.json" and isinstance(data, list):
                ids = [e.get("activity_id") for e in data if e.get("activity_id")]
                dupes = [k for k, v in Counter(ids).items() if v > 1]
                check(f"{slug}/session-log.json no duplicate IDs", not dupes,
                      f"duplicates: {dupes}" if dupes else "")
        except json.JSONDecodeError as e:
            check(f"{slug}/{fname} valid JSON", False, str(e))

# -- box-drawing characters in Python files (break the Edit tool) -------------
BOX_DRAWING = range(0x2500, 0x2580)  # - │ ┌ └ etc.
for f in py_files:
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
        bad = [ch for ch in text if ord(ch) in BOX_DRAWING]
        check(f"No box-drawing: {f.name}", not bad,
              f"found {len(bad)} box-drawing chars — remove them" if bad else "")
    except Exception:
        pass

# -- result -------------------------------------------------------------------
print()
if failures:
    print(f"✗ {len(failures)} check(s) failed. Fix before deploying.")
    sys.exit(1)
else:
    print(f"✓ All checks passed.")
    sys.exit(0)
