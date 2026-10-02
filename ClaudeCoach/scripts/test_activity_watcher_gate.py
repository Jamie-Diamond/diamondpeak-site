"""Tests for activity-watcher.py's new-activity gate. Run: python3 -m pytest ClaudeCoach/scripts/test_activity_watcher_gate.py"""
import importlib.util
import json
import subprocess
from pathlib import Path

_spec = importlib.util.spec_from_file_location("aw", Path(__file__).parent / "activity-watcher.py")
aw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aw)


def act(aid, start, secs=3600, typ="Ride", strava=None, ext=None):
    return {"id": aid, "start_date_local": start, "elapsed_time": secs, "type": typ,
            "strava_id": strava, "external_id": ext}


def gate(monkeypatch, acts, logged):
    monkeypatch.setattr(aw.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout=json.dumps(acts), stderr=""))
    return aw._has_new_activity("x", set(logged))


def test_logged_by_icu_id(monkeypatch):
    assert not gate(monkeypatch, [act("i1", "2026-10-01T08:00:00")], {"i1"})


def test_logged_by_strava_id(monkeypatch):
    # 1 Oct 2026: Jamie's run sat in the log as its Strava id.
    assert not gate(monkeypatch, [act("i192236501", "2026-10-01T12:32:13", typ="Run",
                                      strava="20406359196")], {"20406359196"})


def test_unlogged_activity_is_new(monkeypatch):
    acts = [act("i1", "2026-10-01T08:00:00"), act("i2", "2026-10-01T18:00:00")]
    assert gate(monkeypatch, acts, {"i1"})


def test_second_recording_of_logged_ride_is_not_new(monkeypatch):
    # Zwift copy logged; the watch's copy of the same ride started 40 s later.
    acts = [act("i10", "2026-10-06T07:00:00", 3000, "VirtualRide"),
            act("i11", "2026-10-06T07:00:40", 2950, "Ride")]
    assert not gate(monkeypatch, acts, {"i10"})


def test_back_to_back_sessions_are_both_real(monkeypatch):
    # A brick or a double: same sport, touching but not overlapping.
    acts = [act("i20", "2026-10-06T07:00:00", 3600, "Run"),
            act("i21", "2026-10-06T08:00:00", 1800, "Run")]
    assert gate(monkeypatch, acts, {"i20"})


def test_overlapping_other_sport_is_new(monkeypatch):
    acts = [act("i30", "2026-10-06T07:00:00", 3600, "Ride"),
            act("i31", "2026-10-06T07:30:00", 1800, "WeightTraining")]
    assert gate(monkeypatch, acts, {"i30"})


def test_fetch_failure_fails_open(monkeypatch):
    monkeypatch.setattr(aw.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 1, stdout="", stderr="boom"))
    assert aw._has_new_activity("x", set())
