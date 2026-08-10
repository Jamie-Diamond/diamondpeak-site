#!/usr/bin/env python3
"""
Pull live data from Intervals.icu, write the FULL PRIVATE training data to disk,
then write and publish an ALLOW-LIST SANITISED public variant to GitHub Pages.

Two output tiers, and the distinction matters:

  PRIVATE (gitignored, never published)
    athletes/jamie/training-data.json        full payload, incl. weight/HRV/RHR
    training-data-{slug}.json                full payload per other athlete

  PUBLIC (tracked, served at diamondpeak.uk)
    public/training-data-{slug}.json         allow-list subset only

The public variant is built by lib/public_sanitise.py, which copies across ONLY
the fields named in its spec. It replaced a deny-list (_strip_private) that
published everything it had not been told to pop; that is how body weight, HRV
and resting HR were served publicly from 8 May to 28 Jul 2026. The filenames are
deliberately different so a private file can never be mistaken for a public one.

Run daily (e.g. 06:00 via launchd/cron). Requires git push credentials (SSH key or keychain).
"""
import json, re, subprocess, sys, time, math
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import defaultdict

BASE             = Path(__file__).parent.parent          # ClaudeCoach/
OUT_FILE         = BASE / "athletes/jamie/training-data.json"  # full private copy (gitignored)
PUBLIC_DIR       = BASE / "public"                             # sanitised, tracked, published
GIT_PUSH         = BASE / "scripts/cc-git-commit-push.sh"

sys.path.insert(0, str(BASE / "lib"))
from public_sanitise import sanitise_training_data, write_public_json
PROJECT_DIR      = str(BASE.parent)                        # diamondpeak-site/
LOCK_FILE        = BASE / ".refresh_site_data.lock"
CLAUDE           = "/usr/bin/claude"
ATHLETES_CONFIG  = BASE / "config/athletes.json"

HEAT_LOG          = BASE / "athletes/jamie/heat-log.json"
DECOUPLING_LOG    = BASE / "athletes/jamie/decoupling-log.json"
STATE_JSON        = BASE / "athletes/jamie/current-state.json"
SESSION_LOG       = BASE / "athletes/jamie/session-log.json"
SWIM_LOG          = BASE / "athletes/jamie/swim-log.json"
FITNESS_PREV_CACHE = BASE / "athletes/jamie/fitness-prev-cache.json"

RACE_DATE = date(2026, 9, 19)
PLAN_START = date(2026, 4, 27)  # Week 1 Monday

_POWER_DURATIONS = [
    (5,"5s"),(10,"10s"),(30,"30s"),(60,"1m"),(120,"2m"),(300,"5m"),
    (600,"10m"),(1200,"20m"),(1800,"30m"),(3600,"60m"),(5400,"90m"),(7200,"2h"),
    # Out to race duration. The curve stopped at 2h, which is under half the target
    # bike split - so the one duration that actually decides the race was the one
    # number missing (Jamie, 6 Aug 2026). Intervals.icu already returns these: its
    # curve runs to 18300s on a 300s grid, so nothing new is fetched for them.
    (9000,"2h30"),(10800,"3h"),(12600,"3h30"),(14400,"4h"),(16200,"4h30"),
]

# intervals.icu samples its long-duration curve every 300s, so a race target must be
# snapped to that grid or the lookup silently misses and the row reads "—".
_PC_GRID = 300


