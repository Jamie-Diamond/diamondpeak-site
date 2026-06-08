#!/usr/bin/env python3
"""
Rolling plan generator — runs via VM crontab at 21:00 every Sunday (after weekly-summary.sh).
Fills the next 2 weeks in Intervals.icu if fewer than 7 events exist in that window.
Safe to run manually:
  python3 ClaudeCoach/scripts/generate-plan.py              # all active athletes
  python3 ClaudeCoach/scripts/generate-plan.py --athlete jamie
"""
import argparse, json, re, subprocess, sys, tempfile, os
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

# Load maths live in the tested ironman-analysis package — single implementation
# shared with the analysis primitives. Do NOT reintroduce inline copies here
# (see tests/test_no_duplicate_maths.py and docs/remediation-plan.md WS A).
sys.path.insert(0, str(BASE / "ironman-analysis"))
from primitives.load import (   # noqa: E402
    compute_required_tss,
    compute_projected_ctl,
    derive_phase_ctl_targets,
    compute_race_min_ctl,
)
from primitives.blueprint import (  # noqa: E402
    current_phase,
    is_multisport as event_is_multisport,
)


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


def load_blueprint(slug: str) -> dict:
    """Load athletes/{slug}/reference/training-blueprint.json if present; {} if missing/invalid.

    Emitted by generate-blueprint.py. Windows are anchored to athletes.json
    (plan_start + phase_tss), so they agree with this script's own phase
    resolution. Absent (e.g. before regeneration on a host) → {} → built-in
    phase template is used unchanged.
    """
    p = BASE / "athletes" / slug / "reference" / "training-blueprint.json"
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


