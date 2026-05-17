#!/usr/bin/env python3
"""
Rolling plan generator — runs via VM crontab at 21:00 every Sunday (after weekly-summary.sh).
Fills the next 2 weeks in Intervals.icu if fewer than 7 events exist in that window.
Safe to run manually:
  python3 ClaudeCoach/scripts/generate-plan.py              # all active athletes
  python3 ClaudeCoach/scripts/generate-plan.py --athlete jamie
"""
import argparse, json, subprocess, sys, tempfile, os
from datetime import date
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/
CLAUDE      = "/usr/bin/claude"
NOTIFY      = BASE / "telegram/notify.py"
CONFIG      = BASE / "config/athletes.json"
LOG_DIR     = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE    = LOG_DIR / "generate-plan.log"

TOOLS = "Read,Write,Edit,Bash"


def trim_log(path: Path, max_lines: int = 5000):
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


def load_profile(slug: str) -> dict:
    """Load athletes/{slug}/profile.json if present; return {} if missing."""
    p = BASE / "athletes" / slug / "profile.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def build_prompt(slug: str, cfg: dict, profile: dict) -> str:
    today     = date.today().isoformat()
    athlete_dir = BASE / "athletes" / slug

    name       = cfg.get("name", slug)
    race_name  = profile.get("race_name")  or cfg.get("race_name", "upcoming race")
    race_date  = profile.get("race_date")  or cfg.get("race_date", "")
    ftp        = profile.get("ftp_watts")

    # Derive today+35 for events window
    from datetime import timedelta
    end_35 = (date.today() + timedelta(days=35)).isoformat()

    ftp_note = f"\nAthlete FTP from profile: {ftp} W" if ftp else ""

    return f"""You are generating the rolling 2-week training plan for {name}'s {race_name} coaching system.
{ftp_note}

Step 1 — Pull live data via Bash (use today's date {today} for all calculations):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint fitness --days 14 --newest {end_35}
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {end_35}

Step 2 — Read (skip any file that does not exist):
- {athlete_dir}/current-state.md (ankle, niggles, open actions)
- {athlete_dir}/current-state.json (ankle pain scores, weight)
- {athlete_dir}/reference/rules.md (HARD CONSTRAINTS — read fully if present)
- {athlete_dir}/reference/decision-points.md (upcoming forks if present)
- {athlete_dir}/session-log.json — extract all Ride/GravelRide/Brick entries with duration_min >= 90 and nutrition_g_carb set. Compute g_per_hr = nutrition_g_carb / duration_min * 60 for each. Store as nutrition_history list (most recent first).

From nutrition_history compute:
  nutrition_avg_g_hr = mean of all g_per_hr values (null if no entries)
  nutrition_target_g_hr = min(round(nutrition_avg_g_hr + 10, -1), 90) if avg exists, else 60

Step 3 — Determine the planning window:
- Target: the 2 weeks starting NEXT Monday (not today).
- Check events endpoint for that window.
- If there are already 7+ events planned: set plan_already_populated = true. Do NOT push new sessions (skip Steps 6–7). Continue through Steps 3b–5 for trajectory and constraint review, then jump to Step 8 to output a week-ahead summary of the existing sessions and send Telegram.
- If <7 events: set plan_already_populated = false. Generate enough sessions to fill the week appropriately.

Step 3b — Trajectory check (use fitness endpoint forward projection):
- ctl_today = today's CTL value from fitness endpoint
- ctl_end_wk2 = projected CTL on the last day of the 2-week planning window (passive decay baseline)
- Phase-end CTL blueprint milestones:
    End of Base     (week 6):  >= 75 CTL
    End of Build    (week 10): >= 85 CTL
    End of Specific (week 14): >= 95 CTL
    End of Peak     (week 17): >= 100 CTL
- required_weekly_gain = (target_ctl_phase_end - ctl_today) / max(weeks_to_phase_end, 1)
- Set trajectory_status:
    BEHIND   if required_weekly_gain > 3.0  -> use TOP 20% of phase TSS range
    ON_TRACK if 1.5 <= required_weekly_gain <= 3.0 -> use MIDDLE of phase TSS range
    AHEAD    if required_weekly_gain < 1.5  -> use LOWER 20% of phase TSS range
- Race / key-event check: scan events for days 15-28 from next Monday. If any event has type "Race" or priority "A" or "B":
    -> set pre_event_taper = true: cap WEEK 2 TSS at BOTTOM of phase range regardless of trajectory_status

Step 4 — Determine phase and TSS target:
- Week number = ceil((Monday date - plan_start_date) / 7), where plan_start_date is from athletes.json plan_start field if present, otherwise use 2026-04-27 as default.
- Phase and TSS ranges:
  Week 1-6   (Base):     350-500 TSS/wk, focus Z2 bike volume + aerobic swim + easy run
  Week 7-10  (Build):    450-600 TSS/wk, add threshold bike work, extend long run
  Week 11-14 (Specific): 550-720 TSS/wk, race-pace intervals, brick sessions
  Week 15-17 (Peak):     650-800 TSS/wk, race simulation, consolidate fitness
  Week 18-21 (Taper):    200-350 TSS/wk, sharpen, no new stimuli
- Apply trajectory_status from Step 3b to select the TSS target within the range
- If pre_event_taper = true: week 2 is overridden to BOTTOM of range
- If athlete has phase_tss defined in athletes.json (check current-state.json or athletes config), use those values in preference to the defaults above

Step 5 — Apply mandatory constraints (from rules.md if present — these are HARD overrides):
- Ankle: no quality run sessions (intervals/tempo/race-pace) until current-state.json ankle.four_pain_free_weeks_reached = true. Use walk-run format only (Z2 HR cap 150). Weekly run km increase <= 10%.
- CTL ramp: <= +4 CTL/wk while ankle in rehab.
- Pre-event fatigue management: if pre_event_taper = true, week 2 avoids all intensity, prioritises swim + short Z2 rides only.
- Strength: minimum 1 session/week (target 2).
- Never prescribe new fuel/kit/shoes in the last 4 weeks.
- Always state day-of-week alongside date in session names.
- Travel / access constraints: scan current-state.md "Travel & training blocks" for any dates in the planning window where bike is unavailable. Substitute with swims or runs of equivalent TSS.

Step 6 — Build the 2-week session structure:
Standard week template (adapt to phase):
- Monday: Rest or recovery swim
- Tuesday: Run (Z2, walk-run if ankle protocol applies) + optional swim
- Wednesday: Bike Z2 (60-90 min) or strength
- Thursday: Swim (CSS-based) + optional short run
- Friday: Long ride (Z2 NP target) — key session
- Saturday: Brick (ride + run) or long run
- Sunday: Rest or short active recovery

Session description consistency rules:
- Never combine a fixed-distance label with a fixed-duration label unless provably equivalent
- Walk-run interval counts must match the stated duration (verify arithmetic)
- State distance OR duration in the session name, not both, unless both are internally consistent

For each session push to Intervals.icu via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint push_workout --payload '{{"sport":"Ride|Run|Swim|WeightTraining", "date":"YYYY-MM-DD", "name":"[Day date] — [description]", "description":"full coaching notes", "planned_training_load": N}}'
  Do NOT overwrite sessions that already exist — check date+sport from events output before pushing.
  For any edit required: python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint edit_workout --event-id EVENT_ID --payload '{{"name":"...", "description":"..."}}'

Nutrition instructions for ALL sessions >90 min: state the specific nutrition_target_g_hr computed above.
If nutrition_avg_g_hr is null: "Target: 60g CHO/hr — start building gut training."

Step 7 — Output summary:
If plan_already_populated = true:
  "Week ahead [date range]:
  Week [N] ([phase]): [list each existing session — date, sport, session name, Load]
  Week [N+1] ([phase]): [list each existing session — date, sport, session name, Load]
  Fitness: [X] CTL today → target [Y] by end of [phase] (wk [Z]) · status: [BEHIND / ON_TRACK / AHEAD]
  [Any travel/access constraints flagged in current-state.md for this window]
  Active constraints: [ankle/ramp/strength rules currently in force]"

If plan_already_populated = false:
  "Plan generated: [date range]
  Week [N] ([phase]): [N sessions] · [total Load] planned
  Week [N+1] ([phase]): [N sessions] · [total Load] planned
  Fitness: [X] today -> target [Y] by end of [phase] (wk [Z]) · status: [BEHIND / ON_TRACK / AHEAD]
  Key constraints applied: [list any ankle/ramp/strength rules that shaped the plan]"

Step 8 — Send Telegram notification:
  python3 {NOTIFY} --chat-id CHAT_ID "[summary from Step 7]"
  (Replace CHAT_ID with the value from athletes.json for slug={slug})
  Send this regardless of whether the plan was already populated or freshly generated.

Step 9 — Update {athlete_dir}/current-state.md "Open actions" section: mark "Plan generated through [date]" with today's date.
Run: git add ClaudeCoach/athletes/{slug}/current-state.md && git fetch origin && git rebase --autostash origin/main && git commit -m "plan: generated W[N]-W[N+1] {today}" && git push origin main
"""


