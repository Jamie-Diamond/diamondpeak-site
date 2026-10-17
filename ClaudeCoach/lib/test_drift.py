"""Tests for lib/drift.py (one drift figure everywhere), its use in lib/activity_stats.build,
and the drift cache in lib/performance.py. Run:
python3 -m pytest ClaudeCoach/lib/test_drift.py"""
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import activity_stats as ast_  # noqa: E402
import drift  # noqa: E402
import performance  # noqa: E402

N = 7200                                  # a 2 h session at 1 Hz
TODAY = date(2026, 10, 17)


# ── synthetic sessions ────────────────────────────────────────────────────────
def lin(a, b, n=N):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def streams(**kw):
    """{"watts": [...], "heartrate": [...]} -> the list-of-{"type","data"} shape the API returns."""
    return [{"type": k, "data": v} for k, v in kw.items()]


def ride(extra_np=None):
    return {"type": "Ride", "icu_weighted_avg_watts": extra_np or 205}


def steady_ride(n=N, watts=200.0, hr0=130.0, hr1=140.0):
    return [watts] * n, lin(hr0, hr1, n)


def drift_of(act, watts, hr, **extra):
    return drift.session_drift(act, streams(watts=watts, heartrate=hr, **extra))


def hilly_run(n=N, rise=20.0, wave=2000.0, climb=0.03, gap_ms=3.0, hr=150.0):
    """Constant-effort run: a steady grade-adjusted speed (gap_ms) and constant HR. The first
    half is flat; the second rolls (`rise` metres up and down every `wave` metres) on a gentle
    climb (`climb`, a 3% average gradient). The raw speed drops there, so read raw it looks like
    the athlete slowing down, though the effort never changed."""
    k = 2 * math.pi / wave
    vel, alt, dist, d, d0 = [], [], [], 0.0, None
    for i in range(n):
        if i >= n // 2 and d0 is None:
            d0 = d
        x = d - d0 if d0 is not None else None
        a = climb * x + rise / 2 * (1 - math.cos(k * x)) if x is not None else 0.0
        g = climb + rise / 2 * k * math.sin(k * x) if x is not None else 0.0
        v = gap_ms * 3.6 / ast_._minetti(g)               # the speed that costs the same as flat
        vel.append(v)
        alt.append(a)
        dist.append(d)
        d += v
    return vel, alt, dist, [hr] * n


# ── 1. a steady ride ──────────────────────────────────────────────────────────
def test_steady_ride_reads_a_few_percent_positive_by_power_over_hr():
    w, hr = steady_ride()
    r = drift_of(ride(), w, hr)
    assert r["method"] == "power:HR" and r["reason"] is None
    assert 2.5 < r["pct"] < 4.0                       # HR 130->140 on flat power, by hand ~3.2%
    assert r["pct"] == round(r["pct"], 1)


def test_flat_hr_on_flat_power_is_zero_drift():
    w, hr = steady_ride(hr0=140, hr1=140)
    assert drift_of(ride(), w, hr)["pct"] == 0.0


# ── 2. freewheeling is dropped ────────────────────────────────────────────────
def _with_coasting(w, hr, where):
    w, hr = list(w), list(hr)
    for i in range(N):
        if where(i):
            w[i], hr[i] = 0, hr[i] - 15               # a descent: no power, HR easing
    return w, hr