def _race_bike_seconds(profile) -> int | None:
    """Target bike split in seconds, snapped UP to the intervals.icu curve grid.

    Derived from race_targets.bike_time rather than hard-coded so the curve's top row
    follows the target if it is revised. Jamie's 4:44 becomes 17100s = 4h45.
    """
    raw = str(((profile or {}).get("race_targets") or {}).get("bike_time") or "").strip()
    m = re.match(r"^~?\s*(\d+):(\d{2})(?::(\d{2}))?$", raw)
    if not m:
        return None
    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)
    if secs <= 7200:
        return None
    return -(-secs // _PC_GRID) * _PC_GRID       # ceil to the grid


def _power_durations(profile):
    """_POWER_DURATIONS plus the race-duration row, when there is a target to anchor it."""
    race = _race_bike_seconds(profile)
    if not race or any(t == race for t, _ in _POWER_DURATIONS):
        return list(_POWER_DURATIONS)
    h, m = divmod(race // 60, 60)
    label = f"{h}h{m:02d} · race" if m else f"{h}h · race"
    return [d for d in _POWER_DURATIONS if d[0] < race] + [(race, label)]


def _eftp_from_fitness(fitness_rows: list) -> int | None:
    """Extract eFTP (W) for Ride from the most recent fitness row's sportInfo. Returns None if unavailable."""
    row = fitness_rows[-1] if fitness_rows else {}
    for s in (row.get("sportInfo") or []):
        if s.get("type") == "Ride" and s.get("eftp"):
            return int(s["eftp"])
    return None


def _has_recent_ftp_test(session_log_path: Path, weeks: int = 10) -> bool:
    """Return True if a named FTP test appears in session-log.json within the last N weeks."""
    if not session_log_path.exists():
        return False
    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
    keywords = ("ramp", "ftp test", "20 min", "20-min", "threshold test")
    try:
        for e in json.loads(session_log_path.read_text()):
            if e.get("date", "") >= cutoff and e.get("sport") in ("Ride", "VirtualRide"):
                if any(kw in (e.get("name") or "").lower() for kw in keywords):
                    return True
    except Exception:
        pass
    return False


def _resolve_ftp(profile_ftp: int | None, fitness_rows: list, session_log_path: Path) -> int:
    """Use profile FTP if a confirmed test exists in last 10 weeks, else ICU eFTP, else profile."""
    if not _has_recent_ftp_test(session_log_path):
        eftp = _eftp_from_fitness(fitness_rows)
        if eftp:
            return eftp
    return profile_ftp or 250


def _prev_race_date(slug):
    """The athlete's OWN previous-season race date, or None.

    Both season-overlay windows used to end on a hard-coded 19 September, which is
    Jamie's 2026 race date and belongs to neither prior season of anybody. It caused
    two separate faults (both reported 6 Aug 2026):

      * Jamie's 2025 line stopped on 2025-09-19 when he raced on the 20th, so every
        prior season sat one day short of race day - the "old races are a day out".
      * Kathryn's per-sport prior season was fetched over Jamie's window entirely
        (2025-01-01 to 2025-09-19) when her last race was 2024-10-20, so aligning it
        on her race date threw the line ~a year off the axis and it vanished. That is
        the missing Marathonas 70.3.

    prev_race.date is preferred over prev_race_date because the flat field is null for
    Jamie while the nested one is populated.
    """
    try:
        prof = json.loads((BASE / f"athletes/{slug}/profile.json").read_text())
    except Exception:
        return None
    raw = ((prof.get("prev_race") or {}).get("date")) or prof.get("prev_race_date")
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def fetch_fitness_prev(client):
    """Fetch last season's CTL series once and cache it. Skips if cache exists."""
    if FITNESS_PREV_CACHE.exists():
        return
    log("Fetching last-season fitness (one-time cache)...")
    race = _prev_race_date("jamie")
    if not race:
        log("fitnessPrev skipped: jamie has no previous race date in profile")
        return
    # get_fitness takes (days, newest), NOT (start_date, end_date). The old call passed
    # the latter and raised TypeError every time, caught by the except below and logged
    # as "non-fatal" - so this one-time cache had in fact NEVER been built by this
    # script. Jamie's existed only because build-fitness-prev-cache.py was run by hand,
    # and Kathryn's still does; a permanently broken call hidden behind a soft failure
    # is why nobody noticed. `days` is a span back from `newest`, so Jan 1 to race day
    # is (race - Jan 1).days.
    season_start = date(race.year, 1, 1)
    try:
        rows = client.get_fitness(days=(race - season_start).days,
                                  newest=race.isoformat())
        series = [[r["id"][:10], round(r.get("ctl") or 0, 1)] for r in rows if r.get("ctl")]
        FITNESS_PREV_CACHE.write_text(json.dumps(series))
        log(f"fitnessPrev cached: {len(series)} days")
    except Exception as e:
        log(f"fitnessPrev fetch failed (non-fatal): {e}")


def _focus_sports(slug):
    """The athlete's focus sports, from their blueprint. ["swim","bike","run"] for the
    triathletes, ["bike"] for Calum's sportive.

    The app offers a per-athlete override in Settings; this is the default it starts
    from, so the switch opens on the right answer rather than on all three for someone
    who only rides. Strength and everything else is still logged and still shown - a
    sport not being a FOCUS is not the same as it not counting.
    """
    try:
        bp = json.loads((BASE / f"athletes/{slug}/reference/training-blueprint.json").read_text())
    except Exception:
        return None
    sports = bp.get("sports")
    return sports if isinstance(sports, list) and sports else None


def _zone_distribution(slug, activities, today):
    """Time in zones by sport against the blueprint phase target, or None.

    Costs no API call: get_training_history already returns full activity objects,
    zone arrays included. See lib/zone_distribution.py for why the bands are read from
    the blueprint row rather than bucketed with a fixed easy/z3/z45 table.
    """
    try:
        sys.path.insert(0, str(BASE / "lib"))
        import zone_distribution
        return zone_distribution.build_for_slug(BASE, slug, activities, today)
    except Exception as e:
        log(f"[{slug}] zone distribution skipped (non-fatal): {e}")
        return None


NP_CURVE_CACHE = BASE / "athletes/jamie/np-curve-cache.json"
_BIKE_TYPES = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide")

# Streams are the expensive call in this estate, so activities are converted in batches
# and the curve publishes from whatever is cached rather than waiting for a complete set.
# Measured at 6 rides in about a second, so the original ceiling of 6 was far too timid:
# it spread an 80-ride backfill over 13 runs, and because every run then changed the
# published np values it produced 13 commits and 13 GitHub Pages builds. Pages builds
# here take longer than the 10-minute refresh interval, so each one cancels the last and
# the queue times out - churn in this number is not free. 20 finishes the backfill in
# about four runs and still keeps a single refresh well under a few seconds.
_NP_MAX_NEW_PER_RUN = 20


def _np_curves(client, recent_acts, durations, today, p_from, p_to):
    """Mean-maximal NP for the 90-day window and the same 90 days a year earlier.

    Returns (np_now, np_prev, pending) where pending is the number of rides still
    without a cached curve - published so the app can say "still building" rather than
    presenting a half-populated column as a complete one.
    """
    sys.path.insert(0, str(BASE / "lib"))
    from np_curve import load_cache, save_cache, refresh_cache, best_over

    cache = load_cache(NP_CURVE_CACHE)
    if not isinstance(cache, dict):
        cache = {}

    def _rides(acts, lo, hi):
        out = []
        for a in (acts or []):
            if a.get("type") not in _BIKE_TYPES:
                continue
            day = (a.get("start_date_local") or "")[:10]
            if day and lo.isoformat() <= day <= hi.isoformat():
                out.append(a)
        return out

    now_rides = _rides(recent_acts, today - timedelta(days=90), today)

    # The year-ago rides are outside the 120-day history this job already holds, so
    # they need their own activity-list call. That list is historical and cannot
    # change, so it is cached by window and fetched once rather than every 10 minutes.
    prev_key = f"{p_from.isoformat()}..{p_to.isoformat()}"
    stash = cache.get("_prev_list")
    if isinstance(stash, dict) and stash.get("key") == prev_key:
        prev_rides = stash.get("acts") or []
    else:
        prev_rides = []
        try:
            raw = client._get("activities", {"oldest": p_from.isoformat(),
                                             "newest": p_to.isoformat()})
            prev_rides = [{"id": a.get("id"), "type": a.get("type"),
                           "moving_time": a.get("moving_time"),
                           "start_date_local": a.get("start_date_local")}
                          for a in _rides(raw, p_from, p_to)]
            cache["_prev_list"] = {"key": prev_key, "acts": prev_rides}
        except Exception as e:
            log(f"NP curve: year-ago activity list failed (non-fatal): {e}")

    dset = set(durations)
    todo = now_rides + prev_rides
    cache, fetched = refresh_cache(client, todo, cache, durations,
                                   max_new=_NP_MAX_NEW_PER_RUN, log=log)
    if fetched:
        save_cache(NP_CURVE_CACHE, cache)

    pending = sum(1 for a in todo
                  if not isinstance(cache.get(str(a.get("id"))), dict))
    if pending:
        log(f"NP curve: {fetched} computed this run, {pending} rides still pending")

    ids_now = [a.get("id") for a in now_rides]
    ids_prev = [a.get("id") for a in prev_rides]
    return (best_over(cache, ids_now, dset),
            best_over(cache, ids_prev, dset),
            pending or None)


def _compute_per_sport_ctl(activities, start, today):
    """Per-sport CTL = 42-day EWMA of each sport's daily TSS, day by day from `start`
    to `today`. Returns {sport: [[date, ctl], ...]} for the three endurance sports."""
    SPORTS = ("Ride", "Run", "Swim")
    def _ctl_sport(raw):
        # broader than _sport_normalise: catch every swim/bike/run variant
        # (OpenWaterSwim, VirtualRide, GravelRide, TrailRun, ...) so none are dropped.
        r = (raw or "").lower()
        if "swim" in r:                                                   return "Swim"
        if "run"  in r:                                                   return "Run"
        if "ride" in r or "cycl" in r or "bik" in r or "velomobile" in r: return "Ride"
        return None
    daily = {s: {} for s in SPORTS}
    for a in activities or []:
        d  = (a.get("start_date_local") or "")[:10]
        sp = _ctl_sport(a.get("type"))
        if not d or sp is None:
            continue
        daily[sp][d] = daily[sp].get(d, 0.0) + float(a.get("icu_training_load") or 0)
    series = {}
    for s in SPORTS:
        ctl, out, cur = 0.0, [], start
        while cur <= today:
            ds = cur.isoformat()
            ctl += (daily[s].get(ds, 0.0) - ctl) / 42.0
            out.append([ds, round(ctl, 1)])
            cur += timedelta(days=1)
        series[s] = out
    return series


def _today_fingerprint(activities, today) -> str:
    """id:load for every activity dated `today`, so the per-sport cache can tell
    "already computed today" from "already computed today AND nothing has changed
    since". Includes load, so an ICU re-analysis that revises a load also busts it.
    """
    d = today.isoformat()
    return ",".join(sorted(
        f"{a.get('id')}:{a.get('icu_training_load')}"
        for a in (activities or [])
        if (a.get("start_date_local") or "")[:10] == d))


def _per_sport_ctl_cached(slug, client, today, activities=None):
    """Per-sport CTL for the fitness-by-sport chart. Returns
    {"current": {sport: series}, "prev": {sport: series}, "prev2": {sport: series}}.
    Current season is recomputed at most once a day (the season activity fetch is heavy
    and refresh runs after every activity). The prior seasons are historical, so they are
    fetched once (a single heavier pull back to the earliest one) and cached forever.
    `prev` = the season of the athlete's own prev_race (Jan 1 -> that race, matching
    fitnessPrev); `prev2` = the season of profile.prev2_race_date (Jan 1 -> that race),
    so it lines up with the Barcelona '23 overlay. Both windows are per-ATHLETE: they
    were briefly a shared hard-coded date, which is a bug the module header of
    lib/zone_distribution.py would call inventing data. `prev2` is {} for athletes with
    no prev2_race_date."""
    cache_f = BASE / f"athletes/{slug}/fitness-bysport-cache.json"
    cache = {}
    try:
        cache = json.loads(cache_f.read_text())
    except Exception:
        pass

    prev2_race = None
    try:
        prev2_race = json.loads((BASE / f"athletes/{slug}/profile.json").read_text()).get("prev2_race_date")
    except Exception:
        pass

    # Current season. Recomputed once a day PLUS whenever today's activities change
    # (4 Aug 2026): "once a day" meant the 06:20 run built the series before Jamie's
    # 07:03 swim, every later refresh reused that cache, and his swim CTL sat visibly
    # DECLINING on a day he had swum — the chart said detraining while the swim was
    # already in `recent` on the same page. Moving the cron to */10 could not fix it.
    # `activities` is the history the caller has already fetched, so the freshness
    # check costs no extra API call; without it the old date-only behaviour stands.
    fp = _today_fingerprint(activities, today) if activities is not None else None
    current = cache.get("current") or {}
    stale = (fp is not None and cache.get("today_fp") != fp)
    if cache.get("date") != today.isoformat() or not current or stale:
        start = date(today.year, 1, 1)
        try:
            current = _compute_per_sport_ctl(
                client.get_training_history((today - start).days + 1), start, today)
        except Exception as e:
            log(f"[{slug}] per-sport CTL (current) failed: {e}")

    # Prior seasons — one-time, cached. One fetch back to the earliest season we need,
    # then split into per-season windows.
    prev  = cache.get("prev")
    prev2 = cache.get("prev2")
    need_prev2 = bool(prev2_race)
    if not prev or (need_prev2 and not prev2):
        # The athlete's OWN last race, Jan 1 of that race's year to race day - NOT
        # date(today.year - 1, 9, 19), which was Jamie's 2026 race date applied to
        # every athlete and every prior season. See _prev_race_date().
        p1race = _prev_race_date(slug)
        if p1race:
            p1s, p1e = date(p1race.year, 1, 1), p1race
        else:
            # No recorded prior race: fall back to the calendar year before this one,
            # whole, rather than to a date borrowed from someone else's season.
            p1s, p1e = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
            log(f"[{slug}] no previous race date — per-sport prior season "
                f"falls back to {p1s.year} entire")
        p2s = p2e = None
        earliest = p1s
        if need_prev2:
            try:
                p2e = date.fromisoformat(prev2_race[:10]); p2s = date(p2e.year, 1, 1)
                earliest = min(earliest, p2s)
            except Exception:
                need_prev2 = False
        try:
            acts = client.get_training_history((today - earliest).days + 1)
            def _win(s, e):
                return [a for a in (acts or [])
                        if s.isoformat() <= (a.get("start_date_local") or "")[:10] <= e.isoformat()]
            prev = _compute_per_sport_ctl(_win(p1s, p1e), p1s, p1e)
            prev2 = _compute_per_sport_ctl(_win(p2s, p2e), p2s, p2e) if need_prev2 else {}
        except Exception as e:
            log(f"[{slug}] per-sport CTL (prior seasons) failed: {e}")
            prev = prev or {}; prev2 = prev2 or {}

    out = {"current": current, "prev": prev or {}, "prev2": prev2 or {}}
    try:
        cache_f.write_text(json.dumps({"date": today.isoformat(),
                                       "today_fp": fp, **out}))
    except Exception:
        pass
    return out


def _build_jamie_data(client) -> dict:
    """Fetch Jamie's training data via IcuClient — replaces the old Claude+MCP approach."""
    today         = date.today()
    # 120 days: the calendar shows whole months and lets you page back, so a 14-day
    # window meant most of every month had no data at all.
    HISTORY_DAYS  = 120
    fourteen_ago  = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    seven_ago     = (today - timedelta(days=7)).isoformat()
    twentyone_fwd = (today + timedelta(days=21)).isoformat()

    wellness_60, history_21, events_21, fitness_ytd = client.fetch_all(
        ("get_wellness", 60),
        ("get_training_history", HISTORY_DAYS),
        ("get_events", today.isoformat(), twentyone_fwd),
        ("get_fitness", (today - date(today.year, 1, 1)).days + 1),
    )

    # KPI
    kpi = {}
    if wellness_60:
        w      = wellness_60[-1]
        ctl    = round(w.get("ctl") or 0, 1)
        atl    = round(w.get("atl") or 0, 1)
        ramp7d = round(ctl - round(wellness_60[-8].get("ctl") or 0, 1), 1) if len(wellness_60) >= 8 else 0.0
        kpi    = {"ctl": ctl, "atl": atl, "tsb": round(ctl - atl, 1), "ramp7d": ramp7d,
                  "hrv": w.get("hrv"), "rhr": w.get("restingHR")}

    # fitnessThis
    fitness_this = [[w["id"][:10], round(w.get("ctl") or 0, 1)] for w in fitness_ytd if w.get("ctl")]

    # recent (last HISTORY_DAYS, newest first)
    recent = []
    for a in sorted([x for x in history_21 if x.get("start_date_local","")[:10] >= fourteen_ago],
                    key=lambda x: x.get("start_date_local",""), reverse=True):
        sport  = _sport_normalise(a.get("type","Other"))
        dist_m = a.get("distance") or 0
        dur_s  = a.get("moving_time") or 0
        avg_p  = a.get("average_watts")
        norm_p = a.get("icu_weighted_avg_watts")
        recent.append({
            "_aid":   a.get("id"),   # removed by route_shape.attach_shapes
            "date":   a.get("start_date_local","")[:10],
            "sport":  sport,
            "name":   a.get("name",""),
            "dur":    round(dur_s / 60),
            "dist":   round(dist_m / 1000, 2) if dist_m else None,
            "pace":   _format_pace(sport, dist_m, dur_s),
            "hr":     int(a["average_heartrate"]) if a.get("average_heartrate") else None,
            "powAvg": int(avg_p)  if avg_p  else None,
            "powNp":  int(norm_p) if norm_p else None,
            "tss":    int(a.get("icu_training_load") or 0),
        })

    # weekCalendar (last 7 days completed + next 21 days planned)
    completed_by_date: dict = defaultdict(list)
    for a in history_21:
        d = a.get("start_date_local","")[:10]
        if d >= seven_ago:
            completed_by_date[d].append(a)

    week_calendar = []
    for a in sorted(history_21, key=lambda x: x.get("start_date_local","")):
        d = a.get("start_date_local","")[:10]
        if d < seven_ago:
            continue
        sport  = _sport_normalise(a.get("type","Other"))
        dist_m = a.get("distance") or 0
        dur_s  = a.get("moving_time") or 0
        tss    = int(a.get("icu_training_load") or 0)
        avg_p  = a.get("average_watts")
        norm_p = a.get("icu_weighted_avg_watts")
        if sport == "Ride":
            detail = " · ".join(filter(None, [
                f"NP {int(norm_p)}W" if norm_p else None,
                f"HR {int(a['average_heartrate'])}" if a.get("average_heartrate") else None,
                f"{dist_m/1000:.1f}km" if dist_m else None,
            ]))
        elif sport in ("Run","Swim"):
            detail = " · ".join(filter(None,[_format_pace(sport,dist_m,dur_s),
                                              f"{dist_m/1000:.1f}km" if dist_m else None]))
        else:
            detail = ""
        week_calendar.append({"date":d,"sport":sport,"name":a.get("name",""),
                               "tss":tss,"duration_min":round(dur_s/60),
                               "status":"completed","key":tss>=60,"detail":detail})

    completed_dates = set(completed_by_date.keys())
    for ev in events_21:
        ev_date = (ev.get("start_date_local") or "")[:10]
        if not ev_date or ev_date < today.isoformat():
            continue
        ev_sport = _sport_normalise(ev.get("type") or ev.get("sport_type") or "Other")
        if any(_sport_normalise(a.get("type","")) == ev_sport
               for a in completed_by_date.get(ev_date,[])):
            continue
        # Prefer the workout-computed load; load_target can be a stale/manual over-estimate.
        ev_tss = ev.get("icu_training_load") or ev.get("load") or ev.get("load_target")
        ev_dur = ev.get("moving_time") or ev.get("duration")
        week_calendar.append({"date":ev_date,"sport":ev_sport,"name":ev.get("name",""),
                               "tss":int(ev_tss) if ev_tss else None,
                               "duration_min":round(int(ev_dur)/60) if ev_dur else None,
                               "status":"planned","key":bool(ev_tss and int(ev_tss)>=60),"detail":""})
    week_calendar.sort(key=lambda x: x["date"])

    # loadChart (today−7 to today+7, 15 days)
    tsb_by_date = {w.get("id","")[:10]: round((w.get("ctl") or 0)-(w.get("atl") or 0),1)
                   for w in wellness_60 if w.get("id")}
    load_chart = []
    for i in range(-7, 8):
        d    = (today + timedelta(days=i)).isoformat()
        acts = [{"sport":_sport_normalise(a.get("type","Other")),
                 "tss":int(a.get("icu_training_load") or 0),
                 "dur":round((a.get("moving_time") or 0)/60),
                 "status":"completed"}
                for a in history_21 if a.get("start_date_local","")[:10]==d]
        if i >= 0:   # include TODAY's planned sessions, not just future days
            for ev in events_21:
                ev_d     = (ev.get("start_date_local") or "")[:10]
                ev_sport = _sport_normalise(ev.get("type") or ev.get("sport_type") or "Other")
                if ev_d != d or any(a["sport"]==ev_sport for a in acts):
                    continue
                # Prefer the workout-computed load (icu_training_load); load_target can be a stale/
                # manual over-estimate that doesn't match the prescribed (e.g. Z2) structure.
                ev_tss = ev.get("icu_training_load") or ev.get("load") or ev.get("load_target")
                ev_dur = ev.get("moving_time") or ev.get("duration")
                acts.append({"sport":ev_sport,"tss":int(ev_tss) if ev_tss else None,
                              "dur":round(int(ev_dur)/60) if ev_dur else None,"status":"planned"})
        load_chart.append({"date":d,"tsb":tsb_by_date.get(d),"activities":acts})

    # Forward-project TSB for future loadChart days via Banister EMA decay
    if kpi.get("ctl") is not None and kpi.get("atl") is not None:
        _pctl, _patl = float(kpi["ctl"]), float(kpi["atl"])
        for _entry in load_chart:
            if _entry["date"] > today.isoformat():
                _day_tss = sum((a.get("tss") or 0) for a in _entry.get("activities", [])
                               if a.get("status") == "planned")
                _pctl += (_day_tss - _pctl) / 42.0
                _patl += (_day_tss - _patl) / 7.0
                _entry["tsb"] = round(_pctl - _patl, 1)
                _entry["projected"] = True

    # weightTrend (last 30 days where weight not null)
    weight_trend = [{"date":w.get("id","")[:10],"kg":w["weight"]}
                    for w in wellness_60 if w.get("weight")]

    # Route outlines. Shape only, normalised into a unit box - see lib/route_shape.py
    # for why nothing positional is published.
    try:
        from route_shape import attach_shapes
        _n = attach_shapes(client, BASE, "jamie", recent, log=log)
        if _n:
            log(f"[jamie] route shapes: {_n} newly fetched")
    except Exception as e:
        log(f"[jamie] route shapes skipped (non-fatal): {e}")
        for _r in recent:
            _r.pop("_aid", None)

    # Power curve: best efforts at standard durations over the last 90 days, against
    # THE SAME 90 DAYS ONE YEAR EARLIER.
    #
    # wPrev used to be hard-coded None, so the "Last season" column was permanently
    # blank (Jamie, 3 Aug 2026). The obvious fix - curves="s1", last season entire -
    # would be misleading: a best-of-a-full-season beats a best-of-90-days almost by
    # construction, so every row would read as a regression. Comparing the same 90
    # calendar days a year apart is like-for-like, and the same point in the season.
    #
    # The window is published (powerCurveWindow) because "Now vs Last season" over an
    # unstated period is not a number anyone can act on.
    power_curve = []
    power_curve_window = None
    try:
        _pc_profile = json.loads((BASE / "athletes/jamie/profile.json").read_text())
    except Exception:
        _pc_profile = {}
    pc_durations = _power_durations(_pc_profile)
    try:
        pc_raw = client.get_power_curves(sport="Ride", curves="90d")
        if pc_raw.get("list"):
            curve     = pc_raw["list"][0]
            secs_to_w = dict(zip(curve.get("secs", []), curve.get("values", [])))

            prev_w = {}
            # Shift by 365 days rather than replace(year=...): the latter raises
            # ValueError on 29 Feb, which would take the whole nightly refresh down
            # one day every four years.
            p_from = today - timedelta(days=90 + 365)
            p_to   = today - timedelta(days=365)
            try:
                prev_raw = client.get_power_curves(
                    sport="Ride", curves=f"r.{p_from.isoformat()}.{p_to.isoformat()}")
                if prev_raw.get("list"):
                    pcurve = prev_raw["list"][0]
                    prev_w = dict(zip(pcurve.get("secs", []), pcurve.get("values", [])))
                else:
                    log("Power curve: no year-ago data in range — wPrev left blank")
            except Exception as e:
                # Non-fatal and NAMED: a blank column with no log line is how this
                # sat broken for months.
                log(f"Power curve year-ago fetch failed (non-fatal): {e}")

            # Normalised power at each duration. NOT available from this endpoint -
            # /power-curves is mean-maximal AVERAGE power only, and passing
            # powerField=np or np=true returns the identical average curve without
            # erroring, which is exactly how you would ship a mislabelled column.
            # So NP is computed from the power streams, cached per activity.
            np_now, np_prev, np_ready = {}, {}, None
            try:
                np_now, np_prev, np_ready = _np_curves(
                    client, history_21, [t for t, _ in pc_durations],
                    today, p_from, p_to)
            except Exception as e:
                log(f"NP curve skipped (non-fatal): {e}")

            power_curve = [{"t": t, "label": lbl,
                            "w": secs_to_w.get(t), "wPrev": prev_w.get(t),
                            "np": np_now.get(t), "npPrev": np_prev.get(t)}
                           for t, lbl in pc_durations]
            power_curve_window = {
                "days": 90,
                "now_from": (today - timedelta(days=90)).isoformat(),
                "now_to": today.isoformat(),
                "prev_from": p_from.isoformat(),
                "prev_to": p_to.isoformat(),
                "label": "best 90 days vs same 90 days last year",
                "np_basis": "30s-rolling 4th-power mean over every window of that "
                            "length, best across all rides in the period",
                "np_pending": np_ready,
            }
    except Exception as e:
        log(f"Power curve fetch failed (non-fatal): {e}")

    # Resolve FTP here while raw fitness rows are in scope; post_process (which builds
    # the profile block) cannot see fitness_ytd, so it reads this stashed value instead.
    _prof_f = BASE / "athletes/jamie/profile.json"
    _prof_ftp = None
    if _prof_f.exists():
        try:
            _prof_ftp = json.loads(_prof_f.read_text()).get("ftp_watts")
        except Exception:
            pass
    resolved_ftp = _resolve_ftp(_prof_ftp, fitness_ytd, SESSION_LOG)

    return {
        "generated":    today.isoformat(),
        "kpi":          kpi,
        "fitnessThis":  fitness_this,
        "fitnessBySport": _per_sport_ctl_cached("jamie", client, today, history_21),
        "recent":       recent,
        "weekCalendar": week_calendar,
        "loadChart":    load_chart,
        "weightTrend":  weight_trend,
        "powerCurve":   power_curve,
        "powerCurveWindow": power_curve_window,
        "resolvedFtp":  resolved_ftp,
        "rampCap":      _ramp_cap("jamie"),
        "refreshCadence": _refresh_cadence(),
        "sports":       _focus_sports("jamie"),
        "zoneDistribution": _zone_distribution("jamie", history_21, today),
    }


def _refresh_cadence(cron_lines=None) -> str | None:
    """This job's own schedule, in words, read from crontab.

    Hard-coding it in the app is what produced "nightly, 06:20" still being shown
    hours after the schedule became two-hourly. Derived from the crontab entry that
    actually runs this file, so it cannot drift from reality; returns None on any
    doubt, and the app then says nothing rather than something wrong.
    """
    try:
        if cron_lines is None:                      # injectable so it is testable
            out = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                                 timeout=10)
            if out.returncode != 0:
                return None
            cron_lines = out.stdout.splitlines()
        me = Path(__file__).name
        for line in cron_lines:
            line = line.strip()
            if line.startswith("#") or me not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            minute, hour = parts[0], parts[1]
            # A step minute (*/10) is the whole schedule when the hour is unrestricted:
            # this returned None the moment the job went to */10 on 4 Aug 2026, so the
            # app silently stopped stating its own cadence.
            if minute.startswith("*/") and minute[2:].isdigit() and hour == "*":
                n = int(minute[2:])
                return "every minute" if n == 1 else f"every {n} minutes"
            if not minute.isdigit():
                return None
            mm = int(minute)
            if hour == "*":
                return f"hourly, at {mm:02d} past"
            hours = []
            for chunk in hour.split(","):
                if chunk.isdigit():
                    hours.append(int(chunk))
                elif "/" in chunk:          # e.g. */2 or 6-22/2
                    base, _, step = chunk.partition("/")
                    if not step.isdigit():
                        return None
                    lo, hi = 0, 23
                    if "-" in base:
                        a, _, b = base.partition("-")
                        if a.isdigit() and b.isdigit():
                            lo, hi = int(a), int(b)
                    hours = list(range(lo, hi + 1, int(step)))
                else:
                    return None
            if not hours:
                return None
            hours = sorted(set(hours))
            if len(hours) == 1:
                return f"daily at {hours[0]:02d}:{mm:02d}"
            gaps = {b - a for a, b in zip(hours, hours[1:])}
            span = f"{hours[0]:02d}:{mm:02d}\u2013{hours[-1]:02d}:{mm:02d}"
            if len(gaps) == 1:
                g = gaps.pop()
                unit = "hour" if g == 1 else "hours"
                return f"every {g} {unit}, {span}"
            return f"{len(hours)}\u00d7 daily, {span}"
    except Exception:
        return None
    return None


def _ramp_cap(slug: str) -> float:
    """The athlete's weekly CTL ramp guide (max_ctl_ramp_per_week), default 5.0.

    Published so the app can state the actual target instead of hard-coding 5, which
    would be wrong for any athlete configured differently.
    """
    try:
        cfg = json.loads(ATHLETES_CONFIG.read_text()).get(slug, {})
        return float(cfg.get("max_ctl_ramp_per_week", 5.0))
    except Exception:
        return 5.0


def write_public_variant(data, slug):
    """Write the ALLOW-LIST sanitised public file for one athlete.

    Returns the repo-relative path on success, or None (having logged) on
    failure. Never raises: a sanitiser refusal must not lose the private write
    that has already happened, and must not publish a partial payload either.

    This replaced _strip_private(), a deny-list that popped four known-bad keys
    and published the rest. See lib/public_sanitise.py for why an allow-list is
    the only safe shape here.
    """
    out = PUBLIC_DIR / ("training-data-%s.json" % slug)
    try:
        write_public_json(sanitise_training_data(data), out)
    except Exception as exc:
        log("[%s] PUBLIC WRITE REFUSED - %s" % (slug, exc))
        return None
    log("[%s] wrote %s (%d bytes, allow-listed)" % (slug, out.name, out.stat().st_size))
    return "ClaudeCoach/public/training-data-%s.json" % slug


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}")


