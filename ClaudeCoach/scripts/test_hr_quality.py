#!/usr/bin/env python3
"""Offline tests for lib/hr_quality.py — per-activity heart-rate trust.
Run: python3 ClaudeCoach/scripts/test_hr_quality.py

WHAT THIS GUARDS. The checker exists to take bad wrist HR out of coaching decisions, so
it fails in two directions and both matter:

  1. It misses the artefacts it is for: cadence lock, a cold-start high reading, a
     frozen value, spikes, dropouts, no HR at all.
  2. It condemns honest data. The first live pass (27 Sep 2026) flagged an Ironman
     bike leg and a dozen Z2 rides as "early spike" because it compared the early
     p90 with the later median; a tempo run whose HR honestly sits on 2x cadence
     must not read as locked; a hard start must not read as a sensor fault.

No network, no athlete file: synthetic streams only.
"""
from __future__ import annotations

import math
import random
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))

import hr_quality as q   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(n=3600, hr=None, cad=None, speed=None, watts=None, rr=False, seed=1):
    """Build a 1 Hz stream dict. Callables take the second index."""
    rnd = random.Random(seed)
    s = {"time": list(range(n))}
    s["velocity_smooth"] = [speed(t) if speed else 3.2 for t in range(n)]
    s["cadence"] = [cad(t, rnd) if cad else 85 + rnd.choice((-1, 0, 0, 1)) for t in range(n)]
    s["heartrate"] = [hr(t, rnd, s["cadence"][t]) if hr else None for t in range(n)]
    if watts:
        s["watts"] = [watts(t) for t in range(n)]
    if rr:
        s["hrv"] = [[600] for _ in range(n)]
    return s


def honest(t, rnd, _c=None, base=140):
    """Real-looking HR: lags up over 3 min, drifts slowly, small noise."""
    rise = min(1.0, t / 180)
    return round(100 + (base - 100) * rise + t / 600 + rnd.gauss(0, 0.8))


# 1. clean easy run
r = q.assess(run(hr=honest), "Run", athlete_max_hr=190)
check("clean run is ok", r["verdict"] == "ok", r)

