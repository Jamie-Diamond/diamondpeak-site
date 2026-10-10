#!/usr/bin/env python3
"""Tells Jamie in Peak's Dev tab when the Haiku shadow week has finished (lib/shadow.py),
so the comparison gets reviewed. Run once by cron the morning after SHADOW_UNTIL."""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))
import outbox   # noqa: E402
import shadow   # noqa: E402

from datetime import date, timedelta   # noqa: E402
if date.today() != shadow.SHADOW_UNTIL + timedelta(days=1):   # cron runs it daily; it posts once
    sys.exit(0)

runs = list(shadow.SHADOW_DIR.glob("*/*.json"))
by = {}
for f in runs:
    by[f.name.split("-")[0]] = by.get(f.name.split("-")[0], 0) + 1
cid = json.loads((BASE / "config" / "athletes.json").read_text())["jamie"]["chat_id"]
parts = ", ".join(f"{n} {k}" for k, n in sorted(by.items())) or "none"
outbox.record(cid, f"*Haiku shadow week finished.* {len(runs)} messages written alongside "
                   f"Sonnet's ({parts}), none sent.\n\nAsk me here to *review the shadow week* "
                   "and I'll trace each difference to the prompt that allowed it, fix the prompts, "
                   "then recommend what can move to Haiku.",
              source="dev-session", tab="dev")
