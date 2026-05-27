#!/usr/bin/env python3
"""Morning briefing — polls every 15 min from 06:00–09:00 via VM crontab. Sends once per athlete per day, after Garmin sleep data syncs."""
import json, subprocess, sys
from datetime import datetime
from datetime import date, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "telegram"))
from coaching_levels import level_block as _level_block

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name, race_name, race_date, days_to_race, injuries, recovery=None, wellness_line=None, heat_protocol=True, coaching_level="mid"):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    injury_question = ""
    if injuries:
        injury_question = (
            "- If a run is planned today AND ankle.pain_next_morning in current-state.json is >0 "
            "(do NOT use pain_during — that is a run-specific score, not a morning score): "
            "ask \"Injury pain score before heading out? (0-10)\"\n"
        )
    injury_question += "- Else if no weight reading in the last 3 days: ask \"Weight this morning?\""

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
        )

    wellness_block = (
        f"\n## Today's wellness (pre-fetched)\n{wellness_line}\n"
        if wellness_line else
        "\n## Today's wellness\nNot yet synced — omit Sleep, HRV, and RHR from the card entirely.\n"
    )

    return f"""\
You are generating the morning briefing for {first_name}'s training day.

{_level_block(coaching_level)}
{recovery_block}{wellness_block}
Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read:
- ClaudeCoach/athletes/{slug}/current-state.md (open actions, watchdog flags)
- ClaudeCoach/athletes/{slug}/current-state.json (weight_readings, injury pain scores)
{"- ClaudeCoach/athletes/" + slug + "/heat-log.json (count entries in current ISO week to get sessions_this_week)" if heat_protocol else ""}
- ClaudeCoach/athletes/{slug}/session-log.json — only if today's planned event is a Ride or Brick >90 min: extract the last 4 entries with sport Ride/GravelRide/Brick, duration_min ≥ 90, and nutrition_g_carb set. Compute each g_per_hr and the avg.

Step 3 — Determine ONE question to ask (or none):
{injury_question}
- Else: no question

Step 4 — Output the morning card in Telegram Markdown (no preamble, no sign-off):

Use the recovery score and signals ONLY to decide what to flag — do NOT show the score, label, HRV ratio, or any internal metric to the athlete. Write like a coach sending a morning text, not a dashboard.

*Good morning — [Day date, e.g. Sat 9 May]*

*Today:* [session name] — [duration] min[, ~[TSS] TSS if available]

[Form line — only include if notable:
  · Form < −20: ⚠️ Heavy load today — keep intensity in check
  · Form > +10: 🟢 Fresh legs — good day for quality work
  · Form −1 to −20: omit entirely, that's normal training]
[If recovery ORANGE or RED: ⚠️ [one plain-English sentence on what to do differently — no scores]]
[If watchdog flag active: ⚠️ [flag in plain English — one line]]
[If today's session is Ride or Brick >90 min: 🍌 Nutrition — target [min(avg+10,90)]g/hr · eat at 15 min then every 25 min]
[If any travel block, race, or constraint from current-state.md "Travel & training blocks" starts within 5 days: 📌 [constraint name] in [N] days — [one-line impact]]
[If open action is due within 3 days: 📌 [action] due [date]]
{"[If today ≥ 2026-05-15 AND sessions_this_week < 2 AND today is Wednesday or later: 🌡️ Heat bath due — [N] this week (target 2–3×)]" if heat_protocol else ""}

[Question if applicable — one line]

_{days_to_race} days to {race_name}_

Rules:
- Sleep/HRV/RHR: use ONLY the pre-fetched wellness line above — never infer or estimate values yourself.
- If no planned session: say "Rest day" and skip the Today line.
- Omit any section that has nothing to say — do not pad with dashes or "N/A".
- Never ask for subjective mood, fatigue, or motivation scores.
- The countdown line appears exactly once, at the end.
Wrap your entire output in <telegram> and </telegram> tags. Output nothing outside those tags — no preamble, no reasoning, no tool commentary."""


def notify(msg, chat_id):
    try:
        subprocess.run(
            ["python3", str(NOTIFY), "--chat-id", str(chat_id), msg],
            cwd=PROJECT_DIR, timeout=15,
        )
    except Exception:
        pass


