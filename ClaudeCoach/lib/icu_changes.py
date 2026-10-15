"""Webhooks instead of polling: the cron jobs skip an athlete when Intervals.icu has told
us nothing changed.

Why (15 Oct 2026): Peak's OAuth link to Intervals.icu (lib/icu_link.py) has a shared
allowance of 8,000 calls a day for the whole app, live since 15 Oct, and the cron jobs
were making about 2,000-3,000 calls per athlete per day by asking "anything new?" every
5-15 minutes. Intervals.icu's webhooks (api/server.py /api/icu/webhook) say when
something changes, so a job only needs to ask then.

  record(slug, type)       the webhook receiver: something changed for this athlete
  due(slug, job, every)    a cron job: should I fetch this athlete this time?
  mark_run(slug, job, t)   a cron job: fetched, as of t (the time it STARTED)

Only athletes who linked through Peak (an OAuth token) get webhooks, so only they are
ever skipped; everyone on an API key is polled exactly as before. Safety nets, so a
missed webhook costs a delay, never a silent gap:
  - every job still fetches at least every `every` seconds, and on a new day;
  - an athlete whose webhooks have gone quiet for SILENT_AFTER is polled as before
    (sleep / HRV alone send several a day, so a whole day of silence means the pipe,
    not the athlete).
CC_ICU_WEBHOOK_GATE=0 switches the skipping off everywhere.

State: athletes/<slug>/icu-changes.json (webhook side) and athletes/<slug>/icu-runs/<job>
(one file per job, so two jobs never overwrite each other's time).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
CHANGES_NAME = "icu-changes.json"
RUNS_DIR = "icu-runs"
SILENT_AFTER = 26 * 3600
# What each job cares about. None = any change.
ACTIVITY_TYPES = ("ACTIVITY_UPLOADED", "ACTIVITY_ANALYZED", "ACTIVITY_UPDATED",
                  "ACTIVITY_DELETED", "ACTIVITY_ACHIEVEMENTS")
NOT_A_CHANGE = {"TEST", "APP_SCOPE_CHANGED"}


def _adir(slug: str) -> Path:
    return BASE / "athletes" / slug


def _load(slug: str) -> dict:
    try:
        d = json.loads((_adir(slug) / CHANGES_NAME).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def record(slug: str, etype: str, now: float | None = None) -> None:
    """A webhook arrived for this athlete. Called under the receiver's lock."""
    now = time.time() if now is None else now
    d = _load(slug)
    d["last_webhook"] = now
    if etype not in NOT_A_CHANGE:
        d.setdefault("last", {})[etype] = now
        d["last_change"] = now
    f = _adir(slug) / CHANGES_NAME
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(f)


def webhook_driven(slug: str, now: float | None = None) -> bool:
    """Linked through Peak and webhooks are arriving: safe to skip a poll."""
    if os.environ.get("CC_ICU_WEBHOOK_GATE", "1") == "0":
        return False
    if not (_adir(slug) / "icu_tokens.json").exists():
        return False
    last = _load(slug).get("last_webhook")
    now = time.time() if now is None else now
    return bool(last) and now - float(last) < SILENT_AFTER


def changed_since(slug: str, since: float, types=None) -> bool:
    d = _load(slug)
    if types is None:
        return d.get("last_change") is not None and float(d["last_change"]) >= since
    last = d.get("last") or {}
    return any(last.get(t) is not None and float(last[t]) >= since for t in types)


def last_run(slug: str, job: str) -> float | None:
    try:
        return float((_adir(slug) / RUNS_DIR / job).read_text().strip())
    except (OSError, ValueError):
        return None


def mark_run(slug: str, job: str, started: float) -> None:
    d = _adir(slug) / RUNS_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / job).write_text(f"{started:.0f}\n")
    except OSError:
        pass


def reason(slug: str, job: str, every: float, types=None, now: float | None = None) -> str:
    """Why `job` should fetch this athlete now, or "" when it needn't:
      "polled"   not webhook-driven: every run, as before
      "first"    this job has never run for them
      "changed"  something it cares about changed since its last run (a change during
                 that run counts: runs are marked with their START time)
      "interval" the safety net: `every` seconds have passed
      "new-day"  the safety net: the first run of a local day"""
    now = time.time() if now is None else now
    if not webhook_driven(slug, now):
        return "polled"
    last = last_run(slug, job)
    if last is None:
        return "first"
    if changed_since(slug, last, types):
        return "changed"
    if now - last >= every:
        return "interval"
    if time.strftime("%Y-%m-%d", time.localtime(last)) != time.strftime("%Y-%m-%d", time.localtime(now)):
        return "new-day"
    return ""


def due(slug: str, job: str, every: float, types=None, now: float | None = None) -> bool:
    """Should `job` fetch this athlete now? (reason() says why.)"""
    return bool(reason(slug, job, every, types, now))
