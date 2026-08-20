"""Mean-maximal NORMALISED power curve, computed from activity power streams.

Why this module exists at all: Intervals.icu's /power-curves endpoint returns
mean-maximal AVERAGE power only. There is no NP variant - passing powerField=np or
np=true is silently ignored and returns the identical average curve, which is a trap
worth naming, because the request succeeds and the numbers look plausible. So an NP
curve has to be computed here from the streams.

NP over a window is the standard Coggan definition: 30-second rolling mean power,
raised to the fourth power, averaged, fourth root. "Mean-maximal NP at duration t" is
then the highest such value over any t-length window in any ride in the period.

COST, and how it is contained. Streams are the expensive call in this estate - one per
activity, a few hundred KB each. Fetching 90 days of rides on every 10-minute refresh
would be indefensible, so:

  * every activity's curve is computed ONCE and cached by activity id, keyed with the
    activity's own moving_time so a re-synced or trimmed ride recomputes;
  * the cache is append-only across seasons, which is what makes the year-ago column
    free after the first build;
  * a per-run ceiling (max_new) bounds the first build so the initial population is
    spread over consecutive refreshes instead of one 40-minute stall. The curve is
    published from whatever is cached, so it fills in over a few cycles rather than
    being absent until complete.

Rides with no power stream are cached as empty so they are never re-fetched.
"""

import json

# 30s rolling mean is definitional, not a tunable.
_NP_SMOOTH = 30


def _rolling_mean(vals, win):
    """Trailing rolling mean, one value per input index, ramping in over the first
    `win` samples so the opening seconds are not dropped."""
    out = [0.0] * len(vals)
    run = 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= win:
            run -= vals[i - win]
        out[i] = run / min(i + 1, win)
    return out


def np_curve_for_watts(watts, durations):
    """{duration_s: best NP} over a 1Hz watts array, for each requested duration.

    Durations longer than the ride are absent from the result rather than present and
    wrong - a 2h ride has no 4h45 NP, and reporting its whole-ride NP there would
    quietly claim an effort that never happened.
    """
    vals = [float(w or 0) for w in (watts or [])]
    if len(vals) < _NP_SMOOTH:
        return {}
    r = _rolling_mean(vals, _NP_SMOOTH)
    # Prefix sums of r^4 make every window an O(1) lookup, so the whole curve is
    # O(n + n*len(durations)) rather than O(n^2).
    pre = [0.0] * (len(r) + 1)
    for i, v in enumerate(r):
        pre[i + 1] = pre[i] + v ** 4

    out = {}
    n = len(r)
    for t in durations:
        if t > n:
            continue
        best = 0.0
        for i in range(0, n - t + 1):
            m = (pre[i + t] - pre[i]) / t
            if m > best:
                best = m
        if best > 0:
            out[t] = round(best ** 0.25, 1)
    return out


def np_for_window(watts, start_s, end_s):
    """NP for one caller-chosen [start_s, end_s) segment of a 1Hz watts array, same
    30s-rolling-mean^4 definition as np_curve_for_watts. This is the "give me the NP
    of this segment" entry point windowed/segment analysis needs instead of hand-
    averaging watts in a chat turn.

    The rolling mean is computed over the FULL stream before slicing, not reset at
    start_s - a segment boundary is not a physiological reset, so the smoothing must
    see whatever load came just before it, exactly as np_curve_for_watts already does
    for whole-ride windows.

    Returns None if the window is out of range or shorter than the 30s smoothing
    constant (NP is undefined below that), rather than a misleadingly precise number.
    """
    vals = [float(w or 0) for w in (watts or [])]
    start_s, end_s = int(start_s), int(end_s)
    if start_s < 0 or end_s <= start_s or end_s > len(vals):
        return None
    if end_s - start_s < _NP_SMOOTH:
        return None
    r = _rolling_mean(vals, _NP_SMOOTH)
    seg = r[start_s:end_s]
    mean_p4 = sum(v ** 4 for v in seg) / len(seg)
    return round(mean_p4 ** 0.25, 1)


def np_for_ride(watts):
    """Whole-ride NP via np_for_window over the entire stream - used to reconcile a
    windowed figure against Intervals.icu's own recorded icu_weighted_avg_watts for
    the activity, since the two must use the same method to be comparable."""
    vals = [float(w or 0) for w in (watts or [])]
    return np_for_window(vals, 0, len(vals))


def load_cache(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_cache(path, cache):
    try:
        path.write_text(json.dumps(cache))
    except Exception:
        pass


def refresh_cache(client, activities, cache, durations, max_new=8, log=None):
    """Fill `cache` with NP curves for any ride in `activities` not already there.

    Mutates and returns (cache, fetched_count). `activities` should already be the
    rides for the windows being published - this function does not filter by date.
    """
    fetched = 0
    for a in activities:
        aid = str(a.get("id") or "")
        if not aid:
            continue
        stamp = str(a.get("moving_time") or "")
        hit = cache.get(aid)
        if isinstance(hit, dict) and hit.get("mt") == stamp:
            continue
        if fetched >= max_new:
            break
        try:
            streams = client.get_activity_streams(aid)
            watts = None
            for s in (streams or []):
                if s.get("type") == "watts":
                    watts = s.get("data")
                    break
            curve = np_curve_for_watts(watts, durations) if watts else {}
            cache[aid] = {"mt": stamp, "date": (a.get("start_date_local") or "")[:10],
                          "np": {str(k): v for k, v in curve.items()}}
            fetched += 1
        except Exception as e:
            if log:
                log(f"NP curve: streams failed for {aid} (non-fatal): {e}")
            # Cached as unusable so a permanently broken activity is not retried
            # on every refresh forever.
            cache[aid] = {"mt": stamp, "date": (a.get("start_date_local") or "")[:10],
                          "np": {}}
            fetched += 1
    return cache, fetched


def best_over(cache, ids, durations):
    """{duration: best NP} across the given activity ids."""
    out = {}
    for aid in ids:
        e = cache.get(str(aid))
        if not isinstance(e, dict):
            continue
        for k, v in (e.get("np") or {}).items():
            try:
                t = int(k)
            except ValueError:
                continue
            if t in durations and (out.get(t) is None or v > out[t]):
                out[t] = v
    return out
