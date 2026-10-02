#!/usr/bin/env python3
"""The watchdog's triggers, decided in Python. The model only runs when one is new.

Why (2 Oct 2026). The daily watchdog sent Sonnet a ~40k-token prompt every morning per
athlete to evaluate T1-T11, and most mornings the answer was "nothing new" - ~$0.12 a run,
~$3.60 per athlete a month, for arithmetic. Jamie approved: "Watchdog checks in Python, so
the model only runs when a check fires." T9 and T12 were already Python; this moves the
rest. The model still writes the log line and the suggested adjustment for a NEW trigger,
because that suggestion uses the athlete's context (what they already agreed, the block
they are in), and it still does T9b, which reads prose (decision-points.md), but only when
that file or the open-actions store has changed since it last looked.

Each check returns {"trigger", "tier", "items", "signal"}: `items` identify what fired
(the missed session ids, the flagged rides, the week), `signal` is the finished line with
the real numbers. A trigger is NEW unless every one of its items was already logged for
that trigger in the last SUPPRESS_DAYS days - the prompt's long-standing daily-nag rule,
now kept in athletes/<slug>/watchdog-state.json instead of being re-read from prose.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/
STATE_NAME = "watchdog-state.json"
SUPPRESS_DAYS = 3
Z2_MIN_S = 45 * 60        # T6: shorter rides are not Z2 sessions
KEEP_DAYS = 21


def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _family(t: str) -> str:
    t = str(t or "")
    if "Ride" in t:
        return "Ride"
    if "Run" in t:
        return "Run"
    if "Swim" in t:
        return "Swim"
    return t


def _fire(trigger, tier, items, signal) -> dict:
    return {"trigger": trigger, "tier": tier, "items": sorted(str(i) for i in items), "signal": signal}


# ── the triggers ─────────────────────────────────────────────────────────────

def t1_atl_over_ctl(wellness: list) -> dict | None:
    """ATL > CTL + 25 for 3+ consecutive days, ending at the latest day."""
    rows = [w for w in sorted(wellness, key=lambda w: str(w.get("id")))
            if w.get("ctl") is not None and w.get("atl") is not None]
    streak = []
    for w in reversed(rows):
        if w["atl"] > w["ctl"] + 25:
            streak.append(w)
        else:
            break
    if len(streak) < 3:
        return None
    last, first = streak[0], streak[-1]
    return _fire("T1", 2, [f"from {first['id']}"],
                 f"ATL {last['atl']:.0f} vs CTL {last['ctl']:.0f} (ATL > CTL + 25) for {len(streak)} "
                 f"days since {first['id']}")


def t2_ankle_ramp(wellness: list, ankle: dict | None, today: date) -> dict | None:
    """CTL ramp > 4/wk while the ankle is still in rehab (athletes with an ankle block)."""
    if not ankle or ankle.get("quality_sessions_resumed"):
        return None
    by = {str(w.get("id"))[:10]: w.get("ctl") for w in wellness if w.get("ctl") is not None}
    if not by:
        return None
    latest = max(by)
    week_ago = (date.fromisoformat(latest) - timedelta(days=7)).isoformat()
    if week_ago not in by:
        return None
    ramp = by[latest] - by[week_ago]
    if ramp <= 4:
        return None
    return _fire("T2", 2, [f"week to {latest}"],
                 f"CTL ramp +{ramp:.1f}/wk ({by[week_ago]:.1f} on {week_ago} -> {by[latest]:.1f} on "
                 f"{latest}) while the ankle is still in rehab (cap +4/wk)")


def t3_hrv_down(wellness: list, today: date) -> dict | None:
    """7-day HRV average down more than 7% on the 7 days before. Needs 4+ readings each."""
    def window(lo, hi):
        return [w["hrv"] for w in wellness if w.get("hrv")
                and lo <= (_d(w.get("id")) or date.min) <= hi]
    now = window(today - timedelta(days=6), today)
    before = window(today - timedelta(days=13), today - timedelta(days=7))
    if len(now) < 4 or len(before) < 4:
        return None
    a, b = sum(now) / len(now), sum(before) / len(before)
    change = (a - b) / b * 100
    if change >= -7:
        return None
    return _fire("T3", 1, ["hrv"],
                 f"7-day HRV average {a:.0f} vs {b:.0f} the 7 days before ({change:+.0f}%)")


def t4_short_sleep(wellness: list, today: date) -> dict | None:
    """Sleep under 7 h on 3+ of the last 7 days. Silent with no sleep data."""
    nights = [(str(w["id"])[:10], w["sleepSecs"] / 3600) for w in wellness
              if w.get("sleepSecs") and today - timedelta(days=6) <= (_d(w.get("id")) or date.min) <= today]
    short = [(d, h) for d, h in nights if round(h, 1) < 7]     # 6h57 reads as 7.0 h, not short
    if len(short) < 3:
        return None
    return _fire("T4", 1, [d for d, _ in short],
                 f"sleep under 7 h on {len(short)} of the last 7 nights: "
                 + ", ".join(f"{d} {int(h)}h{round(h % 1 * 60):02d}" for d, h in short))


def t5_missed(events: list, acts: list, today: date) -> dict | None:
    """2+ planned workouts in the last 6 full days with no matching activity. A workout
    counts as done when Intervals.icu paired it, or an unpaired activity of the same sport
    was done that day (a treadmill run for a planned run)."""
    lo = today - timedelta(days=6)
    paired = {str(e.get("paired_activity_id")) for e in events if e.get("paired_activity_id")}
    spare = [a for a in acts if str(a.get("id")) not in paired]
    missed = []
    for e in sorted(events, key=lambda e: str(e.get("start_date_local"))):
        d = _d(e.get("start_date_local"))
        if not d or not (lo <= d < today) or e.get("paired_activity_id"):
            continue
        hit = next((a for a in spare if _d(a.get("start_date_local")) == d
                    and _family(a.get("type")) == _family(e.get("type"))), None)
        if hit:
            spare.remove(hit)
            continue
        missed.append(e)
    if len(missed) < 2:
        return None
    return _fire("T5", 1, [e.get("id") for e in missed],
                 f"{len(missed)} planned sessions missed in the last 7 days: "
                 + "; ".join(f"{_d(e['start_date_local']):%a %d %b} {e.get('name')}" for e in missed))


def t6_decoupling(acts: list, today: date, untrusted: set = frozenset()) -> dict | None:
    """Aerobic decoupling over 5% on any Z2 ride (IF < 0.75, 45 min or more) in the last 7
    days. Decoupling on a short errands ride is noise (Jamie's 27 Sep errands ride read 5.6%)."""
    lo = today - timedelta(days=6)
    rides = [a for a in acts if "Ride" in str(a.get("type"))
             and (a.get("moving_time") or 0) >= Z2_MIN_S
             and lo <= (_d(a.get("start_date_local")) or date.min) <= today
             and a.get("icu_intensity") is not None and a["icu_intensity"] < 75
             and a.get("decoupling") is not None and a["decoupling"] > 5
             and str(a.get("id")) not in untrusted]
    if not rides:
        return None
    return _fire("T6", 1, [a["id"] for a in rides],
                 "aerobic decoupling over 5% on Z2 rides: " + "; ".join(
                     f"{_d(a['start_date_local']):%a %d %b} {a.get('name')} {a['decoupling']:.1f}% "
                     f"(IF {a['icu_intensity'] / 100:.2f})" for a in rides))


def t7_t8_heat(heat: dict, heat_log: list, today: date, maintenance_floor: float,
               protocol_floor: float) -> list:
    """Heat dose over 14 days (an entry without a dose counts 1.0) against the floor that
    applies: maintenance before the protocol starts (opt-in), the protocol target after."""
    if not heat.get("active") or heat.get("silent"):
        return []
    lo = today - timedelta(days=13)
    recent = [h for h in heat_log or [] if lo <= (_d(h.get("date")) or date.min) <= today]
    dose = round(sum(float(h.get("dose", 1.0) or 0) for h in recent), 2)
    starts = _d(heat.get("starts")) or today
    out = []
    if today < starts:
        if heat.get("maintenance") and dose < maintenance_floor:
            out.append(_fire("T7", 2, [f"dose {dose:.1f}"],
                             f"heat maintenance dose {dose:.2f} over 14 days vs floor {maintenance_floor} "
                             f"(formal protocol paused until {starts})"))
        return out
    if dose < protocol_floor:
        out.append(_fire("T7", 1, [f"dose {dose:.1f}"],
                         f"heat dose {dose:.2f} over 14 days vs protocol target {protocol_floor}"))
    last = max((_d(h.get("date")) for h in heat_log or [] if _d(h.get("date"))), default=None)
    if not last or (today - last).days > 7:
        out.append(_fire("T8", 2, [f"last {last}"],
                         f"last heat session {last or 'never'} - more than 7 days ago"))
    return out


def t10_run_km(acts: list, today: date) -> dict | None:
    """Run km this week (Mon-today) more than 10% over last week, when last week > 0."""
    mon = today - timedelta(days=today.weekday())
    def km(lo, hi):
        return sum((a.get("distance") or 0) for a in acts if "Run" in str(a.get("type"))
                   and lo <= (_d(a.get("start_date_local")) or date.min) <= hi) / 1000
    this, last = km(mon, today), km(mon - timedelta(days=7), mon - timedelta(days=1))
    if last <= 0 or this <= last * 1.10:
        return None
    return _fire("T10", 2, [f"week of {mon}"],
                 f"run km +{(this / last - 1) * 100:.0f}% week-on-week ({this:.1f} km vs {last:.1f} km) "
                 f"- 10% cap applies")


def _is_strength(a: dict) -> bool:
    n = str(a.get("name") or "").lower()
    return a.get("type") == "WeightTraining" or any(w in n for w in ("strength", "gym", "s&c"))


def t11_strength(acts: list, today: date, target: int | None) -> dict | None:
    """Strength sessions below target in BOTH of the last 2 completed Mon-Sun weeks."""
    if not target:
        return None
    mon = today - timedelta(days=today.weekday())
    counts = []
    for i in (1, 2):
        lo, hi = mon - timedelta(days=7 * i), mon - timedelta(days=7 * i - 6)
        counts.append(sum(1 for a in acts if _is_strength(a)
                          and lo <= (_d(a.get("start_date_local")) or date.min) <= hi))
    if not all(c < target for c in counts):
        return None
    return _fire("T11", 2, [f"weeks to {mon - timedelta(days=1)}"],
                 f"strength {counts[1]} and {counts[0]} sessions in the last 2 weeks vs target {target}/wk")


def t9_open_actions(firing: list, render) -> dict | None:
    if not firing:
        return None
    return _fire("T9", 2, [f"{i['action']}|{i['bucket']}|{i.get('due')}" for i in firing],
                 "open actions: " + " / ".join(render(i) for i in firing))


def t12_ramp(breaches: list) -> dict | None:
    if not breaches:
        return None
    b = breaches[0]
    return _fire("T12", b["tier"], [b["week_end"] for b in breaches],
                 f"realised CTL {b['ctl_from']} -> {b['ctl_to']} over {b['week_start']}..{b['week_end']} "
                 f"= +{b['ramp']}/wk vs cap {b['cap']}/wk")


def evaluate(today: date, wellness: list, acts: list, events: list, *, ankle: dict | None = None,
             heat: dict | None = None, heat_log: list | None = None, strength_target: int | None = None,
             untrusted: set = frozenset(), open_firing: list | None = None, render=str,
             ramp_breaches: list | None = None, heat_floors: tuple = (2.0, 3.0)) -> list:
    """Every trigger that fires today, before suppression."""
    out = [t1_atl_over_ctl(wellness), t2_ankle_ramp(wellness, ankle, today),
           t3_hrv_down(wellness, today), t4_short_sleep(wellness, today),
           t5_missed(events, acts, today), t6_decoupling(acts, today, untrusted)]
    out += t7_t8_heat(heat or {}, heat_log or [], today, *heat_floors)
    out += [t9_open_actions(open_firing or [], render), t10_run_km(acts, today),
            t11_strength(acts, today, strength_target), t12_ramp(ramp_breaches or [])]
    return [f for f in out if f]


# ── suppression state ────────────────────────────────────────────────────────

def _path(slug: str) -> Path:
    return BASE / "athletes" / slug / STATE_NAME


def load_state(slug: str) -> dict:
    try:
        return json.loads(_path(slug).read_text())
    except (OSError, ValueError):
        return {}


def new_only(fired: list, state: dict, today: date) -> list:
    """Drop a trigger whose items were all logged for it in the last SUPPRESS_DAYS days."""
    lo = (today - timedelta(days=SUPPRESS_DAYS)).isoformat()
    out = []
    for f in fired:
        seen = set()
        for e in (state.get("logged") or {}).get(f["trigger"], []):
            if e.get("date", "") >= lo:
                seen.update(e.get("items") or [])
        if not set(f["items"]) <= seen:
            out.append(f)
    return out


def t9b_fingerprint(slug: str) -> str | None:
    """Changes when decision-points.md or the open-actions store changes; None without
    a decision-points.md (T9b has nothing to compare)."""
    adir = BASE / "athletes" / slug
    dp = adir / "reference" / "decision-points.md"
    if not dp.exists():
        return None
    try:
        oa = json.loads((adir / "current-state.json").read_text()).get("open_actions")
    except (OSError, ValueError):
        oa = None
    return hashlib.sha1((dp.read_text() + json.dumps(oa, sort_keys=True)).encode()).hexdigest()[:16]


def record(slug: str, logged: list, today: date, t9b: str | None = None) -> None:
    """Remember what the model just logged (and the T9b fingerprint it checked)."""
    st = load_state(slug)
    lo = (today - timedelta(days=KEEP_DAYS)).isoformat()
    book = {k: [e for e in v if e.get("date", "") >= lo] for k, v in (st.get("logged") or {}).items()}
    for f in logged:
        book.setdefault(f["trigger"], []).append({"date": today.isoformat(), "items": f["items"]})
    st["logged"] = book
    if t9b:
        st["t9b"] = t9b
    st["updated"] = datetime.now().isoformat(timespec="seconds")
    _path(slug).write_text(json.dumps(st, indent=2))
