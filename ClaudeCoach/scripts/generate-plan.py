#!/usr/bin/env python3
"""
Rolling plan generator — runs via VM crontab at 21:00 every Sunday (after weekly-summary.sh).
Fills the next 2 weeks in Intervals.icu if fewer than 7 events exist in that window.
Safe to run manually:
  python3 ClaudeCoach/scripts/generate-plan.py              # all active athletes
  python3 ClaudeCoach/scripts/generate-plan.py --athlete jamie
"""
import argparse, json, re, subprocess, sys, tempfile, os, time
from datetime import date, timedelta
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
sys.path.insert(0, str(BASE / "lib"))
import ops_log  # noqa: E402
from primitives.load import (   # noqa: E402
    compute_required_tss,
    compute_projected_ctl,
    derive_phase_ctl_targets,
    compute_race_min_ctl,
)
from primitives.blueprint import (  # noqa: E402
    current_phase,
    resolve_phases,
    is_multisport as event_is_multisport,
)
from primitives.validate_plan import validate_week  # noqa: E402


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


_FULL_DAY = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}
_DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_WD_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WD_LEAD_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", re.IGNORECASE)


def corrected_weekday_name(name: str, start_date_iso: str) -> str | None:
    """If a session name leads with a weekday word that disagrees with its actual
    date, return the name with the weekday corrected; else None (no change needed).

    The LLM reliably gets the date number right but miscomputes the weekday word
    (a long-standing failure mode). This is the deterministic correction."""
    nm = (name or "").strip()
    d = (start_date_iso or "")[:10]
    m = _WD_LEAD_RE.match(nm)
    if not m or not d:
        return None
    try:
        correct = _WD_ABBR[date.fromisoformat(d).weekday()]
    except Exception:
        return None
    if m.group(1).capitalize() == correct:
        return None
    return correct + nm[len(m.group(1)):]


def _full_day(d: str) -> str:
    return _FULL_DAY.get(str(d).strip().lower()[:3], str(d))


def hard_day_rule_lines(day_rules: dict) -> str:
    """Render the HARD per-sport day-rule lines from structured day_rules.

    THE single source: the same day_rules dict drives this prompt text AND the
    validate_plan backstop (remediation WS E), so what the planner is told and what
    the validator enforces cannot diverge. Returns "" if no rules — caller falls
    back to its built-in text.
    """
    lines = []
    for key, label, verb in (("swim_days", "SWIM", "Swims"),
                             ("bike_days", "CYCLING", "Bike sessions"),
                             ("run_days", "RUN", "Runs")):
        days = (day_rules or {}).get(key)
        if not days:
            continue
        allowed = [_full_day(d) for d in days]
        forbidden = [d for d in _DAY_ORDER if d not in allowed]
        lines.append(
            f"{label} RULE — HARD: {verb} ONLY on {', '.join(allowed)}. "
            f"Never on any other day ({', '.join(forbidden) or 'none'})."
        )
    return "\n".join(lines)


def planning_window_start(today: date, replan: bool) -> date:
    """First Monday of the planning window — the single source for both the prompt
    and the post-run validation backstop.

    Replan fixes the CURRENT live plan → this week's Monday. Scheduled generation
    plans the upcoming fortnight → next Monday.
    """
    if replan:
        return today - timedelta(days=today.weekday())
    days_to_mon = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=days_to_mon)


def _icu_json(slug: str, endpoint: str, *extra):
    """Run icu_fetch for one endpoint, return parsed JSON or None (soft-fail)."""
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


def prefetch_plan_data(slug: str) -> dict | None:
    """Pre-fetch the Step-1 live data in Python so the LLM doesn't burn 5 tool-call
    round-trips fetching it (~20s each). Returns a dict of the raw payloads, or None
    if ICU is unreachable (caller then leaves the LLM to fetch). Each endpoint
    soft-fails independently."""
    today  = date.today()
    end_35 = (today + timedelta(days=35)).isoformat()
    data = {
        "profile":  _icu_json(slug, "profile"),
        # Recent 14d (up to today) — the actual CTL/ATL trend. (The old Step-1 command
        # used --newest end_35, which returned FUTURE projected decay — not useful here.)
        "fitness":  _icu_json(slug, "fitness", "--days", "14"),
        "wellness": _icu_json(slug, "wellness", "--days", "14"),
        "history":  _icu_json(slug, "history", "--days", "14"),
        "events":   _icu_json(slug, "events", "--start", today.isoformat(), "--end", end_35),
    }
    failed = [k for k, v in data.items() if v is None]
    if failed:
        # Loud, not fatal — the LLM still has its own fetch fallback, but a
        # plan built on partial ICU data is something the coach must know about.
        ops_log.alert("generate-plan",
                      f"ICU prefetch failed for: {', '.join(failed)}", athlete=slug)
    if data["profile"] is None and data["fitness"] is None and data["events"] is None:
        return None
    return data


