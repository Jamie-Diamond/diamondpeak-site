"""Phase 5.5 — symmetric per-sport per-zone bands (floor + ceiling)."""
from primitives.validate_plan import zone_band_deviations, check_intensity_budget

TGT = {"Bike": [72, 22, 6], "Run": [78, 17, 5], "Swim": [62, 0, 38]}


def ps(**kw):
    """kw: Sport=(z3_pct, high_pct); min fixed at 400 (>= min_minutes/2)."""
    return {sp: {"z3_pct": z3, "high_pct": hi, "min": 400} for sp, (z3, hi) in kw.items()}


def _has(devs, sport, zone, kind):
    return any(d["sport"] == sport and d["zone"] == zone and d["kind"] == kind for d in devs)


class TestSymmetricBands:
    def test_bike_vo2_under_floor_advises(self):
        assert _has(zone_band_deviations(ps(Bike=(22, 0)), TGT), "Bike", "high", "floor")

    def test_bike_vo2_over_ceiling_advises(self):
        assert _has(zone_band_deviations(ps(Bike=(22, 15)), TGT), "Bike", "high", "ceiling")

    def test_bike_vo2_in_band_clean(self):
        devs = zone_band_deviations(ps(Bike=(22, 6)), TGT)
        assert not any(d["sport"] == "Bike" and d["zone"] == "high" for d in devs)

    def test_deload_drops_floor_keeps_ceiling(self):
        assert not any(d["kind"] == "floor"
                       for d in zone_band_deviations(ps(Bike=(22, 0)), TGT, deload=True))
        assert _has(zone_band_deviations(ps(Bike=(22, 15)), TGT, deload=True), "Bike", "high", "ceiling")

    def test_run_vo2_floor_fires_generic(self):
        # Phase 5.6 flip: run Z4-5 floor is GENERIC now — a healthy runner (no injury_bands)
        # at 0% vs target 5% IS nudged toward its run-VO2 target, like the bike.
        assert _has(zone_band_deviations(ps(Run=(17, 0)), TGT), "Run", "high", "floor")

    def test_run_vo2_floor_suppressed_by_injury_cap0(self):
        # an injured athlete with a physio cap of 0 (hard) gets NO run-VO2 floor (and no soft dev)
        ib = {"Run": {"high": {"floor": 0.0, "ceiling": 0.0, "cap": 0.0, "hard": True}}}
        devs = zone_band_deviations(ps(Run=(17, 0)), TGT, injury_bands=ib)
        assert not any(d["sport"] == "Run" and d["zone"] == "high" for d in devs)

    def test_run_vo2_ceiling_still_applies(self):
        assert _has(zone_band_deviations(ps(Run=(17, 20)), TGT), "Run", "high", "ceiling")

    def test_z3_floor_and_ceiling(self):
        assert _has(zone_band_deviations(ps(Bike=(10, 6)), TGT), "Bike", "z3", "floor")
        assert _has(zone_band_deviations(ps(Bike=(35, 6)), TGT), "Bike", "z3", "ceiling")

    def test_picker_prefers_in_band(self):
        dev_in = sum(d["dev"] for d in zone_band_deviations(ps(Bike=(22, 6)), TGT))
        dev_out = sum(d["dev"] for d in zone_band_deviations(ps(Bike=(22, 0)), TGT))
        assert dev_in < dev_out

    def test_check_intensity_budget_emits_floor_and_ceiling_codes(self):
        vs = check_intensity_budget(0, 2000, [78, 17, 5],
                                    per_sport=ps(Bike=(22, 0)), per_sport_targets=TGT)
        assert any(v.code == "vo2_low_bike" for v in vs)
        vs2 = check_intensity_budget(0, 2000, [78, 17, 5],
                                     per_sport=ps(Bike=(22, 15)), per_sport_targets=TGT)
        assert any(v.code == "vo2_high_bike" for v in vs2)

    def test_all_advisories_soft(self):
        vs = check_intensity_budget(0, 2000, [78, 17, 5],
                                    per_sport=ps(Bike=(10, 15)), per_sport_targets=TGT)
        assert vs and all(v.severity == "soft" for v in vs)
