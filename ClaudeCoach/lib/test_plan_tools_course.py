"""Tests for `plan_tools.py course` (lib/course.py through the CLI). Run:
python3 -m pytest ClaudeCoach/lib/test_plan_tools_course.py"""
import argparse
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
