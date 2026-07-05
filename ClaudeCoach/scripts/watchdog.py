#!/usr/bin/env python3
"""
Daily watchdog — fires a Telegram notification only if a trigger trips.
Runs via VM crontab at 05:30 daily. Loops over all active athletes.
Safe to run manually: python3 ClaudeCoach/scripts/watchdog.py
"""
import json, subprocess, sys, tempfile, os, time
from datetime import date
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/
sys.path.insert(0, str(BASE / "lib"))
import claude_call
import ops_log
import heat as heat_lib
CLAUDE      = "/usr/bin/claude"
NOTIFY      = BASE / "telegram/notify.py"
CONFIG      = BASE / "config/athletes.json"
LOG_DIR     = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE    = LOG_DIR / "watchdog.log"

TOOLS = "Read,Write,Edit,Bash"


def trim_log(path: Path, max_lines: int = 5000):
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


def load_profile(slug: str) -> dict:
    p = BASE / "athletes" / slug / "profile.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def build_prompt(slug: str, name: str, race_name: str, race_date: str, chat_id: str, heat: dict | None = None, strength_target: int | None = None) -> str:
    today = date.today().isoformat()
    athlete_dir = BASE / "athletes" / slug

    # Heat triggers are two-stage: before `starts` the formal protocol is paused
    # on ambient exposure, so we check a maintenance dose floor that keeps that
    # pause honest; from `starts` the full race-proximal targets apply.
    heat = heat or {"active": False}
    heat_log_read = ""
    heat_triggers = ""
    if heat.get("active"):
        heat_log_read = f"- {athlete_dir}/heat-log.json\n"
        starts = heat.get("starts") or today
        dose_note = ("Dose accounting: sum the dose field over entries in the last 14 days; "
                     "an entry with no dose field counts as 1.0; a missing or empty "
                     "heat-log.json counts as total dose 0.")
        if today < starts:
            # Pre-window checks are opt-in (profile heat_maintenance) — only an
            # athlete who deliberately paused formal heat work on ambient
            # exposure wants that pause policed months before the race.
            if heat.get("maintenance"):
                heat_triggers = (
                    f"T7 (Tier 2): Heat maintenance — the formal heat protocol is PAUSED on ambient "
                    f"exposure until {starts}. {dose_note} If 14-day dose < {heat_lib.MAINTENANCE_DOSE_14D} "
                    f"fire: \"heat maintenance dose low — ambient exposure is not covering the pause; "
                    f"add a sauna/hot-bath session or plan hot-venue training time\".\n"
                )
            else:
                heat_log_read = ""
        else:
            heat_triggers = (
                f"T7 (Tier 1): Formal heat protocol active since {starts}. {dose_note} "
                f"Fire if 14-day dose < {heat_lib.PROTOCOL_DOSE_14D}.\n"
                f"T8 (Tier 2): Most recent date in heat-log.json is >7 days ago (or the log is missing/empty).\n"
            )

    # Injury triggers are athlete-scoped: only an athlete with a structured `ankle`
    # block in current-state.json is in ankle rehab. Unconditional T2 text made the
    # watchdog narrate "ankle still in rehab" for Kathryn, who has no ankle injury.
    has_ankle = False
    try:
        has_ankle = bool((json.loads((athlete_dir / "current-state.json").read_text())
                          or {}).get("ankle"))
    except Exception:
        pass
    t2 = ("T2 (Tier 2): CTL ramp >4/wk while ankle still in rehab (check current-state.md "
          "ankle quality-sessions-resumed field)"
          if has_ankle else
          "T2: skip — this athlete has NO tracked injury rehab; never mention ankle or "
          "rehab status for them")
    t10_ankle = ("  - Also cross-check current-state.json ankle.weekly_run_km_this_week vs "
                 "ankle.weekly_run_km_last_week (if fields exist)\n" if has_ankle else "")

    t11 = ""
    if strength_target:
        t11 = (
            f"T11 (Tier 2): Strength compliance — target {strength_target}/week.\n"
            f"  Count strength sessions (type WeightTraining, or name containing strength/gym/S&C)\n"
            f"  in the history endpoint for each of the LAST 2 COMPLETED weeks (Mon-Sun, exclude the\n"
            f"  current part-week). If BOTH weeks are below target, fire: \"warning T11: strength X\n"
            f"  and Y sessions in last 2 weeks vs target {strength_target}/wk — schedule the missing\n"
            f"  sessions (Tier C needs no equipment)\".\n"
        )

    return f"""You are running the daily watchdog check for {name}'s {race_name} coaching system.
Run silently — only produce output if a trigger fires.

Read these files (skip any that do not exist):
- {athlete_dir}/current-state.md
- {athlete_dir}/current-state.json
- {athlete_dir}/reference/rules.md
- {athlete_dir}/reference/decision-points.md
- {athlete_dir}/session-log.json
{heat_log_read}
Pull live data via Bash (use today's date {today} for all calculations):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint fitness --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 14

Evaluate these triggers in order (skip any whose required data files are missing):
T1 (Tier 2): ATL > CTL + 25 for 3+ consecutive days
{t2}
T3 (Tier 1): HRV trend down >7% over last 7 days
T4 (Tier 1): Sleep <7h for 3+ days in last 7 (skip if no sleep data available)
T5 (Tier 1): Missed planned sessions >=2 in last rolling 7 days
  Suppression: before sending Telegram, check current-state.md for the most recent T5 entry.
  If T5 fired yesterday (or earlier) and the SAME missed session dates are already logged there, do NOT send a Telegram message — log to current-state.md only. Only send Telegram if there is a new missed session not present in the prior T5 entry.
T6 (Tier 1): Aerobic decoupling >5% on any Z2 ride in last 7 days (check via activity_detail for rides with IF < 0.75):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint activity_detail --activity-id ID
  Suppression: before sending Telegram, check current-state.md for the most recent T6 entry.
  If T6 fired in the last 3 days and all flagged rides are already logged there (same activity dates), do NOT send a Telegram message — log to current-state.md only. Only send Telegram if there is a new Z2 ride with decoupling >5% not present in the prior T6 entry.
{heat_triggers}T9 (Tier 2): Decision-point action due within 7 days and not marked done in current-state.json open_actions[].status
  - Read {athlete_dir}/reference/decision-points.md for dated items (skip if file missing)
  - Cross-check against open_actions in current-state.json; fire for any item whose due date <= today+7 and status does not start with "done" and status is not "dropped" and status is not "noted"
  - Example fire: "FTP retest due 2026-05-31 — not yet done"
T10 (Tier 2): Run weekly km increase >10% week-on-week
  - Sum run distance (km) from history endpoint for Mon–today (current week)
  - Sum run distance for the 7 days prior (last week)
{t10_ankle}  - Fire if this_week_km > last_week_km * 1.10 AND last_week_km > 0
  - Fire message: "warning T10: run km +X% week-on-week ([this]km vs [last]km) — 10% cap applies"
{t11}
If NO triggers fire: output nothing. Silent run.

DO NOT SEND ANY TELEGRAM MESSAGE — ever. The watchdog is silent. Its job is to DETECT and
LOG only. The 06:30 morning card reads current-state.md and surfaces any relevant flag to the
athlete then. A 05:30 ping is exactly what the athlete asked us to stop. NEVER run notify.py.

If ANY trigger fires:
1. DAILY-NAG SUPPRESSION — before logging, read current-state.md and check whether this SAME
   trigger (same trigger name + same underlying item/signal, e.g. the same overdue action) was
   already logged within the last 3 days. If it was, do NOTHING for that trigger — no new entry,
   no commit. Only log a trigger that is NEW or whose signal has materially changed. This stops
   the athlete being reminded of the same unfinished thing every single morning.
2. For genuinely new/changed triggers only: update current-state.md — append to the relevant
   section with today's date and trigger name + signal value. Do not rewrite untouched sections.
3. If you appended anything, run: git add ClaudeCoach/athletes/{slug}/current-state.md && git fetch origin && git rebase --autostash origin/main && git commit -m "watchdog: [trigger list] {today}" && git push origin main
4. Output one L2 reasoning trail per trigger to stdout (this goes to the coaching log only — NOT to athletes):
   [signal with real number] -> [rule: T1-T10] -> [suggested adjustment] -> [expected effect]
   Example: "ATL 148 vs CTL 121 for 4 days -> T1 (ATL > CTL +25) -> insert recovery day -> TSB recovers ~8 pts by weekend"
"""


