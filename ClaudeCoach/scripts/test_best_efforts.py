"""Offline tests for the run and swim "best efforts" tables (Jamie, 17 Oct 2026).
Run: python3 -m pytest ClaudeCoach/scripts/test_best_efforts.py -q -p no:cacheprovider

refresh-site-data.py cannot be run here (live intervals.icu credentials, VM-only athlete
files), so the pure helpers are tested against synthetic /pace-curves replies in the
shape lib/performance.py already reads: {"list": [{"distance": [m...], "values": [s...],
"activity_id": [...]}], "activities": {id: {...}}}. The real endpoint was NOT checked
from here.
"""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "refresh_site_data", _here / "refresh-site-data.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

RUN_DISTS = [1000.0, 5000.0, 10000.0, 21097.5, 42195.0]
SWIM_DISTS = [100.0, 400.0, 1500.0, 3800.0]


def reply(dists, secs, ids=None, acts=None):
    return {"list": [{"id": "90d", "distance": dists, "values": secs,
                      "activity_id": ids if ids is not None else ["i1"] * len(dists)}],
            "activities": acts if acts is not None else {}}


def row(rows, label):
    return next(r for r in rows if r["label"] == label)


def test_labels_and_every_target_gets_a_row_even_with_no_data():
    run = R._best_efforts_rows("Run", {"list": []}, None)
    assert [r["label"] for r in run] == ["1 km", "5 km", "10 km", "Half", "Marathon"]
    assert [r["dist_m"] for r in run] == [1000, 5000, 10000, 21097.5, 42195]
    assert all(r["s"] is None and r["sPrev"] is None and r["pace_s"] is None
               and r["pacePrev_s"] is None and r["activity"] is None for r in run)
    swim = R._best_efforts_rows("Swim", None, None)
    assert [r["label"] for r in swim] == ["100 m", "400 m", "1500 m", "3.8 km"]
    assert [r["dist_m"] for r in swim] == [100, 400, 1500, 3800]


def test_exact_distance_run_pace_is_seconds_per_km():
    now = reply(RUN_DISTS, [230, 1200, 2520, 5400, 11700])
    rows = R._best_efforts_rows("Run", now, None)
    r5 = row(rows, "5 km")
    assert r5["s"] == 1200 and r5["pace_s"] == 240.0       # 4:00/km
    assert row(rows, "Half")["pace_s"] == round(5400 / 21.0975, 1)
    assert row(rows, "Marathon")["s"] == 11700


def test_exact_distance_swim_pace_is_seconds_per_100m():
    now = reply(SWIM_DISTS, [80, 340, 1380, 3700])
    rows = R._best_efforts_rows("Swim", now, None)
    assert row(rows, "400 m")["pace_s"] == 85.0            # 1:25/100 m
    assert row(rows, "1500 m")["pace_s"] == 92.0
    assert row(rows, "3.8 km")["pace_s"] == round(3700 / 38, 1)


def test_nearest_distance_within_two_percent_is_scaled():
    # Run list has 5010 m, no 5000: 5010 m in 1203 s -> 1203 * 5000/5010 s at 5 km.
    now = reply([1000.0, 5010.0], [230, 1203])
    r5 = row(R._best_efforts_rows("Run", now, None), "5 km")
    assert r5["s"] == round(1203 * 5000 / 5010, 1)
    # Swim: 1490 m in 1400 s stands in for 1500 m.
    sw = row(R._best_efforts_rows("Swim", reply([1490.0], [1400]), None), "1500 m")
    assert sw["s"] == round(1400 * 1500 / 1490, 1)


def test_nearest_picks_the_closest_and_exact_beats_near():
    # 4950 (1%) and 5025 (0.5%): the closer one is used.
    now = reply([4950.0, 5025.0], [1200, 1215])
    assert row(R._best_efforts_rows("Run", now, None), "5 km")["s"] == round(1215 * 5000 / 5025, 1)
    # An exact 5000 wins over a near 5010 even when the near one is listed first.
    now = reply([5010.0, 5000.0], [1100, 1200])
    assert row(R._best_efforts_rows("Run", now, None), "5 km")["s"] == 1200


def test_distance_outside_two_percent_or_missing_leaves_the_row_empty():
    # 5150 m is 3% off: not usable. 1000 m is present, so only that row fills.
    now = reply([1000.0, 5150.0], [230, 1250])
    rows = R._best_efforts_rows("Run", now, None)
    assert row(rows, "5 km")["s"] is None and row(rows, "5 km")["activity"] is None
    assert row(rows, "1 km")["s"] == 230