def _heat_accl_series(slug):
    """Daily heat-acclimation series for the athlete page chart, or None.

    Recomputed from heat-log.json by lib/heat.acclimation_series on every run —
    the score is never stored, so the chart cannot drift from the model the
    prescription and watchdog use.
    """
    try:
        sys.path.insert(0, str(BASE / "lib"))
        import heat as heat_lib
        return heat_lib.acclimation_series(slug)
    except Exception as e:
        log(f"[{slug}] heatAccl series skipped: {e}")
        return None


def _ctl_project(start_ctl, daily_tss_fn, days):
    """Project CTL forward using exponential decay: CTL_new = CTL + (TSS - CTL) / 42."""
    ctl = start_ctl
    series = []
    today = date.today()
    for i in range(days):
        d = today + timedelta(days=i)
        tss = daily_tss_fn(d)
        ctl = ctl + (tss - ctl) / 42.0
        series.append({"date": d.isoformat(), "ctl": round(ctl, 1)})
    return series


def _ctl_target_milestones(athlete_cfg, current_ctl, today):
    """Phase CTL milestones the planner is aiming for — the SINGLE SOURCE shared by
    the Jamie (post_process) and generic athlete paths, so the site target line can
    never drift from the plan. Uses configured ctl_targets.phase_ctl when present
    (e.g. Jamie), else derives from race_min (mirrors plan_tools exactly, e.g.
    Kathryn/Calum). Returns a list of {date,ctl,label} or None if no CTL basis."""
    ct = athlete_cfg.get("ctl_targets") or {}
    pt = athlete_cfg.get("phase_tss") or {}
    plan_start_str = athlete_cfg.get("plan_start")
    race_str = athlete_cfg.get("race_date")
    if not (plan_start_str and race_str and (ct.get("phase_ctl") or ct.get("race_min"))):
        return None
    plan_start = date.fromisoformat(plan_start_str)
    race_dt    = date.fromisoformat(race_str)
    ends = {"base": pt.get("base_end_week", 6), "build": pt.get("build_end_week", 10),
            "specific": pt.get("specific_end_week", 14), "peak": pt.get("peak_end_week", 17)}
    phase_ctl = ct.get("phase_ctl")
    if phase_ctl:
        derived = {k: phase_ctl.get(k) for k in ("base", "build", "specific", "peak")}
    else:
        sys.path.insert(0, str(BASE / "ironman-analysis"))
        from primitives.load import derive_phase_ctl_targets
        derived = derive_phase_ctl_targets(
            current_ctl, int(ct["race_min"]), plan_start,
            ends["base"], ends["build"], ends["specific"], ends["peak"],
            float(athlete_cfg.get("max_ctl_ramp_per_week", 5.0)),
            float(athlete_cfg.get("taper_overshoot", 1.15)), today=today)
    ms = {}
    for label, key in (("End Base", "base"), ("End Build", "build"),
                       ("Specific", "specific"), ("Peak", "peak")):
        if derived.get(key) is None:
            continue
        md = plan_start + timedelta(weeks=ends[key])
        # First-write wins so collapsed phases keep the earlier, clearer label.
        if today <= md <= race_dt and md.isoformat() not in ms:
            ms[md.isoformat()] = {"date": md.isoformat(), "ctl": derived[key], "label": label}
    race_ctl = ct.get("race_min") or derived.get("peak")
    if race_ctl is not None:
        ms[race_dt.isoformat()] = {"date": race_dt.isoformat(),
                                   "ctl": int(race_ctl), "label": "Race day"}
    return sorted(ms.values(), key=lambda m: m["date"]) or None


