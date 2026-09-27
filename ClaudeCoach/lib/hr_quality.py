#!/usr/bin/env python3
"""Heart-rate data quality — decide, per activity, whether the HR can be trusted.

Why this exists (27 Sep 2026). Every HR number the coach uses (decoupling, HR zone
time, HR load, "keep HR under X") assumed the sensor was right. That holds for a chest
strap and fails routinely for wrist optical HR: it locks onto run cadence, reads high
for the first minutes, drops out, and flatlines. A locked 175 bpm on an easy run reads
as a hard session and a 12% "decoupling" that never happened. Jamie's call: be better
with athletes who do not have a good HR monitor, which starts with knowing which
activities are bad.

What it looks at. The raw ICU streams for ONE activity (`IcuClient.get_activity_streams`,
passed in already keyed by type — see `streams_by_type`). Pure functions, no network:
the caller fetches.

  missing        no HR stream, or HR on under half of the moving time
  dropouts       HR null/zero for a meaningful share of moving time
  flatline       HR frozen at one value for minutes while moving (sensor lost contact
                 and the watch held the last reading)
  spikes         jumps of 20+ bpm inside 3 s — the heart does not do that, sensors do
  cadence_lock   HR sitting on the step (or pedal) cadence AND following its
                 second-to-second wiggles. Sitting on it alone is not enough: an honest
                 170 bpm tempo run at 85 rpm per foot sits on 2x cadence all day. The
                 tell is that a locked sensor tracks cadence noise; a real heart is
                 smoother than the cadence signal and does not follow it
  early_spike    the first minutes read well above the rest of the session while the
                 effort was not higher — the classic cold-wrist / dry-strap artefact
  implausible    max above the athlete's known max by a clear margin

RR intervals. ICU carries an `hrv` stream (beat-to-beat intervals) only when the sensor
sends them, which in practice means a chest or arm strap. Present on most of the
activity = `rr_present`. That is a strong hint the source is a strap, never proof the
data is clean: straps give the early-spike artefact too, so every check still runs.

Verdicts: `ok`, `suspect` (usable, say so), `bad` (exclude from HR-based decisions),
`none` (no HR at all). Swims are assessed but HR is `informational` for them: wrist HR
under water is unreliable by design and nothing should prescribe a swim by HR.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from statistics import median

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/
LOG_NAME = "hr-quality-log.json"

# Thresholds live here, in one place, so the audit report and the tests read the same
# numbers. Tuned against the live history of the three athletes on 27 Sep 2026.
MOVING_SPEED_MPS      = 0.8     # below this a run/ride sample is standing, not moving
MISSING_COVERAGE      = 0.50    # HR on < 50% of moving time = no usable HR
DROPOUT_BAD           = 0.20    # > 20% of moving time without HR = bad
DROPOUT_SUSPECT       = 0.05
FLATLINE_MIN_S        = 90      # one identical value for this long while moving
FLATLINE_BAD_TOTAL_S  = 300
SPIKE_BPM             = 20
SPIKE_WINDOW_S        = 3
SPIKES_BAD            = 5
SPIKES_SUSPECT        = 2
LOCK_TOLERANCE_BPM    = 2
LOCK_MIN_S            = 120     # need this much time on cadence before judging
LOCK_CORR             = 0.35    # HR-vs-cadence first-difference correlation
LOCK_SHARE_BAD        = 0.25    # locked share of moving time that makes it bad
EARLY_SKIP_S          = 60      # the first minute is the strap/watch settling, ignore
EARLY_WINDOW_S        = 600
EARLY_EXCESS_SUSPECT  = 10      # early median above later median, bpm
EARLY_EXCESS_BAD      = 20
EARLY_RATIO           = 1.10    # early HR-per-effort over later, to call it the sensor
MAX_OVER_BAD          = 8       # max above the athlete's known max, bpm
ABS_MAX_BPM           = 215

SWIM_TYPES = ("Swim", "OpenWaterSwim")
RUN_TYPES  = ("Run", "VirtualRun", "TrailRun", "Treadmill")
RIDE_TYPES = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide")


def streams_by_type(streams: list | None) -> dict:
    """ICU returns [{type, data, ...}, ...]; key it by type. allNull streams drop out."""
    out = {}
    for st in streams or []:
        if not isinstance(st, dict) or st.get("allNull"):
            continue
        if st.get("type") and isinstance(st.get("data"), list):
            out[st["type"]] = st["data"]
    return out


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _durations(time_s: list, n: int) -> list:
    """Seconds each sample stands for. Smart recording is irregular, so a sample's
    weight is the gap to the next one, capped so a pause does not count as effort."""
    if not time_s or len(time_s) != n:
        return [1.0] * n
    d = []
    for i in range(n):
        if i + 1 < n and _num(time_s[i]) is not None and _num(time_s[i + 1]) is not None:
            d.append(float(min(max(time_s[i + 1] - time_s[i], 0), 10)))
        else:
            d.append(1.0)
    return d


def _moving_mask(s: dict, n: int, sport: str) -> list:
    if sport in SWIM_TYPES:
        return [True] * n
    v = s.get("velocity_smooth")
    if v and len(v) == n:
        return [(_num(x) or 0) >= MOVING_SPEED_MPS for x in v]
    w = s.get("watts")
    if w and len(w) == n:
        return [(_num(x) or 0) > 0 for x in w]
    c = s.get("cadence")
    if c and len(c) == n:
        return [(_num(x) or 0) > 0 for x in c]
    return [True] * n


def _corr(a: list, b: list) -> float | None:
    n = len(a)
    if n < 20:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5


def assess(streams: dict, sport: str, athlete_max_hr: float | None = None,
           is_race: bool = False) -> dict:
    """Judge one activity's HR. `streams` is `streams_by_type(...)` output; the other
    arguments come off the ICU activity (`athlete_max_hr`, `race`)."""
    hr = streams.get("heartrate")
    time_s = streams.get("time") or []
    n = len(time_s) or len(hr or [])
    rr = streams.get("hrv")
    rr_present = bool(rr) and len(rr) == n and n > 0 and (
        sum(1 for x in rr if x) / n >= 0.5)
    result = {
        "verdict": "ok", "reasons": [], "rr_present": rr_present,
        "informational": sport in SWIM_TYPES, "metrics": {},
    }
    if not hr or n == 0 or len(hr) != n:
        result.update(verdict="none", reasons=["missing"])
        return result

    dur = _durations(time_s, n)
    moving = _moving_mask(streams, n, sport)
    moving_s = sum(d for d, m in zip(dur, moving) if m) or sum(dur)
    valid = [(_num(h) or 0) > 0 for h in hr]
    with_hr_s = sum(d for d, m, ok in zip(dur, moving, valid) if m and ok)
    coverage = with_hr_s / moving_s if moving_s else 0.0
    m = result["metrics"]
    m["moving_s"] = round(moving_s)
    m["coverage"] = round(coverage, 3)
    if coverage < MISSING_COVERAGE:
        result.update(verdict="none", reasons=["missing"])
        return result

    bad, suspect = [], []

    # Dropouts
    gap_share = 1 - coverage
    m["dropout_share"] = round(gap_share, 3)
    if gap_share > DROPOUT_BAD:
        bad.append("dropouts")
    elif gap_share > DROPOUT_SUSPECT:
        suspect.append("dropouts")

    # Flatlines: runs of one identical value while moving
    flat_total, run_val, run_s = 0.0, None, 0.0
    for h, d, mv, ok in zip(hr, dur, moving, valid):
        if ok and mv and h == run_val:
            run_s += d
            continue
        if run_s >= FLATLINE_MIN_S:
            flat_total += run_s
        run_val, run_s = (h, d) if (ok and mv) else (None, 0.0)
    if run_s >= FLATLINE_MIN_S:
        flat_total += run_s
    m["flatline_s"] = round(flat_total)
    if flat_total >= FLATLINE_BAD_TOTAL_S:
        bad.append("flatline")
    elif flat_total >= FLATLINE_MIN_S:
        suspect.append("flatline")

    # Spikes: a 20+ bpm change inside 3 s. One event per 10 s so a single jump that
    # takes three samples to settle is counted once.
    spikes, last_event = 0, None
    pts = [(_num(time_s[i]), hr[i]) for i in range(n) if valid[i] and _num(time_s[i]) is not None]
    lo = 0
    for k, (t, h) in enumerate(pts):
        while pts[lo][0] < t - SPIKE_WINDOW_S:
            lo += 1
        window = [hh for _, hh in pts[lo:k + 1]]
        if max(window) - min(window) >= SPIKE_BPM and (last_event is None or t - last_event > 10):
            spikes += 1
            last_event = t
    m["spikes"] = spikes
    if spikes >= SPIKES_BAD:
        bad.append("spikes")
    elif spikes >= SPIKES_SUSPECT:
        suspect.append("spikes")

    # Cadence lock (runs and rides; swims have no meaningful cadence stream)
    cad = streams.get("cadence")
    if sport not in SWIM_TYPES and cad and len(cad) == n:
        lock_s, d_hr, d_cad = 0.0, [], []
        mult = (1, 2) if sport in RUN_TYPES else (1,)
        prev = None
        for i in range(n):
            c, h = _num(cad[i]), hr[i]
            if not (moving[i] and valid[i] and c and c > 0):
                prev = None
                continue
            k = next((k for k in mult if abs(h - k * c) <= LOCK_TOLERANCE_BPM), None)
            if k is None:
                prev = None
                continue
            lock_s += dur[i]
            if prev is not None and prev[0] == k:
                d_hr.append(h - prev[1])
                d_cad.append(k * c - prev[2])
            prev = (k, h, k * c)
        corr = _corr(d_hr, d_cad) if lock_s >= LOCK_MIN_S else None
        lock_share = lock_s / moving_s if moving_s else 0.0
        m["on_cadence_share"] = round(lock_share, 3)
        m["lock_corr"] = None if corr is None else round(corr, 2)
        if corr is not None and corr >= LOCK_CORR:
            (bad if lock_share >= LOCK_SHARE_BAD else suspect).append("cadence_lock")

    # Early spike: the first minutes read above the rest of the session while the
    # effort was not higher. Compared as HR-per-unit-effort, because a real heart LAGS
    # at the start (ratio lower than later), so a start that reads higher per watt or
    # per m/s than the steady part is the sensor, not the athlete. Medians on both
    # sides: a p90-vs-median comparison flagged every ride with a few early surges.
    # Races skip it: a bike leg straight out of the swim starts high for real.
    t0 = _num(time_s[0]) or 0
    def _win(lo_s, hi_s):
        return [i for i in range(n) if valid[i] and moving[i]
                and lo_s < (_num(time_s[i]) or 0) - t0 <= hi_s]
    early_i = _win(EARLY_SKIP_S, EARLY_WINDOW_S)
    later_i = _win(EARLY_WINDOW_S + 300, float("inf"))
    if not is_race and len(early_i) >= 60 and len(later_i) >= 300:
        excess = median(hr[i] for i in early_i) - median(hr[i] for i in later_i)
        m["early_excess_bpm"] = round(excess, 1)
        effort = streams.get("watts") if sport in RIDE_TYPES else streams.get("velocity_smooth")
        ratio_up = None
        if effort and len(effort) == n:
            e_e = median((_num(effort[i]) or 0) for i in early_i)
            e_l = median((_num(effort[i]) or 0) for i in later_i)
            if e_e > 0 and e_l > 0:
                r_e = median(hr[i] for i in early_i) / e_e
                r_l = median(hr[i] for i in later_i) / e_l
                ratio_up = r_e / r_l
                m["early_hr_per_effort"] = round(ratio_up, 2)
        # Without an effort stream the excess alone can only ever make it suspect.
        confirmed = ratio_up is not None and ratio_up >= EARLY_RATIO
        if excess >= EARLY_EXCESS_BAD and confirmed:
            bad.append("early_spike")
        elif excess >= EARLY_EXCESS_SUSPECT and (confirmed or ratio_up is None):
            suspect.append("early_spike")

    # Implausible max
    hmax = max(hr[i] for i in range(n) if valid[i])
    m["max_hr"] = hmax
    if hmax > ABS_MAX_BPM or (athlete_max_hr and hmax > athlete_max_hr + MAX_OVER_BAD):
        bad.append("implausible_max")

    if bad:
        result["verdict"] = "bad"
    elif suspect:
        result["verdict"] = "suspect"
    result["reasons"] = bad + [r for r in suspect if r not in bad]
    return result


def trusted(result: dict | None) -> bool:
    """Whether HR from this activity may drive a decision. Unknown = not trusted."""
    return bool(result) and result.get("verdict") in ("ok", "suspect") \
        and not result.get("informational")


def assess_activity(client, activity: dict) -> dict:
    """Fetch one ICU activity's streams and judge its HR. Network: one GET."""
    s = streams_by_type(client.get_activity_streams(activity["id"]))
    return assess(s, activity.get("type") or "", activity.get("athlete_max_hr"),
                  bool(activity.get("race")))


