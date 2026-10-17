#!/usr/bin/env python3
"""
Check for new activities and send a brief analysis to Telegram.
Run every 15 min via cron. Loops over all active athletes. Skips if already running.
"""
import json, os, re, ssl, subprocess, sys, time, urllib.request
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

# Grade what was done, not what was planned. Without this the model graded a session of two
# continuous blocks (no lap presses) against a planned 8x100 and printed a vs-plan Load delta.
STRUCTURE_RULE = (
    "Executed structure vs plan: when an EXECUTED STRUCTURE line covers this activity, its verdict"
    " decides whether the planned structure was done; describe the efforts it lists, not"
    " Intervals.icu's auto-intervals. Otherwise:"
    " from the Strava laps, count the work reps and check for lap presses,"
    " then compare with the planned structure (rep count, rep length). If the rep count or lap"
    " presses do not match the plan (e.g. two continuous blocks and no lap presses against a planned"
    " 8x100), the planned structure was NOT executed: output `PLAN_DELTA: none`, do NOT grade rep by"
    " rep against the plan, and say plainly 'planned structure not executed' before describing what"
    " was actually done in ONE line (block count and distances or durations, average pace, HR),"
    " never a line per block. Grade only on what was done. A 'Structure:' line in COMPUTED FACTS"
    " (swims) is that verdict, decided in code."
)
# ONE line: Haiku gave James's easy 16 Oct swim a line per block, 189 words against 46
# (Jamie, 17 Oct 2026: shadow review).

_WATER_SPORTS = {"sail", "watersport", "windsurf", "kitesurf", "kiteboard"}

# Safety net for an athlete whose new activities arrive by webhook: look anyway this often.
# TO RETIRE (Jamie, 15 Oct 2026): once webhooks have proved reliable, drop this to the
# new-day check alone. The evidence is ~/Library/Logs/ClaudeCoach/icu-webhook-misses.log:
# every activity this safety net found that no webhook had announced. Two weeks of it
# empty with OAuth athletes training = safe to retire.
WATCH_EVERY_S = 30 * 60
WEBHOOK_MISSES = Path.home() / "Library/Logs/ClaudeCoach/icu-webhook-misses.log"


def _note_webhook_misses(slug: str, acts: list, existing_ids: set) -> int:
    """The safety net found activities no webhook announced: one line each in
    WEBHOOK_MISSES (the case for keeping the 30-minute check). Returns how many."""
    n = 0
    for a in acts or []:
        if (str(a.get("source") or "").upper() == "STRAVA"
                or duplicates.activity_ids(a) & existing_ids):
            continue
        try:
            start = time.mktime(time.strptime(str(a.get("start_date_local") or "")[:19],
                                              "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if icu_changes.changed_since(slug, start, icu_changes.ACTIVITY_TYPES):
            continue                     # a webhook did come; the analysis is just retrying
        n += 1
        print(f"[{slug}] webhook-miss: {a.get('id')} ({a.get('start_date_local')}) found by "
              f"the safety check, no webhook", file=sys.stderr)
        try:
            WEBHOOK_MISSES.parent.mkdir(parents=True, exist_ok=True)
            with open(WEBHOOK_MISSES, "a") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {slug} {a.get('id')} "
                         f"{a.get('start_date_local')} {a.get('type')}\n")
        except OSError:
            pass
    return n


def _log_to_history(slug: str, message: str) -> None:
    """Append an outbound notification to the athlete's Telegram history so the bot has context for replies."""
    history_file = BASE / "athletes" / slug / "telegram" / "history.json"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(history_file.read_text()) if history_file.exists() else []
    except Exception:
        history = []
    history.append({"user": "", "assistant": message,
                    "ts": datetime.now().isoformat(timespec="seconds")})
    history_file.write_text(json.dumps(history[-30:], indent=2))

sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "ironman-analysis"))
from coaching_levels import level_block as _level_block
import planning_pause          # tracking-only: describe the ride, never chase a plan
import illness as illness_lib   # structured illness/compromised flag (surfacing gate)
import acknowledgement as ack_lib   # §8.3 milestone triggers, evaluated in Python
import ask_gate                    # asked-and-answered + one-question-per-message gate
import coaching_prefs              # heat / fuelling switches (profile.json)
import fuel_basis                  # race vs non-race fuelling figure
import shadow                      # shadow week: Haiku twin of the debrief, never sent
from strength_ask import strength_ask   # strength debrief question (RPE only without HR)
import ops_log
import write_verify            # read-back verdicts for the Strava writes below
import heat as heat_lib
import profile_fields
import claude_call
import claude_billing
import hr_quality              # per-activity HR trust (wrist-sensor artefacts)
import drift                   # one corrected drift figure (lib/drift.py), not Intervals.icu's raw decoupling
import baseline as baseline_lib   # new athletes' baseline block: test capture + close
import duplicates                  # a session in Intervals.icu twice: ask, then delete or merge
import goals as goals_lib         # a goal without a race: block 1 pinned when baseline closes
import icu_changes                # webhooks instead of polling, for athletes linked by OAuth
from git_sync import sync_commit_push
from primitives.run_durability import compute_run_durability, fade_line


def _pace_str(speed_ms: float) -> str:
    if not speed_ms:
        return "?"
    secs = round(1000 / speed_ms)
    return f"{secs // 60}:{secs % 60:02d}/km"


def _tg_send_keyboard(chat_id, text, keyboard):
    """Send a Telegram message with inline keyboard. Returns message_id or None.
    Also recorded for the web app, which shows the same buttons (lib/outbox.py)."""
    try:
        import outbox
        outbox.record(chat_id, text, keyboard, source="activity-watcher", parse_mode="Markdown")
        if not outbox.telegram_on(chat_id):
            return None
    except Exception:
        pass
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

    fuel_on = coaching_prefs.fuelling_on(slug)     # the athlete can switch fuelling off
    if fuel_on and sport == "Ride" and (duration_min or 0) >= 90:
        rows.append([{"text": f"{g}g/hr", "callback_data": cb("c", g)} for g in (40, 50, 60, 70, 80, 90)])

    rows.append([b for b in (
        {"text": "📊 Intervals", "callback_data": f"drill:intervals:{aid}:{slug}"},
        {"text": "🍌 Nutrition",  "callback_data": f"drill:nutrition:{aid}:{slug}"} if fuel_on else None,
        {"text": "💓 HR",         "callback_data": f"drill:hr:{aid}:{slug}"},
        {"text": "↔️ Compare",    "callback_data": f"drill:compare:{aid}:{slug}"},
    ) if b])

    return {"inline_keyboard": rows}


