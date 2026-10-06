"""API-equivalent cost per athlete, by day, from the Claude CLI transcripts on the VM.

The same pricing and job / athlete attribution as scripts/usage-report.py (loaded from
it, one source), kept as per-day sums so Peak's coach page (api/server.py
/api/admin/usage) can show this month, a monthly rate and the total since logging
began. Every ClaudeCoach run has been logged since LOG_START (27 Sep 2026); before
that most scheduled jobs left no transcript, so earlier days are not counted.

Each transcript file is priced once and re-priced only when its size or mtime changes
(chat sessions grow), so a page load after the first costs well under a second.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
LOG_START = date(2026, 9, 27)
RATE_WINDOW_DAYS = 28
DAYS_PER_MONTH = 365.25 / 12          # the average month, for the Avg / month column

_ur = None
_cache: dict = {}          # path -> ((mtime, size), (athlete, job, {day: usd}))
_lock = threading.Lock()


def _report():
    global _ur
    if _ur is None:
        spec = importlib.util.spec_from_file_location(
            "usage_report", BASE / "scripts" / "usage-report.py")
        _ur = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_ur)
    return _ur


def _price_file(f: str, athletes: dict, dev_texts: set) -> tuple:
    ur = _report()
    rows = []
    with open(f, errors="replace") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    msgs = {}
    for r in rows:
        if r.get("type") != "assistant":
            continue
        m = r.get("message") or {}
        ts = r.get("timestamp")
        if not m.get("usage") or not ts:
            continue
        msgs[m.get("id") or r.get("requestId") or id(r)] = (m.get("model"), m["usage"], ts[:10])
    if not msgs:
        return ("system", "none", {}, {})
    ath, job = ur.attribute(f, ur.first_prompt(rows), athletes, dev_texts)
    # Chat turns that changed code, and the whole Dev tab, are dev work, not coaching
    # (usage-report.py DEV).
    dev = ur.dev_message_ids(rows) if job == "chat" else set(msgs) if job == ur.DEV else set()
    days, dev_days = defaultdict(float), defaultdict(float)
    for mid, (model, u, day) in msgs.items():
        # Priced as an API deployment would bill it (usage-report.py): jobs on the
        # 5-minute cache, chats as recorded (1-hour; a person replies minutes apart).
        (dev_days if mid in dev else days)[day] += ur.cost(
            model, u, writes_5m=(job not in ("chat", ur.DEV)))
    return (ath, job, dict(days), dict(dev_days))


def daily(athletes: dict) -> dict:
    """{athlete: {"all": {day: usd}, "chat": {day: usd}}} since LOG_START. "system" holds
    the shared jobs (bug fixer, smoke tests); "dev" Jamie's dev work - the Dev tab, and
    code changed through the coach chat - kept out of his coaching cost."""
    ur = _report()
    out: dict = defaultdict(lambda: {"all": defaultdict(float), "chat": defaultdict(float)})
    start = LOG_START.isoformat()
    dev_texts = ur.dev_tab_texts()
    with _lock:
        for pat in ur.PROJECT_GLOBS:
            for f in glob.glob(str(ur.PROJECTS / pat / "*.jsonl")):
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                key = (st.st_mtime, st.st_size)
                hit = _cache.get(f)
                if not hit or hit[0] != key:
                    try:
                        hit = (key, _price_file(f, athletes, dev_texts))
                    except OSError:
                        continue
                    _cache[f] = hit
                ath, job, days, dev_days = hit[1]
                for day, usd in days.items():
                    if day >= start:
                        out[ath]["all"][day] += usd
                        if job == "chat":
                            out[ath]["chat"][day] += usd
                for day, usd in dev_days.items():
                    if day >= start:
                        out["dev"]["all"][day] += usd
    return out


def summary(athletes: dict, today: date | None = None) -> dict:
    """Per athlete: this week so far, the last 7 days, the average week, this calendar month
    so far, actual spend over the last 28 days (fewer while logging is younger), the total
    since LOG_START, and chat this month - all USD at API list prices."""
    today = today or date.today()
    d = daily(athletes)
    win_start = max(LOG_START, today - timedelta(days=RATE_WINDOW_DAYS - 1))
    n_days = (today - win_start).days + 1
    month = today.strftime("%Y-%m")
    out = {}
    for ath, v in d.items():
        alld, chatd = v["all"], v["chat"]
        last = sum(u for day, u in alld.items() if day >= win_start.isoformat())
        # Weekly view (Jamie, 2 Oct 2026): this week so far (from Monday), the last 7 days,
        # and the average week - over the days since this athlete's first logged cost, so
        # someone who joined later is not averaged over days before they existed.
        mon = (today - timedelta(days=today.weekday())).isoformat()
        d7 = (today - timedelta(days=6)).isoformat()
        first = max(LOG_START, date.fromisoformat(min(alld))) if alld else today
        span = (today - first).days + 1
        out[ath] = {"week": round(sum(u for day, u in alld.items() if day >= mon), 2),
                    "last7": round(sum(u for day, u in alld.items() if day >= d7), 2),
                    "avg_week": round(sum(alld.values()) / span * 7, 2),
                    "avg_month": round(sum(alld.values()) / span * DAYS_PER_MONTH, 2),
                    "month": round(sum(u for day, u in alld.items() if day.startswith(month)), 2),
                    "rate_month": round(last, 2),   # actual spend, last 28 days (not a projection)
                    "total": round(sum(alld.values()), 2),
                    "chat_month": round(sum(u for day, u in chatd.items()
                                            if day.startswith(month)), 2),
                    "chat_by_month": _by_month(chatd)}
    return {"athletes": out, "since": LOG_START.isoformat(), "rate_days": n_days,
            "generated": datetime.now().isoformat(timespec="seconds")}


def _by_month(days: dict) -> dict:
    m = defaultdict(float)
    for day, usd in days.items():
        m[day[:7]] += usd
    return {k: round(v, 6) for k, v in sorted(m.items())}
