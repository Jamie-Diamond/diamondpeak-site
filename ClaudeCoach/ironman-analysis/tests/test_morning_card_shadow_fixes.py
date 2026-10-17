"""Morning card fixes from the Haiku-vs-Sonnet shadow review (Jamie, 16 Oct 2026).

Each one moves a decision or a sum out of the prompt into Python (lib/shadow.py method):
P1 Form reaches the card as Peak's word; P2 the one question is picked and appended in
code; P3 watchdog flags carry a Python-made fact and an `actionable` field; P4 "already
done today" never prints None; P5 a week-so-far fact and the note cap enforced in code.
"""
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "lib"), str(REPO / "ironman-analysis"), str(REPO / "telegram")]
spec = importlib.util.spec_from_file_location("mc_shadow", REPO / "scripts" / "morning-checkin.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)
import recovery_score as rs      # noqa: E402
import watchdog_checks as wc     # noqa: E402

FRI = date(2026, 10, 16)


# ── P1: Form ──────────────────────────────────────────────────────────────────

def test_tsb_is_fitness_minus_fatigue_from_icu_rows():
    rows = [{"id": "2026-10-15", "ctl": 60, "atl": 70, "hrv": 40},
            {"id": "2026-10-16", "ctl": 61.4, "atl": 79.9, "hrv": 42, "sleepSecs": 7 * 3600}]
    _, _, tsb, _, _ = rs._parse_wellness(rows)
    assert tsb == -18.5
    assert rs._parse_wellness([{"id": "2026-10-16", "hrv": 40}])[2] is None


def test_form_word_matches_peak_bands():
    assert [rs.form_word(v) for v in (5.1, 5, -10, -10.1, -30, -30.1, None)] == \
        ["fresh", "balanced", "balanced", "building, tired", "building, tired", "very tired", None]


def test_recovery_score_with_a_real_tsb_is_sensible():
    good = dict(hrv_today=45, hrv_baseline=44, sleep_hrs=7.6, pain=0)
    assert rs.compute(tsb=12, **good)["label"] == "GREEN"          # fresh is ready
    assert rs.compute(tsb=-20, **good)["label"] == "GREEN"         # normal building week
    assert rs.compute(tsb=-35, **good)["label"] == "AMBER"         # very tired: monitor, no more
    # Heavy fatigue on a low-HRV, short-sleep morning: reduce.
    low = rs.compute(hrv_today=38, hrv_baseline=44, tsb=-35, sleep_hrs=6.6, pain=0)
    assert low["label"] == "ORANGE"


def test_form_alone_never_scores_an_athlete_with_no_sleep_device():
    r = rs.compute(tsb=-25, pain=0)
    assert r["signals"]["tsb"]["score"] is None and r["label"] == "GREEN"
    assert r == rs.compute(tsb=None, pain=0) | {"signals": r["signals"]}


def test_form_block_decides_the_form_line():
    none, line = mc._form_block(None)
    assert "say NOTHING about how fresh" in none and line == ""
    fresh, line = mc._form_block("fresh")
    assert "good day for quality work" in fresh and line == ""
    off, _ = mc._form_block("fresh", off_normal="Off your normal: HRV 38 (usual 45)")
    assert "never call it a good day for quality" in off
    tired, line = mc._form_block("very tired")
    assert "**Load:**" in line
    post, line = mc._form_block("fresh", post_race_recovery=True)
    assert "never a cue for quality work" in post and line == ""
    assert "never as final" in mc._form_block("balanced", finalized=False)[0]


def test_prompt_carries_the_form_word_and_no_number():
    rec = rs.compute(hrv_today=45, hrv_baseline=44, tsb=-18.5, sleep_hrs=7.5, pain=0)
    p = mc._build_prompt("x", "X", "", "", None, [], recovery=rec, form="building, tired")
    assert "Form: building, tired" in p and "-18.5" not in p
    assert "how fresh or loaded ONLY from the Form block" in p


# ── P2: the question ──────────────────────────────────────────────────────────

def test_one_question_in_priority_order():
    inj = [{"location": "ankle"}]
    q = mc._the_question
    assert q(cycle_overdue=True, injuries=inj, ask_ankle=True, ask_morning_pain=True,
             ask_weight=True) == mc.Q_CYCLE
    assert q(injuries=inj, ask_ankle=True, ask_morning_pain=True, ask_weight=True) == mc.Q_MORNING_PAIN
    assert q(injuries=inj, ask_ankle=True, run_today=True, ankle_pain_next_morning=2,
             ask_weight=True) == mc.Q_RUN_PAIN
    # No run left today, or a 0 morning score: weight.
    assert q(injuries=inj, ask_ankle=True, run_today=False, ankle_pain_next_morning=2,
             ask_weight=True) == mc.Q_WEIGHT
    assert q(injuries=inj, ask_ankle=True, run_today=True, ankle_pain_next_morning=0,
             ask_weight=True) == mc.Q_WEIGHT
    assert q(injuries=inj, ask_ankle=False, ask_morning_pain=True) == ""
    assert q() == ""


def test_prompt_no_longer_resolves_the_question():
    p = mc._build_prompt("x", "X", "", "", None, [{"location": "ankle"}], question=mc.Q_WEIGHT)
    assert "Else:" not in p and "pain_next_morning in current-state.json" not in p
    assert "Ask NOTHING" in p
    assert "AT MOST 42 words" in p          # 45 minus the 3-word weight question


def test_the_appended_question_is_what_ask_gate_records():
    import ask_gate
    assert ask_gate.asked_in_text(ask_gate.WEIGHT, mc.Q_WEIGHT)
    assert ask_gate.asked_in_text(ask_gate.ANKLE_SCORE, mc.Q_MORNING_PAIN)
    assert ask_gate.asked_in_text(ask_gate.ANKLE_SCORE, mc.Q_RUN_PAIN)


# ── P3: watchdog flags ────────────────────────────────────────────────────────

def _act(n, km):
    return {"id": f"r{n}", "type": "Run", "distance": km * 1000,
            "start_date_local": (FRI - timedelta(days=n)).isoformat() + "T07:00:00"}


def test_t10_names_the_4_week_average_not_week_on_week():
    # Kathryn, 11 Oct 2026: 0 km last week, so the baseline was her 4-week average.
    acts = [_act(1, 10), _act(3, 9.8), _act(12, 18), _act(19, 16), _act(26, 17)]
    f = wc.t10_run_km(acts, FRI)
    assert "4-week average" in f["signal"] and "week-on-week" not in f["signal"]
    assert "Mon 12 Oct-Fri 16 Oct" in f["signal"]


def test_t12_is_actionable_only_for_the_week_just_finished_off_icu():
    b = {"week_start": "2026-10-05", "week_end": "2026-10-11", "ctl_from": 50, "ctl_to": 56,
         "ramp": 6, "cap": 4, "tier": 2, "source": "icu_fitness"}
    assert wc.t12_ramp([b], FRI)["actionable"] is True
    old = dict(b, week_start="2026-09-14", week_end="2026-09-20")
    assert wc.t12_ramp([old], FRI)["actionable"] is False
    approx = dict(b, source="session_log")
    f = wc.t12_ramp([approx], FRI)
    assert f["actionable"] is False and "(approximate)" in f["signal"]


def test_record_keeps_the_fact_for_the_card(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "BASE", tmp_path)
    (tmp_path / "athletes" / "x").mkdir(parents=True)
    f = wc._fire("T5", 1, ["e1", "e2"], "2 planned sessions missed in the last 7 days: Tue 13 Oct Easy run")
    wc.record("x", [f], FRI)
    cards = wc.card_flags(wc.load_state("x"), FRI)
    assert cards == [{"trigger": "T5", "date": "2026-10-16", "tier": 1, "actionable": True,
                      "fact": f["signal"]}]
    assert wc.card_flags(wc.load_state("x"), FRI + timedelta(days=4)) == []


def _adir(tmp_path, md_lines, logged=None):
    adir = tmp_path / "a"
    adir.mkdir()
    (adir / "current-state.md").write_text("# State\n\n## Watchdog flags\n" + "\n".join(md_lines)
                                           + "\n\n## Other\n")
    if logged is not None:
        (adir / wc.STATE_NAME).write_text(json.dumps({"logged": logged}))
    return adir


def test_card_prefers_the_structured_fact_and_hides_non_actionable(tmp_path):
    md = ["- **2026-10-15 — T5 (Tier 1):** 2 planned sessions missed: Tue 13 Oct Easy run; "
          "Wed 14 Oct Tempo. Keep the runs easy until HRV is back toward 65.",
          "- **2026-10-16 — T12 (Tier 2):** realised CTL 6.8 -> 11.3. Keep ride load flat."]
    logged = {"T5": [{"date": "2026-10-15", "items": ["a"], "tier": 1, "actionable": True,
                      "fact": "2 planned sessions missed in the last 7 days: Tue 13 Oct Easy run; Wed 14 Oct Tempo"}],
              "T12": [{"date": "2026-10-16", "items": ["w"], "tier": 2, "actionable": False,
                       "fact": "realised CTL 6.8 -> 11.3 over 2026-09-28..2026-10-04"}]}
    flags = mc._recent_flags(_adir(tmp_path, md, logged), FRI)
    assert flags == ["T5: 2 planned sessions missed in the last 7 days: Tue 13 Oct Easy run; Wed 14 Oct Tempo"]


def test_legacy_lines_without_a_record(tmp_path):
    md = [
        # T12 race-week line: "no training action implied" slipped past the old filter.
        "- **2026-10-14 — T12 (Tier 2):** realised CTL ramp +4.7/wk. It covers the race week, "
        "so it is historical with no training action implied.",
        # Wellness trigger with day-old reasoning: never a card flag.
        "- **2026-10-15 — T3 (Tier 1):** 7-day HRV average 40 vs 43 (-8%). Resting HR 55 today.",
        # T10 with the old label and the model's wellness sentence after it.
        "- **2026-10-14 — T10 (Tier 2):** run km +24% week-on-week (15.8 km vs 12.7 km 4-week "
        "average, 0.0 km last week) - 10% cap applies. Keep runs easy, since HRV and resting HR "
        "are still off baseline.",
    ]
    flags = mc._recent_flags(_adir(tmp_path, md), FRI)
    assert flags == ["T10: run km +24% over the 4-week average (15.8 km vs 12.7 km 4-week "
                     "average, 0.0 km last week) - 10% cap applies"]


# ── P4: already done today ────────────────────────────────────────────────────

def test_done_today_has_sport_and_duration_never_none():
    line = mc._done_today_line([
        {"type": "Ride", "name": None, "moving_time": 3900},
        {"type": "Run", "name": "Threshold 3x8", "moving_time": 2700},
        {"type": None, "name": None, "moving_time": 0},
    ])
    assert line == "Ride, 1 h 05 min; Run, 45 min (Threshold 3x8)"
    assert "None" not in line


# ── P5: week so far and the cap ───────────────────────────────────────────────

def test_week_so_far_counts_planned_done_and_hours():
    def d(n):
        return (FRI - timedelta(days=n)).isoformat() + "T07:00:00"
    events = [{"category": "WORKOUT", "type": "Ride", "start_date_local": d(4), "moving_time": 3600,
               "paired_activity_id": "a1"},
              {"category": "WORKOUT", "type": "Run", "start_date_local": d(3), "moving_time": 2400},
              {"category": "WORKOUT", "type": "Swim", "start_date_local": d(2), "moving_time": 2700},
              {"category": "NOTE", "type": "Note", "start_date_local": d(2)}]
    acts = [{"id": "a1", "type": "Ride", "start_date_local": d(4), "moving_time": 3700},
            {"id": "a2", "type": "VirtualRun", "start_date_local": d(3), "moving_time": 2300},
            {"id": "a3", "type": "Run", "start_date_local": d(0), "moving_time": 1800}]   # today: not counted
    assert mc._week_so_far(events, acts, FRI) == (
        "Week so far (Mon 12 Oct to Thu 15 Oct): 2 of 3 planned sessions done; "
        "1 h 40 min of training against 2 h 25 min planned.")
    mon = date(2026, 10, 12)
    assert mc._week_so_far([], [], mon).startswith("Last week (Mon 05 Oct to Sun 11 Oct)")


def test_cap_keeps_the_opening_and_two_lines():
    note = ("Big ride today and you're fresh: hold the first hour back.\n\n"
            "**Fuel:** aim for 70g of carbs an hour.\n"
            "**Heat:** a heat bath is due.\n"
            "**To do:** book the bike fit by Sunday.\n\n"
            "Weight this morning?")
    out, cut = mc._cap_note(note, mc.Q_WEIGHT)
    assert out.startswith("Big ride today") and "**Fuel:**" in out and "**Heat:**" in out
    assert "**To do:**" not in out and "Weight this morning?" not in out
    assert len(cut) == 2


def test_cap_drops_lines_to_the_word_budget_but_never_the_opening():
    long_line = "**Recovery:** " + " ".join(["word"] * 25)
    note = "Short opening here.\n" + long_line + "\n" + long_line
    out, cut = mc._cap_note(note)
    assert out == "Short opening here.\n" + long_line and len(cut) == 1
    huge = " ".join(["w"] * 60)
    assert mc._cap_note(huge) == (huge, [])
    ok = "Opening.\n**Fuel:** eat."
    assert mc._cap_note(ok) == (ok, [])


def test_legacy_t12_only_for_the_week_just_finished(tmp_path):
    # Fred, 16 Oct 2026: a T12 line for 28 Sep-4 Oct, two weeks gone.
    line = "- **2026-10-16 — T12 (Tier 2):** Realised fitness rose 6.8 to 11.3. Keep ride load flat."
    adir = _adir(tmp_path, [line])
    assert mc._recent_flags(adir, FRI) == []
    (adir / "current-state.json").write_text(json.dumps({"watchdog_flags": [
        {"trigger": "T12", "week_end": "2026-10-11", "source": "icu_fitness"}]}))
    assert mc._recent_flags(adir, FRI) == ["T12: Realised fitness rose 6.8 to 11.3"]


def test_hrv_trend_reaches_the_card_and_stops_fresh():
    """17 Oct 2026: HRV 37 / resting HR 54 did not trip the SD rule, the T3 trend was kept
    off the card, and it said "fresh". The trend is now worked out fresh into off-normal."""
    from datetime import date, timedelta
    today = date(2026, 10, 17)
    rows = [{"id": (today - timedelta(days=i)).isoformat(), "hrv": 38 if i < 7 else 43,
             "sleepSecs": 28000, "restingHR": 52} for i in range(14)]
    line = mc._with_trends("", rows, today)
    assert line.startswith("Off your normal:") and "7-day HRV average" in line
    block, _ = mc._form_block("fresh", off_normal=line)
    assert "never call it a good day for quality" in block
    assert mc._with_trends("Off your normal: resting HR 56 (usual 52)", [], today) \
        == "Off your normal: resting HR 56 (usual 52)"
