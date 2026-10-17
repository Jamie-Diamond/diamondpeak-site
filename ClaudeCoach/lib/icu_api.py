"""Direct Intervals.icu REST API client.

Auth, either:
  - HTTP Basic with username "API_KEY" and the athlete's API key as password, or
  - "Authorization: Bearer <token>" for an athlete linked through Peak's Connect
    Intervals.icu (OAuth, lib/icu_link.py). Pass token=, or pass no key and the token
    stored for that athlete id is found. A key, when there is one, still wins.
All reads can be parallelised via fetch_all().

Every call is counted, one line per request in
~/Library/Logs/ClaudeCoach/icu-requests/<UTC date>.log (scripts/icu-request-count.py
sums it), because Intervals.icu's coming rate limits count per athlete per UTC day.
"""

import os
import sys
import time
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta


BASE_URL = "https://intervals.icu/api/v1"

# Standard durations used by get_best_efforts for Ride/Run power
POWER_DURATIONS = {5: "5s", 60: "1min", 300: "5min", 1200: "20min", 3600: "60min"}

# Training summary columns — enough to aggregate volume, TSS, and zone time
_SUMMARY_COLS = ",".join([
    "id", "start_date_local", "type", "sport_label",
    "distance", "moving_time", "total_elevation_gain",
    "icu_training_load", "icu_zone_times", "icu_hr_zone_times",
    "average_watts", "average_heartrate",
])


def _wellness_row_finalized(row: dict) -> bool:
    """True once Intervals.icu has stopped recomputing this day's CTL/ATL/Form —
    i.e. the row's `updated` timestamp has moved to a later date than the row's
    own date. Until then CTL/ATL/Form/TSB on this row can still shift as that
    day's activities finish syncing, independent of whether any single activity
    already carries a Load figure."""
    row_date = (row.get("id") or "")[:10]
    updated = (row.get("updated") or "")[:10]
    return bool(row_date and updated and updated > row_date)


def drop_orphan_resting_hr(rows: list) -> list:
    """Jamie, 15 Oct 2026: from 31 Jul to 5 Sep his Garmin sent no HRV or sleep, and the
    resting HR on those days swung 40-76: all-day readings, not overnight ones. For an
    athlete whose watch normally sends HRV (on half or more of the days with a resting
    HR), a resting HR on a day with no HRV AND no sleep is moved to `restingHR_raw` and
    left out, so Trends, the morning card and the coach's baseline skip it. Athletes
    without HRV (no overnight data at all) keep every value. Intervals.icu is untouched."""
    with_rhr = [r for r in rows if r.get("restingHR")]
    if not with_rhr or sum(1 for r in with_rhr if r.get("hrv")) * 2 < len(with_rhr):
        return rows
    for r in with_rhr:
        if not r.get("hrv") and not r.get("sleepSecs"):
            r["restingHR_raw"] = r.pop("restingHR")
    return rows


_COUNT_DIR = os.path.expanduser(os.environ.get("CC_ICU_COUNT_DIR", "~/Library/Logs/ClaudeCoach/icu-requests"))
_CALLER = os.path.basename(sys.argv[0] or "") or "python"


def count_request(athlete_id: str, auth: str, r) -> None:
    """One line per Intervals.icu call: time, athlete, key/oauth, caller, method, path,
    status, and the rate-limit headers once Intervals.icu sends them. Never raises."""
    if os.environ.get("CC_ICU_COUNT", "1") == "0" or not os.path.isdir(os.path.dirname(_COUNT_DIR)):
        return
    try:
        os.makedirs(_COUNT_DIR, exist_ok=True)
        path = (getattr(r.request, "path_url", "") or "").split("?")[0]
        left = r.headers.get("X-RateLimit-Remaining", "-")
        line = (f"{time.strftime('%H:%M:%S', time.gmtime())} {athlete_id} {auth} {_CALLER} "
                f"{r.request.method} {path} {r.status_code} {left}\n")
        f = os.path.join(_COUNT_DIR, time.strftime("%Y-%m-%d", time.gmtime()) + ".log")
        if not os.path.exists(f):                      # first call of a UTC day: keep 21 days
            cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 21 * 86400))
            for old in os.listdir(_COUNT_DIR):
                if old.endswith(".log") and old[:10] < cutoff:
                    os.remove(os.path.join(_COUNT_DIR, old))
        fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except Exception:
        pass


