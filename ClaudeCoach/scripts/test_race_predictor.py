#!/usr/bin/env python3
"""Offline tests for the shared IM race predictor (15 Aug 2026).
Run: python3 ClaudeCoach/scripts/test_race_predictor.py

WHAT THIS PINS. Three staleness/correctness changes agreed with the athlete:
  1. FTP is resolved LIVE (intervals.icu eFTP) instead of the frozen profile figure,
     with the source recorded so the bot can say which one it used. The profile value
     must still be used when thresholds cannot be resolved (no slug, no network,
     offline tests) — the predictor may never crash or block on the lookup.
  2. The middle scenario is labelled 'Race day (tapered)' and its CTL still comes from
     config; when today's CTL has already passed that config figure, the returned dict
     says so rather than quietly projecting a race slower than the athlete is now.
  3. The form-comparability guard: the anchor IF embeds last year's tapered freshness,
     so the √CTL scaling is only like-for-like while both ends are tapered similarly.
     A stated, capped IF haircut fires when the projected taper is materially flatter.

No network, no athlete files (those are VM-only) — a fixture profile and a stub
thresholds resolver only.
"""
import inspect
import math
import sys
import time
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
import race_predictor as RP

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


# Jamie's real fixture shape (IM Italy 2025 anchor), profile FTP deliberately stale
# at 305 against a live eFTP of 309 so the two paths are distinguishable.
def profile(**over):
    p = {
        "slug": "jamie",
        "ftp_watts": 305,
        "run_threshold_pace_per_km": "4:02",
        "prev_race": {"name": "IM Italy Emilia-Romagna", "swim_time": "1:09",
                      "bike_time": "4:55", "run_time": "3:52",
                      "bike_np_watts": 201, "bike_if": 0.636},
        "race_predictor": {"anchor_ctl": 88, "raceday_ctl": 97, "target_ctl": 110,
                           "bike_km": 180.0, "t1t2_min": 10},
    }
    p.update(over)
    return p


def stub_thresholds(source="eftp", ftp=309):
    def fn(slug):
        assert slug == "jamie", f"unexpected slug {slug}"
        return {"athlete": slug, "ftp_watts": ftp, "ftp_source": source}
    return fn


def boom(slug):
    """Stands in for 'thresholds unavailable' — no config, no network, offline."""
    raise RuntimeError("no network")


def fresh_cache():
    RP._FTP_CACHE.clear()


def pred(p, ctl, fn=boom):
    """Every call injects a resolver so the suite never touches intervals.icu.
    fn=boom is the offline default, which pins FTP to the profile's 305."""
    fresh_cache()
    return RP.race_predictor(p, ctl, thresholds_fn=fn)


# --- 1) signature and shape unchanged --------------------------------------------------
sig = list(inspect.signature(RP.race_predictor).parameters)
check("call sites keep the two-positional signature race_predictor(profile, ctl)",
      sig[:2] == ["profile", "current_ctl"])
check("the injection point is optional, so no call site needs editing",
      inspect.signature(RP.race_predictor).parameters["thresholds_fn"].default is None)

rp = pred(profile(), 103)
check("two-arg call still returns a prediction", rp is not None)
check("still three rows", len(rp["rows"]) == 3)
check("row keys unchanged", set(rp["rows"][0]) == {
    "label", "ctl", "if", "bike_w", "bike_min", "run_min", "swim_min", "t12_min", "total_min"})
check("anchor block unchanged", set(rp["anchor"]) == {
    "name", "ctl", "if", "bike_w", "bike_min", "run_min", "swim_min", "t12_min", "total_min"})
check("missing inputs still return None", pred({"ftp_watts": 305}, 103) is None)
check("missing CTL still returns None", pred(profile(), None) is None)

# --- 2) FTP source: live when resolvable, profile otherwise ----------------------------
live = pred(profile(), 103, stub_thresholds())
check("live eFTP used when thresholds resolve", live["ftp_source"] == "live eftp 309")
check("live FTP carried in the dict", live["ftp_watts"] == 309)

off = pred(profile(), 103)
check("thresholds failure falls back to profile", off["ftp_source"] == "profile 305")
check("profile FTP carried on fallback", off["ftp_watts"] == 305)
check("live FTP raises projected watts vs profile FTP",
      live["rows"][0]["bike_w"] > off["rows"][0]["bike_w"])

noslug = pred(profile(slug=None), 103, stub_thresholds())
check("no slug means no live lookup", noslug["ftp_source"] == "profile 305")

static = pred(profile(), 103, stub_thresholds(source="static", ftp=340))
check("non-eFTP source is NOT preferred (static FTP is raise-only, can be stale-high)",
      static["ftp_source"] == "profile 305")

athlete_key = pred({**profile(slug=None), "athlete": "jamie"}, 103, stub_thresholds())
check("'athlete' key accepted as the slug", athlete_key["ftp_source"] == "live eftp 309")

