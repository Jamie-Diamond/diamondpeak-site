#!/usr/bin/env python3
"""Baseline block: a new athlete's first week is fitness tests, not a training plan.

Why this exists (27 Sep 2026). Onboarding took whatever FTP / run threshold / CSS
Intervals.icu happened to hold, or asked the athlete to type one in, and built zones
on it. A guessed threshold makes every zone, every "% Pace" step and every load number
wrong, so the plan is pointless from day one. Jamie's call: a good onboarding gets a
baseline first. The agreed experience, verbatim from the approval:

  "a new athlete answers about 10 questions, then gets a baseline block instead of a
   plan: a ramp test, a 30-min run time trial and a CSS swim test, only for the sports
   they race. Their zones are set from the results, and the real plan only starts
   once each sport has a tested number. Until then, that sport is prescribed by RPE."

  Block length: one week when the tests can be spaced inside it, two only when the
  athlete's available days cannot space them (Jamie, 27 Sep).

The bike test is the ramp test Jamie approved: fixed-watt steps rising 20 W a minute
until the athlete cannot hold the target (ICU takes "- 1m 140w" steps, verified live
27 Sep 2026), FTP = 75% of the best minute. It needs no pacing skill, which a new
athlete does not have yet; a 20-minute test paced badly reads low. Its known flaw is
that it reads high for diesel long-course athletes, which is one reason the athlete
confirms every result before it becomes their zones.

RELATION TO THE 8 JUN DECISION. generate-blueprint.py's SCHEDULE_PERFORMANCE_TESTS=False
("never schedule field tests; thresholds come from intervals.icu estimates") stands for
recurring tests across a build. This is a different thing: a one-off baseline for a NEW
athlete, whose Intervals.icu estimates are exactly what cannot be trusted yet. An
athlete who says they tested in the last six weeks skips that sport's test.

Scope. Only athletes onboarded from 27 Sep 2026 carry athletes/<slug>/baseline.json.
No file = an established athlete, and every function here answers "no effect" for
them, so Jamie, Kathryn and Calum are untouched.

Confidence per sport, so the coach never treats a guess as fact:
  tested     a baseline result the athlete confirmed, or a test they did in the last
             six weeks by their own account (athlete-stated figures are law)
  estimated  a number exists (Intervals.icu, or typed in) but nobody tested it
  missing    no number at all
A raced sport that is not `tested` is prescribed by RPE (rpe_only_families).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/
STATE_NAME = "baseline.json"
RECENT_TEST_DAYS = 42
FAMILIES = ("bike", "run", "swim")
ICU_SPORT = {"bike": "Ride", "run": "Run", "swim": "Swim"}
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MIN_TEST_GAP_DAYS = 2          # bike and run tests at least 48 h apart
MIN_WINDOW_DAYS = 5            # a window shorter than this runs on to the next Sunday

_RIDE = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide")
_RUN = ("Run", "VirtualRun", "TrailRun", "Treadmill")
_SWIM = ("Swim", "OpenWaterSwim")


def family_of(icu_type: str | None) -> str | None:
    t = icu_type or ""
    if t in _RIDE:
        return "bike"
    if t in _RUN:
        return "run"
    if t in _SWIM:
        return "swim"
    return None


# ── State file ──────────────────────────────────────────────────────────────

def state_path(slug: str) -> Path:
    return BASE / "athletes" / slug / STATE_NAME


def load(slug: str) -> dict | None:
    p = state_path(slug)
    if not p.exists():
        return None
    try:
        st = json.loads(p.read_text())
        return st if isinstance(st, dict) else None
    except Exception:
        return None


def save(slug: str, st: dict) -> None:
    p = state_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".bl-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Gates (all "no effect" without a state file) ────────────────────────────

def _window(st: dict) -> tuple[date, date] | None:
    w = (st or {}).get("window") or {}
    try:
        return date.fromisoformat(w["start"]), date.fromisoformat(w["end"])
    except Exception:
        return None


def in_window(slug: str, day: date, st: dict | None = None) -> bool:
    """True while the baseline block owns this day's calendar."""
    st = load(slug) if st is None else st
    if not st or st.get("status") != "active":
        return False
    w = _window(st)
    return bool(w) and w[0] <= day <= w[1]


def blocks_week(slug: str, week_start: date, st: dict | None = None) -> bool:
    """Whether the normal weekly plan must stand down for the week starting
    week_start. Pending (approved but not yet scheduled) blocks everything: the
    baseline comes first, so no plan may be built ahead of it."""
    st = load(slug) if st is None else st
    if not st:
        return False
    if st.get("status") == "pending":
        return True
    if st.get("status") != "active":
        return False
    w = _window(st)
    return bool(w) and not (week_start + timedelta(days=6) < w[0] or week_start > w[1])


def confidence(slug: str, st: dict | None = None) -> dict | None:
    st = load(slug) if st is None else st
    if not st:
        return None
    return {f: (st.get("sports_state", {}).get(f) or {}).get("confidence", "missing")
            for f in st.get("sports", [])}


def rpe_only_families(slug: str, st: dict | None = None) -> set:
    """Raced sports with no tested threshold: prescribed by RPE, never by % targets.
    Empty for an established athlete and before the block has started."""
    st = load(slug) if st is None else st
    if not st or st.get("status") not in ("active", "complete"):
        return set()
    conf = confidence(slug, st) or {}
    return {f for f, c in conf.items() if c != "tested"}


# ── Building the state at onboarding ────────────────────────────────────────