def _build_prompt(slug, first_name, ftp, injuries, profile=None, run_hr_cap=150, nutrition_target=90, nutrition_label="race target", recent_chat="",
                  hr_note="", css=None, recent_fuel=None, target_id=None):
    """Build the per-athlete activity analysis prompt. `hr_note` is the code-side HR
    check for the recent activities (_hr_quality_pass); empty when every HR is fine.
    `css` is the swim CSS (M:SS per 100m) from Intervals.icu (_resolve_css), or None.
    `recent_fuel` is _recent_fuel's figure and `target_id` _pick_target's activity, both
    worked out in code (Jamie, 17 Oct 2026: shadow review); None = the old wording."""
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
    strength_q = strength_ask(injuries)
    run_injury_ask = (
        f"- Run (walk-run): If Strava laps show alternating run/walk laps (walk laps: pace >8:00/km or duration ≤90s):"
        f" Your ANALYSIS must be formatted EXACTLY as multiple output lines — each on its own line:"
        f" Line 1 (header): NxDUR / Xmin walk · avg GAP X:XX/km · +/-Xsec vs threshold ({threshold_pace}/km){_thr_note}"
        f" Lines 2..N+1 (one per run rep, use gap_pace if present else pace): Rep N: DUR · GAP X:XX/km · AVGbpm/MAXbpm"
        f" Final lines: % HR ≤{run_hr_cap} bpm cap adherence · decoupling % (COMPUTED FACTS) if >40 min | Injury pain score during? (0-10)"
        " Else (continuous run): Line 1 = distance + avg GAP pace vs threshold — state +/- sec/km."
        f" Line 2 = % time HR ≤{run_hr_cap} bpm + aerobic decoupling % (COMPUTED FACTS) if >40 min."
        " Line 3 = \"Injury pain score during? (0-10)\""
        " — the DURING score only. Never ask about this morning or the next morning:"
        " the morning score belongs to the morning check-in, which tracks its own"
        " answers, and asking here is how it came to be asked twice."
        if injuries else
        f"- Run (walk-run): If Strava laps show alternating run/walk laps (walk laps: pace >8:00/km or duration ≤90s):"
        f" Your ANALYSIS must be formatted EXACTLY as multiple output lines — each on its own line:"
        f" Line 1 (header): NxDUR / Xmin walk · avg GAP X:XX/km · +/-Xsec vs threshold ({threshold_pace}/km){_thr_note}"
        f" Lines 2..N+1 (one per run rep, use gap_pace if present else pace): Rep N: DUR · GAP X:XX/km · AVGbpm/MAXbpm"
        f" Final lines: HR zone split · decoupling % (both from COMPUTED FACTS) if >40 min | How did it feel?"
        " Else (continuous run): Line 1 = distance + avg GAP pace vs threshold — state +/- sec/km."
        " Line 2 = HR zone distribution + aerobic decoupling % (both from COMPUTED FACTS) if >40 min."
        " Line 3 = \"How did it feel?\" (never ask RPE in text: the Quick log card that follows asks it)"
    )

    coaching_level = (profile or {}).get("coaching_level", "mid")

    # CSS written in from Intervals.icu (Jamie, 16 Oct 2026: shadow review). The prompt used
    # to say "CSS from profile swim_css_per_100m", a field the ICU profile fetch does not
    # have, and Sonnet quoted a stale 1:39 against the real 1:41.
    css_line = (f" Swim CSS {css}/100m (Intervals.icu swim threshold - the only CSS to use)."
                if css else " No swim CSS is set: give swim paces with no CSS comparison.")

    # (Jamie, 17 Oct 2026: shadow review) the fuelling average was a sum left to the model
    # (Sonnet "none logged", Haiku "~64 g/hr" off the same log), and which activity to
    # debrief was its choice too (Calum's pair compared two different rides).
    fuel_avg = recent_fuel or ("[avg g/hr from last 4 rides >90 min in session-log.json "
                               "with nutrition_g_carb set]")
    step3 = (f"Step 3 — Debrief activity {target_id} (picked in code: the most recent one not "
             "yet in session-log.json; any other new activity gets its own run). If it IS "
             "already in session-log.json, output ACTIVITY_ID: none.\n  Duplicate upload guard:"
             if target_id else
             "Step 3 — For the most recent activity that is NOT already in session-log.json:\n"
             "  Duplicate upload guard:")

    return f"""\
Check for new activities for {first_name} and stub them into the session log.

{_level_block(coaching_level)}
{planning_pause.prompt_block(slug, first_name)}
{illness_lib.prompt_block(slug, first_name=first_name)}
{hr_quality.prompt_block(profile)}
{baseline_lib.prompt_block(slug, first_name)}
{hr_note}
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

{step3} if multiple activities of the same sport on the same date are NOT in session-log.json, they are likely the same session uploaded from two sources (e.g. Garmin + Strava). Only process the one with the highest numeric ID. If that highest-ID activity is already in session-log.json (by ID, date, or both), output ACTIVITY_ID: none — the session is already logged.

  - Fetch full detail via Bash: python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --caller activity-watcher --endpoint activity_detail --activity-id <id>
  - If sport is Run, VirtualRun, or Swim: also fetch extended metrics:
    python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --caller activity-watcher --endpoint extended_metrics --activity-id <id>
  - If the activity has a strava_id field: fetch Strava laps, splits, and gear:
    python3 ClaudeCoach/lib/strava_fetch.py --athlete {slug} --strava-id <strava_id>
    This is the only sanctioned source for gear (gear_name/gear_id in its output) — ICU's
    gear.id/gear.name fields are stale and must never be read or reported.
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
      "pace_per_100m": <seconds per 100m: the figure on this swim's COMPUTED FACTS "Swim average" line; without one, moving_time_s / (distance_m / 100)>,
      "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null, "notes": null, "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }}
    Also append to ClaudeCoach/athletes/{slug}/swim-log.json:
    {{"date":"<YYYY-MM-DD>","activity_id":"<id>","name":"<name>","distance_m":<int>,"pace_per_100m":<seconds float>,"duration_min":<int>,"tss":<int or null>}}
    Include swim-log.json in the git add below.

  For WeightTraining / Strength:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Strength",
      "tss": <tss or null>, "duration_min": <duration>, "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null, "notes": null, "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }}

  - Write the updated array back to ClaudeCoach/athletes/{slug}/session-log.json.

Step 4 — Respond in EXACTLY this format (no other text). ALL FIVE line types, every time,
each written "none" when it does not apply — the code reads them, so a missing line loses data:
ACTIVITY_ID: <id or none>
ANALYSIS: <coaching message — see rules below; it ENDS with the session's question line>
PLAN_DELTA: <see below, or none>
DECOUPLING: <see below, or none>
TEST_RESULT: <see below, or none>

ALREADY DISCUSSED comes first: before writing ANALYSIS, apply the ALREADY-DISCUSSED CHECK at
the end of this prompt. If it applies, ANALYSIS is exactly `discussed` and nothing else.

{first_name}: FTP {ftp} W.{css_line}{injury_line}

Interval source: prefer Strava laps when they give a cleaner breakdown than ICU (e.g. ICU splits one effort into 3 pieces).
Run pace is GAP: every pace you quote for an OUTDOOR run is Grade-Adjusted Pace - Strava lap gap_pace when you use Strava laps, otherwise the Intervals.icu GAP figures in COMPUTED FACTS (interval `gap`). Flat pace only for a treadmill run, which COMPUTED FACTS labels. Never quote flat pace for an outdoor run, with or without Strava.

{STRUCTURE_RULE}

Rules for ANALYSIS — each logical line must be a separate output line (no semicolons to merge lines):

Always lead with a one-sentence narrative verdict (e.g. "Solid Z2. Form held to the end.").
Describe what the data shows; never offer a CAUSE for it (tiredness, fatigue, a fitness ceiling, heat, a sensor fault, illness) unless a computed note above or the athlete says so. A fade is "the last km slowed by 50 s", not "points to tiredness": the question line is where the athlete tells you why.
Always END with the session's question line from its format below ("How did it feel?", or
the strength question). Short easy-session messages keep it too.

Every format below, regardless of session type, MUST state the session's actual Load (TSS) as a number. This is never dropped for brevity and does not depend on whether PLAN_DELTA fires later in this response — PLAN_DELTA is a separate "vs plan" comparison, not the athlete's only route to seeing what this session cost them.

Then choose the metric set BY SESSION TYPE and surface only what else matters for THIS session, not a fixed block every time:
- Easy / steady / recovery: Load (TSS) plus at most 1-2 other key numbers (e.g. avg HR and decoupling). Keep it short (well under 200 characters), no headers, no per-rep lines.
- Long endurance ride or run (roughly 90 min or more, steady): Load (TSS), duration, IF, aerobic decoupling, and the fuelling prompt. A few lines, no per-rep breakdown.
- Intervals / reps (structured): Load (TSS) plus the per-rep splits and target adherence in the sport format below. This per-rep detail IS the point for these sessions, so show it by default.
- Brick (bike and run in one activity, or bike then run back-to-back): ALWAYS report the bike and run legs separately, splitting time and Load per leg.
This selection is authoritative and governs the per-sport RIDE / RUN / SWIM formats below: use a format's full header and per-rep breakdown ONLY when the session is intervals/structured, or when the athlete has asked for the full breakdown. For easy/steady/recovery and long-endurance sessions, stay with the verdict plus Load and the few other relevant numbers above. Where these rules conflict with the per-sport format instructions below, these rules win.

Decoupling / HR drift: quote ONLY the Drift figure (the corrected aerobic decoupling, warm-up, cool-down, coasting and stops already taken out) on this activity's COMPUTED FACTS line. Never work out decoupling or HR drift yourself from laps, splits or streams. When that line says it is not available (or there is no COMPUTED FACTS line), leave decoupling and HR drift out of the message entirely.

RIDE:
- For Ride sessions: the Zone split line in COMPUTED FACTS (computed in code from Intervals.icu zone times; power zones when the ride has power) is the primary intensity descriptor. Quote its figures as given; never add zone percentages up yourself. NP and IF are secondary and must be qualified against the zone split. Do not characterise a ride as Z2/easy unless its Z1-Z2 figure is above 70% (power zones).
- Structured (Strava laps show alternating hard/easy, or ICU >3 intervals): header line + one line per WORK interval.
  Header: NxDUR @ AVG W (X% FTP) · NP XXXw · IF X.XX
  Rep lines: Rep N: DUR · XXXw (X% FTP) · AVGbpm/MAXbpm
  Final line: completion note if intervals missed, else "Nutrition — g carbs/hr, bottles, and sodium?"
- Unstructured ≤90 min: NP + IF | "Nutrition — g carbs/hr, bottles, and sodium?"
- Unstructured >90 min: NP + IF | aerobic decoupling % | "Nutrition — g carbs/hr, bottles, and sodium? (recent avg: {fuel_avg} · {nutrition_label} {nutrition_target}g/hr)"

RUN:
{run_injury_ask}
Running power: when COMPUTED FACTS has a "Running power" line, add one line after the HR line with its average watts and % of run threshold power, and describe the effort by that % and its power zone (Z1-Z2 easy, Z3 steady, Z4 threshold, Z5+ hard). Never call a run easy when its power is Z3 or above. Skip entirely when there is no Running power line.
Easy/Z2 verdict: When characterising run intensity use icu_average_hr (from activity_detail), Z2 ceiling = {run_hr_cap} bpm, the COMPUTED FACTS Zone split line (HR zones), and total_ascent_m (from activity_detail). Apply deterministically: a run is EASY if avg_hr < {run_hr_cap} bpm AND the Z1-Z2 figure on its COMPUTED FACTS Zone split line (HR zones) is 75% or more. The ONE exception: when the TERRAIN CHECK above says terrain DOES explain the time above Z2 and avg_hr < {run_hr_cap} bpm, the run stays EASY and you may say the climbs account for it. Otherwise terrain is never the reason: not from total_ascent_m, not from the route name, and a run with no TERRAIN CHECK line gets no terrain explanation.
For ANY session (rides too): never name a hill, climb or terrain as the reason for power, pace or heart rate unless its TERRAIN CHECK line says terrain DOES explain it (13 Oct 2026: a ride's power creep was put down to a "climb" that did not exist).
Elevation source: total_elevation_gain / total_elevation_loss / total_ascent_m for a session ALWAYS come from the activity_detail root fields (whole-activity totals). NEVER read elevation from extended_metrics or icu_intervals entries — those are per-segment/per-interval values (often single-digit metres) and will misstate the session's actual terrain by an order of magnitude if quoted as the whole-activity gain.

SWIM:
- Whole-swim average pace and its gap to CSS: quote the COMPUTED FACTS "Swim average" line; never work an average pace out yourself.
- Pool with Strava laps (fetched above): rep durations/paces come from the STRAVA LAPS — they are
  the native Garmin button-press boundaries and are correct. NEVER use icu_intervals for rep
  timings: ICU infers boundaries from velocity_smooth and over-extends every rep ~2-4s into the
  surrounding rest, so its paces read slower than actually swum (bug confirmed 2026-06-11).
  Identify the work reps from the laps (skip warmup/cooldown/drill laps — obvious from the
  distance/pace pattern). Pace per rep (seconds/100m) = (lap duration / lap distance) * 100 →
  format as M:SS/100m. icu_intervals may be used ONLY for per-rep HR, matched to laps by order.
  Report per-rep HR as recorded: never call a reading a sensor spike or fault unless the HR note above flags this activity.
  CSS target: the swim CSS on the {first_name} line above (Intervals.icu), never profile.json or notes. Delta = rep_pace_s - css_s (positive = slower than CSS).
  Header: Nx(distance)m · CSS X:XX/100m · avg rep X:XX/100m (+/-Xsec vs CSS)
  Rep lines (one per work lap): Rep N: X:XX/100m (+/-Xsec vs CSS) · AVGbpm
  Final line: "How did it feel?" (RPE is asked by the Quick log card, never in text)
- Pool with NO Strava laps (no strava_id / fetch failed): fall back to icu_intervals WORK
  intervals (type="WORK"), same format, and append "(paces from ICU — read ~2-4s/100m pessimistic)".
- Else (OWS or neither): use interval_summary from activity detail if present, else distance + avg pace vs CSS +/- seconds.
  Final line: "How did it feel?" (RPE is asked by the Quick log card, never in text)

STRENGTH: one summary line (duration, Load, avg HR; no zone-by-zone breakdown) | {strength_q}
CARDIO (sport Workout with a generic watch name such as "Cardio", and no distance): the watch does not say what it was, so never call it strength or guess the sport. Give duration, Load and avg HR, then END with exactly "What was this one - a run, a ride, or something else?" in place of any other question (Jamie, 14 Oct 2026: Kathryn's planned easy run came in as Garmin "Cardio").

For unstructured rides > 3 hours (or structured rides > 3 hours where Pa:HR data is available): also output a DECOUPLING line:
DECOUPLING: <activity_id>|<date>|<name>|<duration_min>|<intensity_factor>|<decoupling_pct>|<tss>
decoupling_pct is the COMPUTED FACTS Drift figure. If conditions not met or that line says not available: DECOUPLING: none

PLAN_DELTA: copy the PLAN_DELTA line from this activity's COMPUTED FACTS exactly. It is
computed from the planned session Intervals.icu paired with the activity; never match events
or work out the numbers yourself. With no COMPUTED FACTS line: PLAN_DELTA: none.
Also PLAN_DELTA: none when the planned structure was not executed (see the executed-structure rule above).

If the activity looks like a performance threshold test, output:
TEST_RESULT: <type>|<value>|<activity_id>
Rules:
- ftp: activity name contains "ramp", "ftp test", "20 min", "20-min", or "threshold test" (case-insensitive), OR single sustained interval >18 min at >90% FTP. Value = ramp → peak 1-min power × 0.75; 20-min test → 20-min avg power × 0.95. Round to nearest whole watt.
- css: swim activity where name contains "css", "critical swim speed", or "time trial", and distance ≥ 300m. Value = pace as MM:SS per 100m.
- lthr: run activity where name contains "lthr", "lactate", "hr test", "tempo test". Value = avg HR during the sustained effort portion (bpm, integer).
If not a recognisable threshold test: TEST_RESULT: none

TINY RECORDING: when COMPUTED FACTS marks the activity as a TINY RECORDING (under 2 minutes), still write its stub, then output `ANALYSIS: tiny` and nothing else for it: no analysis and no question (the code writes the message).

If no activities at all: ACTIVITY_ID: none  ANALYSIS: none

ALREADY-DISCUSSED CHECK — recent Telegram chat with this athlete (oldest first):
{recent_chat}
If THIS activity's RESULTS have already been discussed in the chat above, output exactly
`ANALYSIS: discussed`. The test: a coach line in the chat, sent AFTER the activity started,
that already gives this activity's results (its distance, time, HR, pace or Load), or the
athlete asking how this completed session went. Match on sport, distance/duration and date.
Numbers the athlete has already seen (distance, pace, HR, Load, decoupling) are NOT new insight;
only produce a normal ANALYSIS here if you have something material the chat did not cover.
A PRE-SESSION message is NOT a discussion: a night-before brief, a morning card, a plan reminder,
or the athlete asking what the session IS ("remind me of today's swim") all describe the session
before it happened and contain no actual results. Never output `discussed` on the strength of one
— if every mention in the chat predates the activity, produce the full normal ANALYSIS. All logging steps (session-log stub, swim-log, etc.)
still apply regardless — `discussed` suppresses the message, never the capture.
When writing the session-log stub, if the chat above contains the athlete's feedback for THIS
activity (RPE, injury/ankle pain scores, nutrition), fill those fields from the chat instead of
leaving them null — data the athlete already gave must never be re-asked by the evening jobs.

CRITICAL: Your entire response must contain only the five lines of Step 4 (ACTIVITY_ID, ANALYSIS, PLAN_DELTA, DECOUPLING, TEST_RESULT). Do not output reasoning steps, file read confirmations, tool call summaries, or any other text. All processing is internal and silent.
ANALYSIS scope: describe only the activity being analysed. Do NOT mention other planned sessions from today's calendar, ask whether a different planned session happened, or comment on sessions that were not completed.
No advice about tomorrow, the rest of the week or what to train next ("tomorrow should be bike or rest"): the morning card and the night-before brief own that. The one exception is the OVER PLAN line, where that block asks for it.
At most ONE "Next time:" line, and only when this session clearly missed its own target. Never one for an activity with an OVER PLAN line: that line already says it. Any number in it must be one of the figures this prompt gives you (run HR cap {run_hr_cap} bpm, FTP {ftp} W, threshold pace {threshold_pace}/km, the swim CSS); never invent a pace, power or HR target. (Jamie, 16 Oct 2026: shadow review, an invented "Next time: run near 6:00/km".)"""


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

        # Untrusted HR (lib/hr_quality.py) must not scale the dose: drop it so the dose
        # falls back to load-per-hour, exactly as for an activity with no HR at all.
        if act and hr_quality.is_untrusted(slug, activity_id):
            act = {**act, "average_heartrate": None}
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
    # Power:HR decoupling off a cadence-locked or dropped-out HR is a number about the
    # sensor. No line and no log entry for an activity lib/hr_quality.py judged bad.
    if hr_quality.is_untrusted(slug, activity_id):
        return "", False
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


