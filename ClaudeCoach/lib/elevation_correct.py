"""Reference-field elevation correction, computed from GPS+altitude activity streams.

Why this module exists at all: the Edge 830's barometric altitude is not trustworthy
for anything gradient-dependent (CdA estimation, climb/descent splits). It drifts with
pressure changes over a ride and is noisy sample-to-sample, so point-to-point altitude
deltas swing by metres for no real elevation change. Re-deriving a correction for this
by hand in chat has been redone three times (17/19 Aug), each time turning into a
longer paragraph of prose in persistent-rules.md - same failure mode as the NP/W'bal
saga, which is why this needs to live in code, not prose.

THE METHOD
==========
The Edge 850 recording the same routes has trustworthy absolute altitude. So:

  1. build a REFERENCE FIELD from clean Edge 850 rides: bin GPS position into ~11m
     cells (CELL_M - roughly a GPS fix's own positional accuracy, so finer bins would
     just split one lane of road across cells and dilute the median for no gain) and
     keep the MEDIAN altitude sampled in each cell. Median, not mean, because a mean
     is dragged permanently off by the occasional multipath/spike sample; a median
     recovers as soon as most samples agree.

  2. for any ride (Edge 830 included), look up each point's reference altitude by
     cell instead of trusting the device's own barometric reading, wherever the route
     has been seen on a clean ride before. Off the mapped network, there is nothing to
     correct against, so the device's own reading is kept rather than inventing one.

  3. compute gradient over an ~80m ground-distance baseline (BASELINE_M), not
     point-to-point - even corrected altitude is a per-sample measurement, and
     differencing adjacent samples turns residual noise into a gradient noise floor
     many times larger than any real gradient change a rider experiences over that
     distance. 80m is short enough to still track real hills.

Only rides the CALLER has already filtered to a trustworthy source (Edge 850, no
obvious dropout/spike) should be passed into accumulate() - this module has no way to
judge activity quality, same never-guess-a-physio-constant boundary as wbal.py's CP/W'.
"""

import json
import math

CELL_M = 11.0               # reference-field grid cell size; matches GPS fix accuracy
BASELINE_M = 80.0           # ground-distance window for gradient, not a tunable knob
MAX_SAMPLES_PER_CELL = 20   # median stabilises well below this; caps field growth as
                             # the same commute route gets ridden dozens of times a year


def _cell(lat, lon):
    """Grid cell key for (lat, lon) at ~CELL_M resolution."""
    lat_step = CELL_M / 111_320.0
    lon_step = CELL_M / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
    return (round(lat / lat_step), round(lon / lon_step))


def _seg_dist_m(lat1, lon1, lat2, lon2):
    """Flat-earth distance between two nearby points - adjacent GPS samples are close
    enough that ignoring curvature is negligible, same approximation route_shape.py
    uses for route outlines."""
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(0.01, math.cos(math.radians((lat1 + lat2) / 2.0)))
    dx = (lon2 - lon1) * m_per_deg_lon
    dy = (lat2 - lat1) * m_per_deg_lat
    return math.hypot(dx, dy)


def accumulate(field, lats, lons, alts):
    """Fold one clean reference ride's GPS+altitude stream into `field` (mutated and
    returned). Only pass rides already filtered to a trustworthy altitude source."""
    for lat, lon, alt in zip(lats or [], lons or [], alts or []):
        if lat is None or lon is None or alt is None:
            continue
        bucket = field.setdefault(_cell(lat, lon), [])
        if len(bucket) < MAX_SAMPLES_PER_CELL:
            bucket.append(float(alt))
    return field


def reference_altitude(field, lat, lon):
    """Median reference altitude (m) for the cell containing (lat, lon), or None if
    no clean ride has ever passed through it."""
    if lat is None or lon is None:
        return None
    samples = field.get(_cell(lat, lon))
    if not samples:
        return None
    s = sorted(samples)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def corrected_altitudes(field, lats, lons, raw_alts):
    """Best-available altitude per point: the reference-field median where the route
    has been mapped by a clean ride, else the device's own raw reading unchanged."""
    out = []
    for lat, lon, raw in zip(lats or [], lons or [], raw_alts or []):
        ref = reference_altitude(field, lat, lon)
        out.append(ref if ref is not None else raw)
    return out


def gradient_over_baseline(lats, lons, alts, baseline_m=BASELINE_M):
    """Per-point gradient (%) using a trailing ground-distance baseline, not
    point-to-point deltas - see module docstring for why. First points (before one
    full baseline of distance has accumulated) use whatever distance is available
    rather than being absent, same ramp-in approach as np_curve's rolling mean."""
    n = len(lats or [])
    if n < 2 or len(lons or []) != n or len(alts or []) != n:
        return []
    cum = [0.0] * n
    for i in range(1, n):
        cum[i] = cum[i - 1] + _seg_dist_m(lats[i - 1], lons[i - 1], lats[i], lons[i])
    out = [0.0] * n
    j = 0
    for i in range(n):
        target = cum[i] - baseline_m
        while j + 1 < i and cum[j + 1] <= target:
            j += 1
        d = cum[i] - cum[j]
        out[i] = round(100.0 * (alts[i] - alts[j]) / d, 2) if d > 0 else 0.0
    return out


def load_field(path):
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {}
    out = {}
    for k, v in raw.items():
        r, c = k.split("_")
        out[(int(r), int(c))] = v
    return out


def save_field(path, field):
    try:
        raw = {f"{k[0]}_{k[1]}": v for k, v in field.items()}
        path.write_text(json.dumps(raw, separators=(",", ":")))
    except Exception:
        pass