def protocol_for(family: str, has_power: bool | None, hr_source: str | None) -> str | None:
    """The test this athlete can actually do. A bike test without power needs a
    trustworthy HR (a strap); without either there is nothing to measure, so the
    bike stays on RPE rather than being "tested" off a wrist sensor."""
    if family == "bike":
        if has_power:
            return "bike_ramp"
        return "bike_hr" if hr_source == "strap" else None
    if family == "run":
        return "run_tt"
    if family == "swim":
        return "swim_css"
    return None


def new_state(sports: list, hr_source: str | None, has_power: bool | None,
              known: dict, stated_recent: set, today: date | None = None,
              expected: dict | None = None) -> dict:
    """known = {"bike": {"ftp": 250, "_source": "icu"}, "run": {"threshold_pace": "4:30"},
    "swim": {"css": "1:45"}}; stated_recent = families the athlete says they tested in
    the last six weeks; expected = {family: parse_estimate(...)}, their rough figures."""
    today = today or date.today()
    sports_state = {}
    for f in sports:
        vals = {k: v for k, v in (known.get(f) or {}).items() if v and not k.startswith("_")}
        # value_source: where the NUMBER came from (icu | athlete). source: why we
        # believe it (stated_test | test). An athlete-typed tested number is not in
        # Intervals.icu yet, so baseline-week.py start writes it there.
        vsrc = (known.get(f) or {}).get("_source") if vals else None
        if vals and f in stated_recent:
            conf, src = "tested", "stated_test"
        elif vals:
            conf, src = "estimated", None
        else:
            conf, src = "missing", None
        proto = None if conf == "tested" else protocol_for(f, has_power, hr_source)
        sports_state[f] = {
            "confidence": conf, "source": src, "value_source": vsrc, "values": vals,
            "test": {"protocol": proto, "status": "unscheduled"} if proto else None,
        }
        if proto and (expected or {}).get(f):
            sports_state[f]["expected"] = expected[f]
    return {
        "version": 1, "status": "pending", "created": today.isoformat(),
        "sports": list(sports), "hr_source": hr_source, "has_power": has_power,
        "window": None, "sports_state": sports_state,
    }


# ── Scheduling ──────────────────────────────────────────────────────────────

def plan_window(first_day: date) -> tuple[date, date]:
    """From first_day to a Sunday, so the normal weekly plan (built Sunday evening
    for the next Monday) takes over cleanly. Runs on to the following Sunday when
    fewer than MIN_WINDOW_DAYS remain."""
    end = first_day + timedelta(days=6 - first_day.weekday())
    if (end - first_day).days + 1 < MIN_WINDOW_DAYS:
        end += timedelta(days=7)
    return first_day, end


def _place_tests(tests: list, days: list[date]) -> dict | None:
    """Assign each test family a day. Hard: one test per day, bike and run tests
    MIN_TEST_GAP_DAYS apart, swim not the day before the run, never the first day
    (arrive at the first test after an easy day). Soft, in order: a clear day between
    every pair of tests where the window allows it ("spaced", Jamie 27 Sep), then
    finish earliest. None if it cannot be done. Ties keep the bike test first, on
    the freshest legs, because permutations() yields the tests list order first."""
    from itertools import permutations
    usable = days[1:] if len(days) > 1 else days
    best, best_key = None, None
    for combo in permutations(usable, len(tests)):
        a = dict(zip(tests, combo))
        if "bike" in a and "run" in a and abs((a["bike"] - a["run"]).days) < MIN_TEST_GAP_DAYS:
            continue
        # Swim is low impact but still an all-out effort: not the day before the run test.
        if "swim" in a and "run" in a and (a["run"] - a["swim"]).days == 1:
            continue
        ordered = sorted(combo)
        gaps = [(ordered[i + 1] - ordered[i]).days for i in range(len(ordered) - 1)]
        spacing = min(min(gaps), MIN_TEST_GAP_DAYS) if gaps else MIN_TEST_GAP_DAYS
        key = (-spacing, max(combo), sum(d.toordinal() for d in combo))
        if best_key is None or key < best_key:
            best, best_key = a, key
    return best


def _spacing(placed: dict) -> int:
    ds = sorted(placed.values())
    return min((ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)) if len(ds) > 1 else 99


