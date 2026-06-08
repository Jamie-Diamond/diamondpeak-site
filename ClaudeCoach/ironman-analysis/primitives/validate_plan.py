"""validate_plan.py — deterministic backstop for the planned training week.

Pure functions, no IO. The planner asks the LLM to build a week and self-check it
against hard constraints; this module lets the Python wrapper independently verify
the *result* (the events the LLM actually pushed to intervals.icu), so a breach
cannot reach the athlete just because the model graded its own homework
(remediation-plan WS E, Issue #2).

SCOPE — mechanical constraints ONLY. This validator checks things that are
unambiguously decidable from structured data:
  - wrong-day sessions (a sport scheduled on a day the athlete's day_rules forbid)
  - weekly planned-TSS over a cap
  - implied CTL ramp over a cap
It deliberately does NOT attempt to model the prose judgment in rules.md (fuelling,
pacing, KPIs, "quality run while ankle uncleared" — which needs reliable session
classification we don't have). Those remain the LLM's job. Adding a check here
means it must be decidable without interpretation.

SINGLE RULE SOURCE — day_rules must be the SAME structure the planner's prompt was
built from (athletes.json), never a second hardcoded copy. If the validator's rules
and the prompt's rules diverge, every correct plan false-blocks. The caller passes
the athlete's day_rules through; this module never invents them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from primitives.load import compute_projected_ctl

# Weekday name → Python weekday() int (Mon=0). Accepts common abbreviations.
_DOW = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}

# intervals.icu event `type` → the day-rule key that governs it. Sports not listed
# (Run, WeightTraining, …) are unrestricted unless the athlete adds a rule for them.
_SPORT_RULE = {
    "swim": "swim_days",
    "ride": "bike_days", "virtualride": "bike_days", "gravelride": "bike_days",
    "run": "run_days",
}


def _to_weekday(name) -> int | None:
    return _DOW.get(str(name).strip().lower())


def _event_date(ev: dict) -> date | None:
    raw = ev.get("start_date_local") or ev.get("date") or ""
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _planned_load(ev: dict) -> float:
    # Planned TSS lives in load_target; icu_training_load is the post-hoc actual.
    for k in ("load_target", "planned_training_load", "icu_training_load"):
        v = ev.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _is_workout(ev: dict) -> bool:
    # Only planned workouts count; notes/races/fitness rows do not.
    cat = (ev.get("category") or "WORKOUT").upper()
    return cat == "WORKOUT"


@dataclass
class Violation:
    code: str          # machine key, e.g. "swim_forbidden_day"
    severity: str      # "hard" (block-worthy) | "soft" (warn)
    detail: str        # human-readable, names the offending session/number

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.detail}"


@dataclass
class WeekReport:
    week_start: date
    total_tss: float
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def hard(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "hard"]


def _normalise_day_rules(day_rules: dict | None) -> dict[str, set[int]]:
    """Turn {'swim_days': ['Tue','Thu'], …} into {'swim_days': {1,3}, …}."""
    out: dict[str, set[int]] = {}
    for key, days in (day_rules or {}).items():
        wd = {_to_weekday(d) for d in (days or [])}
        wd.discard(None)
        out[key] = wd
    return out


def validate_week(
    events: list[dict],
    week_start: date,
    *,
    day_rules: dict | None = None,
    weekly_tss_cap: float | None = None,
    tss_tolerance: float = 0.10,
    ctl_today: float | None = None,
    ramp_cap: float | None = None,
) -> WeekReport:
    """Validate the planned sessions for the 7 days starting `week_start`.

    Only events whose date falls in [week_start, week_start+6] and that are
    WORKOUTs are considered. Every check is OPT-IN: a constraint is only asserted
    when its input is supplied (day_rules / weekly_tss_cap / ramp_cap), so a caller
    that has no structured day_rules yet simply gets the ramp/TSS checks.

    Returns a WeekReport (total_tss + violations). The caller decides what to do
    with hard violations (warn vs block) — this module never has side effects.
    """
    rules = _normalise_day_rules(day_rules)
    week_end = week_start + timedelta(days=6)
    week_events = [
        e for e in events
        if _is_workout(e) and (_event_date(e) is not None)
        and week_start <= _event_date(e) <= week_end
    ]

    violations: list[Violation] = []

    # 1. Wrong-day sessions — a restricted sport scheduled on a forbidden weekday.
    for e in week_events:
        sport = str(e.get("type") or "").strip().lower()
        rule_key = _SPORT_RULE.get(sport)
        if not rule_key or rule_key not in rules:
            continue
        allowed = rules[rule_key]
        if not allowed:
            continue
        d = _event_date(e)
        if d.weekday() not in allowed:
            violations.append(Violation(
                code=f"{sport}_forbidden_day",
                severity="hard",
                detail=(f"{e.get('type')} on {d.isoformat()} ({d.strftime('%a')}) — "
                        f"'{e.get('name', '')}'; allowed days only: "
                        f"{sorted(allowed)} (Mon=0)"),
            ))

    # 2. Weekly planned-TSS cap.
    total_tss = sum(_planned_load(e) for e in week_events)
    if weekly_tss_cap is not None and weekly_tss_cap > 0:
        ceiling = weekly_tss_cap * (1 + tss_tolerance)
        if total_tss > ceiling:
            violations.append(Violation(
                code="weekly_tss_cap",
                severity="hard",
                detail=(f"planned {total_tss:.0f} TSS in week of {week_start} "
                        f"exceeds cap {weekly_tss_cap:.0f} "
                        f"(+{tss_tolerance:.0%} tolerance = {ceiling:.0f})"),
            ))

    # 3. Implied CTL ramp cap — project this week's load forward from today's CTL.
    if ctl_today is not None and ctl_today > 0 and ramp_cap is not None and ramp_cap > 0:
        projected = compute_projected_ctl(ctl_today, total_tss, 1)
        ramp = projected - ctl_today
        if ramp > ramp_cap + 0.5:   # half-CTL grace for rounding
            violations.append(Violation(
                code="ctl_ramp",
                severity="hard",
                detail=(f"week of {week_start}: {total_tss:.0f} TSS implies "
                        f"+{ramp:.1f} CTL/wk (from {ctl_today:.1f}), over cap "
                        f"+{ramp_cap:.1f}"),
            ))

    return WeekReport(week_start=week_start, total_tss=total_tss, violations=violations)


def validate_plan(
    events: list[dict],
    week_starts: list[date],
    **kwargs,
) -> list[WeekReport]:
    """Validate each week in a multi-week window; returns one WeekReport per week."""
    return [validate_week(events, ws, **kwargs) for ws in week_starts]
