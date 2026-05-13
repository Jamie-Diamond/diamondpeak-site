#!/usr/bin/env python3
"""
Check for new activities and send a brief analysis to Telegram.
Run every 15 min via cron. Loops over all active athletes. Skips if already running.
"""
import json, ssl, subprocess, sys, time, urllib.request
from datetime import datetime, date
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
LOCK_FILE       = BASE / ".activity_watcher.lock"
NOTIFY          = BASE / "telegram/notify.py"
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
ATHLETES_CONFIG = BASE / "config/athletes.json"
TG_CONFIG       = BASE / "telegram/config.json"

TOOLS = "Read,Write,Bash"


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

    return {"inline_keyboard": rows}


def _build_prompt(slug, first_name, ftp, injuries):
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

    run_injury_ask = (
        "- Run: Line 1 = distance + avg GAP pace. Line 2 = HR cap adherence (cap 150 bpm)."
        " Line 3 = \"Injury pain score during and this morning? (0-10)\""
        if injuries else
        "- Run: Line 1 = distance + avg GAP pace. Line 2 = HR zone / feel check."
        " Line 3 = \"RPE and how did it feel?\""
    )

    return f"""\
Check for new activities for {first_name} and stub them into the session log.

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 3
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read ClaudeCoach/athletes/{slug}/session-log.json and note all existing activity_id values.

Step 3 — For the most recent activity that is NOT already in session-log.json:
  - Fetch full detail via Bash: python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint activity_detail --activity-id <id>
  - Add a stub entry to ClaudeCoach/athletes/{slug}/session-log.json (prepend to array, most recent first).

  For Ride or Run:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "<sport>",
      "tss": <tss>, "duration_min": <duration>, "distance_km": <distance or null>,
      "avg_power": <avg_power or null>, "norm_power": <norm_power or null>, "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null,
      "injury_pain_during": null, "injury_pain_next_morning": null,
      "nutrition_g_carb": null, "hydration_ml": null, "notes": null,
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
  - Run via Bash: git -C {PROJECT_DIR} add ClaudeCoach/athletes/{slug}/session-log.json ClaudeCoach/athletes/{slug}/swim-log.json && git -C {PROJECT_DIR} commit -m "stub: <name> <date>" && git -C {PROJECT_DIR} push origin main

Step 4 — Respond in EXACTLY this format (no other text):
ACTIVITY_ID: <id or none>
ANALYSIS: <coaching message — see rules below>

{first_name}: FTP {ftp} W.{injury_line}

Rules for ANALYSIS (2-3 lines, max 400 chars):
- Ride (structured, >3 intervals): Line 1 = interval set summary (e.g. "5×10 min @ 272W avg — 105% FTP"). Line 2 = completion vs target if any intervals were cut or missed. Line 3 = "Nutrition — g carbs/hr and bottles?"
- Ride (unstructured): Line 1 = NP + IF. Line 2 (>90 min) = aerobic decoupling %. Line 3 = "Nutrition — g carbs/hr and bottles?"
{run_injury_ask}
- Swim: Line 1 = distance + pace vs CSS target (state +/- seconds). Line 2 = "RPE and how did it feel?"
- Strength: Line 1 = duration. Line 2 = "RPE and what was the main focus?"

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

If no activities at all: ACTIVITY_ID: none  ANALYSIS: none"""


def _notify(msg, chat_id):
    try:
        subprocess.run(
            ["python3", str(NOTIFY), "--chat-id", str(chat_id), msg],
            cwd=PROJECT_DIR, timeout=15,
        )
    except Exception:
        pass


def load_state(state_file):
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {"last_id": None}


def save_state(state, state_file):
    state_file.write_text(json.dumps(state))


def acquire_lock():
    if LOCK_FILE.exists():
        if time.time() - LOCK_FILE.stat().st_mtime < 300:
            return False
    LOCK_FILE.touch()
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


