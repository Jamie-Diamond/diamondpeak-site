#!/usr/bin/env python3
"""
Daily session prescription — runs via VM crontab at 05:00 daily.
Loops over all active athletes. Safe to run manually:
  python3 ClaudeCoach/scripts/daily-prescription.py
"""
import json, re, shutil, subprocess, sys, tempfile, os, time
from datetime import date
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
sys.path.insert(0, str(BASE / "lib"))
from coaching_levels import level_block as _level_block
import ops_log
from git_sync import sync_commit_push
sys.path.insert(0, str(BASE / "ironman-analysis"))
from primitives.modulation import (  # noqa: E402
    modulate_session, classify_session_type,
)
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/
CLAUDE      = shutil.which("claude") or "/usr/bin/claude"
NOTIFY      = BASE / "telegram/notify.py"
CONFIG      = BASE / "config/athletes.json"
LOG_DIR     = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE    = LOG_DIR / "prescription.log"

TOOLS = "Read,Write,Edit,Bash"


def trim_log(path: Path, max_lines: int = 5000):
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


def build_prompt(slug: str, name: str, race_name: str, coaching_level: str = "mid") -> str:
    today = date.today().isoformat()
    athlete_dir = BASE / "athletes" / slug
    first_name  = name.split()[0]

    return f"""You are running the daily session prescription for {name}'s {race_name} coaching system.

{_level_block(coaching_level)}


Step 1 — Pull live data via Bash (use today's date {today} for all calculations):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint fitness --days 7
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 7
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read these files:
- {athlete_dir}/persistent-rules.md (permanent coaching rules — zone targets, HR caps, and any [perm] rules OVERRIDE all defaults when writing event descriptions)
- {athlete_dir}/current-state.md
- {athlete_dir}/session-log.json (most recent entry = last RPE)

Step 3 — Assemble the readiness dict:
  atl: from fitness endpoint most recent row
  ctl: from fitness endpoint most recent row
  hrv_trend_pct: (today HRV - 7d avg HRV) / 7d avg HRV x 100  [if no HRV data, use 0.0]
  sleep_h_last_night: from wellness endpoint — use ONLY if the most recent wellness record is dated {today}. If it is dated before {today}, set sleep_h_last_night to null (wearable hasn't synced yet).
  last_session_rpe: most recent rpe field in session-log.json (null if empty)
  ankle_pain_score: from current-state.md (0 if not present)
  ankle_quality_cleared: from current-state.md (True once 4 consecutive pain-free quality sessions confirmed)
  temp_c: today's forecast ambient temp — use 18.0 as fallback if unavailable
  dew_point_c: today's forecast dew point — use 10.0 as fallback if unavailable

Step 4 — Identify today's planned session from the events endpoint. Map to session_type:
  Threshold/FTP intervals -> bike_threshold
  Z2 / long ride -> bike_z2
  VO2max -> bike_vo2
  Race-pace bike -> bike_race_pace
  Run intervals / tempo -> run_quality
  Easy run / walk-run -> run_easy
  Long run -> run_long
  Brick -> brick
  Swim -> swim
  Gym -> strength
  No session planned -> output "Rest day — no session planned." and stop.

Also extract from the planned session event:
  target_intensity (if not explicit, derive from session type: threshold=1.0, race_pace=0.72, z2=0.65, vo2=1.10)
  interval_count (null if not an interval session)
  interval_duration_min (null if not an interval session)
  recovery_min (null if not an interval session)
  total_duration_min

Step 5 — Call the modulation engine:
  python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/ironman-analysis/scripts/modulate.py '<json with planned and readiness keys>'

Step 6 — If modified or swapped_to_z2: push the adjusted session to Intervals.icu via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint push_workout --payload '{{"sport":"...", "date":"{today}", "name":"...", "description":"...", "planned_training_load": N}}'
  If go == false: push a recovery note workout with description "BLOCKED: [R1 reason from reasoning trail]".
  If no rules fired: no push needed.

Step 7 — Output the prescription card in exactly this format:

---
**Today: [session name] — [GO / MODIFIED / SWAPPED / BLOCKED]**

| Field | Planned | Prescribed |
|---|---|---|
| Intensity | X% FTP | Y% FTP |
| Intervals | N x M min | N' x M min |
| Recovery | X min | X min |
| Duration | X min | X min |

**Reasoning trail(s):**
- [L2 trail for each fired rule — format: (signal with real number) -> (rule) -> (adjustment) -> (expected effect)]

*[One-sentence summary]*

---

If no rules fired: output "Today: [session name] — execute as planned." and the planned targets only (no reasoning trails section).

Step 8 — Update current-state.md: in the "Off-plan in last 7 days" section, note today's prescribed session status (modified/swapped/blocked) and the reason if any rule fired. Also update ankle section if today's prescription was affected by ankle status.

Step 9 — If the session was modified, swapped, or blocked, append this at the very end of your response:
<telegram>[session name]: [one plain-English sentence on what changed and why]</telegram>
Example: <telegram>Morning ride: reduced to Z2 — HRV down 18% vs 7-day average.</telegram>
Rules for the <telegram> line:
- Exactly one sentence. No preamble, no reasoning trail, no tool commentary, no "I".
- Must begin with the session name followed by a colon.
- No markdown formatting inside the tags.
If the session is unchanged (GO with no rule fires), omit the <telegram> line entirely.
The <telegram> content is the ONLY thing sent to the athlete — keep it clean.
"""