# Read-once cache for one batch job (refresh-site-data.py): the same GET asked twice in
# one run is answered from memory. Its builders asked for the same wellness up to 8 times
# per athlete per run (15 Oct 2026). Off unless a job turns it on; writes are never cached.
_RUN_CACHE: dict | None = None


def enable_run_cache() -> None:
    global _RUN_CACHE
    _RUN_CACHE = {}


def _cached_get(session, url: str, params: dict | None):
    import copy
    key = (url, tuple(sorted((params or {}).items())))
    if _RUN_CACHE is not None and key in _RUN_CACHE:
        return copy.deepcopy(_RUN_CACHE[key])
    r = session.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if _RUN_CACHE is not None:
        _RUN_CACHE[key] = copy.deepcopy(data)
    return data


NOTES_MARK = "Notes"


def pace_floor_guard(sport: str, description: str) -> None:
    """Refuse a run or swim step slower than a sane floor, from ANY source (Jamie, 17 Oct
    2026). For four months the renderer pushed run warm-ups at 56-64% of threshold pace
    and swim warm-ups at 58-66% of CSS, and a model writing description text by hand can
    do the same, so the check sits here, on the two calls every calendar write goes
    through. Raises primitives.planned_tss.PaceFloorError (a ValueError) before anything
    is sent. The floors and the parser live with the bands they protect."""
    if not description:
        return
    try:
        from primitives.planned_tss import assert_pace_floor
    except ImportError:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ironman-analysis"))
        from primitives.planned_tss import assert_pace_floor
    assert_pace_floor(sport or "", description)


def with_notes(description: str, notes: str) -> str:
    """The event text Intervals.icu keeps: steps first, then the coaching notes.

    Intervals.icu stores only `description`. `description_raw` is accepted and dropped,
    with or without steps (checked live 17 Oct 2026), so every planner and coach note -
    fuel on long rides, how to ride it - reached nobody. Notes go after the steps under a
    "Notes" line, so a heading in them can't start a repeat block; a line starting "-"
    would be read as a step, so it becomes a bullet. A previous notes block is replaced."""
    steps = (description or "").split("\n\n" + NOTES_MARK + "\n")[0].rstrip()
    lines = []
    for ln in (notes or "").strip().splitlines():
        t = ln.strip()
        lines.append("\u2022 " + t.lstrip("-* ").strip() if t[:1] in "-*" else ln.rstrip())
    body = "\n".join(lines).strip()
    if not body:
        return steps
    return (steps + "\n\n" if steps else "") + NOTES_MARK + "\n" + body


