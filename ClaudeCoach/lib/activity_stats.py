"""Key stats for a done session's sheet in Peak (Jamie, 14 Oct 2026: "add more stats to the
activity page. Peak power. Decoupling (with warm up, coasting etc corrected), other key
stats"). Served with the map by /api/track (lib/track.py), computed from the Intervals.icu
activity and its streams, so an indoor ride with no GPS gets them too.

Returns [[label, value], ...] in display order, only rows that have a value.

DECOUPLING, CORRECTED. Pa:HR (rides: power per beat; runs: speed per beat), first half
against second half of the STEADY part only:
  - the warm-up and cool-down are cut: Intervals.icu's own icu_warmup_time /
    icu_cooldown_time when it found them, else the first WARMUP_S and last COOLDOWN_S
  - coasting (power 0) and stops (speed under STOP_MS) are dropped, so freewheeling
    downhill or waiting at lights doesn't read as "HR rising against output"
  - only for a session long enough to mean something (MIN_STEADY_S left after the cuts)
    and steady enough (rides: VI <= MAX_VI) - on intervals the halves hold different work,
    so the number would describe the session plan, not the athlete's durability.
Positive = HR drifted up against output. Under 5% is the usual "aerobically sound" mark.
"""
from __future__ import annotations

WARMUP_S = 10 * 60
COOLDOWN_S = 5 * 60
MIN_STEADY_S = 30 * 60
MAX_VI = 1.10
STOP_MS = 1.0
PEAKS = ((5, "5 s"), (60, "1 min"), (300, "5 min"), (1200, "20 min"), (3600, "60 min"))


def _streams(raw) -> dict:
    out = {}
    for s in raw or []:
        if isinstance(s, dict) and isinstance(s.get("data"), list):
            out[s.get("type")] = s["data"]
    return out


def _num(v):
    return v if isinstance(v, (int, float)) else None


def peak(vals: list, w: int) -> float | None:
    """Best rolling mean over w samples (1 Hz streams); None if the stream is shorter."""
    v = [x if isinstance(x, (int, float)) else 0 for x in vals or []]
    if len(v) < w:
        return None
    run = sum(v[:w])
    best = run
    for i in range(w, len(v)):
        run += v[i] - v[i - w]
        if run > best:
            best = run
    return best / w


def decoupling(out_vals: list, hr: list, act: dict, ride: bool) -> float | None:
    n = min(len(out_vals or []), len(hr or []))
    if not n:
        return None
    wu = int(act.get("icu_warmup_time") or 0) or WARMUP_S
    cd = int(act.get("icu_cooldown_time") or 0) or COOLDOWN_S
    keep = []
    for i in range(wu, max(wu, n - cd)):
        o, h = _num(out_vals[i]), _num(hr[i])
        if o is None or not h:
            continue
        if ride and o <= 0:                       # coasting
            continue
        if not ride and o < STOP_MS:              # stopped
            continue
        keep.append((o, h))
    if len(keep) < MIN_STEADY_S:
        return None
    half = len(keep) // 2

    def ratio(seg):
        return (sum(o for o, _ in seg) / len(seg)) / (sum(h for _, h in seg) / len(seg))
    r1, r2 = ratio(keep[:half]), ratio(keep[half:])
    return (r1 - r2) / r1 * 100 if r1 else None


def _pace(ms: float, per: int = 1000) -> str:
    s = per / ms
    return f"{int(s // 60)}:{int(round(s % 60)):02d}"


def build(act: dict, raw_streams: list, sport: str) -> list:
    st = _streams(raw_streams)
    ride, run, swim = "Ride" in sport, "Run" in sport, "Swim" in sport
    rows = []

    def add(label, val):
        if val not in (None, "", "0", "0 w"):
            rows.append([label, val])

    watts, hr, vel = st.get("watts") or [], st.get("heartrate") or [], st.get("velocity_smooth") or []
    np_ = _num(act.get("icu_weighted_avg_watts"))
    avg_w = _num(act.get("icu_average_watts"))
    if ride:
        add("Normalised power", f"{np_:.0f} w" if np_ else None)
        add("Intensity (IF)", f"{act['icu_intensity'] / 100:.2f}" if _num(act.get("icu_intensity")) else None)
        add("Variability (VI)", f"{np_ / avg_w:.2f}" if np_ and avg_w else None)
        peaks = [(lab, peak(watts, w)) for w, lab in PEAKS] if watts else []
        add("Peak power", " · ".join(f"{lab} {p:.0f}" for lab, p in peaks if p) + " w"
            if any(p for _, p in peaks) else None)
        j = _num(act.get("icu_joules"))
        add("Work", f"{j / 1000:.0f} kJ" if j else None)
        if watts:
            moving = [w for w in watts if _num(w) is not None]
            coast = sum(1 for w in moving if w <= 0)
            add("Coasting", f"{coast / len(moving) * 100:.0f}%" if moving and coast else None)
    if run or swim:
        dist, mt = _num(act.get("distance")), _num(act.get("moving_time"))
        if dist and mt:
            add("Avg pace", _pace(dist / mt, 100 if swim else 1000) + ("/100m" if swim else "/km"))
        gap = _num(act.get("gap"))
        add("Grade-adjusted pace", _pace(gap) + "/km" if run and gap else None)
    add("Avg heart rate", f"{act['average_heartrate']:.0f} bpm" if _num(act.get("average_heartrate")) else None)
    add("Max heart rate", f"{act['max_heartrate']:.0f} bpm" if _num(act.get("max_heartrate")) else None)
    cad = _num(act.get("average_cadence"))
    if cad:
        add("Cadence", f"{cad * 2:.0f} spm" if run else f"{cad:.0f} rpm")
    if ride or run:
        dec = None
        if ride and watts and hr and np_ and avg_w and np_ / avg_w > MAX_VI:
            add("Decoupling", "not for interval sessions")
        elif ride and watts and hr:
            dec = decoupling(watts, hr, act, True)
        elif run and vel and hr:
            dec = decoupling(vel, hr, act, False)
        add("Decoupling", f"{dec:+.1f}%" if dec is not None else None)
        ef = _num(act.get("icu_efficiency_factor"))     # NP per beat; runs' uses running power
        add("Efficiency (EF)", f"{ef:.2f}" if ride and ef else None)
        el = _num(act.get("total_elevation_gain"))
        add("Elevation", f"{el:.0f} m" if el else None)
    if ride:
        spd = _num(act.get("average_speed"))
        add("Avg speed", f"{spd * 3.6:.1f} km/h" if spd else None)
    kcal = _num(act.get("calories"))
    add("Calories", f"{kcal:.0f} kcal" if kcal else None)
    return rows
