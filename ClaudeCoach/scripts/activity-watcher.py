#!/usr/bin/env python3
"""
Check for new activities and send a brief analysis to Telegram.
Run every 15 min via cron. Loops over all active athletes. Skips if already running.
"""
import json, os, ssl, subprocess, sys, time, urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
LOCK_FILE       = BASE / ".activity_watcher.lock"
NOTIFY          = BASE / "telegram/notify.py"
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
ATHLETES_CONFIG = BASE / "config/athletes.json"
TG_CONFIG       = BASE / "telegram/config.json"

TOOLS = "Read,Write,Bash"

# The identity this job declares to lib/icu_fetch.py, on its own subprocess reads and in
# every icu_fetch line of the prompt. icu_fetch refuses the four WRITE endpoints to this
# caller: a watcher reads finished training and comments on it. It has never been asked to
# create, change or remove a planned session, but holding Bash meant nothing stopped it,
# and an unexplained missing week is the failure that has actually happened.
CALLER = "activity-watcher"

_WATER_SPORTS = {"sail", "watersport", "windsurf", "kitesurf", "kiteboard"}


def _log_to_history(slug: str, message: str) -> None:
    """Append an outbound notification to the athlete's Telegram history so the bot has context for replies."""
    history_file = BASE / "athletes" / slug / "telegram" / "history.json"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(history_file.read_text()) if history_file.exists() else []
    except Exception:
        history = []
    history.append({"user": "", "assistant": message})
    history_file.write_text(json.dumps(history[-30:], indent=2))

sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "ironman-analysis"))
from coaching_levels import level_block as _level_block
import illness as illness_lib   # structured illness/compromised flag (surfacing gate)
import acknowledgement as ack_lib   # §8.3 milestone triggers, evaluated in Python
import ask_gate                    # asked-and-answered + one-question-per-message gate
import ops_log
import write_verify            # read-back verdicts for the Strava writes below
import heat as heat_lib
import profile_fields
import claude_call
from git_sync import sync_commit_push
from primitives.run_durability import compute_run_durability, fade_line


def _pace_str(speed_ms: float) -> str:
    if not speed_ms:
        return "?"
    secs = round(1000 / speed_ms)
    return f"{secs // 60}:{secs % 60:02d}/km"


def _tg_send_keyboard(chat_id, text, keyboard):
    """Send a Telegram message with inline keyboard. Returns message_id or None."""
    try:
        cfg = json.loads(TG_CONFIG.read_text())
        token = cfg.get("bot_token", "")
        if not token:
            return None
        cafile = "/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None
        ctx = ssl.create_default_context(cafile=cafile)
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return json.loads(r.read()).get("result", {}).get("message_id")
    except Exception:
        return None


def _quick_log_keyboard(activity_id, slug, sport, has_injury, duration_min):
    """Return an inline_keyboard dict for post-session quick data capture."""
    aid = str(activity_id)

    def cb(field, val):
        return f"{field}:{aid}:{slug}:{val}"

    rows = []
    if sport == "Run" and has_injury:
        rows.append([{"text": str(i), "callback_data": cb("p", i)} for i in range(0, 6)])
        rows.append([{"text": str(i), "callback_data": cb("p", i)} for i in range(6, 11)])
    else:
        rows.append([{"text": f"RPE {i}", "callback_data": cb("r", i)} for i in range(5, 11)])

    if sport == "Ride" and (duration_min or 0) >= 90:
        rows.append([{"text": f"{g}g/hr", "callback_data": cb("c", g)} for g in (40, 50, 60, 70, 80, 90)])

    rows.append([
        {"text": "📊 Intervals", "callback_data": f"drill:intervals:{aid}:{slug}"},
        {"text": "🍌 Nutrition",  "callback_data": f"drill:nutrition:{aid}:{slug}"},
        {"text": "💓 HR",         "callback_data": f"drill:hr:{aid}:{slug}"},
        {"text": "↔️ Compare",    "callback_data": f"drill:compare:{aid}:{slug}"},
    ])

    return {"inline_keyboard": rows}


