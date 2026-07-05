"""IM race predictor — single source of truth.

Moved verbatim from scripts/refresh-site-data.py (5 Jul 2026) so the same
model serves the website overview (via that cron script), the /race command
and the chat path (via plan_tools.py race-predict). Do not fork this logic.
"""
import math

__all__ = ["race_predictor", "parse_hm", "parse_pace_s"]


def parse_hm(s):
    """'4:55' -> 295 min (4h55m); '1:09' -> 69; '3:52' -> 232."""
    try:
        h, m = str(s).split(":"); return int(h) * 60 + int(m)
    except Exception:
        return None


def parse_pace_s(s):
    """'4:02' -> 242 (seconds per km)."""
    try:
        m, sec = str(s).split(":"); return int(m) * 60 + int(sec)
    except Exception:
        return None


def race_predictor(profile, current_ctl):
    """3-scenario IM race predictor.

    Science (the athlete's own framing): fitness = CTL = the capacity to absorb TSS;
    race TSS = hours x IF^2 x 100, so for a FIXED-distance event the sustainable
    intensity factor scales as IF ∝ √CTL. FTP and run threshold are held FIXED — the
    only lever between "now", "race day" and "target" is CTL (→ IF). Anchored entirely
    to the athlete's previous race (real IF, CTL, power, splits); bike speed scales as
    v ∝ NP^(1/3) (aero-dominated, same course). IF is CAPPED at 0.75 — the top of the
    long-course sustainable band — so an ambitious CTL target can never project a
    physiologically absurd intensity (audit P1-4). The run split is anchored to the
    REAL previous-race run scaled by the same IF ratio, NOT derived from the
    configured run threshold (a placeholder the athlete does not train against;
    per his own race notes the run gain comes from aid-station discipline and heat,
    so projecting it off run fitness overstated it). Returns None if inputs missing."""
    pr  = profile.get("prev_race") or {}
    cfg = profile.get("race_predictor") or {}
    ftp = profile.get("ftp_watts")
    thr = parse_pace_s(profile.get("run_threshold_pace_per_km"))
    anchor_if  = pr.get("bike_if")
    anchor_ctl = cfg.get("anchor_ctl")
    anchor_np  = pr.get("bike_np_watts")
    bike_km    = cfg.get("bike_km", 180.0)
    bike_anchor_min = parse_hm(pr.get("bike_time"))
    swim_min   = parse_hm(pr.get("swim_time"))
    run_anchor_min = parse_hm(pr.get("run_time"))
    t12 = cfg.get("t1t2_min", 10)
    if not all([ftp, thr, anchor_if, anchor_ctl, anchor_np, bike_km,
                bike_anchor_min, swim_min, current_ctl]):
        return None
    v_ref = bike_km / (bike_anchor_min / 60.0)   # km/h at anchor NP
    scenarios = [
        ("If I did it now", float(current_ctl)),
        ("Race day",        float(cfg.get("raceday_ctl", anchor_ctl))),
        ("Target",          float(cfg.get("target_ctl", anchor_ctl))),
    ]
    rows = []
    IF_CAP = 0.75                      # long-course sustainable ceiling
    for label, ctl in scenarios:
        IF   = min(IF_CAP, anchor_if * math.sqrt(ctl / anchor_ctl))
        npw  = round(ftp * IF)
        v    = v_ref * (npw / anchor_np) ** (1 / 3.0)
        bmin = bike_km / v * 60
        if run_anchor_min:
            rmin = run_anchor_min * (anchor_if / IF)
        else:
            rmin = 42.2 * (thr / IF) / 60
        rows.append({"label": label, "ctl": round(ctl), "if": round(IF, 3),
                     "bike_w": npw, "bike_min": round(bmin), "run_min": round(rmin),
                     "swim_min": round(swim_min), "t12_min": t12,
                     "total_min": round(bmin + rmin + swim_min + t12)})
    return {"rows": rows, "anchor": {
        "name": pr.get("name", "Last year"), "ctl": round(anchor_ctl),
        "if": anchor_if, "bike_w": anchor_np, "bike_min": round(bike_anchor_min),
        "run_min": run_anchor_min, "swim_min": round(swim_min), "t12_min": t12,
        "total_min": round(swim_min + bike_anchor_min + (run_anchor_min or 0) + t12)}}
