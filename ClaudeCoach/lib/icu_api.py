"""Direct Intervals.icu REST API client.

Auth: HTTP Basic with username "API_KEY" and the athlete's API key as password.
All reads can be parallelised via fetch_all().
"""

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


class IcuClient:
    def __init__(self, athlete_id: str, api_key: str):
        self.athlete_id = athlete_id
        self.session = requests.Session()
        self.session.auth = ("API_KEY", api_key)

    def _get(self, path: str, params: dict = None, base: str = None) -> dict | list:
        url = (base or f"{BASE_URL}/athlete/{self.athlete_id}") + ("/" + path.lstrip("/") if path else "")
        r = self.session.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def _activity_get(self, activity_id: str, sub: str = "") -> dict | list:
        path = f"{BASE_URL}/activity/{activity_id}"
        if sub:
            path += "/" + sub.lstrip("/")
        r = self.session.get(path, timeout=15)
        r.raise_for_status()
        return r.json()

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
        Pass newest='YYYY-MM-DD' for a future end date (taper/race projection)."""
        oldest = (date.today() - timedelta(days=days)).isoformat()
        params = {"oldest": oldest}
        if newest:
            params["newest"] = newest
        return self._get("wellness", params)

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
        description_raw: free-text coaching notes (appears before structured steps).
        day_of_week: e.g. 'Monday' — used to sanity-check date arithmetic.
        athlete_timezone: from get_athlete_profile, prevents UTC date bugs.
        """
        date_str = event_date if "T" in event_date else f"{event_date}T00:00:00"
        payload = {
            "category": "WORKOUT",
            "start_date_local": date_str,
            "type": sport,
            "name": name,
            "load_target": planned_training_load,
        }
        if description:
            payload["description"] = description
        if description_raw:
            payload["description_raw"] = description_raw
        if tags is not None:
            payload["tags"] = tags
        if athlete_timezone:
            payload["athlete_timezone"] = athlete_timezone
        payload.update(kwargs)
        return self._post("events", payload)

    def edit_workout(self, event_id: str | int, mark_as_done: bool = False,
                     **fields) -> dict:
        """Update a planned event. mark_as_done=True creates a matching manual activity."""
        payload = dict(fields)
        if mark_as_done:
            payload["mark_as_done"] = True
        return self._put(f"events/{event_id}", payload)

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