def _build_prompt(slug, first_name, ftp, injuries, profile=None, run_hr_cap=150, nutrition_target=90, recent_chat=""):
    """Build the per-athlete activity analysis prompt."""
    today = date.today().isoformat()
    # Injury context for the athlete line and run analysis
    injury_line = ""
    if injuries:
        descs = "; ".join(
            f"{inj.get('location','unknown')} ({inj.get('description','')})"
            for inj in injuries if inj.get("location")
        )
        protocol = next((inj.get("protocol", "") for inj in injuries if inj.get("protocol")), "")
        injury_line = f" Injuries: {descs}." + (f" Protocol: {protocol}." if protocol else "")

    # Read via profile_fields: this field may carry a prose caveat after the pace, and
    # it is interpolated into ATHLETE-FACING debrief text below. Passing it raw sent
    # Kathryn the entire internal note on 2026-07-30. Only the M:SS part may leak out.
    _thr_raw = (profile or {}).get("run_threshold_pace_per_km")
    threshold_pace = profile_fields.pace(_thr_raw, "4:02")
    _thr_note = (
        " (that threshold pace is a provisional, non-field-tested estimate: you may say"
        " 'provisional' once if it matters, but NEVER reproduce the caveat text from the"
        " profile field itself — it is an internal note, not athlete-facing)"
        if profile_fields.is_provisional(_thr_raw) else ""
    )
    run_injury_ask = (
        f"- Run (walk-run): If Strava laps show alternating run/walk laps (walk laps: pace >8:00/km or duration ≤90s):"
        f" Your ANALYSIS must be formatted EXACTLY as multiple output lines — each on its own line:"
        f" Line 1 (header): NxDUR / Xmin walk · avg GAP X:XX/km · +/-Xsec vs threshold ({threshold_pace}/km){_thr_note}"
        f" Lines 2..N+1 (one per run rep, use gap_pace if present else pace): Rep N: DUR · GAP X:XX/km · AVGbpm/MAXbpm"
        f" Final lines: % HR ≤{run_hr_cap} bpm cap adherence · decoupling % if >40 min | Injury pain score during? (0-10)"
        " Else (continuous run): Line 1 = distance + avg GAP pace vs threshold — state +/- sec/km."
        f" Line 2 = % time HR ≤{run_hr_cap} bpm + aerobic decoupling % if >40 min."
        " Line 3 = \"Injury pain score during? (0-10)\""
        " — the DURING score only. Never ask about this morning or the next morning:"
        " the morning score belongs to the morning check-in, which tracks its own"
        " answers, and asking here is how it came to be asked twice."
        if injuries else
        f"- Run (walk-run): If Strava laps show alternating run/walk laps (walk laps: pace >8:00/km or duration ≤90s):"
        f" Your ANALYSIS must be formatted EXACTLY as multiple output lines — each on its own line:"
        f" Line 1 (header): NxDUR / Xmin walk · avg GAP X:XX/km · +/-Xsec vs threshold ({threshold_pace}/km){_thr_note}"
        f" Lines 2..N+1 (one per run rep, use gap_pace if present else pace): Rep N: DUR · GAP X:XX/km · AVGbpm/MAXbpm"
        f" Final lines: HR zone split · decoupling % if >40 min | RPE and how did it feel?"
        " Else (continuous run): Line 1 = distance + avg GAP pace vs threshold — state +/- sec/km."
        " Line 2 = HR zone distribution + aerobic decoupling % if >40 min."
        " Line 3 = \"RPE and how did it feel?\""
    )

    coaching_level = (profile or {}).get("coaching_level", "mid")

    return f"""\
Check for new activities for {first_name} and stub them into the session log.

{_level_block(coaching_level)}
{illness_lib.prompt_block(slug, first_name=first_name)}
{ack_lib.PROMPT_NOTE}


Step 1 — Fetch data via Bash. EVERY icu_fetch.py call you make must carry
`--caller activity-watcher`. This job reads training and talks about it; it does not write
the calendar, and that flag is what makes the refusal automatic rather than a rule you have
to remember:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --caller activity-watcher --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --caller activity-watcher --endpoint history --days 3
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --caller activity-watcher --endpoint events --start {today} --end {today}

Step 2 — Read:
- ClaudeCoach/athletes/_shared/persistent-rules.md (GLOBAL coaching rules - apply to every athlete; these govern debriefs and Strava descriptions)
- ClaudeCoach/athletes/{slug}/persistent-rules.md (permanent coaching rules — these override defaults)
- ClaudeCoach/athletes/{slug}/session-log.json — note all existing activity_id values.

Step 3 — For the most recent activity that is NOT already in session-log.json:
  Duplicate upload guard: if multiple activities of the same sport on the same date are NOT in session-log.json, they are likely the same session uploaded from two sources (e.g. Garmin + Strava). Only process the one with the highest numeric ID. If that highest-ID activity is already in session-log.json (by ID, date, or both), output ACTIVITY_ID: none — the session is already logged.

  - Fetch full detail via Bash: python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --caller activity-watcher --endpoint activity_detail --activity-id <id>
  - If sport is Run, VirtualRun, or Swim: also fetch extended metrics:
    python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --caller activity-watcher --endpoint extended_metrics --activity-id <id>
  - If the activity has a strava_id field: fetch Strava laps and splits:
    python3 ClaudeCoach/lib/strava_fetch.py --athlete {slug} --strava-id <strava_id>
    If this fails or there is no strava_id, the athlete may not have Strava connected — proceed
    without it (do NOT retry or troubleshoot) and use the documented fallbacks.
  - Add a stub entry to ClaudeCoach/athletes/{slug}/session-log.json (prepend to array, most recent first).

  For Ride or Run:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "<sport>",
      "tss": <tss>, "duration_min": <duration>, "distance_km": <distance or null>,
      "avg_power": <avg_power or null>, "norm_power": <norm_power or null>, "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null,
      "injury_pain_during": null, "injury_pain_next_morning": null,
      "nutrition_g_carb": null, "hydration_ml": null,
      "nutrition_mg_sodium": null, "notes": null,
      "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }}

  For Swim:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Swim",
      "tss": <tss>, "duration_min": <duration>, "distance_km": <distance_m / 1000>,
      "pace_per_100m": <avg pace in seconds per 100m — distance_m / (duration_s / 100)>,
      "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null, "notes": null, "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }}
    Also append to ClaudeCoach/athletes/{slug}/swim-log.json:
    {{"date":"<YYYY-MM-DD>","activity_id":"<id>","name":"<name>","distance_m":<int>,"pace_per_100m":<seconds float>,"duration_min":<int>,"tss":<int or null>}}
    Include swim-log.json in the git add below.

  For WeightTraining / Strength:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Strength",
      "tss": <tss or null>, "duration_min": <duration>,
      "rpe": null, "feel": null, "notes": null, "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }}

  - Write the updated array back to ClaudeCoach/athletes/{slug}/session-log.json.

Step 4 — Respond in EXACTLY this format (no other text):
ACTIVITY_ID: <id or none>
ANALYSIS: <coaching message — see rules below>

{first_name}: FTP {ftp} W.{injury_line}

Interval source: prefer Strava laps when they give a cleaner breakdown than ICU (e.g. ICU splits one effort into 3 pieces). Use gap_pace from Strava laps where available, else pace.

Rules for ANALYSIS — each logical line must be a separate output line (no semicolons to merge lines):

Always lead with a one-sentence narrative verdict (e.g. "Solid Z2. Form held to the end.").

Then choose the metric set BY SESSION TYPE and surface only what matters for THIS session, not a fixed block every time:
- Easy / steady / recovery: the verdict plus at most 1-2 key numbers (e.g. avg HR and HR drift). Keep it short (well under 200 characters), no headers, no per-rep lines.
- Long endurance ride or run (roughly 90 min or more, steady): duration, IF, aerobic decoupling, and the fuelling prompt. A few lines, no per-rep breakdown.
- Intervals / reps (structured): the per-rep splits and target adherence in the sport format below. This per-rep detail IS the point for these sessions, so show it by default.
- Brick (bike and run in one activity, or bike then run back-to-back): ALWAYS report the bike and run legs separately, splitting time and Load per leg.
This selection is authoritative and governs the per-sport RIDE / RUN / SWIM formats below: use a format's full header and per-rep breakdown ONLY when the session is intervals/structured, or when the athlete has asked for the full breakdown. For easy/steady/recovery and long-endurance sessions, stay with the verdict plus the few relevant numbers above. Where these rules conflict with the per-sport format instructions below, these rules win.

Drift / decoupling: when computing HR drift or HR:power decoupling, exclude laps that fall within icu_warmup_time seconds from the start and icu_cooldown_time seconds from the end (both fields in activity_detail; if absent or zero, use all laps).

RIDE:
- For Ride sessions: ALWAYS fetch power zone distribution (extended_metrics or icu_zone_times) before characterising intensity. Zone split (% time Z1–Z5) is the primary intensity descriptor. NP and IF are secondary and must be qualified against the zone split. Do not characterise a ride as Z2/easy unless >70% of time is in Z1–Z2 by power.
- Structured (Strava laps show alternating hard/easy, or ICU >3 intervals): header line + one line per WORK interval.
  Header: NxDUR @ AVG W (X% FTP) · NP XXXw · IF X.XX
  Rep lines: Rep N: DUR · XXXw (X% FTP) · AVGbpm/MAXbpm
  Final line: completion note if intervals missed, else "Nutrition — g carbs/hr, bottles, and sodium?"
- Unstructured ≤90 min: NP + IF | "Nutrition — g carbs/hr, bottles, and sodium?"
- Unstructured >90 min: NP + IF | aerobic decoupling % | "Nutrition — g carbs/hr, bottles, and sodium? (recent avg: [avg g/hr from last 4 rides >90 min in session-log.json with nutrition_g_carb set] · race target {nutrition_target}g/hr)"

RUN:
{run_injury_ask}
Running power: if icu_average_watts is not null in activity_detail (Garmin running power configured), add one line after the HR line: "Running power: Xw avg · pace-power check: [brief note if pace and power effort level diverge — e.g. power high vs easy pace = headwind/elevation]". Skip entirely if icu_average_watts is null.
Easy/Z2 verdict: When characterising run intensity use icu_average_hr (from activity_detail), Z2 ceiling = {run_hr_cap} bpm, zone time breakdown (icu_zone_times or extended_metrics), and total_ascent_m (from activity_detail). Apply deterministically: a run is EASY if avg_hr < {run_hr_cap} bpm AND (Z1+Z2 time) >= 75% of total run time. Do NOT flip to NOT EASY solely because Z3 exceeded 20% when avg_hr < {run_hr_cap} bpm AND total_ascent_m > 40m — terrain-driven HR spikes on climbs are expected in hilly sub-ceiling easy runs.
Elevation source: total_elevation_gain / total_elevation_loss / total_ascent_m for a session ALWAYS come from the activity_detail root fields (whole-activity totals). NEVER read elevation from extended_metrics or icu_intervals entries — those are per-segment/per-interval values (often single-digit metres) and will misstate the session's actual terrain by an order of magnitude if quoted as the whole-activity gain.

SWIM:
- Pool with Strava laps (fetched above): rep durations/paces come from the STRAVA LAPS — they are
  the native Garmin button-press boundaries and are correct. NEVER use icu_intervals for rep
  timings: ICU infers boundaries from velocity_smooth and over-extends every rep ~2-4s into the
  surrounding rest, so its paces read slower than actually swum (bug confirmed 2026-06-11).
  Identify the work reps from the laps (skip warmup/cooldown/drill laps — obvious from the
  distance/pace pattern). Pace per rep (seconds/100m) = (lap duration / lap distance) * 100 →
  format as M:SS/100m. icu_intervals may be used ONLY for per-rep HR, matched to laps by order.
  CSS target from profile swim_css_per_100m. Delta = rep_pace_s - css_s (positive = slower than CSS).
  Header: Nx(distance)m · CSS X:XX/100m · avg rep X:XX/100m (+/-Xsec vs CSS)
  Rep lines (one per work lap): Rep N: X:XX/100m (+/-Xsec vs CSS) · AVGbpm
  Final line: "RPE and how did it feel?"
- Pool with NO Strava laps (no strava_id / fetch failed): fall back to icu_intervals WORK
  intervals (type="WORK"), same format, and append "(paces from ICU — read ~2-4s/100m pessimistic)".
- Else (OWS or neither): use interval_summary from activity detail if present, else distance + avg pace vs CSS +/- seconds.
  Final line: "RPE and how did it feel?"

STRENGTH: duration | "RPE and what was the main focus?"

For unstructured rides > 3 hours (or structured rides > 3 hours where Pa:HR data is available): also output a DECOUPLING line:
DECOUPLING: <activity_id>|<date>|<name>|<duration_min>|<intensity_factor>|<decoupling_pct>|<tss>
If conditions not met or Pa:HR data unavailable: DECOUPLING: none

For structured sessions with >3 intervals:
SESSION_CHART: {{"name":"<activity name>","ftp":{ftp},"intervals":[{{"duration_seconds":600,"average_power":250,"type":"WORK"}},...}}]}}
Fetch interval data from activity_detail endpoint. type values: WORK, RECOVERY, WARMUP, COOLDOWN.
If unstructured: SESSION_CHART: none

If a planned session exists in today's events matching this sport, output:
PLAN_DELTA: <planned_session_name>|<planned_tss>|<actual_tss>|<delta_pct>
(delta_pct = round((actual_tss - planned_tss) / planned_tss * 100, 1))
If no planned session found for today or TSS unavailable: PLAN_DELTA: none

If the activity looks like a performance threshold test, output:
TEST_RESULT: <type>|<value>|<activity_id>
Rules:
- ftp: activity name contains "ramp", "ftp test", "20 min", "20-min", or "threshold test" (case-insensitive), OR single sustained interval >18 min at >90% FTP. Value = ramp → peak 1-min power × 0.75; 20-min test → 20-min avg power × 0.95. Round to nearest whole watt.
- css: swim activity where name contains "css", "critical swim speed", or "time trial", and distance ≥ 300m. Value = pace as MM:SS per 100m.
- lthr: run activity where name contains "lthr", "lactate", "hr test", "tempo test". Value = avg HR during the sustained effort portion (bpm, integer).
If not a recognisable threshold test: TEST_RESULT: none

If no activities at all: ACTIVITY_ID: none  ANALYSIS: none

ALREADY-DISCUSSED CHECK — recent Telegram chat with this athlete (oldest first):
{recent_chat}
If THIS activity has already been discussed in the chat above — the athlete asked about it or an
analysis of it was already given (match on sport, distance/duration, recency) — output exactly
`ANALYSIS: discussed` instead of repeating it. Numbers the athlete has already seen (distance,
pace, HR, Load, decoupling) are NOT new insight; only produce a normal ANALYSIS here if you have
something material the chat did not cover. All logging steps (session-log stub, swim-log, etc.)
still apply regardless — `discussed` suppresses the message, never the capture.
When writing the session-log stub, if the chat above contains the athlete's feedback for THIS
activity (RPE, injury/ankle pain scores, nutrition), fill those fields from the chat instead of
leaving them null — data the athlete already gave must never be re-asked by the evening jobs.

CRITICAL: Your entire response must contain only the ACTIVITY_ID and ANALYSIS lines above. Do not output reasoning steps, file read confirmations, tool call summaries, or any other text. All processing is internal and silent.
ANALYSIS scope: describe only the activity being analysed. Do NOT mention other planned sessions from today's calendar, ask whether a different planned session happened, or comment on sessions that were not completed."""