# ── Per-athlete log ─────────────────────────────────────────────────────────
# athletes/<slug>/hr-quality-log.json, keyed by ICU activity id. Written by the
# activity watcher as each activity lands and by scripts/hr-quality-audit.py for
# history. Consumers ask `untrusted_ids(slug)`: an activity NOT in the log is treated
# exactly as before this module existed (trusted), so a missing log never changes a
# number, it only fails to improve one.

def log_path(slug: str) -> Path:
    return BASE / "athletes" / slug / LOG_NAME


def load_log(slug: str) -> dict:
    try:
        data = json.loads(log_path(slug).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record(slug: str, activity: dict, result: dict, log: dict | None = None,
           save: bool = True) -> dict:
    """Add one activity's verdict to the log (atomically, unless save=False so a
    backfill can write once at the end). Returns the log."""
    log = load_log(slug) if log is None else log
    log[str(activity["id"])] = {
        "date":          (activity.get("start_date_local") or "")[:10],
        "type":          activity.get("type"),
        "name":          activity.get("name"),
        "device":        activity.get("device_name"),
        "verdict":       result.get("verdict"),
        "reasons":       result.get("reasons", []),
        "rr_present":    result.get("rr_present"),
        "informational": result.get("informational", False),
        "metrics":       result.get("metrics", {}),
        "assessed":      date.today().isoformat(),
    }
    if save:
        save_log(slug, log)
    return log


def save_log(slug: str, log: dict) -> None:
    path = log_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".hrq-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(log, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def untrusted_ids(slug: str, log: dict | None = None) -> set:
    """ICU activity ids whose HR must not drive a decision (verdict bad or none).
    Swims are not in here by default: their HR is informational everywhere already."""
    log = load_log(slug) if log is None else log
    return {aid for aid, r in log.items() if r.get("verdict") in ("bad", "none")}


def is_untrusted(slug: str, activity_id, log: dict | None = None) -> bool:
    return str(activity_id) in untrusted_ids(slug, log)


# ── Declared HR source ──────────────────────────────────────────────────────
# Asked at onboarding (profile.json `hr_source`). It sets the prior; the per-activity
# verdicts above are the evidence. Absent = an athlete from before 27 Sep 2026, who
# keeps exactly today's behaviour.

HR_SOURCES = ("strap", "wrist", "mixed", "none")


def hr_source(profile: dict | None) -> str | None:
    v = (profile or {}).get("hr_source")
    return v if v in HR_SOURCES else None


def prompt_block(profile: dict | None) -> str:
    """Coach-prompt rule for an athlete whose HR cannot carry a prescription. Empty
    for a strap user or an athlete who was never asked."""
    src = hr_source(profile)
    if src == "none":
        return ("HEART RATE: this athlete trains WITHOUT a heart-rate monitor. Never "
                "prescribe, cap or judge a session by HR, and never ask for HR. Use power "
                "(bike), pace (run, swim) and RPE.")
    if src in ("wrist", "mixed"):
        return ("HEART RATE: this athlete's HR is from a wrist sensor"
                + (" some of the time" if src == "mixed" else "")
                + ". Prescribe by power (bike), pace (run, swim) or RPE, never by HR. "
                "HR may be quoted as a loose ceiling only when the activity's HR check "
                "passed; if the watcher says HR is untrusted, do not cite HR, HR zones or "
                "decoupling for that session.")
    return ""


REASON_TEXT = {
    "missing":         "no heart rate recorded",
    "dropouts":        "heart rate dropped out",
    "flatline":        "heart rate froze on one number",
    "spikes":          "sudden jumps no heart makes",
    "cadence_lock":    "heart rate locked onto your step cadence",
    "early_spike":     "reading high in the first minutes",
    "implausible_max": "a max above anything you have hit before",
}


def describe(result: dict) -> str:
    """Short plain-English reason, for athlete-facing text."""
    rs = [REASON_TEXT.get(r, r) for r in (result or {}).get("reasons", [])]
    return "; ".join(rs)
