#!/usr/bin/env python3
"""Offline tests for the HR consumers gated on lib/hr_quality.py (27 Sep 2026).
Run: python3 ClaudeCoach/scripts/test_hr_consumers.py

WHAT THIS GUARDS. hr_quality records a verdict per activity; `untrusted_ids(slug)` is
the set whose HR must not drive a decision. Each consumer below must do two things:

  1. Treat an untrusted activity's HR as missing (it falls back to whatever it already
     did for an activity with no HR).
  2. Leave everything else BYTE-IDENTICAL: an activity not in the set, an empty set,
     and no log file at all must all give exactly today's answer.

No network, no real athlete file: synthetic activities, temp dirs for the log.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

_here = Path(__file__).resolve().parent
BASE = _here.parent
sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "ironman-analysis"))

import hr_quality                                          # noqa: E402
import zone_distribution as zd                             # noqa: E402
from thresholds import estimate_run_threshold_from_gap     # noqa: E402
from primitives.realised_tid import classify_activity, realised_tid   # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


BAD = "i900"       # the activity with the cadence-locked HR
GOOD = "i100"
TODAY = date(2026, 9, 27)


# ── lib/zone_distribution.py ────────────────────────────────────────────────

def _run(aid, day="2026-09-25", hr_z=(1800, 600, 0, 0, 0), moving=2400, pace=None):
    a = {"id": aid, "type": "Run", "start_date_local": f"{day}T07:00:00",
         "moving_time": moving, "icu_hr_zone_times": list(hr_z)}
    if pace is not None:
        a["pace_zone_times"] = list(pace)
    return a


bad = _run(BAD, hr_z=(0, 0, 0, 2400, 0))      # locked sensor: 40 min "in Z4"
good = _run(GOOD)

check("zone_seconds: untrusted activity gives no HR zone seconds",
      zd.zone_seconds(bad, "hr", {BAD}) == {})
check("zone_seconds: same activity, id not untrusted -> unchanged",
      zd.zone_seconds(bad, "hr", {GOOD}) == zd.zone_seconds(bad, "hr") == {4: 2400})
check("zone_seconds: untrusted=None and empty set are today's behaviour",
      zd.zone_seconds(good, "hr", None) == zd.zone_seconds(good, "hr", set())
      == {1: 1800, 2: 600})
check("zone_seconds: pace basis ignores the HR verdict",
      zd.zone_seconds(_run(BAD, pace=(0, 2000, 400)), "pace", {BAD}) == {2: 2000, 3: 400})

# build(): runs carry no pace zones, so the page falls back to HR (Kathryn's case).
BP = {"phases": [{"name": "Build", "start": "2026-09-01", "end": "2026-10-31",
                  "distribution": {"Run": "80% Z1-2 / 10% Z3 / 10% Z4-5"}}]}
acts = [good, bad]
base_out = zd.build(acts, BP, TODAY)
check("build: untrusted=None / empty / unrelated id are byte-identical",
      json.dumps(base_out, sort_keys=True)
      == json.dumps(zd.build(acts, BP, TODAY, untrusted=set()), sort_keys=True)
      == json.dumps(zd.build(acts, BP, TODAY, untrusted={"i555"}), sort_keys=True))
run_wk = base_out["sports"]["week"]["Run"]
check("build (control): locked run's Z4 counted when trusted",
      run_wk["zones"].get("Z4") == 40.0 and run_wk["basis"] == "heart rate", run_wk)
gated = zd.build(acts, BP, TODAY, untrusted={BAD})["sports"]["week"]["Run"]
check("build: untrusted run drops out of the HR fallback split",
      "Z4" not in gated["zones"] and gated["unclassified_sessions"] == 1
      and gated["minutes"] == 40.0, gated)

# build_for_slug(): reads the real log path via hr_quality.BASE (monkeypatched).
_orig_base = hr_quality.BASE
with tempfile.TemporaryDirectory() as td:
    tbase = Path(td)
    (tbase / "athletes/t/reference").mkdir(parents=True)
    (tbase / "athletes/t/reference/training-blueprint.json").write_text(json.dumps(BP))
    hr_quality.BASE = tbase
    try:
        no_log = zd.build_for_slug(tbase, "t", acts, TODAY)
        check("build_for_slug: no quality log -> identical to unfiltered build",
              json.dumps(no_log, sort_keys=True) == json.dumps(base_out, sort_keys=True))
        hr_quality.save_log("t", {BAD: {"verdict": "bad", "reasons": ["cadence_lock"]},
                                  GOOD: {"verdict": "ok", "reasons": []}})
        with_log = zd.build_for_slug(tbase, "t", acts, TODAY)["sports"]["week"]["Run"]
        check("build_for_slug: logged bad run excluded, ok run kept",
              with_log == gated, with_log)
        (tbase / "athletes/t" / hr_quality.LOG_NAME).write_text("{not json")
        check("build_for_slug: corrupt log degrades to no filtering",
              json.dumps(zd.build_for_slug(tbase, "t", acts, TODAY), sort_keys=True)
              == json.dumps(base_out, sort_keys=True))
    finally:
        hr_quality.BASE = _orig_base


# ── ironman-analysis/primitives/realised_tid.py ─────────────────────────────

locked_run = {"id": BAD, "type": "Run", "moving_time": 2700, "average_heartrate": 176}
check("classify: control - trusted locked run reads 'high'",
      classify_activity(locked_run, lthr=180) == "high")
check("classify: untrusted run is not HR-classified (None, as with no HR)",
      classify_activity(locked_run, lthr=180, untrusted={BAD}) is None
      and classify_activity(dict(locked_run, average_heartrate=None), lthr=180) is None)
check("classify: unrelated untrusted id leaves the run classified",
      classify_activity(locked_run, lthr=180, untrusted={GOOD}) == "high")
ride_if = {"id": BAD, "type": "Ride", "moving_time": 3600, "icu_intensity": 0.65,
           "average_heartrate": 175}
check("classify: untrusted ride with power IF still classified by IF",
      classify_activity(ride_if, lthr=180, untrusted={BAD}) == "low")
ride_hr = {"id": BAD, "type": "Ride", "moving_time": 3600, "average_heartrate": 175}
check("classify: untrusted ride without power falls to None",
      classify_activity(ride_hr, lthr=180, untrusted={BAD}) is None)
easy = {"id": GOOD, "type": "Run", "moving_time": 3600, "average_heartrate": 140}
rt_all = realised_tid([easy, locked_run], lthr=180)
rt_gated = realised_tid([easy, locked_run], lthr=180, untrusted={BAD})
check("realised_tid: untrusted run leaves the split and counts unclassified",
      rt_all["high_pct"] > 0 and rt_gated["low_pct"] == 100
      and rt_gated["unclassified_sessions"] == 1, (rt_all, rt_gated))
check("realised_tid: untrusted=None / empty set identical to today",
      realised_tid([easy, locked_run], lthr=180, untrusted=set()) == rt_all)


# ── lib/thresholds.py ───────────────────────────────────────────────────────

class _Client:
    def __init__(self, acts):
        self._a = acts

    def get_training_history(self, days=90):
        return self._a


def _fit_run(i, hr, gap):
    return {"id": f"i{i}", "type": "Run", "moving_time": 2400, "gap": gap,
            "average_heartrate": hr, "decoupling": 2.0, "lthr": 180}


clean = [_fit_run(i, hr, -1.0 + 0.03 * hr) for i, hr in enumerate(range(130, 170, 5))]
# A cadence-locked easy run: 176 bpm at easy pace. Trusted, it drags the fit.
outlier = _fit_run(900, 176, 2.9)
est_clean = estimate_run_threshold_from_gap(_Client(clean))
est_all = estimate_run_threshold_from_gap(_Client(clean + [outlier]))
est_gated = estimate_run_threshold_from_gap(_Client(clean + [outlier]), untrusted={BAD})
check("thresholds (control): the locked run changes the fit when trusted",
      est_all != est_clean, (est_all, est_clean))
check("thresholds: untrusted run excluded -> same as fitting the clean runs only",
      est_gated == est_clean and est_gated["n_runs"] == len(clean), est_gated)
check("thresholds: unrelated id / None identical to today",
      estimate_run_threshold_from_gap(_Client(clean + [outlier]), untrusted={GOOD})
      == est_all
      == estimate_run_threshold_from_gap(_Client(clean + [outlier]), untrusted=None))


# ── scripts/refresh-site-data.py: the log loader never breaks the page ──────

_spec = importlib.util.spec_from_file_location("refresh_site_data", _here / "refresh-site-data.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)
R.log = lambda msg: None
with tempfile.TemporaryDirectory() as td:
    hr_quality.BASE = Path(td)
    try:
        check("refresh _hr_untrusted_ids: no log -> empty set",
              R._hr_untrusted_ids("t") == set())
        hr_quality.save_log("t", {BAD: {"verdict": "bad"}, "i901": {"verdict": "none"},
                                  GOOD: {"verdict": "suspect"}})
        check("refresh _hr_untrusted_ids: bad + none only",
              R._hr_untrusted_ids("t") == {BAD, "i901"})
        _orig_fn = hr_quality.untrusted_ids
        hr_quality.untrusted_ids = lambda slug: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            check("refresh _hr_untrusted_ids: loader failure -> empty set, no raise",
                  R._hr_untrusted_ids("t") == set())
        finally:
            hr_quality.untrusted_ids = _orig_fn
    finally:
        hr_quality.BASE = _orig_base


print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("ALL PASS")