def test_listed_distance_with_no_time_is_skipped_for_a_usable_neighbour():
    now = reply([5000.0, 5020.0], [None, 1210])
    assert row(R._best_efforts_rows("Run", now, None), "5 km")["s"] == round(1210 * 5000 / 5020, 1)
    zero = reply([5000.0], [0])
    assert row(R._best_efforts_rows("Run", zero, None), "5 km")["s"] is None


def test_short_values_list_does_not_raise():
    now = reply([1000.0, 5000.0], [230])                    # values shorter than distance
    rows = R._best_efforts_rows("Run", now, None)
    assert row(rows, "1 km")["s"] == 230 and row(rows, "5 km")["s"] is None


def test_run_glitch_faster_than_world_record_pace_is_null():
    # 1 km in 2:00 = 2:00/km, under the 2:30/km floor -> glitch, in BOTH columns.
    now = reply(RUN_DISTS, [120, 1200, 2520, 5400, 11700])
    prev = reply(RUN_DISTS, [230, 700, 2600, 5500, 11900])   # 5 km in 11:40 = 2:20/km
    rows = R._best_efforts_rows("Run", now, prev)
    r1, r5 = row(rows, "1 km"), row(rows, "5 km")
    assert r1["s"] is None and r1["pace_s"] is None and r1["activity"] is None
    assert r1["sPrev"] == 230                                # prev 1 km is fine
    assert r5["s"] == 1200 and r5["sPrev"] is None and r5["pacePrev_s"] is None


def test_run_at_exactly_the_floor_is_kept():
    now = reply([1000.0], [150])
    assert row(R._best_efforts_rows("Run", now, None), "1 km")["pace_s"] == 150.0


def test_swim_glitch_under_45s_per_100m_is_null():
    now = reply(SWIM_DISTS, [40, 340, 1380, 3700])           # 100 m in 0:40
    rows = R._best_efforts_rows("Swim", now, None)
    assert row(rows, "100 m")["s"] is None
    assert row(rows, "400 m")["s"] == 340
    ok = reply([100.0], [45])                                # exactly 0:45/100 m kept
    assert row(R._best_efforts_rows("Swim", ok, None), "100 m")["pace_s"] == 45.0


def test_glitch_floor_applies_to_scaled_times_too():
    # 5010 m in 700 s scales to ~698 s at 5 km: 2:19/km, a glitch.
    now = reply([5010.0], [700])
    assert row(R._best_efforts_rows("Run", now, None), "5 km")["s"] is None


def test_previous_year_column_is_independent_of_the_current_one():
    now = reply(RUN_DISTS, [230, 1200, 2520, 5400, 11700])
    prev = reply(RUN_DISTS, [240, 1260, 2640, 5700, 12300])
    rows = R._best_efforts_rows("Run", now, prev)
    r10 = row(rows, "10 km")
    assert (r10["s"], r10["sPrev"]) == (2520, 2640)
    assert (r10["pace_s"], r10["pacePrev_s"]) == (252.0, 264.0)
    # Current missing, year-ago present: the row stays so the UI can say so.
    rows = R._best_efforts_rows("Run", {"list": []}, prev)
    assert row(rows, "10 km")["s"] is None and row(rows, "10 km")["sPrev"] == 2640
    # Year-ago fetch failed (None): current still fills.
    rows = R._best_efforts_rows("Run", now, None)
    assert row(rows, "10 km")["s"] == 2520 and row(rows, "10 km")["sPrev"] is None


def test_activity_comes_from_the_current_reply_activities_dict():
    now = reply(RUN_DISTS, [230, 1200, 2520, 5400, 11700],
                ids=["i1", "i2", "i2", "i3", "i4"],
                acts={"i1": {"start_date_local": "2026-10-01T07:00:00", "name": "Parkrun"},
                      "i2": {"start_date_local": "2026-09-14T06:30:00", "name": "Tempo 10k"}})
    prev = reply(RUN_DISTS, [240] * 5, ids=["p1"] * 5,
                 acts={"p1": {"start_date_local": "2025-09-01T07:00:00", "name": "Last year"}})
    rows = R._best_efforts_rows("Run", now, prev)
    assert row(rows, "1 km")["activity"] == {"id": "i1", "date": "2026-10-01", "name": "Parkrun"}
    assert row(rows, "10 km")["activity"] == {"id": "i2", "date": "2026-09-14", "name": "Tempo 10k"}
    # The id is known but the activities dict has no entry: id kept, date/name None.
    assert row(rows, "Half")["activity"] == {"id": "i3", "date": None, "name": None}
    # The year-ago activity is never put on the row.
    assert "Last year" not in str(rows)