_TERRAIN_DENIAL = re.compile(
    r"(climbs?|hills?|terrain|gradient)\W[^.\n]*\b(don'?t|doesn'?t|do not|does not|did not|didn'?t|not)\b[^.\n]*\bexplain"
    r"|\b(don'?t|doesn'?t|do not|does not|not)\b[^.\n]*\bexplain[^.\n]*(climbs?|hills?|terrain|gradient)", re.I)


_INDOOR_TYPES = {"Swim", "WeightTraining", "Workout", "Yoga", "Pilates", "Crossfit", "Elliptical",
                 "StairStepper", "HighIntensityIntervalTraining", "VirtualRide", "VirtualRun",
                 "VirtualRow", "IceSkate"}


def _outdoor(a: dict) -> bool:
    """A session out on real terrain: not on a trainer or treadmill, not a gym or pool sport."""
    typ = str(a.get("type") or "")
    return bool(typ) and not a.get("trainer") and typ not in _INDOOR_TYPES and not typ.startswith("Virtual")


def _terrain_pass(slug: str, session_log_f: Path) -> str:
    """lib/terrain.py for each run in the last 2 days not yet in the session log: whether
    real climbs explain its time above Z2. The debrief may only blame terrain where this
    says so (Jamie, 10 Oct 2026: James's flat Seville run was told "the Z3 time fits the
    terrain" off 145 m of summed altimeter noise). Fail-soft: "" on any error."""
    try:
        import terrain
        from icu_api import IcuClient
        cfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        logged = set()
        if session_log_f.exists():
            logged = {str(e.get("activity_id", "")) for e in json.loads(session_log_f.read_text())}
        # Every OUTDOOR session, not just runs and rides (Jamie, 15 Oct 2026: "the run
        # debrief fix should apply to any outdoor sport"): hikes, walks, open-water swims,
        # e-bike rides, skiing, rowing... Indoor = trainer flag, Virtual*, or a sport with
        # no terrain (gym, pool swim, yoga).
        runs = [a for a in client.get_training_history(days=2) or []
                if _outdoor(a) and str(a.get("id")) not in logged]
        if not runs:
            return ""
        zones = {}
        for ss in client.get_sport_settings() or []:
            if len(ss.get("hr_zones") or []) > 1:
                for t in ss.get("types") or []:
                    zones[t] = float(ss["hr_zones"][1])
        lines = []
        for a in runs:
            typ = str(a.get("type") or "")
            ceiling = (zones.get(typ) or zones.get("Ride" if "Ride" in typ else "Run")
                       or float(cfg.get("run_hr_cap") or 150))
            st = hr_quality.streams_by_type(client.get_activity_streams(str(a["id"])))
            res = terrain.check(st, ceiling)
            lines.append(terrain.note(a.get("name") or typ, str(a["id"]), res, ceiling))
            tbl = terrain.table(a.get("name") or typ, str(a["id"]), st, ceiling,
                                "Run" in typ, res and res["share"])
            if tbl:
                lines.append(tbl)
        return ("TERRAIN CHECK (computed from the altitude and HR streams - authoritative):\n"
                + "\n".join(lines))
    except Exception as exc:
        print(f"[terrain:{slug}] check failed: {exc}", file=sys.stderr)
        return ""


def _structure_pass(slug: str, session_log_f: Path) -> str:
    """For each ride or run in the last 2 days not yet logged that had a structured plan the
    same day: the planned efforts against the efforts found in its stream
    (workout_profile.executed). Shadow week, 13 Oct 2026: Fred has no Strava laps, so his
    3x10 commute was judged from Intervals.icu's auto-intervals - cut at every junction - and
    called "not executed" when the power stream shows all three efforts. Fail-soft: ""."""
    try:
        import workout_profile as wp
        from icu_api import IcuClient
        cfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        logged = set()
        if session_log_f.exists():
            logged = {str(e.get("activity_id", "")) for e in json.loads(session_log_f.read_text())}
        acts = [a for a in client.get_training_history(days=2) or []
                if str(a.get("id")) not in logged
                and any(k in str(a.get("type") or "") for k in ("Ride", "Run"))]
        if not acts:
            return ""
        ssets = client.get_sport_settings() or []
        lines = []
        for a in acts:
            day = str(a.get("start_date_local") or "")[:10]
            fam = "Ride" if "Ride" in str(a.get("type")) else "Run"
            evs = [e for e in client.get_events(day, day) or []
                   if fam in str(e.get("type") or "") and e.get("workout_doc")]
            if not evs:
                continue
            ss = wp.settings_for(ssets, a.get("type"))
            raw = client.get_activity_streams(str(a["id"])) or []
            st = {x.get("type"): x.get("data") for x in raw
                  if isinstance(x, dict) and isinstance(x.get("data"), list)}
            done = wp.completed(st, ss, str(a.get("type")))
            line = wp.executed(wp.planned(evs[0]["workout_doc"], ss), done,
                               "power" if fam == "Ride" else "pace")
            if line:
                lines.append(f"{a.get('name') or fam} ({a['id']}) vs {evs[0].get('name') or 'plan'}: {line}")
        if not lines:
            return ""
        return ("EXECUTED STRUCTURE (efforts found in the stream - authoritative for whether the "
                "planned structure was done; Intervals.icu's auto-intervals split one effort at "
                "every junction or light, so never count reps from them):\n" + "\n".join(lines))
    except Exception as exc:
        print(f"[structure:{slug}] check failed: {exc}", file=sys.stderr)
        return ""


def _overshoot_pass(slug: str, session_log_f: Path) -> str:
    """For each session in the last 2 days not yet logged that ran well over its plan
    (lib/consistency.overshoot): one warm line about letting the next session land (Jamie,
    14 Oct 2026: "going over power or a longer session but skipping the next one isn't how
    to build fitness ... subtly and politely"). Fail-soft: ""."""
    try:
        import consistency
        from icu_api import IcuClient
        from datetime import date as _d
        cfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        logged = set()
        if session_log_f.exists():
            logged = {str(e.get("activity_id", "")) for e in json.loads(session_log_f.read_text())}
        acts = [a for a in client.get_training_history(days=2) or []
                if str(a.get("id")) not in logged]
        if not acts:
            return ""
        today = _d.today()
        evs = [e for e in client.get_events((today - timedelta(days=2)).isoformat(),
                                            (today + timedelta(days=7)).isoformat()) or []
               if (e.get("category") or "WORKOUT") == "WORKOUT"]
        lines = []
        for a in acts:
            day = str(a.get("start_date_local") or "")[:10]
            # The planned session is Intervals.icu's own link, as PLAN_DELTA uses (Jamie,
            # 17 Oct 2026: "B link"); same day + sport only when the activity has no link.
            pid = a.get("paired_event_id")
            plan = ([e for e in evs if str(e.get("id")) == str(pid)] if pid else
                    [e for e in evs if str(e.get("start_date_local") or "")[:10] == day
                     and consistency.family(e.get("type")) == consistency.family(a.get("type"))])
            if not plan or not consistency.overshoot(plan[0], a):
                continue
            nxt = sorted((e for e in evs if str(e.get("start_date_local") or "")[:10] > day),
                         key=lambda e: e.get("start_date_local") or "")
            nxt_txt = (f"the next planned session ({nxt[0].get('name')}, "
                       f"{datetime.fromisoformat(str(nxt[0]['start_date_local'])[:10]):%A})"
                       if nxt else "the next session")
            lines.append(f"{a.get('name') or 'This session'} ({a['id']}) ran well over its plan "
                         f"({plan[0].get('name')}). Next: {nxt_txt}.")
        if not lines:
            return ""
        return ("OVER PLAN (computed - authoritative):\n" + "\n".join(lines) + "\n"
                "For that activity, just before the question line, add ONE short line: credit "
                "the effort first, then say that keeping to the plan is what lets the next "
                "planned session (named above) land well too, because steady sessions build "
                "more than big one-off days. Warm, never a telling-off, no numbers in that line.")
    except Exception as exc:
        print(f"[overshoot:{slug}] check failed: {exc}", file=sys.stderr)
        return ""