def _send_followup_nudge(state, session_log_f, chat_id, injuries=None, state_file=None):
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
            if e.get("injury_pain_during") is not None:
                continue
        elif e.get("rpe") is not None:
            continue
        # Only nudge for today's activities — don't nag about old stubs
        if e.get("date", "") != today_str:
            continue
        aid = str(e.get("activity_id", ""))
        if aid in nudged_ids:
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
            if sport == "Run" and has_injury:
                # ANALYSIS already asked for injury score — ask for RPE here instead
                msg = f"RPE for the {name or 'run'}? (1–10) — and how did the ankle feel this morning?"
            elif sport == "Run":
                msg = f"RPE for the {name or 'run'}? (1–10)"
            elif sport == "Swim":
                msg = f"RPE for the {name or 'swim'}? And how did it feel overall?"
            elif sport == "Ride" and (e.get("duration_min") or 0) >= 90:
                msg = f"Fuelling check for the {name or 'ride'} — roughly g carbs/hr?"
            else:
                msg = f"RPE for {name or 'last session'}? (1–10)"
            _notify(msg, chat_id)
            nudged_ids.add(aid)
            state["nudged_ids"] = list(nudged_ids)
            # Persist nudged_ids so the same stub is never nudged again
            if state_file:
                save_state(state, state_file)
            break  # one nudge per cycle


def _check_test_reminders(adir: Path, chat_id: str, state: dict, state_file: Path | None):
    """Nudge athlete if a performance test is due within 3 days and not yet notified."""
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
            _notify(f"⏱ Test due {when}: *{label}*\n_{protocol}_", chat_id)
            notified_tests.add(key)
            state["notified_tests"] = list(notified_tests)
            changed = True
            break  # one reminder per cycle

    if changed and state_file:
        save_state(state, state_file)


def check_athlete(slug, athlete_cfg):
    """Run activity check for one athlete."""
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

    ftp = profile.get("ftp_watts") or 250
    first_name = profile.get("name", slug).split()[0]
    injuries = profile.get("injuries", [])

    state = load_state(state_file)

    # Test reminders run every cycle regardless of new activity
    _check_test_reminders(adir, chat_id, state, state_file)

    # Snapshot existing IDs before Claude runs
    existing_ids: set = set()
    if session_log_f.exists():
        try:
            existing_ids = {str(e.get("activity_id", "")) for e in json.loads(session_log_f.read_text())}
        except (json.JSONDecodeError, OSError):
            pass

    prompt = _build_prompt(slug, first_name, ftp, injuries)

    t_start = time.time()
    try:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=180,
        )
    except subprocess.TimeoutExpired:
        _notify(
            f"Activity watcher timed out for {first_name} (180s). "
            f"Last known activity: {state.get('last_id', 'unknown')}.",
            chat_id,
        )
        return
    except Exception as exc:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Failed to launch Claude: {exc}", file=sys.stderr)
        return

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
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file)
        return

    # Dedup check
    if activity_id in existing_ids:
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file)
        return

    if activity_id == state.get("last_id"):
        _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file)
        return

    state["last_id"] = activity_id
    state["notified_at"] = datetime.now().isoformat()
    save_state(state, state_file)

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
                sign = "+" if delta_pct >= 0 else ""
                plan_delta_note = f"vs plan ({plan_name}): {int(planned_tss)}→{int(actual_tss)} TSS ({sign}{delta_pct:.0f}%)"
        except Exception:
            pass

    analysis = "\n".join(analysis_lines).strip()
    if plan_delta_note:
        analysis = f"{analysis}\n_{plan_delta_note}_" if analysis else plan_delta_note
    if analysis and analysis != "none":
        _notify(f"*New activity*\n\n{analysis}", chat_id)

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
    if new_entry:
        sport = new_entry.get("sport", "")
        dur = new_entry.get("duration_min", 0) or 0
        kb = _quick_log_keyboard(activity_id, slug, sport, bool(injuries), dur)
        hdr = "Injury pain during (0–10):" if (sport == "Run" and injuries) else "Quick log:"
        _tg_send_keyboard(chat_id, hdr, kb)

    _send_followup_nudge(state, session_log_f, chat_id, injuries=injuries, state_file=state_file)

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
                _tg_send_keyboard(
                    chat_id,
                    f"⚡ That looks like a {labels} test.\nSuggested: *{t_value} {units}*\n\nConfirm to update thresholds:",
                    {"inline_keyboard": [[
                        {"text": f"✅ Confirm {t_value} {units}", "callback_data": confirm_data},
                        {"text": "❌ Dismiss", "callback_data": dismiss_data},
                    ]]},
                )
        except Exception:
            pass

    # Trigger site data refresh in background
    subprocess.Popen(
        ["python3", str(BASE / "scripts/refresh-site-data.py")],
        cwd=PROJECT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    if not acquire_lock():
        sys.exit(0)

    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
        for slug, athlete_cfg in athletes.items():
            if not athlete_cfg.get("active", True):
                continue
            try:
                check_athlete(slug, athlete_cfg)
            except Exception as exc:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] Unhandled error: {exc}", file=sys.stderr)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
