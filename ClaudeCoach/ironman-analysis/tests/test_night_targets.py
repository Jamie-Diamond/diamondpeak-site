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
    lines = nb._target_lines([RIDE, s], "1:41", 305, "4:02", level="pro")
    assert all(l.startswith("•") and l.count("•") == 1 for l in lines)
    assert lines[0] == "• NP target 188 W (IF 0.62)"
    assert lines[1] == "• Main set power 183-214 W"
    assert lines[-1].startswith("• Main focus") and "name none" in lines[-1]
    assert not any(l.startswith(("Ride", "Run", "Swim", "Strength")) for l in lines)
    swim = nb._target_lines([SWIM], "1:38", 190, "5:04", level="pro")
    assert swim == ["• Target pace [the /100m band above, the pace only] vs CSS 1:38", "• Main set structure"]


def test_prompt_carries_title_targets_and_power_without_sport_templates():
    title = nb._title_line([RIDE])
    p = nb._build_prompt("jamie", "Jamie", 305, "1:41", "4:02", "IM", [],
                         pace_bands=[], power_bands=nb._power_bands([RIDE], 305),
                         sessions=nb._sessions_line([RIDE]), title=title,
                         targets=nb._target_lines([RIDE], "1:41", 305, "4:02", level="pro"), level="pro")
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


def test_jargon_follows_the_coaching_level():
    """Minimal jargon, by the level the athlete chose (Jamie, 16 Oct 2026)."""
    pro, mid, beg = (nb._target_lines([RIDE, SWIM], "1:41", 305, "4:02", level=lv)
                     for lv in ("pro", "mid", "beginner"))
    pro, mid, beg = "\n".join(pro), "\n".join(mid), "\n".join(beg)
    assert "NP target" in pro and "Main set power 183-214 W" in pro and "vs CSS" in pro
    assert "NP" not in mid and "IF" not in mid and "Main set power 183-214 W" in mid and "CSS" not in mid
    assert " W" not in beg and "Main set power" not in beg and "How it should feel" in beg
    p = nb._build_prompt("calum", "Calum", 305, "1:41", "4:02", "x", [], targets=beg.split("\n"), level="beginner")
    assert "Coaching level: BEGINNER" in p and "only, no number" in p


# --- Shadow review, 17 Oct 2026 (Jamie): the 16 Oct 20:30 night briefs ---------------------

JAMIE_RIDE = _ev("Ride", "Endurance ride", "- 15m 50-58%\n- 57m 60-70%\n- 15m 50-58%",
                 moving_time=5220, icu_training_load=55, icu_intensity=61.588177)
KB_EMPTY = _ev("WeightTraining", "Kettlebells", "", moving_time=2700, icu_training_load=30)


def test_main_set_watts_is_the_main_band_not_the_warm_up():
    """Jamie's ride listed 148-172 W and 178-208 W; Haiku printed both."""
    assert nb._main_set_watts(JAMIE_RIDE, 297) == "178-208 W"
    assert nb._main_set_watts(RIDE, 305) == "183-214 W"        # 5m 95% opener is not the main set
    intervals = _ev("Ride", "VO2", "Warmup\n- 20m 55-65%\n\nMain set 5x\n- 3m 115%\n- 3m 50%\n\n- 10m 50%")
    assert nb._main_set_watts(intervals, 300) == "345 W"       # 5x3m = 15 min counts as main
    assert nb._main_set_watts(_ev("Ride", "Long", "- 1h40m 60-70%"), 196) == "118-137 W"
    assert nb._main_set_watts(_ev("Ride", "Free ride"), 196) == ""
    assert nb._main_set_watts(JAMIE_RIDE, None) == ""


def test_one_bullet_per_line_with_duration_and_load_written_in_code():
    """Sonnet printed '• NP target ... • Main set power ...' on one line; duration/Load
    bullet came out three ways across three athletes."""
    pro = nb._target_lines([JAMIE_RIDE], "1:41", 297, "4:02", level="pro")
    assert pro[:3] == ["• 87 min · Load 55", "• NP target 183 W (IF 0.62)", "• Main set power 178-208 W"]
    assert pro[3].startswith("• HR cap") and len(pro) == 4
    mid = nb._target_lines([JAMIE_RIDE], "1:41", 297, "4:02", level="mid")
    assert mid[:2] == ["• 87 min · Load 55", "• Main set power 178-208 W"]
    beg = nb._target_lines([JAMIE_RIDE], "1:41", 297, "4:02", level="beginner")
    assert beg[0] == "• 87 min" and "Load" not in "\n".join(beg)
    two = nb._target_lines([JAMIE_RIDE, KB_EMPTY], "1:41", 297, "4:02", level="mid")
    assert "• Endurance ride: 87 min · Load 55" in two and "• Kettlebells: 45 min · Load 30" in two


def test_strength_with_no_exercises_names_none():
    """Sonnet invented swings/goblet squats/deadlifts/carries for an empty Kettlebells event."""
    lines = nb._target_lines([KB_EMPTY], None, None, None, level="mid")
    assert lines[0] == "• 45 min · Load 30"
    assert "name none" in lines[1] and "write no key-movements bullet" in lines[1]
    listed = nb._target_lines([_ev("WeightTraining", "KB", "- 3x10 swings")], None, None, None)
    assert "• Key movements [only the exercises the planned event lists]" in listed


def test_prompt_layout_wins_over_level_and_fuelling_off_drops_nutrition():
    t = nb._target_lines([JAMIE_RIDE], "1:41", 297, "4:02", level="pro")
    p = nb._build_prompt("jamie", "Jamie", 297, "1:41", "4:02", "IM", [], title=nb._title_line([JAMIE_RIDE]),
                         targets=t, level="pro", fuel="0 g/hr carbs, water to thirst")
    assert "Coaching level: PRO" in p and "no narrative or summary sentence" in p
    assert "exactly these bullets, one per line" in p and "2-4 bullets" not in p
    assert "*Nutrition:* 0 g/hr carbs, water to thirst" in p
    k = nb._build_prompt("kathryn", "Kathryn", 186, "1:38", "5:04", "IM", [],
                         title=nb._title_line([KB_EMPTY]), targets=nb._target_lines([KB_EMPTY], None, None, None),
                         fuelling=False)
    assert "Nutrition" not in k and "*Sleep:* ≥8h tonight" in k