def _render_prefetched(d: dict) -> str:
    """Compact, complete-enough rendering of the pre-fetched Step-1 data — the fields
    the prompt actually consumes (CTL/ATL/form, HRV/sleep, recent + planned load,
    event ids), not raw dumps."""
    WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    def _d10(x): return str(x or "")[:10]
    def _wd(iso):
        try: return WD[date.fromisoformat(iso).weekday()]
        except Exception: return "?"
    out = []

    prof = d.get("profile") or {}
    if isinstance(prof, dict) and prof:
        # Training settings ONLY — never inject the raw ICU profile (it carries email,
        # location, bikes, integration tokens that the planner has no use for).
        sports = []
        for s in (prof.get("sportSettings") or []):
            types = s.get("types") or []
            if not types:
                continue
            entry = {"sport": types[0], "ftp": s.get("ftp"), "lthr": s.get("lthr"),
                     "max_hr": s.get("max_hr"), "threshold_pace": s.get("threshold_pace"),
                     "sweet_spot_min": s.get("sweet_spot_min"), "sweet_spot_max": s.get("sweet_spot_max"),
                     "power_zones": s.get("power_zones"), "hr_zones": s.get("hr_zones"),
                     "pace_zones": s.get("pace_zones")}
            sports.append({k: v for k, v in entry.items() if v not in (None, [])})
        weight = prof.get("icu_weight") or prof.get("weight")
        out.append(f"PROFILE (training settings only): weight={weight}")
        out.append("  " + json.dumps(sports, separators=(",", ":"))[:1600])

    fitness = d.get("fitness") or []
    if isinstance(fitness, list) and fitness:
        out.append("FITNESS (date · CTL · ATL · form TSB):")
        eftp = None
        for r in fitness[-14:]:
            ctl = r.get("ctl"); atl = r.get("atl")
            if ctl is None: continue
            tsb = (ctl or 0) - (atl or 0)
            out.append(f"  {_d10(r.get('id') or r.get('date'))}  CTL {ctl:.0f}  ATL {(atl or 0):.0f}  TSB {tsb:+.0f}")
            for s in (r.get("sportInfo") or []):
                if s.get("type") == "Ride" and s.get("eftp"):
                    eftp = int(s["eftp"])
        if eftp:
            out.append(f"  Latest cycling eFTP: {eftp} W")

    wellness = d.get("wellness") or []
    if isinstance(wellness, list) and wellness:
        out.append("WELLNESS (date · HRV · sleep h · RHR):")
        for r in wellness[-14:]:
            slp = r.get("sleepSecs")
            slp_h = f"{slp/3600:.1f}h" if slp else "—"
            out.append(f"  {_d10(r.get('id') or r.get('date'))}  HRV {r.get('hrv') or '—'}  sleep {slp_h}  RHR {r.get('restingHR') or '—'}")

    history = d.get("history") or []
    if isinstance(history, list) and history:
        out.append("RECENT ACTIVITIES (last 14d · date · sport · TSS · min · name):")
        for a in sorted(history, key=lambda x: str(x.get("start_date_local") or "")):
            dt = _d10(a.get("start_date_local"))
            mt = a.get("moving_time") or 0
            out.append(f"  {dt} {_wd(dt)} {str(a.get('type') or ''):6} TSS {int(a.get('icu_training_load') or 0):>3} {round(mt/60):>3}min {str(a.get('name') or '')[:40]}")

    events = d.get("events") or []
    if isinstance(events, list):
        wk = [e for e in events if (e.get("category") or "WORKOUT").upper() == "WORKOUT"]
        out.append(f"PLANNED EVENTS today→+35d ({len(wk)}) (date · sport · id · planned TSS · min · name):")
        for e in sorted(wk, key=lambda x: str(x.get("start_date_local") or "")):
            dt = _d10(e.get("start_date_local"))
            mt = e.get("moving_time")
            out.append(f"  {dt} {_wd(dt)} {str(e.get('type') or ''):6} id={e.get('id')} L={e.get('load_target')} "
                       f"{(str(round(mt/60))+'min') if mt else '—'} {str(e.get('name') or '')[:42]}")
    return "\n".join(out)