# --- computed facts: decisions and arithmetic done here, not by the model ----------------
# Shadow review (Jamie, 16 Oct 2026): the debrief left zone sums, plan matching, decoupling,
# GAP and run-power judgement to the model, and both Sonnet and Haiku got them wrong
# ("25% in Z4+" for 32%, PLAN_DELTA none for a paired ride, decoupling invented from laps,
# flat pace for Kathryn's outdoor run, 422 W called "easy" at 94% of run threshold). Each
# figure below is computed once and handed over as a finished, labelled line
# (lib/shadow.py: move decisions and arithmetic from the prompt into Python).

TINY_RECORDING_S = 120     # under 2 min: an accidental start, not a session (Jamie, 16 Oct 2026: shadow review)


def _secs_to_pace(sec: float, unit: str = "/km") -> str:
    s = int(round(sec))
    return f"{s // 60}:{s % 60:02d}{unit}"


def _treadmill(a: dict) -> bool:
    return str(a.get("type") or "") == "VirtualRun" or bool(a.get("trainer"))


def _is_run(a: dict) -> bool:
    return "Run" in str(a.get("type") or "")


def _zone_split_line(a: dict, hr_trusted: bool = True, hr_only: bool = False) -> str:
    """Labelled % time in zone, from Intervals.icu's zone times: POWER zones for a ride with
    power, HR zones otherwise. Group shares come from the raw seconds, never from the rounded
    per-zone figures, so they always agree with the data. "" when there is nothing usable.
    HR zones also when the power is broken (_power_broken) or the athlete's level is HR only
    (a beginner is told "effort and HR only - do not reference power"; Calum's ride was
    described from power zones; Jamie, 17 Oct 2026: shadow review)."""
    typ = str(a.get("type") or "")
    secs, kind = [], ""
    if "Ride" in typ and a.get("icu_average_watts") and not hr_only and not _power_broken(a):
        secs = [float(z.get("secs") or 0) for z in (a.get("icu_zone_times") or [])
                if isinstance(z, dict) and str(z.get("id") or "").startswith("Z")]   # not "SS": it overlaps Z3/Z4
        kind = "power"
    if not sum(secs) and hr_trusted:
        secs = [float(x or 0) for x in (a.get("icu_hr_zone_times") or [])]
        kind = "HR"
    while secs and not secs[-1]:
        secs.pop()                           # empty top zones add nothing but noise
    total = sum(secs)
    if not total:
        return ""
    pct = lambda s: round(100 * s / total)
    per = " · ".join(f"Z{i + 1} {pct(s)}%" for i, s in enumerate(secs))
    groups = [f"Z1-Z2 {pct(sum(secs[:2]))}%"]
    if len(secs) > 2:
        groups.append(f"Z3 and above {pct(sum(secs[2:]))}%")
    if len(secs) > 3:
        groups.append(f"Z4 and above {pct(sum(secs[3:]))}%")
    return f"Zone split ({kind} zones, % of time): {per} | " + " · ".join(groups)


def _decoupling_line(a: dict, hr_trusted: bool = True, drift_result: dict | None = None,
                     hr_only: bool = False) -> str:
    """The Drift line for the debrief: lib/drift.session_drift's corrected figure and how it
    was measured, never Intervals.icu's raw decoupling (it counts the warm-up, coasting and
    hills as drift; Jamie, 17 Oct 2026). `drift_result` is that function's dict; None (never
    computed) or no pct gives the leave-it-out instruction, with the reason when there is one."""
    r = drift_result or {}
    pct = r.get("pct")
    if pct is not None and hr_trusted:
        if hr_only:                        # an HR-only athlete: never prompt talk of power
            return f"Drift (warm-up and cool-down cut, coasting and stops removed): {float(pct):+.1f}%"
        return (f"Drift (warm-up and cool-down cut, coasting and stops removed, "
                f"{r.get('method') or 'power:HR'}): {float(pct):+.1f}%")
    why = f" ({r['reason']})" if r.get("reason") and hr_trusted else ""
    return (f"Decoupling: not available{why} - leave decoupling and HR drift out of this "
            "debrief entirely")


def _gap_lines(a: dict, intervals: list | None = None, threshold_s: float | None = None) -> list:
    """Pace for a run. Outdoors every pace quoted is Grade-Adjusted Pace (CLAUDE.md); flat
    pace only on a treadmill. Kathryn has no Strava, so before this she got flat pace."""
    if not _is_run(a):
        return []
    if _treadmill(a):
        sp = a.get("average_speed")
        return [f"Treadmill run: quote flat pace{' ' + _pace_str(sp) if sp else ''} "
                "(GAP does not apply indoors)"]
    gap = a.get("gap")
    if not gap:
        return ["GAP not in Intervals.icu for this run: quote Strava lap gap_pace if fetched; "
                "if there is no GAP anywhere, give no pace figure"]
    gap_s = 1000 / float(gap)
    line = f"Avg GAP {_secs_to_pace(gap_s)} (Intervals.icu)"
    if threshold_s:
        delta = round(gap_s - threshold_s)
        line += (f", {abs(delta)} s/km {'slower' if delta > 0 else 'faster'} than threshold "
                 f"{_secs_to_pace(threshold_s)}" if delta else
                 f", level with threshold {_secs_to_pace(threshold_s)}")
    out = [line]
    work = [iv for iv in (intervals or []) if str(iv.get("type") or "") == "WORK" and iv.get("gap")]
    if work:
        out.append("Work intervals by GAP (Intervals.icu, use when there are no Strava laps): "
                   + " · ".join(f"{i}) {_mmss(iv.get('moving_time') or iv.get('elapsed_time') or 0)}"
                                f" at {_secs_to_pace(1000 / float(iv['gap']))}"
                                for i, iv in enumerate(work[:15], 1)))
    return out


def _mmss(sec) -> str:
    s = int(round(float(sec or 0)))
    return f"{s // 60}:{s % 60:02d}"


def _run_power_line(a: dict) -> str:
    """Running power against the run threshold power in force for this activity (icu_ftp on
    a run is the RUN FTP), with the zone it lands in from the athlete's own power zones."""
    w, ftp = a.get("icu_average_watts"), a.get("icu_ftp")
    if not _is_run(a) or not w or not ftp:
        return ""
    pct = 100 * float(w) / float(ftp)
    line = f"Running power: avg {round(float(w))} W = {round(pct)}% of run threshold power {round(float(ftp))} W"
    bounds = a.get("icu_power_zones") or []
    if bounds:
        z = next((i + 1 for i, b in enumerate(bounds) if pct < float(b)), len(bounds))
        line += f" (power zone Z{z})"
    return line


# --- 17 Oct shadow review: more decisions and sums taken off the model -------------------
# (Jamie, 17 Oct 2026: shadow review) James's 16 Oct swim: Sonnet said 1:37/100m for 1.7 km
# in 29 min (the real figure is 1:43), because the swim stub told it pace = distance /
# (time / 100), which is metres per 100 s; Haiku wrote a line per block for an easy swim;
# and the two disagreed on PLAN_DELTA because nothing in code checked a swim's structure.
# Jamie's ride got "no recent fuelling logged" from Sonnet and "~64 g/hr" from Haiku off
# the same session log. James's gravel ride was called easy on 15 W of broken power.

def _swim_pace_s(a: dict) -> float | None:
    """Seconds per 100 m over the MOVING time: time / (distance / 100). None without both."""
    dist = float(a.get("distance") or a.get("icu_distance") or 0)
    secs = float(a.get("moving_time") or a.get("elapsed_time") or 0)
    if dist <= 0 or secs <= 0:
        return None
    return secs / (dist / 100)


def _swim_blocks(a: dict, intervals: list | None) -> list:
    """[(distance_m, seconds)] for the swum blocks, from Intervals.icu's WORK intervals."""
    out = []
    for iv in intervals or []:
        if str(iv.get("type") or "") != "WORK":
            continue
        d = float(iv.get("distance") or 0)
        t = float(iv.get("moving_time") or iv.get("elapsed_time") or 0)
        if d > 0 and t > 0:
            out.append((d, t))
    return out


def _planned_reps(event: dict | None) -> tuple | None:
    """(count, seconds) of the planned rep set: the short step (3 min or less) repeated
    most, 3 times or more, after expanding nested repeats. None without such a set."""
    steps = []

    def walk(ss, reps=1):
        for _ in range(max(1, int(reps or 1))):
            for st in ss or []:
                if isinstance(st, dict) and isinstance(st.get("steps"), list):
                    walk(st["steps"], st.get("reps"))
                elif isinstance(st, dict) and st.get("duration"):
                    steps.append(st)
    walk(((event or {}).get("workout_doc") or {}).get("steps"))
    if len(steps) < 4:
        return None
    # the work step of a rep set is the one followed by a shorter recovery
    counts: dict = {}
    for i, st in enumerate(steps[:-1]):
        d, nxt = int(st["duration"]), int(steps[i + 1]["duration"])
        if d <= 180 and nxt < d:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    dur, n = max(counts.items(), key=lambda kv: kv[1])
    return (n, dur) if n >= 3 else None


def _swim_structure(event: dict | None, blocks: list) -> str | None:
    """'NOT executed' verdict for a pool swim against its planned rep set, or None when
    there is no rep set or it was done. Done = at least half the planned reps found as
    blocks no longer than twice the planned rep (Jamie's rule, STRUCTURE_RULE: continuous
    blocks against planned reps = not executed; 17 Oct 2026: shadow review)."""
    reps = _planned_reps(event)
    if not reps or not blocks:
        return None
    n, dur = reps
    found = sum(1 for _, t in blocks if t <= 2 * dur)
    if found * 2 >= n:
        return None
    dists = ", ".join(f"{round(d)}" for d, _ in blocks[:10])
    return (f"planned structure NOT executed: plan had {n} x {_mmss(dur)} reps, the swim was "
            f"{len(blocks)} continuous block{'s' if len(blocks) != 1 else ''} ({dists} m)")


