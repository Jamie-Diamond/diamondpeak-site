"""Second-by-second W' balance, computed from raw activity power streams.

Why this module exists at all: W' balance ("W'bal") is not a running total of
joules above CP - that has no recovery term and silently overstates fatigue on
any ride with easier riding between hard efforts. The metric coaches actually
mean by W'bal is Skiba's 2012 differential model: balance depletes 1:1 with
work above CP, and recharges toward W' with an exponential time constant that
shortens the further below CP the athlete is recovering (soft-pedalling at
50W refills faster than sitting at CP-10). Hand-computing either version from
a raw stream inside a chat turn reliably drifts toward the wrong (no-recovery)
one, which is why this needs to live in code, not prose.

CP is rarely known to the exact watt, so sweep_cp scans a caller-given band
rather than committing to one guessed value. W' must be supplied by the caller
from a known/measured figure - this module never assumes one, same
never-guess-a-physio-constant rule as the sweat-rate calculator.
"""

import math

# Skiba's recovery time-constant curve: tau(DCP) = 546 * e^(-0.01*DCP) + 316,
# where DCP is how far below CP the athlete currently is. Definitional, not tunable.
_TAU_SCALE = 546.0
_TAU_DECAY = 0.01
_TAU_MIN = 316.0


def wbal_curve(watts, cp, w_prime_j):
    """Second-by-second W' balance remaining (joules), one value per input second.

    Clamped to w_prime_j on the high side (balance cannot exceed full W') and
    allowed to go negative on the low side - a negative value is diagnostic (the
    stream implies an effort beyond what this CP/W' pair can sustain), not clipped
    to zero, so callers can see how far under it went.
    """
    vals = [float(w or 0) for w in (watts or [])]
    if not vals or w_prime_j <= 0 or cp <= 0:
        return []
    bal = float(w_prime_j)
    out = [0.0] * len(vals)
    for i, p in enumerate(vals):
        if p > cp:
            bal -= (p - cp)
        else:
            tau = _TAU_SCALE * math.exp(-_TAU_DECAY * (cp - p)) + _TAU_MIN
            bal += (w_prime_j - bal) * (1 - math.exp(-1.0 / tau))
        bal = min(bal, w_prime_j)
        out[i] = round(bal, 1)
    return out


def wbal_summary(watts, cp, w_prime_j):
    """{'min_j', 'min_pct', 'min_t_s'} for one CP/W' pair - the worst (most
    depleted) point in the ride, and when it happened (seconds from ride start,
    not clock time). None if the stream is empty."""
    curve = wbal_curve(watts, cp, w_prime_j)
    if not curve:
        return None
    min_j = min(curve)
    return {"min_j": round(min_j), "min_pct": round(100 * min_j / w_prime_j, 1),
            "min_t_s": curve.index(min_j)}


def sweep_cp(watts, cp_low, cp_high, w_prime_j, step=1):
    """{cp: wbal_summary} for each CP across [cp_low, cp_high] in `step` increments -
    shows how sensitive the depletion picture is to exactly where CP sits, instead
    of reporting false precision from one guessed value."""
    out = {}
    cp = float(cp_low)
    while cp <= cp_high + 1e-9:
        s = wbal_summary(watts, cp, w_prime_j)
        if s:
            key = int(cp) if cp == int(cp) else round(cp, 1)
            out[key] = s
        cp += step
    return out
