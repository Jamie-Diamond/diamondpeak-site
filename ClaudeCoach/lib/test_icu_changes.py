"""Tests for lib/icu_changes.py: webhooks instead of polling. Run: python3 -m pytest ClaudeCoach/lib/test_icu_changes.py"""
import time

import pytest

import icu_changes as ic

NOON = time.mktime(time.strptime("2026-10-15 12:00", "%Y-%m-%d %H:%M"))


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    (tmp_path / "athletes" / "jamie").mkdir(parents=True)
    (tmp_path / "athletes" / "kathryn").mkdir(parents=True)
    (tmp_path / "athletes" / "jamie" / "icu_tokens.json").write_text('{"access_token": "t"}')
    monkeypatch.setattr(ic, "BASE", tmp_path)
    monkeypatch.delenv("CC_ICU_WEBHOOK_GATE", raising=False)
    return tmp_path


def test_an_athlete_on_an_api_key_is_always_polled(cc):
    ic.record("kathryn", "WELLNESS_UPDATED", now=NOON)
    ic.mark_run("kathryn", "refresh", NOON)
    assert ic.due("kathryn", "refresh", 7200, now=NOON + 60)        # no token: no webhooks


def test_a_linked_athlete_is_skipped_until_something_changes(cc):
    assert ic.due("jamie", "refresh", 7200, now=NOON)                # no webhook yet: poll
    ic.record("jamie", "TEST", now=NOON - 60)                         # the pipe works...
    assert not ic.changed_since("jamie", 0)                           # ...but nothing changed
    assert ic.due("jamie", "refresh", 7200, now=NOON)                # never run: run
    ic.mark_run("jamie", "refresh", NOON)
    assert not ic.due("jamie", "refresh", 7200, now=NOON + 600)      # nothing new: skip
    ic.record("jamie", "WELLNESS_UPDATED", now=NOON + 700)
    assert ic.due("jamie", "refresh", 7200, now=NOON + 900)          # changed since: run


def test_the_watcher_only_wakes_for_activities(cc):
    ic.record("jamie", "WELLNESS_UPDATED", now=NOON - 10)
    ic.mark_run("jamie", "watcher", NOON)
    ic.record("jamie", "WELLNESS_UPDATED", now=NOON + 60)
    assert not ic.due("jamie", "watcher", 1800, ic.ACTIVITY_TYPES, now=NOON + 120)
    ic.record("jamie", "ACTIVITY_UPLOADED", now=NOON + 180)
    assert ic.due("jamie", "watcher", 1800, ic.ACTIVITY_TYPES, now=NOON + 240)


def test_a_change_during_a_run_is_picked_up_next_time(cc):
    ic.record("jamie", "WELLNESS_UPDATED", now=NOON - 10)
    ic.mark_run("jamie", "refresh", NOON)                             # run STARTED at noon
    ic.record("jamie", "CALENDAR_UPDATED", now=NOON + 5)              # arrived mid-run
    assert ic.due("jamie", "refresh", 7200, now=NOON + 900)


def test_safety_nets_interval_new_day_quiet_webhooks_and_the_switch(cc, monkeypatch):
    ic.record("jamie", "WELLNESS_UPDATED", now=NOON - 10)
    ic.mark_run("jamie", "refresh", NOON)
    assert ic.due("jamie", "refresh", 7200, now=NOON + 7200)          # interval passed
    late = time.mktime(time.strptime("2026-10-15 23:50", "%Y-%m-%d %H:%M"))
    ic.record("jamie", "WELLNESS_UPDATED", now=late - 60)
    ic.mark_run("jamie", "refresh", late)
    assert ic.due("jamie", "refresh", 7200, now=late + 20 * 60)       # past midnight
    ic.mark_run("jamie", "refresh", NOON + 3600)
    assert ic.due("jamie", "refresh", 7200, now=late - 60 + ic.SILENT_AFTER + 1)   # webhooks went quiet
    ic.mark_run("jamie", "refresh", late - 30)
    assert not ic.due("jamie", "refresh", 7200, now=late - 20)
    monkeypatch.setenv("CC_ICU_WEBHOOK_GATE", "0")
    assert ic.due("jamie", "refresh", 7200, now=late - 20)