def build_prompt(slug: str, cfg: dict, profile: dict, ctl_today: float = 0.0, replan: bool = False) -> str:
    from datetime import timedelta
    _today      = date.today()
    today       = _today.isoformat()
    today_dow   = _today.strftime("%A")
    if replan:
        # Replan fixes the CURRENT live plan → window starts this week's Monday, not next week's.
        _next_mon = _today - timedelta(days=_today.weekday())
    else:
        # Scheduled generation plans the upcoming fortnight → start next Monday.
        _days_to_mon = (7 - _today.weekday()) % 7 or 7
        _next_mon   = _today + timedelta(days=_days_to_mon)
    next_monday = _next_mon.isoformat()
    date_grid_lines = []
    for i in range(14):
        d = _next_mon + timedelta(days=i)
        date_grid_lines.append(f"  {d.isoformat()} = {d.strftime('%A')}")
    date_grid_str = "\n".join(date_grid_lines)
    end_35      = (_today + timedelta(days=35)).isoformat()

    if replan:
        replan_directive = (
            "- REPLAN MODE IS ON (athlete tapped Replan). The window starts THIS week's Monday\n"
            f"  ({next_monday}) — i.e. the CURRENT live plan, not next week. IGNORE the 7-event\n"
            "  threshold: even if the window is already populated, you WILL rebuild it to hit the\n"
            "  Step 4 TSS target. Set plan_already_populated = false and run Step 6 (Build to Target).\n"
            f"  ONLY touch sessions dated TODAY ({today}) or later — never modify or re-push a day\n"
            "  already completed earlier this week; leave past days exactly as they are.\n"
            "  Goal = hit the Step 4 TSS target. Don't add sessions for the sake of it — only to\n"
            "  the extent needed to reach target. How to rebuild safely, in order of preference:\n"
            "    • FIRST extend sessions that are shorter than the rules prescribe (e.g. a 170-min\n"
            "      Friday ride → 210–240 min) via edit_workout on the existing event id.\n"
            "    • THEN, only if still below target, ADD a session on a rule-permitted day via\n"
            "      push_workout. Stop adding once the week meets target — empty days are fine.\n"
            "    • Do NOT delete an existing session unless it breaks a HARD constraint in rules.md.\n"
            "      Prefer editing over deleting. Never wipe the whole week.\n"
            "  In the Step 7 message, lead with what you CHANGED (added/extended), not the old state."
        )
    else:
        replan_directive = (
            "- Normal mode: respect the 7-event threshold below (do not rebuild a populated week)."
        )

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
    ctl_race_min = compute_race_min_ctl(cfg, profile) or ctl_targets.get("race_min") or 75
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

    # Event-driven discipline branch: the event (race_distance) is the source of
    # truth for whether this is a multisport plan, via the shared event_sports
    # map in primitives.blueprint — one methodology for all athletes/events
    # (remediation WS D). Was a profile-field heuristic (swim/run thresholds).
    _event = profile.get("race_distance") or cfg.get("race_distance") or ""
    is_multisport = event_is_multisport(_event)

    # Resolve phase CTL targets — explicit config wins; auto-derive as fallback
    _phase_ctl_dict   = {}
    _phase_ctl_source = "none"
    if is_multisport:
        _configured = ctl_targets.get("phase_ctl", {})
        if _configured:
            _phase_ctl_dict   = _configured
            _phase_ctl_source = "configured"
        elif ctl_today > 0 and ctl_race_min:
            _taper_over  = float(cfg.get("taper_overshoot", 1.15))
            _derive_ramp = float(cfg.get("max_ctl_ramp_per_week", 5.0))
            _phase_ctl_dict   = derive_phase_ctl_targets(
                ctl_today, ctl_race_min, plan_start_date,
                base_end_wk, build_end_wk, spec_end_wk, peak_end_wk,
                _derive_ramp, _taper_over, today=_today,
            )
            _phase_ctl_source = "auto-derived"

    if is_multisport:
        ctl_base  = _phase_ctl_dict.get("base",     round(ctl_race_min * 0.73))
        ctl_build = _phase_ctl_dict.get("build",    round(ctl_race_min * 0.88))
        ctl_spec  = _phase_ctl_dict.get("specific", round(ctl_race_min * 0.97))
        ctl_peak  = _phase_ctl_dict.get("peak",     ctl_race_min)
        _src_note = " (auto-derived — add phase_ctl to athletes.json to override)" if _phase_ctl_source == "auto-derived" else ""
        phase_milestones = (
            f"    Plan week: {weeks_elapsed} (plan start {plan_start_str}){_src_note}\n"
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
        _injuries = profile.get("injuries") or []
        _ramp_cap = cfg.get("max_ctl_ramp_per_week", 5.0)
        _injury_block = ""
        if _injuries:
            _inj = _injuries[0]
            _injury_block = (
                f"- INJURY ({_inj.get('location','')}): {_inj.get('protocol','follow the rehab protocol')}. "
                f"No quality run sessions (intervals/tempo/race-pace) until cleared; walk-run format only where the protocol requires it. "
                f"This is athlete-specific — do NOT apply it to athletes without a logged injury.\n"
            )
        step5_constraints = (
            _injury_block
            + f"- CTL ramp: <= +{_ramp_cap:.0f} CTL/wk"
            + (" while injury in rehab.\n" if _injuries else ".\n")
            + "- Run progression (ALL athletes): run TSS may rise at most +10% week-on-week vs the "
              "trailing 4-week average run TSS, unless the athlete explicitly asks for more. See the "
              "RUN PROGRESSION GUARD in Step 6.\n"
            + "- Pre-event fatigue management: if pre_event_taper = true, week 2 avoids all intensity, "
              "prioritises swim + short Z2 rides only.\n"
            + "- Travel / access constraints: scan current-state.md \"Travel & training blocks\" for any "
              "dates in the planning window where bike is unavailable. Substitute with swims or runs of equivalent TSS."
        )
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

    if ctl_today > 0 and is_multisport and _phase_ctl_dict:
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

MANDATORY GAP CHECK — runs on EVERY path, including plan_already_populated = true:
Once the week's sessions are final (Step 6 if you built them, the existing events from
Step 3 if the plan was already populated), sum the planned Load for WEEK 1.
If week 1 planned TSS < {_la_gap_threshold} (>10% short of {_prescribe_tss}):
  Include a LOAD GAP section in the Step 7/8 Telegram notification:
  "⚠ Load gap: W{weeks_elapsed} totals [X] TSS — [Y] short of the {_prescribe_tss} target.
   Can we find more time? Options:
   • [specific lever 1 with estimated TSS gain, e.g. +30 min Friday ride ≈ +25 TSS]
   • [specific lever 2 with estimated TSS gain]
   • [specific lever 3 with estimated TSS gain]
   Reply to apply any of these."
