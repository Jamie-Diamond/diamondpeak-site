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


def fetch_ctl(slug: str) -> float:
    """Return the most recent CTL value from Intervals.icu, or 0.0 on failure."""
    try:
        result = subprocess.run(
            ["python3", "ClaudeCoach/lib/icu_fetch.py", "--athlete", slug,
             "--endpoint", "fitness", "--days", "3"],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )
        if result.returncode != 0:
            return 0.0
        data = json.loads(result.stdout.strip())
        if isinstance(data, list):
            for entry in reversed(data):
                ctl = entry.get("ctl")
                if ctl is not None:
                    return float(ctl)
    except Exception:
        pass
    return 0.0


def compute_required_tss(ctl_today: float, ctl_target: float, weeks_remaining: int) -> int:
    """
    Weekly TSS needed to reach ctl_target from ctl_today in weeks_remaining weeks.
    Uses CTL EMA mechanics: CTL(N) = CTL0*(41/42)^N + D*(1-(41/42)^N), solved for D.
    """
    N = weeks_remaining * 7
    if N <= 0:
        return int(ctl_target * 7)
    decay = (41.0 / 42.0) ** N
    required_daily = (ctl_target - ctl_today * decay) / (1.0 - decay)
    return int(max(required_daily, 0.0) * 7)


def compute_projected_ctl(ctl_today: float, weekly_tss: int, weeks: int) -> float:
    """Project CTL after `weeks` weeks of constant `weekly_tss`, using day-by-day EMA."""
    daily = weekly_tss / 7.0
    ctl = ctl_today
    for _ in range(weeks * 7):
        ctl += (daily - ctl) / 42.0
    return ctl


