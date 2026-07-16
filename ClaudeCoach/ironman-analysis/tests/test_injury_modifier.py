"""Phase 5.6 — injury-protocol modifier (hybrid ramp, physio caps)."""
import sys, pathlib
from datetime import date, timedelta
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "lib"))
import injury

ZB = {"z3": (5.0, 5.0), "high": (3.0, 3.0)}
TGT = {"Run": [78, 17, 5], "Bike": [72, 22, 6]}   # generic: run z3 floor 12, run high floor 2


def prof(z3_cap=17, high_cap=0, z3_interim=0.0, last=None, ease=5):
    return {"injuries": [{"location": "right ankle",
                          "physio_allowance": {"Run": {"z3": z3_cap, "high": high_cap}},
                          "ramp_state": {"Run": {"z3": {"interim": z3_interim, "last_progressed": last}}},
                          "ease_threshold": ease}]}


def slog(*pairs, sport="Run", base="2026-07-14"):
    b = date.fromisoformat(base)
    return [{"date": (b - timedelta(days=off)).isoformat(), "sport": sport,
             "ankle_pain_during": p} for off, p in pairs]


class TestEffectiveBands:
    def test_floor_is_min_of_generic_cap_interim(self):
        assert injury.effective_bands(prof(z3_interim=3), TGT, ZB)["Run"]["z3"]["floor"] == 3      # interim binds
        assert injury.effective_bands(prof(z3_interim=20), TGT, ZB)["Run"]["z3"]["floor"] == 12    # generic floor binds

    def test_run_high_cap0_is_hard_and_ceiling_zero(self):
        # physio_cap 0 => NOT cleared => hard=True. This drives stage1's MEDICAL HARD-GATE
        # (run quality in this zone BLOCKS) - a DELIBERATE, user-approved (2026-07-15) exception
        # to "bands are soft". Do not relax this test / soften the gate "for consistency".
        b = injury.effective_bands(prof(), TGT, ZB)["Run"]["high"]
        assert b["hard"] is True and b["ceiling"] == 0

    def test_no_injury_athlete_unaffected(self):
        assert injury.effective_bands({"injuries": []}, TGT, ZB) == {}
        assert injury.effective_bands({}, TGT, ZB) == {}


class TestRamp:
    def test_low_pain_evidence_ramps_up(self):
        p = prof(z3_interim=0.0)
        injury.advance_ramp(p, slog((3, 2), (7, 1)), date(2026, 7, 14), targets=TGT, zone_bands=ZB)
        assert p["injuries"][0]["ramp_state"]["Run"]["z3"]["interim"] == injury.RAMP_PP_PER_WEEK

    def test_pain_steps_back(self):
        p = prof(z3_interim=6.0)
        injury.advance_ramp(p, slog((2, 6)), date(2026, 7, 14), targets=TGT, zone_bands=ZB)
        assert p["injuries"][0]["ramp_state"]["Run"]["z3"]["interim"] == 6.0 - injury.RAMP_PP_PER_WEEK

    def test_no_pain_data_holds(self):
        p = prof(z3_interim=4.0)
        injury.advance_ramp(p, [], date(2026, 7, 14), targets=TGT, zone_bands=ZB)
        assert p["injuries"][0]["ramp_state"]["Run"]["z3"]["interim"] == 4.0
        # run logged but pain not reported -> still HOLD (no positive low-pain evidence)
        p2 = prof(z3_interim=4.0)
        injury.advance_ramp(p2, [{"date": "2026-07-12", "sport": "Run", "ankle_pain_during": None}],
                            date(2026, 7, 14), targets=TGT, zone_bands=ZB)
        assert p2["injuries"][0]["ramp_state"]["Run"]["z3"]["interim"] == 4.0

    def test_ramp_clamped_to_generic_floor(self):
        p = prof(z3_interim=11.0, last="2026-06-01")
        injury.advance_ramp(p, slog((2, 1)), date(2026, 7, 14), targets=TGT, zone_bands=ZB)
        assert p["injuries"][0]["ramp_state"]["Run"]["z3"]["interim"] == 12.0   # min(generic_floor, cap)

    def test_run_high_cap0_never_ramps(self):
        p = prof()
        injury.advance_ramp(p, slog((2, 1)), date(2026, 7, 14), targets=TGT, zone_bands=ZB)
        assert "high" not in p["injuries"][0]["ramp_state"].get("Run", {})

    def test_up_step_idempotent_min_days(self):
        p = prof(z3_interim=2.0, last="2026-07-14")   # progressed today -> too soon
        injury.advance_ramp(p, slog((1, 1)), date(2026, 7, 14), targets=TGT, zone_bands=ZB)
        assert p["injuries"][0]["ramp_state"]["Run"]["z3"]["interim"] == 2.0