def build_prompt(slug: str, cfg: dict, profile: dict, ctl_today: float = 0.0,
                 replan: bool = False, prefetched: dict | None = None) -> str:
    _today      = date.today()
    today       = _today.isoformat()
    today_dow   = _today.strftime("%A")
    _next_mon   = planning_window_start(_today, replan)
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

    # Phase / week calculation. Configured athletes (plan_start in athletes.json)
    # anchor to plan_start exactly as prescribed; unconfigured athletes (e.g.
    # calum) derive their plan start from the race date via the shared
    # resolve_phases — the same source the blueprint sidecar uses — rather than
    # inheriting a hardcoded calendar that belongs to another athlete.
    from datetime import date as _d
    _plan_start_cfg = cfg.get("plan_start")
    _race_date_str  = profile.get("race_date") or cfg.get("race_date", "")
    try:
        _race_dt = _d.fromisoformat(_race_date_str) if _race_date_str else None
    except Exception:
        _race_dt = None
    if _plan_start_cfg:
        plan_start_str = _plan_start_cfg
        try:
            plan_start_date = _d.fromisoformat(plan_start_str)
        except Exception:
            plan_start_date = _d(2026, 4, 27)
    elif _race_dt:
        _derived_phases = resolve_phases(None, None, _race_dt, _today)
        plan_start_date = _derived_phases[0]["start"] if _derived_phases else _today
        plan_start_str  = plan_start_date.isoformat()
    else:
        plan_start_date = _d(2026, 4, 27)
        plan_start_str  = plan_start_date.isoformat()
    weeks_elapsed = max(1, (_next_mon - plan_start_date).days // 7 + 1)

    ctl_targets  = cfg.get("ctl_targets", {})
    _race_min_calc = compute_race_min_ctl(cfg, profile)
    ctl_race_min = _race_min_calc or ctl_targets.get("race_min") or 75
    # A defensible CTL basis = a race_min derived from race_target_splits, or an
    # explicit ctl_targets.race_min. Without one, ctl_race_min is just the 75
    # default — NOT a real target — so we must not synthesise a phase CTL ramp
    # off it (that would fabricate periodisation for e.g. a survival Sportive).
    _has_ctl_basis = bool(_race_min_calc or ctl_targets.get("race_min"))
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

    # Resolve phase CTL targets — explicit config wins; auto-derive as fallback.
    # Event-agnostic (was gated on is_multisport, which conflated "is a triathlete"
    # with "has a load target"). An athlete only gets targets if they have a
    # defensible basis: configured phase_ctl, or a derivable race_min. Athletes
    # without one (e.g. a survival Sportive) get no targets and fall through to
    # availability-based guidance — never a fabricated CTL ramp (remediation WS D).
    _phase_ctl_dict   = {}
    _phase_ctl_source = "none"
    _configured = ctl_targets.get("phase_ctl", {})
    if _configured:
        _phase_ctl_dict   = _configured
        _phase_ctl_source = "configured"
    elif ctl_today > 0 and _has_ctl_basis:
        _taper_over  = float(cfg.get("taper_overshoot", 1.15))
        _derive_ramp = float(cfg.get("max_ctl_ramp_per_week", 5.0))
        _phase_ctl_dict   = derive_phase_ctl_targets(
            ctl_today, ctl_race_min, plan_start_date,
            base_end_wk, build_end_wk, spec_end_wk, peak_end_wk,
            _derive_ramp, _taper_over, today=_today,
        )
        _phase_ctl_source = "auto-derived"

    # Phase CTL milestones — defined on EVERY path the LOAD ACCOUNTABILITY block
    # can take. That block now gates on _phase_ctl_dict (not is_multisport), so a
    # cycling athlete who is given a CTL basis (race_min/phase_ctl in athletes.json)
    # also reaches it — these must exist for them too, not only inside the
    # multisport content branch. Defaults derive from race-day CTL.
    ctl_base  = _phase_ctl_dict.get("base",     round(ctl_race_min * 0.73))
    ctl_build = _phase_ctl_dict.get("build",    round(ctl_race_min * 0.88))
    ctl_spec  = _phase_ctl_dict.get("specific", round(ctl_race_min * 0.97))
    ctl_peak  = _phase_ctl_dict.get("peak",     ctl_race_min)

    # Whether the athlete has fixed training days. Drives the day-specific wording in
    # the cross-training block below — without it, never assert a particular athlete's
    # day-lock (e.g. "bike locked to Fri–Sun") as if it were universal.
    _has_day_rules = bool(cfg.get("day_rules"))
    _strength_max = int((cfg.get("day_rules") or {}).get("strength_max", 2))
    if _has_day_rules:
        _xtrain_intro = (
            "CROSS-TRAINING — the gap-closer when bike/run/swim are capped (e.g. ankle limits run volume,\n"
            "bike is locked to Fri–Sun). Low-impact aerobic on an elliptical / basic hotel-gym machine /\n"
            "aqua-jog is NOT cycling, so it can sit on the otherwise-empty Mon and Wed without breaking the\n"
            "no-Mon–Thu-cycling rule, and adds Z2 load with no ankle impact."
        )
        _xt_free = "(Mon/Wed are free)"
    else:
        _xtrain_intro = (
            "CROSS-TRAINING — the gap-closer when bike/run/swim are capped (e.g. a niggle limits run volume,\n"
            "or travel limits bike access). Low-impact aerobic on an elliptical / basic hotel-gym machine /\n"
            "aqua-jog is NOT cycling, so it can sit on any day with no bike/run/swim session already on it,\n"
            "and adds Z2 load at low impact."
        )
        _xt_free = "(tell me which days)"

    if is_multisport:
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
    Specific: convert fitness to race shape — race-IF work, race-rate fuelling on key sessions, race sim late in phase
    Peak:     race simulation, consolidate fitness — high density week
    Taper:    sharpen, no new stimuli; volume steps down ~70 → 55 → 40% of peak week"""
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
        # Day layout comes from athletes.json day_rules (the single source also used
        # by the validate_plan backstop). An athlete WITH fixed days gets HARD day-rule
        # lines + a day-specific skeleton; an athlete WITHOUT (a flexible week) gets
        # neither — sessions are placed on whatever days suit their availability. Never
        # assert a fixed day, or carry over another athlete's day pattern, for someone
        # who has not set fixed days.
        _dr_lines = hard_day_rule_lines(cfg.get("day_rules"))
        if _dr_lines:
            week_template = f"""Standard week template (adapt to phase):
{_dr_lines}
- Monday: Rest (recovery day — no training)
- Tuesday: Swim (aerobic/CSS) AM + Run (Z2, walk-run if ankle protocol applies) PM
- Wednesday: Run (Z2) + ONE optional spare slot (strength OR low-impact cross-training) — NOT a default strength day; the run is the priority. NO cycling.
- Thursday: Swim only (CSS-based) — no run
- Friday: Long ride (~3.5–4 hr, Z2 NP target) — key session
- Saturday: Prefer riding — second long/endurance ride (or a run/brick if the week needs it)
- Sunday: Prefer riding — Z2 ride (or rest)"""
        else:
            week_template = """Standard week template (adapt to phase). This athlete has NO fixed training days — place each session on whatever day fits their weekly availability (profile training_days, and any current-state.md travel blocks). Honour per-day duration caps from rules.md (e.g. a Saturday long-ride time cap). Do NOT impose a fixed day for any sport, and do NOT carry over another athlete's day pattern.
Across the week, include (place freely, adapt to phase):
- The weekly LONG RIDE — the key endurance session; protect its duration and grow it week to week.
- A second, easier Z2 ride when the phase calls for more bike volume.
- 2 swims (aerobic / CSS-based).
- 2–3 runs (mostly Z2; add tempo/quality per phase, within the run-progression guard).
- Strength 1–2×.
- At least one rest or easy day. Do NOT pad days just to fill the week."""
    else:
        # Cycling-only event with no load-target basis (e.g. a survival Sportive).
        # No CTL/TSS numbers are chased — the plan is built around the weekly hour
        # ceiling and a progressive long ride. Numbers come from the athlete's
        # profile (max_hours_per_week, live CTL), never hardcoded per-athlete.
        _hrs = profile.get("max_hours_per_week")
        _hrs_str = f"~{_hrs} hr/wk" if _hrs else "the athlete's stated weekly availability"
        _ctl_note = (f"~{ctl_today:.0f} bike CTL" if ctl_today > 0
                     else "not available from the fitness feed")
        phase_milestones = (
            f"    Plan week: {weeks_elapsed} (plan start {plan_start_str})\n"
            f"    Goal: COMPLETE the event (finish/survive) — there is NO CTL target to chase, so do\n"
            f"      not invent one or flag the athlete as 'behind'.\n"
            f"    Bike fitness is {_ctl_note}; any CrossFit/other training adds general base not\n"
            f"      captured in bike CTL — factor it in, do not try to 'make it up' with extra rides.\n"
            f"    Progress = a steadily longer LONG RIDE (durability), inside the weekly hour ceiling."
        )
        phase_tss = (
            f"  Weekly bike volume CEILING: {_hrs_str} — a hard CAP, not a target to fill. Do NOT exceed it.\n"
            f"  Keep most riding easy Z2 endurance. The weekly LONG RIDE is the key session — grow its\n"
            f"  duration progressively toward the event demand; that durability matters more than CTL.\n"
            f"  Base: build the habit + aerobic base.   Build: extend the long ride, add some climbing tempo.\n"
            f"  Peak: longest long ride(s), event-terrain simulation where possible.   Taper: freshen, no new load.\n"
            f"  This is survival prep for a long, hard day — set expectations honestly, do not over-prescribe."
        )
        step5_constraints = (
            "- Weekly bike hours must NOT exceed the athlete's stated ceiling (above). More volume is not "
            "the goal here; consistency and the long ride are.\n"
            "- Concurrent training (CrossFit etc.): real load not captured in bike CTL. Plan the key long "
            "ride clear of hard CrossFit days, keep easy bike days genuinely easy, and do NOT add bike load "
            "to compensate for what CTL doesn't 'see'.\n"
            "- Pre-event fatigue management: if pre_event_taper = true, cut volume to easy spins only, no new stimuli.\n"
            "- Travel / access constraints: scan current-state.md \"Travel & training blocks\" for dates in the "
            "window where the bike is unavailable; substitute strength/cross-training — don't try to recoup the load."
        )
        week_template = """Standard week template — cycling only (do NOT add swim or run sessions). Built around the weekly hour ceiling:
- The LONG RIDE is the anchor: schedule it first, protect its duration, grow it week to week.
- Add 1–2 shorter rides around it (Z2 endurance or an easy spin), staying within the weekly hour cap.
- Strength / CrossFit continues on non-key days (the athlete already does this) — leave room for it.
- Rest days are expected at this volume; do NOT pad the week to fill empty days.
Indicative shape: one weekend long ride (key) + one midweek shorter ride; remaining days rest or CrossFit."""

    # Load accountability — only for athletes with explicit phase CTL targets
    _prescribe_tss   = 0  # 0 = fallback to phase ranges
    _max_weekly_tss  = 0
    _la_required_tss = 0
    _la_target_ctl   = ctl_race_min
    _la_phase        = ""
    _la_phase_end_date = ""
    load_accountability_block = ""

    if ctl_today > 0 and _phase_ctl_dict:
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
    elif _has_ctl_basis:
        # No live CTL (ctl_today == 0, e.g. ICU fetch failed) but the athlete has
        # CTL milestones — fall back to the milestone-driven trajectory estimate.
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
    else:
        # No CTL target at all (completion/survival goal). Do NOT compute a CTL
        # trajectory or label the athlete BEHIND/AHEAD against a target that
        # doesn't exist — assess readiness qualitatively instead.
        step3b_content = (
            "Step 3b — Readiness check (NO CTL target — completion/survival goal):\n"
            "- There is no phase CTL target. Do NOT compute required_weekly_gain or label the athlete\n"
            "  BEHIND / AHEAD / ON_TRACK against a CTL number — there is nothing to be behind.\n"
            "- Assess readiness qualitatively from live data: is weekly bike volume consistent and within\n"
            "  the hour ceiling? Is the LONG RIDE growing week to week? Is the athlete fresh (TSB >= 0) or\n"
            "  carrying fatigue? Factor in CrossFit / other training as uncaptured general base.\n"
            "- Race / key-event check: scan events for days 15–28. If type=Race or priority A/B:\n"
            "    -> set pre_event_taper = true"
        )
        step4_content = (
            f"Step 4 — Determine phase and weekly volume:\n"
            f"Current plan week: {weeks_elapsed} (plan start {plan_start_str}, next Monday {next_monday}).\n"
            f"There is no TSS target. Build the week WITHIN the volume ceiling, prioritising the long ride:\n"
            f"{phase_tss}\n"
            f"If pre_event_taper = true: reduce volume, easy spins only, no new stimuli."
        )

    # WS C — blueprint guidance: pull per-phase content (intensity distribution,
    # bricks, fuelling, tests due) from the sidecar, keyed by the phase the
    # 14-day window falls in. Absent sidecar → empty block (built-in template
    # used unchanged), logged once.
    from datetime import date as _bd
    _bp = load_blueprint(slug)
    blueprint_block = ""
    durability_block = ""
    strength_block = ""
    if profile.get("strength_programme"):
        strength_block = f"""
STRENGTH PROGRAMME — this athlete follows ClaudeCoach/blueprints/strength.md (READ it before
Step 6). Rules:
- PUSH the week's strength sessions ({_strength_max}/week in base/build/specific; taper = 1 light,
  none in race week — see the phase table) as real workouts with the session content (warm-up /
  main / ankle / core blocks) written into the event description. Default to the Tier C
  (bodyweight + band) variant — always possible, so strength is never silently dropped.
