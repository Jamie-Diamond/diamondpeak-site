"""Phase 5.7 — deterministic quality-injection tests (no LLM; injected build/audit stubs)."""
import sys, pathlib, copy
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "lib"))
import quality_inject as qi

# faithful _seg_if mirror (post run-z3 fix: run z3 = 0.88)
_IF = {"bike": {"z1": 0.55, "z2": 0.65, "z3": 0.80, "z4": 0.95, "z5": 1.05},
       "run":  {"z1": 0.60, "z2": 0.83, "z3": 0.88, "z4": 0.97, "z5": 1.06},
       "swim": {"z1": 0.60, "z2": 0.72, "z3": 0.85, "z4": 1.00, "z5": 1.08}}
def seg_if(sport, seg):
    if seg.get("if") is not None:
        return seg["if"]
    s = (sport or "").lower()
    sp = "bike" if any(k in s for k in ("bike", "ride", "brick")) else "run" if "run" in s else "swim" if "swim" in s else ""
    return _IF.get(sp, {}).get((seg.get("zone") or "").lower().strip(), 0.65)

def zones(proposal, bucket):
    cut = 0.76 if bucket == "Bike" else 0.85
    z3 = hi = tot = 0.0
    for s in proposal["sessions"]:
        if not qi._is(s.get("sport"), bucket):
            continue
        for sg in s.get("segments", []):
            f = seg_if(s.get("sport"), sg); m = sg.get("minutes", 0)
            tot += m
            if f >= 0.90: hi += m
            elif f >= cut: z3 += m
    return (round(z3/tot*100) if tot else 0, round(hi/tot*100) if tot else 0, tot)

# stub build/audit: build echoes total minutes; audit clean unless a rule says block
def build_ok(ath, prop, tgt, brief):
    return {"sessions": prop["sessions"], "total_tss": sum(qi._sess_min(s) for s in prop["sessions"]), "ok": True}
def audit_clean(brief, built, tgt, prop):
    return [], []

def easy(sport, mins, name, date):
    return {"sport": sport, "name": name, "date": date, "segments": [{"minutes": mins, "zone": "z2"}]}

def kathryn_all_easy():
    return {"sessions": [
        easy("Bike", 120, "Endurance ride", "2026-08-05"),
        easy("Bike", 180, "Long ride", "2026-08-08"),      # long — protected/not injected onto
        easy("Run", 60, "Easy run", "2026-08-06"),
        easy("Run", 90, "Long run", "2026-08-09"),
        easy("Swim", 60, "Aerobic swim", "2026-08-04"),
    ]}

KATH = {"week_type": "build",
        "distribution_targets": {"Bike": [70, 18, 12], "Run": [78, 12, 10], "Swim": [65, 0, 35]},
        "injury_bands": {}, "_prior_zones": {}}


class TestInjection:
    def test_all_easy_hits_targets(self):
        out, notes = qi.inject_quality(kathryn_all_easy(), KATH, "kathryn", 640,
                                       build_fn=build_ok, audit_fn=audit_clean, seg_if_fn=seg_if)
        bz3, bhi, _ = zones(out, "Bike"); rz3, rhi, _ = zones(out, "Run"); _, shi, _ = zones(out, "Swim")
        assert 14 <= bz3 <= 22 and 8 <= bhi <= 15, (bz3, bhi)       # bike ~18/12
        assert 8 <= rz3 <= 16 and 6 <= rhi <= 14, (rz3, rhi)        # run ~12/10
        assert shi >= 25, shi                                       # swim toward 35

    def test_convert_preserves_volume(self):
        p = kathryn_all_easy()
        before = {b: zones(p, b)[2] for b in ("Bike", "Run", "Swim")}
        out, _ = qi.inject_quality(p, KATH, "kathryn", 640, build_fn=build_ok, audit_fn=audit_clean, seg_if_fn=seg_if)
        after = {b: zones(out, b)[2] for b in ("Bike", "Run", "Swim")}
        assert before == after, (before, after)                    # CONVERT, not ADD

    def test_jamie_run_vo2_stays_empty(self):
        brief = {"week_type": "specific",
                 "distribution_targets": {"Bike": [72, 22, 6], "Run": [78, 17, 5], "Swim": [62, 0, 38]},
                 "injury_bands": {"Run": {"z3": {"floor": 0, "ceiling": 17, "cap": 17, "hard": False},
                                          "high": {"floor": 0, "ceiling": 0, "cap": 0, "hard": True}}},
                 "_prior_zones": {}}
        p = {"sessions": [easy("Bike", 180, "Endurance ride", "2026-07-29"),
                          easy("Run", 60, "Easy run", "2026-07-30"),
                          easy("Swim", 60, "Aerobic swim", "2026-07-28")]}
        out, notes = qi.inject_quality(p, brief, "jamie", 700, build_fn=build_ok, audit_fn=audit_clean, seg_if_fn=seg_if)
        _, rhi, _ = zones(out, "Run"); bz3, bhi, _ = zones(out, "Bike")
        assert rhi == 0, (rhi, notes)                              # run VO2 hard-gated → EMPTY
        assert bhi <= 9 and bz3 >= 15, (bz3, bhi)                  # bike sweetspot-led + small VO2 ~6

    def test_deload_no_injection(self):
        p = kathryn_all_easy()
        brief = dict(KATH); brief["week_type"] = "deload"
        out, notes = qi.inject_quality(p, brief, "kathryn", 400, build_fn=build_ok, audit_fn=audit_clean, seg_if_fn=seg_if)
        assert out == p and "no injection" in notes[0]

    def test_over_ceiling_trimmed(self):
        p = {"sessions": [{"sport": "Bike", "name": "VO2 ride", "date": "2026-08-05",
                           "segments": [{"minutes": 60, "zone": "z4"}, {"minutes": 120, "zone": "z2"}]},
                          easy("Bike", 60, "Easy spin", "2026-08-07")]}
        out, notes = qi.inject_quality(p, KATH, "kathryn", 640, build_fn=build_ok, audit_fn=audit_clean, seg_if_fn=seg_if)
        _, bhi, _ = zones(out, "Bike")
        assert bhi <= 15, (bhi, notes)                            # trimmed from 25% toward 12

    def test_no_sane_day_skips(self):
        # only bike session is a LONG ride → no sane placement → skip + advise
        p = {"sessions": [easy("Bike", 180, "Long ride", "2026-08-08"),
                          easy("Swim", 60, "Aerobic swim", "2026-08-04")]}
        out, notes = qi.inject_quality(p, KATH, "kathryn", 640, build_fn=build_ok, audit_fn=audit_clean, seg_if_fn=seg_if)
        bz3, bhi, _ = zones(out, "Bike")
        assert bz3 == 0 and bhi == 0, (bz3, bhi)                  # bike untouched
        assert any("no sane day" in n for n in notes)

    def test_would_block_backed_off(self):
        # audit blocks any proposal containing a bike z4 (VO2) segment → bike VO2 injection backs off
        def audit_block_vo2(brief, built, tgt, prop):
            for s in prop["sessions"]:
                for sg in s.get("segments", []):
                    if sg.get("zone") == "z4" and qi._is(s.get("sport"), "Bike"):
                        return ["bike VO2 blocked (stub)"], []
            return [], []
        p = kathryn_all_easy()
        out, notes = qi.inject_quality(p, KATH, "kathryn", 640, build_fn=build_ok, audit_fn=audit_block_vo2, seg_if_fn=seg_if)
        _, bhi, _ = zones(out, "Bike")
        assert bhi == 0, (bhi, notes)                             # bike VO2 backed off (would block)
        assert any("backed off" in n for n in notes)