def test_activity_id_that_is_a_number_matches_a_string_key():
    now = reply([1000.0], [230], ids=[12345],
                acts={"12345": {"start_date_local": "2026-10-02T07:00:00", "name": "Run"}})
    a = row(R._best_efforts_rows("Run", now, None), "1 km")["activity"]
    assert a == {"id": "12345", "date": "2026-10-02", "name": "Run"}


def test_no_activity_id_gives_null_activity():
    now = reply([1000.0], [230], ids=[])
    r = row(R._best_efforts_rows("Run", now, None), "1 km")
    assert r["s"] == 230 and r["activity"] is None


def test_malformed_replies_do_not_raise():
    for bad in (None, {}, {"list": None}, {"list": [None]}, {"list": [{}]}, [], "x",
                {"list": [{"distance": "abc", "values": 5}]},
                {"list": [{"distance": [None, "x"], "values": ["a", None]}]}):
        rows = R._best_efforts_rows("Run", bad, bad)
        assert len(rows) == 5 and all(r["s"] is None for r in rows)


class FakeClient:
    def __init__(self, replies=None, fail=()):
        self.calls, self.replies, self.fail = [], replies or {}, fail

    def _get(self, path, params=None):
        self.calls.append((path, dict(params)))
        key = (params["type"], "now" if params["curves"] == "90d" else "prev")
        if key in self.fail:
            raise RuntimeError("boom")
        return self.replies.get(key, {"list": []})


TODAY = date(2026, 10, 17)


def test_block_asks_for_90d_and_the_same_90_days_a_year_earlier():
    c = FakeClient({("Run", "now"): reply(RUN_DISTS, [230, 1200, 2520, 5400, 11700]),
                    ("Run", "prev"): reply(RUN_DISTS, [240, 1260, 2640, 5700, 12300])})
    b = R._best_efforts_block(c, "Run", TODAY)
    assert b["window"] == {"from": (TODAY - timedelta(days=90)).isoformat(), "to": "2026-10-17",
                           "prevFrom": (TODAY - timedelta(days=455)).isoformat(),
                           "prevTo": (TODAY - timedelta(days=365)).isoformat()}
    assert c.calls[0] == ("pace-curves", {"type": "Run", "curves": "90d"})
    assert c.calls[1] == ("pace-curves", {"type": "Run",
                                          "curves": f"r.{b['window']['prevFrom']}.{b['window']['prevTo']}"})
    assert row(b["rows"], "5 km")["s"] == 1200 and row(b["rows"], "5 km")["sPrev"] == 1260


def test_year_ago_failure_keeps_the_current_column():
    c = FakeClient({("Swim", "now"): reply(SWIM_DISTS, [80, 340, 1380, 3700])},
                   fail={("Swim", "prev")})
    b = R._best_efforts_block(c, "Swim", TODAY)
    assert row(b["rows"], "400 m")["s"] == 340 and row(b["rows"], "400 m")["sPrev"] is None


def test_current_failure_gives_none_and_never_raises():
    c = FakeClient(fail={("Run", "now")})
    assert R._best_efforts_block(c, "Run", TODAY) is None
    out = R._best_efforts_blocks(c, "x", TODAY)
    assert out["runBests"] is None and out["swimBests"]["rows"]