- Placement: Wednesday spare slot first, second session after a swim day; never the day before
  the long ride; >=8 h from any quality bike/run session.
- EQUIPMENT ASK — the Step 7 message must ALWAYS ask what equipment is available this week
  (full gym / dumbbells-kettlebells / bodyweight only). When the athlete answers, upgrade the
  pushed sessions' descriptions to the matching tier via edit_workout. Ask EVERY week — travel
  changes availability.
"""
    _cur = current_phase(_bp, _next_mon)
    # Durability — fatigue resistance is trained by working at intensity on tired
    # legs, not by Z2 hours alone; the long ride must finish with work from build
    # onwards. (Jamie's 2025 race limiter: −60 W on lap 2, 14.5% decoupling.)
    if _cur and any(k in str(_cur.get("name", "")).lower()
                    for k in ("build", "specific", "peak")):
        durability_block = """
DURABILITY — in build/specific/peak the weekly long ride must FINISH WITH WORK, not just
accumulate hours. Schedule the final portion at race intensity and write it explicitly into the
session description: early build = last 2x20 min at race IF; progress through the phase toward a
continuous 60–90 min race-IF finish by peak. The Z2 body of the ride stays; only the closing
block is at intensity, and it counts toward the quality share of the weekly distribution. Long
RUNS keep their existing structure — the run progression guard applies and no quality is added
to long runs unless the athlete's rules say so.
"""
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
        # Bricks only apply to multisport events; omit the line for bike-only.
        _brick_line = (f"\n- Bricks this phase: aim {_cur.get('brick_min')} — {_cur.get('brick_type')}"
                       if _cur.get("brick_min") else "")
        blueprint_block = f"""
