"""Tests for scripts/strava-to-icu.py. Run: python3 -m pytest ClaudeCoach/scripts/test_strava_to_icu.py"""
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("s2i", Path(__file__).parent / "strava-to-icu.py")
s2i = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s2i)

NS = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
      "x": "http://www.garmin.com/xmlschemas/ActivityExtension/v2"}


def run_detail(sid=1, start="2026-09-20T06:00:00Z", sport="Run", laps=True):
    d = {"id": sid, "name": f"Run {sid}", "sport_type": sport, "start_date": start,
         "start_date_local": start.replace("06:", "07:"), "elapsed_time": 6, "distance": 30,
         "moving_time": 6}
    if laps:
        d["laps"] = [{"start_index": 0, "end_index": 3, "elapsed_time": 3, "distance": 15},
                     {"start_index": 3, "end_index": 5, "elapsed_time": 3, "distance": 15}]
    return d


STREAMS = {"time": {"data": [0, 1, 2, 3, 4, 5]},
           "latlng": {"data": [[50.9, -1.4]] * 6},
           "heartrate": {"data": [120, 121, 122, 130, 131, 132]},
           "cadence": {"data": [85] * 6},
           "distance": {"data": [0, 5, 10, 15, 20, 30]},
           "velocity_smooth": {"data": [5.0] * 6}}


def test_tcx_has_a_lap_per_strava_lap_and_every_sample():
    root = ET.fromstring(s2i.build_tcx(run_detail(), STREAMS))
    act = root.find(".//t:Activity", NS)
    assert act.get("Sport") == "Running"
    laps = act.findall("t:Lap", NS)
    assert [len(lap.findall(".//t:Trackpoint", NS)) for lap in laps] == [3, 3]
    first = laps[0].find(".//t:Trackpoint", NS)
    assert first.find("t:Time", NS).text == "2026-09-20T06:00:00Z"
    assert first.find(".//t:HeartRateBpm/t:Value", NS).text == "120"
    assert first.find(".//x:RunCadence", NS).text == "85"
    assert laps[1].get("StartTime") == "2026-09-20T06:00:03Z"


def test_tcx_without_laps_or_gps_is_one_lap():
    streams = {"time": {"data": [0, 10]}, "heartrate": {"data": [100, 110]}}
    root = ET.fromstring(s2i.build_tcx(run_detail(sport="Swim", laps=False), streams))
    assert root.find(".//t:Activity", NS).get("Sport") == "Other"
    assert len(root.findall(".//t:Lap", NS)) == 1 and root.find(".//t:Position", NS) is None


def test_matches_by_copy_or_by_start_time():
    x = {"id": 7, "start_date": "2026-09-20T06:00:00Z", "start_date_local": "2026-09-20T07:00:00Z"}
    assert s2i._matches(x, [{"external_id": "strava-7", "start_date_local": "2020-01-01T00:00:00"}])
    assert s2i._matches(x, [{"start_date_local": "2026-09-20T07:01:30"}])
    assert not s2i._matches(x, [{"start_date_local": "2026-09-20T07:03:00"}])


class FakeStrava:
    lists = {}
    calls = []

    def __init__(self, slug):
        pass

    def get(self, path, params=None, missing_ok=False):
        FakeStrava.calls.append((path, dict(params or {})))
        if path == "/athlete/activities":
            key = "after" if "after" in params else "before"
            return FakeStrava.lists.get(key, [])
        sid = int(path.split("/")[2])
        if path.endswith("/streams"):
            return STREAMS
        return run_detail(sid, start=f"2026-09-{sid:02d}T06:00:00Z")


class FakeIcu:
    pulls_strava = False
    shown = []
    uploads = []

    def __init__(self, *a):
        pass

    def profile(self):
        return {"strava_sync_activities": FakeIcu.pulls_strava}

    def activities(self, lo, hi):
        return FakeIcu.shown

    def upload(self, name, tcx, detail):
        FakeIcu.uploads.append(detail["id"])
        return f"i{detail['id']}"

    def manual(self, detail):
        return "m"

    def set_type(self, icu_id, detail):
        pass


@pytest.fixture()
def world(tmp_path, monkeypatch):
    adir = tmp_path / "athletes" / "robin"
    adir.mkdir(parents=True)
    (adir / "strava_tokens.json").write_text("{}")
    monkeypatch.setattr(s2i, "CC", tmp_path)
    monkeypatch.setattr(s2i, "Strava", FakeStrava)
    monkeypatch.setattr(s2i, "Icu", FakeIcu)
    sent = []
    monkeypatch.setattr(s2i.outbox, "record", lambda cid, text, **k: sent.append((cid, text)))
    FakeStrava.lists, FakeStrava.calls = {}, []
    FakeIcu.pulls_strava, FakeIcu.shown, FakeIcu.uploads = False, [], []
    return adir, sent


def summary(sid):
    return {"id": sid, "name": f"Run {sid}", "start_date": f"2026-09-{sid:02d}T06:00:00Z",
            "start_date_local": f"2026-09-{sid:02d}T07:00:00Z"}


A = {"icu_athlete_id": "i9", "icu_api_key": "k", "chat_id": "web-abc123"}


def test_copies_new_then_history_and_skips_what_is_already_there(world):
    adir, _ = world
    FakeStrava.lists = {"after": [summary(25)], "before": [summary(20), summary(12)]}
    FakeIcu.shown = [{"start_date_local": "2026-09-12T07:00:30"}]      # from a watch already
    s2i.run_athlete("robin", A)
    st = json.loads((adir / "strava-bridge.json").read_text())
    assert FakeIcu.uploads == [25, 20]
    assert st["copied"] == {"25": "i25", "20": "i20", "12": "already there"}
    assert st["oldest"] == int(s2i._utc("2026-09-12T06:00:00Z").timestamp())
    assert st["newest"] >= int(s2i._utc("2026-09-25T06:00:00Z").timestamp())

    FakeStrava.lists = {"after": [], "before": []}
    s2i.run_athlete("robin", A)
    assert json.loads((adir / "strava-bridge.json").read_text())["history_done"] is True


def test_waits_and_asks_once_while_intervals_also_pulls_strava(world):
    adir, sent = world
    FakeIcu.pulls_strava = True
    FakeStrava.lists = {"after": [summary(25)]}
    s2i.run_athlete("robin", A)
    s2i.run_athlete("robin", A)
    assert FakeIcu.uploads == [] and FakeStrava.calls == []
    assert len(sent) == 1 and "untick _Download activities_" in sent[0][1]


def test_a_failing_workout_is_retried_then_skipped(world, monkeypatch):
    adir, _ = world
    FakeStrava.lists = {"after": [summary(25)], "before": []}

    def boom(*a):
        raise RuntimeError("bad file")
    monkeypatch.setattr(FakeIcu, "upload", lambda self, *a: boom())
    for _ in range(3):
        s2i.run_athlete("robin", A)
    st = json.loads((adir / "strava-bridge.json").read_text())
    assert st["copied"]["25"].startswith("skipped")