# 2. honest tempo run sitting on 2x cadence (170 vs 85) must NOT read as locked
r = q.assess(run(hr=lambda t, rnd, c: round(170 + rnd.gauss(0, 0.8)) if t > 200 else 120 + t // 4),
             "Run", athlete_max_hr=190)
check("honest 170 bpm at 85 rpm is not cadence lock", "cadence_lock" not in r["reasons"], r)
check("  ...and it sits on cadence a lot (so the test means something)",
      (r["metrics"].get("on_cadence_share") or 0) > 0.3, r["metrics"])


# 3. real cadence lock: HR = 2x cadence and follows its wiggles
def wobbly_cad(t, rnd):
    return 85 + round(2 * math.sin(t / 7)) + rnd.choice((-1, 0, 1))


r = q.assess(run(cad=wobbly_cad, hr=lambda t, rnd, c: 2 * c + rnd.choice((-1, 0, 0, 1))),
             "Run", athlete_max_hr=190)
check("HR tracking 2x cadence is cadence_lock, bad", r["verdict"] == "bad"
      and "cadence_lock" in r["reasons"], r)

# 4. flatline: frozen for 6 minutes mid-run
r = q.assess(run(hr=lambda t, rnd, c: 147 if 1200 <= t < 1560 else honest(t, rnd)),
             "Run", athlete_max_hr=190)
check("6-min frozen HR is flatline, bad", r["verdict"] == "bad" and "flatline" in r["reasons"], r)

# 5. spikes: six 40 bpm blips
blips = {600, 900, 1300, 1800, 2400, 3000}
r = q.assess(run(hr=lambda t, rnd, c: honest(t, rnd) + (40 if t in blips else 0)),
             "Run", athlete_max_hr=190)
check("six 40 bpm blips are spikes, bad", r["verdict"] == "bad" and "spikes" in r["reasons"], r)

# 6. dropouts: 30% of samples without HR
r = q.assess(run(hr=lambda t, rnd, c: None if t % 10 < 3 else honest(t, rnd)), "Run", athlete_max_hr=190)
check("30% missing HR is dropouts, bad", r["verdict"] == "bad" and "dropouts" in r["reasons"], r)

# 7. missing: no HR at all
r = q.assess(run(), "Run")
check("no HR stream is none", r["verdict"] == "none" and r["reasons"] == ["missing"], r)

# 8. cold-wrist early spike: 165 for 8 min at easy pace, then honest 135
r = q.assess(run(hr=lambda t, rnd, c: round(165 + rnd.gauss(0, 1)) if t < 480 else honest(t, rnd, base=135)),
             "Run", athlete_max_hr=190)
check("165 then 135 at the same pace is early_spike, bad",
      r["verdict"] == "bad" and "early_spike" in r["reasons"], r)

# 9. a hard start is not a sensor fault: fast first 10 min, HR honestly higher
r = q.assess(run(speed=lambda t: 4.2 if t < 600 else 3.0,
                 hr=lambda t, rnd, c: round(160 + rnd.gauss(0, 1)) if 90 < t < 600 else honest(t, rnd, base=135)),
             "Run", athlete_max_hr=190)
check("fast start with high HR is not early_spike", "early_spike" not in r["reasons"], r)

# 10. a race skips the early check (bike leg out of the swim starts high for real)
bike = run(hr=lambda t, rnd, c: round(165 + rnd.gauss(0, 1)) if t < 480 else honest(t, rnd, base=135),
           watts=lambda t: 200)
r = q.assess(bike, "Ride", athlete_max_hr=190, is_race=True)
check("race bike leg skips early_spike", "early_spike" not in r["reasons"], r)
r = q.assess(bike, "Ride", athlete_max_hr=190, is_race=False)
check("  ...the same stream outside a race is flagged", "early_spike" in r["reasons"], r)

# 11. implausible max
r = q.assess(run(hr=lambda t, rnd, c: 225 if t == 2000 else honest(t, rnd)), "Run", athlete_max_hr=190)
check("225 bpm on a 190 max is implausible_max", "implausible_max" in r["reasons"], r)

# 12. swims are informational, never trusted for decisions
r = q.assess(run(hr=honest), "Swim")
check("swim HR is informational and not trusted", r["informational"] and not q.trusted(r), r)

# 13. RR intervals present = rr_present, and a strap still gets checked
r = q.assess(run(hr=lambda t, rnd, c: round(165 + rnd.gauss(0, 1)) if t < 480 else honest(t, rnd, base=135), rr=True),
             "Run", athlete_max_hr=190)
check("strap with a dry-contact start is rr_present AND flagged",
      r["rr_present"] and "early_spike" in r["reasons"], r)

# 14. log round trip and untrusted_ids
with tempfile.TemporaryDirectory() as d:
    q.BASE = Path(d)
    log = q.record("x", {"id": "i1", "type": "Run", "start_date_local": "2026-09-01T07:00"},
                   {"verdict": "bad", "reasons": ["spikes"]}, save=False)
    q.record("x", {"id": "i2", "type": "Run"}, {"verdict": "ok"}, log=log, save=False)
    q.record("x", {"id": "i3", "type": "Ride"}, {"verdict": "none", "reasons": ["missing"]}, log=log)
    check("untrusted_ids = bad + none only", q.untrusted_ids("x") == {"i1", "i3"}, q.untrusted_ids("x"))
    check("unlogged activity is not untrusted", not q.is_untrusted("x", "i999"))

# 15. prompt block: silent for strap and for athletes never asked
check("no prompt block for strap", q.prompt_block({"hr_source": "strap"}) == "")
check("no prompt block for legacy athlete", q.prompt_block({}) == "")
check("wrist gets a never-prescribe-by-HR rule", "never by HR" in q.prompt_block({"hr_source": "wrist"}))

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