def _swim_lines(a: dict, intervals: list | None, css_s: float | None,
                event: dict | None) -> tuple:
    """(lines, not_executed) for a swim: the average pace worked out once, its CSS gap, the
    blocks as ONE line, and the structure verdict. ([], False) for any other sport."""
    if "Swim" not in str(a.get("type") or ""):
        return [], False
    lines = []
    pace = _swim_pace_s(a)
    if pace:
        dist = float(a.get("distance") or a.get("icu_distance") or 0)
        mins = round(float(a.get("moving_time") or a.get("elapsed_time") or 0) / 60)
        line = (f"Swim average: {dist / 1000:.1f} km in {mins} min moving = "
                f"{_secs_to_pace(pace, '/100m')}")
        if css_s:
            d = round(pace - css_s)
            line += (f" ({'+' if d > 0 else '-' if d < 0 else '±'}{abs(d)} s vs CSS "
                     f"{_secs_to_pace(css_s, '/100m')}; positive = slower)")
        line += (f". The session-log and swim-log pace_per_100m for this swim is "
                 f"{pace:.1f} (seconds per 100 m)")
        lines.append(line)
    blocks = _swim_blocks(a, intervals)
    if blocks:
        lines.append("Blocks swum (Intervals.icu, each ~2-4 s/100m pessimistic; Strava laps win "
                     "when fetched): " + " · ".join(
                         f"{round(d)} m {_secs_to_pace(t / (d / 100), '')}" for d, t in blocks[:12])
                     + " - for an easy or technique swim give these as ONE short line at most "
                     "(count, distances), never a line per block")
    verdict = _swim_structure(event, blocks)
    if verdict:
        lines.append("Structure: " + verdict + ". Say 'planned structure not executed' once, "
                     "then describe what was swum in one line")
    return lines, bool(verdict)


def _power_broken(a: dict) -> str:
    """A ride whose power meter dropped out: average watts under a quarter of FTP over 20+
    minutes while Intervals.icu's heart-rate Load is at least double the power Load. James's
    17 Oct gravel ride: 15 W average (NP 98), power Load 31 against HR Load 124, and both
    models called it easy at "98% in Zones 1-2". "" when power looks real."""
    w, ftp = a.get("icu_average_watts"), a.get("icu_ftp")
    if "Ride" not in str(a.get("type") or "") or not w or not ftp:
        return ""
    if float(a.get("moving_time") or 0) < 1200 or float(w) >= 0.25 * float(ftp):
        return ""
    pl, hl = a.get("power_load"), a.get("hr_load")
    if not pl or not hl or float(hl) < 2 * float(pl):
        return ""
    return (f"POWER DATA BROKEN: avg {round(float(w))} W is {round(100 * float(w) / float(ftp))}% "
            f"of FTP {round(float(ftp))} W, so the power meter dropped out. Load "
            f"{a.get('icu_training_load')} comes from that power and reads far too low; the "
            f"heart-rate Load is {round(float(hl))}. Give Load as '{a.get('icu_training_load')} "
            f"from power, which reads low - heart rate puts it at {round(float(hl))}'. Describe "
            "this ride by heart rate only: no watts, no power zones, never 'easy on power'")


def _recent_fuel(session_log: list, exclude_id: str = "", n: int = 4) -> str:
    """The 'recent avg' for the long-ride fuelling question: g carbs per hour over the last
    n rides over 90 min with nutrition_g_carb logged (grams for the whole ride)."""
    rates = []
    for e in session_log or []:
        if exclude_id and str(e.get("activity_id") or "") == exclude_id:
            continue
        if "Ride" not in str(e.get("sport") or ""):
            continue
        g, mins = e.get("nutrition_g_carb"), e.get("duration_min")
        if not isinstance(g, (int, float)) or isinstance(g, bool) or not isinstance(mins, (int, float)):
            continue
        if mins > 90:
            rates.append(g / (mins / 60))
        if len(rates) >= n:
            break
    if not rates:
        return "none logged on recent rides over 90 min"
    return (f"{round(sum(rates) / len(rates))} g/hr over the last {len(rates)} "
            f"ride{'s' if len(rates) != 1 else ''} over 90 min")


def _decoupling_entry(a: dict, decoupling, hr_trusted: bool = True,
                      method: str | None = None) -> dict | None:
    """The decoupling-log entry, built in code: rides of 3 hours or more with a corrected
    drift figure (lib/drift.py, not Intervals.icu's raw one) only. `method` ("power:HR"...)
    goes in the entry when known. (Sonnet wrote a DECOUPLING line for Fred's 124-min ride;
    the prompt said over 3 hours, nothing held it; 17 Oct 2026: shadow review.)"""
    if "Ride" not in str(a.get("type") or "") or decoupling is None or not hr_trusted:
        return None
    mins = float(a.get("moving_time") or 0) / 60
    if mins < 180:
        return None
    entry = {"activity_id": str(a.get("id")), "date": str(a.get("start_date_local") or "")[:10],
             "name": a.get("name") or "", "duration_min": round(mins),
             "if": round(float(a.get("icu_intensity") or 0) / 100, 2),
             "decoupling_pct": round(float(decoupling), 1),
             "tss": float(a.get("icu_training_load") or 0)}
    if method:
        entry["method"] = method
    return entry


def _pick_target(cand_ids: list, acts: list | None) -> str | None:
    """The one activity this run debriefs: the most recent unlogged one (the prompt's own
    rule, now decided here), and of two copies of the same session the higher id. Calum's
    17 Oct pair: Sonnet took the older ride, Haiku the newer, so the pair compared two
    different sessions (17 Oct 2026: shadow review)."""
    by_id = {str(a.get("id")): a for a in acts or []}
    pool = [by_id[c] for c in cand_ids or [] if c in by_id]
    if not pool:
        return None
    newest = max(pool, key=lambda a: (str(a.get("start_date_local") or ""), _num_id(a)))
    group = [a for a in pool if a is newest or duplicates.same_session(a, newest)]
    return str(max(group, key=_num_id).get("id"))


def _num_id(a: dict) -> int:
    try:
        return int(re.sub(r"\D", "", str(a.get("id") or "")) or 0)
    except ValueError:
        return 0


def _plan_delta(a: dict, event: dict | None) -> str | None:
    """`name|planned|actual|delta_pct` against the event Intervals.icu PAIRED with this
    activity (paired_event_id), or None. Whole numbers, and the % comes from the two whole
    numbers shown, so the athlete's own sum agrees with it."""
    if not event:
        return None
    try:
        import plan_tools
        planned = (plan_tools._event_tss(event) or {}).get("tss")
    except Exception:
        planned = event.get("icu_training_load") or event.get("load_target")
    actual = a.get("icu_training_load")
    if not planned or actual is None:
        return None
    p, act = round(float(planned)), round(float(actual))
    if not p:
        return None
    name = str(event.get("name") or "planned session").replace("|", "/").strip()
    return f"{name}|{p}|{act}|{round((act - p) / p * 100)}"


def _activity_facts(a: dict, event: dict | None = None, intervals: list | None = None,
                    hr_trusted: bool = True, threshold_s: float | None = None,
                    css_s: float | None = None, hr_only: bool = False,
                    drift_result: dict | None = None) -> tuple:
    """(lines, plan_delta_raw, tiny) for one activity. Pure: everything is passed in,
    `drift_result` being lib/drift.session_drift's dict (None: no figure)."""
    moving = float(a.get("moving_time") or a.get("elapsed_time") or 0)
    if 0 < moving < TINY_RECORDING_S:
        return ([f"TINY RECORDING: only {int(moving)} s recorded - no analysis, the code "
                 "writes this one's message"], None, True)
    lines = []
    pb = "" if hr_only else _power_broken(a)
    if pb:
        lines.append(pb)
    z = _zone_split_line(a, hr_trusted, hr_only)
    if z:
        lines.append(z)
    if _is_run(a) or "Ride" in str(a.get("type") or ""):
        # decoupling is power against HR: meaningless when the power dropped out
        lines.append(_decoupling_line(a, hr_trusted and not pb, drift_result, hr_only))
    lines += _gap_lines(a, intervals, threshold_s)
    rp = _run_power_line(a)
    if rp:
        lines.append(rp)
    swim, not_done = _swim_lines(a, intervals, css_s, event)
    lines += swim
    pd = _plan_delta(a, event)
    if pd and not_done:
        # STRUCTURE_RULE: no vs-plan Load delta for a structure that was not swum, decided
        # here so both models give the same answer (17 Oct 2026: shadow review).
        lines.append("PLAN_DELTA: none (planned structure not executed)")
        return lines, None, False
    lines.append(f"PLAN_DELTA: {pd or 'none'}"
                 + ("" if pd else " (Intervals.icu paired no planned session with it)"))
    return lines, pd, False


def _tiny_message(a: dict) -> str:
    sport = {"VirtualRide": "ride", "VirtualRun": "run", "WeightTraining": "session"}.get(
        str(a.get("type") or ""), str(a.get("type") or "session").lower())
    secs = int(float(a.get("moving_time") or a.get("elapsed_time") or 0))
    return (f"That {sport} only recorded {secs} seconds. Was it an accidental start, "
            "or did the watch cut out?")


DRIFT_MIN_S = 2400    # drift is only worked out for a ride or run of 40 minutes or more


def _session_drift(client, a: dict, trusted: bool) -> dict:
    """lib/drift.session_drift for one activity: {"pct", "method", "reason"}. Streams are
    fetched once, and only for a ride or run of DRIFT_MIN_S moving time or more with a
    trusted heart rate and working power. Fail-soft: any error is "no figure", never a crash.
    This replaces Intervals.icu's raw `decoupling`, which counts the warm-up, coasting and
    hills as drift (Jamie, 17 Oct 2026)."""
    typ = str(a.get("type") or "")
    no = lambda why: {"pct": None, "method": None, "reason": why}
    if "Ride" not in typ and "Run" not in typ:
        return no("not a ride or run")
    if float(a.get("moving_time") or 0) < DRIFT_MIN_S:
        return no("under 40 minutes")
    if not trusted:
        return drift.session_drift(a, [], False)
    if _power_broken(a):
        return no("power meter dropped out")
    try:
        streams = client.get_activity_streams(str(a["id"]))
        if not streams:
            return no("no stream data")
        return drift.session_drift(a, streams, True)
    except Exception as exc:
        print(f"[drift:{a.get('id')}] {exc}", file=sys.stderr)
        return no("streams unavailable")


