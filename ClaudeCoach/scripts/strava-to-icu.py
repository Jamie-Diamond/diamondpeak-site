#!/usr/bin/env python3
"""Copy Strava workouts into Intervals.icu for athletes who connected Strava in Peak.

For watches with no direct Intervals.icu link (Apple Watch...): activities Intervals.icu
pulls from Strava itself are hidden from its API, so ClaudeCoach can't see them. This
copies each one - heart rate, pace, power, cadence, GPS, laps - as a TCX upload, which
is visible, so everything else reads it like any other activity. See lib/strava_link.py.

Runs from the VM crontab every 30 minutes. Each run, per athlete ("strava_bridge": true
in athletes.json): new workouts first, then the history, going back as far as Strava
has it, a batch at a time inside Strava's rate limits.

Never copies a workout Intervals.icu already shows (same start within 2 minutes, or
already copied). If Intervals.icu is also pulling Strava itself, a copy would count the
workout twice, so nothing is copied and the athlete is asked, once, to untick Download
activities under Strava in Intervals.icu. A workout that fails 3 runs running is
skipped (logged) so it can't hold up the rest.

    python3 ClaudeCoach/scripts/strava-to-icu.py [--athlete slug] [--dry-run]
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC / "lib"))
import outbox  # noqa: E402
from strava_client import StravaClient  # noqa: E402

try:
    import ops_log  # noqa: E402
except Exception:  # pragma: no cover
    ops_log = None

ATHLETES = CC / "config" / "athletes.json"
STATE_NAME = "strava-bridge.json"
STRAVA = "https://www.strava.com/api/v3"
ICU = "https://intervals.icu/api/v1"
PER_ATHLETE = 20            # workouts per athlete per run (2 Strava reads each)
READ_BUDGET = 120           # Strava reads per run, all athletes (app limit 200 / 15 min)
STOP_AT = (160, 1800)       # stop when Strava says this many reads used (15 min, day)
MATCH_S = 120
RUNNING = {"Run", "TrailRun", "VirtualRun"}
RIDING = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide",
          "EMountainBikeRide", "Velomobile", "Handcycle"}
STREAM_KEYS = "time,latlng,altitude,heartrate,cadence,watts,distance,velocity_smooth"
DOUBLE_MSG = ("I'm ready to copy your Strava workouts, but Intervals.icu is also pulling them "
              "from Strava itself, which would count every workout twice. In Intervals.icu, "
              "open *Settings* -> *Connections* and untick _Download activities_ under Strava. "
              "I'll start copying as soon as that's off.")


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


class Budget(Exception):
    pass


class Strava:
    """Strava reads for one athlete, counted against the run's budget."""
    used = 0

    def __init__(self, slug: str):
        self.client = StravaClient(slug)

    def get(self, path: str, params: dict | None = None, missing_ok: bool = False):
        if Strava.used >= READ_BUDGET:
            raise Budget("run budget spent")
        r = requests.get(STRAVA + path, params=params or {}, timeout=30,
                         headers={"Authorization": f"Bearer {self.client.access_token()}"})
        Strava.used += 1
        usage = r.headers.get("X-ReadRateLimit-Usage") or r.headers.get("X-RateLimit-Usage") or ""
        try:
            q, d = (int(x) for x in usage.split(",")[:2])
            if q >= STOP_AT[0] or d >= STOP_AT[1]:
                Strava.used = READ_BUDGET                     # finish this call, then stop
        except ValueError:
            pass
        if r.status_code == 429:
            raise Budget("Strava rate limit")
        if missing_ok and r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


# ── building the file ──

