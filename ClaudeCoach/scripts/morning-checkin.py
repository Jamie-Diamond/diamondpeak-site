#!/usr/bin/env python3
"""Morning briefing — polls every 15 min from 06:00–09:00 via VM crontab. Sends once per athlete per day, after Garmin sleep data syncs."""
import fcntl, json, subprocess, sys, time
from datetime import datetime
from datetime import date, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOCK_FILE       = BASE / ".morning_checkin.lock"  # prevents overlapping cron runs double-sending
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "telegram"))
sys.path.insert(0, str(BASE / "ironman-analysis"))
import claude_call
from coaching_levels import level_block as _level_block
import illness as illness_lib   # structured illness/compromised flag (surfacing gate)
import weekly_availability     # per-week declared hours (Sunday ask + resolver)
from primitives.planned_tss import planned_sessions_block
from primitives.nutrition import fuel_target, recent_avg_g_hr
import ops_log
import heat as heat_lib
import menstrual as menstrual_lib
import races as races_lib

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name, race_name, race_date, days_to_race, injuries, recovery=None, wellness_line=None, heat_protocol=True, coaching_level="mid", planned_block="", cycle=None, fuel_target_g_hr=60, nutrition_race=90, heat_accl_pct=None, heat_accl_trend="", long_run_cap_km=None, wellness_finalized=True, ask_morning_pain=False, race_block=""):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    cycle_block = ""
    cycle_card_line = ""
    cycle_question = ""
    if cycle:
        if cycle.get("phase"):
            cycle_block = (
                f"\n## Menstrual cycle (pre-computed — authoritative)\n"
                f"Cycle day {cycle['day']} — {cycle['phase']} phase: {cycle['cue']}\n"
            )
        if cycle.get("phase") in ("menstrual", "luteal"):
            cycle_card_line = (
                "[🌸 One plain-English line from the cycle block above — what today may "
                "feel like and how to approach targets. No cycle-day numbers, no jargon.]\n"
            )
        if cycle.get("overdue"):
            cycle_question = (
                "- FIRST PRIORITY question (overrides the ones below): the cycle anchor is "
                "overdue — ask \"Has your period started? Reply 'period started' (or "
                "'period started yesterday') so cycle-aware planning stays accurate.\"\n"
            )

    morning_pain_question = ""
    if injuries and ask_morning_pain:
        morning_pain_question = (
            "- FIRST PRIORITY (overrides the questions below): ask \"Ankle score this morning? (0-10)\" "
            "— record the answer as injury_pain_next_morning on yesterday's run in session-log.json.\n"
        )
    injury_question = ""
    if injuries and not ask_morning_pain:
        # Suppressed when the morning-score question fires: at most ONE ankle question per card.
        injury_question = (
            "- If a run is planned today AND ankle.pain_next_morning in current-state.json is >0 "
            "(do NOT use pain_during — that is a run-specific score, not a morning score): "
            "ask \"Injury pain score before heading out? (0-10)\"\n"
        )
    injury_question += "- Else if no weight reading in the last 3 days: ask \"Weight this morning?\""
    injury_question = cycle_question + morning_pain_question + injury_question

    injuries_note = (
        "; ".join(
            f"{i.get('location','unknown')}: {i.get('description','')}"
            + (f" — {i.get('protocol','')}" if i.get("protocol") else "")
            for i in injuries
        )
        if injuries else "None"
    )

    recovery_block = ""
    if recovery:
        score  = recovery.get("score", "?")
        label  = recovery.get("label", "?")
        rec    = recovery.get("recommendation", "")
        sigs   = recovery.get("signals", {})
        hrv_r  = sigs.get("hrv",   {}).get("ratio")
        tsb_v  = sigs.get("tsb",   {}).get("value")
        slp_v  = sigs.get("sleep", {}).get("value")
        pain_v = sigs.get("pain",  {}).get("value")
        parts  = []
        if hrv_r  is not None: parts.append(f"HRV ratio {hrv_r:.2f}")
        if tsb_v  is not None: parts.append(f"Form {tsb_v:+.1f}")
        if slp_v  is not None: parts.append(f"sleep {slp_v:.1f}h")
        if pain_v is not None and pain_v > 0: parts.append(f"pain {pain_v}/10")
        recovery_block = (
            f"\n## Recovery score (pre-computed)\n"
            f"Score: {score}/100 — {label}. {rec}\n"
            f"Signals: {', '.join(parts) if parts else 'no data'}.\n"
            f"Use this to modulate session prescription: GREEN = train as planned; "
            f"AMBER = note and monitor; ORANGE = reduce intensity or volume; RED = flag for easy day.\n"
            + ("Today's Form is still recalculating on Intervals.icu and can shift several "
               "points as activities finish syncing — treat any Form-based flag as provisional, "
               "don't state today's Fitness/Fatigue/Form as final.\n"
               if tsb_v is not None and not wellness_finalized else "")
        )

    wellness_block = (
        f"\n## Today's wellness (pre-fetched)\n{wellness_line}\n"
        if wellness_line else
        "\n## Today's wellness\nNot yet synced — omit Sleep, HRV, and RHR from the card entirely.\n"
    )

    planned_section = (
        f"\n## Today's planned sessions (pre-computed — authoritative)\n{planned_block}\n"
        if planned_block else ""
    )

    heat_block = ""
    heat_card_line = ""
    if heat_protocol:
        # The acclimation score is decision-support for the model, never card copy:
        # §6.2 is explicit that the card carries no score, label, ratio or internal
        # metric, and a bare "62% ↑" is unrankable by the athlete anyway (R5). The
        # actionable half — bath due, N this week against a 2–3x target — is the
        # whole message, so the card line is the same whether or not a score exists.
        if heat_accl_pct is not None:
            heat_block = (
                f"\n## Heat acclimation (pre-computed — authoritative)\n"
                f"Score: {heat_accl_pct}%{heat_accl_trend}. Use this ONLY to decide whether to "
                f"flag heat work — do NOT show the score, the percentage or the trend arrow to "
                f"the athlete, and do not recompute it.\n"
            )
        heat_card_line = ("[If sessions_this_week < 2 AND today is Wednesday or later: "
                          "🌡️ Heat bath due — [N] this week (target 2–3×)]")

    long_run_cap_block = ""
    if long_run_cap_km is not None:
        long_run_cap_block = (
            f"\n## Long-run cap (pre-computed — authoritative)\n"
            f"Ceiling: {long_run_cap_km:.1f} km — do not reference or target a distance above this for today's long run.\n"
        )

    return f"""\
You are generating the morning briefing for {first_name}'s training day.

{_level_block(coaching_level)}
{illness_lib.prompt_block(slug, first_name=first_name)}
{recovery_block}{wellness_block}{cycle_block}{planned_section}{race_block}{long_run_cap_block}{heat_block}
Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read:
- ClaudeCoach/athletes/_shared/persistent-rules.md (GLOBAL coaching rules - apply to every athlete)
- ClaudeCoach/athletes/{slug}/persistent-rules.md (permanent coaching rules — these override defaults and MUST be followed)
- ClaudeCoach/athletes/{slug}/current-state.md (open actions, watchdog flags — only surface flags dated within the last 3 days)
- ClaudeCoach/athletes/{slug}/daily-prescription-latest.md — the 05:00 prescription check (no longer messaged directly). Use it ONLY if its date line is today. Key points to carry into the card: whether today's session is GO as planned or was modified/swapped (and the one-line reason). Ignore it if dated earlier than today.
- ClaudeCoach/athletes/{slug}/current-state.json (weight_readings, injury pain scores)
{"- ClaudeCoach/athletes/" + slug + "/heat-log.json (count entries in current ISO week to get sessions_this_week)" if heat_protocol else ""}
- ClaudeCoach/athletes/{slug}/session-log.json — only if today's planned event is a Ride or Brick >90 min: extract the last 4 entries with sport Ride/GravelRide/Brick, duration_min ≥ 90, and nutrition_g_carb set. Compute each g_per_hr and the avg.

Step 3 — Determine ONE question to ask (or none):
{injury_question}
- Else: no question

Step 4 — Output the morning card in Telegram Markdown (no preamble, no sign-off):

Use the recovery score and signals ONLY to decide what to flag — do NOT show the score, label, HRV ratio, or any internal metric to the athlete. Write like a coach sending a morning text, not a dashboard.

*Good morning — [Day date, e.g. Sat 9 May]*

*Today:* [session name] — [duration] min · [Load] Load
  Name, duration and Load come ONLY from the pre-computed planned-sessions block above — copy them
  verbatim. NEVER estimate, recompute, or round Load yourself. If that block is absent, omit the
  Load part entirely rather than guessing.

[Form line — only include if notable:
  · Form < −20: ⚠️ Heavy load today — keep intensity in check
  · Form > +10: 🟢 Fresh legs — good day for quality work
  · Form −1 to −20: omit entirely, that's normal training]
[If recovery ORANGE or RED: ⚠️ [one plain-English sentence on what to do differently — no scores]]
[If watchdog flag active: ⚠️ [flag in plain English — one line]]
[If the 05:00 prescription check modified or swapped today's session: 🔁 [what changed and why — one plain line, e.g. "Swapped to easy spin — HRV low". If it confirmed the session as planned, say nothing]]
{cycle_card_line}
[If today's session is Ride or Brick >90 min: 🍌 Nutrition — target {fuel_target_g_hr}g/hr (progress toward {nutrition_race}g/hr race target) · eat at 15 min then every 25 min]
[If any travel block, race, or constraint from current-state.md "Travel & training blocks" starts within 5 days: 📌 [constraint name] in [N] days — [one-line impact]]
[If open action is due within 3 days: 📌 [action] due [date]]
{heat_card_line}

[Question if applicable — one line]

_{days_to_race} days to {race_name}_

Rules:
- Sleep/HRV/RHR: use ONLY the pre-fetched wellness line above — never infer or estimate values yourself.
- If no planned session: say "Rest day" and skip the Today line.
- Omit any section that has nothing to say — do not pad with dashes or "N/A".
- Never ask for subjective mood, fatigue, or motivation scores.
- The countdown line appears exactly once, at the end.
Wrap your entire output in <telegram> and </telegram> tags. Output nothing outside those tags — no preamble, no reasoning, no tool commentary."""


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


