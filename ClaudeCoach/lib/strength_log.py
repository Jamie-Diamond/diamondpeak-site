"""Strength sessions logged in Peak, and their merge into the watch's activity (Jamie,
17 Oct 2026: "log weights and reps"; athletes record on their own watch, so the Peak log
goes INTO that activity, never a second one).

Peak's strength player (coach/strength-player.js) posts each finished session to
/api/strength/<slug>; it is kept in athletes/<slug>/strength-log.json:

  {"id": "<client uuid>", "date": "2026-10-19", "title": "Hamstrings",
   "session_id": "is:0:0", "event_id": 145440926, "physio": true,
   "started_at": "2026-10-19T07:05:00", "ended_at": "2026-10-19T07:52:00",   # phone local time
   "entries": [{"ex": "single_leg_hamstring_curl", "name": "...", "log": "weight_reps",
                "effort": "right", "sets": [{"side": "L", "kg": 27.5, "val": 10}, ...]}],
   "icu": {"activity_id": "i197842625", "mode": "merged" | "manual", "at": "..."}}

reconcile() runs from the activity watcher every cycle and right after a save:
  - a strength activity from the watch on that day whose time overlaps the session gets
    the logged sets written into its description (below any text already there; a
    re-merge replaces only Peak's block), and a generic watch name ("Strength") becomes
    the session's title;
  - no such activity GRACE_H hours after the session ended: Peak's log becomes a manual
    WeightTraining activity, load from duration and the effort answers (the same formula
    as plan_tools log-strength), so the work still counts.

Each athlete's sessions to do (sessions()) are their own issue protocols first (profile
injuries[].sessions, e.g. a physio's hamstring and quad sessions, set_by "physio"), then
the library's suggested sessions until the planner writes them (step 2).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
FILE = "strength-log.json"
GRACE_H = 3                      # wait this long for the watch before logging it ourselves
MATCH_PAD_MIN = 45               # how far a watch start may sit outside the Peak session
STRENGTH_TYPES = {"WeightTraining", "Workout"}
GENERIC_NAMES = re.compile(r"^(strength( training)?|weight ?training|workout|cardio|gym)$", re.I)
MARK = "Logged in Peak:"
EFFORT_RPE = {"easy": 6, "right": 7.5, "hard": 9}
EFFORT_WORD = {"easy": "easy", "right": "about right", "hard": "too hard"}
KEEP_DAYS = 400
# Who sees the player in Peak. Pilot first (Jamie, 17 Oct 2026: "1 first, with Fred and
# you as the pilot"); everyone else keeps today's session sheet until it is opened up.
PILOT = {"jamie", "fred"}


def on(slug: str) -> bool:
    return slug in PILOT


def _path(slug: str, base: Path | None = None) -> Path:
    return (base or BASE) / "athletes" / slug / FILE


def load(slug: str, base: Path | None = None) -> list:
    try:
        v = json.loads(_path(slug, base).read_text())
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _write(slug: str, rows: list, base: Path | None = None) -> None:
    p = _path(slug, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    tmp.replace(p)


def _num(v):
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 2)
    except (TypeError, ValueError):
        return None


def clean(rec: dict) -> dict:
    """Only the fields we keep, typed, so a bad client can't write junk into the log."""
    if not isinstance(rec, dict):
        raise ValueError("not a session")
    sid = str(rec.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{6,64}", sid):
        raise ValueError("bad session id")
    day = str(rec.get("date") or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("bad date")
    entries = []
    for en in rec.get("entries") or []:
        if not isinstance(en, dict) or not re.fullmatch(r"[a-z0-9_]{2,60}", str(en.get("ex") or "")):
            continue
        sets = []
        for s in en.get("sets") or []:
            if not isinstance(s, dict):
                continue
            side = s.get("side") if s.get("side") in ("L", "R") else None
            sets.append({"side": side, "kg": _num(s.get("kg")), "val": _num(s.get("val"))})
        if sets:
            entries.append({"ex": en["ex"], "name": str(en.get("name") or "")[:80],
                            "log": str(en.get("log") or "reps")[:12],
                            "effort": en.get("effort") if en.get("effort") in EFFORT_RPE else None,
                            "sets": sets[:60]})
    if not entries:
        raise ValueError("nothing logged")

    def ts(k):
        v = str(rec.get(k) or "")[:19]
        return v if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", v) else None
    out = {"id": sid, "date": day, "title": str(rec.get("title") or "Strength")[:80],
           "session_id": str(rec.get("session_id") or "")[:60] or None,
           "event_id": rec.get("event_id") if isinstance(rec.get("event_id"), (int, str)) else None,
           "physio": bool(rec.get("physio")),
           "started_at": ts("started_at"), "ended_at": ts("ended_at"), "entries": entries}
    return out


def save(slug: str, rec: dict, base: Path | None = None) -> dict:
    """Add or replace (same id) one session. Returns the stored record."""
    rec = clean(rec)
    rows = [r for r in load(slug, base) if r.get("id") != rec["id"]]
    old = next((r for r in load(slug, base) if r.get("id") == rec["id"]), None)
    if old and old.get("icu"):
        rec["icu"] = old["icu"]          # a re-sent session keeps its merge
    rows.append(rec)
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).date().isoformat()
    rows = sorted((r for r in rows if r.get("date", "") >= cutoff), key=lambda r: (r.get("date"), r.get("started_at") or ""))
    _write(slug, rows, base)
    return rec


def recent(slug: str, days: int = 120, base: Path | None = None) -> list:
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
    return [r for r in load(slug, base) if r.get("date", "") >= cutoff]


# ── the text that goes into the activity ──

def _kg(v) -> str:
    return "" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


def sets_line(sets: list, log: str = "weight_reps") -> str:
    """'L 27.5 kg × 10, 10, 10 · R 32.5 kg × 10, 10, 9'"""
    by: dict = {}
    for s in sets:
        by.setdefault(s.get("side") or "", []).append(s)
    unit = " s" if log == "seconds" else ""
    parts = []
    for side, ss in by.items():
        kgs = {s.get("kg") for s in ss}
        vals = ", ".join(f"{_kg(s.get('val')) or '?'}{unit}" for s in ss)
        if log == "weight_reps" and len(kgs) == 1 and None not in kgs:
            body = f"{_kg(ss[0]['kg'])} kg × {vals}"
        elif log == "weight_reps" and any(s.get("kg") is not None for s in ss):
            body = ", ".join(f"{_kg(s.get('kg'))} kg × {_kg(s.get('val'))}" for s in ss)
        else:
            body = vals
        parts.append((side + " " if side else "") + body)
    return " · ".join(parts)


def describe(rec: dict) -> str:
    lines = [MARK + (f" {rec['title']}" if rec.get("title") else "")]
    for en in rec.get("entries") or []:
        felt = f" ({EFFORT_WORD[en['effort']]})" if en.get("effort") in EFFORT_WORD else ""
        lines.append(f"{en.get('name') or en['ex'].replace('_', ' ')}: {sets_line(en['sets'], en.get('log'))}{felt}")
    return "\n".join(lines)


def merged_description(existing: str | None, rec: dict) -> str:
    """Keep whatever the activity already says; replace only Peak's own block."""
    text = (existing or "").split(MARK)[0].rstrip()
    return (text + "\n\n" if text else "") + describe(rec)


# ── matching the watch's activity ──

def _dt(v: str | None):
    try:
        return datetime.fromisoformat(str(v)[:19])
    except (TypeError, ValueError):
        return None


def _window(rec: dict):
    start, end = _dt(rec.get("started_at")), _dt(rec.get("ended_at"))
    if start and not end:
        end = start + timedelta(minutes=60)
    if end and not start:
        start = end - timedelta(minutes=60)
    return start, end


def match(rec: dict, acts: list, taken: set) -> dict | None:
    """The watch's strength activity for this session: same day, overlapping in time
    (MATCH_PAD_MIN either side). Without times, the day's only strength activity."""
    cands = [a for a in acts or [] if a.get("type") in STRENGTH_TYPES and str(a.get("id")) not in taken
             and str(a.get("start_date_local") or "")[:10] == rec.get("date")
             and str(a.get("source") or "").upper() != "MANUAL"]
    if not cands:
        return None
    start, end = _window(rec)
    if not start:
        return cands[0] if len(cands) == 1 else None
    pad = timedelta(minutes=MATCH_PAD_MIN)
    best, best_ov = None, None
    for a in cands:
        a0 = _dt(a.get("start_date_local"))
        if not a0:
            continue
        a1 = a0 + timedelta(seconds=int(a.get("elapsed_time") or a.get("moving_time") or 0))
        ov = (min(a1, end + pad) - max(a0, start - pad)).total_seconds()
        if ov > 0 and (best_ov is None or ov > best_ov):
            best, best_ov = a, ov
    return best


def manual_load(rec: dict) -> tuple[int, int]:
    """(minutes, load) for a manual activity: plan_tools log-strength's formula, RPE from
    the effort answers (no answers: 7)."""
    start, end = _window(rec)
    minutes = int((end - start).total_seconds() // 60) if start and end else 45
    minutes = max(10, min(minutes, 150))
    rpes = [EFFORT_RPE[e["effort"]] for e in rec.get("entries") or [] if e.get("effort") in EFFORT_RPE]
    rpe = sum(rpes) / len(rpes) if rpes else 7
    est_if = 0.55 + 0.025 * rpe
    return minutes, int(round(minutes / 60.0 * 100 * est_if * est_if))


def reconcile(slug: str, client, acts: list | None = None, now: datetime | None = None,
              base: Path | None = None) -> list:
    """Merge every unmerged session into its watch activity, or log it as a manual one
    once GRACE_H has passed. Returns what it did, one line per session."""
    rows = load(slug, base)
    pending = [r for r in rows if not r.get("icu")]
    if not pending:
        return []
    now = now or datetime.now()
    if acts is None:
        days = max(3, (now.date() - min(datetime.fromisoformat(r["date"]).date() for r in pending)).days + 1)
        acts = client.get_training_history(days=min(days, 14))
    taken = {str((r.get("icu") or {}).get("activity_id")) for r in rows if r.get("icu")}
    done = []
    for r in pending:
        a = match(r, acts, taken)
        if a:
            fields = {"description": merged_description(a.get("description"), r)}
            if GENERIC_NAMES.match(str(a.get("name") or "").strip()) and r.get("title"):
                fields["name"] = r["title"]
            client.edit_activity(a["id"], **fields)
            r["icu"] = {"activity_id": str(a["id"]), "mode": "merged", "at": now.isoformat(timespec="seconds")}
            taken.add(str(a["id"]))
            done.append(f"merged {r['id']} into {a['id']}")
            continue
        _, end = _window(r)
        end = end or datetime.fromisoformat(r["date"] + "T23:59:00")
        if now - end >= timedelta(hours=GRACE_H):
            minutes, load_ = manual_load(r)
            start = _window(r)[0] or datetime.fromisoformat(r["date"] + "T18:00:00")
            made = client.create_manual_activity(
                sport="WeightTraining", start_date_local=start.isoformat(timespec="seconds"),
                name=r.get("title") or "Strength", moving_time_s=minutes * 60, training_load=load_,
                description=describe(r))
            r["icu"] = {"activity_id": str(made.get("id")), "mode": "manual", "at": now.isoformat(timespec="seconds")}
            done.append(f"logged {r['id']} as manual activity {made.get('id')}")
    if done:
        _write(slug, rows, base)
    return done


# ── the athlete's sessions to do ──

def _active(inj: dict) -> bool:
    return str(inj.get("status") or "active").lower() not in ("cleared", "resolved")


def own_sessions(profile: dict) -> list:
    """Sessions from the athlete's own issues (a physio's plan), ready for the player."""
    out = []
    for i, inj in enumerate(profile.get("injuries") or []):
        if not isinstance(inj, dict) or not _active(inj):
            continue
        prot = inj.get("protocol")
        sess = inj.get("sessions") or (prot.get("sessions") if isinstance(prot, dict) else None) or []
        for n, s in enumerate(sess):
            if not isinstance(s, dict) or not s.get("items"):
                continue
            out.append({"id": f"is:{i}:{n}", "title": s.get("title") or "Session",
                        # set_by in live profiles ("source" there already means where we
                        # heard it); the library's schema calls it source, so read both
                        "physio": "physio" in (str(inj.get("set_by") or "").lower(),
                                               str(inj.get("source") or "").lower()),
                        "set_by": inj.get("set_by"), "issue": inj.get("description"),
                        "match": s.get("match") or [str(s.get("title") or "").lower()],
                        "parts": [{"title": s.get("title") or "Session", "order": "any", "items": s["items"]}]})
    return out


PLAN_FILE = "strength-plan.json"


def planned(slug: str, base: Path | None = None) -> dict:
    """Sessions the planner wrote for planned strength events (step 2):
    athletes/<slug>/strength-plan.json = {"<event_id>": {"date", "title", "parts": [...]}}."""
    try:
        v = json.loads(((base or BASE) / "athletes" / slug / PLAN_FILE).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def sessions(slug: str, library: dict | None = None, base: Path | None = None) -> list:
    try:
        profile = json.loads(((base or BASE) / "athletes" / slug / "profile.json").read_text())
    except (OSError, ValueError):
        profile = {}
    out = []
    for eid, s in planned(slug, base).items():
        if isinstance(s, dict) and s.get("parts"):
            out.append({"id": "ev:" + str(eid), "event_id": eid, "date": s.get("date"),
                        "title": s.get("title") or "Strength", "physio": False, "planned": True,
                        "minutes": s.get("minutes"), "parts": s["parts"]})
    out += own_sessions(profile)
    for s in (library or {}).get("example_sessions") or []:
        out.append({"id": "ss:" + str(s.get("id")), "title": s.get("title"), "physio": False,
                    "suggested": True, "for": s.get("for"), "minutes": s.get("minutes"),
                    "parts": s.get("parts") or []})
    return out


def for_event(name: str, sess: list) -> dict | None:
    """The session a planned event means, by its name ('Strength: hamstrings')."""
    low = str(name or "").lower()
    for s in sess:
        if any(m and m in low for m in s.get("match") or []):
            return s
    return None