# Race predictor moved to lib/race_predictor.py (5 Jul 2026) so /race and the
# chat path (plan_tools.py race-predict) run the SAME model as this cron script.
sys.path.insert(0, str(BASE / "lib"))
from race_predictor import race_predictor as _race_predictor  # noqa: E402


def _phase_daily_tss_projection(d):
    """Daily TSS for the CTL PROJECTION CURVES ONLY — never as a plan target.

    RENAMED 28 Jul 2026 (was `_phase_daily_tss`). This table is a FOURTH set of
    weekly numbers, hard-coded here and blind to the athlete's config: it knows
    nothing about manual_easy_weeks, deload_skip_weeks, phase_tss boundaries or the
    CTL the athlete actually has. Used as a plan target it declared 854 TSS for the
    week of 20 Jul 2026, a week the planning engine had deliberately cut to ~505 for
    the Dorney B-race taper — so an exactly-executed taper rendered as a 24% miss.
    planVsActual now reads the engine's own target (see _weekly_plan_targets).

    It survives ONLY as the "what if he trained a generic phase block" shape for
    _ctl_project(), which takes a date->daily-TSS function and evolves CTL itself;
    the engine cannot substitute there without changing that contract. The name says
    projection so no future surface can quietly reuse it as a target again.

    Calibrated to 2025 actuals (spring ~110/day, peak ~133/day).
    Projects peak CTL ~123, race-day CTL ~105 from current ~79 — exceeding 2025.
    - Base (wk 1-6):    105/day = 735/wk
    - Build (wk 7-10):  112/day = 784/wk
    - Specific (wk 11-14): 122/day = 854/wk
    - Peak (wk 15-18):  135/day = 945/wk  (4 weeks, ends ~Aug 30)
    - Taper (wk 19+):    75/day = 525/wk  (matches 2025 actual ~76/day)"""
    week = max(1, math.ceil((d - PLAN_START).days / 7))
    if week <= 6:    return 105   # Base: ~735/wk
    if week <= 10:   return 112   # Build: ~784/wk
    if week <= 14:   return 122   # Specific: ~854/wk
    if week <= 18:   return 135   # Peak: ~945/wk
    return 75                     # Taper: ~525/wk — matches 2025 actual


