"""Realised-TID audit primitive (methodology audit P1-1, Phase 3)."""
from primitives.realised_tid import classify_activity, realised_tid, tid_verdict


def _a(mins, if_=None, hr=None, type_="Ride"):
    d = {"moving_time": mins * 60, "type": type_}
    if if_ is not None:
        d["icu_intensity"] = if_
    if hr is not None:
        d["average_heartrate"] = hr
    return d


class TestClassify:
    def test_power_if_bounds(self):
        assert classify_activity(_a(60, if_=0.65)) == "low"
        assert classify_activity(_a(60, if_=0.80)) == "moderate"
        assert classify_activity(_a(60, if_=0.92)) == "high"

    def test_icu_percent_form_normalised(self):
        assert classify_activity(_a(60, if_=65)) == "low"
        assert classify_activity(_a(60, if_=92)) == "high"

    def test_hr_fallback_uses_lthr(self):
        assert classify_activity(_a(45, hr=150, type_="Run"), lthr=180) == "low"
        assert classify_activity(_a(45, hr=160, type_="Run"), lthr=180) == "moderate"
        assert classify_activity(_a(45, hr=175, type_="Run"), lthr=180) == "high"

    def test_unclassifiable(self):
        assert classify_activity(_a(45, type_="Run")) is None            # no signal
        assert classify_activity(_a(45, if_=0.6, type_="WeightTraining")) is None
        assert classify_activity(_a(0, if_=0.6)) is None


class TestRealisedTid:
    def test_time_weighted_split(self):
        acts = [_a(120, if_=0.65), _a(30, if_=0.80), _a(30, if_=0.92)]
        r = realised_tid(acts)
        assert (r["low_pct"], r["moderate_pct"], r["high_pct"]) == (67, 17, 17)
        assert r["classified_hours"] == 3.0

    def test_none_when_nothing_classifiable(self):
        assert realised_tid([_a(45, type_="Run")]) is None
        assert realised_tid([]) is None


class TestVerdict:
    TARGET = [80, 12, 8]

    def test_on_distribution(self):
        v = tid_verdict({"low_pct": 78, "moderate_pct": 15, "high_pct": 7,
                         "classified_hours": 6}, self.TARGET)
        assert v["breach"] is None

    def test_excess_quality_fires(self):
        # the audit\'s Kathryn shape: 36/51/14 vs 80% low target
        v = tid_verdict({"low_pct": 36, "moderate_pct": 51, "high_pct": 14,
                         "classified_hours": 6}, self.TARGET)
        assert v["breach"][0] == "excess_quality"

    def test_missing_quality_fires(self):
        v = tid_verdict({"low_pct": 100, "moderate_pct": 0, "high_pct": 0,
                         "classified_hours": 6}, self.TARGET)
        assert v["breach"][0] == "missing_quality"

    def test_missing_quality_needs_volume_and_a_quality_target(self):
        v = tid_verdict({"low_pct": 100, "moderate_pct": 0, "high_pct": 0,
                         "classified_hours": 2}, self.TARGET)      # tiny week
        assert v["breach"] is None
        v2 = tid_verdict({"low_pct": 100, "moderate_pct": 0, "high_pct": 0,
                          "classified_hours": 6}, [90, 7, 3])      # base-ish target
        assert v2["breach"] is None
