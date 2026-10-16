"""Night-before brief: titles, pace/power bands, NP/IF and target bullets worked out in code
(Jamie, 16 Oct 2026: shadow review)."""
import importlib.util
import sys
from pathlib import Path

CC = Path(__file__).resolve().parents[2]
sys.argv = ["x"]
_spec = importlib.util.spec_from_file_location("nbb_t", CC / "scripts" / "night-before-brief.py")
nb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nb)


def _ev(t, name, desc="", start="2026-10-17T00:00:00", **kw):
    return {"category": "WORKOUT", "type": t, "name": name, "description": desc,
            "start_date_local": start, **kw}


SWIM = _ev("Swim", "Technique swim", "- 10m 70-80% Pace\n- 30m 58-66% Pace\n- 5m 70-80% Pace")
RUN = _ev("Run", "Tempo run", "- 20m 78-88% Pace")
RIDE = _ev("Ride", "Endurance ride", "- 15m 50-58%\n- 57m 60-70%\n- 5m 95%\n- 10m 70% HR",
           icu_intensity=61.588177)


def test_swim_pace_is_per_100m_from_css_not_run_threshold():
    bands = nb._pace_bands([SWIM], "5:04/km - DERIVED working estimate", "1:38")
    assert bands == ["Swim 70-80% Pace = 2:03-2:20/100m", "Swim 58-66% Pace = 2:28-2:49/100m"]
    assert not any("/km" in b for b in bands)


def test_run_pace_unchanged_and_ride_gets_no_pace():
    assert nb._pace_bands([RUN], "4:02", "1:41") == ["Run 78-88% Pace = 4:35-5:10/km"]
    assert nb._pace_bands([RIDE], "4:02", "1:41") == []
    assert nb._pace_bands([SWIM], "4:02", None) == []          # no CSS, no swim band


def test_power_bands_from_ftp_rides_only():
    assert nb._power_bands([RIDE], 305) == ["Ride 50-58% FTP = 152-177 W",
                                            "Ride 60-70% FTP = 183-214 W",
                                            "Ride 95% FTP = 290 W"]           # % HR skipped
    assert nb._power_bands([RUN, SWIM], 305) == []
    assert nb._power_bands([RIDE], None) == []


def test_np_and_if_to_two_dp():
    assert nb._np_if(RIDE, 305) == "NP target 188 W (IF 0.62)"
    assert nb._np_if({"icu_intensity": 64.8}, 190) == "NP target 123 W (IF 0.65)"
    assert nb._np_if({"icu_intensity": None}, 190) == ""


def test_title_built_from_names_in_calendar_order():
    s = _ev("WeightTraining", "Strength: quads", start="2026-10-17T18:00:00", moving_time=2400)
    assert nb._title_line([s, RIDE]) == "*Tomorrow — Endurance ride + Strength: quads*"
    assert "WeightTraining" not in nb._title_line([s])


def test_targets_only_tomorrows_sports_and_no_labels():
    s = _ev("WeightTraining", "Kettlebells")
    lines = nb._target_lines([RIDE, s], "1:41", 305, "4:02")
    assert len(lines) == 2 and all(l.startswith("•") for l in lines)
    assert lines[0].startswith("• NP target 188 W (IF 0.62) • Main set power")
    assert lines[1] == "• Main focus • Key movements"
    assert not any(l.startswith(("Ride", "Run", "Swim", "Strength")) for l in lines)
    swim = nb._target_lines([SWIM], "1:38", 190, "5:04")
    assert swim == ["• Target pace [the /100m band above] vs CSS 1:38 • Main set structure"]


def test_prompt_carries_title_targets_and_power_without_sport_templates():
    title = nb._title_line([RIDE])
    p = nb._build_prompt("jamie", "Jamie", 305, "1:41", "4:02", "IM", [],
                         pace_bands=[], power_bands=nb._power_bands([RIDE], 305),
                         sessions=nb._sessions_line([RIDE]), title=title,
                         targets=nb._target_lines([RIDE], "1:41", 305, "4:02"))
    assert title in p and "[session name]" not in p
    assert "Ride 60-70% FTP = 183-214 W" in p and "NP target 188 W (IF 0.62)" in p
    for label in ("\nRide:", "\nRun:", "\nSwim:", "\nStrength:"):
        assert label not in p
    assert "Target pace" not in p                       # no run/swim template for a ride day


def test_threshold_line_drops_prose():
    p = nb._build_prompt("kathryn", "Kathryn", 190, "1:38", "5:04/km - DERIVED working estimate",
                         "IM", [])
    assert "run threshold 5:04/km," in p and "DERIVED" not in p


class _FakeIcu:
    def __init__(self, ride_ftp):
        self.ride_ftp, self.calls = ride_ftp, 0

    def get_wellness(self, days=2):
        return []

    def get_sport_settings(self):
        self.calls += 1
        return [{"types": ["Ride", "VirtualRide"], "ftp": self.ride_ftp},
                {"types": ["Run", "TrailRun"], "hr_zones": [130, 146, 160]},
                {"types": ["Swim"]}]


def test_ride_ftp_comes_from_intervals_sport_settings_in_the_same_call():
    from datetime import date
    c = _FakeIcu(297)
    form, cap, ftp = nb._form_and_cap(c, date(2026, 10, 16))
    assert (cap, ftp, c.calls) == ("146", 297, 1)
    assert nb._power_bands([RIDE], ftp)[1] == "Ride 60-70% FTP = 178-208 W"   # matches ICU's Z2 watts
    assert nb._np_if(RIDE, ftp) == "NP target 183 W (IF 0.62)"
    assert nb._form_and_cap(_FakeIcu(None), date(2026, 10, 16))[2] is None    # caller falls back to profile