def notify(msg, chat_id, slug=""):
    """Send via notify.py, retry once. Returns True only if the send succeeded —
    the caller uses this to decide whether to write the sent-today sentinel."""
    for _attempt in (1, 2):
        try:
            # --no-history: this script appends to history itself after the send
            r = subprocess.run(
                ["python3", str(NOTIFY), "--no-history", "--chat-id", str(chat_id), msg],
                cwd=PROJECT_DIR, timeout=15,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    ops_log.alert("morning-checkin", "Telegram send failed after retry — card not delivered",
                  athlete=slug)
    return False


_CHART_SPORT_MAP = {
    "VirtualRide": "Ride", "GravelRide": "Ride", "MountainBikeRide": "Ride",
    "EBikeRide": "Ride", "Cycling": "Ride", "TrailRun": "Run",
    "VirtualRun": "Run", "OpenWaterSwim": "Swim",
    "WeightTraining": "Strength", "Workout": "Strength", "Elliptical": "Strength",
}


def _build_load_chart_payload(today, wellness_rows, history_acts, events):
    """Pure payload builder for the morning load chart (±8 days) — extracted from
    the send path so the day construction is testable. The today-bar rule lives
    here: today shows completed activities PLUS planned sessions whose sport has
    not been completed yet (at a 06:30 send nothing is completed, which is why an
    earlier completed-only version rendered today permanently empty)."""
    seed_ctl = seed_atl = None
    if wellness_rows:
        w = wellness_rows[-1]
        seed_ctl = round(float(w.get("ctl") or 0), 1)
        seed_atl = round(float(w.get("atl") or 0), 1)

    tsb_by_date = {}
    for w in wellness_rows or []:
        d = (w.get("id") or "")[:10]
        if d:
            tsb_by_date[d] = round((w.get("ctl") or 0) - (w.get("atl") or 0), 1)

    acts_by_date = {}
    for act in (history_acts or []):
        d = (act.get("start_date_local") or "")[:10]
        if not d:
            continue
        sport = _CHART_SPORT_MAP.get(act.get("type", ""), act.get("type", "Other"))
        tss = round(float(act.get("icu_training_load") or 0), 1)
        dur = round((act.get("moving_time") or 0) / 60)
        acts_by_date.setdefault(d, []).append(
            {"sport": sport, "tss": tss, "dur": dur, "status": "completed"}
        )

    # Planned events by date (today and future)
    plans_by_date = {}
    for ev in (events or []):
        d = (ev.get("start_date_local") or "")[:10]
        if not d or d < today.isoformat():
            continue
        raw   = ev.get("type") or ev.get("category") or ""
        sport = _CHART_SPORT_MAP.get(raw, raw) or "Other"
        tss = round(float(ev.get("icu_training_load") or ev.get("load_target") or 0), 1)
        dur = round((ev.get("moving_time") or 0) / 60)
        plans_by_date.setdefault(d, []).append(
            {"sport": sport, "tss": tss, "dur": dur, "status": "planned"}
        )

    days = []
    for i in range(-8, 8):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        is_future = d > today
        if is_future:
            acts = plans_by_date.get(d_str, [])
        elif d == today:
            done = acts_by_date.get(d_str, [])
            done_sports = {a["sport"] for a in done}
            acts = done + [p for p in plans_by_date.get(d_str, [])
                           if p["sport"] not in done_sports]
        else:
            acts = acts_by_date.get(d_str, [])
        days.append({
            "date": d_str,
            "tsb": None if is_future else tsb_by_date.get(d_str),
            "activities": acts,
        })

    return {
        "today": today.strftime("%m-%d"),
        "seed_ctl": seed_ctl,
        "seed_atl": seed_atl,
        "days": days,
    }


def _send_morning_load_chart(chat_id, slug, wellness_rows, coaching_level="mid"):
    """Generate and send the training load chart (±8 days) as part of the morning brief."""
    try:
        import charts as _charts
        from icu_api import IcuClient

        athletes_cfg = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes_cfg[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])

        today = date.today()
        end_date = (today + timedelta(days=8)).isoformat()

        history_acts, events = client.fetch_all(
            ("get_training_history", 10),
            ("get_events", today.isoformat(), end_date),
        )

        payload = _build_load_chart_payload(today, wellness_rows, history_acts, events)
        png = _charts.load_chart(payload, coaching_level=coaching_level)
        if png:
            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tf.write(png)
                tmp_path = tf.name
            try:
                subprocess.run(
                    ["python3", str(NOTIFY), "--chat-id", str(chat_id), "--photo", tmp_path],
                    cwd=PROJECT_DIR, timeout=60,
                )
            finally:
                os.unlink(tmp_path)
    except Exception as e:
        print(f"[morning chart] {e}", file=sys.stderr)


