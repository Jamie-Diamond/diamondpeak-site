#!/usr/bin/env python3
"""Tracking-only mode — planning and adherence surfaces paused for one athlete.

Why this exists (6 Sep 2026). An athlete asked to keep ClaudeCoach as a TRACKER
and drop the coaching: he wanted his rides named, described and analysed, and he
wanted to be able to ask questions, but he did not want a prescribed week, a
daily session card, or a system that tells him he missed a target when he had
simply chosen a week off. The only lever that existed was `active: false` in
config/athletes.json, which is all-or-nothing: it silences the activity watcher
too, so "I want the tracker" and "I don't want the coaching" could not both be
true. This module is that missing middle setting.

Paused means: nothing is PRESCRIBED and nothing is JUDGED.

  OFF  weekly plan build, daily prescription, blueprint generation,
       morning / evening check-ins, night-before brief, session sync,
       watchdog triggers, plan audit, macro projection, weekly summary
  ON   activity watcher (name / description / analysis of what was actually
       done), the Telegram bot (chat, questions, data lookups), public site
       data, FTP tracking

Two sources, either of which pauses an athlete:

  1. config/planning-paused.json — TRACKED IN GIT, so a pause takes effect on
     the VM through the normal 30-minute cc-gitpull with no shell access to the
     box. This is the one to edit.
  2. `"planning_paused": true` in config/athletes.json, for a pause set on the
     VM alongside the rest of an athlete's config.

Unpausing is deleting the entry (or setting `"paused": false`). Nothing here
expires on its own: a pause the athlete asked for should end when they ask for
it to end, not when a timer runs out.
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent      # ClaudeCoach/
PAUSED_FILE = BASE / "config" / "planning-paused.json"

_DEFAULT_REASON = "coach-set: tracking only, no planning"


def _load(path=None) -> dict:
    """The tracked pause file, or {} when absent/unreadable.

    Unreadable is deliberately NOT an error: a malformed pause file must not
    take down the activity watcher or the bot. It fails OPEN (nobody paused),
    which is the pre-existing behaviour of every caller.
    """
    p = Path(path or PAUSED_FILE)
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def entry(slug: str, path=None) -> dict:
    """The pause record for `slug` ({} when not paused via the tracked file)."""
    e = _load(path).get(slug)
    if e is True:                      # shorthand: {"calum": true}
        return {"paused": True, "reason": _DEFAULT_REASON}
    if isinstance(e, dict) and e.get("paused", True):
        return e
    return {}


def is_paused(slug: str, cfg: dict | None = None, path=None) -> bool:
    """True when planning/adherence is paused for this athlete.

    `cfg` is the athlete's config/athletes.json block when the caller already
    has it (every scheduled script does) — passing it avoids a second read and
    lets the VM-side `planning_paused` flag work.
    """
    if cfg is not None and cfg.get("planning_paused"):
        return True
    return bool(entry(slug, path))


def reason(slug: str, cfg: dict | None = None, path=None) -> str:
    """One line explaining the pause; '' when the athlete is not paused."""
    if not is_paused(slug, cfg, path):
        return ""
    return (entry(slug, path).get("reason")
            or (cfg or {}).get("planning_paused_reason")
            or _DEFAULT_REASON)


def since(slug: str, path=None) -> str:
    return entry(slug, path).get("since", "")


def paused_slugs(path=None) -> list:
    """Every slug paused via the tracked file (not the athletes.json flag)."""
    return sorted(s for s in _load(path) if entry(s, path))


def skip_line(slug: str, script: str, cfg: dict | None = None, path=None) -> str:
    """The stderr line a scheduled script logs when it stands down."""
    return (f"[{slug}] SKIP {script}: planning paused "
            f"({reason(slug, cfg, path)})")


# -- Coach prompt --------------------------------------------------------------
# Injected into the athlete's system prompt (lib/engine.system_prompt_with_level)
# for every chat surface. The scheduled scripts stand down in code; the bot
# cannot, because the athlete is talking to it — so the pause has to reach the
# model as an instruction. Written as behaviour, not as a mode name, because
# "tracking-only mode" on its own gets interpreted as "be brief".
_PROMPT_BLOCK = (
    "TRACKING-ONLY MODE — THIS OVERRIDES ANY COACHING INSTRUCTION ABOVE. "
    "{name} has paused the coaching side and uses ClaudeCoach as a training TRACKER. "
    "Do: log and describe what they actually did, answer questions about their data, "
    "compare sessions, and give advice when they ASK for it. "
    "Do NOT: prescribe or build a training week, publish sessions to their calendar, "
    "set targets, or reference a plan, a phase, a weekly TSS/load target or a race "
    "countdown as something they are meant to hit. "
    "Never tell them they missed a session, missed a target, fell behind, or need to "
    "catch up, and never chase a gap in their training — a quiet week is a choice they "
    "have made, not a failure to report. If they ask what they should do, answer as a "
    "knowledgeable friend in a sentence or two, without turning it into a plan. "
    "If they ask for structured coaching or a plan back, tell them planning is paused "
    "and Jamie can switch it back on."
)


def prompt_block(slug: str, first_name: str = "", cfg: dict | None = None, path=None) -> str:
    """The instruction block for a paused athlete's prompt; '' when not paused."""
    if not is_paused(slug, cfg, path):
        return ""
    return _PROMPT_BLOCK.format(name=first_name or "This athlete")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args:
        slug = args[0]
        print(f"{slug}: {'PAUSED — ' + reason(slug) if is_paused(slug) else 'active'}")
    else:
        print("\n".join(f"{s}\t{reason(s)}" for s in paused_slugs()) or "nobody paused")