def _send_morning_load_chart(chat_id, slug, wellness_rows):
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

        # Seed CTL/ATL from last wellness row
        seed_ctl = seed_atl = None
        if wellness_rows:
            w = wellness_rows[-1]
            seed_ctl = round(float(w.get("ctl") or 0), 1)
            seed_atl = round(float(w.get("atl") or 0), 1)

        # Historical TSB by date from already-fetched wellness
        tsb_by_date = {}
        for w in wellness_rows:
            d = (w.get("id") or "")[:10]
            if d:
                tsb_by_date[d] = round((w.get("ctl") or 0) - (w.get("atl") or 0), 1)

        # Completed activities by date
        acts_by_date = {}
        _sport_map = {
            "VirtualRide": "Ride", "GravelRide": "Ride", "MountainBikeRide": "Ride",
            "EBikeRide": "Ride", "Cycling": "Ride", "TrailRun": "Run",
            "VirtualRun": "Run", "OpenWaterSwim": "Swim",
            "WeightTraining": "Strength", "Workout": "Strength", "Elliptical": "Strength",
        }
        for act in (history_acts or []):
            d = (act.get("start_date_local") or "")[:10]
            if not d:
                continue
            sport = _sport_map.get(act.get("type", ""), act.get("type", "Other"))
            tss = round(float(act.get("icu_training_load") or 0), 1)
            dur = round((act.get("moving_time") or 0) / 60)
            acts_by_date.setdefault(d, []).append(
                {"sport": sport, "tss": tss, "dur": dur, "status": "completed"}
            )

        # Planned events by date (future only)
        plans_by_date = {}
        for ev in (events or []):
            d = (ev.get("start_date_local") or "")[:10]
            if not d or d <= today.isoformat():
                continue
            raw   = ev.get("type") or ev.get("category") or ""
            sport = _sport_map.get(raw, raw) or "Other"
            tss = round(float(ev.get("load_target") or ev.get("icu_training_load") or 0), 1)
            dur = round((ev.get("moving_time") or 0) / 60)
            plans_by_date.setdefault(d, []).append(
                {"sport": sport, "tss": tss, "dur": dur, "status": "planned"}
            )

        days = []
        for i in range(-8, 8):
            d = today + timedelta(days=i)
            d_str = d.isoformat()
            is_future = d > today
            days.append({
                "date": d_str,
                "tsb": None if is_future else tsb_by_date.get(d_str),
                "activities": plans_by_date.get(d_str, []) if is_future else acts_by_date.get(d_str, []),
            })

        payload = {
            "today": today.strftime("%m-%d"),
            "seed_ctl": seed_ctl,
            "seed_atl": seed_atl,
            "days": days,
        }
        png = _charts.load_chart(payload)
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
    heat_protocol = profile.get("heat_protocol", True)

    try:
        rd = date.fromisoformat(race_date_str) if race_date_str else None
        days_to_race = (rd - date.today()).days if rd else "?"
    except Exception:
        days_to_race = "?"

    # Pre-compute recovery score and extract today's wellness values directly
    recovery = None
    wellness_line = None
    wellness_rows = []
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
        hrv_t, hrv_b, tsb, sleep = rs._parse_wellness(wellness_rows)
        pain = 0
        state_f = adir / "current-state.json"
        if state_f.exists():
            ankle = json.loads(state_f.read_text()).get("ankle", {})
            # Use today's resting pain if logged today; otherwise use next-morning pain.
            # Never use pain_during — that's an in-run score and is not morning-relevant.
            resting_today = ankle.get("pain_today_resting_date") == today_str
            pain = (ankle.get("pain_today_resting", 0) if resting_today
                    else ankle.get("pain_next_morning", 0)) or 0
        recovery = rs.compute(hrv_t, hrv_b, tsb, sleep, pain)
    except Exception:
        pass  # score is optional — morning card still sends without it

    # Wait for Garmin sync only if the athlete has a sleep device and it hasn't synced yet.
    # Athletes with no sleep device (sleepSecs always null) send immediately.
    if wellness_line is None and has_sleep_device and datetime.now().hour < 9:
        print(f"[{slug}] wellness not yet synced — will retry", file=sys.stderr)
        return

    coaching_level = profile.get("coaching_level", "mid")
    prompt = _build_prompt(slug, first_name, race_name, race_date_str, days_to_race, injuries, recovery,
                           wellness_line=wellness_line, heat_protocol=heat_protocol,
                           coaching_level=coaching_level)

    with open(log_file, "a") as lf:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS, "--model", "claude-sonnet-4-6"],
            stdout=subprocess.PIPE, stderr=lf, text=True,
            cwd=PROJECT_DIR, timeout=180,
        )

    raw = (result.stdout or "").strip()
    import re as _re
    m = _re.search(r"<telegram>(.*?)</telegram>", raw, _re.DOTALL)
    output = m.group(1).strip() if m else ""
    if output:
        notify(output, chat_id)
        _send_morning_load_chart(chat_id, slug, wellness_rows)
    # Write sentinel regardless — if Claude ran without error, don't retry even if output was empty
    if result.returncode == 0:
        sentinel.touch()


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] morning-checkin starting", file=sys.stderr)
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


if __name__ == "__main__":
    main()
