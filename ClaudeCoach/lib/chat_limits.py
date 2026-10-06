#!/usr/bin/env python3
"""Per-athlete monthly chat allowance (28-29 Sep 2026, Jamie).

Why: at API prices chat is the one cost an athlete drives. The scheduled coaching
(plan, debriefs, daily cards) costs roughly the same for everyone and is never limited
here. Chat varies tenfold between athletes (Jamie ~$98/month, Kathryn ~$12), so it gets
an allowance.

Agreed behaviour:
  - Every chat reply's cost is metered per athlete per calendar month (the CLI's own
    total_cost_usd for that run, i.e. API list price). Resets on the 1st.
  - Default allowance $15/month; athletes.json `chat_allowance_usd` overrides it. Was $20;
    6 Oct 2026, Jamie: price at most £45 a month including VAT, at a 50% margin, so an
    athlete may cost £18.75 (~$25). Less ~$9 of scheduled jobs once the daily messages are
    on Haiku leaves ~$15 for chat.
  - At 80%: chat steps DOWN one model (Opus -> Sonnet, Sonnet -> Haiku), and both the
    athlete and Jamie get a one-line warning, once that month.
  - At 100%: chat carries on, on the stepped-down model, capped at DAILY_CAP_OVER
    replies a day. Athlete and Jamie told once that month. Never a hard stop.
  - Jamie (admin, or `chat_limit_exempt: true`) is exempt.
The warnings go out at the athlete's next message after the crossing, which is also the
first reply the step-down applies to.

Ledger: athletes/<slug>/chat-usage.json
  {"2026-09": {"usd": 12.3, "replies": 81, "warned80": false, "warned100": false,
               "days": {"2026-09-29": 4}}}
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/
DEFAULT_ALLOWANCE_USD = 15.0
STEP_DOWN_AT = 0.80
DAILY_CAP_OVER = 5            # Jamie, 29 Sep 2026
LEDGER = "chat-usage.json"

STEP_DOWN = {"opus": "sonnet", "sonnet": "haiku", "haiku": "haiku", "fable": "opus"}

_lock = threading.Lock()


def _path(slug: str) -> Path:
    return BASE / "athletes" / slug / LEDGER


def _load(slug: str) -> dict:
    try:
        d = json.loads(_path(slug).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(slug: str, d: dict) -> None:
    p = _path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".cu-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _month(today: date) -> str:
    return today.strftime("%Y-%m")


def _next_reset(today: date) -> date:
    return date(today.year + (today.month == 12), today.month % 12 + 1, 1)


def exempt(cfg: dict | None, admin_chat_id: str = "") -> bool:
    cfg = cfg or {}
    return bool(cfg.get("chat_limit_exempt")) or (
        bool(admin_chat_id) and str(cfg.get("chat_id", "")) == str(admin_chat_id))


def allowance(cfg: dict | None) -> float:
    try:
        v = float((cfg or {}).get("chat_allowance_usd") or 0)
        return v if v > 0 else DEFAULT_ALLOWANCE_USD
    except (TypeError, ValueError):
        return DEFAULT_ALLOWANCE_USD


# ── one run's own cost ─────────────────────────────────────────────────────────
# The CLI's total_cost_usd is the SESSION's running total whenever it restores a resumed
# session's cost state (verified on the VM, 1 Oct 2026: three resumed Haiku replies
# reported 0.016, 0.019, 0.021 while each cost 0.016, 0.002, 0.002). Charging it per
# reply counted every earlier reply again: Jamie's 12 chat replies on 1 Oct metered
# $48.90 against $8.90 priced from the transcripts. The result's `usage` is this run's
# own tokens (summed over its turns) and `modelUsage` the restored running totals, so a
# run whose modelUsage exceeds its usage carried an earlier total in. Its own cost is then
# the total less the last total seen for that session, or, with none on record, the
# total scaled by its share of the (price-weighted) tokens.
_TOKEN_WEIGHTS = {"input": 1.0, "cache_creation": 1.25, "cache_read": 0.1, "output": 5.0}
SESSIONS_KEY = "_sessions"         # ledger: last total_cost_usd seen per CLI session
_SESSIONS_KEPT = 40


def _run_tokens(run: dict) -> dict | None:
    u = run.get("usage")
    if not isinstance(u, dict):
        return None
    return {"input": u.get("input_tokens") or 0,
            "cache_creation": u.get("cache_creation_input_tokens") or 0,
            "cache_read": u.get("cache_read_input_tokens") or 0,
            "output": u.get("output_tokens") or 0}


def _session_tokens(run: dict) -> dict | None:
    mu = run.get("modelUsage")
    if not isinstance(mu, dict) or not mu:
        return None
    out = dict.fromkeys(_TOKEN_WEIGHTS, 0)
    for v in mu.values():
        if isinstance(v, dict):
            out["input"] += v.get("inputTokens") or 0
            out["cache_creation"] += v.get("cacheCreationInputTokens") or 0
            out["cache_read"] += v.get("cacheReadInputTokens") or 0
            out["output"] += v.get("outputTokens") or 0
    return out


def _weighted(t: dict) -> float:
    return sum(t[k] * w for k, w in _TOKEN_WEIGHTS.items())


def run_cost(slug: str, run, today: date | None = None) -> float:
    """This CLI run's own cost in USD, from its result (json / stream-json `result`).
    A plain number is taken as it is. Remembers the session's total for the next run."""
    if not isinstance(run, dict):
        try:
            return max(0.0, float(run or 0))
        except (TypeError, ValueError):
            return 0.0
    try:
        total = float(run.get("total_cost_usd") or 0)
    except (TypeError, ValueError):
        return 0.0
    sid = str(run.get("session_id") or "")
    mine, sess_t = _run_tokens(run), _session_tokens(run)
    restored = bool(mine and sess_t and sum(sess_t.values()) > sum(mine.values()) * 1.01 + 10)
    with _lock:
        d = _load(slug)
        sessions = d.get(SESSIONS_KEY) if isinstance(d.get(SESSIONS_KEY), dict) else {}
        prev = sessions.get(sid) if sid else None
        if not restored:
            cost = total
        elif prev is not None and total >= float(prev):
            cost = total - float(prev)
        else:
            cost = total * min(1.0, _weighted(mine) / max(1e-9, _weighted(sess_t)))
        if sid and slug:
            sessions.pop(sid, None)
            sessions[sid] = total
            while len(sessions) > _SESSIONS_KEPT:
                sessions.pop(next(iter(sessions)))
            d[SESSIONS_KEY] = sessions
            _save(slug, d)
    return max(0.0, round(cost, 6))


