"""Workout profiles for Peak: a session as zone-coloured blocks, planned or done (Jamie,
13 Oct 2026, from Intervals.icu's activity card: "Can our planned and completed workout
have something like this ... I want it to look modern!").

A profile is a compact list of blocks, [[seconds, percent, zone], ...]:
  seconds  how long the block lasts (its width)
  percent  intensity as % of threshold - FTP for power, threshold pace for run / swim
           pace, LTHR for heart rate (its height; 100 = threshold)
  zone     1-7 in the athlete's own Intervals.icu zones for that sport (its colour)

planned(doc, ss)   from an event's workout_doc steps (repeats expanded)
completed(...)     from an activity's streams: power for a ride with power, else speed for
                   a run or swim, else heart rate; binned to ~1 minute, at most MAX_BLOCKS
attach(...)        fills `prof` on recent[] rows (cached by activity id, like route shapes)

Pure apart from attach(); never raises out of attach().
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

MAX_BLOCKS = 90
MIN_BIN_S = 60
MAX_NEW_FETCHES = 60         # per refresh, so a first run can't stall the data job
FETCH_PAUSE_S = 0.3
PROFILE_DAYS = 60            # completed sessions this recent get a profile


def _zone(pct: float, bounds: list | None) -> int:
    """1-based zone for a % of threshold, from Intervals.icu % boundaries (power_zones /
    pace_zones are % of FTP / threshold pace, upper bounds)."""
    if not bounds:
        bounds = [55, 75, 90, 105, 120, 150, 999]
    for i, b in enumerate(bounds):
        if pct <= float(b):
            return i + 1
    return len(bounds)


def _hr_zone(bpm: float, hr_bounds: list | None) -> int:
    if not hr_bounds:
        return 1
    for i, b in enumerate(hr_bounds):
        if bpm <= float(b):
            return i + 1
    return len(hr_bounds) + 1


def settings_for(sport_settings: list, sport: str) -> dict:
    sport = sport or ""
    for ss in sport_settings or []:
        types = ss.get("types") or []
        if any(t == sport or t in sport or sport in t for t in types):
            return ss
    return {}


def _step_block(st: dict, ss: dict):
    for key in ("power", "pace", "hr"):
        t = st.get(key)
        if not isinstance(t, dict):
            continue
        lo = t.get("start", t.get("value"))
        hi = t.get("end", lo)
        if lo is None:
            continue
        pct = (float(lo) + float(hi)) / 2
        units = str(t.get("units") or "")
        if key == "power":
            z = _zone(pct, ss.get("power_zones"))
        elif key == "pace":
            z = _zone(pct, ss.get("pace_zones"))
        else:
            if "lthr" in units and ss.get("lthr"):
                z = _hr_zone(pct * float(ss["lthr"]) / 100, ss.get("hr_zones"))
            else:
                z = _zone(pct, [81, 89, 94, 100, 103, 106, 999])
        return [int(st.get("duration") or 0), round(pct), z]
    if st.get("duration"):
        return [int(st["duration"]), 50, 1]
    return None


def planned(doc: dict | None, ss: dict) -> list:
    """Blocks for a planned workout; [] without structure."""
    out = []

    def walk(steps, reps=1):
        for _ in range(max(1, reps)):
            for st in steps or []:
                if not isinstance(st, dict):
                    continue
                if isinstance(st.get("steps"), list):
                    walk(st["steps"], int(st.get("reps") or 1))
                else:
                    b = _step_block(st, ss)
                    if b and b[0] > 0:
                        out.append(b)
    walk((doc or {}).get("steps"))
    return out


def completed(streams: dict, ss: dict, sport: str, threshold_pace_s=None) -> list:
    """Blocks for a done session from its streams ({type: data}); [] if nothing usable."""
    watts, vel, hr = streams.get("watts"), streams.get("velocity_smooth"), streams.get("heartrate")
    t = streams.get("time")
    n = max(len(watts or []), len(vel or []), len(hr or []))
    if n < 120:
        return []
    kind = None
    has_power = bool(watts and ss.get("ftp") and sum(1 for w in watts if w) > n * 0.3)
    has_pace = bool(vel and "Ride" not in sport and (threshold_pace_s or ss.get("threshold_pace")))
    # Rides by power; runs and swims by pace (running power against a placeholder run FTP
    # put Jamie's easy run in Z3 when Intervals.icu has it in Z1-Z2 by pace).
    if has_power and ("Ride" in sport or not has_pace):
        kind, src, ref = "power", watts, float(ss["ftp"])
    elif has_pace:
        thr_v = (float(ss["threshold_pace"]) if ss.get("threshold_pace")
                 else (100.0 / threshold_pace_s if "Swim" in sport else 1000.0 / threshold_pace_s))
        kind, src, ref = "pace", vel, thr_v
    elif hr and ss.get("lthr"):
        kind, src, ref = "hr", hr, float(ss["lthr"])
    if not kind:
        return []
    times = t if t and len(t) == n else list(range(n))
    # 30-second slices, then merged into blocks of steady effort (see _segment).
    segs, i = [], 0
    while i < n:
        j = i
        while j < n and times[j] - times[i] < SLICE_S:
            j += 1
        vals = [float(v) for v in src[i:j] if v is not None]
        if vals:
            dur = int(times[min(j, n - 1)] - times[i]) or SLICE_S
            vv = [float(v) for v in (vel or [])[i:j] if v is not None]
            metres = dur * sum(vv) / len(vv) if vv else None      # for lap lengths
            segs.append([dur, 100.0 * (sum(vals) / len(vals)) / ref, metres])
        i = max(j, i + 1)
    blocks = laps(segs, _segment(segs), sport)
    out = []
    for dur, pct in blocks:
        if kind == "power":
            z = _zone(pct, ss.get("power_zones"))
        elif kind == "pace":
            z = _zone(pct, ss.get("pace_zones"))
        else:
            z = _hr_zone(pct * ref / 100, ss.get("hr_zones"))
        out.append([int(dur), round(pct), z])
    return out


SLICE_S = 30
MERGE_PCT = 9.0          # neighbours closer than this (% of threshold) are one effort
MIN_SEG_S = 90           # anything shorter is folded into its closer neighbour
MAX_SEGS = 16            # at most this many blocks in a session's profile


def _segment(segs: list) -> list:
    """Bottom-up merge of [seconds, pct] slices into blocks of steady effort (Jamie, 13 Oct
    2026: "where there are obvious intervals it should display as one block, not lots of
    little blocks"). Always merges the adjacent pair that distorts the picture least
    (cost = gap^2 x ab / (a + b): a short piece next to a long one, or two near-equal
    efforts, go first), until at most MAX_SEGS blocks remain AND every neighbour differs by
    MERGE_PCT or more, and nothing is under MIN_SEG_S. The biggest changes of effort - an
    interval against its recovery - are the last to go, so they survive: Jamie's treadmill
    5-4-3-2-1 stays five efforts, an easy run is one block."""
    segs = [list(x) for x in segs if x[0] > 0]

    def cost(k):
        a, b = segs[k], segs[k + 1]
        return (a[1] - b[1]) ** 2 * a[0] * b[0] / (a[0] + b[0])

    while len(segs) > 1:
        gaps = [abs(segs[k][1] - segs[k + 1][1]) for k in range(len(segs) - 1)]
        if len(segs) <= MAX_SEGS and min(gaps) >= MERGE_PCT:
            # The blocks are distinct: only a piece too short to read is folded now, into
            # its own nearer neighbour - never by merging two real blocks.
            shorts = [i for i, x in enumerate(segs) if x[0] < MIN_SEG_S]
            if not shorts:
                break
            i = min(shorts, key=lambda i: segs[i][0])
            if i == 0:
                k = 0
            elif i == len(segs) - 1:
                k = i - 1
            else:
                k = i - 1 if gaps[i - 1] <= gaps[i] else i
        else:
            k = min(range(len(segs) - 1), key=cost)
        a, b = segs[k], segs[k + 1]
        d = a[0] + b[0]
        segs[k:k + 2] = [[d, (a[1] * a[0] + b[1] * b[0]) / d]]
    return segs


# Done sessions of every sport (Jamie, 15 Oct 2026: "when I look back at activities and
# just see one long line it doesn't show me much ... at least three ... warm-up, main
# session and warm-down ... a maximum of 5K per lap; an 8k main session split four and
# four, not five and three", then "this applies to all activities of any type"). A steady
# session merges to one block above; here it is cut back into laps, each drawn at the
# effort actually done in it, so the warm-up, drift and finish show.
MIN_LAPS = 3
# Longest lap per sport, in km (runs as Jamie set; the rest scaled to similar lap times).
MAX_LAP_KM = {"Run": 5.0, "Walk": 5.0, "Hike": 5.0, "Ride": 20.0, "Swim": 0.5, "Row": 5.0}
MAX_LAP_MIN = 25          # no distance (indoor ride, gym): laps by time instead
RUN_MAX_LAP_KM = MAX_LAP_KM["Run"]


def _max_km(sport: str) -> float | None:
    for k, v in MAX_LAP_KM.items():
        if k in (sport or ""):
            return v
    return None


def _cut(slices: list, n: int, dist) -> list:
    """slices -> n consecutive groups of equal distance (by dist(slice)), at slice edges."""
    if n <= 1 or len(slices) < n:
        return [slices]
    total = sum(dist(x) for x in slices) or 1
    groups, cur, acc, k = [], [], 0.0, 1
    for x in slices:
        cur.append(x)
        acc += dist(x)
        if k < n and acc >= total * k / n and len(slices) - slices.index(x) - 1 >= n - k:
            groups.append(cur)
            cur, k = [], k + 1
    groups.append(cur)
    return [g for g in groups if g]


def laps(slices: list, blocks: list, sport: str) -> list:
    """Re-cut a done session's merged blocks into laps: at least MIN_LAPS, none longer
    than the sport's MAX_LAP_KM (or MAX_LAP_MIN when there is no speed stream), equal
    lengths within a block. Each lap's % is the average of the 30 s slices in it (the
    real effort, not the block's). Intervals keep their blocks; long steady ones are cut."""
    blocks = [[b[0], b[1]] for b in blocks or []]   # _segment can pass slices through as-is
    if not slices or not blocks:
        return blocks
    cap = _max_km(sport)
    if cap and all(len(x) > 2 and x[2] is not None for x in slices):
        dist = lambda x: x[2] / 1000                                  # km
    else:
        cap = float(MAX_LAP_MIN)
        dist = lambda x: x[0] / 60                                    # minutes
    # Which slices make up each block (blocks are runs of consecutive slices).
    groups, i = [], 0
    for dur, _ in blocks:
        g, acc = [], 0
        while i < len(slices) and (acc < dur - 1 or not g):
            g.append(slices[i])
            acc += slices[i][0]
            i += 1
        groups.append(g)
    if i < len(slices):
        groups[-1].extend(slices[i:])
    km = [sum(dist(x) for x in g) for g in groups]
    n = [max(1, math.ceil(k / cap - 0.1)) for k in km]
    pieces = []
    if len(groups) == 1 and sum(x[0] for x in groups[0]) >= 20 * 60:
        # One steady block: warm-up, the main part (cut by distance), warm-down.
        g = groups[0]
        tot = sum(x[0] for x in g)
        wu_s = max(300, min(720, tot * 0.12))
        cd_s = max(180, min(480, tot * 0.08))
        a = b = 0
        acc = 0
        while a < len(g) and acc < wu_s:
            acc += g[a][0]; a += 1
        acc, b = 0, len(g)
        while b > a + 1 and acc < cd_s:
            b -= 1; acc += g[b][0]
        main = g[a:b]
        main_km = sum(dist(x) for x in main)
        pieces = [g[:a]] + _cut(main, max(1, math.ceil(main_km / cap - 0.1)), dist) + [g[b:]]
    else:
        while sum(n) < MIN_LAPS and any(len(g) > k for g, k in zip(groups, n)):
            j = max(range(len(groups)), key=lambda j: km[j] / n[j] if len(groups[j]) > n[j] else -1)
            n[j] += 1
        for g, k in zip(groups, n):
            pieces += _cut(g, k, dist)
    out = []
    for g in pieces:
        d = sum(x[0] for x in g)
        if d:
            out.append([d, sum(x[0] * x[1] for x in g) / d])
    return out if len(out) >= len(blocks) else blocks


def from_intervals(intervals: list, ss: dict, threshold_pace_s=None) -> list:
    """Blocks for a pool swim from Intervals.icu's intervals: each rep its own bar, each
    rest a low grey one (Jamie, 13 Oct 2026: "10x100 swim sets should be 10 bars"; 30 s
    slices could not separate reps with 25 s rests). Speed vs CSS / threshold pace; the
    reps run ~2-4 s long into the rest (velocity-smoothed boundaries), which a picture can
    carry but a rep time must not - those come from laps, not from here."""
    thr_v = (100.0 / threshold_pace_s) if threshold_pace_s else float(ss.get("threshold_pace") or 0)
    out = []
    for iv in intervals or []:
        dur = int(iv.get("moving_time") or iv.get("elapsed_time") or
                  ((iv.get("end_time") or 0) - (iv.get("start_time") or 0)))
        if dur <= 0:
            continue
        v = iv.get("average_speed")
        if iv.get("type") == "WORK" and v and thr_v:
            pct = 100.0 * float(v) / thr_v
            out.append([dur, round(pct), _zone(pct, ss.get("pace_zones"))])
        else:
            out.append([dur, 0, 1])
    return out if sum(1 for b in out if b[1]) >= 2 else []


def _cache_path(base: Path, slug: str) -> Path:
    # v3: swims from intervals; v2: blocks of steady effort (_segment), not 1-minute bins - a new file so every
    # activity is rebuilt once rather than mixing the two
    # v4: every sport cut into laps (laps)
    return base / "athletes" / slug / "profile-cache-v4.json"


def attach(client, base: Path, slug: str, recent: list, sport_settings: list,
           since: str, id_key: str = "_aid", profile: dict | None = None, log=None) -> int:
    """Fill `prof` on recent[] rows dated `since` or later. Reads row[id_key] but does NOT
    remove it (route_shape.attach_shapes does that afterwards). Returns new fetches."""
    p = _cache_path(base, slug)
    try:
        cache = json.loads(p.read_text())
    except Exception:
        cache = {}
    css_s = None
    try:
        import re
        m = re.match(r"(\d+):(\d{2})", str((profile or {}).get("swim_css_per_100m") or ""))
        css_s = int(m.group(1)) * 60 + int(m.group(2)) if m else None
    except Exception:
        pass
    fetched = 0
    for row in recent:
        aid = row.get(id_key)
        if not aid or str(row.get("date") or "") < since:
            continue
        if aid in cache:
            if cache[aid]:
                row["prof"] = cache[aid]
            continue
        if fetched >= MAX_NEW_FETCHES:
            continue
        try:
            if fetched:
                time.sleep(FETCH_PAUSE_S)
            raw = client.get_activity_streams(aid) or []
            st = {s.get("type"): s.get("data") for s in raw
                  if isinstance(s, dict) and isinstance(s.get("data"), list)}
            sport = str(row.get("sport") or "")
            icu_sport = {"Bike": "Ride", "Run": "Run", "Swim": "Swim"}.get(sport, sport)
            ss = settings_for(sport_settings, icu_sport)
            prof = []
            if "Swim" in icu_sport:                  # reps and rests from the intervals
                try:
                    ivs = (client.get_extended_metrics(aid) or {}).get("icu_intervals") or []
                    prof = from_intervals(ivs, ss, css_s)
                except Exception:
                    prof = []
            prof = prof or completed(st, ss, icu_sport, css_s if "Swim" in icu_sport else None)
            cache[aid] = prof or None
            fetched += 1
            if prof:
                row["prof"] = prof
        except Exception as e:
            if log:
                log(f"[{slug}] workout profile {aid} failed (non-fatal): {e}")
    try:
        p.write_text(json.dumps(cache))
    except OSError:
        pass
    return fetched


# ── the plan, laid over what was done (Jamie, 13 Oct 2026) ───────────────────────
# "For completed activities let's say the warm up is 5 mins longer it's annoying when all
# the target lines are out of sync. So we should move where the intervals are in the plan
# and add a red dotted line where the duration is extended or cut."
WORK_PCT = 85            # a block at or above this % of threshold is an effort
CHANGED_S = 60           # a part counts as extended / cut beyond this...
CHANGED_FRAC = 0.15      # ...and this share of its planned length


def _groups(blocks: list) -> list:
    """[(is_work, [blocks])] runs of effort / not-effort, in order."""
    out = []
    for b in blocks:
        w = (b[1] or 0) >= WORK_PCT
        if out and out[-1][0] == w:
            out[-1][1].append(b)
        else:
            out.append((w, [b]))
    return out


def fit_plan(plan: list, done: list) -> list | None:
    """The planned blocks re-timed onto the done session: [[seconds, pct, flag], ...]
    summing to the done session's length. flag 0: as planned. A part that ran OVER keeps
    its planned length and is followed by a flag 2 piece for the extra time only, drawn red
    (Jamie: red when too long, yellow when too short); a part CUT short is scaled to what
    was done and its blocks carry flag -1 (drawn yellow). Each
    planned effort is pinned to the matching done effort (in order); the easier parts
    between stretch or shrink to fit. None when the efforts don't pair up (a skipped or
    added interval), so the caller falls back to the plain proportional outline."""
    if not plan or not done:
        return None
    pg, dg = _groups(plan), _groups(done)
    if [w for w, _ in pg] != [w for w, _ in dg]:
        # same efforts but the done session starts or ends with a part the plan lacks
        # (or vice versa) - pad the shorter with an empty easy part so they line up
        if [w for w, _ in pg if w] != [w for w, _ in dg if w] or \
                sum(1 for w, _ in pg if w) != sum(1 for w, _ in dg if w):
            return None
        if pg and dg and pg[0][0] != dg[0][0]:
            (pg if pg[0][0] else dg).insert(0, (False, [[0, 0, 1]]))
        if pg and dg and pg[-1][0] != dg[-1][0]:
            (pg if pg[-1][0] else dg).append((False, [[0, 0, 1]]))
        if [w for w, _ in pg] != [w for w, _ in dg]:
            return None
    out = []
    for (pw, pbs), (dw, dbs) in zip(pg, dg):
        p_s = sum(b[0] for b in pbs)
        d_s = sum(b[0] for b in dbs)
        if not d_s:
            continue
        changed = abs(d_s - p_s) > max(CHANGED_S, CHANGED_FRAC * p_s)
        if not p_s:                                  # done but not planned: all extra
            out.append([d_s, 0, 2])
            continue
        if changed and d_s > p_s:                    # over: as planned, then the extra
            out += [[b[0], b[1], 0] for b in pbs]
            out.append([d_s - p_s, pbs[-1][1], 2])
            continue
        out += [[round(b[0] * d_s / p_s), b[1], -1 if changed else 0] for b in pbs]
    return out


def _mins(sec: float) -> str:
    m = sec / 60
    return f"{m:.0f} min" if abs(m - round(m)) < 0.25 else f"{m:.1f} min"


def executed(plan: list, done: list, kind: str = "power") -> str:
    """One line for the debrief: the planned efforts against the efforts found in the
    stream (shadow week, 13 Oct 2026: with no Strava laps, Fred's 3x10 sweetspot commute
    was judged from Intervals.icu's auto-intervals, which cut each effort at junctions, and
    called "not executed" when the stream shows all three). "" when the plan has no efforts."""
    pe = [bs for w, bs in _groups(plan) if w]
    if not pe or not done:
        return ""
    de = [bs for w, bs in _groups(done) if w]

    def desc(groups):
        out = []
        for bs in groups:
            sec = sum(b[0] for b in bs) or 1
            pct = sum(b[0] * b[1] for b in bs) / sec
            out.append(f"{_mins(sec)} at {pct:.0f}%")
        return ", ".join(out) or "none"
    ok = len(de) >= len(pe) and all(
        sum(b[0] for b in d) >= 0.8 * sum(b[0] for b in p) for p, d in zip(pe, de))
    verdict = ("planned structure EXECUTED" if ok else
               f"planned structure NOT fully executed ({len(de)} of {len(pe)} efforts found)")
    return (f"planned efforts: {desc(pe)} | efforts in the {kind} stream: {desc(de)} | {verdict}"
            f" (% of threshold; efforts are {WORK_PCT}%+)")