@pytest.mark.parametrize("name,where", [
    ("spread", lambda i: i % 20 < 3),                 # 15% of the ride, all the way through
    ("late", lambda i: i > N // 2 and i % 10 < 3),    # the descents all in the second half
])
def test_freewheeling_does_not_change_the_figure(name, where):
    w, hr = steady_ride()
    base = drift_of(ride(), w, hr)["pct"]
    cw, chr_ = _with_coasting(w, hr, where)
    got = drift_of(ride(), cw, chr_)
    assert got["method"] == "power:HR"
    assert abs(got["pct"] - base) < 0.3, (name, got, base)    # kept in, the late coasting would read ~+29%


# ── 3. warm-up is cut ─────────────────────────────────────────────────────────
def _with_warmup(w, hr, secs, power=100.0, hr_lo=95.0):
    w, hr = list(w), list(hr)
    w[:secs] = [power] * secs
    hr[:secs] = lin(hr_lo, hr[secs], secs)
    return w, hr


def test_a_low_power_low_hr_warmup_does_not_change_the_figure():
    w, hr = steady_ride()
    base = drift_of(ride(), w, hr)["pct"]
    ww, whr = _with_warmup(w, hr, ast_.WARMUP_S)
    assert drift_of(ride(), ww, whr)["pct"] == pytest.approx(base, abs=1e-9)


def test_intervals_own_warmup_time_replaces_the_default_cut():
    w, hr = steady_ride()
    act = dict(ride(), icu_warmup_time=900)
    ww, whr = _with_warmup(w, hr, 900)
    assert drift_of(act, ww, whr)["pct"] == pytest.approx(drift_of(act, w, hr)["pct"], abs=1e-9)
    # a 900 s warm-up with the default 600 s cut would leave 300 s of it in the figure
    assert drift_of(ride(), ww, whr)["pct"] != pytest.approx(drift_of(ride(), w, hr)["pct"], abs=0.1)


# ── 4. intervals give no figure ───────────────────────────────────────────────
def interval_ride():
    w = [320.0 if (i // 240) % 2 == 0 else 120.0 for i in range(N)]
    hr = [150.0 if x > 200 else 125.0 for x in w]
    return w, hr


def test_interval_ride_has_no_figure():
    w, hr = interval_ride()
    r = drift_of(ride(extra_np=270), w, hr)           # pedalling mean 220 -> VI 1.23 > MAX_VI
    assert r["pct"] is None and r["method"] is None
    assert r["reason"].startswith("intervals")


def test_max_vi_is_the_cut_off_for_a_ride():
    w, hr = steady_ride()
    mean = sum(w) / len(w)
    assert drift_of(ride(extra_np=mean * drift.MAX_VI * 0.99), w, hr)["pct"] is not None
    over = drift_of(ride(extra_np=mean * drift.MAX_VI * 1.01), w, hr)
    assert over["pct"] is None and over["reason"].startswith("intervals")


# ── 5. hills on a ride are not drift ──────────────────────────────────────────
def test_hilly_ride_where_hr_follows_power_reads_about_zero():
    w = [200 + 60 * math.sin(2 * math.pi * i / 600) for i in range(N)]    # climbs every 10 min
    hr = [0.65 * x for x in w]
    r = drift_of(ride(extra_np=212), w, hr)
    assert r["method"] == "power:HR" and abs(r["pct"]) < 0.5


# ── 6. a run with no power: grade-adjusted pace ───────────────────────────────
def test_run_without_power_uses_grade_adjusted_pace_so_hills_are_not_drift():
    vel, alt, dist, hr = hilly_run()
    act = {"type": "Run"}
    gap = drift.session_drift(act, streams(velocity_smooth=vel, altitude=alt, distance=dist, heartrate=hr))
    assert gap["method"] == "grade-adjusted pace:HR" and gap["reason"] is None
    assert abs(gap["pct"]) < 1.5
    raw = drift.session_drift(act, streams(velocity_smooth=vel, distance=dist, heartrate=hr))
    assert raw["method"] == "pace:HR"
    assert raw["pct"] > 3 and raw["pct"] > abs(gap["pct"]) + 2    # the hills read as slowing down


def test_fixed_altitude_is_used_in_preference_to_altitude():
    vel, alt, dist, hr = hilly_run()
    junk = [0.0] * len(alt)
    r = drift.session_drift({"type": "Run"}, streams(velocity_smooth=vel, altitude=junk,
                                                      fixed_altitude=alt, distance=dist, heartrate=hr))
    assert r["method"] == "grade-adjusted pace:HR" and abs(r["pct"]) < 1.5


def test_grade_adjusted_speed_is_the_flat_speed_on_the_flat_and_slower_climbs_count_more():
    n = 200
    flat = drift._grade_adjusted([3.0] * n, [10.0] * n, [3.0 * i for i in range(n)])
    assert all(v == pytest.approx(3.0) for v in flat)
    climb = drift._grade_adjusted([3.0] * n, [0.05 * 3.0 * i for i in range(n)], [3.0 * i for i in range(n)])
    assert climb[n // 2] > 3.0                        # 5% up at 3 m/s is more work than 3 m/s flat
    down = drift._grade_adjusted([3.0] * n, [-0.05 * 3.0 * i for i in range(n)], [3.0 * i for i in range(n)])
    assert down[n // 2] < 3.0


# ── 7. a run with power ───────────────────────────────────────────────────────
def power_run(stops=()):
    w, hr = steady_ride(watts=250.0, hr0=140.0, hr1=150.0)
    vel = [3.0] * N
    for s in stops:                                   # 2 minutes at a gate: nothing moving
        for i in range(s, s + 120):
            w[i], vel[i], hr[i] = 0, 0.0, hr[i] - 10
    return w, hr, vel


def test_run_with_power_goes_by_power_over_hr_and_stops_do_not_move_it():
    act = {"type": "Run"}
    w, hr, vel = power_run()
    base = drift.session_drift(act, streams(watts=w, heartrate=hr, velocity_smooth=vel))
    assert base["method"] == "power:HR" and 2.0 < base["pct"] < 4.0
    sw, shr, svel = power_run(stops=(2000, 5000))
    got = drift.session_drift(act, streams(watts=sw, heartrate=shr, velocity_smooth=svel))
    assert got["method"] == "power:HR"
    assert abs(got["pct"] - base["pct"]) < 0.3


def test_run_without_power_drops_stops_by_speed():
    n = N
    vel, hr = [3.0] * n, lin(140, 150, n)
    base = drift.session_drift({"type": "Run"}, streams(velocity_smooth=vel, heartrate=hr))
    sv, shr = list(vel), list(hr)
    for s in (2000, 5000):
        for i in range(s, s + 120):
            sv[i], shr[i] = 0.0, shr[i] - 10
    got = drift.session_drift({"type": "Run"}, streams(velocity_smooth=sv, heartrate=shr))
    assert got["method"] == "pace:HR" and abs(got["pct"] - base["pct"]) < 0.3


@pytest.mark.parametrize("sport", ["VirtualRide", "GravelRide"])
def test_other_ride_types_count_as_rides(sport):
    w, hr = steady_ride()
    assert drift.session_drift({"type": sport, "icu_weighted_avg_watts": 205},
                               streams(watts=w, heartrate=hr))["method"] == "power:HR"


# ── 8. no figure, and why ─────────────────────────────────────────────────────
def test_session_too_short_for_a_figure():
    w, hr = steady_ride(n=1500)                       # 25 min
    r = drift_of(ride(), w, hr)
    assert r["pct"] is None and "30 minutes" in r["reason"]


def test_steady_part_must_reach_min_steady_s_after_the_cuts():
    need = ast_.WARMUP_S + ast_.COOLDOWN_S + ast_.MIN_STEADY_S
    w, hr = steady_ride(n=need)
    assert drift_of(ride(), w, hr)["pct"] is not None
    w, hr = steady_ride(n=need - 1)
    assert "30 minutes" in drift_of(ride(), w, hr)["reason"]


def test_untrusted_heart_rate_gives_no_figure():
    w, hr = steady_ride()
    r = drift.session_drift(ride(), streams(watts=w, heartrate=hr), hr_trusted=False)
    assert r == {"pct": None, "method": None, "reason": "heart rate failed the check"}


def test_no_heart_rate_stream():
    w, _ = steady_ride()
    assert drift.session_drift(ride(), streams(watts=w))["reason"] == "no heart rate"
    assert drift.session_drift(ride(), streams(watts=w, heartrate=[]))["reason"] == "no heart rate"
    assert drift.session_drift(ride(), [])["reason"] == "no heart rate"


def test_ride_without_power():
    _, hr = steady_ride()
    assert drift.session_drift(ride(), streams(heartrate=hr))["reason"] == "no power"
    assert drift.session_drift(ride(), streams(watts=[0] * N, heartrate=hr))["reason"] == "no power"


def test_streams_may_be_the_parsed_dict_already():
    w, hr = steady_ride()
    assert drift.session_drift(ride(), {"watts": w, "heartrate": hr}) == drift_of(ride(), w, hr)


# ── 9. the activity sheet's Decoupling row ────────────────────────────────────
def _row(rows, label):
    return next((r[1] for r in rows if r[0] == label), None)


def test_build_shows_the_session_drift_figure_for_a_steady_ride():
    w, hr = steady_ride()
    act = ride()
    raw = streams(watts=w, heartrate=hr)
    rows = ast_.build(act, raw, "Ride")
    expect = drift.session_drift(act, raw)["pct"]
    assert _row(rows, "Decoupling") == f"{expect:+.1f}%"


def test_build_says_n_a_for_intervals():
    w, hr = interval_ride()
    rows = ast_.build(ride(extra_np=270), streams(watts=w, heartrate=hr), "Ride")
    assert _row(rows, "Decoupling") == "n/a: intervals or surges (power too variable)"


def test_build_has_no_decoupling_row_for_a_short_ride():
    w, hr = steady_ride(n=1500)
    assert _row(ast_.build(ride(), streams(watts=w, heartrate=hr), "Ride"), "Decoupling") is None


def test_build_shows_grade_adjusted_drift_for_a_run_with_no_power():
    vel, alt, dist, hr = hilly_run()
    act = {"type": "Run"}
    raw = streams(velocity_smooth=vel, altitude=alt, distance=dist, heartrate=hr)
    shown = _row(ast_.build(act, raw, "Run"), "Decoupling")
    assert shown == f"{drift.session_drift(act, raw)['pct']:+.1f}%"


# ── 10. performance.drift_cache ───────────────────────────────────────────────
class StreamClient:
    """The one call drift_cache makes: get_activity_streams(id) -> the streams, or raises."""

    def __init__(self, by_id=None, fail=(), default=None):
        self.by_id, self.fail, self.default, self.calls = by_id or {}, set(fail), default, []

    def get_activity_streams(self, aid):
        self.calls.append(aid)
        if aid in self.fail:
            raise RuntimeError("Intervals.icu said no")
        return self.by_id.get(aid, self.default)


def dact(aid, days_ago, mins, type_="Run", race=False):
    return {"id": aid, "type": type_, "race": race, "moving_time": mins * 60,
            "start_date_local": f"{TODAY - timedelta(days=days_ago)}T07:00:00"}


STEADY = streams(watts=steady_ride(n=3000)[0], heartrate=steady_ride(n=3000)[1])


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(performance, "BASE", tmp_path)
    (tmp_path / "athletes" / "x").mkdir(parents=True)
    return tmp_path / "athletes" / "x" / "drift-cache.json"


def test_drift_cache_fetches_only_long_enough_sessions_inside_the_window(home):
    acts = [
        dact("run-ok", 10, performance.DRIFT_RUN_MIN),
        dact("run-short", 10, performance.DRIFT_RUN_MIN - 1),
        dact("ride-ok", 12, performance.DRIFT_RIDE_MIN, type_="Ride"),
        dact("ride-short", 12, performance.DRIFT_RIDE_MIN - 1, type_="Ride"),
        dact("vride-ok", 14, performance.DRIFT_RIDE_MIN + 20, type_="VirtualRide"),
        dact("run-edge", performance.DRIFT_DAYS, 100),
        dact("run-old", performance.DRIFT_DAYS + 1, 100),
        dact("run-race", 20, 100, race=True),
        dact("swim", 5, 120, type_="Swim"),
    ]
    c = StreamClient(default=STEADY)
    cache = performance.drift_cache("x", c, acts, TODAY)
    assert set(c.calls) == {"run-ok", "ride-ok", "vride-ok", "run-edge"}
    assert set(cache) == set(c.calls)
    e = cache["run-ok"]
    assert e["sport"] == "Run" and e["duration_min"] == performance.DRIFT_RUN_MIN
    assert e["date"] == (TODAY - timedelta(days=10)).isoformat()
    assert e["method"] == "power:HR" and e["reason"] is None and e["pct"] is not None
    assert cache["vride-ok"]["sport"] == "Ride" and cache["vride-ok"]["duration_min"] == 200
    assert json.loads(home.read_text()) == cache


def test_drift_cache_fetches_each_id_once_across_calls(home):
    acts = [dact("a", 3, 90), dact("b", 4, 200, type_="Ride")]
    c = StreamClient(default=STEADY)
    first = performance.drift_cache("x", c, acts, TODAY)
    second = performance.drift_cache("x", c, acts, TODAY + timedelta(days=1))
    assert sorted(c.calls) == ["a", "b"] and second == first
    c2 = StreamClient(default=STEADY)                  # a fresh process reads the file
    third = performance.drift_cache("x", c2, acts + [dact("c", 2, 100)], TODAY)
    assert c2.calls == ["c"] and {k: third[k] for k in first} == first


def test_drift_cache_caps_new_fetches_per_call_newest_first(home):
    extra = 5
    acts = [dact(f"r{d}", d, 100) for d in range(1, performance.DRIFT_NEW_PER_BUILD + extra + 1)]
    c = StreamClient(default=STEADY)
    first = performance.drift_cache("x", c, list(reversed(acts)), TODAY)     # input order must not matter
    n = performance.DRIFT_NEW_PER_BUILD
    assert c.calls == [f"r{d}" for d in range(1, n + 1)] and len(first) == n
    second = performance.drift_cache("x", c, acts, TODAY)
    assert c.calls[n:] == [f"r{d}" for d in range(n + 1, n + extra + 1)]
    assert len(second) == n + extra and len(set(c.calls)) == len(c.calls)


def test_drift_cache_one_failed_fetch_does_not_stop_the_others(home):
    acts = [dact("a", 1, 100), dact("boom", 2, 100), dact("c", 3, 100)]
    c = StreamClient(default=STEADY, fail={"boom"})
    cache = performance.drift_cache("x", c, acts, TODAY)
    assert set(cache) == {"a", "c"} and c.calls == ["a", "boom", "c"]
    c.fail = set()                                    # next build: the failure is retried, the rest are not
    c.calls.clear()
    assert set(performance.drift_cache("x", c, acts, TODAY)) == {"a", "boom", "c"}
    assert c.calls == ["boom"]


def test_drift_cache_untrusted_heart_rate_is_stored_with_its_reason(home):
    acts = [dact("good", 1, 100), dact("bad", 2, 100)]
    c = StreamClient(default=STEADY)
    seen = []

    def trusted(aid):
        seen.append(aid)
        return aid != "bad"
    cache = performance.drift_cache("x", c, acts, TODAY, trusted)
    assert cache["bad"]["pct"] is None and cache["bad"]["reason"] == "heart rate failed the check"
    assert cache["bad"]["method"] is None and cache["bad"]["sport"] == "Run"
    assert cache["good"]["pct"] is not None and set(seen) == {"good", "bad"}


def test_drift_cache_a_session_with_no_usable_streams_is_cached_with_a_reason(home):
    c = StreamClient(default=[])                      # fetched fine, but nothing in it
    cache = performance.drift_cache("x", c, [dact("a", 1, 100)], TODAY)
    assert cache["a"]["pct"] is None and cache["a"]["reason"] == "no heart rate"


# ── 11. performance._log_drift ────────────────────────────────────────────────
def _e(days_ago, sport, mins, pct):
    return {"date": (TODAY - timedelta(days=days_ago)).isoformat(), "sport": sport,
            "duration_min": mins, "pct": pct, "method": "power:HR", "reason": None}


def test_log_drift_filters_by_sport_minutes_and_window():
    cache = {
        "run-a": _e(10, "Run", 100, 2.0),
        "run-b": _e(56, "Run", 100, 4.0),              # the last day inside the window
        "run-old": _e(57, "Run", 100, 100.0),
        "run-short": _e(5, "Run", 74, 50.0),
        "run-today": _e(0, "Run", 100, 90.0),          # the window is the days *before* as_of
        "run-none": _e(3, "Run", 100, None),           # no figure: not counted
        "ride": _e(5, "Ride", 200, 7.0),
    }
    assert performance._log_drift(cache, TODAY, 75, sport="Run") == 3.0
    assert performance._log_drift(cache, TODAY, 180, sport="Ride") == 7.0
    assert performance._log_drift(cache, TODAY, 180, sport="Run") is None
    assert performance._log_drift(cache, TODAY, 75) == 4.0                    # no sport: all three
    assert performance._log_drift(cache, TODAY, 75, days=10, sport="Run") == 2.0
    assert performance._log_drift(cache, TODAY, 75, days=9, sport="Run") is None
    assert performance._log_drift({}, TODAY, 75, sport="Run") is None


def test_log_drift_still_reads_the_old_watcher_log_list():
    log = [{"date": (TODAY - timedelta(days=d)).isoformat(), "duration_min": 200, "decoupling_pct": p}
           for d, p in ((3, 2.0), (9, 5.0), (80, 40.0))]
    assert performance._log_drift(log, TODAY, 180) == 3.5


# ── the cache feeds the run and bike blocks ───────────────────────────────────
def test_durability_and_bike_read_the_drift_cache():
    runs = [dact("r1", 5, 100), dact("r2", 9, 120)]
    cache = {"r1": _e(5, "Run", 100, 2.0), "r2": _e(9, "Run", 120, 4.0)}
    out = performance.durability(runs, TODAY, drift=cache)
    assert out["drift_pct"] == 3.0 and out["n"] == 2 and out["source"] == "power"
    cache["r2"]["method"] = "grade-adjusted pace:HR"
    assert performance.durability(runs, TODAY, drift=cache)["source"] == "pace"
    rides = [dact("b1", 5, 200, type_="Ride"), dact("b2", 9, 240, type_="VirtualRide")]
    bcache = {"b1": _e(5, "Ride", 200, 3.0), "b2": _e(9, "Ride", 240, 6.0)}
    b = performance.bike(rides, TODAY, None, None, drift=bcache)
    assert b["durability"] == {"drift_pct": 4.5, "n": 2}