def record(slug: str, usd: float | None, today: date | None = None) -> None:
    """Add one reply's cost. Called by the engine after every athlete chat run."""
    if not slug or usd is None:
        return
    today = today or date.today()
    with _lock:
        d = _load(slug)
        m = d.setdefault(_month(today), {"usd": 0.0, "replies": 0, "warned80": False,
                                         "warned100": False, "days": {}})
        m["usd"] = round(m["usd"] + max(0.0, float(usd)), 6)
        m["replies"] += 1
        m["days"][today.isoformat()] = m["days"].get(today.isoformat(), 0) + 1
        _save(slug, d)


def status(slug: str, cfg: dict | None, today: date | None = None) -> dict:
    today = today or date.today()
    m = _load(slug).get(_month(today), {})
    spent = float(m.get("usd", 0.0))
    allow = allowance(cfg)
    pct = spent / allow if allow else 0.0
    tier = "capped" if pct >= 1.0 else ("stepped_down" if pct >= STEP_DOWN_AT else "normal")
    return {"spent": round(spent, 2), "allowance": allow, "pct": pct, "tier": tier,
            "replies_today": int((m.get("days") or {}).get(today.isoformat(), 0)),
            "warned80": bool(m.get("warned80")), "warned100": bool(m.get("warned100")),
            "resets": _next_reset(today)}