def schedule(st: dict, first_day: date, training_days: list | None = None,
             max_hours: float | None = None) -> list[dict]:
    """The block's sessions, pure. Sets st["window"] and each test's date. Returns
    [{date, family, kind: test|easy|rest, protocol?, minutes?}] for every day."""
    start, end = plan_window(first_day)
    allowed = set(training_days or DAYS)
    tests = [f for f in ("bike", "swim", "run")
             if (st["sports_state"].get(f) or {}).get("test")]

    def _avail(s, e):
        return [s + timedelta(days=i) for i in range((e - s).days + 1)
                if DAYS[(s + timedelta(days=i)).weekday()] in allowed]

    placed = _place_tests(tests, _avail(start, end)) if tests else {}
    # A second week only when the first cannot SPACE the tests (Jamie, 27 Sep: "could
    # be done in a week if spaced"). A mid-week start with three tests would otherwise
    # cram them onto three consecutive days; a single test never needs the extension.
    if len(tests) > 1 and (placed is None or _spacing(placed) < MIN_TEST_GAP_DAYS):
        wider = _place_tests(tests, _avail(start, end + timedelta(days=7)))
        if wider and (placed is None or _spacing(wider) > _spacing(placed)):
            placed, end = wider, end + timedelta(days=7)
    if tests and placed is None:            # still not: place what fits, in priority order
        placed = {}
        for f in tests:
            trial = _place_tests(list(placed) + [f], _avail(start, end))
            if trial:
                placed = trial
    placed = placed or {}
    st["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    for f in tests:
        t = st["sports_state"][f]["test"]
        if f in placed:
            t.update(status="scheduled", date=placed[f].isoformat())
        else:
            t.update(status="unplaceable", date=None)

    scale = min(1.5, max(0.75, (max_hours or 8) / 8))
    easy_min = {"bike": round(60 * scale / 5) * 5, "run": round(40 * scale / 5) * 5,
                "swim": round(35 * scale / 5) * 5}
    test_days = {d: f for f, d in placed.items()}
    # One rest day in every 7-day stretch of six days or more that has none already
    # (a day the athlete cannot train counts). Preferred: the day after the run test,
    # the hardest on the legs, then after the bike test, else the stretch's last free
    # day. Never the first day, which is the easy lead-in.
    rest_days = set()
    cs = start
    while cs <= end:
        ce = min(end, cs + timedelta(days=6))
        span = [cs + timedelta(days=i) for i in range((ce - cs).days + 1)]
        if len(span) >= 6 and all(DAYS[x.weekday()] in allowed for x in span):
            free = [x for x in span if x not in test_days and x != start]
            prefs = [placed[f] + timedelta(days=1) for f in ("run", "bike", "swim") if f in placed]
            pick = next((x for x in prefs if x in free), free[-1] if free else None)
            if pick:
                rest_days.add(pick)
        cs = ce + timedelta(days=1)
    days, rotation, n_easy = [], list(st["sports"]), 0
    d = start
    while d <= end:
        if d in test_days:
            f = test_days[d]
            item = {"date": d.isoformat(), "family": f, "kind": "test",
                    "protocol": st["sports_state"][f]["test"]["protocol"]}
            if item["protocol"] == "bike_ramp":
                ss = st["sports_state"][f]
                item["ramp_start_w"] = ramp_start_watts(
                    (ss.get("values") or {}).get("ftp") or expected_mid(ss.get("expected")))
            days.append(item)
        elif DAYS[d.weekday()] not in allowed or d in rest_days:
            days.append({"date": d.isoformat(), "kind": "rest"})
        else:
            nxt = test_days.get(d + timedelta(days=1))
            prv = test_days.get(d - timedelta(days=1))
            choice = None
            for k in range(len(rotation)):
                f = rotation[(n_easy + k) % len(rotation)]
                if f not in (nxt, prv):
                    choice = f
                    break
            choice = choice or rotation[n_easy % len(rotation)]
            n_easy += 1
            mins = easy_min[choice]
            if nxt:                          # the day before a test stays short
                mins = max(20, round(mins * 0.6 / 5) * 5)
            days.append({"date": d.isoformat(), "family": choice, "kind": "easy",
                         "minutes": mins})
        d += timedelta(days=1)
    return days


# ── Workouts (ICU structured text; RPE steps, verified live 27 Sep 2026) ────
# A step is "- <text> <duration>" with NO target: ICU keeps the text as the step
# cue and leaves pace/power/HR empty, so the watch shows the cue and a timer.
# Swim distances use "mtr" (a bare "m" is minutes).

def _expand(steps):
    out = []
    for s in steps:
        if isinstance(s, tuple):
            n, sub = s
            for _ in range(n):
                out.extend(sub)
        else:
            out.append(s)
    return "\n".join(out)


TESTS = {
    "bike_ramp": {
        "sport": "Ride", "name": "Baseline: ramp test", "minutes": 45, "load": 50,
        # Steps are built per athlete by _ramp_steps (the start watts vary).
        "steps": None,
        "notes": ("Baseline bike test. This sets your bike zones. Best on a smart trainer in "
                  "ERG mode; outdoors, pick a long flat road with no junctions. After the "
                  "warm-up the target rises 20 W every minute. Hold each step as long as you "
                  "can; the test ends when you cannot keep the target, then cool down. Most "
                  "people stop between 8 and 20 minutes in. Your FTP is 75% of your best "
                  "minute. Easy or rest the day before."),
    },
    "bike_hr": {
        "sport": "Ride", "name": "Baseline: 30-min bike time trial", "minutes": 60, "load": 70,
        "steps": ["- Warm up easy, RPE 3 15m",
                  "- Build to hard, RPE 6 3m",
                  "- Easy spin 2m",
                  "- 30 MIN TIME TRIAL: hardest even effort, RPE 9 30m",
                  "- Cool down easy 10m"],
        "notes": ("Baseline bike test, heart-rate version (no power meter). Wear your chest "
                  "strap and wet it first. Turbo or a flat road, no stopping, even effort for the "
                  "full 30 minutes. Your threshold heart rate is the average over the last 20."),
    },
    "run_tt": {
        "sport": "Run", "name": "Baseline: 30-min run time trial", "minutes": 60, "load": 70,
        "steps": ["- Warm up easy jog, RPE 3 15m",
                  (4, ["- Stride, quick and relaxed, RPE 7 20s", "- Easy jog 40s"]),
                  "- Easy jog 1m",
                  "- 30 MIN TIME TRIAL: hardest even pace you can hold, RPE 9 30m",
                  "- Cool down easy jog 10m"],
        "notes": ("Baseline run test. This sets your run pace zones. Flat route or a track, "
                  "on your own, no stopping (pause your watch only if you must). Run it evenly: "
                  "hold back in the first 5 minutes. Your threshold pace is your average pace over "
                  "the last 20 minutes."),
    },
    "swim_css": {
        "sport": "Swim", "name": "Baseline: CSS swim test (400 + 200)", "minutes": 40, "load": 45,
        "steps": ["- Warm up easy, RPE 3 300mtr",
                  "- Drills or easy 200mtr",
                  (4, ["- Build each length, RPE 6 50mtr", "- Rest 15s"]),
                  "- Rest 2m",
                  "- 400 ALL OUT, even pace 400mtr",
                  "- Easy swim or rest, recover fully 5m",
                  "- 200 ALL OUT 200mtr",
                  "- Cool down easy 200mtr"],
        "notes": ("Baseline swim test. This sets your swim pace zones. Push off the wall, no "
                  "dive. Pace the 400 evenly, then recover fully before the 200. Press lap after "
                  "each rep so the times are clean. Your CSS (threshold swim pace) comes from the "
                  "two times."),
    },
}

EASY = {
    "bike": ("Ride", "Easy ride", "- Easy ride, RPE 3, conversational {m}m",
             "Easy by feel, RPE 3: you can talk in full sentences. No targets this week."),
    "run": ("Run", "Easy run", "- Easy run, RPE 3, conversational {m}m",
            "Easy by feel, RPE 3: you can talk in full sentences. No targets this week."),
    "swim": ("Swim", "Easy swim", "- Easy swim, RPE 3 {m}m",
             "Easy by feel, RPE 3, relaxed technique. No targets this week."),
}


RAMP_STEP_W = 20
RAMP_STEPS = 25


def ramp_start_watts(known_ftp) -> int:
    """40% of an estimated FTP, else 100 W: low enough that nobody fails the first
    step, high enough that the test is not ten minutes of junk."""
    try:
        return max(80, int(round(0.4 * float(known_ftp) / 10) * 10))
    except (TypeError, ValueError):
        return 100


def _ramp_steps(start_w: int) -> list:
    return (["- Warm up easy, RPE 3 10m",
             "- Build, RPE 6 2m",
             "- Easy spin 3m"]
            + [f"- Ramp: hold it as long as you can 1m {start_w + i * RAMP_STEP_W}w"
               for i in range(RAMP_STEPS)]
            + ["- Cool down easy 10m"])


def workout_for(day: dict) -> dict | None:
    """ICU push fields for one scheduled day. None for a rest day."""
    if day["kind"] == "test":
        t = TESTS[day["protocol"]]
        steps = _ramp_steps(day.get("ramp_start_w") or 100) if t["steps"] is None else t["steps"]
        return {"sport": t["sport"], "date": day["date"], "name": "🔬 " + t["name"],
                "description": _expand(steps), "description_raw": t["notes"],
                "duration_min": t["minutes"], "load": t["load"]}
    if day["kind"] == "easy":
        sport, name, step, notes = EASY[day["family"]]
        m = day["minutes"]
        return {"sport": sport, "date": day["date"], "name": f"{name} {m} min",
                "description": step.format(m=m), "description_raw": notes,
                "duration_min": m, "load": round(m / 60 * 42)}
    return None


# ── Rough figures (30 Sep 2026) ─────────────────────────────────────────────
# For a sport the baseline week will test, sign-up asks the athlete for a rough figure
# (a recent race, a guessed FTP, a steady swim time). It is NEVER a threshold: it gives
# the test an expected range, so a result far outside it is questioned before it sets
# zones, and a loose guide beside RPE for the by-feel week. Jamie, 30 Sep: "range is
# worth teasing out before we do the tests".
#   {"low", "high", "unit": "w" | "s_km" | "s_100m", "raw"}  low/high are the range in
#   that unit (for paces, low is the FASTER end).

_RACE_KM = ((r"half|21\.1|13\.1", 21.0975), (r"marathon|42\.2|26\.2", 42.195),
            (r"10\s*k|10\s*km", 10.0), (r"\b5\s*k|5\s*km|parkrun", 5.0))
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _riegel(t_s: float, d_km: float, to_km: float) -> float:
    return t_s * (to_km / d_km) ** 1.06


def parse_estimate(family: str, text: str) -> dict | None:
    """The athlete's rough figure as a range, or None ("don't know", unreadable)."""
    raw = (text or "").strip()
    low = raw.lower()
    if not raw or re.match(r"^\s*(no|none|n/?a|not sure|unsure|don'?t know|dunno|idk)\b|^\s*\?", low):
        return None
    if family == "bike":
        nums = [int(n) for n in re.findall(r"\d{2,3}", raw) if 60 <= int(n) <= 600]
        if not nums:
            return None
        lo, hi = (min(nums[:2]), max(nums[:2])) if len(nums) >= 2 else (nums[0] * 0.92, nums[0] * 1.08)
        return {"low": round(lo), "high": round(hi), "unit": "w", "raw": raw[:80]}
    m = _CLOCK.search(raw)
    if not m:
        return None
    a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
    if family == "run":
        km = next((k for pat, k in _RACE_KM if re.search(pat, low)), None)
        if not km:
            return None
        secs = a * 3600 + b * 60 + int(c) if c else a * 60 + b
        if not c and secs / km < 150:          # "1:52" for a half is h:mm, not mm:ss
            secs = a * 3600 + b * 60
        if not 150 <= secs / km <= 600:
            return None
        # Threshold sits between 10k and half-marathon race pace for most amateurs.
        fast = _riegel(secs, km, 10.0) / 10.0 * 0.98
        slow = _riegel(secs, km, 21.0975) / 21.0975 * 1.02
        return {"low": round(fast), "high": round(slow), "unit": "s_km", "raw": raw[:80]}
    if family == "swim":
        secs = a * 60 + b
        dist = 400 if re.search(r"400", low) or (not re.search(r"100", low) and secs >= 240) else 100
        per100 = secs / (dist / 100)
        if not 50 <= per100 <= 240:
            return None
        lo, hi = (per100 * 0.95, per100 * 1.05) if dist == 400 else (per100 * 0.97, per100 * 1.10)
        return {"low": round(lo), "high": round(hi), "unit": "s_100m", "raw": raw[:80]}
    return None


def _clock(s: float) -> str:
    m, sec = divmod(int(round(s)), 60)
    return f"{m}:{sec:02d}"


def _pace_s(v) -> float | None:
    m = _CLOCK.match(str(v or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def expected_mid(exp: dict | None) -> float | None:
    return (exp["low"] + exp["high"]) / 2 if exp else None


def expected_note(family: str, vals: dict, exp: dict | None) -> str:
    """A line for the result message when a test lands well outside the athlete's own
    rough figure (10% beyond either end); "" otherwise."""
    if not exp:
        return ""
    if family == "bike" and vals.get("ftp"):
        v, better_high = float(vals["ftp"]), True
    elif family == "run" and vals.get("threshold_pace"):
        v, better_high = _pace_s(vals["threshold_pace"]), False
    elif family == "swim" and vals.get("css"):
        v, better_high = _pace_s(vals["css"]), False
    else:
        return ""
    if v is None or exp["low"] * 0.9 <= v <= exp["high"] * 1.1:
        return ""
    above = v > exp["high"] * 1.1
    stronger = above if better_high else not above
    return (f"\n\n⚠️ That's a lot {'stronger' if stronger else 'weaker'} than your rough figure "
            f"(_{exp['raw']}_). If the test went wrong, tap *Test went wrong* and we'll redo it.")


def rough_guide(family: str, exp: dict | None) -> str:
    """Easy-effort guide from the rough figure, for the by-feel week: "" without one."""
    if not exp:
        return ""
    mid = expected_mid(exp)
    if exp["unit"] == "w":
        return f"bike easy roughly {round(exp['low'] * 0.55)}-{round(exp['high'] * 0.70)} W (from '{exp['raw']}')"
    if exp["unit"] == "s_km":
        return f"run easy roughly {_clock(mid * 1.15)}-{_clock(mid * 1.30)}/km (from '{exp['raw']}')"
    return f"swim easy roughly {_clock(mid + 5)}-{_clock(mid + 12)}/100m (from '{exp['raw']}')"


# ── Result maths (pure) ─────────────────────────────────────────────────────

def _resample(time_s: list, values: list) -> list:
    """1 Hz series, holding the last value across smart-recording gaps (capped
    at 10 s so a pause reads as missing, not as effort)."""
    if not time_s or not values or len(time_s) != len(values):
        return []
    out, t0 = [], time_s[0] or 0
    for i in range(len(time_s)):
        t = (time_s[i] or 0) - t0
        nxt = ((time_s[i + 1] or 0) - t0) if i + 1 < len(time_s) else t + 1
        v = values[i]
        span = max(1, min(nxt - t, 10))
        while len(out) < t:
            out.append(None)
        out.extend([v] * int(span))
    return out


def _best_window(series: list, secs: int) -> tuple[float, int] | None:
    """Highest mean over any `secs` window with at least 90% data. (mean, start)."""
    if len(series) < secs:
        return None
    vals = [v if isinstance(v, (int, float)) else None for v in series]
    csum, ccnt = [0.0], [0]
    for v in vals:
        csum.append(csum[-1] + (v or 0))
        ccnt.append(ccnt[-1] + (1 if v is not None else 0))
    best = None
    for s in range(0, len(vals) - secs + 1):
        n = ccnt[s + secs] - ccnt[s]
        if n < 0.9 * secs:
            continue
        m = (csum[s + secs] - csum[s]) / n
        if best is None or m > best[0]:
            best = (m, s)
    return best


def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and x > 0]
    return sum(xs) / len(xs) if xs else None


def pace_str(mps: float, per_m: int) -> str:
    s = per_m / mps
    m, sec = divmod(int(round(s)), 60)
    return f"{m}:{sec:02d}"


def bike_ramp_result(streams: dict) -> dict | None:
    """FTP = 75% of the best 60 s. A best minute under 150 W is not a ramp that
    was ridden to failure (or the power meter dropped out)."""
    p = _resample(streams.get("time") or [], streams.get("watts") or [])
    w = _best_window(p, 60)
    if not w or w[0] < 150:
        return None
    return {"ftp": int(round(0.75 * w[0])), "best1m_w": int(round(w[0]))}


def _last20_of_best30(series_for_window: list, secs: int = 1800):
    w = _best_window(series_for_window, secs)
    if not w:
        return None
    return w[1] + 600, w[1] + secs          # the last 20 min of the best 30


def bike_hr_result(streams: dict, hr_trusted: bool) -> dict | None:
    if not hr_trusted:
        return None
    hr = _resample(streams.get("time") or [], streams.get("heartrate") or [])
    span = _last20_of_best30(hr)
    if not span:
        return None
    lthr = _mean(hr[span[0]:span[1]])
    return {"lthr": int(round(lthr))} if lthr else None


def run_tt_result(streams: dict, hr_trusted: bool) -> dict | None:
    """Threshold pace = average speed over the last 20 minutes of the fastest
    30-minute stretch; LTHR = average HR over the same 20 minutes, only when the
    activity's HR passed lib/hr_quality."""
    t = streams.get("time") or []
    v = _resample(t, streams.get("velocity_smooth") or [])
    span = _last20_of_best30(v)
    if not span:
        return None
    mps = _mean(v[span[0]:span[1]])
    if not mps or not (1.5 <= mps <= 7.0):
        return None
    out = {"threshold_mps": round(mps, 4), "threshold_pace": pace_str(mps, 1000)}
    if hr_trusted:
        hr = _resample(t, streams.get("heartrate") or [])
        lthr = _mean(hr[span[0]:span[1]])
        if lthr:
            out["lthr"] = int(round(lthr))
    return out


def css_from(d_long: float, t_long: float, d_short: float, t_short: float) -> dict | None:
    """CSS speed = (D400 - D200) / (T400 - T200). Works in yards pools too because
    it uses the distances actually swum."""
    if t_long <= t_short or d_long <= d_short:
        return None
    mps = (d_long - d_short) / (t_long - t_short)
    if not (0.5 <= mps <= 2.2):
        return None
    return {"css_mps": round(mps, 4), "css": pace_str(mps, 100),
            "t400_s": int(round(t_long)), "t200_s": int(round(t_short))}


def swim_css_result(intervals: list) -> dict | None:
    """From ICU's detected WORK intervals. ICU swim intervals run each rep ~2-4 s
    into the rest (CLAUDE.md hard rule), but CSS is a DIFFERENCE of two rep times,
    so a similar overrun on both reps cancels. Still, the athlete confirms."""
    work = [iv for iv in intervals or [] if (iv.get("type") == "WORK"
            and iv.get("distance") and iv.get("moving_time"))]
    longs = [iv for iv in work if 340 <= iv["distance"] <= 440]
    if not longs:
        return None
    l400 = max(longs, key=lambda iv: iv.get("average_speed") or 0)
    shorts = [iv for iv in work if 170 <= iv["distance"] <= 220
              and (iv.get("start_index") or 0) > (l400.get("start_index") or 0)]
    if not shorts:
        return None
    s200 = max(shorts, key=lambda iv: iv.get("average_speed") or 0)
    return css_from(l400["distance"], l400["moving_time"], s200["distance"], s200["moving_time"])


_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


def parse_swim_times(text: str) -> tuple[int, int] | None:
    """'400 in 7:10, 200 in 3:25' -> (430, 205). Two m:ss times; the longer is the
    400. None unless exactly two plausible times are present."""
    ts = [int(m) * 60 + int(s) for m, s in _TIME_RE.findall(text or "") if int(s) < 60]
    if len(ts) != 2:
        return None
    t400, t200 = max(ts), min(ts)
    if not (150 <= t400 <= 1200 and 60 <= t200 <= 600 and t400 > 1.6 * t200):
        return None
    return t400, t200


# ── Applying a confirmed result ─────────────────────────────────────────────

def _profile_path(slug):
    return BASE / "athletes" / slug / "profile.json"


def _write_profile(slug: str, family: str, vals: dict) -> None:
    p = _profile_path(slug)
    try:
        prof = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        prof = {}
    if family == "bike" and vals.get("ftp"):
        prof["ftp_watts"] = vals["ftp"]
    if vals.get("lthr") and family in ("bike", "run"):
        prof["lthr"] = vals["lthr"]
    if family == "run" and vals.get("threshold_pace"):
        prof["run_threshold_pace_per_km"] = vals["threshold_pace"]
    if family == "swim" and vals.get("css"):
        prof["swim_css_per_100m"] = vals["css"]
    p.write_text(json.dumps(prof, indent=2, ensure_ascii=False))


def icu_payload(family: str, vals: dict, trainer: bool = False) -> dict:
    """The sport-settings fields a result sets. ICU stores threshold_pace in METRES
    PER SECOND (lib/thresholds.py) and derives the pace zones from it."""
    if family == "bike":
        out = {}
        if vals.get("ftp"):
            out["ftp"] = vals["ftp"]
            if trainer:
                out["indoor_ftp"] = vals["ftp"]
        if vals.get("lthr"):
            out["lthr"] = vals["lthr"]
        return out
    if family == "run":
        out = {"threshold_pace": vals["threshold_mps"]} if vals.get("threshold_mps") else {}
        if vals.get("lthr"):
            out["lthr"] = vals["lthr"]
        return out
    if family == "swim" and vals.get("css_mps"):
        return {"threshold_pace": vals["css_mps"]}
    return {}


def apply(slug: str, family: str, vals: dict, client, source: str,
          trainer: bool = False, st: dict | None = None) -> dict:
    """Write a result to Intervals.icu sport settings, profile.json and the state,
    and mark the sport tested. The ICU write goes first: if it fails nothing local
    claims a zone the calendar does not have."""
    st = load(slug) if st is None else st
    payload = icu_payload(family, vals, trainer)
    if payload:
        client._put(f"sport-settings/{ICU_SPORT[family]}", payload)
    _write_profile(slug, family, vals)
    if st:
        ss = st["sports_state"].setdefault(family, {"test": None})
        ss.update(confidence="tested", source=source,
                  values={k: v for k, v in vals.items() if not k.endswith("_s")})
        if ss.get("test"):
            ss["test"]["status"] = "done"
            ss["test"].pop("pending", None)
        save(slug, st)
    return payload


# ── Result capture from an activity ─────────────────────────────────────────

def match_test(st: dict, activity: dict) -> str | None:
    """Which scheduled test this activity is, if any: paired to the test's calendar
    event, or the right sport on the test day (or up to two days late, if named as
    the baseline)."""
    if not st or st.get("status") not in ("active", "complete"):
        return None
    fam = family_of(activity.get("type"))
    ss = (st.get("sports_state") or {}).get(fam) or {}
    t = ss.get("test") or {}
    if t.get("status") != "scheduled" or not t.get("date"):
        return None
    if t.get("icu_event_id") and str(activity.get("paired_event_id")) == str(t["icu_event_id"]):
        return fam
    try:
        lag = (date.fromisoformat((activity.get("start_date_local") or "")[:10])
               - date.fromisoformat(t["date"])).days
    except Exception:
        return None
    named = "baseline" in (activity.get("name") or "").lower()
    if lag == 0 or (named and 0 <= lag <= 2):
        return fam
    return None


LABEL = {"bike": "🚴", "run": "🏃", "swim": "🏊"}


def result_text(family: str, vals: dict, prior: dict | None) -> str:
    was = ""
    if family == "bike" and vals.get("ftp"):
        if (prior or {}).get("ftp"):
            was = f" (Intervals.icu had {prior['ftp']} W)"
        return (f"🔬 Ramp test: best minute *{vals['best1m_w']} W*, so your FTP is "
                f"*{vals['ftp']} W*{was}. Set your bike zones from this?")
    if family == "bike":
        return (f"🔬 Bike test: threshold heart rate *{vals['lthr']} bpm*, from the last 20 "
                f"minutes. Set your bike heart-rate zones from this?")
    if family == "run":
        if (prior or {}).get("threshold_pace"):
            was = f" (Intervals.icu had {prior['threshold_pace']}/km)"
        hr = f" Threshold heart rate *{vals['lthr']} bpm*." if vals.get("lthr") else ""
        return (f"🔬 Run test: threshold pace *{vals['threshold_pace']}/km*{was}, your "
                f"average over the last 20 minutes.{hr} Set your run zones from this?")
    t4, t2 = vals.get("t400_s"), vals.get("t200_s")
    times = f"400 in {t4 // 60}:{t4 % 60:02d}, 200 in {t2 // 60}:{t2 % 60:02d}, " if t4 and t2 else ""
    if (prior or {}).get("css"):
        was = f" (Intervals.icu had {prior['css']}/100m)"
    return (f"🔬 Swim test: {times}so your CSS is *{vals['css']}/100m*{was}. "
            f"Set your swim zones from this?")


def keyboard(slug: str, family: str, vals: dict) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Set my zones", "callback_data": f"bl:yes:{slug}:{family}"},
        {"text": "❌ Test went wrong", "callback_data": f"bl:no:{slug}:{family}"},
    ]]}


