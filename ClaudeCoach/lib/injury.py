"""Phase 5.6 — injury-protocol modifier (hybrid: auto-ramp, physio caps it).

Layered ON the generic Phase 5.5 per-sport per-zone bands. For an athlete with an ACTIVE
injury protocol (an injuries[] entry carrying `physio_allowance`), the AFFECTED sport+zones
get EFFECTIVE floors/ceilings:

    effective_floor(sport, zone)   = min(generic_floor, physio_cap, interim)
    effective_ceiling(sport, zone) = min(generic_ceiling, physio_cap)

`physio_cap` is the physio's per-zone clearance (% of that sport's time); the coach/physio
raises it as the athlete is cleared. **cap == 0 means NOT cleared → that zone's quality is
HARD-gated** (a medical gate, not a soft preference; floors stay soft). `interim` AUTO-RAMPS
upward toward the generic floor on positive low-pain evidence and STEPS BACK on pain, never
exceeding the cap.

Coherence with modulation.R1: R1 is the ACUTE per-session pain gate (ease a session when
day-of pain >= threshold). This modifier is the CHRONIC clearance ramp (how far a zone's
floor may climb, week to week). Same threshold, different timescales.

SAFETY: the ramp advances ONLY on positive evidence (a logged run in the window with pain
< threshold). Absence of data (no runs / nothing logged) HOLDS the ramp — never advance on
silence (an athlete who stopped reporting because it hurts must not be progressed).
Brief-building is PURE (reads stored ramp_state only); pain is read + the ramp advanced +
state persisted ONLY in advance_ramp(), called on PUSH.
"""
from __future__ import annotations
from datetime import date, timedelta

# -- exposed config -----------------------------------------------------------
RAMP_PP_PER_WEEK = 2.0        # gentle interim climb per qualifying low-pain step (pp of zone %)
RAMP_MIN_DAYS = 7             # min days between up-steps (idempotent; step-back is immediate)
PAIN_WINDOW_DAYS = 14         # trailing window for recent-pain evidence
DEFAULT_EASE_THRESHOLD = 5    # pain >= this => step back (matches modulation R1)

_ZONE_IDX = {"z3": 1, "high": 2}
_PAIN_KEYS = ("ankle_pain_during", "ankle_pain_next_morning",
              "injury_pain_during", "injury_pain_next_morning")


def active_injuries(profile: dict) -> list:
    """Injury entries that carry a physio_allowance (i.e. an active protocol)."""
    return [inj for inj in (profile.get("injuries") or []) if inj.get("physio_allowance")]


def effective_bands(profile: dict, targets: dict, zone_bands: dict) -> dict:
    """PURE (stored ramp_state only; no pain read, no write). Returns
    {sport: {zone: {"floor","ceiling","cap","hard"}}} for affected sport+zones.
    zone_bands = {zone: (tol_lo, tol_hi)}; targets = {sport: [low, z3, high]}."""
    out: dict = {}
    for inj in active_injuries(profile):
        allow = inj.get("physio_allowance") or {}
        state = inj.get("ramp_state") or {}
        for sport, caps in allow.items():
            tgt = targets.get(sport)
            if not tgt:
                continue
            for zone, cap in (caps or {}).items():
                if zone not in _ZONE_IDX or zone not in zone_bands:
                    continue
                tol_lo, tol_hi = zone_bands[zone]
                target = float(tgt[_ZONE_IDX[zone]])
                cap = float(cap)
                interim = float(((state.get(sport) or {}).get(zone) or {}).get("interim", 0.0))
                out.setdefault(sport, {})[zone] = {
                    "floor": min(target - tol_lo, cap, interim),
                    "ceiling": min(target + tol_hi, cap),
                    "cap": cap,
                    "hard": cap <= 0.0,          # NOT physio-cleared → hard-gate this zone
                }
    return out


def recent_pain(session_log: list, sport: str, today: date, days: int = PAIN_WINDOW_DAYS):
    """(has_evidence, max_pain). has_evidence = >=1 `sport` session in the window carrying a
    non-null pain field. No data => (False, None) => the caller HOLDS the ramp."""
    lo = today - timedelta(days=days)
    mx = None
    evidence = False
    for e in (session_log or []):
        if not isinstance(e, dict) or (e.get("sport") or "") != sport:
            continue
        try:
            d = date.fromisoformat((e.get("date") or "")[:10])
        except Exception:
            continue
        if not (lo <= d <= today):
            continue
        for k in _PAIN_KEYS:
            v = e.get(k)
            if v is not None:
                evidence = True
                try:
                    mx = max(mx if mx is not None else 0.0, float(v))
                except Exception:
                    pass
    return evidence, mx


def advance_ramp(profile: dict, session_log: list, today: date, *, targets: dict,
                 zone_bands: dict, ramp_pp: float = RAMP_PP_PER_WEEK) -> list:
    """Advance / step-back ramp_state IN PLACE (call on PUSH only). For each cleared (cap>0)
    affected zone: pain>=threshold => interim -= ramp_pp (immediate step back, floored 0);
    else positive low-pain evidence AND >=RAMP_MIN_DAYS since last step => interim += ramp_pp
    (clamped to min(generic_floor, cap)); else HOLD. Returns human-readable change notes."""
    notes = []
    for inj in active_injuries(profile):
        allow = inj.get("physio_allowance") or {}
        state = inj.setdefault("ramp_state", {})
        thr = inj.get("ease_threshold", DEFAULT_EASE_THRESHOLD)
        for sport, caps in allow.items():
            tgt = targets.get(sport)
            for zone, cap in (caps or {}).items():
                if float(cap) <= 0 or zone not in _ZONE_IDX or not tgt or zone not in zone_bands:
                    continue                                  # not cleared / unknown → no ramp
                zs = state.setdefault(sport, {}).setdefault(zone, {"interim": 0.0, "last_progressed": None})
                ceiling = min(float(tgt[_ZONE_IDX[zone]]) - zone_bands[zone][0], float(cap))
                evidence, mx = recent_pain(session_log, sport, today)
                if mx is not None and mx >= thr:              # pain → step back (always)
                    new = max(0.0, zs["interim"] - ramp_pp)
                    if new != zs["interim"]:
                        notes.append(f"{sport}/{zone}: interim {zs['interim']:.0f}→{new:.0f}pp (pain {mx:.0f}>={thr}, step back)")
                        zs["interim"], zs["last_progressed"] = new, today.isoformat()
                elif evidence and (mx is None or mx < thr):   # positive low-pain evidence → ramp up
                    last = zs.get("last_progressed")
                    try:
                        last_d = date.fromisoformat(last) if last else None
                    except Exception:
                        last_d = None
                    if last_d is None or (today - last_d).days >= RAMP_MIN_DAYS:
                        new = min(ceiling, zs["interim"] + ramp_pp)
                        if new != zs["interim"]:
                            notes.append(f"{sport}/{zone}: interim {zs['interim']:.0f}→{new:.0f}pp (low-pain week)")
                            zs["interim"], zs["last_progressed"] = new, today.isoformat()
                # else: no evidence / too soon → HOLD (never advance on silence)
    return notes