def gate(slug: str, cfg: dict | None, first_name: str, admin_chat_id: str = "",
         today: date | None = None) -> dict:
    """Decide how the next chat reply may run. Returns
      {"step_down": bool, "blocked": bool, "block_text": str,
       "athlete_notice": str, "admin_notice": str}
    Notices are returned at most once per threshold per month (marked as sent here)."""
    out = {"step_down": False, "blocked": False, "block_text": "",
           "athlete_notice": "", "admin_notice": ""}
    if exempt(cfg, admin_chat_id):
        return out
    today = today or date.today()
    st = status(slug, cfg, today)
    if st["tier"] == "normal":
        return out
    out["step_down"] = True
    reset = f"{st['resets']:%a %d %b}"
    money = f"${st['spent']:.2f} of ${st['allowance']:.0f}"
    with _lock:
        d = _load(slug)
        m = d.setdefault(_month(today), {"usd": 0.0, "replies": 0, "warned80": False,
                                         "warned100": False, "days": {}})
        if st["tier"] == "capped" and not m.get("warned100"):
            m["warned100"] = m["warned80"] = True
            # The day the cap starts, the daily count starts from here, so "up to N a
            # day" is true today too rather than blocking the very next message.
            m["cap_base"] = {today.isoformat(): st["replies_today"]}
            out["athlete_notice"] = (
                f"You've used this month's chat allowance. Chat carries on, on a lighter "
                f"model, for up to {DAILY_CAP_OVER} messages a day until it resets on {reset}. "
                f"Your plan, debriefs and daily cards aren't affected.")
            out["admin_notice"] = (f"Chat allowance: {first_name} has used 100% ({money}). "
                                   f"Now capped at {DAILY_CAP_OVER} replies/day on a lighter "
                                   f"model until {reset}.")
        elif st["tier"] == "stepped_down" and not m.get("warned80"):
            m["warned80"] = True
            out["athlete_notice"] = (
                f"Heads up: you've used 80% of this month's chat allowance. Chat carries on, on "
                f"a lighter model, until it resets on {reset}. Your plan, debriefs and daily "
                f"cards aren't affected.")
            out["admin_notice"] = (f"Chat allowance: {first_name} is at 80% ({money}). Chat "
                                   f"stepped down a model until {reset}.")
        base = int((m.get("cap_base") or {}).get(today.isoformat(), 0))
        _save(slug, d)
    if st["tier"] == "capped" and st["replies_today"] - base >= DAILY_CAP_OVER:
        out["blocked"] = True
        out["block_text"] = (f"That's today's {DAILY_CAP_OVER} messages on this month's chat "
                             f"allowance; chat opens again tomorrow. Your plan, debriefs and "
                             f"daily cards carry on as normal.")
    return out


def step_down(model: str) -> str:
    return STEP_DOWN.get(model, model)


def report(athletes: dict, admin_chat_id: str = "", today: date | None = None) -> str:
    """For the /limits admin command."""
    today = today or date.today()
    lines = [f"*Chat allowances, {today:%B}*"]
    for slug, cfg in athletes.items():
        if not cfg.get("active"):
            continue
        st = status(slug, cfg, today)
        name = (cfg.get("name") or slug).split()[0]
        if exempt(cfg, admin_chat_id):
            lines.append(f"• {name}: ${st['spent']:.2f} this month (exempt)")
            continue
        lines.append(f"• {name}: ${st['spent']:.2f} of ${st['allowance']:.0f} "
                     f"({st['pct']:.0%}) · {st['tier'].replace('_', ' ')}")
    lines.append("\n_Set one: /limits <handle> <dollars>_")
    return "\n".join(lines)