def capture(slug: str, activity: dict, streams: dict, hr_trusted: bool,
            intervals: list | None = None, st: dict | None = None) -> dict | None:
    """Read a baseline test's result off its activity and park it as PENDING until
    the athlete confirms. Returns {"text", "keyboard"} to send, or None when the
    activity is not a scheduled test. Pure apart from saving the state."""
    st = load(slug) if st is None else st
    fam = match_test(st, activity)
    if not fam:
        return None
    t = st["sports_state"][fam]["test"]
    proto = t["protocol"]
    if proto == "bike_ramp":
        vals = bike_ramp_result(streams)
    elif proto == "bike_hr":
        vals = bike_hr_result(streams, hr_trusted)
    elif proto == "run_tt":
        vals = run_tt_result(streams, hr_trusted)
    else:
        vals = swim_css_result(intervals or [])
    t["activity_id"] = str(activity.get("id"))
    t["trainer"] = bool(activity.get("trainer"))
    prior = st["sports_state"][fam].get("values") or {}
    if not vals:
        if proto == "swim_css":
            t["status"] = "awaiting_times"
            save(slug, st)
            return {"text": ("🔬 Swim test done. I couldn't read the 400 and 200 cleanly off "
                             "the watch, so send me the two times, e.g. _400 in 7:10, 200 in 3:25_."),
                    "keyboard": None}
        why = ("your heart rate didn't read cleanly, so there's nothing reliable to set"
               if proto == "bike_hr" and not hr_trusted else
               "I couldn't find a ramp ridden to the end in it" if proto == "bike_ramp" else
               "I couldn't find a steady 30-minute effort in it")
        t["status"] = "unreadable"
        save(slug, st)
        return {"text": (f"🔬 That looks like your {fam} test, but {why}. Your {fam} sessions "
                         f"stay by feel for now; say when you want to redo it."),
                "keyboard": None}
    t["status"] = "result_pending"
    t["pending"] = vals
    save(slug, st)
    note = expected_note(fam, vals, st["sports_state"][fam].get("expected"))
    return {"text": result_text(fam, vals, prior) + note, "keyboard": keyboard(slug, fam, vals)}


