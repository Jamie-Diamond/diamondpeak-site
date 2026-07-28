"""Run durability metrics from per-second activity streams (running-power era).

Measures form fade WITHIN a single run — the failure mode that cost ~15 min of
walk-breaks in the 2025 race. Three signals:

  decoupling_pct    power:HR drift, first half vs second half — same definition
                    and 5% flag threshold as the bike T6 trigger
  cadence_fade_pct  mean cadence, final third vs first third (negative = fade)
  cost_fade_pct     running cost (watts per m/s), final third vs first third
                    (positive = each m/s costs more = form breaking down)

Pure functions, no IO. Flag thresholds are standard estimates, not personally
calibrated — tune against logged history once a few weeks accumulate.
"""
from __future__ import annotations

MIN_DURATION_S        = 1200   # don't judge runs under 20 min of clean samples
WARMUP_SKIP_S         = 300    # first 5 min excluded (HR lag)
MIN_SPEED_MS          = 0.5    # standing/walking-pause samples excluded
DECOUPLING_FLAG_PCT   = 5.0
CADENCE_FADE_FLAG_PCT = -3.0
COST_FADE_FLAG_PCT    = 5.0


def _mean(xs):
    return sum(xs) / len(xs)


def compute_run_durability(time_s, watts, heartrate, cadence, velocity) -> dict | None:
    """Durability metrics from parallel per-second streams, or None when the
    run is too short / missing power or HR. Inputs are equal-length lists;
    None entries and stopped samples are dropped."""
    if not (time_s and watts and heartrate and velocity):
        return None
    n = min(len(time_s), len(watts), len(heartrate), len(velocity),
            len(cadence) if cadence else len(time_s))
    samples = []
    for i in range(n):
        t, w, hr, v = time_s[i], watts[i], heartrate[i], velocity[i]
        cad = cadence[i] if cadence else None
        if None in (t, w, hr, v) or w <= 0 or hr <= 0 or v < MIN_SPEED_MS:
            continue
        if t < WARMUP_SKIP_S:
            continue
        samples.append((t, float(w), float(hr), float(cad) if cad else None, float(v)))
    if len(samples) < 2 or (samples[-1][0] - samples[0][0]) < MIN_DURATION_S:
        return None

    t0, t1 = samples[0][0], samples[-1][0]

    def _window(frac_lo, frac_hi):
        lo, hi = t0 + (t1 - t0) * frac_lo, t0 + (t1 - t0) * frac_hi
        return [s for s in samples if lo <= s[0] <= hi]

    # power:HR decoupling — efficiency factor first half vs second half
    h1, h2 = _window(0.0, 0.5), _window(0.5, 1.0)
    ef1 = _mean([s[1] for s in h1]) / _mean([s[2] for s in h1])
    ef2 = _mean([s[1] for s in h2]) / _mean([s[2] for s in h2])
    decoupling_pct = round((ef1 - ef2) / ef1 * 100, 1)

    # final third vs first third
    f1, f3 = _window(0.0, 1 / 3), _window(2 / 3, 1.0)
    cadence_fade_pct = None
    c1 = [s[3] for s in f1 if s[3]]
    c3 = [s[3] for s in f3 if s[3]]
    if c1 and c3:
        cadence_fade_pct = round((_mean(c3) - _mean(c1)) / _mean(c1) * 100, 1)

    cost1 = _mean([s[1] for s in f1]) / _mean([s[4] for s in f1])   # W per m/s
    cost3 = _mean([s[1] for s in f3]) / _mean([s[4] for s in f3])
    cost_fade_pct = round((cost3 - cost1) / cost1 * 100, 1)

    flags = []
    if decoupling_pct > DECOUPLING_FLAG_PCT:
        flags.append(f"decoupling {decoupling_pct}% > {DECOUPLING_FLAG_PCT}%")
    if cadence_fade_pct is not None and cadence_fade_pct < CADENCE_FADE_FLAG_PCT:
        flags.append(f"cadence fade {cadence_fade_pct}%")
    if cost_fade_pct > COST_FADE_FLAG_PCT:
        flags.append(f"running cost +{cost_fade_pct}%")

    return {
        "decoupling_pct": decoupling_pct,
        "cadence_fade_pct": cadence_fade_pct,
        "cost_fade_pct": cost_fade_pct,
        "clean_seconds": round(samples[-1][0] - samples[0][0]),
        "flags": flags,
    }


def fade_line(m: dict) -> str:
    """Athlete-facing durability read: verdict first, then the one number worth
    ranking (pace-to-heart-rate efficiency fade). The other two signals are named
    in words when they broke down and left out when they held, because neither is
    rankable by the reader. Thresholds are compared against the module constants,
    not against the wording of `flags`. It stops at what the finding means — the
    session advice belongs to the coaching analysis this line is appended to.
    """
    dec = m["decoupling_pct"]
    cad = m.get("cadence_fade_pct")
    dec_faded = dec > DECOUPLING_FLAG_PCT
    cad_faded = cad is not None and cad < CADENCE_FADE_FLAG_PCT
    cost_faded = m["cost_fade_pct"] > COST_FADE_FLAG_PCT

    # A negative decoupling means efficiency IMPROVED across the run — "faded -2.4%"
    # is nonsense to read, and the improvement is worth naming (guide Pair 6 does
    # exactly this: "the opposite of fatigue").
    if dec < 0:
        dec_phrase = (f"your pace-to-heart-rate efficiency actually improved "
                      f"{abs(dec)}% over the run, the opposite of fatigue")
    else:
        dec_phrase = f"your pace-to-heart-rate efficiency faded {dec}% over the run"

    broke = []
    if cad_faded:
        broke.append("your cadence dropped away")
    if cost_faded:
        broke.append("you were spending more energy for the same speed")

    if broke:
        # The efficiency clause is only worth a number when it moved; a signal that
        # held is named in words, so the line never prints a number that means nothing.
        if dec_faded:
            tail = f", and {dec_phrase}."
        elif dec < 0:
            tail = f", though {dec_phrase}."
        else:
            tail = ", though your pace-to-heart-rate efficiency held steady."
        return ("Your form drifted in the last third of the run — "
                + " and ".join(broke) + tail)
    if dec_faded:
        return (f"Durability was the weak point — {dec_phrase}, more than a steady "
                f"effort should, though your cadence and your energy cost per unit "
                f"of speed both held.")
    return f"Durability held up — {dec_phrase}, and nothing else drifted."