## BLUEPRINT GUIDANCE — phase {_cur.get('name')} ({_cur.get('start')}–{_cur.get('end')}), from training-blueprint.json
{"Shapes session TYPE and intensity mix. Does NOT override the LOAD ACCOUNTABILITY TSS target above." if _prescribe_tss > 0 else "Shapes session TYPE, intensity mix, and weekly emphasis."}
- Intensity distribution (weekly average per sport): {_dist_line}{_brick_line}
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

    # Step 1 data: if pre-fetched in Python (run_for_athlete), inject it so the LLM
    # skips 5 tool-call round-trips. Otherwise tell it to fetch (tests / fallback).
    if prefetched:
        step1_block = (
            "Step 1 — Live data has ALREADY been fetched for you and is in the STEP 1 DATA\n"
            "block below. Do NOT run icu_fetch for profile / fitness / wellness / history /\n"
            "events — use the block. (Only fetch if you genuinely need a field that isn't there.)\n\n"
            "## STEP 1 DATA — pre-fetched (today " + today + ")\n" + _render_prefetched(prefetched)
        )
    else:
        step1_block = (
            "Step 1 — Pull live data via Bash (use today's date " + today + " for all calculations):\n"
            "  python3 ClaudeCoach/lib/icu_fetch.py --athlete " + slug + " --endpoint profile\n"
            "  python3 ClaudeCoach/lib/icu_fetch.py --athlete " + slug + " --endpoint fitness --days 14 --newest " + end_35 + "\n"
            "  python3 ClaudeCoach/lib/icu_fetch.py --athlete " + slug + " --endpoint wellness --days 14\n"
            "  python3 ClaudeCoach/lib/icu_fetch.py --athlete " + slug + " --endpoint history --days 14\n"
            "  python3 ClaudeCoach/lib/icu_fetch.py --athlete " + slug + " --endpoint events --start " + today + " --end " + end_35
        )

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
{step1_block}

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
- If there are already 7+ events planned: set plan_already_populated = true. Do NOT push new sessions (skip Step 6's session building). Continue through Steps 3b–5 for trajectory and constraint review, {"run the MANDATORY GAP CHECK against the existing sessions, then " if _prescribe_tss > 0 else ""}go to Step 7 to compose the summary and Step 8 to send it.
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
- Strength: 1–2 sessions/week. HARD CAP: never more than {_strength_max} strength sessions in a single week — extra strength is a FAILED plan. It is supplementary; do NOT use strength to fill slots that should hold a run, swim, or ride.
{strength_block}
- Never prescribe new fuel/kit/shoes in the last 4 weeks.
- Always state day-of-week alongside date in session names.

Step 6 — Build the 2-week session structure:
{week_template}

KEY SESSION — for a long-course triathlon in base/build, the weekly LONG AEROBIC RIDE is the
anchor. It must be present, must be a genuine long Z2/endurance ride (not displaced by an
interval/sweetspot session), and must be its full prescribed duration. Quality/interval work
is secondary to it. Never drop or shorten the long ride to make room for intervals.
{durability_block}

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

{_xtrain_intro}
- DO NOT assume it's available — the athlete travels and hotel-gym access varies by week.
- DO NOT speculatively push cross-training sessions to the calendar.
- Instead, if a load gap remains after maxing the rule-permitted bike/run/swim, ASK in the
  Step 7 message which days this week have elliptical/hotel-gym access, and state the TSS each
  day would add. E.g.: "Still ~Xtss short. If you'll have elliptical/gym access, tell me which
  days {_xt_free} and I'll add Z2 cross-training — ~45 TSS for 45 min each."
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

ICU COMMANDS — this is the COMPLETE interface. Do NOT run `--help`, grep, sed, cat, or
otherwise inspect ClaudeCoach/lib (icu_fetch.py / icu_api.py): everything you need is here.
Issuing an unnecessary exploration command wastes a full turn — there are only three commands:
  ADD a new session:
    python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint push_workout --payload '{{"sport":"Ride|Run|Swim|WeightTraining", "date":"YYYY-MM-DD", "name":"[Day date] — [description]", "description":"full coaching notes", "planned_training_load": N}}'
  EDIT an existing session (use its event id from the Step 1 events output):
    python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint edit_workout --event-id EVENT_ID --payload '{{"name":"...", "description":"...", "planned_training_load": N}}'
  DELETE a session (no payload):
    python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint delete_workout --event-id EVENT_ID
  Do NOT push a duplicate — if a session for that date+sport already exists in the Step 1 events
  output, EDIT it by its event id instead. Decide ALL changes first, then issue the calls back to
  back; do not re-read files or re-derive your plan between each call.

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
Then run this ONCE to persist it (the */30 sync also commits it, so this is best-effort):
  git add ClaudeCoach/athletes/{slug}/current-state.md && git commit -m "plan: generated W[N]-W[N+1] {today}" && git push origin main
If any git step errors (nothing to commit, push rejected, rebase needed), that is NON-FATAL —
do NOT retry, re-stage, force, or debug it; the scheduled sync reconciles it. Move on to Step 8.
Do this BEFORE emitting the <telegram> block so the block is the last thing in your output.
"""


def _refetch_window_events(slug: str, window_start: date) -> list[dict]:
    """Re-read the events the LLM just pushed for the 2-week window (read-only)."""
    end = window_start + timedelta(days=13)
    try:
        result = subprocess.run(
            ["python3", "ClaudeCoach/lib/icu_fetch.py", "--athlete", slug,
             "--endpoint", "events",
             "--start", window_start.isoformat(), "--end", end.isoformat()],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout.strip())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _correct_weekday_labels(slug: str, window_start: date) -> int:
    """Deterministic post-push guard: fix any pushed event whose name leads with a
    weekday word that disagrees with its actual date. Edits ONLY the wrong names via
    edit_workout (idempotent — a correct name yields no change). Soft-fails; returns
    the count corrected."""
    fixed = 0
    try:
        for e in _refetch_window_events(slug, window_start):
            eid = e.get("id")
            new_name = corrected_weekday_name(e.get("name", ""), e.get("start_date_local", ""))
            if not eid or not new_name:
                continue
            subprocess.run(
                ["python3", "ClaudeCoach/lib/icu_fetch.py", "--athlete", slug,
                 "--endpoint", "edit_workout", "--event-id", str(eid),
                 "--payload", json.dumps({"name": new_name})],
                capture_output=True, text=True, cwd=PROJECT_DIR,
            )
            fixed += 1
        if fixed:
            with open(LOG_FILE, "a") as lf:
                lf.write(f"[generate-plan:{slug}] weekday-label guard: corrected "
                         f"{fixed} mislabelled event name(s).\n")
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] weekday-label guard error (non-fatal): {e}\n")
    return fixed


def _backstop_validate(slug: str, cfg: dict, ctl_today: float, replan: bool) -> dict:
    """Deterministic backstop (remediation WS E). Re-fetches the plan the LLM pushed
    and validates it against the athlete's hard constraints.

    Returns {"mode", "breaches", "hard"} for the caller to act on. Read-only and
    fully soft-failing — any error returns an empty result so plan delivery never
    breaks.

    Modes (env ENFORCE_VALIDATION):
      "warn" (default) — log breaches, send the athlete's plan UNCHANGED.
      "block"          — caller runs a single remediation re-prompt, then coach-
                         alerts and withholds the athlete message if still breached.
      "0"/"off"        — skip entirely.

    ACTIVE checks: CTL ramp (always) + day-rules (when the athlete has day_rules in
    athletes.json — the same source the prompt's HARD rule lines are rendered from).
    weekly_tss_cap is intentionally not passed (redundant with the ramp ceiling)."""
    # Default flipped warn -> block 2026-06-10 (Jamie's call) after an all-clean
    # observation window in warn mode: a hard breach now gets one remediation
    # pass, then the plan is withheld with a coach alert instead of reaching
    # the athlete. Soft violations (distribution, strength count) never block.
    mode = os.environ.get("ENFORCE_VALIDATION", "block").strip().lower()
    if mode in ("0", "off", "none", "false"):
        return {"mode": "off", "breaches": [], "hard": []}
    try:
        ws = planning_window_start(date.today(), replan)
        events = _refetch_window_events(slug, ws)
        if not events:
            return {"mode": mode, "breaches": [], "hard": []}
        day_rules = cfg.get("day_rules")
        ramp_cap  = float(cfg.get("max_ctl_ramp_per_week", 5.0))
        strength_max = (day_rules or {}).get("strength_max")
        bp = load_blueprint(slug)
        reports = []
        for ws_i in (ws, ws + timedelta(days=7)):
            # Distribution targets are per-phase — resolve the phase containing
            # each validated week so boundary weeks check against the right table.
            phase = current_phase(bp, ws_i) or {}
            reports.append(validate_week(
                events, ws_i,
                day_rules=day_rules, ctl_today=ctl_today, ramp_cap=ramp_cap,
                strength_max=strength_max,
                distribution=phase.get("distribution"),
            ))
        breaches = [v for r in reports for v in r.violations]
        hard = [v for v in breaches if v.severity == "hard"]
        total = sum(r.total_tss for r in reports)
        with open(LOG_FILE, "a") as lf:
            if breaches:
                lf.write(f"[generate-plan:{slug}] VALIDATION ({mode}): "
                         f"{len(breaches)} breach(es) in the pushed plan ({total:.0f} TSS):\n")
                for v in breaches:
                    lf.write(f"    {v}\n")
                if mode != "block":
                    lf.write("    warn mode — plan sent to athlete UNCHANGED. "
                             f"day_rules={'set' if day_rules else 'ABSENT → day-rule checks inert'}.\n")
                else:
                    lf.write("    block mode — attempting one remediation pass before sending.\n")
            else:
                lf.write(f"[generate-plan:{slug}] VALIDATION ({mode}): clean — "
                         f"{total:.0f} TSS over {len(reports)} week(s), no breaches.\n")
        return {"mode": mode, "breaches": breaches, "hard": hard}
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] VALIDATION error (non-fatal): {e}\n")
        return {"mode": mode, "breaches": [], "hard": []}


def _coach_alert(slug: str, breaches: list) -> None:
    """Coach-facing alert (block mode): a hard breach survived remediation, so the
    athlete message is withheld. Always logs loudly; also Telegrams a coach chat if
    COACH_CHAT_ID is set (kept off the athlete's own chat)."""
    summary = "; ".join(str(v) for v in breaches)
    with open(LOG_FILE, "a") as lf:
        lf.write(f"[generate-plan:{slug}] *** COACH ALERT — plan WITHHELD from athlete: "
                 f"unresolved hard breach after remediation: {summary}\n")
    ops_log.alert("generate-plan", f"plan WITHHELD — unresolved hard breach: {summary[:300]}",
                  athlete=slug)
    coach_chat = os.environ.get("COACH_CHAT_ID", "").strip()
    if coach_chat:
        try:
            subprocess.run(
                ["python3", str(NOTIFY), "--chat-id", coach_chat,
                 f"⚠️ ClaudeCoach: {slug}'s plan withheld — unresolved breach: {summary[:600]}"],
                cwd=PROJECT_DIR,
            )
        except Exception:
            pass


def _remediate_plan(slug: str, cfg: dict, profile: dict, ctl_today: float,
                    replan: bool, hard_breaches: list) -> str | None:
    """Block mode: one correction pass. Re-prompts the LLM to FIX the breaching
    sessions it already pushed (edit/delete by date+sport — idempotent), then
    re-fetches and re-validates. Returns the corrected athlete message if the plan
    is then clean of hard breaches, else None (caller coach-alerts + withholds).

    NOTE (must verify before relying on this in production): assumes intervals.icu
    is read-after-write consistent enough that the post-correction re-fetch sees the
    LLM's edits. In warn mode this is harmless; in block mode it is load-bearing."""
    breach_text = "\n".join(f"  - {v.detail}" for v in hard_breaches)
    correction = build_prompt(slug, cfg, profile, ctl_today, replan=replan) + f"""

## VALIDATION FAILURE — you MUST fix these before the message is sent
The plan you just pushed breaches HARD constraints that were independently checked:
{breach_text}

This is a CORRECTION pass. For each breach, locate the offending session in the
events endpoint by its date + sport and FIX it: move it to a permitted day or
delete it via edit_workout/push_workout (check date+sport first so you don't
double-push). Do NOT introduce new breaches and do NOT touch compliant sessions.
Then re-emit ONLY the <telegram> block for the corrected week."""
    with tempfile.NamedTemporaryFile(mode="w", prefix="claudecoach_fix_",
                                     delete=False, suffix=".txt") as f:
        f.write(correction)
        fix_file = f.name
    try:
        result = subprocess.run(
            [CLAUDE, "-p", open(fix_file).read(), "--allowedTools", TOOLS,
             "--model", "claude-sonnet-4-6", "--output-format", "json"],
            capture_output=True, text=True, cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL, timeout=420,
        )
        _out = (result.stdout or "").strip()
        try:
            _j = json.loads(_out)
            if isinstance(_j, dict) and "result" in _j:
                _out = str(_j.get("result") or "").strip()
        except Exception:
            pass
        m = re.search(r"<telegram>(.*?)</telegram>", _out,
                      re.DOTALL | re.IGNORECASE)
        recheck = _backstop_validate(slug, cfg, ctl_today, replan)
        if not recheck["hard"]:
            with open(LOG_FILE, "a") as lf:
                lf.write(f"[generate-plan:{slug}] remediation SUCCEEDED — corrected plan is clean.\n")
            return m.group(1).strip() if m else "Plan updated — check your week in Intervals.icu."
        return None
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] remediation error (non-fatal): {e}\n")
        return None
    finally:
        os.unlink(fix_file)


def _scan_transcripts_for_telegram(name: str, start_ts: float) -> str | None:
    """Find the <telegram> message in the newest claude session transcript touched since
    start_ts (matched to this athlete by name). claude writes the transcript even when the
    process later hangs on exit, so this recovers the message reliably without waiting out
    the hang. Returns the last <telegram> block found, or None."""
    try:
        proj = Path.home() / ".claude" / "projects" / str(PROJECT_DIR).replace("/", "-")
        cands = [f for f in proj.glob("*.jsonl") if f.stat().st_mtime >= start_ts - 3]
    except Exception:
        return None
    for f in sorted(cands, key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            raw = f.read_text()
        except Exception:
            continue
        if name and name not in raw:
            continue                       # not this athlete's run
        texts = []
        for line in raw.splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            m = r.get("message", {})
            # ASSISTANT messages ONLY. The user message IS the prompt, which itself
            # contains the literal "<telegram>...</telegram>" instruction — matching that
            # would fire instantly, before the model has built anything.
            role = (m.get("role") if isinstance(m, dict) else None) or r.get("type")
            if role != "assistant":
                continue
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, list):
                texts += [x.get("text", "") for x in c
                          if isinstance(x, dict) and x.get("type") == "text"]
            elif isinstance(c, str):
                texts.append(c)
        found = re.findall(r"<telegram>(.*?)</telegram>", "\n".join(texts),
                           re.DOTALL | re.IGNORECASE)
        if found:
            return found[-1].strip()
    return None


def run_for_athlete(slug: str, cfg: dict, replan: bool = False) -> str | None:
    profile   = load_profile(slug)
    ctl_today = fetch_ctl(slug)
    # Pre-fetch the Step-1 live data in Python (~2s) so the LLM doesn't spend ~5
    # tool-call round-trips (~100s) fetching it. Soft-fails to None → LLM fetches.
    prefetched = prefetch_plan_data(slug)
    if prefetched is None:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] prefetch unavailable — LLM will fetch Step 1.\n")
    prompt    = build_prompt(slug, cfg, profile, ctl_today, replan=replan, prefetched=prefetched)

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="claudecoach_plan_", delete=False, suffix=".txt"
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    name = cfg.get("name", slug)
    try:
        # `claude -p` builds the plan (pushes sessions + git, then emits the <telegram>
        # block LAST per Step 8/9) in ~1-3 min, then HANGS on a post-completion CLI step
        # for several minutes before exiting. So we run it as a background process and
        # poll its session transcript: the instant the <telegram> message appears the
        # plan is fully built, so we reap the (hung) process and move on — no waiting out
        # the hang, and the message is recovered from the transcript (stdout never flushes
        # on a kill). Hard cap stops a genuinely stuck run.
        start_ts = time.time()
        proc = subprocess.Popen(
            [CLAUDE, "-p", open(prompt_file).read(),
             "--allowedTools", TOOLS, "--model", "claude-sonnet-4-6",
             "--output-format", "json"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, cwd=PROJECT_DIR,
        )
        message = None
        HARD_CAP = 900   # generous — a single planning turn can sit in API 429-backoff for ~7-8 min
        while time.time() - start_ts < HARD_CAP:
            exited = proc.poll() is not None
            message = _scan_transcripts_for_telegram(name, start_ts)
            if message or exited:
                break
            time.sleep(5)
        # Reap the process — it has almost certainly emitted the plan and is now hanging
        # on exit; there's no more useful work for it to do.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(8)
            except Exception:
                proc.kill()
        if message is None:                       # final sweep after exit
            message = _scan_transcripts_for_telegram(name, start_ts)
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] claude finished in {time.time()-start_ts:.0f}s "
                     f"(message {'recovered' if message else 'MISSING'}).\n")
        # Deterministic guard: fix any weekday word the LLM miscomputed in the names it
        # pushed (dates are right, the day-of-week word drifts). Runs before validation.
        _correct_weekday_labels(slug, planning_window_start(date.today(), replan))
        # Backstop: independently validate the plan the LLM just pushed (WS E).
        _v = _backstop_validate(slug, cfg, ctl_today, replan)
        if _v["mode"] == "block" and _v["hard"]:
            fixed = _remediate_plan(slug, cfg, profile, ctl_today, replan, _v["hard"])
            if fixed is not None:
                return fixed
            _coach_alert(slug, _v["hard"])
            return None
        if message:
            return message
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[generate-plan:{slug}] NO <telegram> recovered — sending fallback.\n")
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