def _utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def _z(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def tcx_sport(sport_type: str) -> str:
    return "Running" if sport_type in RUNNING else "Biking" if sport_type in RIDING else "Other"


def build_tcx(detail: dict, streams: dict) -> bytes:
    """One Strava activity as TCX: a lap per Strava lap, a trackpoint per stream sample."""
    def data(k):
        return (streams.get(k) or {}).get("data") or []
    times = data("time")
    n = len(times)
    latlng, alt, hr, cad = data("latlng"), data("altitude"), data("heartrate"), data("cadence")
    watts, dist, vel = data("watts"), data("distance"), data("velocity_smooth")
    t0 = _utc(detail["start_date"])
    sport = tcx_sport(detail.get("sport_type") or detail.get("type") or "")

    laps = sorted((lap for lap in detail.get("laps") or []
                   if 0 <= int(lap.get("start_index", -1)) < max(n, 1)),
                  key=lambda lap: lap["start_index"])
    starts = [int(lap["start_index"]) for lap in laps] or [0]
    starts[0] = 0

    def at(seq, i):
        return seq[i] if i < len(seq) and seq[i] is not None else None

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" '
           'xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">',
           f'<Activities><Activity Sport="{sport}"><Id>{_z(t0)}</Id>']
    for k, s in enumerate(starts):
        e = (starts[k + 1] - 1) if k + 1 < len(starts) else n - 1
        lap = laps[k] if k < len(laps) else {}
        secs = lap.get("elapsed_time") or (
            (times[e] - times[s]) if n and e >= s else detail.get("elapsed_time") or 0)
        metres = lap.get("distance") if lap else (
            (dist[e] - dist[0]) if dist and e < len(dist) else detail.get("distance") or 0)
        start = t0 + timedelta(seconds=times[s] if n else 0)
        out.append(f'<Lap StartTime="{_z(start)}"><TotalTimeSeconds>{float(secs):.1f}</TotalTimeSeconds>'
                   f'<DistanceMeters>{float(metres or 0):.1f}</DistanceMeters><Calories>0</Calories>'
                   '<Intensity>Active</Intensity><TriggerMethod>Manual</TriggerMethod><Track>')
        for i in range(s, e + 1):
            p = [f"<Trackpoint><Time>{_z(t0 + timedelta(seconds=times[i]))}</Time>"]
            ll = at(latlng, i)
            if ll:
                p.append(f"<Position><LatitudeDegrees>{ll[0]}</LatitudeDegrees>"
                         f"<LongitudeDegrees>{ll[1]}</LongitudeDegrees></Position>")
            if at(alt, i) is not None:
                p.append(f"<AltitudeMeters>{alt[i]}</AltitudeMeters>")
            if at(dist, i) is not None:
                p.append(f"<DistanceMeters>{dist[i]}</DistanceMeters>")
            if at(hr, i):
                p.append(f"<HeartRateBpm><Value>{int(hr[i])}</Value></HeartRateBpm>")
            if at(cad, i) is not None and sport == "Biking":
                p.append(f"<Cadence>{int(cad[i])}</Cadence>")
            ext = []
            if at(vel, i) is not None:
                ext.append(f"<ns3:Speed>{vel[i]}</ns3:Speed>")
            if at(cad, i) is not None and sport == "Running":
                ext.append(f"<ns3:RunCadence>{int(cad[i])}</ns3:RunCadence>")
            if at(watts, i) is not None:
                ext.append(f"<ns3:Watts>{int(watts[i])}</ns3:Watts>")
            if ext:
                p.append("<Extensions><ns3:TPX>" + "".join(ext) + "</ns3:TPX></Extensions>")
            p.append("</Trackpoint>")
            out.append("".join(p))
        out.append("</Track></Lap>")
    out.append("</Activity></Activities></TrainingCenterDatabase>")
    return "\n".join(out).encode()


# ── Intervals.icu ──

class Icu:
    def __init__(self, athlete_id: str, key: str):
        self.id = athlete_id
        self.s = requests.Session()
        self.s.auth = ("API_KEY", key)

    def profile(self) -> dict:
        r = self.s.get(f"{ICU}/athlete/{self.id}", timeout=30)
        r.raise_for_status()
        return r.json()

    def activities(self, oldest: str, newest: str) -> list:
        r = self.s.get(f"{ICU}/athlete/{self.id}/activities",
                       params={"oldest": oldest, "newest": newest}, timeout=60)
        r.raise_for_status()
        return [a for a in r.json() if isinstance(a, dict)]

    def upload(self, name: str, tcx: bytes, detail: dict) -> str | None:
        params = {"name": name, "external_id": f"strava-{detail['id']}"}
        if detail.get("description"):
            params["description"] = detail["description"][:2000]
        if detail.get("device_name"):
            params["device_name"] = detail["device_name"][:80]
        r = self.s.post(f"{ICU}/athlete/{self.id}/activities", params=params, timeout=120,
                        files={"file": (f"strava-{detail['id']}.tcx.gz", gzip.compress(tcx))})
        r.raise_for_status()
        j = r.json() if r.content else {}
        acts = j.get("activities") if isinstance(j, dict) else None
        return str((acts[0] if acts else j).get("id") or "") or None

    def manual(self, detail: dict) -> str | None:
        body = {"start_date_local": str(detail["start_date_local"]).replace("Z", ""),
                "type": detail.get("sport_type") or detail.get("type") or "Workout",
                "name": detail.get("name") or "Workout",
                "moving_time": detail.get("moving_time"), "elapsed_time": detail.get("elapsed_time"),
                "distance": detail.get("distance"), "external_id": f"strava-{detail['id']}"}
        if detail.get("average_heartrate"):
            body["average_heartrate"] = detail["average_heartrate"]
        r = self.s.post(f"{ICU}/athlete/{self.id}/activities/manual", json=body, timeout=60)
        r.raise_for_status()
        return str((r.json() or {}).get("id") or "") or None

    def set_type(self, icu_id: str, detail: dict) -> None:
        body = {"type": detail.get("sport_type") or detail.get("type")}
        if detail.get("trainer"):
            body["trainer"] = True
        try:
            self.s.put(f"{ICU}/activity/{icu_id}", json=body, timeout=30).raise_for_status()
        except requests.RequestException as e:
            log(f"  type not set on {icu_id}: {e}")


# ── one athlete ──

def _load_state(adir: Path, now: int) -> dict:
    try:
        return json.loads((adir / STATE_NAME).read_text())
    except (OSError, ValueError):
        return {"newest": now, "oldest": now, "history_done": False, "copied": {},
                "warned_double": False}


