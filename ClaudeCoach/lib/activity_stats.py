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
    and steady enough (rides: pedalling VI <= drift.MAX_VI) - on intervals the halves hold different work,
    so the number would describe the session plan, not the athlete's durability.
Positive = HR drifted up against output. Under 5% is the usual "aerobically sound" mark.
"""
from __future__ import annotations

WARMUP_S = 10 * 60
COOLDOWN_S = 5 * 60
MIN_STEADY_S = 30 * 60
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


def build(act: dict, raw_streams: list, sport: str, history: list | None = None,
          curve: dict | None = None) -> list:
    """`history`: the athlete's activities over the last HISTORY_DAYS (for efficiency vs
    normal); `curve`: {secs: best watts} over the last 90 days (for peak-vs-best)."""
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
        # "★" = at or above the athlete's best for that duration over the last 90 days.
        peaks = [(w, lab, peak(watts, w)) for w, lab in PEAKS] if watts else []
        add("Peak power", " · ".join(
            f"{lab} {p:.0f}" + ("★" if curve and curve.get(w) and p >= curve[w] - 1 else "")
            for w, lab, p in peaks if p) + " w" if any(p for _, _, p in peaks) else None)
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
        # One drift method everywhere (lib/drift.py, Jamie 17 Oct 2026): warm-up/cool-down
        # cut, freewheeling and stops dropped, power:HR (or grade-adjusted pace on a run with
        # no power) so hills don't read as drift, steady sessions only.
        import drift as _drift
        d = _drift.session_drift(act, st)
        if d["pct"] is not None:
            add("Decoupling", f"{d['pct']:+.1f}%")
        elif d["reason"] and d["reason"].startswith("intervals"):
            add("Decoupling", "n/a: intervals or surges (power too variable)")
        ef = _num(act.get("icu_efficiency_factor"))     # NP per beat; runs' uses running power
        add("Efficiency (EF)", f"{ef:.2f}" if ride and ef else None)
        el = _num(act.get("total_elevation_gain"))
        add("Elevation", f"{el:.0f} m" if el else None)
    if ride:
        spd = _num(act.get("average_speed"))
        add("Avg speed", f"{spd * 3.6:.1f} km/h" if spd else None)
    if ride or run:
        add("Efficiency vs your normal", efficiency_vs_normal(act, history or [], ride))
        add("Split" if run else "Fade", split(st, act, ride))
    add("HR recovery", hr_recovery(hr, watts if ride else vel))
    kcal = _num(act.get("calories"))
    add("Calories", f"{kcal:.0f} kcal" if kcal else None)
    return rows


# ── insight: each compared with the athlete's own data (Jamie, 14 Oct 2026, "do all") ──
HISTORY_DAYS = 42
EASY_IF = 75            # rides: Intensity under this is an easy ride
MIN_EASY_S = 30 * 60
MIN_PEERS = 3


def _run_eff(a: dict) -> float | None:
    """Metres per minute of grade-adjusted pace per heartbeat."""
    gap, hr = _num(a.get("gap")), _num(a.get("average_heartrate"))
    return gap * 60 / hr if gap and hr else None


def _easy(a: dict, ride: bool) -> bool:
    if (a.get("moving_time") or 0) < MIN_EASY_S:
        return False
    if ride:
        return bool(_num(a.get("icu_intensity"))) and a["icu_intensity"] < EASY_IF
    z = a.get("icu_hr_zones") or []
    return bool(z) and bool(_num(a.get("average_heartrate"))) and a["average_heartrate"] <= z[1]


def efficiency_vs_normal(act: dict, history: list, ride: bool) -> str | None:
    """This easy session's efficiency (rides: NP per beat; runs: GAP per beat) against
    the athlete's easy sessions of the same sport over the last HISTORY_DAYS. Only for an
    easy session: on intervals, output per beat measures the workout, not the athlete."""
    if not _easy(act, ride):
        return None
    eff = (lambda a: _num(a.get("icu_efficiency_factor"))) if ride else _run_eff
    fam = "Ride" if ride else "Run"
    peers = [eff(a) for a in history if fam in str(a.get("type") or "")
             and str(a.get("id")) != str(act.get("id")) and _easy(a, ride) and eff(a)]
    me = eff(act)
    if not me or len(peers) < MIN_PEERS:
        return None
    base = sum(peers) / len(peers)
    return f"{(me / base - 1) * 100:+.0f}% vs your last {len(peers)} easy {'rides' if ride else 'runs'}"


def _minetti(g: float) -> float:
    """Energy cost of running at grade g (J/kg/m), Minetti 2002; 3.6 on the flat."""
    g = max(-0.3, min(0.3, g))
    return 155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + 3.6


def split(st: dict, act: dict, ride: bool) -> str | None:
    """Runs: second half against first half by grade-adjusted pace (by distance).
    Rides of 2 h+: average power in the last hour against the first."""
    if ride:
        w = st.get("watts") or []
        if len(w) < 7200:
            return None
        a = [v if _num(v) is not None else 0 for v in w]
        f, l = sum(a[:3600]) / 3600, sum(a[-3600:]) / 3600
        return f"last hour {(l / f - 1) * 100:+.0f}% power vs the first" if f else None
    d, v = st.get("distance") or [], st.get("velocity_smooth") or []
    alt = st.get("fixed_altitude") or st.get("altitude") or []
    n = min(len(d), len(v))
    if n < 1200 or not d[n - 1]:
        return None
    half = d[n - 1] / 2

    def gap_pace(lo, hi):                     # seconds per km, grade-adjusted
        cost_m, secs = 0.0, 0
        for i in range(max(lo, 10), hi):
            if not _num(v[i]) or v[i] < STOP_MS:
                continue
            dd = (d[i] - d[i - 10]) if _num(d[i]) and _num(d[i - 10]) else 0
            g = ((alt[i] - alt[i - 10]) / dd) if alt and dd > 5 and i < len(alt) else 0.0
            cost_m += v[i] * _minetti(g) / 3.6
            secs += 1
        return 1000 / (cost_m / secs) if secs else None
    mid = next(i for i in range(n) if _num(d[i]) and d[i] >= half)
    p1, p2 = gap_pace(0, mid), gap_pace(mid, n)
    if not p1 or not p2:
        return None
    diff = p2 - p1
    word = "negative split" if diff < -2 else ("even" if abs(diff) <= 2 else "faded")
    return f"{word}, 2nd half {diff:+.0f} s/km (GAP)"


def hr_recovery(hr: list, out_vals: list | None = None) -> str | None:
    """Beats dropped in the 60 s after the session's peak heart rate (5 s smoothed). Only
    when output (power or speed) fell by a third or more in that minute - a "drop" while
    still pushing is noise, not recovery."""
    h = [x if _num(x) else None for x in hr or []]
    if len(h) < 300:
        return None
    sm = []
    for i in range(len(h)):
        win = [x for x in h[max(0, i - 4):i + 1] if x]
        sm.append(sum(win) / len(win) if win else None)
    cands = [(x, i) for i, x in enumerate(sm[:-60]) if x]
    if not cands:
        return None
    top, i = max(cands)
    if out_vals:
        o = [x if _num(x) else 0 for x in out_vals]
        before = sum(o[max(0, i - 60):i]) / max(1, min(60, i))
        nxt = sum(o[i:i + 60]) / 60 if len(o) >= i + 60 else None
        if not before or nxt is None or nxt > before * 0.67:
            return None
    after = sm[i + 60]
    if not after or top - after < 3:
        return None
    return f"-{top - after:.0f} bpm in 1 min after peak {top:.0f}"


def zones(act: dict, sport: str) -> list:
    """Time-in-zone bars for the sheet: [{"label", "secs": [...]}], the sport's own
    measure first (rides power, runs GAP pace), then heart rate."""
    out = []
    if "Ride" in sport:
        zt = [z.get("secs") or 0 for z in act.get("icu_zone_times") or []
              if isinstance(z, dict) and str(z.get("id", "")).startswith("Z")]
        if sum(zt):
            out.append({"label": "Power", "secs": zt})
    elif "Run" in sport:
        zt = act.get("gap_zone_times") or act.get("pace_zone_times") or []
        if sum(zt or []):
            out.append({"label": "Pace (GAP)" if act.get("gap_zone_times") else "Pace", "secs": zt})
    hz = act.get("icu_hr_zone_times") or []
    if sum(hz):
        out.append({"label": "Heart rate", "secs": hz})
    return out