def _notify(msg, chat_id, slug=None):
    sent = False
    for _attempt in (1, 2):
        try:
            # --no-history: this script appends to history itself (below)
            r = subprocess.run(
                ["python3", str(NOTIFY), "--no-history", "--chat-id", str(chat_id), msg],
                cwd=PROJECT_DIR, timeout=15,
            )
            if r.returncode == 0:
                sent = True
                break
        except Exception:
            pass
    if not sent:
        ops_log.alert("activity-watcher", "Telegram send failed after retry", athlete=slug or "")
    if slug and sent:
        try:
            _log_to_history(slug, msg)
        except Exception:
            pass
    return sent


def _credit_heat_exposure(slug: str, activity_id: str, profile: dict) -> None:
    """Auto-log outdoor heat exposure (device ambient ≥25°C) as acclimation dose
    in heat-log.json — "I'm in hot venues enough" becomes measured, not assumed."""
    try:
        if not heat_lib.state(slug, profile)["active"]:
            return
        r = subprocess.run(
            ["python3", str(BASE / "lib/icu_fetch.py"), "--athlete", slug,
             "--caller", CALLER, "--endpoint", "history", "--days", "3"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=30,
        )
        act = next((a for a in json.loads(r.stdout)
                    if str(a.get("id")) == str(activity_id)), None)

        # Recover GPS from the streams endpoint when the summary has none — ICU
        # stores lat in the latlng stream's `data` and lon in `data2`.
        def _latlng_from_streams(aid):
            try:
                from icu_api import IcuClient
                acfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
                cl = IcuClient(acfg["icu_athlete_id"], acfg["icu_api_key"])
                ll = next((s for s in cl.get_activity_streams(aid)
                           if s.get("type") == "latlng"), None)
                if not ll:
                    return None
                lat, lon = ll.get("data") or [], ll.get("data2") or []
                for i in range(min(len(lat), len(lon))):
                    if lat[i] is not None and lon[i] is not None:
                        return [float(lat[i]), float(lon[i])]
            except Exception as e:
                print(f"[heat-credit:{slug}] latlng stream fallback failed: {e}", file=sys.stderr)
            return None

        entry = heat_lib.exposure_entry(act, latlng_fallback=_latlng_from_streams) if act else None
        if not entry:
            return
        log_f = BASE / f"athletes/{slug}/heat-log.json"
        entries = json.loads(log_f.read_text()) if log_f.exists() else []
        if any(str(e.get("activity_id", "")) == str(activity_id) for e in entries):
            return
        entries.append(entry)
        log_f.write_text(json.dumps(entries, indent=2))
        ops_log.record_run("activity-watcher", athlete=slug, ok=True,
                           detail=f"heat dose {entry['dose']} auto-credited ({entry['context']})")
    except Exception as exc:
        print(f"[heat-credit:{slug}] {exc}", file=sys.stderr)


def _acknowledgement(slug: str, activity_id: str,
                     coaching_level: str) -> tuple[str, list]:
    """The §8.3 session-level acknowledgement for this activity, or ("", []).

    Evaluated HERE rather than in the prompt because _build_prompt runs before
    the activity is known — the model discovers it and emits ACTIVITY_ID — so an
    id-keyed conditional in the prompt would be the instruction-following this
    repo has documented failing twice (lib/plan_builder.py:5-7). Rendered in
    Python once the id is authoritative, the acknowledgement is unconditional
    and cannot be dropped, softened or duplicated by the model.

    Reads the ICU history feed, never session-log.json — the feed reaches ~12
    months where the logs reach three to four, and it carries no stub rows with a
    null distance, which is what triggers 3 and 4 actually depend on (see the
    lib docstring). Returns the keys so the caller can mark them only after the
    message has actually gone out.

    Any failure returns ("", []) — a missing acknowledgement is a non-event, a
    wrong one discredits the rest of the debrief.
    """
    try:
        from icu_api import IcuClient
        cfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        activity = client.get_activity_detail(activity_id)
        if not isinstance(activity, dict) or not activity:
            return "", []
        history = client.get_training_history(365)
        adir = BASE / "athletes" / slug
        hits = ack_lib.evaluate_activity_from_dir(
            adir, activity, history, coaching_level=coaching_level,
            fired=ack_lib.load_state_from_dir(adir)["fired"])
        # Every hit that evaluated true is marked, but only the first is said
        # (§8.4 bans padding). A "longest ride" held back today in favour of the
        # comeback line is still a fact about TODAY's ride — letting it re-fire
        # on a later, shorter ride would be the false claim.
        return ack_lib.activity_prefix(hits), [h["key"] for h in hits]
    except Exception:
        return "", []


def _run_durability_note(slug: str, activity_id: str) -> tuple[str, bool]:
    """Deterministic durability line for a completed RUN with power — computed
    from the per-second streams, logged for trending, appended to the analysis
    message. Returns (note, flagged); ("", False) when not a run / no power /
    too short / any failure. `flagged` comes from the metrics rather than from
    a marker in the rendered text, so the prose is free to change."""
    try:
        from icu_api import IcuClient
        cfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        detail = client.get_activity_detail(activity_id)
        if (detail.get("type") or "") not in ("Run", "TrailRun", "VirtualRun"):
            return "", False
        streams = {s.get("type"): s.get("data") for s in client.get_activity_streams(activity_id)}
        m = compute_run_durability(streams.get("time"), streams.get("watts"),
                                   streams.get("heartrate"), streams.get("cadence"),
                                   streams.get("velocity_smooth"))
        if not m:
            return "", False
        log_f = BASE / f"athletes/{slug}/run-durability-log.json"
        entries = json.loads(log_f.read_text()) if log_f.exists() else []
        if not any(str(e.get("activity_id")) == str(activity_id) for e in entries):
            entries.append({
                "activity_id": str(activity_id),
                "date": (detail.get("start_date_local") or "")[:10],
                "name": detail.get("name", ""),
                "duration_min": round((detail.get("moving_time") or 0) / 60),
                **{k: m[k] for k in ("decoupling_pct", "cadence_fade_pct", "cost_fade_pct", "flags")},
            })
            log_f.write_text(json.dumps(entries[-200:], indent=2))
        return "\n\n" + fade_line(m), bool(m.get("flags"))
    except Exception as exc:
        print(f"[run-durability:{slug}] {exc}", file=sys.stderr)
        return "", False


def _dedup_session_log(path: Path) -> None:
    """Remove duplicate activity_ids, keeping the most-complete entry for each."""
    if not path.exists():
        return
    try:
        entries = json.loads(path.read_text())
        seen: dict = {}
        order: list = []
        for e in entries:
            aid = str(e.get("activity_id", ""))
            if not aid:
                order.append(e)
                continue
            if aid not in seen:
                seen[aid] = e
                order.append(aid)
            else:
                existing = seen[aid]
                if sum(1 for v in e.values() if v is not None) > sum(1 for v in existing.values() if v is not None):
                    seen[aid] = e
        deduped = [seen[x] if isinstance(x, str) else x for x in order]
        if len(deduped) < len(entries):
            path.write_text(json.dumps(deduped, indent=2))
            print(f"[dedup] removed {len(entries) - len(deduped)} duplicate(s)", file=sys.stderr)
    except Exception:
        pass


def _resolve_ftp(slug: str, profile: dict, session_log_f: Path) -> int:
    """Return FTP to use: profile value if a test exists in last 10 weeks, else ICU sport_settings eFTP."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(weeks=10)).isoformat()
    test_keywords = ("ramp", "ftp test", "20 min", "20-min", "threshold test")
    try:
        if session_log_f.exists():
            for e in json.loads(session_log_f.read_text()):
                if e.get("date", "") >= cutoff and e.get("sport") in ("Ride", "VirtualRide"):
                    if any(kw in (e.get("name") or "").lower() for kw in test_keywords):
                        return profile.get("ftp_watts") or 250
    except Exception:
        pass
    # No recent test — use ICU eFTP from fitness endpoint sportInfo
    try:
        r = subprocess.run(
            ["python3", str(BASE / "lib/icu_fetch.py"), "--athlete", slug,
             "--caller", CALLER, "--endpoint", "fitness", "--days", "1"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=30,
        )
        rows = json.loads(r.stdout)
        row = rows[-1] if rows else {}
        for s in (row.get("sportInfo") or []):
            if s.get("type") == "Ride" and s.get("eftp"):
                return int(s["eftp"])
    except Exception:
        pass
    return profile.get("ftp_watts") or 250


def _has_new_activity(slug: str, existing_ids: set) -> bool:
    """Cheap, LLM-free pre-gate: True if ICU history shows any activity whose id
    is not already in session-log.json. This lets the expensive analysis LLM be
    skipped entirely on the ~99% of cycles with nothing new — so the watcher can
    poll often without burning the Sonnet weekly bucket.

    Fail-OPEN: any fetch/parse error returns True so we never silently miss an
    activity (worst case = one wasted LLM call that returns ACTIVITY_ID: none,
    exactly as before this gate existed)."""
    try:
        r = subprocess.run(
            ["python3", str(BASE / "lib/icu_fetch.py"), "--athlete", slug,
             "--caller", CALLER, "--endpoint", "history", "--days", "3"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=30,
        )
        if r.returncode != 0:
            return True
        for a in json.loads(r.stdout):
            if str(a.get("id", "")) not in existing_ids:
                return True
        return False
    except Exception:
        return True


def load_state(state_file):
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {"last_id": None}


def save_state(state, state_file):
    state_file.write_text(json.dumps(state))


def acquire_lock():
    if LOCK_FILE.exists():
        if time.time() - LOCK_FILE.stat().st_mtime < 1200:
            return False
    LOCK_FILE.touch()
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


# --- deferring the evening ask into the 21:00 check-in -------------------------------
# A session finished at 18:30 used to produce a debrief ending in a question at ~18:35 and
# a follow-up nudge at ~20:35, on top of the 20:30 brief and the 21:00 check-in. Inside
# this window the question is not sent here at all: it is queued for evening-checkin.py,
# which is now the only evening surface that asks anything. The quick-log keyboard still
# goes out with the debrief, so the one-tap capture route is unaffected — this defers the
# TEXT question only.
_EVENING_WINDOW_START = 18   # ~3h before the check-in
_EVENING_CHECKIN_HOUR = 21
QUEUE_FILE = ".evening-ask.json"


def _in_evening_window(now=None):
    now = now or datetime.now()
    return _EVENING_WINDOW_START <= now.hour < _EVENING_CHECKIN_HOUR


def _queue_evening_ask(slug, activity_id, sport, name, question, state=None,
                       state_file=None):
    """Append one question to the athlete's evening-ask queue and suppress the follow-up
    nudge for that activity, so the deferral does not simply reappear 2h later as its own
    push. evening-checkin consumes the queue (and drops entries whose data has since been
    captured)."""
    qpath = BASE / "athletes" / slug / QUEUE_FILE
    try:
        entries = json.loads(qpath.read_text()) if qpath.exists() else []
        if not isinstance(entries, list):
            entries = []
    except Exception:
        entries = []
    aid = str(activity_id or "")
    entries = [e for e in entries if str(e.get("activity_id", "")) != aid]
    entries.append({
        "activity_id": aid,
        "sport": sport or "",
        "name": name or "",
        "question": question.strip(),
        # WHICH question this is, so the 21:00 consumer can check the right field.
        # evening-checkin._field_already_captured currently drops the entry if ANY
        # of rpe / injury_pain_during / nutrition_g_carb / feel came in, which errs
        # toward not asking (safe) but will drop a fuelling ask because an RPE
        # arrived. With this stamp that check can become per-field; the consumer is
        # in evening-checkin.py and is not changed here.
        "kind": ask_gate.classify_question(question) or "",
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        qpath.parent.mkdir(parents=True, exist_ok=True)
        qpath.write_text(json.dumps(entries[-20:], indent=1))
    except Exception:
        return False
    if state is not None and aid:
        nudged = set(state.get("nudged_ids") or [])
        nudged.add(aid)
        state["nudged_ids"] = list(nudged)
        if state_file:
            save_state(state, state_file)
    return True


def _defer_trailing_question(analysis, slug, activity_id, sport, name, state=None,
                             state_file=None, now=None):
    """Strip the debrief's trailing question line(s) inside the evening window and queue
    them for the 21:00 check-in. Returns the analysis to send.

    Deterministic rather than a prompt instruction: every per-sport format in the prompt
    above ends its block with a question line, and a model told to sometimes omit it
    reformats the rest. Only the trailing block is touched: the scan stops at the first
    line from the bottom that carries no '?'."""
    if not analysis or not _in_evening_window(now):
        return analysis
    lines = analysis.rstrip().split("\n")
    stripped = []
    # `contains ?`, not `ends with ?`: the real question lines carry a trailing
    # parenthetical — "Injury pain score during and this morning? (0-10)",
    # "Nutrition — g carbs/hr, bottles, and sodium? (recent avg: 54g/hr · race target 70g/hr)".
    # Scanning from the bottom and stopping at the first non-question line keeps this
    # confined to the trailing block.
    while lines and (not lines[-1].strip() or "?" in lines[-1]):
        line = lines.pop()
        if line.strip():
            stripped.insert(0, line.strip())
    if not stripped:
        return analysis
    if not lines:
        # The whole message was the question — there is no debrief to send without it.
        # Queue it and send nothing; the check-in asks at 21:00.
        _queue_evening_ask(slug, activity_id, sport, name, " ".join(stripped),
                           state, state_file)
        return ""
    _queue_evening_ask(slug, activity_id, sport, name, " ".join(stripped),
                       state, state_file)
    return "\n".join(lines).rstrip()


def _load_history(slug):
    """The athlete's Telegram transcript, or [] — used only for the volunteered
    scan, which is why a failed read degrades to 'not volunteered' (ask) rather
    than to silence."""
    hist_f = BASE / "athletes" / slug / "telegram" / "history.json"
    try:
        data = json.loads(hist_f.read_text()) if hist_f.exists() else []
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _filter_debrief_questions(analysis, entry, slug, history=None):
    """Drop or fold the debrief's trailing asks: nothing already answered, and at
    most ONE question in the message.

    `history` is injected for the tests (scripts/test_question_fatigue.py) and
    read off the athlete's transcript when omitted.

    Runs BEFORE `_defer_trailing_question` on purpose. That function queues the
    trailing question for the 21:00 check-in, so a question suppressed here must
    be suppressed first or the suppression just moves the nag three hours later.

    Deterministic for the reason the whole prompt is: every per-sport format ends
    its block with a question line, and a model told to sometimes omit one
    reformats the rest (`lib/plan_builder.py:5-7`). It edits only segments that
    carry a '?', and leaves any question it cannot classify untouched — a garbled
    debrief is worse than an extra ask.
    """
    if not analysis or not isinstance(entry, dict):
        return analysis

    history = _load_history(slug) if history is None else history
    since = ask_gate.entry_synced_at(entry)

    # First pass: what is being asked, and what of it is already known. Both the
    # session-log entry and the athlete's own chat turns count as answered.
    asked, answered = [], {}
    for line in analysis.split("\n"):
        for seg in line.split("|"):
            kind = ask_gate.classify_question(seg)
            if not kind:
                continue
            if kind not in asked:
                asked.append(kind)
            if kind not in answered:
                answered[kind] = ask_gate.already_answered(
                    entry, kind, history=history, since=since)

    unanswered = [k for k in asked if not answered.get(k)]
    keep = ask_gate.pick_question(entry, unanswered)

    out_lines = []
    for line in analysis.split("\n"):
        segs = line.split("|")
        rebuilt, dropped_any = [], False
        for seg in segs:
            kind = ask_gate.classify_question(seg)
            if kind is None:
                rebuilt.append(seg)
                continue
            if answered.get(kind):
                # Fold the value in instead of asking for it again. An empty fold
                # (nothing quotable) just drops the ask.
                fold = ask_gate.known_value_line(entry, kind)
                dropped_any = True
                if fold:
                    rebuilt.append(f" {fold} " if len(segs) > 1 else fold)
                continue
            if kind != keep:
                dropped_any = True      # rides along in the next natural exchange
                continue
            # The one surviving question. The compound ankle ask still loses its
            # morning half: that score has one owner, the morning check-in.
            rebuilt.append(ask_gate.strip_morning_half(seg) if kind == ask_gate.ANKLE
                           else seg)
        if dropped_any and not any(s.strip() for s in rebuilt):
            continue                    # the whole line was asks that are gone
        out_lines.append("|".join(rebuilt).strip(" |") if dropped_any
                         else "|".join(rebuilt))

    return "\n".join(out_lines).rstrip()


def _chat_has_recent_feedback(slug, lookback_minutes=30):
    """True if the athlete's Telegram messages in the last 30 minutes contain session
    feedback (RPE / how-it-felt / pain) — so the follow-up nudge doesn't re-ask
    for data the athlete already gave (the logged re-ask bug)."""
    hist_f = BASE / "athletes" / slug / "telegram" / "history.json"
    if not hist_f.exists():
        return False
    try:
        entries = json.loads(hist_f.read_text())
    except Exception:
        return False
    kws = ("rpe", "/10", "felt", "feeling", "pain", "ankle")
    cutoff = datetime.now() - timedelta(minutes=lookback_minutes)
    for e in reversed(entries):
        ts_str = e.get("ts", "")
        if ts_str:
            try:
                if datetime.fromisoformat(ts_str) < cutoff:
                    break
            except Exception:
                pass
        u = (e.get("user") or "").lower()
        if u and any(k in u for k in kws):
            return True
    return False


def _send_followup_nudge(state, session_log_f, chat_id, injuries=None, state_file=None, slug=None):
    """If any stub from today is >2h old with rpe=null and hasn't been nudged, send one re-ping."""
    if not session_log_f.exists():
        return
    try:
        log_entries = json.loads(session_log_f.read_text())
    except Exception:
        return

    now = datetime.now()
    today_str = now.date().isoformat()
    nudged_ids = set(state.get("nudged_ids") or [])
    has_injury = bool(injuries)

    for e in log_entries:
        if not e.get("stub", False):
            continue
        sport = e.get("sport", "session")
        # For injury runs the ANALYSIS already asks for injury score — skip nudge if that's done
        if sport == "Run" and has_injury:
            if e.get("injury_pain_during") is not None or e.get("rpe") is not None:
                continue
        elif e.get("rpe") is not None:
            continue
        # Only nudge for today's activities — don't nag about old stubs
        if e.get("date", "") != today_str:
            continue
        aid = str(e.get("activity_id", ""))
        if aid in nudged_ids:
            continue
        # Don't re-ask if the athlete already gave RPE/feel/pain in chat recently —
        # the analysis fills the stub from chat, but a race can leave rpe=null while
        # the data sits in history; re-asking then is the logged bug.
        if slug and _chat_has_recent_feedback(slug):
            nudged_ids.add(aid)
            state["nudged_ids"] = list(nudged_ids)
            if state_file:
                save_state(state, state_file)
            continue
        logged_at = e.get("logged_at")
        if not logged_at:
            continue
        try:
            logged_dt = datetime.fromisoformat(logged_at + "T00:00:00" if "T" not in logged_at else logged_at)
        except Exception:
            continue
        age_hours = (now - logged_dt).total_seconds() / 3600
        if 2 <= age_hours <= 24:
            name = e.get("name", "")
            # ONE question, chosen in Python. The old branch asked "RPE for the
            # run? (1-10) — and how did the ankle feel this morning?": two
            # questions in one nudge, and the morning half belongs to the morning
            # check-in, which tracks its own answers. Per-kind asked-and-answered
            # on top of the coarse _chat_has_recent_feedback gate above — that one
            # is any-feedback-in-30-minutes, this one is THIS question since THIS
            # activity synced, so neither subsumes the other.
            _hist = _load_history(slug) if slug else []
            _since = ask_gate.entry_synced_at(e)
            wanted = []
            if sport == "Run" and has_injury:
                wanted.append(ask_gate.ANKLE)
            wanted.append(ask_gate.RPE)
            if (e.get("duration_min") or 0) >= ask_gate.LONG_SESSION_MIN:
                wanted.append(ask_gate.NUTRITION)
            wanted = [k for k in wanted
                      if not ask_gate.already_answered(e, k, history=_hist, since=_since)]
            kind = ask_gate.pick_question(e, wanted)
            if kind is None:
                # Everything this nudge could ask for is already known. Mark it so
                # the stub is not revisited every cycle.
                nudged_ids.add(aid)
                state["nudged_ids"] = list(nudged_ids)
                if state_file:
                    save_state(state, state_file)
                continue
            label = name or {"Run": "run", "Swim": "swim",
                             "Ride": "ride"}.get(sport, "last session")
            if kind == ask_gate.ANKLE:
                msg = f"Ankle score during the {label}? (0–10)"
            elif kind == ask_gate.NUTRITION:
                msg = f"Fuelling check for the {label} — roughly g carbs/hr?"
            elif sport == "Swim":
                msg = f"How did the {label} feel? (RPE 1–10 if you have it)"
            else:
                msg = f"RPE for the {label}? (1–10)"
            if slug and _in_evening_window():
                # Inside the evening window this nudge is exactly the push being merged
                # away: it would land between the 20:30 brief and the 21:00 check-in. Queue
                # it for the check-in instead. _queue_evening_ask marks it nudged.
                _queue_evening_ask(slug, aid, sport, name, msg, state, state_file)
                break
            _notify(msg, chat_id, slug=slug)
            nudged_ids.add(aid)
            state["nudged_ids"] = list(nudged_ids)
            # Persist nudged_ids so the same stub is never nudged again
            if state_file:
                save_state(state, state_file)
            break  # one nudge per cycle


def _check_test_reminders(adir: Path, chat_id: str, state: dict, state_file: Path | None):
    """Nudge athlete if a performance test is due within 3 days and not yet notified."""
    slug = adir.name
    test_f = adir / "test-schedule.json"
    if not test_f.exists():
        return
    try:
        tests = json.loads(test_f.read_text())
    except Exception:
        return

    today = date.today()
    notified_tests: set = set(state.get("notified_tests") or [])
    changed = False

    for t in tests:
        if t.get("completed"):
            continue
        test_date_str = t.get("date", "")
        label = t.get("label", "Performance test")
        key = f"{t.get('type')}:{test_date_str}"
        if key in notified_tests:
            continue
        try:
            test_dt = date.fromisoformat(test_date_str)
        except Exception:
            continue
        days_until = (test_dt - today).days
        if 0 <= days_until <= 3:
            when = "today" if days_until == 0 else ("tomorrow" if days_until == 1
                   else f"in {days_until} days ({test_dt.strftime('%A')})")
            protocol = t.get("protocol", "")
            _notify(f"⏱ Test due {when}: *{label}*\n_{protocol}_", chat_id, slug=slug)
            notified_tests.add(key)
            state["notified_tests"] = list(notified_tests)
            changed = True
            break  # one reminder per cycle

    if changed and state_file:
        save_state(state, state_file)


def _strava_description(first_name: str, sport: str, analysis: str,
                         plan_delta_raw: str | None, session_entry: dict | None,
                         laps: list | None = None, splits: list | None = None,
                         segment_prs: list | None = None,
                         coaching_level: str = "mid") -> str:
    """Call Claude to write a neutral, factual 3-line Strava description."""
    import re as _re

    clean_analysis = _re.sub(r"[*_]", "", analysis or "").strip()

    plan_block = "No planned session found."
    if plan_delta_raw and plan_delta_raw != "none":
        parts = plan_delta_raw.split("|")
        if len(parts) == 4:
            plan_name, planned_tss, actual_tss, delta_pct = parts
            sign = "+" if float(delta_pct) >= 0 else ""
            plan_block = (
                f"Planned: {plan_name.strip()} (target Load {planned_tss.strip()})\n"
                f"Actual Load: {actual_tss.strip()} ({sign}{delta_pct.strip()}% vs plan)"
            )

    entry = session_entry or {}
    sport_line = sport
    dur = entry.get("duration_min")
    dist = entry.get("distance_km")
    if dur:   sport_line += f", {int(dur)} min"
    if dist:  sport_line += f", {dist:.1f}km"

    # Build laps block (cap at 20 to keep prompt lean)
    laps_block = ""
    if laps:
        parts = []
        for i, lap in enumerate(laps[:20], 1):
            spd = lap.get("average_speed") or 0
            hr  = lap.get("average_heartrate")
            dist_km = (lap.get("distance") or 0) / 1000
            pace = _pace_str(spd) if spd else "?"
            hr_str = f", {int(hr)}bpm" if hr else ""
            parts.append(f"Lap {i}: {pace}{hr_str}, {dist_km:.2f}km")
        laps_block = "Device laps: " + " | ".join(parts) + "\n"

    splits_block = ""
    if splits:
        parts = []
        for s in splits[:20]:
            spd  = s.get("average_speed") or 0
            hr   = s.get("average_heartrate")
            n    = s.get("split", "?")
            pace = _pace_str(spd) if spd else "?"
            hr_str = f", {int(hr)}bpm" if hr else ""
            parts.append(f"km{n}: {pace}{hr_str}")
        splits_block = "Per-km splits: " + " | ".join(parts) + "\n"

    prs_block = ""
    if segment_prs:
        prs_block = f"Segment PRs set: {', '.join(segment_prs)}\n"

    if coaching_level == "beginner":
        line2_instruction = (
            "[one plain-English observation about how the session went. "
            "No power, pace, or zone numbers — effort and feel only. "
            "Neutral, factual tone.]"
        )
    elif coaching_level == "pro":
        line2_instruction = (
            "[one factual, data-dense observation. For bike sessions include NP, IF, and zone split. "
            "For runs include avg GAP and decoupling %. Use lap/split data where relevant. "
            "Never include RPE. Neutral and factual — lead with numbers, no commentary.]"
        )
    else:  # mid
        line2_instruction = (
            "[one factual observation about how the session went. "
            "For bike sessions include NP (e.g. \"NP 218W\") and HR avg. Never include RPE. "
            "Use lap/split data if it adds something specific. Neutral and factual — "
            "state what was done, no commentary or judgement.]"
        )

    prompt = f"""\
Write a Strava activity description for {first_name}.

Sport: {sport_line}
{plan_block}
Coaching analysis: {clean_analysis}
{laps_block}{splits_block}{prs_block}
IMPORTANT: The "Coaching analysis" may contain auto-detected interval efforts from the device — these are NOT training targets. The Aim must always reflect what was PLANNED (see "Planned:" above), not auto-detected efforts. If no plan is listed, use the sport type and duration to infer a sensible aim (e.g. "Z2 base ride").

Write exactly 2 lines, plain text, no markdown, no hashtags, no exclamation marks:
Line 1 — "Aim: [one plain sentence on what the session was PLANNED to target — from the Planned block, not the analysis]"
Line 2 — {line2_instruction} [append " 🏆" if any segment PRs were set]
Never sign the description or mention ClaudeCoach anywhere in it (Jamie, 13 Aug 2026 — descriptions are his public Strava, not the coach's).

The tone must be neutral and factual throughout — describe what was done using the data. No jokes, no wit, no sarcasm, no commentary on how the session compared to plan, nothing that implies the athlete fell short or quit.

Examples of the right tone:
- "Held Z2 throughout. Decoupling 3.2%."
- "NP 206W, IF 0.69. Z1–Z2 71%, Z4–Z5 15% on the climbs."
- "Intervals completed. NP 4W above target."
- "Continuous run, avg GAP 5:10/km. HR held in Z2."

Total under 300 characters. Output nothing else."""

    try:
        result = subprocess.run(
            [CLAUDE, "-p", "--model", "claude-haiku-4-5-20251001"],
            input=prompt,  # prompt on stdin, not argv (MAX_ARG_STRLEN)
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=60,
        )
        text = (result.stdout or "").strip()
        if text:
            return text
    except Exception:
        pass

    # Fallback: deterministic factual-only summary — never derived from clean_analysis,
    # which may contain private coaching content (RPE, pain, questions). No sign-off:
    # descriptions are the athlete's public Strava, not the coach's (Jamie, 13 Aug 2026).
    return f"{sport_line}."


def _derive_activity_name(slug: str, activity_date: str, sport: str, icu_id: str) -> str:
    """
    Call Claude Haiku to derive a Strava activity name from persistent-rules.md.
    Returns a name string, or the sentinel 'ask' if no matching rule is found.
    """
    rules_file = BASE / f"athletes/{slug}/persistent-rules.md"
    rules_text = rules_file.read_text() if rules_file.exists() else "(no rules)"

    prompt = f"""\
Derive a Strava activity name for a {sport} activity on {activity_date}.

Rules file:
{rules_text}

Look for any [perm] or [expires:...] rules that describe a sailing event, regatta, or training block covering {activity_date}.
If you find a matching rule, produce a concise activity name (e.g. "J70 Cervia — Race Day 2", "J70 Club Series — Practice Race").
If no rule covers this date, output exactly: ask

Output only the name or "ask". Nothing else."""

    try:
        result = subprocess.run(
            [CLAUDE, "-p", "--model", "claude-haiku-4-5-20251001"],
            input=prompt,  # prompt on stdin, not argv (MAX_ARG_STRLEN)
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=60,
        )
        name = (result.stdout or "").strip()
        if name:
            return name
    except Exception:
        pass
    return "ask"


def _verified_strava_write(sc, slug: str, strava_id, field: str, value: str) -> bool:
    """Write a Strava field, READ IT BACK, and retry once if it did not land.

    Why (13 Aug audit): both writes below discarded the boolean from update_activity —
    which is literally `r.status == 200`, so a non-200 that raises no HTTPError returned
    False into nothing — and then printed "updated" unconditionally. The log said the
    description was written on runs where Strava had taken nothing, which is how the bot
    came to tell Jamie four times that it had updated Strava when it had not.

    This is the absolute case for verification: we know the exact text we meant to store,
    so the read-back is proof either way (see write_verify.strava_desc_verdict with
    `expected`). Returns True only when the value is actually on Strava."""
    for attempt in (1, 2):
        try:
            sc.update_activity(strava_id, **{field: value})
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava {field} write attempt "
                  f"{attempt} raised: {exc}", file=sys.stderr)
        try:
            back = (sc.get_activity_detail(strava_id) or {}).get(field) or ""
        except Exception as exc:
            # Read-back failed: the write may well have landed, so do NOT claim failure
            # and do NOT retry into a possible duplicate. Say only what is known.
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava {field} read-back "
                  f"failed — write UNVERIFIED ({strava_id}): {exc}", file=sys.stderr)
            return False
        if write_verify.strava_desc_verdict(back, expected=value) == "ok":
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava {field} verified on "
                  f"{strava_id}" + (" (after retry)" if attempt == 2 else ""), file=sys.stderr)
            return True
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava {field} did NOT land on "
              f"{strava_id} (attempt {attempt})", file=sys.stderr)
    try:
        ops_log.alert("activity-watcher",
                      f"Strava {field} write did not land after a retry (activity {strava_id}) — "
                      f"the activity on Strava is unchanged", athlete=slug)
    except Exception:
        pass
    return False


