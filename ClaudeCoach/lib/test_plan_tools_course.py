"""Tests for `plan_tools.py course` (lib/course.py through the CLI). Run:
python3 -m pytest ClaudeCoach/lib/test_plan_tools_course.py"""
import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import course  # noqa: E402
import plan_tools  # noqa: E402

KM_PER_DEG_LAT = 111.195


def gpx_text(legs):
    """A GPX heading due north: legs = [(km, grade)], a point every 500 m."""
    pts, km, ele = [], 0.0, 0.0
    pts.append((km, ele))
    for length, grade in legs:
        for _ in range(int(length / 0.5)):
            km += 0.5
            ele += 500 * grade
            pts.append((km, ele))
    body = "".join(f'<trkpt lat="{51 + k / KM_PER_DEG_LAT:.6f}" lon="-1.000000"><ele>{e:.1f}</ele></trkpt>'
                   for k, e in pts)
    return f'<?xml version="1.0"?><gpx><trk><trkseg>{body}</trkseg></trk></gpx>'


def ns(**kw):
    base = dict(athlete="tester", race=None, gpx=None, name=None, save=False, watts=None, kg=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def no_training_data(monkeypatch):
    monkeypatch.setattr(plan_tools, "_training_data", lambda slug: {})


def test_gpx_flat_then_climb(tmp_path):
    f = tmp_path / "little-hill.gpx"
    f.write_text(gpx_text([(2, 0.0), (3, 0.06)]))
    out = plan_tools.cmd_course(ns(gpx=str(f), watts=250, kg=80))
    assert out["course"]["name"] == "little hill"           # from the file name
    assert out["course"]["ascent_m"] > 150
    assert len(out["climbs"]) == 1
    c = out["climbs"][0]
    assert c["name"] == "unnamed" and c["minutes"] > 0 and c["avg_pct"] > 4
    assert out["needs"] == []                                 # no checkpoints on a bare GPX
    assert out["power_needed_w"] is None
    assert out["climb_power_w"] == 200                        # CLIMB_IF 0.80 x 250
    assert "Never predict a finish time" in out["rule"]
    assert "saved" not in out


def test_save_then_found_by_race_name(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_tools, "BASE", tmp_path)
    monkeypatch.setattr(course, "BASE", tmp_path)
    f = tmp_path / "route.gpx"
    f.write_text(gpx_text([(2, 0.0), (3, 0.06)]))
    out = plan_tools.cmd_course(ns(gpx=str(f), name="Little Hill 100", save=True, watts=250, kg=80))
    saved = tmp_path / "athletes" / "tester" / "courses" / "little-hill-100.json"
    assert out["saved"] == str(saved) and saved.exists()
    found = course.for_race("The Little Hill 100 sportive", "tester")
    assert found and found["name"] == "Little Hill 100"
    again = plan_tools.cmd_course(ns(race="Little Hill 100", watts=250, kg=80))
    assert again["climbs"] == out["climbs"]


def test_unknown_race_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(course, "BASE", tmp_path)
    monkeypatch.setattr(course, "RACES_DIR", tmp_path / "none")
    with pytest.raises(SystemExit) as e:
        plan_tools.cmd_course(ns(race="Nowhere Nonsense 999"))
    assert "no course for" in str(e.value)


def test_needs_exactly_one_of_race_or_gpx():
    with pytest.raises(SystemExit):
        plan_tools.cmd_course(ns())
    with pytest.raises(SystemExit):
        plan_tools.cmd_course(ns(race="Mallorca 312", gpx="x.gpx"))


def test_mallorca_312_needs():
    out = plan_tools.cmd_course(ns(race="Mallorca 312", watts=211, kg=93))
    assert out["course"]["ascent_m"] == 5050                  # the official figure, not the GPX's
    assert out["needs"][0]["km"] == 97
    assert out["power_needed_w"] > 200
    assert out["climbs"] and all("minutes" in c for c in out["climbs"])
    assert "30 km/h" in out["assumptions"]


def test_no_rider_numbers_still_returns_the_course():
    out = plan_tools.cmd_course(ns(race="Mallorca 312"))
    assert out["climbs"] and "minutes" not in out["climbs"][0]
    assert "needs" not in out and "power_needed_w" not in out
    assert "--watts" in out["note"]


# ── race facts: --start / --cutoff / --on-miss / --finish / --feed / --year ───────────────

LONG_LEGS = [(25, 0.0), (5, 0.06), (35, 0.0)]          # about 65 km with a climb at km 25-30


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Athlete folder and reference races both inside tmp_path."""
    monkeypatch.setattr(plan_tools, "BASE", tmp_path)
    monkeypatch.setattr(course, "BASE", tmp_path)
    monkeypatch.setattr(course, "RACES_DIR", tmp_path / "reference-races")
    return tmp_path


def gpx_file(tmp_path, name="long-one.gpx"):
    f = tmp_path / name
    f.write_text(gpx_text(LONG_LEGS))
    return str(f)


def courses_dir(tmp_path):
    return tmp_path / "athletes" / "tester" / "courses"


def saved_json(out):
    return course.load(out["saved"])


def test_gpx_save_with_race_facts(store):
    out = plan_tools.cmd_course(ns(
        gpx=gpx_file(store), name="Long One", save=True, watts=250, kg=80, start="07:00",
        cutoff=["30@09:00", "60@11:00"], finish="15:00", feed=["20:Village"], year="2027"))
    c = saved_json(out)
    assert c["start"] == "07:00" and c["year"] == "2027"
    assert [(cp["km"], cp["clock"], cp["type"]) for cp in c["checkpoints"]] == [
        (30, "09:00", "cutoff"), (60, "11:00", "cutoff"), (c["distance_km"], "15:00", "finish")]
    assert c["feeds"] == [{"km": 20, "label": "Village"}]
    assert len(out["needs"]) == 3 and out["needs"][-1]["clock"] == "15:00"
    assert out["power_needed_w"] is not None
    assert out["facts_updated"] == ["start", "year", "cutoffs", "finish", "feeds"]
    assert "warning" not in out and course.for_race("Long One", "tester")["start"] == "07:00"


def test_gpx_facts_without_save_warn(store):
    out = plan_tools.cmd_course(ns(gpx=gpx_file(store), watts=250, kg=80, start="07:00", finish="15:00"))
    assert "saved" not in out and "--save" in out["warning"]
    assert len(out["needs"]) == 1                          # still worked out for this answer


def test_cutoffs_sorted_and_finish_last(store):
    out = plan_tools.cmd_course(ns(gpx=gpx_file(store), save=True, start="07:00",
                                   cutoff=["60@11:00", "30@09:00"], finish="15:00",
                                   feed=["40:Late feed", "10:Early: with a colon"]))
    c = saved_json(out)
    assert [cp["km"] for cp in c["checkpoints"]] == [30, 60, c["distance_km"]]
    assert c["feeds"] == [{"km": 10, "label": "Early: with a colon"}, {"km": 40, "label": "Late feed"}]


def test_race_facts_replace_cutoffs_and_keep_finish_on_own_course(store):
    first = plan_tools.cmd_course(ns(gpx=gpx_file(store), name="Long One", save=True, start="07:00",
                                     cutoff=["30@09:00", "60@11:00"], finish="15:00", feed=["20:Village"]))
    out = plan_tools.cmd_course(ns(race="Long One", watts=250, kg=80, cutoff=["45@10:00"]))
    assert out["saved"] == first["saved"]                  # updated in place, no second file
    assert len(list(courses_dir(store).glob("*.json"))) == 1
    c = saved_json(out)
    assert [(cp["km"], cp["type"]) for cp in c["checkpoints"]] == [(45, "cutoff"), (c["distance_km"], "finish")]
    assert c["checkpoints"][-1]["clock"] == "15:00"        # finish kept
    assert c["start"] == "07:00" and c["feeds"] == [{"km": 20, "label": "Village"}]
    assert out["facts_updated"] == ["cutoffs"] and len(out["needs"]) == 2
    out2 = plan_tools.cmd_course(ns(race="Long One", feed=["15:A", "50:B"], finish="16:30", start="06:30"))
    c2 = saved_json(out2)
    assert [f["km"] for f in c2["feeds"]] == [15, 50]
    assert c2["checkpoints"][-1]["clock"] == "16:30" and c2["start"] == "06:30"
    assert [cp["clock"] for cp in c2["checkpoints"][:-1]] == ["10:00"]


def test_race_facts_on_reference_course_write_an_athlete_copy(store):
    ref_dir = store / "reference-races"
    ref_dir.mkdir()
    ref = course.from_gpx(gpx_text(LONG_LEGS), "Ref Race 100", start="06:30",
                          checkpoints=[{"km": 30, "clock": "09:00", "type": "cutoff"},
                                       {"km": 64, "clock": "14:00", "type": "finish", "label": "Finish"}])
    ref_file = ref_dir / "ref-race-100.json"
    ref_file.write_text(json.dumps(ref, indent=1))
    before = ref_file.read_bytes()

    out = plan_tools.cmd_course(ns(race="The Ref Race 100", watts=250, kg=80, start="07:00",
                                   cutoff=["30@09:30"], feed=["20:Village"], year="2027"))
    copy = courses_dir(store) / "ref-race-100.json"
    assert out["saved"] == str(copy) and copy.exists()
    assert ref_file.read_bytes() == before                  # shared file untouched
    c = course.load(copy)
    assert c["start"] == "07:00" and c["year"] == "2027" and c["feeds"] == [{"km": 20, "label": "Village"}]
    assert [(cp["km"], cp["clock"]) for cp in c["checkpoints"]] == [(30, "09:30"), (64, "14:00")]     # finish kept as it was
    assert c["ele"] == ref["ele"] and len(out["needs"]) == 2
    # the copy now wins, and a second update goes to it rather than making another
    out2 = plan_tools.cmd_course(ns(race="Ref Race 100", year="2028"))
    assert out2["saved"] == str(copy) and len(list(courses_dir(store).glob("*.json"))) == 1
    assert ref_file.read_bytes() == before


@pytest.mark.parametrize("flags, words", [
    (dict(start="25:00"), "not a time"),
    (dict(start="7pm"), "not a time"),
    (dict(start="07:00", finish="12:60"), "not a time"),
    (dict(start="07:00", cutoff=["30@9am"]), "not a time"),
    (dict(start="07:00", cutoff=["30@06:00"]), "later than"),
    (dict(start="07:00", cutoff=["30@07:00"]), "later than"),
    (dict(start="07:00", cutoff=["30@10:00", "60@09:00"]), "later than"),
    (dict(start="07:00", cutoff=["30@09:00"], finish="08:00"), "later than"),
    (dict(start="07:00", cutoff=["999@10:00"]), "beyond"),
    (dict(start="07:00", cutoff=["0@10:00"]), "more than 0"),
    (dict(start="07:00", cutoff=["-5@10:00"]), "more than 0"),
    (dict(start="07:00", cutoff=["30@09:00", "30@10:00"]), "same km"),
    (dict(start="07:00", cutoff=["thirty@10:00"]), "not a distance"),
    (dict(start="07:00", cutoff=["30-10:00"]), "KM@HH:MM"),
    (dict(cutoff=["30@10:00"]), "start time"),
    (dict(feed=["999:Nowhere"]), "beyond"),
    (dict(feed=["20"]), "KM:LABEL"),
    (dict(feed=["20:"]), "KM:LABEL"),
    (dict(start="07:00", on_miss="out"), "--cutoff"),
])
def test_bad_race_facts_exit_and_save_nothing(store, flags, words):
    with pytest.raises(SystemExit) as e:
        plan_tools.cmd_course(ns(gpx=gpx_file(store), save=True, **flags))
    assert words in str(e.value)
    assert not courses_dir(store).exists()


def test_bad_facts_leave_an_existing_course_alone(store):
    first = plan_tools.cmd_course(ns(gpx=gpx_file(store), name="Long One", save=True, start="07:00",
                                     cutoff=["30@09:00"], finish="15:00"))
    before = Path(first["saved"]).read_bytes()
    for flags in (dict(start="09:30"),                      # now later than the saved 09:00 cut-off
                  dict(finish="08:00"),
                  dict(cutoff=["30@06:00"])):
        with pytest.raises(SystemExit):
            plan_tools.cmd_course(ns(race="Long One", **flags))
    assert Path(first["saved"]).read_bytes() == before


def test_on_miss_lands_on_intermediate_cutoffs_only(store):
    out = plan_tools.cmd_course(ns(gpx=gpx_file(store), save=True, watts=250, kg=80, start="07:00",
                                   cutoff=["30@09:00", "60@11:00"], finish="15:00", on_miss="shorter route"))
    cps = saved_json(out)["checkpoints"]
    assert [cp.get("on_miss") for cp in cps] == ["shorter route", "shorter route", None]
    assert [n["on_miss"] for n in out["needs"]] == ["shorter route", "shorter route", None]


def test_no_fact_flags_changes_nothing(store):
    out = plan_tools.cmd_course(ns(gpx=gpx_file(store), name="Plain", save=True))
    assert "facts_updated" not in out and "warning" not in out
    c = saved_json(out)
    assert "start" not in c and "checkpoints" not in c and "feeds" not in c
    again = plan_tools.cmd_course(ns(race="Plain"))
    assert "facts_updated" not in again and "saved" not in again