If week 1 planned TSS >= {_la_gap_threshold}: proceed silently.
A plan that is >10% short of target with no LOAD GAP section in the message is a FAILED run.
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

    # WS C — blueprint guidance: pull per-phase content (intensity distribution,
    # bricks, fuelling, tests due) from the sidecar, keyed by the phase the
    # 14-day window falls in. Absent sidecar → empty block (built-in template
    # used unchanged), logged once.
    from datetime import date as _bd
    _bp = load_blueprint(slug)
    blueprint_block = ""
    _cur = current_phase(_bp, _next_mon)
    if _cur:
        _win_end = _next_mon + timedelta(days=13)
        _dist = _cur.get("distribution") or {}
        _dist_line = " / ".join(f"{s}: {d}" for s, d in _dist.items()) if _dist else "(not specified for this event)"
        _tests_due = []
        for _t in _bp.get("tests", []):
            try:
                _td = _bd.fromisoformat(_t["date"])
            except Exception:
                continue
            if _next_mon <= _td <= _win_end:
                _tests_due.append(f"{_t.get('label', _t.get('type', 'test'))} ({_t['date']})")
        _tests_line = "; ".join(_tests_due) if _tests_due else "none"
        blueprint_block = f"""
## BLUEPRINT GUIDANCE — phase {_cur.get('name')} ({_cur.get('start')}–{_cur.get('end')}), from training-blueprint.json
Shapes session TYPE and intensity mix. Does NOT override the LOAD ACCOUNTABILITY TSS target above.
- Intensity distribution (weekly average per sport): {_dist_line}
- Bricks this phase: aim {_cur.get('brick_min', '—')} — {_cur.get('brick_type', '')}
- Fuelling target: {_cur.get('fuelling', '—')}
- Performance tests due in this 14-day window: {_tests_line}
In Step 6, honour this distribution, include the brick(s), and schedule any due test in the first 1–2 days of an easy/recovery block.
"""
    else:
        try:
            with open(LOG_FILE, "a") as _lf:
                _lf.write(f"[generate-plan:{slug}] no training-blueprint.json sidecar — built-in phase template used.\n")
        except Exception:
            pass

    return f"""You are generating the rolling 2-week training plan for {name}'s {race_name} coaching system.
{ftp_note}

## DATE ANCHOR — Python-computed, authoritative
Today       : {today} ({today_dow})
Next Monday : {next_monday} (planning window start — always a Monday)
Current plan week: {weeks_elapsed} (plan started {plan_start_str})
14-day date grid — THE ONLY source of truth for day-of-week:
{date_grid_str}
HARD RULE — day-of-week comes ONLY from this grid. You are bad at date arithmetic; do
NOT compute weekdays in your head. For any date, find its line in the grid above and copy
that weekday verbatim. E.g. if a session is on 2026-06-15 and the grid says
"2026-06-15 = Monday", you write "Mon 15" — never "Sun 15".
SELF-CHECK before emitting the message: for every day-name you wrote, re-locate that date
in the grid and confirm the weekday matches. If any disagree, fix them. A wrong weekday is
a failed run — {name} loses trust when the dates are wrong.
If the profile endpoint current_date_local disagrees with {today}, flag it and use {today}.
{load_accountability_block}
{blueprint_block}
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
{replan_directive}
- If there are already 7+ events planned: set plan_already_populated = true. Do NOT push new sessions (skip Step 6's session building). Continue through Steps 3b–5 for trajectory and constraint review, run the MANDATORY GAP CHECK against the existing sessions, then go to Step 7 to compose the summary and Step 8 to send it.
- If <7 events: set plan_already_populated = false. Generate enough sessions to fill the week appropriately.

{step3b_content}

{step4_content}

Step 4b — DELOAD CHECK (do this before deciding the week is a recovery/deload week):
A "recovery week" means reduced TSS and easy/Z2-only sessions. Do NOT designate one unless
it is genuinely earned. Before scheduling a deload, answer these from the live data:
  1. Has there been a sustained build to recover FROM? (3+ progressively loaded weeks just gone.)
  2. Is the athlete actually fatigued? (TSB clearly negative / form negative, HRV suppressed.)
  3. Has a deload effectively ALREADY happened by accident? Real life deloads you without it
     being planned — illness, travel, a busy week, missed sessions. Check the ACTUAL TSS of the
     last 2–3 weeks (history endpoint, all sports). If a recent week was well below the phase
     band, THAT was the deload — do not stack another planned one on top.
Decision:
  • If fatigued after a real build AND no recent accidental deload → schedule the recovery week;
    state WHY in the message ("recovery week — you're at TSB X after 3 build weeks").
  • If the athlete is FRESH (TSB ≥ 0 / positive form), OR a recent week already ran light
    (accidental deload), OR they are behind their CTL target → do NOT deload. Build a normal
    load week toward the Step 4 target instead, and ASK in the Step 7 message:
      "A recovery week was on the cadence, but you're [fresh at TSB +X / coming off a light
       week (Y TSS, illness/travel)] and [N] CTL below target — I've built a normal week instead.
       Want a deload anyway?"
Never schedule an all-Z2 reduced week silently on cadence alone. Cadence is a prompt to ASK,
not a licence to deload.

Step 5 — Apply mandatory constraints (from rules.md if present — these are HARD overrides):
{step5_constraints}
- Strength: minimum 1 session/week (target 2).
- Never prescribe new fuel/kit/shoes in the last 4 weeks.
- Always state day-of-week alongside date in session names.

Step 6 — Build the 2-week session structure:
{week_template}

KEY SESSION — for a long-course triathlon in base/build, the weekly LONG AEROBIC RIDE is the
anchor. It must be present, must be a genuine long Z2/endurance ride (not displaced by an
interval/sweetspot session), and must be its full prescribed duration. Quality/interval work
is secondary to it. Never drop or shorten the long ride to make room for intervals.

BUILD TO TARGET — the weekly TSS target (Step 4) is the objective. Session count and which
days are used are just the MEANS to reach it, not goals in themselves.
- Hitting the TSS target with fewer, longer sessions is perfectly fine. A blank day or an
  unused permitted slot is NOT a problem if the week still hits target. Do NOT pad the week
  with extra sessions just to fill slots.
- The failure mode to avoid is the opposite: under-loading. Conservative forks (run instead
  of a planned long ride, 170 min where the rules say 3.5–4 hr) leave TSS on the table.
- Method: draft the week, sum its planned TSS. If it is BELOW target, close the gap — prefer
  EXTENDING existing sessions to their full prescribed duration first, then ADD a session on a
  rule-permitted day only if extending isn't enough. If it already MEETS target, stop — do not
  add more.
- GAP-CLOSING ORDER (do NOT close a gap by piling on runs): 1st extend/add the LONG RIDE and
  bike volume, 2nd swim, 3rd cross-training (if available — see below), and ONLY then running,
  within the run-progression cap below. Running is the LAST lever, never the first.

RUN PROGRESSION GUARD — HARD. Running carries the most injury risk; never balloon it to hit a
TSS target.
- Metric is run TSS (not km). Weekly run TSS may increase by at most +10% week-on-week, UNLESS
  the athlete has explicitly asked for more.
- Baseline is the AVERAGE weekly run TSS over the LAST 4 WEEKS (from the history endpoint, all
  run sessions), NOT a single week. Using a 4-week mean stops one anomalous week — a deload,
  an illness/travel week, or a single big week — from distorting the cap up or down.
- So: this week's planned run TSS ≤ (4-week average weekly run TSS) × 1.10. Compute the 4-week
  average, state it and the resulting ceiling in your reasoning, and keep planned run TSS at or
  under it.
- Respect the athlete's normal run STRUCTURE too: don't suddenly add a run day or multiple long
  runs if they normally do e.g. 3 runs with one long. Match their pattern.
- If a load gap remains after the long ride / bike / swim / cross-training are maxed within their
  rules, leave the gap and surface it (per the MANDATORY GAP CHECK) — do NOT close it with runs
  beyond this +10% cap. An over-built run week is a FAILED plan, same as an under-built one.

CROSS-TRAINING — the gap-closer when bike/run/swim are capped (e.g. ankle limits run volume,
bike is locked to Fri–Sun). Low-impact aerobic on an elliptical / basic hotel-gym machine /
aqua-jog is NOT cycling, so it can sit on the otherwise-empty Mon and Wed without breaking the
no-Mon–Thu-cycling rule, and adds Z2 load with no ankle impact.
- DO NOT assume it's available — the athlete travels and hotel-gym access varies by week.
- DO NOT speculatively push cross-training sessions to the calendar.
- Instead, if a load gap remains after maxing the rule-permitted bike/run/swim, ASK in the
  Step 7 message which days this week have elliptical/hotel-gym access, and state the TSS each
  day would add. E.g.: "Still ~Xtss short. If you'll have elliptical/gym access, tell me which
  days (Mon/Wed are free) and I'll add Z2 cross-training — ~45 TSS for 45 min each."
- When the athlete replies with the available days, those sessions get added then (not now).

- If you genuinely cannot reach target within the rules — and cross-training availability is
  unknown — that in-week shortfall is fine; surface it honestly with the cross-training ask
  above. Do NOT call it a failed plan when the constraints simply cap it.
Judge the plan on TSS vs target, never on how many slots are filled.

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

Step 7 — Compose the message. {name}'s exact spec: "tell me at a high level what's
happening when, if that's OK, any flags, or ask if we can find more time." Answer in
THAT order. The #1 thing he must learn from this message is WHAT HIS WEEK IS — lead
with the plain week, always. Never make him hunt for it.

Format (fill the brackets; drop any optional line that doesn't apply):

  *W[N] ([date range]) · [phase] — [ON TRACK / BEHIND / AHEAD]*
  This week: [plain-English one-liner of the week — e.g. "2 easy runs, Thu swim, Fri threshold ride (3×20 SS), strength ×2"].
  [Load line ONLY if behind or load changed: "[planned] TSS planned vs [required] to stay on track. Can we find more time?"]
  [• lever (+TSS)  • lever (+TSS)   ← only if a load gap, max 3, one line total if they fit]
  [Fix in ICU: [breach → where to move it] · [breach → where]   ← only if constraint breaches]
  [📌 [travel/race/access constraint in window] — one line]

Hard rules:
- Max 6 lines. No Markdown tables (Telegram shows raw pipes). Never list every session — group the routine ones.
- If STEADY (on-track, no gap, no breach, no travel, same pattern as last week): collapse to TWO lines —
  the header line + "This week: [plain one-liner]. On track, nothing to change."
- No methodology, no CTL projection maths, no "15 sessions already in Intervals.icu", no nutrition-target
  arithmetic. Those live in the logs, not in {name}'s message.

Step 8 — Output ONLY the message from Step 7, wrapped in <telegram>...</telegram> tags, and
NOTHING ELSE. Do NOT run notify.py. Do NOT send anything yourself. Do NOT print a "here's
what ran" report, preamble, or reasoning. The Python wrapper extracts the tagged text and
sends it exactly once — if you send it too, {name} gets duplicates. Your entire stdout must
be the <telegram> block.

Step 9 — Update {athlete_dir}/current-state.md "Open actions" section: mark "Plan generated through [date]" with today's date.
Run: git add ClaudeCoach/athletes/{slug}/current-state.md && git fetch origin && git rebase --autostash origin/main && git commit -m "plan: generated W[N]-W[N+1] {today}" && git push origin main
Do this BEFORE emitting the <telegram> block so the block is the last thing in your output.
"""


