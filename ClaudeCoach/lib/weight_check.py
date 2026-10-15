"""Weigh-ins that can't be the athlete, set aside until they say otherwise (Jamie, 15 Oct
2026: Fred had 79.5 kg on 25-26 Sep between 87.6 and 86.5 - someone else on his scale or
a wrong entry - "yes, but allow them if the athlete approves. If an athlete only weighed
in every 3 months, 10% is feasible").

A weigh-in is SUSPECT when it disagrees with the readings either side of it by more than a
body could change in the time between them:

    allowed % = BASE_PCT (day-to-day water and food) + PER_WEEK_PCT x weeks apart

so 2 kg overnight on an 85 kg athlete is fine, 10% after three months is fine, 7 kg in six
days is not. Readings within CLUSTER_DAYS of each other are judged together (two mornings
on someone else's scale agree with each other), and a group is suspect only when it
disagrees with the readings on BOTH sides; the first and last groups have one side, so
they are never flagged (which side is wrong can't be told).

Suspect groups are left out of Peak's weight graphs and averages. The athlete is asked
once ("Was that you?"); Yes puts them back, No keeps them out. State per athlete in
athletes/<slug>/weight-review.json: {"approved": [...], "rejected": [...], "asked": [...]}.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BASE_PCT = 2.5
PER_WEEK_PCT = 0.8
CLUSTER_DAYS = 3


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)


def allowed_pct(days_apart: int) -> float:
    return BASE_PCT + PER_WEEK_PCT * days_apart / 7


def _clusters(readings: list) -> list:
    rows = sorted((r for r in readings or [] if r.get("kg")), key=lambda r: r["date"])
    out = []
    for r in rows:
        if out and _days(out[-1][-1]["date"], r["date"]) <= CLUSTER_DAYS:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def suspects(readings: list) -> list:
    """[{"dates": [...], "kg": mean kg, "usual": neighbours' mean kg}] for each suspect group."""
    cl = _clusters(readings)
    out = []
    mean = lambda g: sum(float(r["kg"]) for r in g) / len(g)
    for i in range(1, len(cl) - 1):
        g, prev, nxt = cl[i], cl[i - 1], cl[i + 1]
        m = mean(g)
        off_prev = abs(m - mean(prev)) / mean(prev) * 100 > allowed_pct(_days(prev[-1]["date"], g[0]["date"]))
        off_next = abs(m - mean(nxt)) / mean(nxt) * 100 > allowed_pct(_days(g[-1]["date"], nxt[0]["date"]))
        if off_prev and off_next:
            out.append({"dates": [r["date"] for r in g], "kg": round(m, 1),
                        "usual": round((mean(prev) + mean(nxt)) / 2, 1),
                        "readings": [{"date": r["date"], "kg": r["kg"]} for r in g]})
    return out


def _path(slug: str) -> Path:
    return BASE / "athletes" / slug / "weight-review.json"


def load(slug: str) -> dict:
    try:
        st = json.loads(_path(slug).read_text())
    except Exception:
        st = {}
    for k in ("approved", "rejected", "asked"):
        st.setdefault(k, [])
    return st


def save(slug: str, st: dict) -> None:
    try:
        _path(slug).write_text(json.dumps(st, indent=1))
    except OSError:
        pass


def keep(slug: str, readings: list) -> tuple[list, list]:
    """(readings to use, suspect groups set aside). Approved groups are used."""
    st = load(slug)
    out_dates = set()
    held = []
    for s in suspects(readings):
        if any(d in st["approved"] for d in s["dates"]):
            continue
        out_dates.update(s["dates"])
        held.append(s)
    return [r for r in readings if r.get("date") not in out_dates], held


def answer(slug: str, dates: list, approve: bool) -> None:
    st = load(slug)
    key, other = ("approved", "rejected") if approve else ("rejected", "approved")
    for d in dates:
        if d not in st[key]:
            st[key].append(d)
        if d in st[other]:
            st[other].remove(d)
    save(slug, st)


def to_ask(slug: str, readings: list) -> list:
    """Suspect groups nobody has asked about yet (none while a "hold_until" time, set
    around a deploy so the Yes / No handler is live first, hasn't passed)."""
    st = load(slug)
    from datetime import datetime
    if st.get("hold_until") and datetime.now().isoformat() < st["hold_until"]:
        return []
    seen = set(st["asked"]) | set(st["approved"]) | set(st["rejected"])
    return [s for s in suspects(readings) if not any(d in seen for d in s["dates"])]


def mark_asked(slug: str, dates: list) -> None:
    st = load(slug)
    st["asked"] = sorted(set(st["asked"]) | set(dates))
    save(slug, st)


def _fmt(d: str) -> str:
    x = date.fromisoformat(d[:10])
    return f"{x.day} {x.strftime('%b')}"


def question(s: dict) -> str:
    when = (_fmt(s["dates"][0]) if len(s["dates"]) == 1
            else f"{_fmt(s['dates'][0])} to {_fmt(s['dates'][-1])}")
    return (f"Your scale logged {s['kg']:.1f} kg on {when}, about {abs(s['usual'] - s['kg']):.0f} kg "
            f"{'under' if s['kg'] < s['usual'] else 'over'} your usual {s['usual']:.1f} kg. "
            "I've left it out of your weight trend. Was that you?")


def buttons(slug: str, s: dict) -> dict:
    key = ",".join(s["dates"])
    return {"inline_keyboard": [[{"text": "Yes, that was me", "callback_data": f"wt:y:{slug}:{key}"},
                                 {"text": "No, leave it out", "callback_data": f"wt:n:{slug}:{key}"}]]}