class IcuClient:
    def __init__(self, athlete_id: str, api_key: str = "", token: str | None = None):
        self.athlete_id = athlete_id
        self.session = requests.Session()
        if not token and not api_key:
            try:
                import icu_link
                token = icu_link.token_for_athlete(athlete_id)
            except Exception:
                token = None
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
            self.auth = "oauth"
        else:                         # no key either: Intervals.icu answers 401, as before
            self.session.auth = ("API_KEY", api_key)
            self.auth = "key"
        self.session.hooks["response"].append(
            lambda r, *a, **k: count_request(str(athlete_id), self.auth, r))

    def _get(self, path: str, params: dict = None, base: str = None) -> dict | list:
        url = (base or f"{BASE_URL}/athlete/{self.athlete_id}") + ("/" + path.lstrip("/") if path else "")
        return _cached_get(self.session, url, params)

    def _activity_get(self, activity_id: str, sub: str = "") -> dict | list:
        path = f"{BASE_URL}/activity/{activity_id}"
        if sub:
            path += "/" + sub.lstrip("/")
        return _cached_get(self.session, path, None)

    def _put(self, path: str, payload: dict) -> dict:
        url = f"{BASE_URL}/athlete/{self.athlete_id}/{path.lstrip('/')}"
        r = self.session.put(url, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{BASE_URL}/athlete/{self.athlete_id}/{path.lstrip('/')}"
        r = self.session.post(url, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> None:
        url = f"{BASE_URL}/athlete/{self.athlete_id}/{path.lstrip('/')}"
        r = self.session.delete(url, timeout=15)
        r.raise_for_status()

    # ── Read endpoints ──────────────────────────────────────────────────────

    def get_athlete_profile(self) -> dict:
        return self._get("")

    def get_sport_settings(self, sport: str = None) -> list | dict:
        """Sport config including FTP, indoor_ftp, power/HR/pace zones.
        Pass sport='Ride' to get just that sport's dict. Omit for all sports."""
        if sport:
            return self._get(f"sport-settings/{sport}")
        return self._get("sport-settings")

    def get_fitness(self, days: int = 28, newest: str = None) -> list:
        """CTL, ATL, TSB (rampRate). Supports future `newest` for taper projection."""
        return self.get_wellness(days=days, newest=newest)

    def get_wellness(self, days: int = 7, newest: str = None) -> list:
        """Daily CTL/ATL/TSB plus HRV, sleep, weight, restingHR, subjective scores.
        Pass newest='YYYY-MM-DD' for a future or past end date.
        Each row is annotated with `wellness_finalized` — False means this day's
        CTL/ATL/Form is still recalculating on Intervals.icu and must not be
        quoted as final (see _wellness_row_finalized)."""
        anchor = date.fromisoformat(newest) if newest else date.today()
        oldest = (anchor - timedelta(days=days)).isoformat()
        params = {"oldest": oldest}
        if newest:
            params["newest"] = newest
        rows = self._get("wellness", params)
        for row in rows:
            row["wellness_finalized"] = _wellness_row_finalized(row)
        return drop_orphan_resting_hr(rows)

    def get_training_history(self, days: int = 30, sport: str = None) -> list:
        """Completed activities. sport filters e.g. 'Ride', 'Run', 'Swim'."""
        oldest = (date.today() - timedelta(days=days)).isoformat()
        params = {"oldest": oldest}
        if sport:
            params["type"] = sport
        return self._get("activities", params)

    def get_events(self, start: str, end: str, category: str = None) -> list:
        """Planned calendar events. category: WORKOUT, RACE, NOTE, HOLIDAY."""
        params = {"oldest": start, "newest": end}
        if category:
            params["category"] = category
        return self._get("events", params)

    def get_event(self, event_id: str | int) -> dict:
        """One planned calendar event."""
        return self._get(f"events/{event_id}")

    def get_activity_detail(self, activity_id: str) -> dict:
        """Full activity data. Response includes garmin_attribution — display it as
        a footer whenever showing this data to the athlete (API terms requirement)."""
        return self._activity_get(activity_id)

    def get_extended_metrics(self, activity_id: str) -> dict:
        """Per-interval metrics: power, HR, pace, zone distribution, decoupling.
        Use for biomechanics questions, drift analysis, W' balance, DFA alpha1."""
        return self._activity_get(activity_id, "intervals")

    def get_activity_streams(self, activity_id: str) -> list:
        """Raw time-series streams: watts, HR, cadence, lat/lng, altitude, HRV, etc."""
        return self._activity_get(activity_id, "streams")

    def get_best_efforts(self, sport: str = "Ride", period: str = "1y") -> dict:
        """Best efforts for key durations/distances.

        For Ride and Run (with power): mean-maximal power at 5s, 1min, 5min, 20min, 60min
        derived from power curves.
        period: '4w', '6w', '8w', '3m', '6m', '1y' (default), '2y', 'all',
                or 'r.YYYY-MM-DD.YYYY-MM-DD' for a custom range.

        Returns dict with 'power' key (and 'wkg' key) mapping duration label → watts.
        For Run distance bests (5k, 10k etc.) call get_training_history and filter
        icu_achievements — they are attached per-activity.
        """
        curves_map = {"4w": "28d", "6w": "42d", "8w": "56d",
                      "3m": "90d", "6m": "180d", "1y": "1y",
                      "2y": "2y", "all": "all"}
        curves_param = curves_map.get(period, period)
        data = self._get("power-curves", {"type": sport, "curves": curves_param})
        curve_list = data.get("list", [])
        if not curve_list:
            return {}
        c = curve_list[0]
        secs = c.get("secs", [])
        watts = c.get("watts", [])
        wkg = c.get("watts_per_kg", [])
        if not secs or not watts:
            return {"label": c.get("label"), "vo2max_5m": c.get("vo2max_5m")}
        result = {"label": c.get("label"), "power": {}, "wkg": {}, "vo2max_5m": c.get("vo2max_5m")}
        for target_s, label in POWER_DURATIONS.items():
            if target_s in secs:
                idx = secs.index(target_s)
                result["power"][label] = watts[idx]
                if wkg:
                    result["wkg"][label] = round(wkg[idx], 2)
        return result

    def get_power_curves(self, sport: str = "Ride", curves: str = "90d") -> dict:
        """Full mean-maximal power curve data.
        sport: 'Ride' or 'Run'
        curves: '90d', '1y', 's0' (current season), 's1' (last season),
                'all', or 'r.YYYY-MM-DD.YYYY-MM-DD' for custom range."""
        return self._get("power-curves", {"type": sport, "curves": curves})

    def get_training_summary(self, oldest: str, newest: str, sport: str = None) -> dict:
        """Aggregate training stats for a date range: volume, TSS, zone times, weekly breakdown.
        Computed from raw activities — equivalent to IcuSync's get_training_summary."""
        params = {"oldest": oldest, "newest": newest, "cols": _SUMMARY_COLS}
        if sport:
            params["type"] = sport
        activities = self._get("activities", params)

        by_sport: dict = defaultdict(lambda: {"count": 0, "distance_m": 0,
                                               "moving_time_s": 0, "tss": 0})
        by_week: dict = defaultdict(lambda: {"distance_m": 0, "moving_time_s": 0, "tss": 0})
        total_tss = 0
        power_zone_secs: dict = defaultdict(int)  # {"Z1": secs, ...}
        hr_zone_secs: list = []

        for a in activities:
            stype = a.get("sport_label") or a.get("type") or "Other"
            dist = a.get("distance") or 0
            move = a.get("moving_time") or 0
            tss = a.get("icu_training_load") or 0
            total_tss += tss
            by_sport[stype]["count"] += 1
            by_sport[stype]["distance_m"] += dist
            by_sport[stype]["moving_time_s"] += move
            by_sport[stype]["tss"] += tss
            start = a.get("start_date_local", "")[:10]
            if start:
                d = date.fromisoformat(start)
                week_start = (d - timedelta(days=d.weekday())).isoformat()
                by_week[week_start]["distance_m"] += dist
                by_week[week_start]["moving_time_s"] += move
                by_week[week_start]["tss"] += tss
            for zone in (a.get("icu_zone_times") or []):
                power_zone_secs[zone["id"]] += zone.get("secs", 0)
            hr_arr = a.get("icu_hr_zone_times") or []
            while len(hr_zone_secs) < len(hr_arr):
                hr_zone_secs.append(0)
            for i, t in enumerate(hr_arr):
                hr_zone_secs[i] += t

        return {
            "oldest": oldest,
            "newest": newest,
            "total_tss": round(total_tss),
            "activity_count": len(activities),
            "by_sport": dict(by_sport),
            "by_week": dict(sorted(by_week.items())),
            "power_zone_times_s": dict(power_zone_secs),
            "hr_zone_times_s": hr_zone_secs,
        }

    # ── Write endpoints ─────────────────────────────────────────────────────

    def push_workout(self, sport: str, event_date: str, name: str,
                     description: str = "", description_raw: str = "",
                     planned_training_load: int = 0,
                     day_of_week: str = None, athlete_timezone: str = None,
                     tags: list = None, **kwargs) -> dict:
        """Push a workout to the calendar.

        description: structured workout text (IcuSync format — parsed into zone charts).
            e.g. 'Main Set 4x\\n- 8m Z4 Pace intensity=interval\\n- 2m Z1 Pace intensity=recovery'
        description_raw: free-text coaching notes. Intervals.icu drops this field, so it is
            written into description after the steps (with_notes).
        day_of_week: e.g. 'Monday' — used to sanity-check date arithmetic.
        athlete_timezone: from get_athlete_profile, prevents UTC date bugs.
        """
        pace_floor_guard(sport, description)     # raises before anything is sent
        date_str = event_date if "T" in event_date else f"{event_date}T00:00:00"
        # A day the athlete moved indoors (lib/indoor.py, Peak's Ride indoors button)
        # stays indoors when that day is planned again.
        try:
            import indoor as _indoor
            sport = _indoor.sticky_type(self.athlete_id, sport, date_str[:10])
        except Exception:
            pass
        payload = {
            "category": "WORKOUT",
            "start_date_local": date_str,
            "type": sport,
            "name": name,
            "load_target": planned_training_load,
        }
        text = with_notes(description, description_raw) if description_raw else description
        if text:
            payload["description"] = text
        if tags is not None:
            payload["tags"] = tags
        if athlete_timezone:
            payload["athlete_timezone"] = athlete_timezone
        payload.update(kwargs)
        return self._post("events", payload)

    def edit_workout(self, event_id: str | int, mark_as_done: bool = False,
                     **fields) -> dict:
        """Update a planned event. NOTE: mark_as_done does NOT create an activity
        (verified 5 Jul 2026) — use create_manual_activity for that."""
        payload = dict(fields)
        # Only a description the CALLER sends is checked: a notes-only or type-only edit
        # must not be refused over steps it does not touch (plan_audit flags those).
        if payload.get("description"):
            sport = payload.get("type") or (self.get_event(event_id).get("type") or "")
            pace_floor_guard(sport, payload["description"])
        if "description_raw" in payload:          # dropped by Intervals.icu: see with_notes
            raw = payload.pop("description_raw") or ""
            base = payload["description"] if "description" in payload else (self.get_event(event_id).get("description") or "")
            payload["description"] = with_notes(base, raw)
        if mark_as_done:
            payload["mark_as_done"] = True
        return self._put(f"events/{event_id}", payload)

    def edit_activity(self, activity_id: str | int, **fields) -> dict:
        """Update a completed activity (e.g. name, description)."""
        url = f"{BASE_URL}/activity/{activity_id}"
        r = self.session.put(url, json=fields, timeout=15)
        r.raise_for_status()
        return r.json()

    def create_manual_activity(self, sport: str, start_date_local: str, name: str,
                               moving_time_s: int, training_load: int,
                               description: str = "") -> dict:
        """Create a MANUAL activity (source=MANUAL, no file) so non-device
        training (gym / CrossFit) feeds real CTL/ATL. Verified live 5 Jul 2026."""
        return self._post("activities/manual", {
            "type": sport, "start_date_local": start_date_local, "name": name,
            "moving_time": int(moving_time_s), "icu_training_load": int(training_load),
            "description": description})

    def put_activity_streams(self, activity_id: str | int, streams: list) -> dict:
        """Write streams into a completed activity, e.g. [{"type": "heartrate", "data": [...]}].
        Intervals.icu recomputes HR metrics at once (verified live 2 Oct 2026)."""
        url = f"{BASE_URL}/activity/{activity_id}/streams"
        r = self.session.put(url, json=streams, timeout=30)
        r.raise_for_status()
        return r.json()

    def delete_activity(self, activity_id: str | int) -> None:
        """Delete a completed activity (undo path for log-strength mistakes)."""
        url = f"{BASE_URL}/activity/{activity_id}"
        r = self.session.delete(url, timeout=15)
        r.raise_for_status()

    def delete_workout(self, event_id: str | int) -> None:
        self._delete(f"events/{event_id}")

    # ── Parallel fetch ──────────────────────────────────────────────────────

    def fetch_all(self, *specs) -> list:
        """Fetch multiple endpoints in parallel.

        Each spec is either a method name string or a (method_name, *args) tuple.
        Returns results in the same order as specs.

        Example:
            profile, wellness, history, events, sport = client.fetch_all(
                "get_athlete_profile",
                ("get_wellness", 14),
                ("get_training_history", 14),
                ("get_events", "2026-05-11", "2026-06-01"),
                "get_sport_settings",
            )
        """
        resolved = []
        for spec in specs:
            if isinstance(spec, str):
                resolved.append((spec, ()))
            else:
                resolved.append((spec[0], spec[1:]))

        results = [None] * len(resolved)
        with ThreadPoolExecutor(max_workers=max(len(resolved), 1)) as pool:
            futures = {
                pool.submit(getattr(self, name), *args): idx
                for idx, (name, args) in enumerate(resolved)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
        return results
