#!/usr/bin/env python3
"""Weekly rule check (Jamie, 2 Oct 2026): at most ONE standing rule a week is put back to
each athlete - "still right?" with Still right / Change it / Drop it - instead of asking
them to read their rules. lib/athlete_rules.next_check picks it: a rule on a topic that
goes out of date (when they train, training, health, fuelling), standing a month or more,
not confirmed or asked about in eight weeks. Dated rules end on their own and are never
asked about. The buttons are handled in telegram/bot.py (_handle_rule_check).

Skips tracking-only athletes (they asked for a tracker, not a coach), inactive ones and
anyone with `rule_check: false` in athletes.json.

Crontab (VM): Sunday 18:45, after the weekly plan.
    python3 ClaudeCoach/scripts/rule-check.py [--dry-run] [--athlete X]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
sys.path.insert(0, str(BASE / "lib"))

import athlete_rules                                     # noqa: E402
import planning_pause                                    # noqa: E402


def message(c: dict) -> tuple[str, dict]:
    slug_id = c["callback"]
    text = ("Quick check on one thing I always keep in mind for you:\n\n"
            f"*{c['summary']}*\n\nStill right?")
    markup = {"inline_keyboard": [[
        {"text": "Still right", "callback_data": f"rule:keep:{slug_id}"},
        {"text": "Change it", "callback_data": f"rule:change:{slug_id}"},
        {"text": "Drop it", "callback_data": f"rule:drop:{slug_id}"}]]}
    return text, markup


def run(dry_run: bool = False, only: str | None = None, base: Path | None = None) -> list:
    base = base or BASE
    cfg = json.loads((base / "config" / "athletes.json").read_text())
    sent = []
    for slug, a in cfg.items():
        if only and slug != only:
            continue
        if not isinstance(a, dict) or not a.get("active") or a.get("rule_check") is False:
            continue
        if planning_pause.is_paused(slug, a) or not a.get("chat_id"):
            continue
        c = athlete_rules.next_check(slug, base)
        if not c:
            continue
        c["callback"] = f"{slug}:{c['id']}"
        text, markup = message(c)
        if not dry_run:
            import outbox
            if outbox.record(str(a["chat_id"]), text, reply_markup=markup, source="rule-check",
                             parse_mode="Markdown"):
                athlete_rules.mark_asked(slug, c["id"], base)
        sent.append({"athlete": slug, "id": c["id"], "summary": c["summary"]})
    return sent


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--athlete")
    a = ap.parse_args()
    for s in run(a.dry_run, a.athlete):
        print(f"[rule-check] {s['athlete']}: {s['id']} - {s['summary']}"
              + (" (dry run)" if a.dry_run else ""))