# The bot is a long-lived poll loop, so a cached FTP must expire rather than pin a
# figure for days. (Injected resolvers are never cached, hence the direct _resolve_ftp.)
fresh_cache()
RP._FTP_CACHE["jamie"] = (time.time() + 60, (999, "live eftp 999"))
check("a live cache entry is reused inside its TTL",
      RP._resolve_ftp(profile()) == (999, "live eftp 999"))
RP._FTP_CACHE["jamie"] = (time.time() - 1, (999, "live eftp 999"))
check("an expired cache entry is not reused",
      RP._resolve_ftp(profile(), stub_thresholds()) == (309, "live eftp 309"))
check("an injected resolver never writes the shared cache",
      RP._FTP_CACHE["jamie"][1] == (999, "live eftp 999"))
fresh_cache()

# --- 3) race-day row: relabelled, value still from config, staleness announced ----------
rp = pred(profile(), 103)
check("middle row relabelled 'Race day (tapered)'",
      rp["rows"][1]["label"] == "Race day (tapered)")
check("race-day CTL still read from config, not hardcoded", rp["rows"][1]["ctl"] == 97)
check("staleness note fires when current CTL passes raceday_ctl",
      any("stale" in n for n in rp["notes"]))

cfg_updated = profile()
cfg_updated["race_predictor"]["raceday_ctl"] = 106
fresh = pred(cfg_updated, 103)
check("no staleness note once config is ahead of today's CTL", fresh["notes"] == [])
check("updated config figure flows straight through", fresh["rows"][1]["ctl"] == 106)

# --- 4) form-comparability guard -------------------------------------------------------
def haircut_for(deficit):
    """anchor TSB +15, projected TSB set to open the requested deficit."""
    p = profile()
    p["race_predictor"]["anchor_race_tsb"] = 15
    p["race_predictor"]["projected_race_tsb"] = 15 - deficit
    return pred(p, 103)

for deficit, expect in ((0, 0.0), (10, 1.0), (20, 2.0), (40, 3.0)):
    r = haircut_for(deficit)
    check(f"{deficit}-point freshness deficit gives a {expect}% IF haircut",
          r["if_haircut_pct"] == expect)
    check(f"form_note {'present' if expect else 'absent'} at {deficit} points",
          (r["form_note"] is not None) == bool(expect))

r20 = haircut_for(20)
base = pred(profile(), 103)
check("haircut actually moves IF on every row",
      all(abs(a["if"] - b["if"] * 0.98) < 0.0015
          for a, b in zip(r20["rows"], base["rows"])))
check("haircut is never silent", "TSB" in (r20["form_note"] or ""))

p = profile()
p["race_predictor"]["anchor_race_tsb"] = 15      # projected missing
half = pred(p, 103)
check("one TSB value alone means no haircut and no note",
      half["if_haircut_pct"] == 0.0 and half["form_note"] is None)

p = profile()
p["race_predictor"]["anchor_race_tsb"] = 5
p["race_predictor"]["projected_race_tsb"] = 20   # FRESHER than the anchor
better = pred(p, 103)
check("a fresher projected taper never earns a bonus",
      better["if_haircut_pct"] == 0.0 and better["form_note"] is None)

# --- 5) IF cap still holds, including under the haircut --------------------------------
p = profile()
p["race_predictor"]["target_ctl"] = 400          # absurd CTL, must not project absurd IF
capped = pred(p, 103)
check("IF cap still respected at an absurd target CTL",
      capped["rows"][2]["if"] <= RP.IF_CAP + 1e-9)
check("cap binds at exactly the ceiling with no haircut",
      capped["rows"][2]["if"] == round(RP.IF_CAP, 3))

p["race_predictor"]["anchor_race_tsb"] = 15
p["race_predictor"]["projected_race_tsb"] = -25  # 40 points, capped 3%
capped_hc = pred(p, 103)
check("haircut applies below the cap, so the cap stays visible",
      capped_hc["rows"][2]["if"] == round(RP.IF_CAP * 0.97, 3))
check("a capped row is still capped after the haircut",
      capped_hc["rows"][2]["if"] < RP.IF_CAP)

# --- 6) the arithmetic itself is unchanged for the untouched path ----------------------
rp = pred(profile(), 103)
row = rp["rows"][0]
expect_if = min(RP.IF_CAP, 0.636 * math.sqrt(103 / 88))
check("IF still scales as anchor_if x sqrt(CTL/anchor_ctl)",
      abs(row["if"] - round(expect_if, 3)) < 1e-9)
check("bike watts are FTP x IF", row["bike_w"] == round(305 * expect_if))
check("total is the sum of the legs plus T1/T2 (within per-leg rounding)",
      abs(row["total_min"] - (row["bike_min"] + row["run_min"]
                              + row["swim_min"] + row["t12_min"])) <= 1)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all race-predictor tests passed")