def _rename_strava(slug: str, icu_id: str, new_name: str) -> bool:
    """Rename a Strava activity. Returns True on success."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE / "lib"))
        from icu_api import IcuClient
        from strava_client import StravaClient

        athletes_cfg = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes_cfg[slug]
        icu = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        detail = icu.get_activity_detail(icu_id)
        strava_id = detail.get("strava_id")
        if not strava_id:
            return False
        sc = StravaClient(slug)
        ok = _verified_strava_write(sc, slug, strava_id, "name", new_name)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava rename {icu_id} → "
              f"{new_name!r}: {'done' if ok else 'NOT APPLIED'}", file=sys.stderr)
        return ok
    except FileNotFoundError:
        return False
    except Exception as exc:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava rename failed: {exc}", file=sys.stderr)
        return False


def _strava_update(slug: str, icu_activity_id: str, analysis: str,
                   plan_delta_raw: str | None = None, session_entry: dict | None = None,
                   chat_id: str = "", coaching_level: str = "mid"):
    """Write a coaching note to the Strava activity description. Silent on any error."""
    # Water sports: no description — rename handled separately
    if (session_entry or {}).get("sport", "").lower() in _WATER_SPORTS:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Skipping description for {(session_entry or {}).get('sport')} {icu_activity_id}", file=sys.stderr)
        return
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE / "lib"))
        from icu_api import IcuClient
        from strava_client import StravaClient

        athletes_cfg = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes_cfg[slug]
        icu = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        detail = icu.get_activity_detail(icu_activity_id)
        strava_id = detail.get("strava_id")
        if not strava_id:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] No strava_id on {icu_activity_id} — skipping Strava", file=sys.stderr)
            return

        profile = {}
        adir = BASE / f"athletes/{slug}"
        if (adir / "profile.json").exists():
            try:
                profile = json.loads((adir / "profile.json").read_text())
            except Exception:
                pass
        first_name = profile.get("name", slug).split()[0]
        sport = (session_entry or {}).get("sport", "session")

        # Fetch Strava detail for laps, splits, and segment PRs
        sc = StravaClient(slug)
        laps = splits = segment_prs = None
        try:
            strava_detail = sc.get_activity_detail(strava_id)
            laps = strava_detail.get("laps") or None
            splits = strava_detail.get("splits_metric") or None
            segment_prs = [
                se["name"] for se in (strava_detail.get("segment_efforts") or [])
                if se.get("pr_rank") == 1 and se.get("name")
            ] or None
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava detail fetch failed: {exc}", file=sys.stderr)

        # Notify segment PRs via Telegram
        if segment_prs and chat_id:
            pr_lines = "\n".join(f"🏆 PR: {n}" for n in segment_prs)
            _notify(pr_lines, chat_id, slug=slug)

        description = _strava_description(
            first_name, sport, analysis, plan_delta_raw, session_entry,
            laps=laps, splits=splits, segment_prs=segment_prs,
            coaching_level=coaching_level,
        )

        _verified_strava_write(sc, slug, strava_id, "description", description)
    except FileNotFoundError:
        pass  # no tokens yet — silently skip
    except Exception as exc:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava update failed: {exc}", file=sys.stderr)


def _live_planned_load(slug: str, plan_name: str, days_back: int = 1) -> dict | None:
    """The planned event's load read LIVE from ICU, matched by event NAME.

    Split of responsibility, deliberately: the model identifies WHICH planned session
    a completed activity corresponds to (naming - it is good at that), and this reads
    the NUMBER (lookup + arithmetic - it is not). `_event_tss` resolves the load
    through the tested primitive, preferring `icu_training_load` then `load_target`,
    which is the authoritative order.

    Window is yesterday..today because an activity can sync after midnight. Returns
    None on any failure, so a debrief never breaks over this - the caller keeps the
    model's figure and the note still renders.
    """
    if not plan_name:
        return None
    try:
        import plan_tools  # local: pulls primitives, only needed on this path
        end = date.today()
        start = end - timedelta(days=max(0, days_back))
        r = subprocess.run(
            [sys.executable, str(BASE / "lib" / "icu_fetch.py"), "--athlete", slug,
             "--caller", CALLER, "--endpoint", "events", "--start", start.isoformat(),
             "--end", end.isoformat()],
            capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return None
        events = json.loads(r.stdout or "[]")
        if isinstance(events, dict):
            events = events.get("events") or events.get("data") or []
        want = plan_name.strip().lower()
        cands = [e for e in (events or [])
                 if not e.get("category") or e.get("category") == "WORKOUT"]
        for match in (lambda n: n == want,
                      lambda n: n.startswith(want) or want.startswith(n),
                      lambda n: want in n or n in want):
            for ev in cands:
                name = (ev.get("name") or "").strip().lower()
                if name and match(name):
                    res = plan_tools._event_tss(ev)
                    if res and res.get("tss"):
                        return {"name": ev.get("name") or "", "tss": float(res["tss"]),
                                "source": res.get("source"), "event_id": ev.get("id")}
    except Exception as e:
        print(f"[plan-delta:{slug}] live planned-load lookup failed: {e}",
              file=sys.stderr)
    return None


def _strava_refresh_updated(slug: str, state: dict, state_file: Path | None):
    """Re-describe any recent activities whose user-added fields changed since last Strava write."""
    import hashlib as _hl
    from datetime import date, timedelta

    log_path = BASE / f"athletes/{slug}/session-log.json"
    if not log_path.exists():
        return

    try:
        entries = json.loads(log_path.read_text())
    except Exception:
        return

    hashes  = state.get("strava_hashes") or {}
    cutoff  = (date.today() - timedelta(days=3)).isoformat()
    changed = False

    for e in entries:
        if (e.get("date") or "") < cutoff:
            continue
        aid = str(e.get("activity_id", ""))
        if not aid:
            continue
        fields = "|".join(str(e.get(f, "")) for f in ("rpe", "nutrition_g_carb", "injury_pain_during", "feel", "notes"))
        current = _hl.md5(fields.encode()).hexdigest()[:8]
        if hashes.get(aid) == current:
            continue
        # Fields updated since last write — regenerate description
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Strava refresh for {aid}", file=sys.stderr)
        try:
            subprocess.run(
                ["python3", str(BASE / "scripts/strava-update-activity.py"),
                 "--athlete", slug, "--icu-id", aid],
                cwd=PROJECT_DIR, timeout=90,
            )
            hashes[aid] = current
            changed = True
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] strava-update-activity failed: {exc}", file=sys.stderr)

    if changed:
        state["strava_hashes"] = hashes
        if state_file:
            save_state(state, state_file)


def check_athlete(slug, athlete_cfg, announce_empty=False):
    """Run activity check for one athlete. announce_empty=True (on-demand button)
    sends a brief 'nothing new' confirmation when the gate finds no new activity."""
    adir = BASE / f"athletes/{slug}"
    state_file = adir / "last_activity_state.json"
    session_log_f = adir / "session-log.json"
    decoupling_log = adir / "decoupling-log.json"
    chat_id = athlete_cfg.get("chat_id", "")

    # Load profile for athlete-specific data
    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    ftp = _resolve_ftp(slug, profile, session_log_f)
    first_name = profile.get("name", slug).split()[0]
    injuries = profile.get("injuries", [])
    run_hr_cap       = int(athlete_cfg.get("run_hr_cap", 150))
    nutrition_target = int(athlete_cfg.get("nutrition_target_g_hr", 90))

    state = load_state(state_file)

    # Dedup session log before any processing to catch merge-conflict duplicates
    _dedup_session_log(session_log_f)

    # Test reminders and Strava refresh run every cycle regardless of new activity
    _check_test_reminders(adir, chat_id, state, state_file)
    _strava_refresh_updated(slug, state, state_file)

    # Snapshot existing IDs before Claude runs
    existing_ids: set = set()
    if session_log_f.exists():
        try:
            existing_ids = {str(e.get("activity_id", "")) for e in json.loads(session_log_f.read_text())}
        except (json.JSONDecodeError, OSError):
            pass

    # Python-first gate: skip the analysis LLM entirely when ICU history shows
    # nothing new. The cheap cyclic tasks above (test reminders, Strava refresh)
    # and the follow-up nudge below still run every cycle — only the expensive
    # Claude call is gated, so this script can poll every 5 min cheaply.
    if not _has_new_activity(slug, existing_ids):
        if announce_empty and chat_id:
            _notify("No new activity since your last logged session. 👍", chat_id, slug=slug)
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries,
                             state_file=state_file, slug=slug)
        return

    # Recent chat so the analysis can suppress itself when the athlete already
    # discussed this activity with the bot ("we've already discussed this").
    recent_chat = "(no recent chat)"
    hist_f = adir / "telegram/history.json"
    if hist_f.exists():
        try:
            chat_lines = []
            for e in json.loads(hist_f.read_text())[-10:]:
                if e.get("user"):
                    chat_lines.append("athlete: " + str(e["user"])[:200].replace("\n", " "))
                if e.get("assistant"):
                    chat_lines.append("coach: " + str(e["assistant"])[:200].replace("\n", " "))
            if chat_lines:
                recent_chat = "\n".join(chat_lines[-16:])
        except Exception:
            pass

    prompt = _build_prompt(slug, first_name, ftp, injuries, profile,
                           run_hr_cap=run_hr_cap, nutrition_target=nutrition_target,
                           recent_chat=recent_chat)

    t_start = time.time()
    # Sonnet -> Haiku fallback: keeps activity analysis alive when the Sonnet
    # weekly bucket is maxed. run_claude returns rc=-1 on timeout (no chain burn).
    result = claude_call.run_claude(
        prompt, model=claude_call.SONNET, allowed_tools=TOOLS,
        cwd=PROJECT_DIR, timeout=300, label=slug,
        # Bound to this athlete for the same reason as the caller flag above: this
        # run holds Bash, and the loop serves every athlete in turn, so a per-spawn
        # env (never os.environ) keeps one athlete's scope out of the next one's run.
        env={**os.environ, "CC_ATHLETE_SCOPE": slug},
    )
    if result.returncode == -1:
        # A single timeout is almost always transient (API/CPU contention — e.g. an
        # Opus chat reply running at the same time) and the next 5-min cycle recovers.
        # Only alert after 2 consecutive timeouts stuck at the same position: last_id
        # doesn't advance until an analysis succeeds, so a matching last_id means we
        # are still stuck on the same new activity.
        stuck_at = state.get("last_id")
        if state.get("timeout_stuck_at") == stuck_at:
            state["timeout_count"] = state.get("timeout_count", 1) + 1
        else:
            state["timeout_stuck_at"] = stuck_at
            state["timeout_count"] = 1
        save_state(state, state_file)
        if state["timeout_count"] >= 2:
            # Log-only, deliberately: a stalled watcher is an engineering fact and
            # the athlete has nothing to do about it (tone guide R4 / §4c). The
            # ops log is where this belongs; ops-digest renders it sent=False.
            # detail= is echoed verbatim into the ops digest, so it stays free of the
            # timeout value and the activity id (§4a/§4c) even though that digest is
            # log-only today (ops-digest.py:158 logs it sent=False and calls no notify).
            # The mechanism the developer actually needs goes to stderr, which the cron
            # log captures, so nothing diagnostic is lost.
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] activity analysis timed "
                  f"out {state['timeout_count']}x consecutive (300s each), stuck at "
                  f"activity {stuck_at or 'unknown'}", file=sys.stderr)
            ops_log.alert(
                "activity-watcher",
                f"activity analysis timed out {state['timeout_count']}x in a row",
                athlete=slug,
            )
        else:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] activity analysis "
                  f"timed out (1st, transient — alert suppressed, retry next cycle)",
                  file=sys.stderr)
        return

    # Analysis completed within budget — clear any prior transient-timeout streak.
    if state.get("timeout_count"):
        state.pop("timeout_stuck_at", None)
        state.pop("timeout_count", None)
        save_state(state, state_file)

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Claude exited {result.returncode}: {stderr_snippet}", file=sys.stderr)
        return

    output = result.stdout.strip()
    if not output:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Claude returned no output", file=sys.stderr)
        return

    activity_id = None
    decoupling_raw = None
    plan_delta_raw = None
    session_chart_raw = None
    test_result_raw = None
    analysis_lines = []
    in_analysis = False
    for line in output.split("\n"):
        if line.startswith("ACTIVITY_ID:"):
            activity_id = line.split(":", 1)[1].strip()
            in_analysis = False
        elif line.startswith("DECOUPLING:"):
            decoupling_raw = line.split(":", 1)[1].strip()
            in_analysis = False
        elif line.startswith("SESSION_CHART:"):
            session_chart_raw = line.split(":", 1)[1].strip()
            in_analysis = False
        elif line.startswith("PLAN_DELTA:"):
            plan_delta_raw = line.split(":", 1)[1].strip()
            in_analysis = False
        elif line.startswith("TEST_RESULT:"):
            test_result_raw = line.split(":", 1)[1].strip()
            in_analysis = False
        elif line.startswith("ANALYSIS:"):
            in_analysis = True
            first = line.split(":", 1)[1].strip()
            if first:
                analysis_lines.append(first)
        elif in_analysis and not line.startswith(("ACTIVITY_ID:", "DECOUPLING:", "SESSION_CHART:", "PLAN_DELTA:", "TEST_RESULT:")):
            analysis_lines.append(line)

    if not activity_id or activity_id == "none":
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file, slug=slug)
        return

    # Dedup check
    if activity_id in existing_ids:
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file, slug=slug)
        return

    if activity_id == state.get("last_id"):
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file, slug=slug)
        return

    state["last_id"] = activity_id
    state["notified_at"] = datetime.now().isoformat()
    save_state(state, state_file)

    # Dedup again in case Claude introduced a duplicate stub
    _dedup_session_log(session_log_f)

    # Ambient heat exposure → acclimation dose (no-op unless heat protocol active)
    _credit_heat_exposure(slug, activity_id, profile)

    # Commit stub entry written by Claude
    sync_commit_push(
        [f"ClaudeCoach/athletes/{slug}/session-log.json",
         f"ClaudeCoach/athletes/{slug}/swim-log.json"],
        f"stub: activity {activity_id} {slug}",
        script="activity-watcher", athlete=slug,
    )

    # Log decoupling for long rides
    if decoupling_raw and decoupling_raw != "none":
        try:
            parts = decoupling_raw.split("|")
            if len(parts) == 7:
                entry = {
                    "activity_id": parts[0].strip(),
                    "date":         parts[1].strip(),
                    "name":         parts[2].strip(),
                    "duration_min": float(parts[3].strip()),
                    "if":           float(parts[4].strip()),
                    "decoupling_pct": float(parts[5].strip()),
                    "tss":          float(parts[6].strip()),
                }
                entries = json.loads(decoupling_log.read_text()) if decoupling_log.exists() else []
                if not any(e.get("activity_id") == entry["activity_id"] for e in entries):
                    entries.append(entry)
                    decoupling_log.write_text(json.dumps(entries, indent=2))
        except Exception:
            pass

    # Send session structure chart
    if session_chart_raw and session_chart_raw != "none":
        try:
            import sys as _sys
            _sys.path.insert(0, str(BASE / "telegram"))
            import charts as _charts
            chart_data = json.loads(session_chart_raw)
            png = _charts.session_chart(
                chart_data.get("name", "Session"),
                chart_data.get("intervals", []),
                ftp=chart_data.get("ftp", ftp),
            )
            if png:
                import tempfile, os as _os
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    f.write(png)
                    tmp_path = f.name
                subprocess.run(
                    ["python3", str(NOTIFY), "--chat-id", str(chat_id), "--photo", tmp_path],
                    cwd=PROJECT_DIR,
                )
                _os.unlink(tmp_path)
        except Exception:
            pass

    plan_delta_note = ""
    if plan_delta_raw and plan_delta_raw != "none":
        try:
            pd_parts = plan_delta_raw.split("|")
            if len(pd_parts) == 4:
                plan_name = pd_parts[0].strip()
                planned_tss = float(pd_parts[1].strip())
                actual_tss = float(pd_parts[2].strip())
                delta_pct = float(pd_parts[3].strip())
                # The model is trusted to IDENTIFY which planned session this was - a
                # naming task - but never for the NUMBER. Kathryn, 28 Jul 2026: the
                # debrief quoted planned Load 122 for the Tue sweetspot ride when the
                # live ICU event (125308764) read icu_training_load 97, making a session
                # that was 9% under plan look 28% under. The stale figure came from
                # recall, because PLAN_DELTA asks the model to supply planned_tss and to
                # compute delta_pct by hand. Re-read the load from ICU and recompute.
                live = _live_planned_load(slug, plan_name)
                if live and live.get("tss"):
                    if abs(float(live["tss"]) - planned_tss) >= 1:
                        print(f"[plan-delta:{slug}] model said planned Load "
                              f"{planned_tss:.0f}, live ICU event says {live['tss']:.0f}"
                              f" - using live", file=sys.stderr)
                    planned_tss = float(live["tss"])
                    plan_name = live.get("name") or plan_name
                if planned_tss:
                    delta_pct = (actual_tss - planned_tss) / planned_tss * 100
                sign = "+" if delta_pct >= 0 else ""
                plan_delta_note = f"vs plan ({plan_name}): {int(planned_tss)}→{int(actual_tss)} Load ({sign}{delta_pct:.0f}%)"
        except Exception:
            pass

    analysis = "\n".join(analysis_lines).strip()
    already_discussed = analysis.lower().startswith("discussed")
    if not already_discussed and analysis and analysis != "none":
        # Evening window: the trailing question moves to the 21:00 check-in.
        _sport_now = ""
        _entry_now = {}
        try:
            for _e in json.loads(session_log_f.read_text()):
                if str(_e.get("activity_id", "")) == activity_id:
                    _sport_now = _e.get("sport", "") or ""
                    _name_now = _e.get("name", "") or ""
                    _entry_now = _e
                    break
            else:
                _name_now = ""
        except Exception:
            _name_now = ""
        # Asked-and-answered, and one question at most. BEFORE the evening defer,
        # so a suppressed ask is not queued for 21:00 instead of dropped.
        analysis = _filter_debrief_questions(analysis, _entry_now, slug)
        analysis = _defer_trailing_question(analysis, slug, activity_id, _sport_now,
                                            _name_now, state=state, state_file=state_file)
        if not analysis:
            # The debrief was nothing but the question. Don't push a bare plan-delta line
            # in its place — the check-in carries it at 21:00.
            plan_delta_note = ""
    if plan_delta_note and not already_discussed:
        analysis = f"{analysis}\n_{plan_delta_note}_" if analysis else plan_delta_note
    # §8.3 acknowledgement. Kept OUT of `analysis` deliberately: `analysis` is
    # handed to _strava_update below, and §8.4 rules Strava the wrong surface —
    # it is public and stays neutral. This only ever reaches Telegram.
    ack_prefix, ack_keys = _acknowledgement(
        slug, activity_id, profile.get("coaching_level", "mid"))
    ack_sent = False

    if already_discussed:
        # Chat already covered this activity — stay silent, but a durability
        # flag is new insight the chat can't have had (it's computed here).
        note, flagged = _run_durability_note(slug, activity_id)
        if flagged:
            # W1: if there is an acknowledgement it leads, and the flag still
            # lands at full strength immediately after. Praise-then-finding is
            # §8.5 move 0; the §8.4 sandwich is the other order.
            _notify(f"*New activity*\n\n{ack_prefix}{note.lstrip()}"
                    if ack_prefix else f"*New activity*{note}", chat_id, slug=slug)
            ack_sent = bool(ack_prefix)
    elif analysis and analysis != "none":
        analysis += _run_durability_note(slug, activity_id)[0]
        _notify(f"*New activity*\n\n{ack_prefix}{analysis}", chat_id, slug=slug)
        ack_sent = bool(ack_prefix)

    # Marked only once it has actually been said — an unsent occurrence must
    # stay available, or the one message §8.3 exists to produce is silently
    # spent. Idempotent, so a re-run cannot double-fire.
    if ack_sent and ack_keys:
        try:
            ack_lib.mark_fired_in(BASE / "athletes" / slug, ack_keys)
        except Exception as e:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] acknowledgement "
                  f"state not written: {e}", file=sys.stderr)

    # Send quick-log keyboard for immediate data capture
    new_entry = None
    if session_log_f.exists():
        try:
            for e in json.loads(session_log_f.read_text()):
                if str(e.get("activity_id", "")) == activity_id:
                    new_entry = e
                    break
        except Exception:
            pass
    # The keyboard is a SECOND message carrying a question, so it obeys the same
    # asked-and-answered rule as the debrief: if everything it captures is already
    # filled — the analysis fills these from chat when the athlete volunteered them
    # — then offering the buttons is the re-ask in button form. Only when ALL of
    # them are known: a long ride whose RPE is in but whose fuelling is not still
    # gets the keyboard, because the carb buttons are the cheap way to answer.
    _kb_kinds = [ask_gate.ANKLE if new_entry and new_entry.get("sport") == "Run" and injuries
                 else ask_gate.RPE]
    if new_entry and (new_entry.get("duration_min") or 0) >= ask_gate.LONG_SESSION_MIN \
            and new_entry.get("sport") == "Ride":
        _kb_kinds.append(ask_gate.NUTRITION)     # matches _quick_log_keyboard's carb row
    _kb_known = bool(new_entry) and all(
        ask_gate.already_answered(new_entry, _k, history=_load_history(slug),
                                  since=ask_gate.entry_synced_at(new_entry))
        for _k in _kb_kinds)
    if (new_entry and not already_discussed and not _kb_known
            and new_entry.get("sport", "").lower() not in _WATER_SPORTS):
        sport = new_entry.get("sport", "")
        dur = new_entry.get("duration_min", 0) or 0
        kb = _quick_log_keyboard(activity_id, slug, sport, bool(injuries), dur)
        activity_label = new_entry.get("name") or sport
        if sport == "Run" and injuries:
            hdr = f"Injury pain during (0–10) — {activity_label}:"
        else:
            hdr = f"Quick log — {activity_label}:"
        _tg_send_keyboard(chat_id, hdr, kb)
        try:
            _log_to_history(slug, hdr)
        except Exception:
            pass

    _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file, slug=slug)

    # Zone-spotting: if Claude detected a threshold test, prompt for confirmation
    if test_result_raw and test_result_raw != "none":
        try:
            parts = test_result_raw.split("|")
            if len(parts) == 3:
                t_type, t_value, t_aid = parts[0].strip(), parts[1].strip(), parts[2].strip()
                units = {"ftp": "W", "css": "/100m", "lthr": "bpm"}.get(t_type, "")
                labels = {"ftp": "FTP", "css": "CSS", "lthr": "LTHR"}.get(t_type, t_type.upper())
                confirm_data = f"test:{t_type}:{slug}:{t_value}"
                dismiss_data = f"test:dismiss:{slug}:{t_aid}"
                tst_hdr = f"⚡ That looks like a {labels} test.\nSuggested: *{t_value} {units}*\n\nConfirm to update thresholds:"
                _tg_send_keyboard(
                    chat_id,
                    tst_hdr,
                    {"inline_keyboard": [[
                        {"text": f"✅ Confirm {t_value} {units}", "callback_data": confirm_data},
                        {"text": "❌ Dismiss", "callback_data": dismiss_data},
                    ]]},
                )
                try:
                    _log_to_history(slug, tst_hdr)
                except Exception:
                    pass
        except Exception:
            pass

    # Write coaching note to Strava activity description and store initial field hash
    coaching_level = profile.get("coaching_level", "mid")
    _strava_update(slug, activity_id, "" if already_discussed else analysis,
                   plan_delta_raw=plan_delta_raw,
                   session_entry=new_entry, chat_id=chat_id, coaching_level=coaching_level)
    if new_entry:
        import hashlib as _hl
        fields = "|".join(str(new_entry.get(f, "")) for f in ("rpe", "nutrition_g_carb", "injury_pain_during", "feel", "notes"))
        state.setdefault("strava_hashes", {})[activity_id] = _hl.md5(fields.encode()).hexdigest()[:8]
        save_state(state, state_file)

    # Water-sport rename: derive name from rules or ask Jamie via Telegram
    if new_entry and new_entry.get("sport", "").lower() in _WATER_SPORTS:
        activity_date = new_entry.get("date", date.today().isoformat())
        sport = new_entry.get("sport", "")
        derived = _derive_activity_name(slug, activity_date, sport, activity_id)
        if derived and derived.lower() != "ask":
            _rename_strava(slug, activity_id, derived)
        elif chat_id:
            _notify(
                f"Sailing session logged ({activity_date}). What should I name it on Strava?",
                chat_id, slug=slug,
            )

    # Trigger site data refresh in background
    subprocess.Popen(
        ["python3", str(BASE / "scripts/refresh-site-data.py")],
        cwd=PROJECT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", help="run one athlete on demand (e.g. from the bot's "
                                       "'Check for activity' button); skips the shared lock")
    args = ap.parse_args()

    athletes = json.loads(ATHLETES_CONFIG.read_text())

    # On-demand single athlete (manual / button): bypass the shared cron lock so a
    # running cron cycle can't block the user's tap.
    if args.athlete:
        cfg = athletes.get(args.athlete)
        if not cfg:
            print(f"unknown athlete: {args.athlete}", file=sys.stderr)
            sys.exit(1)
        try:
            check_athlete(args.athlete, cfg, announce_empty=True)
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{args.athlete}] Unhandled error: {exc}", file=sys.stderr)
        return

    if not acquire_lock():
        sys.exit(0)
    try:
        for slug, athlete_cfg in athletes.items():
            if not athlete_cfg.get("active", True):
                continue
            try:
                check_athlete(slug, athlete_cfg)
            except Exception as exc:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Unhandled error: {exc}", file=sys.stderr)
    finally:
        release_lock()

    # 28 Jul 2026: one unconditional heartbeat per cron cycle, AFTER the lock is
    # released and every athlete has been attempted. Every existing ops_log call
    # in this file is conditional (heat-credit success, send failure, stuck
    # timeout), so a fully quiet cycle -- no new activity for anyone, the common
    # case -- wrote NOTHING to run-status.jsonl. That made this, the busiest
    # athlete-facing sender (12 ticks/hour), invisible to coach_alert's gap check:
    # there was no positive signal to look for "did it run" against, only failure
    # signals for "did it send", and a dead process leaves no failure either.
    # One per invocation, not per athlete (12/hour * 288/day is already the
    # right order of magnitude for run-status.jsonl's 6000-line cap against a
    # 7-day window; per-athlete would multiply that by the athlete count for no
    # gain, since a shared cron process either completes this line or it does not).
    # Placed outside the try/finally on purpose: an unhandled exception inside the
    # loop is already caught per-athlete, but if something escapes that (or the
    # process is killed) this line is never reached, and its absence IS the "did
    # not run" signal coach_alert.DELIVERABLES' rolling window is built to catch.
    ops_log.record_run("activity-watcher", ok=True, detail="cycle complete")


if __name__ == "__main__":
    main()