def run_for_athlete(slug: str, cfg: dict) -> str | None:
    profile = load_profile(slug)
    prompt  = build_prompt(slug, cfg, profile)

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="claudecoach_plan_", delete=False, suffix=".txt"
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
                lf.write(f"[generate-plan:{slug}] STDERR: {stderr}\n")
        return output or None
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] Exception: {e}\n")
        return None
    finally:
        os.unlink(prompt_file)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", default=None,
                    help="Slug of a single athlete to run for (default: all active)")
    args = ap.parse_args()

    athletes = json.loads(CONFIG.read_text())

    if args.athlete:
        if args.athlete not in athletes:
            print(f"ERROR: athlete '{args.athlete}' not found in athletes.json", file=sys.stderr)
            sys.exit(1)
        slugs = [args.athlete]
    else:
        slugs = [s for s, a in athletes.items() if a.get("active")]

    for slug in slugs:
        cfg    = athletes[slug]
        chat_id = str(cfg.get("chat_id", ""))
        output = run_for_athlete(slug, cfg)
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] {'output' if output else 'no output'}\n")
        if output:
            print(output, flush=True)
            if chat_id:
                subprocess.run(
                    ["python3", str(NOTIFY), "--chat-id", chat_id, output[:4000]],
                    cwd=PROJECT_DIR,
                )
    trim_log(LOG_FILE)


if __name__ == "__main__":
    main()