def _ctl_on(ctl_by_date, d):
    """CTL as at date `d` — exact day if present, else the most recent earlier day.
    A past week must be judged against the fitness the athlete HAD when it was
    planned, not against today's."""
    ds = d.isoformat()
    if ds in ctl_by_date:
        return ctl_by_date[ds]
    earlier = [k for k in ctl_by_date if k <= ds]
    return ctl_by_date[max(earlier)] if earlier else None


def _weekly_plan_targets(cfg, week_starts, ctl_by_date, weekly_actual):
    """{week_start: {planned_tss, week_type, week_num}} from the PLANNING ENGINE.

    plan_tools.required_tss is the single source the CLI, the weekly brief, the plan
    audit and plan_builder all already agree on: phase CTL target -> required weekly
    TSS, capped by the ramp, then reduced by the deload / manual-easy-week / taper
    branches. Feeding it each week's own CTL and the prior week's executed load
    reproduces the target that week was actually built to, including the reductions
    that were the POINT of the week — which the old hard-coded phase table could not
    represent, so every correctly-executed down-week read as a compliance miss.

    week_num comes from the engine's `training_week` too: the ETL's own
    ceil(days/7) formula ran one week ahead of the engine's days//7+1 from day 8 of
    a week onward, so the chart labelled weeks differently from every other surface.

    Returns {} rather than raising if the athlete has no CTL basis or plan_start —
    the caller then simply omits the series instead of inventing a target.
    """
    targets = {}
    try:
        sys.path.insert(0, str(BASE / "lib"))
        sys.path.insert(0, str(BASE / "ironman-analysis"))
        from plan_tools import required_tss
    except Exception as e:
        log(f"planVsActual: planning engine unavailable ({e}) — series omitted")
        return targets
    for ws in week_starts:
        ctl = _ctl_on(ctl_by_date, ws)
        if not ctl:
            continue
        prev = weekly_actual.get((ws - timedelta(days=7)).isoformat())
        try:
            r = required_tss(cfg, float(ctl), today=ws, last_week_tss=prev)
        except Exception as e:
            log(f"planVsActual: required_tss failed for {ws} ({e})")
            continue
        target = r.get("recommended_weekly_tss")
        if r.get("error") or target is None:
            continue
        targets[ws.isoformat()] = {
            "planned_tss": int(round(target)),
            "week_type": r.get("week_type"),
            "week_num": r.get("training_week"),
        }
    return targets


def post_process(data):
    """Add heat, decoupling, and CTL projection fields to the training-data dict."""
    # Heat protocol
    heat_entries = json.loads(HEAT_LOG.read_text()) if HEAT_LOG.exists() else []
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    this_week = [e for e in heat_entries if e.get("date", "") >= week_start.isoformat()]
    last_date = max((e["date"] for e in heat_entries), default=None)
    data["heatProtocol"] = {
        "sessions_cumulative": len(heat_entries),
        "sessions_this_week": len(this_week),
        "last_session_date": last_date,
        "protocol_start_date": "2026-05-15",
        "target_min": 14,
        "target_max": 20,
    }
    accl = _heat_accl_series("jamie")
    if accl:
        data["heatAccl"] = accl

    # Last-season CTL overlay (cached once — 2025 data never changes)
    if FITNESS_PREV_CACHE.exists():
        try:
            data["fitnessPrev"] = json.loads(FITNESS_PREV_CACHE.read_text())
        except Exception:
            pass

    fitness_2023_cache = BASE / "athletes/jamie/fitness-2023-cache.json"
    if fitness_2023_cache.exists():
        try:
            data["fitnessPrev2"] = json.loads(fitness_2023_cache.read_text())
        except Exception:
            pass

    # Decoupling trend
    dcoup = json.loads(DECOUPLING_LOG.read_text()) if DECOUPLING_LOG.exists() else []
    data["decouplingTrend"] = sorted(dcoup, key=lambda e: e.get("date", ""))

    # CTL projection
    current_ctl = data["kpi"]["ctl"]
    ramp7d = data["kpi"]["ramp7d"]
    days_to_race = (RACE_DATE - today).days + 1

    def current_trend_tss(d):
        return max(0, current_ctl + ramp7d / 7)  # extend current ramp

    # planned_sessions: use actual planned event TSS from weekCalendar for the
    # next 14 days, then fall back to phase averages beyond the known window
    planned_tss_by_date = {}
    completed_dates = set()
    for e in data.get("weekCalendar", []):
        d_str = e.get("date", "")
        if e.get("status") == "completed":
            completed_dates.add(d_str)
        elif e.get("status") == "planned":
            planned_tss_by_date[d_str] = planned_tss_by_date.get(d_str, 0) + (e.get("tss") or 0)
    known_window_end = today + timedelta(days=14)

    def planned_sessions_tss(d):
        d_str = d.isoformat()
        # If there's already a completed activity on this date, current_ctl
        # already reflects it — don't add planned TSS on top.
        if d_str in completed_dates:
            return 0
        # Within known window: use actual planned session TSS (0 = rest day)
        # Beyond known window: 0 (nothing booked yet — CTL decays honestly)
        if d <= known_window_end:
            return planned_tss_by_date.get(d_str, 0)
        return 0

    sick_week_num = 10
    def sick_week_tss(d):
        week = max(1, math.ceil((d - PLAN_START).days / 7))
        return 0 if week == sick_week_num else _phase_daily_tss_projection(d)

    # planned_sessions is only a genuine forecast out to the last date we actually
    # have calendar data for (weekCalendar — completed history + booked events).
    # Beyond that date there is nothing to project from, so continuing the series
    # to race day just plots a "what if you never train again" decay curve and it
    # reads as a real forecast. Truncate the series there instead of hard-coding
    # a horizon length — it must track however far weekCalendar actually reaches.
    _wc_dates = [e.get("date") for e in data.get("weekCalendar", []) if e.get("date")]
    last_known_date = date.fromisoformat(max(_wc_dates)) if _wc_dates else today
    planned_sessions_horizon_days = max(1, (last_known_date - today).days + 1)

    # Jamie's config (phase_ctl) drives the Target CTL line — same helper as every
    # other athlete, so the displayed target tracks athletes.json automatically.
    try:
        _jamie_cfg = json.loads((BASE / "config/athletes.json").read_text()).get("jamie", {})
    except Exception:
        _jamie_cfg = {}
    data["ctlProjection"] = {
        "current_trend":    _ctl_project(current_ctl, current_trend_tss, days_to_race),
        "planned_build":    _ctl_project(current_ctl, _phase_daily_tss_projection, days_to_race),
        "planned_sessions": _ctl_project(current_ctl, planned_sessions_tss, days_to_race)[:planned_sessions_horizon_days],
        "sick_week":        _ctl_project(current_ctl, sick_week_tss, days_to_race),
        "target_milestones": _ctl_target_milestones(_jamie_cfg, current_ctl, today),
        "race_date": RACE_DATE.isoformat(),
        "target_ctl_min": 105,
        "target_ctl_max": 115,
    }

    # Race predictor — Now / Race day / Target, from IF ∝ √CTL (see _race_predictor).
    try:
        _prof = json.loads((BASE / "athletes/jamie/profile.json").read_text())
        _rp = _race_predictor(_prof, current_ctl)
        if _rp:
            data["racePredictor"] = _rp
    except Exception as exc:
        log(f"[jamie] racePredictor skipped: {exc}")

    # Profile fields needed by the dashboard (goals, thresholds)
    profile_f = BASE / "athletes/jamie/profile.json"
    if profile_f.exists():
        try:
            prof = json.loads(profile_f.read_text())
            data["profile"] = {
                "a_goal":                    prof.get("a_goal"),
                "b_goal":                    prof.get("b_goal"),
                "swim_css_per_100m":         prof.get("swim_css_per_100m"),
                "run_threshold_pace_per_km": prof.get("run_threshold_pace_per_km"),
                "lthr":                      prof.get("lthr"),
                "ftp_watts":                 data.get("resolvedFtp") or prof.get("ftp_watts"),
                "weight_kg":                 prof.get("weight_kg"),
                "race_distance":             prof.get("race_distance"),
                "race_date":                 prof.get("race_date"),
                "race_name":                 prof.get("race_name"),
                "prev_race":                 prof.get("prev_race"),
                "prev_race_date":            prof.get("prev_race_date"),
                "prev2_race_date":           prof.get("prev2_race_date"),
                # Splits for the 2023 race, so the Goals table can show the
                # progression 2023 -> 2025 -> target rather than one prior race.
                "prev2_race":                prof.get("prev2_race"),
                "prev2_race_name":           prof.get("prev2_race_name"),
                "race_targets":              prof.get("race_targets"),
            }
        except Exception as e:
            # Loud, not silent: an empty profile makes the whole zones/race-scenario
            # panel vanish from the athlete page (regression: 2026-06-07).
            log(f"PROFILE BUILD FAILED — athlete page zones/race panel will be empty: {e}")

    # Current state snapshot (ankle, watchdog flags, open actions)
    if STATE_JSON.exists():
        try:
            cs = json.loads(STATE_JSON.read_text())
            data["currentState"] = {
                "ankle_pain_during": cs.get("ankle", {}).get("pain_during"),
                "ankle_pain_next_morning": cs.get("ankle", {}).get("pain_next_morning"),
                "bike_ftp": cs.get("bike_ftp"),
                "watchdog_flags": cs.get("watchdog_flags", []),
                "open_actions": cs.get("open_actions", []),
                "weight_readings": cs.get("weight_readings", [])[-5:],
            }
        except Exception:
            pass

    # Weekly discipline breakdown (from athlete-summary.json if available)
    athlete_summary_f = BASE / "athletes/jamie/athlete-summary.json"
    if athlete_summary_f.exists():
        try:
            summary = json.loads(athlete_summary_f.read_text())
            data["weeklyBreakdown"] = summary.get("weeks", [])
            data["swimProgression"] = summary.get("swim_progression", [])
        except Exception:
            pass

    # Session log — last 60 confirmed (non-stub) entries.
    #
    # Was 10, which was too short to be useful and actively misleading: fuelling is
    # logged on a minority of sessions, so a 10-row window was all nulls and the app
    # reported "no water logged" while ten real values (2800ml, 1800ml, 1100ml...)
    # sat just outside it. The published FIELDS are unchanged - the allow-list decides
    # those - this only widens how far back the rows go.
    if SESSION_LOG.exists():
        try:
            all_entries = json.loads(SESSION_LOG.read_text())
            confirmed = [e for e in all_entries if not e.get("stub", True)]
            data["sessionLog"] = confirmed[-60:]
        except Exception:
            pass

    # Swim log — full history for progression chart
    if SWIM_LOG.exists():
        try:
            data["swimLog"] = json.loads(SWIM_LOG.read_text())
        except Exception:
            pass

    # Progress charts — cycling NP/VI, run pace/EF, fuelling g/hr
    if SESSION_LOG.exists():
        try:
            all_s = json.loads(SESSION_LOG.read_text())
            # Drop double-uploaded activities (same date+sport+distance+duration under
            # different ICU ids) — keep whichever entry has the most fields filled in.
            best = {}
            for s in all_s:
                k = (s.get("date"), s.get("sport"),
                     round(float(s.get("distance_km") or 0), 1),
                     int(s.get("duration_min") or 0))
                cur = best.get(k)
                if cur is None or sum(v is not None for v in s.values()) > sum(v is not None for v in cur.values()):
                    best[k] = s
            all_s = list(best.values())
            long_rides = sorted(
                [s for s in all_s if s.get("sport") == "Ride"
                 and s.get("norm_power") and s.get("avg_power")
                 and int(s.get("duration_min") or 0) >= 150],   # rides > 2.5 h only
                key=lambda x: x["date"]
            )
            hr_runs = sorted(
                [s for s in all_s if s.get("sport") == "Run"
                 and s.get("avg_hr") and s.get("distance_km")
                 and int(s.get("duration_min") or 0) >= 60],     # runs > 60 min only
                key=lambda x: x["date"]
            )
            carb_s = sorted(
                [s for s in all_s if s.get("nutrition_g_carb") and s.get("duration_min")],
                key=lambda x: x["date"]
            )
            ftp = (data.get("profile") or {}).get("ftp_watts") or 316
            data["progressData"] = {
                "ftp": ftp,
                "rides": [
                    {"date": s["date"], "np": s["norm_power"],
                     "vi": round(s["norm_power"] / s["avg_power"], 3),
                     "ef": round(s["norm_power"] / s["avg_hr"], 3) if s.get("avg_hr") else None,
                     "hr": s.get("avg_hr"), "dur": s.get("duration_min"),
                     "name": (s.get("name") or "")[:40]}
                    for s in long_rides
                ],
                "runs": [
                    {"date": s["date"],
                     "pace": round(float(s["duration_min"]) / float(s["distance_km"]), 3),
                     "ef": round(float(s["distance_km"]) * 1000 / float(s["duration_min"]) / float(s["avg_hr"]), 4),
                     "hr": s.get("avg_hr"), "dist": round(float(s["distance_km"]), 1),
                     "name": (s.get("name") or "")[:40]}
                    for s in hr_runs
                ],
                "carb": [
                    {"date": s["date"],
                     "g_per_hr": round(float(s["nutrition_g_carb"]) / float(s["duration_min"]) * 60, 1),
                     "sport": s.get("sport"), "dur": s.get("duration_min"),
                     "name": (s.get("name") or "")[:40]}
                    for s in carb_s
                ],
            }
        except Exception:
            pass

    # Plan vs actual — last 6 weeks, grouped by week.
    # Actual TSS from session-log.json; PLANNED from the planning engine's own
    # weekly target for that athlete and that week (was: a hard-coded phase table
    # that knew nothing about his taper and deload weeks).
    if SESSION_LOG.exists():
        try:
            all_entries = json.loads(SESSION_LOG.read_text())
            weekly_actual = defaultdict(float)
            seen_ids: set = set()
            for e in all_entries:
                aid = e.get("activity_id")
                if aid:
                    if aid in seen_ids:
                        continue
                    seen_ids.add(aid)
                d_str = e.get("date", "")
                if not d_str:
                    continue
                dt = date.fromisoformat(d_str)
                wk_start = dt - timedelta(days=dt.weekday())
                weekly_actual[wk_start.isoformat()] += e.get("tss") or 0

            this_monday = today - timedelta(days=today.weekday())
            weeks = [this_monday - timedelta(weeks=i) for i in range(5, -1, -1)]
            try:
                _cfg = json.loads((BASE / "config/athletes.json").read_text()).get("jamie", {})
            except Exception:
                _cfg = {}
            targets = _weekly_plan_targets(_cfg, weeks,
                                           dict(data.get("fitnessThis") or []),
                                           weekly_actual)
            plan_actual = []
            for wk_start in weeks:
                t = targets.get(wk_start.isoformat())
                if not t:
                    continue
                plan_actual.append({
                    "week_start": wk_start.isoformat(),
                    "week_num": t["week_num"],
                    "actual_tss": round(weekly_actual.get(wk_start.isoformat(), 0)),
                    "planned_tss": t["planned_tss"],
                    "week_type": t["week_type"],
                    # The current week is still being executed — without this the
                    # chart shows a part-done week as a near-total miss.
                    "in_progress": wk_start == this_monday,
                })
            data["planVsActual"] = plan_actual or None
        except Exception:
            pass

    return data


