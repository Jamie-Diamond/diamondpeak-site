"""Phase 5.7 — deterministic stage-2 quality injection (no LLM).

After the picker chooses the winning proposal, bring each sport's per-zone distribution toward
its TARGET MIDPOINT by CONVERTING easy (Z1-2) minutes into a coherent quality block (Z3 sweetspot
/ Z4-5 VO2) on a physiologically-sane day — so plans hit the intensity targets regardless of LLM
variance. TSS is held by the caller re-running close_to_target (it scales easy endurance and
protects the long sessions).

Guarantees (team-lead):
- CONSERVATIVE placement: never the long session and never the day-after-a-long-session; if no
  sane same-sport session exists for a zone → inject NOTHING for that zone and advise
  (safety > 100% guarantee).
- NEVER produce a blocking week: each conversion is re-built + re-audited; if a NEW blocking
  violation appears the step is BACKED OFF. Injection only improves or no-ops.
- Injury effective bands: hard-gated zones (Jamie run VO2) get NOTHING; capped zones stay ≤ their
  effective ceiling. Skip entirely on deload/taper. Aim the midpoint; trim symmetric over-ceiling.

Dependency-injected (build_fn/audit_fn/seg_if_fn) so it is pure + unit-testable without the LLM.
"""
import copy
from datetime import date, timedelta

_SPORTS = ("Bike", "Run", "Swim")
_MATCH = {"Bike": ("bike", "ride", "brick"), "Run": ("run",), "Swim": ("swim",)}
_SEG_ZONE = {"z3": "z3", "high": "z4"}          # written seg label (maps via _seg_if to the zone)
_DOSE_NAME = {"z3": "sweetspot", "high": "VO2"}
_MIN_DELTA = 5.0                                # ignore sub-5pp-min nudges (close enough)
_MIN_EASY_TO_CONVERT = 10.0


def _is(sport, bucket):
    s = (sport or "").lower()
    return any(k in s for k in _MATCH[bucket])


def _sess_min(s):
    return sum((sg.get("minutes") or 0) for sg in (s.get("segments") or []))


def _is_long(s):
    return "long" in (s.get("name") or "").lower()


def _cut(bucket):
    return 0.76 if bucket == "Bike" else 0.85


def _easy_min(s, bucket, seg_if_fn):
    c = _cut(bucket)
    return sum((sg.get("minutes") or 0) for sg in (s.get("segments") or [])
               if (seg_if_fn(s.get("sport", ""), sg) or 0) < c)


def _sport_total(proposal, bucket):
    return sum(_sess_min(s) for s in (proposal.get("sessions") or []) if _is(s.get("sport"), bucket))


def _zone_min(proposal, bucket, zone, seg_if_fn):
    c = _cut(bucket)
    tot = 0.0
    for s in (proposal.get("sessions") or []):
        if not _is(s.get("sport"), bucket):
            continue
        for sg in (s.get("segments") or []):
            f = seg_if_fn(s.get("sport", ""), sg) or 0
            m = sg.get("minutes") or 0
            if zone == "high" and f >= 0.90:
                tot += m
            elif zone == "z3" and c <= f < 0.90:
                tot += m
    return tot


def _days_after_long(proposal):
    out = set()
    for s in (proposal.get("sessions") or []):
        if _is_long(s):
            try:
                out.add((date.fromisoformat((s.get("date") or "")[:10]) + timedelta(days=1)).isoformat())
            except Exception:
                pass
    return out


def _sane_session(proposal, bucket, seg_if_fn):
    """Same-sport, non-long session on a sane day (not day-after-long) with the most convertible
    easy minutes. None if no candidate → caller skips + advises."""
    after = _days_after_long(proposal)
    cands = [s for s in (proposal.get("sessions") or [])
             if _is(s.get("sport"), bucket) and not _is_long(s)
             and (s.get("date") or "") not in after
             and _easy_min(s, bucket, seg_if_fn) >= _MIN_EASY_TO_CONVERT]
    return max(cands, key=lambda s: _easy_min(s, bucket, seg_if_fn)) if cands else None