def _facts_pass(slug: str, session_log_f: Path, threshold_s: float | None = None,
                css_s: float | None = None, hr_only: bool = False) -> tuple:
    """COMPUTED FACTS for each activity in the last 3 days not yet in the session log.
    Returns (prompt block, {activity_id: {"plan_delta", "tiny", "decoupling", "drift", "activity"}}),
    "decoupling" being the corrected drift pct (lib/drift.py) and "drift" its whole result.
    Fail-soft: ("", {}) on any error, and the prompt's own rules still apply."""
    try:
        from icu_api import IcuClient
        cfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        logged = set()
        if session_log_f.exists():
            logged = {str(e.get("activity_id", "")) for e in json.loads(session_log_f.read_text())}
        acts = [a for a in client.get_training_history(days=3) or []
                if str(a.get("id")) not in logged and a.get("type")
                and str(a.get("source") or "").upper() != "STRAVA"][:8]
        if not acts:
            return "", {}
        blocks, meta = [], {}
        for a in acts:
            aid = str(a["id"])
            event = intervals = None
            if a.get("paired_event_id"):
                try:
                    event = client.get_event(a["paired_event_id"])
                except Exception as exc:
                    print(f"[facts:{slug}] event {a.get('paired_event_id')}: {exc}", file=sys.stderr)
            if ((_is_run(a) and not _treadmill(a)) or "Swim" in str(a.get("type") or "")) \
                    and a.get("moving_time", 0) >= TINY_RECORDING_S:
                try:
                    intervals = (client.get_extended_metrics(aid) or {}).get("icu_intervals")
                except Exception:
                    intervals = None
            trusted = not hr_quality.is_untrusted(slug, aid)
            dr = _session_drift(client, a, trusted)
            lines, pd, tiny = _activity_facts(a, event, intervals, trusted, threshold_s,
                                              css_s=css_s, hr_only=hr_only, drift_result=dr)
            meta[aid] = {"plan_delta": pd, "tiny": tiny, "activity": a,
                         "decoupling": dr.get("pct"), "drift": dr}
            blocks.append(f"{a.get('name') or a.get('type')} ({aid}, "
                          f"{str(a.get('start_date_local') or '')[:10]} {a.get('type')}):\n  "
                          + "\n  ".join(lines))
        return ("COMPUTED FACTS (done in code from Intervals.icu - authoritative; quote these "
                "figures as given, never re-add zones, compute decoupling or match planned "
                "sessions yourself):\n" + "\n".join(blocks)), meta
    except Exception as exc:
        print(f"[facts:{slug}] failed: {exc}", file=sys.stderr)
        return "", {}


def _hold_plan_delta(model_raw: str | None, meta: dict | None, analysis: str,
                     structure_note: str, activity_id: str) -> str | None:
    """The PLAN_DELTA the code acts on. With computed facts for this activity, the computed
    line wins (Sonnet wrote "none" for a ride paired to a planned 39), except where the
    planned structure was not executed: EXECUTED STRUCTURE says so for this activity, or the
    model said "none" and wrote "planned structure not executed", as STRUCTURE_RULE asks.
    Without facts (the pass failed), the model's line stands, checked live further on."""
    if meta is None:
        return model_raw
    pd = meta.get("plan_delta")
    if not pd:
        return "none"
    not_done = any(f"({activity_id})" in ln and "NOT fully executed" in ln
                   for ln in (structure_note or "").split("\n"))
    if (model_raw or "none").strip().lower() == "none" and \
            "planned structure not executed" in (analysis or "").lower():
        not_done = True
    return "none" if not_done else pd


def _resolve_css(slug: str) -> str | None:
    """Swim CSS as M:SS per 100m from Intervals.icu's Swim threshold_pace (m/s), the same
    source as lib/thresholds.py. Not profile.json: its swim_css_per_100m went stale (Sonnet
    quoted 1:39 against a real 1:41, Jamie, 16 Oct 2026: shadow review). None when unset."""
    try:
        from icu_api import IcuClient
        cfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
        swim = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"]).get_sport_settings("Swim") or {}
        if isinstance(swim, list):
            swim = swim[0] if swim else {}
        mps = swim.get("threshold_pace")
        return _secs_to_pace(100 / float(mps), "") if mps else None
    except Exception as exc:
        print(f"[css:{slug}] lookup failed: {exc}", file=sys.stderr)
        return None


def _hr_quality_pass(slug: str, chat_id: str) -> str:
    """Code-side HR check and baseline-test capture for the last 3 days' activities,
    run BEFORE the analysis prompt is built so the model is told which HR to ignore.

    HR (27 Sep 2026, lib/hr_quality.py): each run/ride/swim not yet in the athlete's
    hr-quality-log.json is judged from its streams and recorded; consumers then skip
    the bad ones. Returns the prompt note naming this window's untrusted activities
    ("" when none), so a cadence-locked "175 bpm easy run" is never debriefed as hard.

    Baseline (lib/baseline.py): an activity that is a scheduled baseline test has its
    result read here, deterministically, and sent with a confirm keyboard; nothing is
    applied until the athlete taps. Fail-soft throughout: an ICU hiccup costs this
    cycle's check, never the debrief."""
    try:
        from icu_api import IcuClient
        cfg = json.loads(ATHLETES_CONFIG.read_text())[slug]
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        acts = client.get_training_history(days=3)
    except Exception as exc:
        print(f"[hr-quality:{slug}] history fetch failed: {exc}", file=sys.stderr)
        return ""
    log = hr_quality.load_log(slug)
    st = baseline_lib.load(slug)
    changed = False
    for a in acts:
        aid = str(a.get("id", ""))
        fam = baseline_lib.family_of(a.get("type"))
        if not aid or not fam:
            continue
        is_test = bool(st) and baseline_lib.match_test(st, a) is not None
        if aid in log and not is_test:
            continue
        try:
            streams = hr_quality.streams_by_type(client.get_activity_streams(aid))
            res = hr_quality.assess(streams, a.get("type") or "", a.get("athlete_max_hr"),
                                    bool(a.get("race")))
            hr_quality.record(slug, a, res, log=log, save=False)
            changed = True
            if is_test:
                ivs = None
                if fam == "swim":
                    ivs = (client.get_extended_metrics(aid) or {}).get("icu_intervals")
                out = baseline_lib.capture(slug, a, streams, hr_quality.trusted(res),
                                           intervals=ivs, st=st)
                if out:
                    if out.get("keyboard"):
                        _tg_send_keyboard(chat_id, out["text"], out["keyboard"])
                        try:
                            _log_to_history(slug, out["text"])
                        except Exception:
                            pass
                    else:
                        _notify(out["text"], chat_id, slug=slug)
        except Exception as exc:
            print(f"[hr-quality:{slug}] {aid}: {exc}", file=sys.stderr)
    if changed:
        try:
            hr_quality.save_log(slug, log)
        except Exception as exc:
            print(f"[hr-quality:{slug}] log not saved: {exc}", file=sys.stderr)
    bad = []
    for a in acts:
        r = log.get(str(a.get("id", "")))
        if r and r.get("verdict") in ("bad", "none") and not r.get("informational"):
            why = hr_quality.describe(r) or "unreliable"
            bad.append(f"  - {a.get('id')} ({(a.get('start_date_local') or '')[:10]} "
                       f"{a.get('type')}, \"{a.get('name') or ''}\"): {why}")
    if not bad:
        return ""
    return ("HR CHECK (done in code from the raw streams; trust it over the HR numbers):\n"
            + "\n".join(bad) + "\n"
            "For THESE activities the heart rate is untrusted: do NOT quote average/max HR, HR "
            "zones, time under an HR cap or decoupling, and do not judge the effort by HR. "
            "Describe the session by pace/power and RPE. Where HR was recorded but is "
            "unreliable, say so once, plainly; where none was recorded, just leave HR out. "
            "Do not output a DECOUPLING line for them.")


def _baseline_tick(slug: str, chat_id: str) -> None:
    """Close a finished baseline block and tell the athlete, once. Then rebuild their
    blueprint (Jamie, 30 Sep 2026): the one written at sign-up predates the tested
    zones and any history that arrived since (Download old data, Strava copying)."""
    try:
        msg = baseline_lib.tick(slug)
        if msg and chat_id:
            _notify(msg, chat_id, slug=slug)
    except Exception as exc:
        print(f"[baseline:{slug}] tick failed: {exc}", file=sys.stderr)
        return
    if msg:
        try:
            subprocess.Popen(["python3", str(BASE / "scripts" / "generate-blueprint.py"),
                              "--athlete", slug], cwd=str(BASE.parent))
            print(f"[baseline:{slug}] block closed, blueprint rebuilding")
        except Exception as exc:
            print(f"[baseline:{slug}] blueprint rebuild failed to start: {exc}", file=sys.stderr)
        # A goal without a race (lib/goals.py): block 1 starts the coming Monday, measured
        # from the numbers the baseline week just tested.
        try:
            acfg = json.loads(ATHLETES_CONFIG.read_text()).get(slug) or {}
            if goals_lib.goal_cfg(acfg) and not acfg["goal"].get("start"):
                ctl = None
                try:
                    from icu_api import IcuClient
                    w = IcuClient(acfg["icu_athlete_id"], acfg["icu_api_key"]).get_wellness(days=3)
                    ctl = (w[-1].get("ctl") if w else None)
                except Exception:
                    pass
                g = goals_lib.pin_start(slug, ctl=ctl)
                if g:
                    print(f"[baseline:{slug}] goal block 1 starts {g.get('start')}")
        except Exception as exc:
            print(f"[baseline:{slug}] goal start not pinned: {exc}", file=sys.stderr)


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


_HISTORY_SPORT = {"VirtualRide": "Ride", "GravelRide": "Ride", "MountainBikeRide": "Ride",
                  "TrailRun": "Run", "VirtualRun": "Run", "WeightTraining": "Strength"}


def _log_pre_activation_history(slug: str, athlete_cfg: dict, session_log_f: Path,
                                acts: list | None = None) -> int:
    """Sessions from BEFORE the athlete was approved are history, not news (Fred, 1 Oct
    2026: approved at 20:32, his Garmin history landed in Intervals.icu, and the 29 and
    30 Sep strength sessions were debriefed with "Quick log - RPE" and asked about again
    at the evening check-in). Each one inside the watcher's window is written to the
    session log as a quiet history entry, so the gate never sees it as new. Athletes with
    no `activated` time (onboarded before 2 Oct 2026) are untouched. Returns the count."""
    activated = str(athlete_cfg.get("activated") or "")
    if not activated:
        return 0
    if acts is None:                    # the cycle's own 3-day fetch, when it has one
        try:
            from icu_api import IcuClient
            acts = IcuClient(athlete_cfg["icu_athlete_id"],
                             athlete_cfg.get("icu_api_key") or "").get_training_history(days=3) or []
        except Exception:
            return 0
    old = [a for a in acts if str(a.get("start_date_local") or "")[:19] < activated[:19]]
    if not old:
        return 0
    try:
        log = json.loads(session_log_f.read_text()) if session_log_f.exists() else []
    except (json.JSONDecodeError, OSError):
        return 0
    have = {str(e.get("activity_id", "")) for e in log}
    added = []
    for a in old:
        aid = str(a.get("id") or "")
        if not aid or aid in have:
            continue
        added.append({"activity_id": aid, "date": str(a.get("start_date_local"))[:10],
                      "name": a.get("name") or "", "sport": _HISTORY_SPORT.get(a.get("type"), a.get("type")),
                      "tss": int(a.get("icu_training_load") or 0),
                      "duration_min": round((a.get("moving_time") or 0) / 60),
                      "rpe": None, "feel": None, "notes": None,
                      "logged_at": datetime.now().isoformat(timespec="seconds"),
                      "stub": True, "history": True})
    if added:
        session_log_f.write_text(json.dumps(added + log, indent=2))
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] {len(added)} session(s) from "
              f"before approval logged as history, not debriefed")
    return len(added)