def _save_state(adir: Path, st: dict) -> None:
    tmp = adir / (STATE_NAME + ".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(adir / STATE_NAME)


def _matches(summary: dict, shown: list) -> bool:
    """Intervals.icu already shows this workout (copied before, or from a watch)."""
    ext = f"strava-{summary['id']}"
    local = _utc(str(summary.get("start_date_local") or summary["start_date"]))
    for a in shown:
        if a.get("external_id") == ext:
            return True
        try:
            t = datetime.fromisoformat(str(a.get("start_date_local"))[:19]).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if abs((t - local).total_seconds()) <= MATCH_S:
            return True
    return False


def copy_one(strava: Strava, icu: Icu, summary: dict, dry: bool) -> str:
    detail = strava.get(f"/activities/{summary['id']}")
    streams = strava.get(f"/activities/{summary['id']}/streams",
                         {"keys": STREAM_KEYS, "key_by_type": "true"}, missing_ok=True)
    name = detail.get("name") or "Workout"
    if dry:
        return "dry-run"
    if streams and (streams.get("time") or {}).get("data"):
        icu_id = icu.upload(name, build_tcx(detail, streams), detail)
        sport = detail.get("sport_type") or detail.get("type") or ""
        if icu_id and (sport not in ("Run", "Ride") or detail.get("trainer")):
            icu.set_type(icu_id, detail)      # TCX only knows Running / Biking / Other
        return icu_id or "uploaded"
    return icu.manual(detail) or "manual"


def run_athlete(slug: str, a: dict, dry: bool = False) -> None:
    adir = CC / "athletes" / slug
    if not (adir / "strava_tokens.json").exists():
        log(f"[{slug}] no Strava tokens - skipped")
        return
    now = int(time.time())
    st = _load_state(adir, now)
    icu = Icu(str(a["icu_athlete_id"]), str(a["icu_api_key"]))
    if icu.profile().get("strava_sync_activities"):
        if not st.get("warned_double") and not dry:
            outbox.record(a.get("chat_id"), DOUBLE_MSG, source="strava-to-icu")
            st["warned_double"] = True
            _save_state(adir, st)
        log(f"[{slug}] waiting: Intervals.icu is still pulling Strava itself")
        return
    st["warned_double"] = False
    strava = Strava(slug)

    new = strava.get("/athlete/activities", {"after": st["newest"], "per_page": 30}) or []
    new.sort(key=lambda x: x["start_date"])
    todo = [("new", x) for x in new]
    if not st["history_done"] and len(todo) < PER_ATHLETE:
        old = strava.get("/athlete/activities", {"before": st["oldest"], "per_page": 30}) or []
        if not old:
            st["history_done"] = True
            log(f"[{slug}] history complete")
        old.sort(key=lambda x: x["start_date"], reverse=True)
        todo += [("old", x) for x in old]
    todo = todo[:PER_ATHLETE]
    if not todo:
        _save_state(adir, st)
        return

    days = sorted(str(x["start_date_local"])[:10] for _, x in todo)
    lo = (datetime.fromisoformat(days[0]) - timedelta(days=1)).date().isoformat()
    hi = (datetime.fromisoformat(days[-1]) + timedelta(days=1)).date().isoformat()
    shown = icu.activities(lo, hi)

    copied = 0
    tries = st.setdefault("tries", {})
    for kind, x in todo:
        sid, epoch = str(x["id"]), int(_utc(x["start_date"]).timestamp())
        if sid not in st["copied"]:
            if _matches(x, shown):
                st["copied"][sid] = "already there"
            else:
                try:
                    st["copied"][sid] = copy_one(strava, icu, x, dry)
                    copied += 1
                    tries.pop(sid, None)
                except Budget:
                    raise
                except Exception as e:
                    tries[sid] = tries.get(sid, 0) + 1
                    log(f"[{slug}] {sid} {x.get('name')!r} failed ({tries[sid]}/3): {e}")
                    if tries[sid] < 3:
                        if not dry:
                            _save_state(adir, st)
                        break       # try again next run, in order
                    st["copied"][sid] = f"skipped: {str(e)[:120]}"
        if kind == "new":
            st["newest"] = max(st["newest"], epoch)
        else:
            st["oldest"] = min(st["oldest"], epoch)
        if not dry:
            _save_state(adir, st)

    if not dry:
        _save_state(adir, st)
    log(f"[{slug}] copied {copied}, history {'done' if st['history_done'] else 'going'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    lock = open("/tmp/strava-to-icu.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    athletes = json.loads(ATHLETES.read_text())
    for slug, a in athletes.items():
        if not isinstance(a, dict) or not a.get("strava_bridge"):
            continue
        if args.athlete and slug != args.athlete:
            continue
        try:
            run_athlete(slug, a, args.dry_run)
        except Budget as e:
            log(f"[{slug}] stopped: {e}")
            break
        except Exception as e:
            log(f"[{slug}] failed: {e}")
            if ops_log and "401" in str(e):
                ops_log.alert("strava-to-icu", f"Strava refused {slug}'s token (disconnected?): {e}",
                              athlete=slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
