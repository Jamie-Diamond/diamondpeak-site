"""Tests for lib/duplicates.py. Run: python3 -m pytest ClaudeCoach/lib/test_duplicates.py"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import duplicates as d


def act(aid, start, secs=3000, typ="VirtualRide", source="ZWIFT", streams=("time", "watts"),
        device=None):
    return {"id": aid, "start_date_local": start, "start_date": start + "Z", "elapsed_time": secs,
            "type": typ, "source": source, "stream_types": list(streams), "device_name": device}


ZWIFT = act("i10", "2026-10-06T07:00:00", streams=("time", "watts", "cadence"))
WATCH = act("i11", "2026-10-06T07:00:40", 2950, "Ride", "GARMIN_CONNECT", ("time", "heartrate"),
            "Garmin fenix 6")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "BASE", tmp_path)
    (tmp_path / "athletes" / "fred").mkdir(parents=True)
    return tmp_path / "athletes" / "fred"


def test_pairs_finds_zwift_and_watch_copy():
    assert d.pairs([ZWIFT, WATCH]) == [(ZWIFT, WATCH)]


def test_back_to_back_runs_are_not_a_pair():
    a = act("i1", "2026-10-06T07:00:00", 3600, "Run", "GARMIN_CONNECT")
    b = act("i2", "2026-10-06T08:00:00", 1800, "Run", "GARMIN_CONNECT")
    assert d.pairs([a, b]) == []


def test_strava_stub_is_skipped():
    stub = {**WATCH, "source": "STRAVA"}
    assert d.pairs([ZWIFT, stub]) == []


def test_power_only_and_hr_only_merge_into_the_power_copy():
    p = d.plan(ZWIFT, WATCH)
    assert p["kind"] == "merge" and p["src"] is WATCH and p["dst"] is ZWIFT


def test_true_duplicate_keeps_the_copy_with_more_data():
    full = {**ZWIFT, "stream_types": ["time", "watts", "heartrate"]}
    p = d.plan(full, WATCH)
    assert p["kind"] == "delete" and p["drop"] is WATCH and p["keep"] is full


def test_merge_question_has_one_merge_button_and_keep_both():
    q = d.question("fred", ZWIFT, WATCH)
    rows = q["keyboard"]["inline_keyboard"]
    assert "twice" in q["text"] and "Merge" in q["text"]
    assert [r[0]["callback_data"] for r in rows] == ["dup:merge:fred:i11:i10", "dup:keep:fred:i10:i11"]


def test_delete_question_has_a_button_per_copy_and_short_callbacks():
    full = {**ZWIFT, "stream_types": ["time", "watts", "heartrate"]}
    q = d.question("kathryn", full, WATCH)
    cbs = [r[0]["callback_data"] for r in q["keyboard"]["inline_keyboard"]]
    assert cbs[:2] == ["dup:del:kathryn:i10:i11", "dup:del:kathryn:i11:i10"]
    assert "I'd delete copy 2 (Garmin fenix 6)" in q["text"]
    assert all(len(c.encode()) <= 64 for c in cbs)          # Telegram's callback limit


def test_each_pair_is_asked_about_once(home):
    assert len(d.new_questions("fred", [ZWIFT, WATCH])) == 1
    assert d.new_questions("fred", [ZWIFT, WATCH]) == []


def test_hr_lands_on_the_power_copy_by_wall_clock_time():
    t0 = datetime(2026, 10, 6, 7, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 6, 7, 0, 10, tzinfo=timezone.utc)      # watch started 10 s later
    hr = d.hr_onto(list(range(30)), t0, list(range(30)), [100 + i for i in range(30)], t1)
    assert hr[:4] == [None] * 4          # more than 5 s before the watch's first sample
    assert hr[10] == 100 and hr[15] == 105
    assert hr[29] == 119


class FakeClient:
    def __init__(self, acts, streams):
        self.acts, self.streams, self.deleted, self.put = acts, streams, [], None

    def get_activity_detail(self, aid):
        if aid not in self.acts:
            raise Exception("404 Client Error: Not Found")
        a = dict(self.acts[aid])
        if aid == "i10" and self.put:
            a["has_heartrate"] = True
        return a

    def get_activity_streams(self, aid):
        return self.streams[aid]

    def put_activity_streams(self, aid, streams):
        self.put = (aid, streams)
        return {"updated": ["heartrate"]}

    def delete_activity(self, aid):
        self.deleted.append(aid)


def _client():
    n = 60
    # The watch started 2 s after Zwift (the real one is 40 s, but these streams are 60 s long).
    return FakeClient({"i10": ZWIFT, "i11": {**WATCH, "start_date": "2026-10-06T07:00:02Z"}}, {
        "i10": [{"type": "time", "data": list(range(n))}, {"type": "watts", "data": [200] * n}],
        "i11": [{"type": "time", "data": list(range(n))}, {"type": "heartrate", "data": [140] * n}]})


def test_merge_writes_hr_then_deletes_the_watch_copy_and_relinks_the_log(home):
    (home / "session-log.json").write_text(json.dumps([{"activity_id": "i11", "date": "2026-10-06"}]))
    c = _client()
    reply = d.apply("fred", "merge", "i11", "i10", c)
    assert c.put[0] == "i10" and c.put[1][0]["type"] == "heartrate"
    assert c.deleted == ["i11"]
    assert "with both power and heart rate" in reply
    assert json.loads((home / "session-log.json").read_text())[0]["activity_id"] == "i10"


def test_merge_refused_when_the_recordings_do_not_line_up(home):
    c = _client()
    c.acts["i11"] = {**WATCH, "start_date": "2026-10-06T09:00:00Z"}     # two hours apart
    reply = d.apply("fred", "merge", "i11", "i10", c)
    assert c.put is None and c.deleted == [] and "haven't changed anything" in reply


def test_delete_drops_the_extra_log_entry_when_both_were_logged(home):
    (home / "session-log.json").write_text(json.dumps(
        [{"activity_id": "i10"}, {"activity_id": "i11"}]))
    c = _client()
    d.apply("fred", "del", "i11", "i10", c)
    assert c.deleted == ["i11"]
    assert json.loads((home / "session-log.json").read_text()) == [{"activity_id": "i10"}]


def test_copy_already_gone(home):
    c = _client()
    del c.acts["i11"]
    assert "already gone" in d.apply("fred", "del", "i11", "i10", c)
    assert c.deleted == []


def test_keep_both_changes_nothing(home):
    c = _client()
    assert "leave both" in d.apply("fred", "keep", "i10", "i11", c)
    assert c.deleted == [] and c.put is None
