"""Tests for lib/strength_log.py: what Peak's strength player saves, and how a session
gets into the watch's Intervals.icu activity (or becomes a manual one)."""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strength_log as sl  # noqa: E402

REC = {"id": "s-0001", "date": "2026-10-19", "title": "Hamstrings", "physio": True,
       "started_at": "2026-10-19T07:05:00", "ended_at": "2026-10-19T07:50:00",
       "entries": [{"ex": "single_leg_hamstring_curl", "name": "Single-leg hamstring curl",
                    "log": "weight_reps", "effort": "right",
                    "sets": [{"side": "L", "kg": 27.5, "val": 10}, {"side": "R", "kg": 32.5, "val": 10},
                             {"side": "L", "kg": 27.5, "val": 10}, {"side": "R", "kg": 32.5, "val": 9}]},
                   {"ex": "raised_single_leg_hamstring_bridge", "name": "Raised bridge", "log": "reps",
                    "sets": [{"side": "L", "kg": None, "val": 15}]}]}


class FakeIcu:
    def __init__(self, acts):
        self.acts, self.edits, self.made = acts, [], []

    def get_training_history(self, days=30, sport=None):
        return self.acts

    def edit_activity(self, aid, **fields):
        self.edits.append((aid, fields))
        return {}

    def create_manual_activity(self, **kw):
        self.made.append(kw)
        return {"id": "i999"}


def test_clean_keeps_only_typed_fields():
    out = sl.clean(dict(REC, junk="x", entries=REC["entries"] + [{"ex": "BAD NAME", "sets": [{"kg": 1}]}]))
    assert "junk" not in out and len(out["entries"]) == 2
    assert out["entries"][0]["sets"][1] == {"side": "R", "kg": 32.5, "val": 10}
    for bad in ({"id": "x"}, dict(REC, date="soon"), dict(REC, entries=[])):
        try:
            sl.clean(bad)
            raise AssertionError("accepted %r" % bad)
        except ValueError:
            pass


def test_save_replaces_the_same_session(tmp_path):
    sl.save("fred", REC, base=tmp_path)
    sl.save("fred", dict(REC, title="Hamstrings (physio)"), base=tmp_path)
    rows = sl.load("fred", base=tmp_path)
    assert len(rows) == 1 and rows[0]["title"] == "Hamstrings (physio)"


def test_describe_shows_each_side():
    d = sl.describe(sl.clean(REC))
    assert d.splitlines()[0] == "Logged in Peak: Hamstrings"
    assert "Single-leg hamstring curl: L 27.5 kg × 10, 10 · R 32.5 kg × 10, 9 (about right)" in d
    assert "Raised bridge: L 15" in d


def test_merges_into_the_watch_activity_and_keeps_its_text(tmp_path):
    sl.save("fred", REC, base=tmp_path)
    garmin = {"id": "i1", "type": "WeightTraining", "name": "Strength", "start_date_local": "2026-10-19T07:08:01",
              "elapsed_time": 2900, "source": "GARMIN_CONNECT", "description": "Felt good"}
    other_day = dict(garmin, id="i2", start_date_local="2026-10-18T07:08:01")
    icu = FakeIcu([other_day, garmin])
    out = sl.reconcile("fred", icu, now=datetime(2026, 10, 19, 8, 0), base=tmp_path)
    assert out == ["merged s-0001 into i1"]
    aid, fields = icu.edits[0]
    assert aid == "i1" and fields["name"] == "Hamstrings"
    assert fields["description"].startswith("Felt good\n\nLogged in Peak: Hamstrings")
    assert sl.load("fred", base=tmp_path)[0]["icu"]["mode"] == "merged"
    assert sl.reconcile("fred", icu, base=tmp_path) == []          # once only
    # a re-merge replaces Peak's block, not the athlete's text
    again = sl.merged_description(fields["description"], sl.clean(REC))
    assert again.count("Logged in Peak") == 1 and again.startswith("Felt good")


def test_a_named_activity_keeps_its_name(tmp_path):
    sl.save("fred", REC, base=tmp_path)
    icu = FakeIcu([{"id": "i1", "type": "WeightTraining", "name": "Physio legs",
                    "start_date_local": "2026-10-19T07:00:00", "elapsed_time": 3000}])
    sl.reconcile("fred", icu, now=datetime(2026, 10, 19, 8, 0), base=tmp_path)
    assert "name" not in icu.edits[0][1]


def test_no_watch_activity_becomes_manual_after_the_grace_period(tmp_path):
    sl.save("fred", REC, base=tmp_path)
    icu = FakeIcu([{"id": "i5", "type": "Ride", "start_date_local": "2026-10-19T07:00:00"}])
    assert sl.reconcile("fred", icu, now=datetime(2026, 10, 19, 9, 0), base=tmp_path) == []
    out = sl.reconcile("fred", icu, now=datetime(2026, 10, 19, 11, 0), base=tmp_path)
    assert out == ["logged s-0001 as manual activity i999"]
    made = icu.made[0]
    assert made["sport"] == "WeightTraining" and made["start_date_local"] == "2026-10-19T07:05:00"
    assert made["moving_time_s"] == 45 * 60 and made["training_load"] > 0
    assert made["description"].startswith("Logged in Peak")


def test_physio_sessions_come_first_and_match_planned_events(tmp_path):
    (tmp_path / "athletes" / "fred").mkdir(parents=True)
    (tmp_path / "athletes" / "fred" / "profile.json").write_text(json.dumps({"injuries": [{
        "description": "Knee surgery rehab", "status": "active", "set_by": "physio",
        "sessions": [{"title": "Hamstrings", "match": ["hamstring"], "items": [{"ex": "hip_thrust", "sets": 4, "reps": "8", "kg": 75}]},
                     {"title": "Quads", "match": ["quad"], "items": [{"ex": "back_squat", "sets": 4, "reps": "8", "kg": 80}]}]}]}))
    lib = {"example_sessions": [{"id": "tri_a", "title": "Tri A", "parts": []}]}
    sess = sl.sessions("fred", lib, base=tmp_path)
    assert [s["id"] for s in sess] == ["is:0:0", "is:0:1", "ss:tri_a"] and sess[0]["physio"]
    assert sl.for_event("Strength: quads", sess)["title"] == "Quads"
    assert sl.for_event("Strength A (Tier C)", sess) is None