def run_for_athlete(slug: str, cfg: dict) -> str | None:
    name      = cfg.get("name", slug)
    race_name = cfg.get("race_name", "upcoming race")
    race_date = cfg.get("race_date", "")
    chat_id   = str(cfg.get("chat_id", ""))

    profile = load_profile(slug)
    heat = heat_lib.state(slug, profile)
    strength_target = None
    if profile.get("strength_programme"):
        strength_target = int((cfg.get("day_rules") or {}).get("strength_max", 2))

    prompt = build_prompt(slug, name, race_name, race_date, chat_id, heat=heat,
                          strength_target=strength_target)

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="claudecoach_watchdog_", delete=False, suffix=".txt"
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        result = claude_call.run_claude(
            open(prompt_file).read(),
            model=claude_call.SONNET, allowed_tools=TOOLS,
            cwd=PROJECT_DIR, timeout=None, label=slug,
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            with open(LOG_FILE, "a") as lf:
                lf.write(f"[watchdog:{slug}] STDERR: {stderr}\n")
        if result.returncode != 0:
            ops_log.alert("watchdog",
                          f"claude CLI exit {result.returncode}: {stderr[-300:]}", athlete=slug)
            return None
        ops_log.record_run("watchdog", athlete=slug, ok=True,
                           detail="triggered" if output else "silent")
        return output or None
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[watchdog:{slug}] Exception: {e}\n")
        ops_log.alert("watchdog", f"exception: {e}", athlete=slug)
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
        if processed:
            # Space the athletes' Claude runs to avoid bursting the rate limit.
            time.sleep(ATHLETE_STAGGER_S)
        processed = True
        chat_id = str(cfg.get("chat_id", ""))
        output = run_for_athlete(slug, cfg)
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[watchdog:{slug}] {'triggered' if output else 'silent'}\n")
        if output:
            # Log the reasoning trail only — Claude sends the Telegram notification
            # itself via the Bash tool with the injected chat_id. Sending output here
            # would leak the reasoning trail to the athlete.
            print(output, flush=True)
    trim_log(LOG_FILE)


if __name__ == "__main__":
    main()