def _apply(proposal, bucket, zone, delta, seg_if_fn):
    """Return a NEW proposal with `delta` (pp-min) converted: >0 = easy→zone on a sane session
    (coherent block); <0 = trim `-delta` of the zone back to easy Z2. None if up-inject has no
    sane placement."""
    p = copy.deepcopy(proposal)
    c = _cut(bucket)
    if delta > 0:
        s = _sane_session(p, bucket, seg_if_fn)
        if s is None:
            return None
        need = delta
        for sg in (s.get("segments") or []):
            if need <= 0:
                break
            if (seg_if_fn(s.get("sport", ""), sg) or 0) < c:
                take = min(need, sg.get("minutes") or 0)
                sg["minutes"] = (sg.get("minutes") or 0) - take
                need -= take
        moved = delta - need
        if moved < _MIN_DELTA:
            return None
        s.setdefault("segments", []).append({"minutes": round(moved), "zone": _SEG_ZONE[zone]})
        s["segments"] = [sg for sg in s["segments"] if (sg.get("minutes") or 0) > 0]
        if _DOSE_NAME[zone].lower() not in (s.get("name") or "").lower():
            s["name"] = f"{s.get('name', bucket + ' session')} + {_DOSE_NAME[zone]}"
        return p
    else:
        need = -delta
        for s in (p.get("sessions") or []):
            if not _is(s.get("sport"), bucket) or _is_long(s):
                continue
            for sg in list(s.get("segments") or []):
                if need <= 0:
                    break
                f = seg_if_fn(s.get("sport", ""), sg) or 0
                inzone = (f >= 0.90) if zone == "high" else (c <= f < 0.90)
                if inzone:
                    take = min(need, sg.get("minutes") or 0)
                    sg["minutes"] = (sg.get("minutes") or 0) - take
                    s["segments"].append({"minutes": round(take), "zone": "z2"})
                    need -= take
            s["segments"] = [sg for sg in s["segments"] if (sg.get("minutes") or 0) > 0]
        return p


def inject_quality(proposal, brief, athlete, target, *, build_fn, audit_fn, seg_if_fn):
    """Bring each sport/zone to its target midpoint (2-week rolling aware), conservatively placed,
    never blocking. Returns (new_proposal, notes)."""
    wt = (brief.get("week_type") or "").lower()
    if wt in ("deload", "taper"):
        return proposal, ["deload/taper → no injection (unloading is the point)"]
    targets = brief.get("distribution_targets") or {}
    injury = brief.get("injury_bands") or {}
    prior = brief.get("_prior_zones") or {}
    notes = []
    base_built = build_fn(athlete, proposal, target, brief)
    base_block, _ = audit_fn(brief, base_built, target, proposal)
    nb = len(base_block)
    for bucket in _SPORTS:
        tgt = targets.get(bucket)
        if not tgt or len(tgt) < 3:
            continue
        for zone, idx in (("z3", 1), ("high", 2)):
            ib = (injury.get(bucket) or {}).get(zone)
            if ib and ib.get("hard"):
                notes.append(f"{bucket}/{zone}: injury hard-gate → left EMPTY")
                continue
            aim = float(tgt[idx])
            if ib and ib.get("ceiling") is not None:
                aim = min(aim, float(ib["ceiling"]))
            if aim <= 0:
                continue
            # SINGLE-WEEK target midpoint (not the 2-week mean): each week should sit ON target,
            # not swing hard to compensate for a low prior week (that is a spike + is what the
            # rolling ADVISORY/picker tolerate, but the prescription itself aims the week).
            tw_tot = _sport_total(proposal, bucket)
            if tw_tot <= 0:
                continue
            want = max(0.0, min(aim / 100.0 * tw_tot, tw_tot))
            delta = want - _zone_min(proposal, bucket, zone, seg_if_fn)
            if abs(delta) < _MIN_DELTA:
                continue
            cand = _apply(proposal, bucket, zone, delta, seg_if_fn)
            if cand is None:
                notes.append(f"{bucket}/{zone}: no sane day → skipped + advise")
                continue
            built = build_fn(athlete, cand, target, brief)
            block, _ = audit_fn(brief, built, target, cand)
            if len(block) > nb:
                notes.append(f"{bucket}/{zone}: would block → backed off")
                continue
            proposal, nb = cand, len(block)
            notes.append(f"{bucket}/{zone}: {'+' if delta > 0 else ''}{delta:.0f}min → aim {aim:.0f}%")
    return proposal, notes