def build_prompt(slug: str, cfg: dict, profile: dict, ctl_today: float = 0.0) -> str:
    from datetime import timedelta
    _today      = date.today()
    today       = _today.isoformat()
    today_dow   = _today.strftime("%A")
    _days_to_mon = (7 - _today.weekday()) % 7 or 7
    _next_mon   = _today + timedelta(days=_days_to_mon)
    next_monday = _next_mon.isoformat()
    date_grid_lines = []
    for i in range(14):
        d = _next_mon + timedelta(days=i)
        date_grid_lines.append(f"  {d.isoformat()} = {d.strftime('%A')}")
    date_grid_str = "\n".join(date_grid_lines)
    end_35      = (_today + timedelta(days=35)).isoformat()

    # Phase / week calculation from athletes.json
    plan_start_str = cfg.get("plan_start", "2026-04-27")
    try:
        from datetime import date as _d
        plan_start_date = _d.fromisoformat(plan_start_str)
    except Exception:
        from datetime import date as _d
        plan_start_date = _d(2026, 4, 27)
    weeks_elapsed = max(1, (_next_mon - plan_start_date).days // 7 + 1)

    ctl_targets  = cfg.get("ctl_targets", {})
    ctl_race_min = ctl_targets.get("race_min", 75)
    phase_tss_cfg = cfg.get("phase_tss", {})
    base_end_wk  = phase_tss_cfg.get("base_end_week", 6)
    build_end_wk = phase_tss_cfg.get("build_end_week", 10)
    spec_end_wk  = phase_tss_cfg.get("specific_end_week", 14)
    peak_end_wk  = phase_tss_cfg.get("peak_end_week", 17)

    athlete_dir = BASE / "athletes" / slug

    name       = cfg.get("name", slug)
    race_name  = profile.get("race_name")  or cfg.get("race_name", "upcoming race")
    race_date  = profile.get("race_date")  or cfg.get("race_date", "")
    ftp        = profile.get("ftp_watts")

    ftp_note = f"\nAthlete FTP from profile: {ftp} W" if ftp else ""

    is_triathlete = bool(profile.get("swim_css_per_100m") or profile.get("run_threshold_pace_per_km"))

    if is_triathlete:
        phase_ctl = ctl_targets.get("phase_ctl", {})
        ctl_base  = phase_ctl.get("base",  round(ctl_race_min * 0.73))
        ctl_build = phase_ctl.get("build", round(ctl_race_min * 0.88))
        ctl_spec  = phase_ctl.get("specific", round(ctl_race_min * 0.97))
        ctl_peak  = phase_ctl.get("peak", ctl_race_min)
        phase_milestones = (
            f"    Plan week: {weeks_elapsed} (plan start {plan_start_str})\n"
            f"    End of Base     (week {base_end_wk}):  >= {ctl_base} CTL\n"
            f"    End of Build    (week {build_end_wk}): >= {ctl_build} CTL\n"
            f"    End of Specific (week {spec_end_wk}): >= {ctl_spec} CTL\n"
            f"    End of Peak     (week {peak_end_wk}): >= {ctl_peak} CTL (peak before taper)\n"
            f"    Race day target: {ctl_race_min} CTL"
        )
        phase_tss = """  TSS target is Python-computed in the LOAD ACCOUNTABILITY block above — use that figure.
  Indicative phase context (DO NOT use to override LOAD ACCOUNTABILITY target):
    Base:     aerobic volume, Z2 dominance, build swim/run base
    Build:    threshold bike work, extend long run, introduce bricks
    Specific: race-pace intervals, brick sessions, sport-specific intensity
    Peak:     race simulation, consolidate fitness — high density week
    Taper:    sharpen, no new stimuli, 50–60% of peak load"""
        step5_constraints = """- Ankle: no quality run sessions (intervals/tempo/race-pace) until current-state.json ankle.four_pain_free_weeks_reached = true. Use walk-run format only (Z2 HR cap 150). Weekly run km increase <= 10%.
- CTL ramp: <= +4 CTL/wk while ankle in rehab.
- Pre-event fatigue management: if pre_event_taper = true, week 2 avoids all intensity, prioritises swim + short Z2 rides only.
- Travel / access constraints: scan current-state.md "Travel & training blocks" for any dates in the planning window where bike is unavailable. Substitute with swims or runs of equivalent TSS."""
        week_template = """Standard week template (adapt to phase):
SWIM RULE — HARD: Swims on TUESDAY and THURSDAY only. Never prescribe a swim on Monday, Wednesday, Friday, Saturday, or Sunday.
CYCLING RULE — HARD: No cycling Monday through Thursday. Bike sessions on Friday, Saturday, Sunday only.
- Monday: Rest or short Z1 run — no cycling, no swimming
- Tuesday: Swim (aerobic/CSS) + Run (Z2, walk-run if ankle protocol applies)
- Wednesday: Strength or Run (Z2) — NO cycling
- Thursday: Swim (CSS-based) + optional short run
- Friday: Long ride (~3.5–4 hr, Z2 NP target) — key session
- Saturday: Run (Z2) or Bike (if second ride week)
- Sunday: Z2 ride (2–3 hr) or rest"""
    else:
        phase_milestones = """    End of Base     (week 6):  >= 30 CTL
    End of Build    (week 10): >= 40 CTL
    End of Specific (week 14): >= 50 CTL
    End of Peak     (week 17): >= 55 CTL"""
        phase_tss = """  Week 1-6   (Base):     100-200 TSS/wk, focus Z2 bike volume, building aerobic base
  Week 7-10  (Build):    150-280 TSS/wk, add sweetspot work, extend long ride
  Week 11-14 (Specific): 200-350 TSS/wk, threshold and over-threshold intervals, longer rides
  Week 15-17 (Peak):     250-400 TSS/wk, consolidate fitness, race simulation rides
  Week 18-21 (Taper):    80-150 TSS/wk, sharpen, no new stimuli"""
        step5_constraints = """- CTL ramp: <= +5 CTL/wk.
- Pre-event fatigue management: if pre_event_taper = true, week 2 prioritises short Z2 rides only, no intensity.
- Concurrent training: check current-state.md for CrossFit or other non-cycling load not captured in CTL — plan hard bike sessions on CrossFit rest days where possible.
- Travel / access constraints: scan current-state.md "Travel & training blocks" for any dates in the planning window where bike is unavailable. Substitute with strength sessions."""
        week_template = """Standard week template — cycling only (adapt to phase; do NOT add swim or run sessions):
- Monday: Rest
- Tuesday: Strength or easy Z2 ride 45–60 min
- Wednesday: Key bike session — sweetspot or threshold intervals
- Thursday: Rest or strength
- Friday: Rest or easy Z2 ride 45–60 min
- Saturday: Long ride (Z2 NP target, progressive) — key session
- Sunday: Rest or short active recovery"""

    # Load accountability — only for athletes with explicit phase CTL targets
    _prescribe_tss   = 0  # 0 = fallback to phase ranges
    _max_weekly_tss  = 0
    _la_required_tss = 0
    _la_target_ctl   = ctl_race_min
    _la_phase        = ""
    _la_phase_end_date = ""
    load_accountability_block = ""

    if ctl_today > 0 and is_triathlete and ctl_targets.get("phase_ctl"):
        if weeks_elapsed <= base_end_wk:
            _la_phase, _la_target_ctl, _la_phase_end_wk = "Base", ctl_base, base_end_wk
        elif weeks_elapsed <= build_end_wk:
            _la_phase, _la_target_ctl, _la_phase_end_wk = "Build", ctl_build, build_end_wk
        elif weeks_elapsed <= spec_end_wk:
            _la_phase, _la_target_ctl, _la_phase_end_wk = "Specific", ctl_spec, spec_end_wk
        else:
            _la_phase, _la_target_ctl, _la_phase_end_wk = "Peak", ctl_peak, peak_end_wk

        _la_weeks_remaining = max(1, _la_phase_end_wk - weeks_elapsed + 1)
        _la_required_tss    = compute_required_tss(ctl_today, _la_target_ctl, _la_weeks_remaining)
        _la_phase_end_date  = (plan_start_date + timedelta(weeks=_la_phase_end_wk)).isoformat()

        # Ramp rate ceiling — from athletes.json, default 5 CTL/wk
        _max_ramp      = float(cfg.get("max_ctl_ramp_per_week", 5.0))
        _decay_7       = (41.0 / 42.0) ** 7
        _max_daily     = ctl_today + _max_ramp / (1.0 - _decay_7)
        _max_weekly_tss = int(_max_daily * 7)
        _prescribe_tss  = min(_la_required_tss, _max_weekly_tss)

        # Timeline note — if target is not achievable at safe ramp rate, project actual landing CTL
        if _la_required_tss > _max_weekly_tss:
            _projected_ctl = compute_projected_ctl(ctl_today, _max_weekly_tss, _la_weeks_remaining)
            _timeline_note = (
                f"TIMELINE SLIPPAGE: reaching {_la_target_ctl} CTL by {_la_phase_end_date} requires "
                f"{_la_required_tss} TSS/wk but the safe ramp limit ({_max_ramp:.0f} CTL/wk) caps "
                f"prescribable load at {_max_weekly_tss} TSS/wk.\n"
                f"At max safe load, projected CTL = {_projected_ctl:.0f} by {_la_phase_end_date} "
                f"(target: {_la_target_ctl}).\n"
                f"In Step 7/8 Telegram: state the projected landing CTL and ask {name} whether to "
                f"accept the revised trajectory, extend the phase, or increase load beyond the ramp guideline."
            )
        else:
            _timeline_note = f"Target achievable at safe ramp rate. Prescribe {_prescribe_tss} TSS/wk."

        _la_gap_threshold = int(_prescribe_tss * 0.9)
        load_accountability_block = f"""
## LOAD ACCOUNTABILITY — Python-computed, authoritative
CTL today             : {ctl_today:.1f}
Current phase         : {_la_phase} (plan week {weeks_elapsed} of {_la_phase_end_wk})
Phase CTL target      : {_la_target_ctl} by end of week {_la_phase_end_wk} ({_la_phase_end_date})
Weeks remaining       : {_la_weeks_remaining}
Required weekly TSS   : {_la_required_tss} (to hit target in time)
Max safe weekly TSS   : {_max_weekly_tss} (ramp rate cap: {_max_ramp:.0f} CTL/wk)
PRESCRIBED WEEK 1 TSS : {_prescribe_tss}

{_timeline_note}

After Step 6 (plan built), sum the planned Load for WEEK 1.
If week 1 planned TSS < {_la_gap_threshold} (>10% short of {_prescribe_tss}):
  Include a LOAD GAP section in the Step 7/8 Telegram notification:
  "Load gap: Week {weeks_elapsed} totals [X] TSS — [Y] short of the {_prescribe_tss} target.
   Options to close (within constraints):
   • [specific lever 1 with estimated TSS gain, e.g. +30 min Friday ride ≈ +25 TSS]
   • [specific lever 2 with estimated TSS gain]
   • [specific lever 3 with estimated TSS gain]
   Reply to apply any of these."
If week 1 planned TSS >= {_la_gap_threshold}: proceed silently.
"""

    # Step 3b / Step 4 content — dynamic when LOAD ACCOUNTABILITY block is active
    if _prescribe_tss > 0:
        _traj_status = (
            "AHEAD"    if ctl_today >= _la_target_ctl else
            "BEHIND"   if _la_required_tss > _max_weekly_tss else
            "ON_TRACK"
        )
        _traj_meaning = {
            "ON_TRACK": f"required {_la_required_tss} TSS/wk is within safe ramp limit {_max_weekly_tss} — prescribe {_prescribe_tss}",
            "BEHIND":   f"required {_la_required_tss} TSS/wk exceeds safe ramp limit {_max_weekly_tss} — prescribe max {_prescribe_tss} and flag timeline slippage",
            "AHEAD":    f"CTL {ctl_today:.0f} already >= phase target {_la_target_ctl} — hold load at recovery level",
        }[_traj_status]
        step3b_content = (
            f"Step 3b — Trajectory (Python-computed — do NOT re-derive):\n"
            f"CTL today = {ctl_today:.1f}, phase target = {_la_target_ctl} by {_la_phase_end_date}\n"
            f"Trajectory: {_traj_status} — {_traj_meaning}\n"
            f"Race / key-event check: scan events for days 15–28. If type=Race or priority A/B:\n"
            f"  set pre_event_taper = true; cap WEEK 2 TSS at 60% of {_prescribe_tss} = {int(_prescribe_tss * 0.6)}"
        )
        step4_content = (
            f"Step 4 — TSS target (Python-computed — do NOT override with phase ranges):\n"
            f"  Week 1: {_prescribe_tss} TSS\n"
            f"  Week 2: {int(_prescribe_tss * 0.6)} TSS if pre_event_taper = true, otherwise {_prescribe_tss} TSS\n"
            f"  Phase context (for session type selection only):\n"
            f"{phase_tss}"
        )
    else:
        step3b_content = (
            "Step 3b — Trajectory check (use fitness endpoint forward projection):\n"
            "- ctl_today = today's CTL value from fitness endpoint\n"
            "- Phase-end CTL blueprint milestones:\n"
            + phase_milestones + "\n"
            "- required_weekly_gain = (target_ctl_phase_end - ctl_today) / max(weeks_to_phase_end, 1)\n"
            "- Set trajectory_status:\n"
            "    BEHIND   if required_weekly_gain > 3.0\n"
            "    ON_TRACK if 1.5 <= required_weekly_gain <= 3.0\n"
            "    AHEAD    if required_weekly_gain < 1.5\n"
            "- Race / key-event check: scan events for days 15–28. If type=Race or priority A/B:\n"
            "    -> set pre_event_taper = true"
        )
        step4_content = (
            f"Step 4 — Determine phase and TSS target:\n"
            f"Current plan week: {weeks_elapsed} (plan start {plan_start_str}, next Monday {next_monday}).\n"
            f"Phase and TSS ranges:\n"
            f"{phase_tss}\n"
            f"Apply trajectory_status from Step 3b to select TSS within range.\n"
            f"If pre_event_taper = true: cap week 2 at bottom of range."
        )

    return f"""You are generating the rolling 2-week training plan for {name}'s {race_name} coaching system.
{ftp_note}

## DATE ANCHOR — Python-computed, authoritative
Today       : {today} ({today_dow})
Next Monday : {next_monday} (planning window start — always a Monday)
Current plan week: {weeks_elapsed} (plan started {plan_start_str})
14-day date grid (use for ALL session names — never guess the day):
{date_grid_str}
RULE: every session name must include the day-of-week from this grid.
If the profile endpoint current_date_local disagrees with {today}, flag it and use {today}.
{load_accountability_block}
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

{step3b_content}

{step4_content}

Step 5 — Apply mandatory constraints (from rules.md if present — these are HARD overrides):
{step5_constraints}
- Strength: minimum 1 session/week (target 2).
- Never prescribe new fuel/kit/shoes in the last 4 weeks.
- Always state day-of-week alongside date in session names.

Step 6 — Build the 2-week session structure:
{week_template}

Session description consistency rules:
- Never combine a fixed-distance label with a fixed-duration label unless provably equivalent
- Walk-run interval counts must match the stated duration (verify arithmetic)
- State distance OR duration in the session name, not both, unless both are internally consistent

VALIDATION GATE — before pushing ANY session, verify each one against rules.md:
1. List every session you are about to push (date, day-of-week from the date grid, sport, duration).
2. For each session, check it against rules.md constraints. Flag any violation.
3. If a violation is found: remove or reschedule the session before pushing. Do NOT push a session that breaks a hard constraint.
4. Check total weekly duration against daily and weekly caps in rules.md. If over cap, reduce lowest-priority sessions first.
5. Only after this check passes for all sessions: proceed to push.

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
    profile   = load_profile(slug)
    ctl_today = fetch_ctl(slug)
    prompt    = build_prompt(slug, cfg, profile, ctl_today)

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