def test_blocks_publishes_both_keys_and_survives_a_helper_blowing_up(monkeypatch):
    c = FakeClient()
    out = R._best_efforts_blocks(c, "x", TODAY)
    assert set(out) == {"runBests", "swimBests"}
    assert len(out["runBests"]["rows"]) == 5 and len(out["swimBests"]["rows"]) == 4
    monkeypatch.setattr(R, "_best_efforts_block",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    out = R._best_efforts_blocks(c, "x", TODAY)
    assert out == {"runBests": None, "swimBests": None}


# ── bike power table: _power_curve_block, shared by every athlete's builder ───────

class PowerClient:
    def __init__(self, now=None, prev=None, fail_prev=False, fail_now=False):
        self.calls, self.now, self.prev = [], now, prev
        self.fail_prev, self.fail_now = fail_prev, fail_now

    def get_power_curves(self, sport="Ride", curves="90d"):
        self.calls.append((sport, curves))
        if curves == "90d":
            if self.fail_now:
                raise RuntimeError("boom")
            return self.now if self.now is not None else {"list": []}
        if self.fail_prev:
            raise RuntimeError("boom")
        return self.prev if self.prev is not None else {"list": []}


def pc_reply(secs, watts):
    return {"list": [{"secs": secs, "values": watts}]}


def test_power_block_90d_and_year_ago_rows_for_any_athlete():
    c = PowerClient(now=pc_reply([5, 60, 300, 1200], [900, 500, 340, 280]),
                    prev=pc_reply([5, 60, 300, 1200], [850, 480, 330, 270]))
    rows, win = R._power_curve_block(c, "fred", {}, TODAY)
    assert [t for t, _ in R._POWER_DURATIONS] == [r["t"] for r in rows]   # no race target
    r5m = next(r for r in rows if r["t"] == 300)
    assert (r5m["label"], r5m["w"], r5m["wPrev"]) == ("5m", 340, 330)
    assert next(r for r in rows if r["t"] == 10)["w"] is None             # not in the curve
    p_from, p_to = TODAY - timedelta(days=455), TODAY - timedelta(days=365)
    assert c.calls == [("Ride", "90d"), ("Ride", f"r.{p_from.isoformat()}.{p_to.isoformat()}")]
    assert win["days"] == 90 and win["now_to"] == "2026-10-17"
    assert win["now_from"] == (TODAY - timedelta(days=90)).isoformat()
    assert (win["prev_from"], win["prev_to"]) == (p_from.isoformat(), p_to.isoformat())


def test_power_block_without_np_fn_leaves_np_none():
    c = PowerClient(now=pc_reply([300], [340]), prev=pc_reply([300], [330]))
    rows, win = R._power_curve_block(c, "fred", {}, TODAY)
    assert all(r["np"] is None and r["npPrev"] is None for r in rows)
    assert win["np_basis"] is None and win["np_pending"] is None


def test_power_block_with_np_fn_fills_np_and_is_failure_safe():
    calls = []

    def np_fn(durations, p_from, p_to):
        calls.append((list(durations), p_from, p_to))
        return {300: 350}, {300: 335}, 2

    c = PowerClient(now=pc_reply([300], [340]), prev=pc_reply([300], [330]))
    rows, win = R._power_curve_block(c, "jamie", {}, TODAY, np_fn=np_fn)
    r5m = next(r for r in rows if r["t"] == 300)
    assert (r5m["np"], r5m["npPrev"]) == (350, 335)
    assert win["np_pending"] == 2 and "4th-power" in win["np_basis"]
    assert calls[0][1:] == (TODAY - timedelta(days=455), TODAY - timedelta(days=365))
    # NP blowing up costs the NP columns only, never the average-power table.
    rows, win = R._power_curve_block(
        c, "jamie", {}, TODAY, np_fn=lambda *a: (_ for _ in ()).throw(ValueError("x")))
    r5m = next(r for r in rows if r["t"] == 300)
    assert r5m["w"] == 340 and r5m["np"] is None and win is not None


def test_power_block_missing_year_ago_gives_wprev_none():
    for c in (PowerClient(now=pc_reply([300], [340])),                     # no list
              PowerClient(now=pc_reply([300], [340]), fail_prev=True)):    # fetch fails
        rows, win = R._power_curve_block(c, "fred", {}, TODAY)
        r5m = next(r for r in rows if r["t"] == 300)
        assert r5m["w"] == 340 and r5m["wPrev"] is None
        assert all(r["wPrev"] is None for r in rows) and win["prev_from"]


def test_power_block_no_current_curve_or_failure_gives_empty_never_raises():
    assert R._power_curve_block(PowerClient(), "fred", {}, TODAY) == ([], None)
    assert R._power_curve_block(PowerClient(fail_now=True), "fred", {}, TODAY) == ([], None)


def test_power_block_adds_the_race_duration_row_from_that_athletes_profile():
    prof = {"race_targets": {"bike_time": "5:30:00"}}                      # 19800 s
    c = PowerClient(now=pc_reply([300], [340]))
    rows, _ = R._power_curve_block(c, "fred", prof, TODAY)
    assert rows[-1]["t"] == 19800 and rows[-1]["label"].endswith("race")