# ---------------------------------------------------------------------------
# Prescription backstop (remediation WS E) — the wrapper computes the modulation
# engine's prescription from deterministic inputs, rather than trusting the LLM to
# call it. SHADOW mode (default): log the engine result next to the LLM's so it can
# be observed on real runs; not yet authoritative. Set PRESCRIPTION_BACKSTOP=off to
# skip. Authoritative injection is a deliberate later flip after shadow observation.
# ---------------------------------------------------------------------------

def _icu(slug: str, endpoint: str, *extra) -> object:
    try:
        r = subprocess.run(
            ["python3", "ClaudeCoach/lib/icu_fetch.py", "--athlete", slug,
             "--endpoint", endpoint, *extra],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=60,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout.strip())
    except Exception:
        return None


def _latest_fitness(slug: str):
    data = _icu(slug, "fitness", "--days", "7")
    if isinstance(data, list):
        for e in reversed(data):
            if e.get("ctl") is not None:
                return float(e.get("atl") or 0.0), float(e.get("ctl") or 0.0)
    return None, None


def _hrv_trend_and_sleep(slug: str):
    """hrv_trend_pct (latest vs trailing mean, negative = declining) + last sleep h."""
    data = _icu(slug, "wellness", "--days", "14")
    if not isinstance(data, list) or not data:
        return 0.0, None
    hrvs = [float(e["hrv"]) for e in data if e.get("hrv") is not None]
    trend = 0.0
    if len(hrvs) >= 3:
        latest, prior = hrvs[-1], hrvs[:-1]
        base = sum(prior) / len(prior)
        if base:
            trend = round((latest - base) / base * 100, 1)
    sleep_h = None
    for e in reversed(data):
        if e.get("sleepSecs"):
            sleep_h = round(float(e["sleepSecs"]) / 3600.0, 2)
            break
    return trend, sleep_h


def _last_rpe(slug: str):
    p = BASE / "athletes" / slug / "session-log.json"
    if p.exists():
        try:
            log = json.loads(p.read_text())
            for e in reversed(log if isinstance(log, list) else []):
                if e.get("rpe") is not None and not e.get("stub"):
                    return int(e["rpe"])
        except Exception:
            pass
    return None


def _ankle_state(slug: str):
    """(pain_score, quality_cleared) from current-state.json's nested ankle block."""
    p = BASE / "athletes" / slug / "current-state.json"
    if p.exists():
        try:
            a = (json.loads(p.read_text()) or {}).get("ankle") or {}
            pain = max(float(a.get("pain_during", 0) or 0),
                       float(a.get("pain_next_morning", 0) or 0))
            cleared = bool(a.get("four_pain_free_weeks_reached", False))
            return int(round(pain)), cleared
        except Exception:
            pass
    return 0, True   # no ankle block → not an injured athlete → unrestricted


_INTENSITY_BY_TYPE = {
    "bike_threshold": 0.95, "bike_vo2": 1.1, "bike_race_pace": 0.85, "bike_z2": 0.65,
    "run_quality": 1.0, "run_long": 0.7, "run_easy": 0.65, "brick": 0.75,
    "swim": 0.7, "strength": 0.0,
}

# Fallback planned-duration (min) by session_type when the name carries nothing parseable.
_DUR_DEFAULTS = {
    "bike_threshold": 75, "bike_vo2": 75, "bike_race_pace": 90, "bike_z2": 90,
    "run_quality": 60, "run_long": 90, "run_easy": 50, "brick": 75,
    "swim": 45, "strength": 40,
}