def _race_focus(slug, profile, athlete_cfg):
    """Focus points for the pre-race message: a curated profile.json race_focus list if
    there is one, otherwise derived from the athlete's own standing rules and logged
    fuelling (lib/races.py derive_focus). Every failure mode degrades to NO focus points
    rather than to a guess — an empty list simply drops that sentence from the message."""
    try:
        rules_f = BASE / f"athletes/{slug}/persistent-rules.md"
        rules = rules_f.read_text() if rules_f.exists() else ""
        avg = None
        try:
            log_f = BASE / f"athletes/{slug}/session-log.json"
            if log_f.exists():
                avg = recent_avg_g_hr(json.loads(log_f.read_text()))
        except Exception:
            pass
        return races_lib.focus_for(profile, rules, avg,
                                   athlete_cfg.get("nutrition_target_g_hr"))
    except Exception as exc:
        print(f"[{slug}] race focus derivation failed: {exc}", file=sys.stderr)
        return []


def _block_fact(athlete_cfg, today):
    """One true, already-computed fact about the work behind the race (§8.6 "name the
    work behind it"). Weeks since plan_start is the only such fact available for every
    athlete without inventing anything; no plan_start means no claim is made."""
    ps = athlete_cfg.get("plan_start")
    if not ps:
        return ""
    try:
        weeks = (today - date.fromisoformat(ps)).days // 7
    except Exception:
        return ""
    if weeks < 2:
        return ""
    return f"You have {weeks} weeks of work behind you for this one"