def capture_swim_times(slug: str, text: str, st: dict | None = None) -> dict | None:
    """The athlete's typed 400/200 times, when the watch could not give them."""
    st = load(slug) if st is None else st
    t = (((st or {}).get("sports_state") or {}).get("swim") or {}).get("test") or {}
    if t.get("status") != "awaiting_times":
        return None
    times = parse_swim_times(text)
    if not times:
        return None
    vals = css_from(400, times[0], 200, times[1])
    if not vals:
        return None
    t["status"] = "result_pending"
    t["pending"] = vals
    save(slug, st)
    prior = st["sports_state"]["swim"].get("values") or {}
    note = expected_note("swim", vals, st["sports_state"]["swim"].get("expected"))
    return {"text": result_text("swim", vals, prior) + note, "keyboard": keyboard(slug, "swim", vals)}


def confirm(slug: str, family: str, client, st: dict | None = None) -> str:
    st = load(slug) if st is None else st
    t = (((st or {}).get("sports_state") or {}).get(family) or {}).get("test") or {}
    vals = t.get("pending")
    if not vals:
        return "That result has already been dealt with."
    apply(slug, family, vals, client, source="test", trainer=bool(t.get("trainer")), st=st)
    if family == "bike" and vals.get("ftp"):
        z = f"Easy rides now sit at *{round(vals['ftp'] * 0.60)}-{round(vals['ftp'] * 0.70)} W*."
    elif family == "run" and vals.get("threshold_pace"):
        z = "Your run sessions now carry pace targets."
    elif family == "swim":
        z = "Your swim sessions now carry pace targets."
    else:
        z = "Your bike zones are set."
    return f"✅ Done, zones set in Intervals.icu. {z}"