def _sport_normalise(raw):
    return {"VirtualRide": "Ride", "GravelRide": "Ride", "VirtualRun": "Run", "TrailRun": "Run"}.get(raw, raw)


def _format_pace(sport, dist_m, duration_s):
    if sport == "Ride" and dist_m and duration_s:
        return f"{dist_m / 1000 / (duration_s / 3600):.1f} kph"
    if sport == "Run" and dist_m and duration_s:
        spm = duration_s / (dist_m / 1000)
        return f"{int(spm)//60}:{int(spm)%60:02d}/km"
    if sport == "Swim" and dist_m and duration_s:
        spc = duration_s / (dist_m / 100)
        return f"{int(spc)//60}:{int(spc)%60:02d}/100m"
    return None


def _build_athlete_training_data(slug, athlete_cfg):
    """Build training-data-{slug}.json using IcuClient (Python only — no Claude call)."""
    sys.path.insert(0, str(BASE / "lib"))
    from icu_api import IcuClient

    today = date.today()
    client = IcuClient(athlete_cfg["icu_athlete_id"], athlete_cfg["icu_api_key"])

    HISTORY_DAYS = 120
    seven_ago  = (today - timedelta(days=7)).isoformat()
    history_from = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    seven_fwd  = (today + timedelta(days=7)).isoformat()
    twentyone_fwd = (today + timedelta(days=21)).isoformat()
    year_start = f"{today.year}-01-01"

    # Parallel fetch
    # 120 days, up from 49: planVsActual needs six completed weeks plus the week
    # before them, and the app's calendar shows whole months and pages backwards, so
    # a short window left most of every month with no data - which the calendar then
    # miscounted as rest days ("19 rest days in July" for Kathryn). history_21 below
    # still exists so pre-existing 14/21-day consumers are unaffected.
    wellness_60, history_49, events_21, fitness_ytd = client.fetch_all(
        ("get_wellness", 60),
        ("get_training_history", HISTORY_DAYS),
        ("get_events", today.isoformat(), twentyone_fwd),
        ("get_fitness", (today - date(today.year, 1, 1)).days + 1),
    )
    twentyone_ago = (today - timedelta(days=21)).isoformat()
    history_21 = [a for a in history_49
                  if (a.get("start_date_local") or "")[:10] >= twentyone_ago]

    # -- kpi ------------------------------------------------------------------
    kpi = {}
    if wellness_60:
        w = wellness_60[-1]
        ctl = round(w.get("ctl") or 0, 1)
        atl = round(w.get("atl") or 0, 1)
        ramp7d = round(ctl - round(wellness_60[-8].get("ctl") or 0, 1), 1) if len(wellness_60) >= 8 else 0
        kpi = {"ctl": ctl, "atl": atl, "tsb": round(ctl - atl, 1), "ramp7d": ramp7d,
               "hrv": w.get("hrv"), "rhr": w.get("restingHR")}

    # -- fitnessThis -----------------------------------------------------------
    fitness_this = [[w["id"][:10], round(w.get("ctl") or 0, 1)] for w in fitness_ytd if w.get("ctl")]

    # -- recent (full history window) ------------------------------------------
    # From history_49 (120 days), NOT history_21: filtering the 21-day list by a wider
    # date bound still gives 21 days, which is why widening the fetch alone changed
    # nothing here.
    recent = []
    for a in sorted([x for x in history_49 if x.get("start_date_local", "")[:10] >= history_from],
                    key=lambda x: x.get("start_date_local", ""), reverse=True):
        sport = _sport_normalise(a.get("type", "Other"))
        dist_m = a.get("distance") or 0
        dur_s  = a.get("moving_time") or 0
        dur    = round(dur_s / 60)
        avg_p  = a.get("average_watts")
        norm_p = a.get("icu_weighted_avg_watts")
        recent.append({
            "_aid":   a.get("id"),   # removed by route_shape.attach_shapes
            "date":   a.get("start_date_local", "")[:10],
            "sport":  sport,
            "name":   a.get("name", ""),
            "dur":    dur,
            "dist":   round(dist_m / 1000, 2) if dist_m else None,
            "pace":   _format_pace(sport, dist_m, dur_s),
            "hr":     int(a["average_heartrate"]) if a.get("average_heartrate") else None,
            "powAvg": int(avg_p) if avg_p else None,
            "powNp":  int(norm_p) if norm_p else None,
            "tss":    int(a.get("icu_training_load") or 0),
        })

    # -- weekCalendar (last 7 days + next 14 days) -----------------------------
    completed_by_date: dict[str, list] = defaultdict(list)
    for a in history_21:
        d = a.get("start_date_local", "")[:10]
        if d >= seven_ago:
            completed_by_date[d].append(a)

    week_calendar = []
    for a in sorted(history_21, key=lambda x: x.get("start_date_local", "")):
        d = a.get("start_date_local", "")[:10]
        if d < seven_ago:
            continue
        sport = _sport_normalise(a.get("type", "Other"))
        dist_m = a.get("distance") or 0
        dur_s  = a.get("moving_time") or 0
        tss    = int(a.get("icu_training_load") or 0)
        avg_p  = a.get("average_watts")
        norm_p = a.get("icu_weighted_avg_watts")
        if sport == "Ride":
            detail = " · ".join(filter(None, [
                f"NP {int(norm_p)}W" if norm_p else None,
                f"HR {int(a['average_heartrate'])}" if a.get("average_heartrate") else None,
                f"{dist_m/1000:.1f}km" if dist_m else None,
            ]))
        elif sport in ("Run", "Swim"):
            detail = " · ".join(filter(None, [
                _format_pace(sport, dist_m, dur_s),
                f"{dist_m/1000:.1f}km" if dist_m else None,
            ]))
        else:
            detail = ""
        week_calendar.append({
            "date": d, "sport": sport, "name": a.get("name", ""),
            "tss": tss, "duration_min": round(dur_s / 60),
            "status": "completed", "key": tss >= 60, "detail": detail,
        })

    completed_dates = set(completed_by_date.keys())
    for ev in events_21:
        ev_date = (ev.get("start_date_local") or "")[:10]
        if not ev_date or ev_date < today.isoformat():
            continue
        ev_sport = _sport_normalise(ev.get("type") or ev.get("sport_type") or "Other")
        # Skip if there's already a completed activity of same sport on that date
        if any(_sport_normalise(a.get("type", "")) == ev_sport
               for a in completed_by_date.get(ev_date, [])):
            continue
        ev_tss = ev.get("icu_training_load") or ev.get("load")
        ev_dur = ev.get("moving_time") or ev.get("duration")
        week_calendar.append({
            "date": ev_date, "sport": ev_sport, "name": ev.get("name", ""),
            "tss": int(ev_tss) if ev_tss else None,
            "duration_min": round(int(ev_dur) / 60) if ev_dur else None,
            "status": "planned", "key": bool(ev_tss and int(ev_tss) >= 60), "detail": "",
        })
    week_calendar.sort(key=lambda x: x["date"])

    # -- loadChart (today−7 to today+7, 15 days) -------------------------------
    tsb_by_date = {}
    for w in wellness_60:
        d = w.get("id", "")[:10]
        ctl = w.get("ctl") or 0
        atl = w.get("atl") or 0
        if d:
            tsb_by_date[d] = round(ctl - atl, 1)

    load_chart = []
    for i in range(-7, 8):
        d = (today + timedelta(days=i)).isoformat()
        acts = []
        for a in history_21:
            if a.get("start_date_local", "")[:10] == d:
                acts.append({
                    "sport": _sport_normalise(a.get("type", "Other")),
                    "tss":   int(a.get("icu_training_load") or 0),
                    "dur":   round((a.get("moving_time") or 0) / 60),
                    "status": "completed",
                })
        if i > 0:
            for ev in events_21:
                ev_d = (ev.get("start_date_local") or "")[:10]
                if ev_d != d:
                    continue
                ev_sport = _sport_normalise(ev.get("type") or ev.get("sport_type") or "Other")
                if any(a["sport"] == ev_sport for a in acts):
                    continue
                ev_tss = ev.get("icu_training_load") or ev.get("load")
                ev_dur = ev.get("moving_time") or ev.get("duration")
                acts.append({
                    "sport": ev_sport,
                    "tss":   int(ev_tss) if ev_tss else None,
                    "dur":   round(int(ev_dur) / 60) if ev_dur else None,
                    "status": "planned",
                })
        load_chart.append({"date": d, "tsb": tsb_by_date.get(d), "activities": acts})

    # Forward-project TSB for future loadChart days via Banister EMA decay
    if kpi.get("ctl") is not None and kpi.get("atl") is not None:
        _pctl, _patl = float(kpi["ctl"]), float(kpi["atl"])
        for _entry in load_chart:
            if _entry["date"] > today.isoformat():
                _day_tss = sum((a.get("tss") or 0) for a in _entry.get("activities", [])
                               if a.get("status") == "planned")
                _pctl += (_day_tss - _pctl) / 42.0
                _patl += (_day_tss - _patl) / 7.0
                _entry["tsb"] = round(_pctl - _patl, 1)
                _entry["projected"] = True

    # Route outlines, shape only (see lib/route_shape.py).
    try:
        from route_shape import attach_shapes
        _n = attach_shapes(client, BASE, slug, recent, log=log)
        if _n:
            log(f"[{slug}] route shapes: {_n} newly fetched")
    except Exception as e:
        log(f"[{slug}] route shapes skipped (non-fatal): {e}")
        for _r in recent:
            _r.pop("_aid", None)

    # -- session log + swim log from local files -------------------------------
    # 60, matching Jamie's path: fuelling is logged on a minority of sessions, so a
    # 10-row window could not show it even when the values existed.
    session_log = []
    sl_file = BASE / f"athletes/{slug}/session-log.json"
    if sl_file.exists():
        try:
            all_e = json.loads(sl_file.read_text())
            session_log = [e for e in all_e if not e.get("stub", True)][-60:]
        except Exception:
            pass

    # Fuelling history. This lived ONLY in post_process(), which runs for Jamie alone,
    # so Kathryn's Fuel tab was empty while 24 logged carb sessions sat in her file
    # (Calum, 4). Same computation, every athlete: g/hr = grams / minutes * 60, with
    # the duration carried so the app can show 120g over 1h apart from 120g over 5h.
    progress_data = {}
    if sl_file.exists():
        try:
            carb_s = [e for e in json.loads(sl_file.read_text())
                      if e.get("nutrition_g_carb") and e.get("duration_min")]
            if carb_s:
                progress_data["carb"] = [
                    {"date": e["date"],
                     "g_per_hr": round(float(e["nutrition_g_carb"]) /
                                       float(e["duration_min"]) * 60, 1),
                     "sport": e.get("sport"), "dur": e.get("duration_min"),
                     "name": (e.get("name") or "")[:40]}
                    for e in carb_s
                ]
        except Exception as e:
            log(f"[{slug}] carb history skipped (non-fatal): {e}")

    swim_log = []
    sw_file = BASE / f"athletes/{slug}/swim-log.json"
    if sw_file.exists():
        try:
            swim_log = json.loads(sw_file.read_text())
        except Exception:
            pass

    data = {
        "generated":    today.isoformat(),
        "kpi":          kpi,
        "fitnessThis":  fitness_this,
        "fitnessBySport": _per_sport_ctl_cached(slug, client, today, history_49),
        "recent":       recent,
        "weekCalendar": week_calendar,
        "loadChart":    load_chart,
        "sessionLog":   session_log,
        "progressData": progress_data,
        "rampCap":      _ramp_cap(slug),
        "refreshCadence": _refresh_cadence(),
        "swimLog":      swim_log,
        "sports":       _focus_sports(slug),
        "zoneDistribution": _zone_distribution(slug, history_49, today),
    }

    # -- planVsActual ----------------------------------------------------------
    # Every active athlete has a CTL basis in athletes.json (phase_ctl or race_min),
    # so required_tss returns a defensible weekly target for all of them and the
    # series is no longer Jamie-only — it was null for Kathryn and Calum purely
    # because the old hard-coded phase table was written against Jamie's plan.
    # Actuals come from ICU training load (authoritative and complete) rather than
    # session-log.json, which for these two is partial.
    try:
        weekly_actual = defaultdict(float)
        for a in history_49:
            d_str = (a.get("start_date_local") or "")[:10]
            if not d_str:
                continue
            dt = date.fromisoformat(d_str)
            wk = dt - timedelta(days=dt.weekday())
            weekly_actual[wk.isoformat()] += float(a.get("icu_training_load") or 0)
        this_monday = today - timedelta(days=today.weekday())
        weeks = [this_monday - timedelta(weeks=i) for i in range(5, -1, -1)]
        targets = _weekly_plan_targets(athlete_cfg, weeks, dict(fitness_this),
                                       weekly_actual)
        plan_actual = []
        for wk_start in weeks:
            t = targets.get(wk_start.isoformat())
            if not t:
                continue
            plan_actual.append({
                "week_start": wk_start.isoformat(),
                "week_num": t["week_num"],
                "actual_tss": round(weekly_actual.get(wk_start.isoformat(), 0)),
                "planned_tss": t["planned_tss"],
                "week_type": t["week_type"],
                "in_progress": wk_start == this_monday,
            })
        if plan_actual:
            data["planVsActual"] = plan_actual
    except Exception as e:
        log(f"[{slug}] planVsActual skipped (non-fatal): {e}")

    accl = _heat_accl_series(slug)
    if accl:
        data["heatAccl"] = accl

    # Previous season CTL overlay (if cache exists for this athlete)
    prev_cache = BASE / f"athletes/{slug}/fitness-prev-cache.json"
    if prev_cache.exists():
        try:
            data["fitnessPrev"] = json.loads(prev_cache.read_text())
        except Exception:
            pass

    prev2_cache = BASE / f"athletes/{slug}/fitness-2023-cache.json"
    if prev2_cache.exists():
        try:
            data["fitnessPrev2"] = json.loads(prev2_cache.read_text())
        except Exception:
            pass

    # Profile (goals + thresholds)
    profile_f = BASE / f"athletes/{slug}/profile.json"
    if profile_f.exists():
        try:
            prof = json.loads(profile_f.read_text())
            session_log_f = BASE / f"athletes/{slug}/session-log.json"
            data["profile"] = {
                "a_goal":                    prof.get("a_goal"),
                "b_goal":                    prof.get("b_goal"),
                "swim_css_per_100m":         prof.get("swim_css_per_100m"),
                "run_threshold_pace_per_km": prof.get("run_threshold_pace_per_km"),
                "lthr":                      prof.get("lthr"),
                "ftp_watts":                 _resolve_ftp(prof.get("ftp_watts"), fitness_ytd, session_log_f),
                "weight_kg":                 prof.get("weight_kg"),
                "race_distance":             prof.get("race_distance"),
                "race_date":                 prof.get("race_date"),
                "race_name":                 prof.get("race_name"),
                "prev_race":                 prof.get("prev_race"),
                "prev_race_date":            prof.get("prev_race_date"),
                "prev2_race_date":           prof.get("prev2_race_date"),
                # Splits for the 2023 race, so the Goals table can show the
                # progression 2023 -> 2025 -> target rather than one prior race.
                "prev2_race":                prof.get("prev2_race"),
                "prev2_race_name":           prof.get("prev2_race_name"),
                "race_targets":              prof.get("race_targets"),
            }
        except Exception:
            pass

    # Weekly discipline breakdown (from athlete-summary.json)
    summary_f = BASE / f"athletes/{slug}/athlete-summary.json"
    if summary_f.exists():
        try:
            summary = json.loads(summary_f.read_text())
            data["weeklyBreakdown"] = summary.get("weeks", [])
            data["swimProgression"] = summary.get("swim_progression", [])
        except Exception:
            pass

    # CTL projection — SINGLE SOURCE OF TRUTH. The target CTL milestones come from
    # the SAME planner maths (derive_phase_ctl_targets / compute_required_tss in
    # primitives.load) that stage1-plan uses, driven by ctl_targets.race_min in
    # athletes.json. The site chart plots whatever lands in ctlProjection — there
    # are NO hardcoded targets — so changing an athlete's race_min moves both the
    # plan and the website together. (Was: a stale phase_tss-defaults projection
    # that ignored ctl_targets and drifted from the plan.)
    try:
        sys.path.insert(0, str(BASE / "ironman-analysis"))
        from primitives.load import derive_phase_ctl_targets, compute_required_tss
        phase_cfg      = athlete_cfg.get("phase_tss", {})
        ctl_targets    = athlete_cfg.get("ctl_targets", {})
        race_min       = ctl_targets.get("race_min")
        plan_start_str = athlete_cfg.get("plan_start")
        race_dt        = date.fromisoformat(athlete_cfg["race_date"])
        if race_min and plan_start_str and kpi.get("ctl"):
            plan_start_dt = date.fromisoformat(plan_start_str)
            current_ctl   = kpi["ctl"]
            # Defaults MUST mirror plan_tools.required_tss exactly, or the site
            # target and the plan target drift apart again.
            ends = {
                "base":     phase_cfg.get("base_end_week", 6),
                "build":    phase_cfg.get("build_end_week", 10),
                "specific": phase_cfg.get("specific_end_week", 14),
                "peak":     phase_cfg.get("peak_end_week", 17),
            }
            max_ramp        = float(athlete_cfg.get("max_ctl_ramp_per_week", 5.0))
            taper_overshoot = float(athlete_cfg.get("taper_overshoot", 1.15))
            derived = derive_phase_ctl_targets(
                current_ctl, int(race_min), plan_start_dt,
                ends["base"], ends["build"], ends["specific"], ends["peak"],
                max_ramp, taper_overshoot, today=today)

            # Target CTL milestones — shared single-source helper (handles configured
            # phase_ctl and race_min-derived identically for every athlete).
            target_milestones = _ctl_target_milestones(athlete_cfg, current_ctl, today)

            # Planned build: ramp to the peak target then taper, using the same
            # required-TSS maths the planner prescribes (not a static phase table).
            days_to_race  = (race_dt - today).days + 1
            peak_end_date = plan_start_dt + timedelta(weeks=ends["peak"])
            weeks_to_peak = max(1, math.ceil((peak_end_date - today).days / 7))
            build_daily   = compute_required_tss(current_ctl, derived["peak"], weeks_to_peak) / 7.0

            def _planned_build(d):
                return build_daily if d <= peak_end_date else build_daily * 0.6

            proj_build = _ctl_project(current_ctl, _planned_build, days_to_race)
            data["ctlProjection"] = {
                "planned_build":    proj_build,
                "target_milestones": target_milestones,
                "race_date":        race_dt.isoformat(),
                "target_ctl_min":   ctl_targets.get("race_min", 60),
                "target_ctl_max":   ctl_targets.get("race_max", 80),
            }
    except Exception as exc:
        log(f"[{slug}] ctlProjection skipped: {exc}")

    out = BASE / f"training-data-{slug}.json"
    out.write_text(json.dumps(data, separators=(",", ":")))
    log(f"[{slug}] training-data-{slug}.json: CTL {kpi.get('ctl')}, {len(recent)} activities")

    # Sanitised public variant. This path had NO stripping of any kind before
    # 28 Jul 2026 - _strip_private() was only ever applied to jamie - so these
    # files were published complete with sessionLog (injury/pain, notes,
    # hydration, nutrition), kpi.hrv, kpi.rhr, profile.weight_kg and
    # profile.lthr. Every athlete now goes through the same allow-list.
    pub_rel = write_public_variant(data, slug)
    if pub_rel:
        _PUBLISHED.append(pub_rel)