def run_athlete(slug, athlete_cfg):
    adir = BASE / f"athletes/{slug}"
    chat_id = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "morning-checkin.log"
    if not chat_id:
        print(f"[{slug}] SKIP: no chat_id in athletes.json", file=sys.stderr)
        return

    today_str = date.today().isoformat()

    # Sentinel: skip if card already sent today
    sentinel = LOG_DIR / f"morning-sent-{slug}-{today_str}.flag"
    if sentinel.exists():
        return

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    race_name = profile.get("race_name") or athlete_cfg.get("race_name", "your race")
    race_date_str = profile.get("race_date") or athlete_cfg.get("race_date", "")
    injuries = profile.get("injuries", [])
    # Morning heat nudges only once the formal race−4wk block has begun; before
    # that the watchdog's maintenance-dose check owns heat visibility.
    # "surface", not "in_protocol_window": profile heat_silent suppresses the
    # proactive nudge without touching the dose model or the score.
    heat_protocol = heat_lib.state(slug, profile)["surface"]

    # Pre-compute 0–100% acclimation score so the card shows it without Claude guessing.
    heat_accl_pct = None
    heat_accl_trend = ""
    if heat_protocol and (adir / "heat-log.json").exists():
        try:
            _score_now = heat_lib.acclimation_score(slug)
            _score_7d  = heat_lib.acclimation_score(slug, date.today() - timedelta(days=7))
            _delta = _score_now - _score_7d
            heat_accl_pct = round(_score_now)
            if abs(_delta) > 5:
                heat_accl_trend = " ↑" if _delta > 0 else " ↓"
        except Exception:
            pass

    # Race-aware branch. Until now `race_date` was structured data that nothing
    # branched on: on race morning this function rendered the ordinary training card
    # with a 0 in the countdown (docs/tone-of-voice-guide.md §8.6/§8.7). On race day
    # and the days just after, the card is REPLACED by a deterministic message rather
    # than generated — §8.6's first rule is "do not introduce anything new", and a
    # generative prompt on race morning is precisely how a new pacing target arrives
    # at 06:30. Race-week and race-eve stay generative and only get a prompt block,
    # because those days still carry a real session.
    _races = races_lib.load_races(slug, {slug: athlete_cfg})
    _phase = races_lib.race_phase(_races, date.today())
    if _phase["phase"] in ("race_day", "race_completed"):
        if _phase["phase"] == "race_day":
            msg = races_lib.render_pre_race(
                _phase["race"], first_name, phase="race_day",
                block_fact=_block_fact(athlete_cfg, date.today()),
                # Focus points must already be in the agreed race plan (§8.6: "every one
                # must be something already trained and already agreed"), so they are
                # READ from the profile and never generated. No athlete has this key yet,
                # so nothing renders until someone curates it.
                focus=_race_focus(slug, profile, athlete_cfg))
        else:
            # good_day is left unknown on purpose: nothing in the system yet tells us
            # whether the race went well, and §8.6 splits the good day from the bad one
            # before writing a word. Unknown means ASK, not assume.
            msg = races_lib.render_post_race(_phase["race"], first_name, good_day=None)
        if notify(msg, chat_id, slug=slug):
            sentinel.touch()
            if _phase["phase"] == "race_completed":
                # Mark AFTER a successful send so a failed send retries next run, and so
                # the question is asked once rather than every morning in the window.
                try:
                    races_lib.mark_post_race_sent(slug, _phase["race"]["date"])
                except Exception as exc:
                    print(f"[{slug}] could not mark post-race sent: {exc}", file=sys.stderr)
            ops_log.record_run("morning-checkin", athlete=slug, ok=True,
                               detail=f"{_phase['phase']} message sent")
            try:
                _log_to_history(slug, msg)
            except Exception:
                pass
        return

    try:
        rd = date.fromisoformat(race_date_str) if race_date_str else None
        days_to_race = (rd - date.today()).days if rd else "?"
    except Exception:
        days_to_race = "?"

    # Pre-compute recovery score and extract today's wellness values directly
    recovery = None
    wellness_line = None
    wellness_rows = []
    wellness_finalized = True  # False = today's CTL/ATL/Form still recalculating on ICU
    has_sleep_device = False  # True if athlete has ever had sleep data
    try:
        from icu_api import IcuClient
        import recovery_score as rs
        athletes_cfg = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes_cfg[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        wellness_rows = client.get_wellness(8)
        has_sleep_device = any(r.get("sleepSecs") is not None for r in wellness_rows)
        for row in wellness_rows:
            if (row.get("id") or "")[:10] == today_str:
                wellness_finalized = row.get("wellness_finalized", True)
                break
        # ICU/Garmin stores last night's sleep under the WAKING day = today's date
        sleep_date = date.today().isoformat()
        print(f"[{slug}] querying sleep data for {sleep_date}", file=sys.stderr)
        for row in wellness_rows:
            if (row.get("id") or "")[:10] == sleep_date:
                sleep_secs = row.get("sleepSecs")
                hrv_val    = row.get("hrv") or row.get("hrvSdnn")
                rhr_val    = row.get("restingHR")
                # Only use the row if sleep has actually synced — RHR alone is not enough
                if sleep_secs is not None:
                    sleep_score = row.get("sleepScore")
                    steps_val   = row.get("steps")
                    parts = [f"Sleep: {sleep_secs/3600:.1f}h"]
                    if sleep_score is not None: parts.append(f"Score: {int(sleep_score)}")
                    if hrv_val    is not None: parts.append(f"HRV: {int(hrv_val)}")
                    if rhr_val    is not None: parts.append(f"RHR: {int(rhr_val)} bpm")
                    if steps_val  is not None: parts.append(f"Steps: {int(steps_val):,}")
                    wellness_line = " · ".join(parts)
                break
        if wellness_line is None:
            # Guard: if today's sleep hasn't synced yet, check whether yesterday has data
            # so we can warn rather than silently appear as "not yet synced"
            prev_day = (date.today() - timedelta(days=1)).isoformat()
            for row in wellness_rows:
                if (row.get("id") or "")[:10] == prev_day and row.get("sleepSecs") is not None:
                    print(
                        f"[{slug}] WARNING: no sleep data for {sleep_date}; "
                        f"stale data exists for {prev_day} — not using it",
                        file=sys.stderr,
                    )
                    break
        hrv_t, hrv_b, tsb, sleep, sleep_score = rs._parse_wellness(wellness_rows)
        pain = 0
        state_f = adir / "current-state.json"
        if state_f.exists():
            ankle = json.loads(state_f.read_text()).get("ankle", {})
            # Use today's resting pain if logged today; otherwise use next-morning pain.
            # Never use pain_during — that's an in-run score and is not morning-relevant.
            resting_today = ankle.get("pain_today_resting_date") == today_str
            pain = (ankle.get("pain_today_resting", 0) if resting_today
                    else ankle.get("pain_next_morning", 0)) or 0
        recovery = rs.compute(hrv_t, hrv_b, tsb, sleep, pain,
                              in_taper=rs.in_taper(slug), sleep_score=sleep_score)
    except Exception:
        pass  # score is optional — morning card still sends without it

    # Does yesterday's run stub still need the next-morning pain score?
    needs_morning_pain_ask = False
    if injuries:
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()
        try:
            cs_f = adir / "current-state.json"
            cs_data = json.loads(cs_f.read_text()) if cs_f.exists() else {}
            already_asked = cs_data.get("ankle", {}).get("pain_next_morning_asked_date") == today_str
            if not already_asked:
                sl_f = adir / "session-log.json"
                if sl_f.exists():
                    for e in json.loads(sl_f.read_text()):
                        if (e.get("date") == yesterday_str
                                and e.get("sport") in ("Run", "TrailRun", "VirtualRun")
                                and e.get("injury_pain_next_morning") is None):
                            needs_morning_pain_ask = True
                            break
        except Exception:
            pass

    # Wait for Garmin sync only if the athlete has a sleep device and it hasn't synced yet.
    # Athletes with no sleep device (sleepSecs always null) send immediately.
    if wellness_line is None and has_sleep_device and datetime.now().hour < 9:
        print(f"[{slug}] wellness not yet synced — will retry", file=sys.stderr)
        return

    # Pre-compute today's planned-session TSS in Python — the 11 Jun card guessed
    # "~35 TSS" for a swim whose plan event carried load_target=60.
    planned_block = ""
    _events = []  # kept for long-run cap computation below
    try:
        from icu_api import IcuClient as _Icu
        _a = json.loads(ATHLETES_CONFIG.read_text())[slug]
        _events = _Icu(_a["icu_athlete_id"], _a["icu_api_key"]).get_events(today_str, today_str)
        planned_block = planned_sessions_block(_events)
    except Exception as exc:
        print(f"[{slug}] planned-TSS prefetch failed: {exc}", file=sys.stderr)

    coaching_level = profile.get("coaching_level", "mid")

    # Menstrual-cycle phase (tracking athletes only) — same wellness rows give the
    # same-day ICU override if the athlete also logs the phase in intervals.icu.
    cycle = None
    try:
        cycle = menstrual_lib.phase_for(slug, profile=profile, wellness=wellness_rows)
    except Exception:
        pass

    # Deterministic fuelling target (gap-closing ramp, never the old avg+10 guess).
    try:
        _sl = json.loads((adir / "session-log.json").read_text()) if (adir / "session-log.json").exists() else []
        _fuel_target_g_hr = fuel_target(recent_avg_g_hr(_sl), int(athlete_cfg.get("nutrition_target_g_hr") or 90))
    except Exception:
        _fuel_target_g_hr = int(athlete_cfg.get("nutrition_alert_threshold_g_hr") or 60)

    # Pre-compute long-run distance cap — only fires when today's calendar has a
    # long-run WORKOUT event. See lib/progression.py for the cap rule itself.
    _long_run_cap_km = None
    try:
        from primitives.modulation import classify_session_type as _lr_classify
        from progression import long_run_cap_km as _lr_cap
        _lr_sl_path = adir / "session-log.json"
        _lr_sl = json.loads(_lr_sl_path.read_text()) if _lr_sl_path.exists() else []
        _long_run_cap_km = _lr_cap(_events, _lr_sl, _lr_classify)
    except Exception as exc:
        print(f"[{slug}] long-run cap pre-compute failed: {exc}", file=sys.stderr)

    prompt = _build_prompt(slug, first_name, race_name, race_date_str, days_to_race, injuries, recovery,
                           wellness_line=wellness_line, heat_protocol=heat_protocol,
                           coaching_level=coaching_level, planned_block=planned_block,
                           race_block=races_lib.prompt_block(_races, date.today()),
                           cycle=cycle,
                           fuel_target_g_hr=_fuel_target_g_hr,
                           nutrition_race=int(athlete_cfg.get("nutrition_target_g_hr") or 90),
                           ask_morning_pain=needs_morning_pain_ask,
                           heat_accl_pct=heat_accl_pct, heat_accl_trend=heat_accl_trend,
                           long_run_cap_km=_long_run_cap_km,
                           wellness_finalized=wellness_finalized)

    with open(log_file, "a") as lf:
        result = claude_call.run_claude(
            prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
            allowed_tools=TOOLS, stderr=lf, cwd=PROJECT_DIR, timeout=180, label=slug,
        )

    raw = (result.stdout or "").strip()
    import re as _re
    m = _re.search(r"<telegram>(.*?)</telegram>", raw, _re.DOTALL)
    output = m.group(1).strip() if m else ""

    # Atomic sentinel claim: write BEFORE notifying so a second cron instance
    # that slips past the 20-min process lock (slow multi-athlete run) can never
    # also deliver. If Claude failed, skip — allow retry on next poll.
    if result.returncode == 0:
        _sl_lock_path = LOG_DIR / f"morning-sent-{slug}-{today_str}.flag.lock"
        with open(_sl_lock_path, "w") as _sl_fd:
            fcntl.flock(_sl_fd, fcntl.LOCK_EX)
            if sentinel.exists():
                return
            sentinel.touch()

    # WEEKLY HOURS ASK (Sunday only) — appended DETERMINISTICALLY to the card rather
    # than asked for in the prompt, because the Sunday build at 18:00 derives the
    # week's Load ceiling from the answer and a dropped question means the build
    # silently falls back to the standing config figure. weekday() == 6 is Sunday;
    # the week asked about is tomorrow's Monday. weekly_availability owns the copy,
    # the already-answered gate and the illness gate. The existing per-athlete daily
    # sentinel already guarantees once-per-day, so this needs no idempotence of its
    # own. Zero extra pushes: the morning card was going out anyway.
    if output and date.today().weekday() == 6:
        try:
            _ask = weekly_availability.sunday_hours_ask(
                slug, date.today() + timedelta(days=1), coaching_level=coaching_level)
            if _ask:
                output = f"{output}\n\n{_ask}"
        except Exception as _e:
            # The card must still send if the ask cannot be built — but it must NOT do so
            # silently. This is the one question the Sunday 18:00 ceiling derives from, so
            # a swallowed failure here means the build quietly falls back to the standing
            # config figure and nobody finds out until the plan is already pushed. Alert,
            # then carry on. (ops_log is log-only since 27 Jul; the evening digest picks
            # it up, which is well inside the nine-hour answer window.)
            ops_log.alert("morning-checkin",
                          f"Sunday weekly-hours ask could NOT be built "
                          f"({type(_e).__name__}: {_e}) — the 18:00 build will fall back "
                          f"to profile.max_hours_per_week for this athlete",
                          athlete=slug)

    sent = False
    if output:
        sent = notify(output, chat_id, slug=slug)
        if sent:
            ops_log.record_run("morning-checkin", athlete=slug, ok=True, detail="card sent")
            try:
                _log_to_history(slug, output)
            except Exception:
                pass
            _send_morning_load_chart(chat_id, slug, wellness_rows, coaching_level=coaching_level)
    elif result.returncode == 0:
        ops_log.alert("morning-checkin", "claude produced no card output", athlete=slug)
    else:
        ops_log.alert("morning-checkin",
                      f"claude CLI exit {result.returncode} — no card generated", athlete=slug)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] morning-checkin starting", file=sys.stderr)

    # Lock: morning-checkin had no lock, so a slow 06:00 run still going when 06:30
    # fired could pass the per-athlete sentinel check and BOTH send — the 17 Jun
    # duplicate-card bug. Skip if another run is active (stale after 20 min).
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 1200:
        print(f"[{ts}] another morning-checkin run is active — skipping", file=sys.stderr)
        return
    LOCK_FILE.touch()
    try:
        try:
            athletes = json.loads(ATHLETES_CONFIG.read_text())
        except Exception as e:
            print(f"[{ts}] Failed to load athletes config: {e}", file=sys.stderr)
            sys.exit(1)

        for slug, cfg in athletes.items():
            if not cfg.get("active", True):
                continue
            try:
                run_athlete(slug, cfg)
            except Exception as exc:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] morning-checkin error: {exc}", file=sys.stderr)
                ops_log.alert("morning-checkin", f"exception: {exc}", athlete=slug)
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