def _recent_activities(slug: str) -> list | None:
    """The last 3 days of Intervals.icu activities, or None if the fetch failed."""
    try:
        r = subprocess.run(
            ["python3", str(BASE / "lib/icu_fetch.py"), "--athlete", slug,
             "--caller", CALLER, "--endpoint", "history", "--days", "3"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=30,
        )
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


def _duplicate_tick(slug: str, chat_id: str, acts: list | None) -> None:
    """Ask once about each session that is in Intervals.icu twice (lib/duplicates.py).
    The buttons are handled by the bot's dup: callback."""
    if not chat_id or not acts:
        return
    try:
        for out in duplicates.new_questions(slug, acts):
            _tg_send_keyboard(chat_id, out["text"], out["keyboard"])
            _log_to_history(slug, out["text"])
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] asked about a duplicate", file=sys.stderr)
    except Exception as exc:
        print(f"[{slug}] duplicate check failed: {exc}", file=sys.stderr)


# What one athlete's analysis may cost (16 Oct 2026, Jamie: Kathryn's 139 Sonnet runs in
# a day were "an unacceptable fall back"). Two rules, so no loop can run it every 5 min:
#   - Intervals.icu unreadable: the model is NOT run. It reads the same Intervals.icu, so
#     it could only answer "none"; the activity is still there when Intervals.icu answers
#     again, so nothing is missed. An hour of it is one ops-log line.
#   - The same unlogged activities and nothing logged since: at most MAX_PASSES passes,
#     RETRY_GAP_S apart, then it stops (one ops-log line) until something changes. A new
#     activity, or one getting logged, changes the set and starts the count again.
MAX_PASSES = 3
RETRY_GAP_S = 30 * 60
ICU_ALERT_AFTER_S = 60 * 60


def _has_new_activity(slug: str, existing_ids: set, acts: list | None = None) -> bool:
    """True only when an unlogged activity is visible: see _new_candidates."""
    return bool(_new_candidates(slug, existing_ids, acts))


def _new_candidates(slug: str, existing_ids: set, acts: list | None = None) -> list | None:
    """Cheap, LLM-free pre-gate: the ids of Intervals.icu activities not already in
    session-log.json. This lets the expensive analysis LLM be skipped entirely on the
    ~99% of cycles with nothing new — so the watcher can poll often cheaply.

    A second recording of a logged session is not new: the analysis prompt's
    duplicate guard answers ACTIVITY_ID: none for it, so letting it through would
    call Sonnet every 5 min until it leaves the 3-day window.

    None when Intervals.icu could not be read, or this check itself failed: nobody can
    tell, the model included, so the caller waits for the next cycle (fail CLOSED since
    16 Oct 2026; it used to fail open, and a 503 then ran Sonnet for every athlete every
    5 minutes)."""
    try:
        acts = _recent_activities(slug) if acts is None else acts
        if acts is None:
            return None
        # A Strava-sourced activity comes through the API as an empty stub (no type, no
        # time, no data): there is nothing to analyse, and the watch's own copy is the
        # real one. Kathryn's 15 Oct run had one, and Sonnet answered ACTIVITY_ID: none
        # every 5 minutes for it.
        acts = [a for a in acts if str(a.get("source") or "").upper() != "STRAVA"]
        logged = [a for a in acts if duplicates.activity_ids(a) & existing_ids]
        out = []
        for a in acts:
            if duplicates.activity_ids(a) & existing_ids or any(duplicates.same_session(a, b) for b in logged):
                continue
            out.append(str(a.get("id")))
        return out
    except Exception:
        return None


def _pass_allowed(state: dict, cands: list, now: float | None = None,
                  manual: bool = False) -> tuple[bool, str]:
    """(run the analysis?, why not: "wait" / "max"). Only a repeat is limited: the same
    unlogged activities as the last pass, with nothing logged since. An athlete's own
    "Check for activity" tap always runs."""
    if manual:
        return True, ""
    last = state.get("analysis_pass") or {}
    if last.get("set") != sorted(cands):
        return True, ""
    if last.get("count", 0) >= MAX_PASSES:
        return False, "max"
    if (now or time.time()) - float(last.get("ts") or 0) < RETRY_GAP_S:
        return False, "wait"
    return True, ""


def _record_pass(state: dict, cands: list, now: float | None = None) -> None:
    last = state.get("analysis_pass") or {}
    same = last.get("set") == sorted(cands)
    state["analysis_pass"] = {"set": sorted(cands), "ts": now or time.time(),
                              "count": (last.get("count", 0) if same else 0) + 1}


def _icu_readable(state: dict, state_file, ok: bool, slug: str = "",
                  now: float | None = None) -> None:
    """Track an unreadable Intervals.icu: one ops-log line once it has lasted an hour."""
    now = now or time.time()
    if ok:
        if state.pop("icu_unreadable_since", None) is not None:
            state.pop("icu_unreadable_alerted", None)
            save_state(state, state_file)
        return
    since = state.setdefault("icu_unreadable_since", now)
    if now - since >= ICU_ALERT_AFTER_S and not state.get("icu_unreadable_alerted"):
        state["icu_unreadable_alerted"] = True
        ops_log.alert("activity-watcher", f"Intervals.icu unreadable for "
                      f"{int((now - since) // 60)} min: activity analysis waiting for it "
                      f"(not run blind)", athlete=slug)
    save_state(state, state_file)


