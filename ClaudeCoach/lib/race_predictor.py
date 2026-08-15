"""IM race predictor — single source of truth.

Moved verbatim from scripts/refresh-site-data.py (5 Jul 2026) so the same
model serves the website overview (via that cron script), the /race command
and the chat path (via plan_tools.py race-predict). Do not fork this logic.
"""
import math
import time

__all__ = ["race_predictor", "parse_hm", "parse_pace_s"]

IF_CAP = 0.75                      # long-course sustainable ceiling
TSB_DEFICIT_GATE = 10.0            # TSB points of freshness deficit before any haircut
TSB_HAIRCUT_CAP_PCT = 3.0          # max IF haircut, per cent

# slug -> (expiry_epoch, (ftp, source)). The bot is a long-lived poll loop, so this
# is TTL'd: it exists to stop repeated calls inside one refresh, not to pin a figure.
_FTP_CACHE = {}
_FTP_TTL_S = 1800


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


def _resolve_ftp(profile, thresholds_fn=None):
    """(ftp, source_string). Live eFTP when it can be resolved, else the profile value.

    Only ftp_source == 'eftp' overrides the profile: the ICU *static* FTP is raise-only
    by design (thresholds.sync_ftp_from_eftp) so it can sit stale-high, which makes it
    less trustworthy than the profile figure, not more. Any failure — no slug, no
    network, no config (offline tests) — falls back silently to the profile."""
    profile_ftp = profile.get("ftp_watts")
    slug = profile.get("slug") or profile.get("athlete")
    if slug:
        cached = _FTP_CACHE.get(slug) if thresholds_fn is None else None
        if cached and cached[0] > time.time():
            return cached[1]
        try:
            fn = thresholds_fn
            if fn is None:
                from thresholds import get_thresholds as fn
            t = fn(slug) or {}
            live = t.get("ftp_watts")
            if live and t.get("ftp_source") == "eftp":
                out = (int(live), f"live eftp {int(live)}")
                if thresholds_fn is None:
                    _FTP_CACHE[slug] = (time.time() + _FTP_TTL_S, out)
                return out
        except Exception:
            pass
    return profile_ftp, f"profile {profile_ftp}"


def race_predictor(profile, current_ctl, thresholds_fn=None):
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
    so projecting it off run fitness overstated it). Returns None if inputs missing.

    FORM COMPARABILITY — the model's core assumption. The anchor IF is a TAPERED race-day
    IF: it already embeds last year's freshness. Because the projected race is also raced
    tapered, the two IFs are like-for-like and √CTL scaling alone is valid — which is why
    there is no explicit TSB term. That holds only while both ends are tapered to a similar
    freshness. If the projected taper lands materially flatter than the anchor's, the
    comparison is no longer like-for-like, and the optional 'anchor_race_tsb' /
    'projected_race_tsb' guard below applies a stated haircut rather than letting the
    assumption fail silently. The haircut is applied to EVERY row, including 'If I did it
    now': all three rows are race projections differing only in CTL, and haircutting some
    of them would break that invariant.

    FTP is resolved live (eFTP) when possible and falls back to the profile value; the
    row scenarios still hold it fixed across scenarios. 'ftp_source', 'notes' and
    'form_note' in the returned dict say which inputs were used and where they are stale."""
    pr  = profile.get("prev_race") or {}
    cfg = profile.get("race_predictor") or {}
    ftp, ftp_source = _resolve_ftp(profile, thresholds_fn)
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
    raceday_ctl = float(cfg.get("raceday_ctl", anchor_ctl))
    scenarios = [
        ("If I did it now",  float(current_ctl)),
        ("Race day (tapered)", raceday_ctl),
        ("Target",           float(cfg.get("target_ctl", anchor_ctl))),
    ]

    # Staleness self-announcement: raceday_ctl is an operator-maintained config figure,
    # so a fitness level already past it means the config, not the athlete, is behind.
    notes = []
    if float(current_ctl) > raceday_ctl:
        notes.append(f"raceday_ctl {round(raceday_ctl)} is below today's fitness "
                     f"(CTL {round(float(current_ctl))}): stale, update it")

    # Form-comparability guard (see docstring). Deterministic: 1% IF per 10 TSB points
    # of freshness deficit, gated at 10 points, capped at 3%. Both values or nothing.
    a_tsb, p_tsb = cfg.get("anchor_race_tsb"), cfg.get("projected_race_tsb")
    haircut_pct, form_note = 0.0, None
    if a_tsb is not None and p_tsb is not None:
        deficit = float(a_tsb) - float(p_tsb)
        if deficit >= TSB_DEFICIT_GATE:
            haircut_pct = min(TSB_HAIRCUT_CAP_PCT, deficit / 10.0)
            form_note = (f"projected race TSB {float(p_tsb):+.0f} vs anchor {float(a_tsb):+.0f}: "
                         f"{deficit:.0f} points less fresh, so IF cut {haircut_pct:.1f}% "
                         f"(1% per 10 TSB points, capped {TSB_HAIRCUT_CAP_PCT:.0f}%)")

    rows = []
    for label, ctl in scenarios:
        IF   = min(IF_CAP, anchor_if * math.sqrt(ctl / anchor_ctl)) * (1 - haircut_pct / 100.0)
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
    return {"rows": rows, "ftp_watts": ftp, "ftp_source": ftp_source,
            "notes": notes, "form_note": form_note,
            "if_haircut_pct": round(haircut_pct, 2), "anchor": {
        "name": pr.get("name", "Last year"), "ctl": round(anchor_ctl),
        "if": anchor_if, "bike_w": anchor_np, "bike_min": round(bike_anchor_min),
        "run_min": run_anchor_min, "swim_min": round(swim_min), "t12_min": t12,
        "total_min": round(swim_min + bike_anchor_min + (run_anchor_min or 0) + t12)}}
