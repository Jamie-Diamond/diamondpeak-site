"""route_shape.py — publishable route outlines that cannot be geolocated.

WHY THIS SHAPE OF DATA
======================
Jamie asked for a small route outline when tapping an activity in the app. The app is
served from a PUBLIC URL, so publishing a GPS track would publish the start and end of
every ride and run — which is his home address, several times a week, with timestamps.
That is not a thing to put on the open web to decorate a card.

So nothing positional is published. Each route is:

  1. sampled down to at most MAX_POINTS points,
  2. converted to LOCAL METRES relative to the route's own first point (so the shape is
     not distorted by latitude — a degree of longitude is shorter than a degree of
     latitude everywhere except the equator),
  3. normalised into a unit box by the LARGER of its own width/height, preserving aspect
     ratio,
  4. rounded to 3 decimal places.

What survives is the shape of the loop and nothing else: no coordinates, no scale, no
bearing origin. Two identical laps ridden 400 miles apart produce the same array. You
cannot recover where it was, because the information is not there — this is not
obfuscation, the absolute frame is discarded before publication.

Distance and duration are already published per activity, so scale is not secret; what
matters is that no POSITION is.

CACHING
=======
Streams are the most expensive call in the API (a 3-hour ride is ~8k floats for latlng
alone, and the endpoint returns every other channel with it). Shapes never change once
computed, so they are cached per activity id in athletes/<slug>/route-shapes.json and
each refresh fetches at most MAX_NEW_FETCHES new ones. The cache is private; the ids in
it are never published (activity_id is withheld from the public subset deliberately).
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

MAX_POINTS = 64          # enough for a recognisable outline at ~90px wide
MAX_NEW_FETCHES = 25     # per refresh, so a cold cache warms over a few runs
FETCH_PAUSE_S = 0.4      # streams is the heaviest endpoint; a backfill of 100+
                         # activities earns HTTP 429 without a pause between calls
GPS_SPORTS = {"Ride", "VirtualRide", "GravelRide", "Run", "TrailRun", "VirtualRun",
              "OpenWaterSwim", "Walk", "Hike"}


def _cache_path(base: Path, slug: str) -> Path:
    return base / f"athletes/{slug}/route-shapes.json"


def _load_cache(base: Path, slug: str) -> dict:
    f = _cache_path(base, slug)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}          # a corrupt cache must not stop a refresh


def _save_cache(base: Path, slug: str, cache: dict) -> None:
    f = _cache_path(base, slug)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cache, separators=(",", ":")))
    except Exception:
        pass               # best-effort; the shapes are recomputable


def _pair(data, data2=None) -> list:
    """Coordinate pairs from whatever shape Intervals.icu returns.

    THE LAYOUT THAT CAUGHT ME OUT: the latlng stream is TWO PARALLEL ARRAYS -
    `data` holds latitudes and `data2` holds longitudes. It is not interleaved. Pairing
    adjacent entries of `data` alone pairs latitude with latitude, which plots a perfect
    diagonal line: every route came out as a straight 45-degree stroke, which is what
    Jamie reported. The values look plausible individually, so nothing crashes and
    nothing looks obviously null - only the picture is wrong.

    Interleaved and list-of-pairs layouts are still handled, because this is one
    undocumented API shape and I would rather not rediscover it.
    """
    # Two parallel arrays: the documented-by-observation case.
    if data2 and isinstance(data2, list) and isinstance(data, list):
        n = min(len(data), len(data2))
        return [(data[i], data2[i]) for i in range(n)
                if isinstance(data[i], (int, float)) and isinstance(data2[i], (int, float))]
    if not isinstance(data, list) or not data:
        return []
    # List of [lat, lng] pairs.
    if isinstance(data[0], (list, tuple)):
        return [(p[0], p[1]) for p in data
                if isinstance(p, (list, tuple)) and len(p) >= 2
                and isinstance(p[0], (int, float)) and isinstance(p[1], (int, float))]
    # Flat interleaved.
    out = []
    for i in range(0, len(data) - 1, 2):
        la, lo = data[i], data[i + 1]
        if isinstance(la, (int, float)) and isinstance(lo, (int, float)):
            out.append((la, lo))
    return out


def normalise(data: list, data2: list | None = None) -> list | None:
    """Coordinates -> unit-box shape [[x, y], ...], or None.

    Returns None when there is nothing worth drawing (no fix, or a track that never
    moves, e.g. a turbo session that still emitted a single stationary point).
    """
    pts = _pair(data, data2)
    if len(pts) < 4:
        return None

    # A sanity gate on the parse itself. If "longitude" occupies the same numeric range
    # as "latitude", the two series are almost certainly the same quantity and the shape
    # would be a diagonal artefact - refuse rather than publish a lie.
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    if not (-90 <= min(lats) and max(lats) <= 90 and -180 <= min(lons) and max(lons) <= 180):
        return None
    if abs(min(lats) - min(lons)) < 1e-4 and abs(max(lats) - max(lons)) < 1e-4:
        return None

    # Even sampling. Douglas-Peucker would keep corners better, but this runs on every
    # activity and an outline at 90px wide does not reward the extra complexity.
    if len(pts) > MAX_POINTS:
        step = len(pts) / float(MAX_POINTS)
        pts = [pts[min(len(pts) - 1, int(i * step))] for i in range(MAX_POINTS)]

    lat0, lon0 = pts[0]
    # Local flat-earth metres. cos(lat) is what stops a route looking stretched
    # east-west; over one activity the error from ignoring curvature is negligible.
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(0.01, math.cos(math.radians(lat0)))
    xs = [(lo - lon0) * m_per_deg_lon for _, lo in pts]
    ys = [(la - lat0) * m_per_deg_lat for la, _ in pts]

    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    span = max(w, h)
    if span < 50:          # <50 m of movement: a trainer or a bad fix, not a route
        return None

    x0, y0 = min(xs), min(ys)
    # Centre the smaller axis so the outline sits in the middle of its box.
    padx = (span - w) / 2.0
    pady = (span - h) / 2.0
    out = []
    for x, y in zip(xs, ys):
        nx = (x - x0 + padx) / span
        # SVG y grows downward; flip here so north is up without the caller caring.
        ny = 1.0 - (y - y0 + pady) / span
        out.append([round(nx, 3), round(ny, 3)])
    return out


def attach_shapes(client, base: Path, slug: str, recent: list,
                  id_key: str = "_aid", log=None) -> int:
    """Fill `shape` on each recent[] row that has a cached or fetchable outline.

    `recent` rows must carry the activity id under `id_key`; it is REMOVED before
    returning, so the caller cannot accidentally publish it. Returns how many shapes
    were newly fetched.
    """
    cache = _load_cache(base, slug)
    fetched = 0

    for row in recent:
        aid = row.pop(id_key, None)
        if not aid:
            continue
        if aid in cache:
            if cache[aid]:
                row["shape"] = cache[aid]
            continue
        if row.get("sport") not in GPS_SPORTS:
            cache[aid] = None            # remember the miss; never ask again
            continue
        if fetched >= MAX_NEW_FETCHES:
            continue
        try:
            if fetched:
                time.sleep(FETCH_PAUSE_S)
            streams = client.get_activity_streams(aid)
            ll = next((s for s in (streams or []) if s.get("type") == "latlng"), None)
            shape = normalise((ll or {}).get("data") or [], (ll or {}).get("data2"))
            cache[aid] = shape
            fetched += 1
            if shape:
                row["shape"] = shape
        except Exception as e:
            # Non-fatal and NOT cached as a miss: a transient API error should be
            # retried next run, unlike a genuine absence of GPS.
            if log:
                log(f"[{slug}] route shape {aid} failed (non-fatal): {e}")

    _save_cache(base, slug, cache)
    return fetched