def _css_seconds(css) -> int | None:
    """Parse swim_css_per_100m ('1:40' or seconds) to integer seconds, or None."""
    if not css:
        return None
    try:
        if ":" in str(css):
            mm, ss = str(css).split(":")
            return int(mm) * 60 + int(ss)
        return int(float(css))
    except Exception:
        return None


def _duration_from_name(name: str, session_type: str, css_per_100m=None) -> int:
    """Best-effort planned duration (minutes) when an event has no moving_time.

    Planned events (esp. swims) often carry no moving_time. We read the duration the
    coach wrote into the NAME ('4hr 30min', '90min', '50 min'); for swims given only a
    distance ('2.6km') we estimate from CSS. We NEVER treat load_target (TSS) as
    minutes — that produced a 2.6km swim = '5 min'. Falls back to a per-type default."""
    n = (name or "").lower()
    m = re.search(r"(\d+)\s*hr(?:\s*(\d+)\s*min)?", n)        # "4hr" / "2hr 30min"
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"(\d+)\s*min", n)                          # "90min" / "50 min"
    if m:
        return int(m.group(1))
    if "swim" in (session_type or "") or "swim" in n:         # distance → CSS estimate
        km = re.search(r"(\d+(?:\.\d+)?)\s*km", n)
        metres = float(km.group(1)) * 1000 if km else None
        if metres is None:
            mm = re.search(r"(\d{3,4})\s*m\b", n)
            metres = float(mm.group(1)) if mm else None
        if metres:
            css = _css_seconds(css_per_100m) or 110           # ~1:50/100m default
            return int(round(metres / 100 * css / 60 * 1.15))  # +15% rest buffer
    return _DUR_DEFAULTS.get(session_type, 60)


def _todays_planned(slug: str, today: str):
    """The day's primary planned session as a modulation `planned` dict, or None."""
    data = _icu(slug, "events", "--start", today, "--end", today)
    workouts = [e for e in (data or []) if e.get("category") == "WORKOUT"]
    if not workouts:
        return None
    # Primary = highest planned load (so a strength add-on doesn't mask the key session).
    primary = max(workouts, key=lambda e: float(e.get("load_target") or 0))
    st = classify_session_type(primary.get("type", ""), primary.get("name", ""))
    dur = primary.get("moving_time")
    if dur:
        dur_min = int(float(dur) / 60)
    else:
        css = None
        prof_f = BASE / "athletes" / slug / "profile.json"
        if prof_f.exists():
            try:
                css = json.loads(prof_f.read_text()).get("swim_css_per_100m")
            except Exception:
                css = None
        dur_min = _duration_from_name(primary.get("name", ""), st, css)
    return {
        "session_type": st,
        "target_intensity": _INTENSITY_BY_TYPE.get(st, 0.7),
        "interval_count": None, "interval_duration_min": None, "recovery_min": None,
        "total_duration_min": dur_min,
        "_name": primary.get("name", ""),
    }


def _prescription_shadow(slug: str, cfg: dict) -> None:
    """Compute and LOG the engine's prescription for today (shadow, non-authoritative)."""
    mode = os.environ.get("PRESCRIPTION_BACKSTOP", "shadow").strip().lower()
    if mode in ("0", "off", "none", "false"):
        return
    try:
        today = date.today().isoformat()
        planned = _todays_planned(slug, today)
        if not planned:
            with open(LOG_FILE, "a") as lf:
                lf.write(f"[prescription:{slug}] BACKSTOP ({mode}): no planned session today — nothing to modulate.\n")
            return
        atl, ctl = _latest_fitness(slug)
        hrv_trend, sleep_h = _hrv_trend_and_sleep(slug)
        pain, cleared = _ankle_state(slug)
        readiness = {
            "atl": atl or 0.0, "ctl": ctl or 0.0,
            "hrv_trend_pct": hrv_trend, "sleep_h_last_night": sleep_h,
            "last_session_rpe": _last_rpe(slug),
            "ankle_pain_score": pain, "ankle_quality_cleared": cleared,
            # temp_c / dew_point_c omitted → engine uses benign defaults (no heat fetch).
        }
        rx = modulate_session({k: v for k, v in planned.items() if not k.startswith("_")},
                              readiness)
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[prescription:{slug}] BACKSTOP ({mode}) — engine prescription for "
                     f"'{planned['_name']}' [{planned['session_type']}]:\n")
            lf.write(f"    {rx.summary}\n")
            if rx.applied_rules:
                lf.write(f"    rules fired: {', '.join(rx.applied_rules)}\n")
            lf.write(f"    inputs: atl={readiness['atl']:.0f} ctl={readiness['ctl']:.0f} "
                     f"hrv_trend={hrv_trend}% sleep={sleep_h}h rpe={readiness['last_session_rpe']} "
                     f"ankle_pain={pain} cleared={cleared}\n")
            lf.write("    shadow mode — LLM's own prescription is what reached the athlete; "
                     "compare the two before making this authoritative.\n")
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[prescription:{slug}] BACKSTOP error (non-fatal): {e}\n")


