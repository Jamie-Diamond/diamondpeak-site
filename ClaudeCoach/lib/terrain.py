"""Did the hills cause a run's time above Z2? Worked out from the streams, never assumed
(Jamie, 10 Oct 2026).

The debrief rule let a run with Z3 time stay "EASY" whenever avg HR was under the cap
and total ascent passed 40 m, and the model then told the athlete "the Z3 time fits the
terrain". James's 15 km in Seville logged 145 m of "climbing" and was flat: the highest
point was 21 m above sea level and no single rise passed 10 m. The total was bridges,
ramps and altimeter noise summed over 15 km. Jamie ran beside him at 132 bpm to his 144.

So the excuse now needs the climbs to be real and the hard time to be on them:

  climb     a rise of MIN_CLIMB_M or more from a low point (a 5 m dip ends it), on a
            30-sample moving average so altimeter jitter cannot build one
  on-climb  from the low point to the top, plus LAG_S after it (HR keeps rising for a
            while after the gradient stops)
  explains  at least MIN_ABOVE_S above the Z2 ceiling, and SHARE of it on climbs
"""
from __future__ import annotations

MIN_CLIMB_M = 15.0
DIP_M = 5.0
SMOOTH = 30
LAG_S = 90
MIN_ABOVE_S = 60
SHARE = 0.6


def _smooth(xs: list, n: int = SMOOTH) -> list:
    out, run, q = [], 0.0, []
    for v in xs:
        q.append(v)
        run += v
        if len(q) > n:
            run -= q.pop(0)
        out.append(run / len(q))
    return out


def climbs(alt: list) -> list[tuple[int, int, float]]:
    """[(start index, top index, metres)] for every real climb."""
    pts = [(i, float(v)) for i, v in enumerate(alt or []) if v is not None]
    if len(pts) < 2:
        return []
    idx = [i for i, _ in pts]
    sm = _smooth([v for _, v in pts])
    out = []
    lo_k, hi_k, climbing = 0, 0, False
    for k in range(1, len(sm)):
        if not climbing:
            if sm[k] < sm[lo_k]:
                lo_k = k
            elif sm[k] - sm[lo_k] >= MIN_CLIMB_M:
                climbing, hi_k = True, k
        else:
            if sm[k] > sm[hi_k]:
                hi_k = k
            elif sm[hi_k] - sm[k] >= DIP_M:
                out.append((idx[lo_k], idx[hi_k], round(sm[hi_k] - sm[lo_k], 1)))
                climbing, lo_k = False, k
    if climbing:
        out.append((idx[lo_k], idx[hi_k], round(sm[hi_k] - sm[lo_k], 1)))
    return out


def check(streams: dict, z2_ceiling: float) -> dict | None:
    """{"climbs", "biggest_m", "above_s", "above_on_climbs_s", "share", "explains"}, or
    None without altitude and heart rate."""
    alt, hr = streams.get("altitude"), streams.get("heartrate")
    if not alt or not hr or not z2_ceiling:
        return None
    t = streams.get("time") or list(range(len(hr)))
    cl = climbs(alt)
    on = [False] * len(hr)
    for a, b, _m in cl:
        end_t = (t[b] if b < len(t) else b) + LAG_S
        for i in range(a, len(hr)):
            ti = t[i] if i < len(t) else i
            if i > b and ti > end_t:
                break
            on[i] = True
    above = above_on = 0.0
    for i in range(1, len(hr)):
        if hr[i] is None or hr[i] <= z2_ceiling:
            continue
        dt = (t[i] - t[i - 1]) if i < len(t) else 1
        dt = dt if 0 < dt <= 10 else 1
        above += dt
        if on[i]:
            above_on += dt
    share = (above_on / above) if above else 0.0
    return {"climbs": len(cl), "biggest_m": max((m for *_, m in cl), default=0.0),
            "above_s": int(above), "above_on_climbs_s": int(above_on),
            "share": round(share, 2),
            "explains": above >= MIN_ABOVE_S and share >= SHARE}


def note(name: str, aid: str, res: dict | None, ceiling: float) -> str:
    """One prompt line for the debrief."""
    if res is None:
        return f"- {name} ({aid}): no altitude/HR streams, so terrain may NOT be given as a reason."
    mins = round(res["above_s"] / 60)
    if res["explains"]:
        return (f"- {name} ({aid}): {res['climbs']} real climb(s), biggest {res['biggest_m']:.0f} m; "
                f"{round(res['share'] * 100)}% of the {mins} min above {ceiling:.0f} bpm came on them. "
                "Terrain DOES explain the time above Z2.")
    why = ("no real climbs (no rise of 15 m or more)" if not res["climbs"] else
           f"only {round(res['share'] * 100)}% of the {mins} min above {ceiling:.0f} bpm came on "
           f"the {res['climbs']} climb(s), biggest {res['biggest_m']:.0f} m")
    return (f"- {name} ({aid}): {why}. Terrain does NOT explain the time above Z2: never say it "
            "does, whatever total_ascent_m says.")
