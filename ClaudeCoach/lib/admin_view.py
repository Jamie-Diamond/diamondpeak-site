"""Peak's "Athletes & usage" page for the coach (Jamie, 1 Oct 2026: "an admin page so I
don't need to ask you every time"): one row per person with their sign-up stage, last
message, chat this month against the allowance, and API-equivalent cost.

    rows(users, athletes, pending, onboarding, costs, today) -> [row]

A row: {name, email, slug, stage, stage_kind, last_message,
        chat: {replies, usd, allowance, exempt} | None,
        cost: {month, rate_month, total} | None}

stage_kind orders the list and colours the tag: coached, test_week, approval, tracking,
signing_up, invited. Costs come from lib/usage_ledger.summary (transcripts priced at API
list prices); chat replies and spend from each athlete's chat-usage.json ledger.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/

# Sign-up question keys (telegram/bot.py onboarding) in plain words.
QUESTION = {
    "name": "their name", "race": "race or goal", "goal": "choosing a goal",
    "goal_sports": "choosing sports", "icu_has": "Intervals.icu: do they use it",
    "icu_setup": "setting up Intervals.icu", "icu_key": "Intervals.icu key",
    "icu_fix": "fixing their Intervals.icu set-up", "a_goal": "race goal",
    "experience": "experience", "injuries": "injuries", "max_hours": "hours per week",
    "hr_source": "heart rate", "power": "power meter", "recent_tests": "recent tests",
    "ftp": "FTP", "run_threshold": "run threshold", "swim_css": "swim CSS",
    "weight": "weight", "est_run": "rough run figure", "est_bike": "rough FTP",
    "est_swim": "rough swim figure", "compare_race": "comparison race", "heat": "heat",
    "fuel": "fuelling", "level": "coaching level", "slug": "account handle (last question)",
}
ORDER = {"coached": 0, "test_week": 1, "approval": 2, "tracking": 3, "signing_up": 4,
         "invited": 5}


def _iso_date(v) -> str | None:
    s = str(v or "")[:10]
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _mtime_iso(p: Path) -> str | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")
    except OSError:
        return None


def last_message(slug: str | None = None, chat_id: str | None = None,
                 base: Path = BASE) -> str | None:
    """When the person last wrote to the coach: the newest athlete line in their chat
    history, or for a sign-up, the last change to their sign-up chat."""
    if slug:
        try:
            hist = json.loads((base / "athletes" / slug / "telegram" / "history.json").read_text())
        except (OSError, ValueError):
            hist = []
        for e in reversed(hist if isinstance(hist, list) else []):
            if isinstance(e, dict) and e.get("user") and e.get("ts"):
                return str(e["ts"])[:16]
        return None
    if chat_id:
        return _mtime_iso(base / "config" / "web-signup" / str(chat_id) / "web-outbox.jsonl")
    return None


def athlete_stage(slug: str, cfg: dict, today: date, base: Path = BASE) -> tuple:
    """(kind, words) for a scaffolded athlete."""
    import planning_pause
    import plan_tools as pt
    import goals as goals_lib
    if not cfg.get("active"):
        return "approval", "Signed up · waiting for your approval"
    if planning_pause.is_paused(slug, cfg):
        return "tracking", "Tracking only"
    try:
        bl = json.loads((base / "athletes" / slug / "baseline.json").read_text())
    except (OSError, ValueError):
        bl = {}
    if bl.get("status") == "pending":
        return "test_week", "Approved · test week being scheduled"
    if bl.get("status") == "active":
        end = ((bl.get("window") or {}).get("end") or "")[:10]
        return "test_week", "Test week" + (f" · ends {end}" if end else "")
    if pt.goal_active(cfg, today):
        return "coached", "Coached · goal: " + goals_lib.GOALS[cfg["goal"]["type"]]["label"]
    if pt.a_race_ahead(cfg, today) and cfg.get("race_name"):
        return "coached", f"Coached · {cfg['race_name']}"
    return "coached", "Coached"


def rows(users: dict, athletes: dict, pending: list, onboarding: dict, costs: dict,
         today: date | None = None, admin_chat_id: str = "", base: Path = BASE) -> list:
    import chat_limits
    today = today or date.today()
    month = today.strftime("%Y-%m")
    pending = {str(x) for x in pending or []}
    by_cid = {str(a.get("chat_id") or ""): s for s, a in athletes.items() if isinstance(a, dict)}
    seen, out = set(), []

    def athlete_row(slug, email=None):
        cfg = athletes.get(slug) or {}
        kind, words = athlete_stage(slug, cfg, today, base)
        try:
            led = json.loads((base / "athletes" / slug / "chat-usage.json").read_text())
        except (OSError, ValueError):
            led = {}
        m = led.get(month) or {}
        c = (costs or {}).get(slug)
        return {"name": cfg.get("name") or slug, "email": email, "slug": slug,
                "stage": words, "stage_kind": kind,
                "last_message": last_message(slug, base=base),
                "chat": {"replies": int(m.get("replies") or 0),
                         "usd": round(float(m.get("usd") or 0), 2),
                         "allowance": chat_limits.allowance(cfg),
                         "exempt": chat_limits.exempt(cfg, admin_chat_id)},
                "cost": ({k: c.get(k) for k in ("month", "rate_month", "total")} if c else None)}

    for email, u in (users or {}).items():
        if not isinstance(u, dict):
            continue
        cid = str(u.get("chat_id") or "")
        slug = u.get("slug") or by_cid.get(cid)
        if slug and slug in athletes:
            seen.add(slug)
            out.append(athlete_row(slug, email))
            continue
        if not cid:
            continue
        st = (onboarding or {}).get(cid)
        invited = _iso_date(u.get("invited"))
        if st:
            q = QUESTION.get(st.get("current_key"), st.get("current_key") or "?")
            name = ((st.get("answers") or {}).get("name") or "").strip() or email.split("@")[0]
            out.append({"name": name, "email": email, "slug": None,
                        "stage": f"Signing up · at {q}", "stage_kind": "signing_up",
                        "last_message": last_message(chat_id=cid, base=base),
                        "chat": None, "cost": None})
        elif cid in pending:
            out.append({"name": email.split("@")[0], "email": email, "slug": None,
                        "stage": "Invited" + (f" {invited}" if invited else "") + " · not started",
                        "stage_kind": "invited", "last_message": None,
                        "chat": None, "cost": None})
    # Athletes with no Peak login (Telegram-era, or not linked yet).
    for slug, a in athletes.items():
        if isinstance(a, dict) and slug not in seen:
            out.append(athlete_row(slug))
    out.sort(key=lambda r: (ORDER.get(r["stage_kind"], 9), r["name"].lower()))
    return out


def totals(costs: dict) -> dict:
    """All athletes plus the shared jobs, for the page's footer row."""
    t = {"month": 0.0, "rate_month": 0.0, "total": 0.0}
    for c in (costs or {}).values():
        for k in t:
            t[k] += float(c.get(k) or 0)
    return {k: round(v, 2) for k, v in t.items()}