def reject(slug: str, family: str, st: dict | None = None) -> str:
    st = load(slug) if st is None else st
    t = (((st or {}).get("sports_state") or {}).get(family) or {}).get("test") or {}
    if not t.get("pending"):
        return "That result has already been dealt with."
    t.pop("pending", None)
    t["status"] = "rejected"
    save(slug, st)
    return (f"No problem, nothing changed. Your {family} sessions stay by feel until we "
            f"test again; tell me when you want to redo it.")


# ── Closing the block ───────────────────────────────────────────────────────

def tick(slug: str, today: date | None = None, st: dict | None = None) -> str | None:
    """Close the block once its window has passed. Returns the athlete message to
    send (once), else None. Tests never done are marked missed; their sports stay
    on RPE until tested."""
    today = today or date.today()
    st = load(slug) if st is None else st
    if not st or st.get("status") != "active":
        return None
    w = _window(st)
    if not w or today <= w[1]:
        return None
    lines = []
    for f in st["sports"]:
        ss = st["sports_state"].get(f) or {}
        t = ss.get("test") or {}
        if t.get("status") in ("scheduled", "unplaceable", "awaiting_times"):
            t["status"] = "missed"
        if ss.get("confidence") == "tested":
            v = ss.get("values") or {}
            num = (f"FTP {v['ftp']} W" if v.get("ftp") else
                   f"threshold {v['threshold_pace']}/km" if v.get("threshold_pace") else
                   f"CSS {v['css']}/100m" if v.get("css") else
                   f"threshold HR {v['lthr']} bpm" if v.get("lthr") else "tested")
            lines.append(f"• {LABEL[f]} {num}, zones set")
        elif t.get("status") == "result_pending":
            lines.append(f"• {LABEL[f]} result waiting for your tap above")
        else:
            lines.append(f"• {LABEL[f]} not tested yet, so {f} sessions stay by feel (RPE)")
    st["status"] = "complete"
    st["completed"] = today.isoformat()
    save(slug, st)
    return ("Baseline week done.\n" + "\n".join(lines)
            + "\n\nYour training plan starts this week. Say when you want to do any "
              "missing test and I'll put it on the calendar.")