def _last_logged_label(session_log_f) -> str:
    """'Swim today · 51min · Load 68' for the most recent session-log entry."""
    try:
        entries = json.loads(session_log_f.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    dated = [e for e in entries if e.get("date")]
    if not dated:
        return ""
    e = max(dated, key=lambda x: x["date"])
    today = datetime.now().strftime("%Y-%m-%d")
    when = "today" if e["date"] == today else e["date"]
    bits = [f"{e.get('sport', 'session')} {when}"]
    if e.get("duration_min"):
        bits.append(f"{int(e['duration_min'])}min")
    if e.get("tss"):
        bits.append(f"Load {int(e['tss'])}")
    return " · ".join(bits)


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


MIN_ASK_MIN = 15      # warm-up / cool-down bits: never asked about (Jamie's 3 and 10 min rides, 7 Oct)


def _too_short_to_ask(e: dict) -> bool:
    try:
        return 0 < float((e or {}).get("duration_min") or 0) < MIN_ASK_MIN
    except (TypeError, ValueError):
        return False


def _strength_with_hr(e: dict) -> bool:
    """A strength session recorded with heart rate: its load comes from HR, so RPE is never
    asked for it, by card or reminder (Jamie, 7 Oct 2026)."""
    return (e or {}).get("sport") in ("Strength", "WeightTraining") and bool((e or {}).get("avg_hr"))


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
        if e.get("history"):            # from before approval: never asked about
            continue
        if not e.get("stub", False):
            continue
        sport = e.get("sport", "session")
        # For injury runs the ANALYSIS already asks for injury score — skip nudge if that's done
        if sport == "Run" and has_injury:
            if e.get("injury_pain_during") is not None or e.get("rpe") is not None:
                continue
        elif e.get("rpe") is not None:
            continue
        if _strength_with_hr(e) or _too_short_to_ask(e):   # never asked about at all
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
            if (e.get("duration_min") or 0) >= ask_gate.LONG_SESSION_MIN \
                    and coaching_prefs.fuelling_on(slug):
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
                         coaching_level: str = "mid", slug: str | None = None) -> str:
    """Call Claude to write a neutral, factual 3-line Strava description. `slug` says
    which account pays (lib/claude_billing.py)."""
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
        # claude_billing.run, not subprocess.run: on a refused API key the CLI prints the
        # refusal to stdout, and it must never become the athlete's public description.
        result = claude_billing.run(
            [CLAUDE, "-p", "--model", "haiku", "--effort", "high"],
            athlete=slug,
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
        result = claude_billing.run(
            [CLAUDE, "-p", "--model", "haiku", "--effort", "high"],
            athlete=slug,
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
        from strava_client import StravaClient, safe_to_auto_write_description

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
        from strava_client import StravaClient, safe_to_auto_write_description

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

        if not safe_to_auto_write_description(slug, icu_activity_id, strava_id, sc, session_entry):
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Description left alone on {icu_activity_id}: athlete-authored or not snapshotted", file=sys.stderr)
            return

        description = _strava_description(
            first_name, sport, analysis, plan_delta_raw, session_entry,
            laps=laps, splits=splits, segment_prs=segment_prs,
            coaching_level=coaching_level, slug=slug,
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

    first_name = profile.get("name", slug).split()[0]
    injuries = [_i for _i in profile.get("injuries", []) if str(_i.get("status") or "").lower() not in ("cleared", "resolved")]   # cleared = not current
    run_hr_cap       = int(athlete_cfg.get("run_hr_cap", 150))
    nutrition_target = fuel_basis.ceiling(athlete_cfg)        # never Jamie's 90 for a non-racer
    nutrition_label = fuel_basis.label(athlete_cfg)

    state = load_state(state_file)

    # Dedup session log before any processing to catch merge-conflict duplicates, and the
    # other one-entry-per-activity logs the analysis appends to (13 Oct 2026: Jamie's
    # swim-log held 26 Aug twice, which double-counts it in the swim trends).
    _dedup_session_log(session_log_f)
    for _log in ("swim-log.json", "decoupling-log.json", "run-durability-log.json"):
        _dedup_session_log(adir / _log)

    # Test reminders and Strava refresh run every cycle regardless of new activity
    _check_test_reminders(adir, chat_id, state, state_file)
    _strava_refresh_updated(slug, state, state_file)
    _baseline_tick(slug, chat_id)

    # Webhooks instead of polling (lib/icu_changes.py, 15 Oct 2026): an athlete linked
    # through Peak is only asked for activities when Intervals.icu said one arrived, or
    # every WATCH_EVERY_S as a safety net. Everyone else: every cycle, as before.
    poll_started = time.time()
    why = icu_changes.reason(slug, "watcher", WATCH_EVERY_S, icu_changes.ACTIVITY_TYPES)
    poll = bool(why)
    recent = _recent_activities(slug) if poll else []
    if poll and recent is not None:
        icu_changes.mark_run(slug, "watcher", poll_started)

    if poll:
        _log_pre_activation_history(slug, athlete_cfg, session_log_f, acts=recent)

    # Strength sessions logged in Peak go into the watch's own activity, or become a
    # manual one if the watch never sends it (lib/strength_log.py, 17 Oct 2026). Only
    # touches Intervals.icu while a logged session is still unmerged.
    try:
        import strength_log
        if any(not r.get("icu") for r in strength_log.load(slug)) and athlete_cfg.get("icu_athlete_id"):
            from icu_api import IcuClient
            for _line in strength_log.reconcile(slug, IcuClient(athlete_cfg["icu_athlete_id"],
                                                                athlete_cfg.get("icu_api_key") or "")):
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] strength: {_line}", file=sys.stderr)
    except Exception as exc:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] strength merge failed: {exc}", file=sys.stderr)

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
    _duplicate_tick(slug, chat_id, recent)
    if why in ("interval", "new-day") and recent:
        _note_webhook_misses(slug, recent, existing_ids)
    cands = _new_candidates(slug, existing_ids, recent)
    _icu_readable(state, state_file, cands is not None, slug)
    if cands is None:
        if announce_empty and chat_id:
            _notify("Intervals.icu isn't answering just now, so I can't check. Your "
                    "session will be picked up as soon as it's back.", chat_id, slug=slug)
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries,
                             state_file=state_file, slug=slug)
        return
    allowed, why_not = _pass_allowed(state, cands, manual=announce_empty) if cands else (False, "")
    if not allowed:
        last_pass = state.get("analysis_pass") or {}
        if why_not == "max" and not last_pass.get("alerted"):
            last_pass["alerted"] = True
            save_state(state, state_file)
            ops_log.alert("activity-watcher", f"{len(cands)} activit"
                          f"{'y' if len(cands) == 1 else 'ies'} still not logged after "
                          f"{MAX_PASSES} analysis passes ({', '.join(cands)}): stopped "
                          f"until something changes", athlete=slug)
    if not cands or not allowed:
        if announce_empty and chat_id:
            last = _last_logged_label(session_log_f)
            msg = (f"Nothing new to log — last session already logged: {last}. 👍"
                   if last else "Nothing new to log. 👍")
            _notify(msg, chat_id, slug=slug)
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries,
                             state_file=state_file, slug=slug)
        return
    # Counted before the run, so a timeout or a crash is a pass too.
    _record_pass(state, cands)
    save_state(state, state_file)

    # Looked up only now there is something to analyse: it is an Intervals.icu call, and
    # it used to run every 5 minutes for every athlete whether or not anything was new.
    ftp = _resolve_ftp(slug, profile, session_log_f)

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

    hr_note = _hr_quality_pass(slug, chat_id)
    _terr = _terrain_pass(slug, session_log_f)
    if _terr:
        hr_note = (hr_note + "\n\n" + _terr).strip()
    _struct = _structure_pass(slug, session_log_f)
    if _struct:
        hr_note = (hr_note + "\n\n" + _struct).strip()
    _over = _overshoot_pass(slug, session_log_f)
    if _over:
        hr_note = (hr_note + "\n\n" + _over).strip()
    # Zone split, decoupling, GAP, run power and PLAN_DELTA computed here, not by the model
    # (Jamie, 16 Oct 2026: shadow review). facts_meta lets the code hold PLAN_DELTA,
    # decoupling and tiny recordings to these figures whatever the model writes.
    _thr = profile_fields.pace(profile.get("run_threshold_pace_per_km"))
    _thr_s = (int(_thr.split(":")[0]) * 60 + int(_thr.split(":")[1])) if _thr else None
    # Swim pace vs CSS, swim structure, broken power, HR-only zones, the fuelling average and
    # which activity to debrief: also decided here (Jamie, 17 Oct 2026: shadow review).
    _css = _resolve_css(slug)
    _css_s = (int(_css.split(":")[0]) * 60 + int(_css.split(":")[1])) if _css else None
    _hr_only = str(profile.get("coaching_level") or "").lower() == "beginner"
    _facts, facts_meta = _facts_pass(slug, session_log_f, threshold_s=_thr_s,
                                     css_s=_css_s, hr_only=_hr_only)
    if _facts:
        hr_note = (hr_note + "\n\n" + _facts).strip()
    try:
        _fuel = _recent_fuel(json.loads(session_log_f.read_text()))
    except Exception:
        _fuel = None
    prompt = _build_prompt(slug, first_name, ftp, injuries, profile,
                           run_hr_cap=run_hr_cap, nutrition_target=nutrition_target,
                           nutrition_label=nutrition_label,
                           recent_chat=recent_chat, hr_note=hr_note,
                           css=_css, recent_fuel=_fuel,
                           target_id=_pick_target(cands, recent))
    prompt += coaching_prefs.prompt_note(slug)

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
        athlete=slug,
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
    # Shadow week (lib/shadow.py): Haiku writes the same debrief, read-only, never sent.
    _ids = re.findall(r"^ACTIVITY_ID:\s*(i?\d+)", output, re.M)
    if _ids:
        shadow.launch("debrief", slug, prompt, output, note=(
            "TEST RE-RUN, READ-ONLY: the live run has just logged activity "
            f"{', '.join(_ids)} to session-log.json. Treat it as not logged yet; every other "
            "rule, including the ALREADY-DISCUSSED CHECK, applies as written. Do not write "
            "any file; output only."))

    activity_id = None
    decoupling_raw = None
    plan_delta_raw = None
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
        elif in_analysis and not line.startswith(("ACTIVITY_ID:", "DECOUPLING:", "PLAN_DELTA:", "TEST_RESULT:")):
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

    # last_id/notified_at are set once the debrief send is confirmed below —
    # not here — so a dropped Telegram send leaves the activity un-debriefed
    # and the next watcher pass retries it (i183196403).

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

    # Log decoupling for long rides. Never for an activity whose HR failed the code-side
    # check (lib/hr_quality.py): the prompt says so too, this is the gate that holds.
    # With computed facts for this activity the entry is built in code from Intervals.icu
    # (rides of 3 h or more, the corrected drift figure of lib/drift.py), whatever DECOUPLING line the model
    # wrote: Sonnet logged Fred's 124-min ride (Jamie, 17 Oct 2026: shadow review), and both
    # models once invented decoupling from laps (16 Oct). Without facts, the model's line.
    try:
        entry = None
        _fm = facts_meta.get(activity_id)
        if _fm is not None:
            entry = _decoupling_entry(_fm["activity"], _fm.get("decoupling"),
                                      not hr_quality.is_untrusted(slug, activity_id),
                                      (_fm.get("drift") or {}).get("method"))
        elif decoupling_raw and decoupling_raw != "none" and \
                not hr_quality.is_untrusted(slug, decoupling_raw.split("|")[0].strip()):
            parts = decoupling_raw.split("|")
            if len(parts) == 7 and parts[5].strip():
                entry = {
                    "activity_id": parts[0].strip(),
                    "date":         parts[1].strip(),
                    "name":         parts[2].strip(),
                    "duration_min": float(parts[3].strip()),
                    "if":           float(parts[4].strip()),
                    "decoupling_pct": float(parts[5].strip()),
                    "tss":          float(parts[6].strip()),
                }
        if entry:
            entries = json.loads(decoupling_log.read_text()) if decoupling_log.exists() else []
            if not any(e.get("activity_id") == entry["activity_id"] for e in entries):
                entries.append(entry)
                decoupling_log.write_text(json.dumps(entries, indent=2))
    except Exception:
        pass

    plan_delta_raw = _hold_plan_delta(plan_delta_raw, facts_meta.get(activity_id),
                                      "\n".join(analysis_lines), _struct, activity_id)
    _computed_pd = activity_id in facts_meta
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
                live = None if _computed_pd else _live_planned_load(slug, plan_name)
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
    tiny = bool((facts_meta.get(activity_id) or {}).get("tiny"))
    if tiny:
        # Under 2 minutes is an accidental start, not a session: one fixed question, no
        # analysis, no plan delta, no Quick log card, nothing on Strava (Jamie, 16 Oct
        # 2026: shadow review, a 13-second recording was asked "How did it feel?").
        analysis = _tiny_message(facts_meta[activity_id]["activity"])
        plan_delta_note = ""
    if "EXCURSIONS above" not in hr_note:
        # No per-excursion table was computed: a "the climbs don't explain it" verdict is unevidenced.
        analysis = "\n".join(
            ln for ln in analysis.split("\n")
            if not _TERRAIN_DENIAL.search(ln)).strip()
    already_discussed = analysis.lower().startswith("discussed")
    if not already_discussed and not tiny and analysis and analysis != "none":
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
    if tiny:
        ack_prefix, ack_keys = "", []      # no praise ahead of "was it an accidental start?"
    ack_sent = False
    debrief_ok = True

    if already_discussed:
        # Chat already covered this activity — stay silent, but a durability
        # flag is new insight the chat can't have had (it's computed here).
        note, flagged = _run_durability_note(slug, activity_id)
        if flagged:
            # W1: if there is an acknowledgement it leads, and the flag still
            # lands at full strength immediately after. Praise-then-finding is
            # §8.5 move 0; the §8.4 sandwich is the other order.
            debrief_ok = _notify(f"*New activity*\n\n{ack_prefix}{note.lstrip()}"
                    if ack_prefix else f"*New activity*{note}", chat_id, slug=slug)
            ack_sent = bool(ack_prefix) and debrief_ok
    elif analysis and analysis != "none":
        if not tiny:
            analysis += _run_durability_note(slug, activity_id)[0]
        debrief_ok = _notify(f"*New activity*\n\n{ack_prefix}{analysis}", chat_id, slug=slug)
        ack_sent = bool(ack_prefix) and debrief_ok

    # Marked only once it has actually been said — an unsent occurrence must
    # stay available, or the one message §8.3 exists to produce is silently
    # spent. Idempotent, so a re-run cannot double-fire.
    if ack_sent and ack_keys:
        try:
            ack_lib.mark_fired_in(BASE / "athletes" / slug, ack_keys)
        except Exception as e:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] acknowledgement "
                  f"state not written: {e}", file=sys.stderr)

    # Only advance last_id/notified_at once the debrief has actually gone out
    # (or there was never one to send). A failed send leaves last_id behind, so
    # state.get("last_id") no longer claims this activity was debriefed when
    # the Telegram message never arrived (i183196403).
    if debrief_ok:
        state["last_id"] = activity_id
        state["notified_at"] = datetime.now().isoformat()
        save_state(state, state_file)
    else:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] debrief send failed for "
              f"activity {activity_id} — leaving un-debriefed for retry", file=sys.stderr)

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
            and new_entry.get("sport") == "Ride" and coaching_prefs.fuelling_on(slug):
        _kb_kinds.append(ask_gate.NUTRITION)     # matches _quick_log_keyboard's carb row
    _kb_known = bool(new_entry) and all(
        ask_gate.already_answered(new_entry, _k, history=_load_history(slug),
                                  since=ask_gate.entry_synced_at(new_entry))
        for _k in _kb_kinds)
    if (new_entry and not already_discussed and not _kb_known and not tiny
            and not _strength_with_hr(new_entry) and not _too_short_to_ask(new_entry)
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
        # The card IS the ask: it stays in the chat to tap, so the 2-hour reminder must not
        # ask the same thing again in words (Jamie, 9 Oct 2026: "stop asking for the RPE
        # twice" - every debrief got the card and then "RPE for the run? (1-10)").
        _nudged = set(state.get("nudged_ids") or [])
        _nudged.add(str(activity_id))
        state["nudged_ids"] = list(_nudged)
        save_state(state, state_file)

    _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file, slug=slug)

    # Zone-spotting: if Claude detected a threshold test, prompt for confirmation.
    # Not for an athlete with a baseline block: their tests are read in code by
    # _hr_quality_pass and confirmed through the block's own keyboard, and a second
    # keyboard would write local files only (this path never reaches Intervals.icu).
    if test_result_raw and test_result_raw != "none" and baseline_lib.load(slug) is None:
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
    if not tiny:
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