# Repo-relative paths of the sanitised public files written this run. Only these
# are ever staged; it is populated exclusively by write_public_variant().
_PUBLISHED = []


def acquire_lock():
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 600:
        return False
    LOCK_FILE.touch()
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def main():
    if not acquire_lock():
        log("Already running — skipping")
        sys.exit(0)

    try:
        sys.path.insert(0, str(BASE / "lib"))
        from icu_api import IcuClient
        athletes_map = json.loads(ATHLETES_CONFIG.read_text())
        jamie_cfg    = athletes_map.get("jamie", {})
        client       = IcuClient(jamie_cfg["icu_athlete_id"], jamie_cfg["icu_api_key"])

        fetch_fitness_prev(client)  # one-time cache of 2025 CTL — skips if already exists

        # The app's Library tab reads public/session-library.json, because
        # config/ is excluded from the published site by _config.yml and always
        # must be. Republish it here so an edit to the library reaches the app
        # without a second manual step; failure is non-fatal, the app just shows
        # the last published copy.
        try:
            r = subprocess.run(
                [sys.executable, str(BASE / "scripts" / "publish-session-library.py")],
                capture_output=True, text=True, timeout=60,
            )
            log(f"Session library: {(r.stdout or r.stderr).strip().splitlines()[0]}"
                if (r.stdout or r.stderr) else "Session library: published")
            if r.returncode == 0:
                _PUBLISHED.append("ClaudeCoach/public/session-library.json")
        except Exception as e:
            log(f"Session library publish warning: {e} — app keeps last copy")

        # Nutrition subset for the app's Food tab. OPT-IN: the script writes nothing at
        # all for an athlete whose profile lacks nutrition_tracker: true, so a new
        # athlete is never published by omission. Non-fatal, like the library above:
        # this refresh must never fail because a secondary publish did.
        try:
            r = subprocess.run(
                [sys.executable, str(BASE / "scripts" / "publish-nutrition-data.py")],
                capture_output=True, text=True, timeout=120,
            )
            out = (r.stdout or r.stderr).strip().splitlines()
            log("Nutrition: " + ("; ".join(out[-3:]) if out else "nothing published"))
            for line in out:
                slug = line.split(":")[0].strip()
                if "wrote" in line and slug:
                    _PUBLISHED.append(f"ClaudeCoach/public/nutrition-{slug}.json")
        except Exception as e:
            log(f"Nutrition publish warning: {e}, app keeps last copy")

        log("Fetching live data via IcuClient...")
        try:
            data = _build_jamie_data(client)
            log(f"Fetch ok: CTL {data['kpi'].get('ctl')}, {len(data['recent'])} activities")
        except Exception as e:
            log(f"IcuClient fetch failed: {e}")
            sys.exit(1)

        # Add locally-computed fields (heat, decoupling, CTL projection, session log…)
        try:
            data = post_process(data)
            log("Post-processing: heat, decoupling, CTL projection added")
        except Exception as e:
            log(f"Post-processing warning: {e} — continuing without extra fields")
        OUT_FILE.write_text(json.dumps(data, separators=(",", ":")))

        # Sanitised public variant for GitHub Pages. The old root-level
        # ClaudeCoach/training-data.json write is gone: it was a deny-list
        # output living at a path whose name implied it was safe, which is
        # exactly the confusion that kept the leak invisible for 11 weeks.
        # Nothing on the box reads it (only HTTP did, and the dashboards now
        # fetch public/ instead). The stale file is left on disk untouched.
        pub_rel = write_public_variant(data, "jamie")
        if pub_rel:
            _PUBLISHED.append(pub_rel)

        # Refresh per-athlete training data for other athletes (using IcuClient directly)
        if ATHLETES_CONFIG.exists():
            try:
                athletes_map = json.loads(ATHLETES_CONFIG.read_text())
                for slug, acfg in athletes_map.items():
                    if slug == "jamie" or not acfg.get("active", True):
                        continue
                    try:
                        _build_athlete_training_data(slug, acfg)
                    except Exception as e:
                        log(f"[{slug}] training-data refresh failed (non-fatal): {e}")
            except Exception as e:
                log(f"athletes.json load error: {e}")

        # Publish ONLY the sanitised public files. Every path staged here has
        # passed a sanitiser: the training-data files come from
        # write_public_variant() (allow-list + forbidden-key tripwire), and
        # public/session-library.json is only appended when
        # publish-session-library.py exits 0, which it does only after asserting
        # no athlete name survives in the output. Nothing is staged unsanitised.
        # The private files remain
        # gitignored, and cc-git-commit-push.sh stages by explicit path only
        # (never `git add -A`), so an untracked private file cannot be swept in.
        #
        # There is no private-origin option: diamondpeak.uk IS this public repo
        # via GitHub Pages, so anything the dashboards fetch must be committed
        # here. Publication is therefore gated on the sanitiser, not on hosting.
        if _PUBLISHED:
            try:
                r = subprocess.run(
                    [str(GIT_PUSH), "refresh: sanitised public training data"] + _PUBLISHED,
                    cwd=PROJECT_DIR, capture_output=True, text=True, timeout=300)
                log(f"publish rc={r.returncode} {(r.stdout or '').strip()[-300:]}")
                if r.returncode != 0:
                    log(f"publish stderr: {(r.stderr or '').strip()[-300:]}")
            except Exception as e:
                log(f"publish failed (non-fatal): {e}")
        else:
            log("Nothing sanitised successfully - published nothing.")
        log("Done.")

    finally:
        release_lock()


if __name__ == "__main__":
    main()
