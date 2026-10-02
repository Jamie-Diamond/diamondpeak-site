#!/usr/bin/env python3
"""A session that is in Intervals.icu twice: ask the athlete once, then delete or merge.

Why this exists (2 Oct 2026). Fred was wrongly told to remove his Zwift link because
his rides "reach Intervals via Garmin" - they don't: Garmin Connect only passes on
rides recorded on a Garmin device. Athletes now keep every link, and Intervals.icu
usually spots a duplicate on arrival. When it misses one (Zwift + a watch recording the
same ride, a late Garmin re-sync), the session counts twice towards Fitness. The agreed
experience, from Jamie's approval:

  Same data (both have power, or both only heart rate): one message listing both
  copies, a Delete button under each, and the coach's suggestion of which to delete.
  Different data (one has power, the other heart rate): one message with a Merge
  button that copies the heart rate into the power copy and deletes the other, so
  one ride is left with both.

Merging writes a heartrate stream into the power copy (PUT /activity/{id}/streams).
Verified live 2 Oct 2026 on a throwaway ride: Intervals.icu recomputes average/max HR,
HR zones, HR load and TRIMP at once, and power load is untouched.

Copies that came only from Strava are skipped: the Intervals.icu API returns them as
empty stubs and refuses edits.

    athletes/<slug>/duplicate-state.json   {"<id>|<id>": {"asked": iso, "status": ...}}
    status: asked | deleted | merged | kept
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/
STATE_NAME = "duplicate-state.json"
KEEP_DAYS = 30            # forget answered pairs after this
MAX_GAP_S = 5             # an HR sample further than this from a power sample is a gap
MIN_COVERAGE = 0.5        # merge refused if under half the ride gets heart rate
_SPORT_WORD = {"Ride": "ride", "Run": "run", "Swim": "swim"}


def activity_ids(a: dict) -> set:
    """Every id a session can be logged under: Intervals, Strava, Garmin. i192236501
    sat in session-log.json as Strava 20406359196, so matching on the ICU id alone
    woke Sonnet every 5 min from 1 Oct 2026 14:00 to answer ACTIVITY_ID: none."""
    return {str(a.get(k) or "") for k in ("id", "strava_id", "external_id")} - {""}


def _sport(x: dict) -> str:
    t = str(x.get("type") or "")
    return next((s for s in ("Ride", "Run", "Swim") if s in t), t)


def _span(x: dict):
    start = datetime.fromisoformat(str(x.get("start_date_local") or ""))
    return start, start + timedelta(seconds=int(x.get("elapsed_time") or x.get("moving_time") or 0))


def same_session(a: dict, b: dict) -> bool:
    """True when a and b are two recordings of one session (Zwift + a watch, Garmin +
    Strava) that Intervals.icu did not merge: same sport (VirtualRide counts as Ride)
    and their times overlap by more than half of the shorter one."""
    try:
        if _sport(a) != _sport(b):
            return False
        (s1, e1), (s2, e2) = _span(a), _span(b)
        overlap = (min(e1, e2) - max(s1, s2)).total_seconds()
        return overlap > 0.5 * min((e1 - s1).total_seconds(), (e2 - s2).total_seconds())
    except (TypeError, ValueError):
        return False


def _data(a: dict) -> set:
    st = set(a.get("stream_types") or [])
    has = set()
    if "watts" in st or (not st and a.get("device_watts")):
        has.add("power")
    if "heartrate" in st or (not st and a.get("has_heartrate")):
        has.add("hr")
    return has


def pairs(acts: list) -> list:
    """Every (a, b) pair in an Intervals.icu activity list that is one session twice."""
    usable = [a for a in acts if a.get("id") and str(a.get("source") or "") != "STRAVA"]
    out = []
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            if same_session(a, b):
                out.append((a, b))
    return out


def plan(a: dict, b: dict, logged: set = frozenset()) -> dict:
    """What to offer for one pair. {"kind": "merge", "src", "dst"} when one copy has
    power without heart rate and the other heart rate without power; otherwise
    {"kind": "delete", "drop", "keep"} with the suggested copy to delete."""
    da, db = _data(a), _data(b)
    if da == {"power"} and db == {"hr"}:
        return {"kind": "merge", "src": b, "dst": a}
    if db == {"power"} and da == {"hr"}:
        return {"kind": "merge", "src": a, "dst": b}

    def score(x):   # keep the copy with more data, then more streams, then the logged one,
        return (len(_data(x)), len(x.get("stream_types") or []),   # then the first to arrive
                bool(activity_ids(x) & logged), -int(str(x["id"]).lstrip("i") or 0))
    keep, drop = (a, b) if score(a) >= score(b) else (b, a)
    return {"kind": "delete", "drop": drop, "keep": keep}


def _label(a: dict) -> str:
    src = str(a.get("source") or "")
    if src == "ZWIFT":
        return "Zwift"
    return str(a.get("device_name") or "").strip() or {"GARMIN_CONNECT": "Garmin"}.get(src, "uploaded file")


def _line(a: dict) -> str:
    start, end = _span(a)
    mins = round((end - start).total_seconds() / 60)
    has = _data(a)
    data = " + ".join(w for k, w in (("power", "power"), ("hr", "heart rate")) if k in has) or "no power or heart rate"
    return f"{_label(a)}, {start:%H:%M}, {mins} min, {data}"


def _key(a: dict, b: dict) -> str:
    return "|".join(sorted((str(a["id"]), str(b["id"]))))


def question(slug: str, a: dict, b: dict, logged: set = frozenset()) -> dict:
    """The message and buttons for one pair: {"text", "keyboard"}."""
    p = plan(a, b, logged)
    start, _ = _span(a)
    what = _SPORT_WORD.get(_sport(a), "session")
    head = (f"*Your {what} on {start:%a} {start.day} {start:%b} is in Intervals.icu twice*, "
            f"so it counts twice towards your Fitness.\n\n")
    if p["kind"] == "merge":
        src, dst = p["src"], p["dst"]
        ld, ls = _label(dst), _label(src)
        if ld == ls:
            ld, ls = "One copy", "the other"
        text = (head + f"{ld} has your power and {ls} has your heart rate. Tap *Merge* and I'll "
                f"copy the heart rate into the copy with power and delete the other, so you have "
                f"one {what} with both.")
        rows = [[{"text": "🔗 Merge", "callback_data": f"dup:merge:{slug}:{src['id']}:{dst['id']}"}]]
    else:
        drop, keep = p["drop"], p["keep"]
        first, second = sorted((a, b), key=lambda x: _span(x)[0])
        n = 1 if drop is first else 2
        text = (head + f"1. {_line(first)}\n2. {_line(second)}\n\n"
                f"I'd delete copy {n} ({_label(drop)})" +
                (", since the other has more data." if len(_data(keep)) > len(_data(drop)) else ".") +
                " Tap the one to delete.")
        rows = [[{"text": f"🗑 Delete {i}. {_label(x)}",
                  "callback_data": f"dup:del:{slug}:{x['id']}:{y['id']}"}]
                for i, (x, y) in enumerate(((first, second), (second, first)), 1)]
    rows.append([{"text": "Keep both", "callback_data": f"dup:keep:{slug}:{a['id']}:{b['id']}"}])
    return {"text": text, "keyboard": {"inline_keyboard": rows}}


# ── state ────────────────────────────────────────────────────────────────────

def _state_path(slug: str) -> Path:
    return BASE / "athletes" / slug / STATE_NAME


def _load(slug: str) -> dict:
    try:
        return json.loads(_state_path(slug).read_text())
    except (OSError, ValueError):
        return {}


def _save(slug: str, st: dict) -> None:
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat()
    st = {k: v for k, v in st.items() if (v.get("asked") or "") >= cutoff}
    _state_path(slug).write_text(json.dumps(st, indent=2))


def _mark(slug: str, a_id: str, b_id: str, status: str) -> None:
    st = _load(slug)
    k = "|".join(sorted((str(a_id), str(b_id))))
    st.setdefault(k, {"asked": datetime.now().isoformat(timespec="seconds")})["status"] = status
    _save(slug, st)


def _logged_ids(slug: str) -> set:
    try:
        return {str(e.get("activity_id", "")) for e in
                json.loads((BASE / "athletes" / slug / "session-log.json").read_text())}
    except (OSError, ValueError):
        return set()


def new_questions(slug: str, acts: list) -> list:
    """Messages for pairs not asked about yet; records them as asked. Called by the
    activity watcher every cycle with the last 3 days of Intervals.icu activities."""
    st = _load(slug)
    logged = _logged_ids(slug)
    out = []
    for a, b in pairs(acts or []):
        k = _key(a, b)
        if k in st:
            continue
        out.append(question(slug, a, b, logged))
        st[k] = {"asked": datetime.now().isoformat(timespec="seconds"), "status": "asked"}
    if out:
        _save(slug, st)
    return out


# ── acting on a tap ──────────────────────────────────────────────────────────

def _relink_log(slug: str, gone: dict | str, kept: dict | str) -> None:
    """Point session-log.json at the copy that stays. An entry for the deleted copy
    takes the kept id, unless the kept copy has its own entry, then it is dropped."""
    f = BASE / "athletes" / slug / "session-log.json"
    try:
        entries = json.loads(f.read_text())
    except (OSError, ValueError):
        return
    gone_ids = activity_ids(gone) if isinstance(gone, dict) else {str(gone)}
    kept_id = str(kept["id"] if isinstance(kept, dict) else kept)
    have_kept = any(str(e.get("activity_id", "")) == kept_id for e in entries)
    out, changed = [], False
    for e in entries:
        if str(e.get("activity_id", "")) in gone_ids:
            changed = True
            if have_kept:
                continue
            e = {**e, "activity_id": kept_id}
            have_kept = True
        out.append(e)
    if changed:
        f.write_text(json.dumps(out, indent=2))


def _series(streams: list, kind: str) -> list:
    return next((s.get("data") or [] for s in streams or [] if s.get("type") == kind), [])


def hr_onto(dst_times: list, dst_start: datetime, src_times: list, src_hr: list,
            src_start: datetime) -> list:
    """src heart rate resampled onto dst's time axis by wall-clock time. A dst second
    with no src sample within MAX_GAP_S (before the watch started, a pause) is None."""
    off = (src_start - dst_start).total_seconds()
    src = [(off + t, h) for t, h in zip(src_times, src_hr) if t is not None]
    out, j = [], 0
    for t in dst_times:
        while j + 1 < len(src) and src[j + 1][0] <= t:
            j += 1
        cands = [src[k] for k in (j, j + 1) if k < len(src)]
        best = min(cands, key=lambda p: abs(p[0] - t)) if cands else None
        out.append(best[1] if best and abs(best[0] - t) <= MAX_GAP_S else None)
    return out


def _start_utc(a: dict) -> datetime:
    return datetime.fromisoformat(str(a.get("start_date") or a.get("start_date_local")).replace("Z", "+00:00"))


def apply(slug: str, action: str, x_id: str, y_id: str, client) -> str:
    """Do what the athlete tapped and return the reply. del: delete x, keep y.
    merge: copy x's heart rate into y, then delete x. keep: leave both."""
    if action == "keep":
        _mark(slug, x_id, y_id, "kept")
        return "OK, I'll leave both copies as they are."
    try:
        x = client.get_activity_detail(x_id)
    except Exception as e:
        if "404" in str(e):
            _mark(slug, x_id, y_id, "deleted")
            return "That copy has already gone, so there's nothing to do."
        raise
    if action == "merge":
        y = client.get_activity_detail(y_id)
        ys, xs = client.get_activity_streams(y_id), client.get_activity_streams(x_id)
        if not _series(ys, "heartrate"):
            hr = hr_onto(_series(ys, "time"), _start_utc(y), _series(xs, "time"),
                         _series(xs, "heartrate"), _start_utc(x))
            if not hr or sum(h is not None for h in hr) < MIN_COVERAGE * len(hr):
                return ("The two recordings don't line up well enough to merge, so I haven't "
                        "changed anything. Tap a Delete button instead if you'd rather keep one.")
            client.put_activity_streams(y_id, [{"type": "heartrate", "data": hr}])
            if not client.get_activity_detail(y_id).get("has_heartrate"):
                return ("Intervals.icu didn't take the heart rate, so I haven't deleted anything. "
                        "Both copies are still there.")
        client.delete_activity(x_id)
        _relink_log(slug, x, y)
        _mark(slug, x_id, y_id, "merged")
        return (f"Done: you have one {_SPORT_WORD.get(_sport(y), 'session')} now, with both power "
                f"and heart rate, and the extra copy is deleted.")
    client.delete_activity(x_id)
    _relink_log(slug, x, y_id)
    _mark(slug, x_id, y_id, "deleted")
    return f"Deleted the {_label(x)} copy. It's in Intervals.icu once now, so it counts once."