def run_for_athlete(slug: str, cfg: dict) -> str | None:
    name      = cfg.get("name", slug)
    race_name = cfg.get("race_name", "upcoming race")
    chat_id   = str(cfg.get("chat_id", ""))
    if not chat_id:
        print(f"[{slug}] SKIP: no chat_id in athletes.json", file=sys.stderr)
        return None

    coaching_level = "mid"
    profile_path = BASE / "athletes" / slug / "profile.json"
    if profile_path.exists():
        try:
            coaching_level = json.loads(profile_path.read_text()).get("coaching_level", "mid")
        except Exception:
            pass

    # ICU preflight — if intervals.icu is unreachable (outage, revoked key) the
    # LLM must not prescribe on null CTL/HRV/sleep. Standing rule: say so and
    # ask for a manual paste instead.
    fitness = _icu(slug, "fitness", "--days", "7")
    if not isinstance(fitness, list) or not fitness:
        ops_log.alert("daily-prescription",
                      "ICU fitness fetch failed — prescription aborted, needs manual data paste",
                      athlete=slug)
        return None
    if _icu(slug, "wellness", "--days", "14") is None:
        ops_log.alert("daily-prescription",
                      "ICU wellness fetch failed — prescription aborted, needs manual data paste",
                      athlete=slug)
        return None

    prompt = build_prompt(slug, name, race_name, coaching_level=coaching_level)

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="claudecoach_prescription_", delete=False, suffix=".txt"
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        result = subprocess.run(
            [CLAUDE, "-p", open(prompt_file).read(),
             "--allowedTools", TOOLS,
             "--model", "claude-sonnet-4-6"],
            capture_output=True, text=True,
            cwd=PROJECT_DIR,
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            with open(LOG_FILE, "a") as lf:
                lf.write(f"[prescription:{slug}] STDERR: {stderr}\n")
        # Prescription backstop (WS E): compute + log the engine's prescription from
        # deterministic inputs. Shadow-only — additive, does not change the LLM output.
        _prescription_shadow(slug, cfg)
        # Commit any current-state.md changes Claude made
        today = date.today().isoformat()
        sync_commit_push(
            [f"ClaudeCoach/athletes/{slug}/current-state.md"],
            f"prescription: {today} {slug}",
            script="daily-prescription", athlete=slug,
        )
        return output or None
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[prescription:{slug}] Exception: {e}\n")
        ops_log.alert("daily-prescription", f"exception: {e}", athlete=slug)
        return None
    finally:
        os.unlink(prompt_file)


ATHLETE_STAGGER_S = int(os.environ.get("ATHLETE_STAGGER_S", "90"))


def main():
    athletes = json.loads(CONFIG.read_text())
    processed = False
    for slug, cfg in athletes.items():
        if not cfg.get("active"):
            continue
        if not cfg.get("daily_prescription", True):
            continue
        if processed:
            # Space the athletes' Claude runs out — back-to-back large requests
            # burst the account rate limit (the 429s behind slow replans).
            time.sleep(ATHLETE_STAGGER_S)
        processed = True
        output = run_for_athlete(slug, cfg)
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[prescription:{slug}]\n{output or '(no output)'}\n---\n")
        if output:
            ops_log.record_run("daily-prescription", athlete=slug, ok=True, detail="prescribed")
            m = re.search(r"<telegram>(.*?)</telegram>", output, re.DOTALL)
            summary = m.group(1).strip() if m else None
            if summary:
                # No 05:00 Telegram message — the summary is written here and the
                # 06:30 morning card surfaces the key points instead.
                latest = BASE / f"athletes/{slug}/daily-prescription-latest.md"
                latest.write_text(
                    f"date: {date.today().isoformat()}\n\n{summary}\n"
                )
    trim_log(LOG_FILE)

    # Refresh site data after prescriptions — background, non-blocking
    subprocess.Popen(
        ["python3",
         "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/scripts/refresh-site-data.py"],
        stdout=open(LOG_DIR / "refresh.log", "a"),
        stderr=subprocess.STDOUT,
    )


if __name__ == "__main__":
    main()