# ── Coach prompt ────────────────────────────────────────────────────────────

def prompt_block(slug: str, first_name: str = "", today: date | None = None) -> str:
    """What the coach model must know. Empty for an established athlete."""
    st = load(slug)
    if not st:
        return ""
    today = today or date.today()
    name = first_name or "The athlete"
    conf = confidence(slug, st) or {}
    lines = []
    if st.get("status") in ("pending", "active"):
        w = _window(st)
        span = f" ({w[0]:%a %d %b} to {w[1]:%a %d %b})" if w else ""
        lines.append(
            f"BASELINE BLOCK: {name} is in their baseline block{span}: fitness tests plus "
            "easy sessions by feel, NOT a training plan. Do not build, push or replan "
            "training for these dates. Protect the test days: nothing hard the day before a "
            "test, and if a test must move, keep bike and run tests 48 h apart.")
        for f in st.get("sports", []):
            t = (st["sports_state"].get(f) or {}).get("test") or {}
            if t.get("date"):
                lines.append(f"  {f} test: {t['date']} ({t.get('status')})")
    rpe = rpe_only_families(slug, st)
    if rpe:
        guides = [g for g in (rough_guide(f, (st["sports_state"].get(f) or {}).get("expected"))
                              for f in sorted(rpe)) if g]
        lines.append(
            "UNTESTED SPORTS (prescribe by RPE only, never quote "
            + ("a zone" if guides else "a pace, power or zone")
            + " for these, and never treat an Intervals.icu estimate as the athlete's "
            "threshold): " + ", ".join(sorted(rpe)) + ".")
        if guides:
            lines.append(
                "ROUGH GUIDE from the athlete's own rough figures (not tested, not zones): "
                + "; ".join(guides) + ". You may give these beside RPE as a loose range, "
                "labelled rough; RPE decides.")
    tested = [f for f, c in conf.items() if c == "tested"]
    if tested:
        lines.append("Tested thresholds (trust these): " + ", ".join(tested) + ".")
    if st.get("status") == "complete" and rpe:
        lines.append(
            "If the athlete wants to do a missing test, book it with: python3 "
            f"ClaudeCoach/scripts/baseline-week.py book --athlete {slug} --sport <bike|run|swim> "
            "--date YYYY-MM-DD (then its result is read automatically when they do it).")
    lines.append(
        "If the athlete tells you a tested figure directly (e.g. 'my FTP test was 245'), "
        f"record it with: python3 ClaudeCoach/scripts/baseline-week.py record --athlete {slug} "
        "--sport <bike|run|swim> --value <245 | 4:30 | 1:45>.")
    return "\n".join(lines)