def run_for_athlete(slug: str, cfg: dict, replan: bool = False) -> str | None:
    profile   = load_profile(slug)
    ctl_today = fetch_ctl(slug)
    prompt    = build_prompt(slug, cfg, profile, ctl_today, replan=replan)

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
        # Extract ONLY the athlete-facing message. Never fall back to raw stdout —
        # that is Claude's internal report and must never reach the athlete.
        m = re.search(r"<telegram>(.*?)</telegram>", output, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] NO <telegram> TAG — sending fallback. Raw output:\n{output[:1000]}\n")
        return "Plan updated — check your week in Intervals.icu."
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
    ap.add_argument("--replan", action="store_true",
                    help="Rebuild the upcoming window to target even if already populated")
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
        output = run_for_athlete(slug, cfg, replan=args.replan)
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] {'output' if output else 'no output'}{' (replan)' if args.replan else ''}\n")
        if output and chat_id:
            # Single canonical send. notify.py also logs to the athlete's history.
            subprocess.run(
                ["python3", str(NOTIFY), "--chat-id", chat_id, output[:4000]],
                cwd=PROJECT_DIR,
            )
        # stdout is a short status only — the bot must NOT echo the message (notify sent it).
        print(f"[{slug}] plan message sent" if output else f"[{slug}] no message", flush=True)
    trim_log(LOG_FILE)


if __name__ == "__main__":
    main()
