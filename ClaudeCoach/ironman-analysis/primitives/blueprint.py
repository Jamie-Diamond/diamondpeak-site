"""blueprint.py — structured training-blueprint sidecar validation.

Pure functions, no IO. The blueprint sidecar (athletes/{slug}/reference/
training-blueprint.json) is the machine-readable counterpart to the prose
training-blueprint.md, emitted by generate-blueprint.py and consumed by the
planner/validator (remediation-plan WS B/C/E). This module validates its shape
so a malformed sidecar fails loudly at generation time, not at planning time.
"""
from __future__ import annotations

from datetime import date, timedelta

SCHEMA_VERSION = 1

# Ordered phase spec: (family, display name, athletes.json end-week key).
# An athlete may omit phases (e.g. no 'specific') — missing keys are skipped.
_PHASE_SPEC = [
    ("base",     "Base",     "base_end_week"),
    ("build",    "Build",    "build_end_week"),
    ("specific", "Specific", "specific_end_week"),
    ("peak",     "Peak",     "peak_end_week"),
]

REQUIRED_TOP = [
    "schema_version",
    "slug",
    "generated",
    "event_type",
    "race_date",
    "phases",
    "tests",
]
REQUIRED_PHASE = ["name", "family", "start", "end", "weeks"]
VALID_FAMILIES = {"base", "build", "specific", "peak", "taper"}


# -- Event → sports ------------------------------------------------------------
# The single source of which disciplines an event involves. Drives which tests
# are scheduled, whether bricks apply, which distribution rows show (the
# blueprint generator) and the multisport-vs-cycling planning branch (the
# planner). Both scripts import these so the partition is defined once
# (remediation WS D — one methodology for all athletes/events).

EVENT_SPORTS = {
    "Full Ironman": ["swim", "bike", "run"],
    "70.3":         ["swim", "bike", "run"],
    "Sportive":     ["bike"],
    "Gravel":       ["bike"],
}
# Cycling events share one content profile keyed "Sportive".
CYCLING_EVENTS = {"Sportive", "Gravel", "Gran Fondo", "Road Sportive"}


def event_sports(event: str) -> list[str]:
    """Disciplines an event involves; defaults to full triathlon if unknown.

    Any cycling event (CYCLING_EVENTS) is bike-only, so the two sets stay
    consistent — e.g. 'Gran Fondo' keys to 'Sportive' content AND is bike-only,
    rather than falling through to the triathlon default.
    """
    if event in CYCLING_EVENTS:
        return ["bike"]
    return EVENT_SPORTS.get(event, ["swim", "bike", "run"])


def is_multisport(event: str) -> bool:
    """True when the event involves swim or run (not a cycling-only event)."""
    sports = event_sports(event)
    return ("swim" in sports) or ("run" in sports)


def event_key(event: str) -> str:
    """Normalise an event to its content-table key (cycling events → 'Sportive')."""
    return "Sportive" if event in CYCLING_EVENTS else event


def canonical_phases(
    plan_start: date | None,
    phase_tss: dict | None,
    race_date: date | None,
) -> list[dict]:
    """Build canonical phase windows from athletes.json config, anchored to plan_start.

    This is the single source of phase boundaries (remediation 2026-06-07
    decision): the planner already resolves phases from plan_start + phase_tss
    end-weeks, and the blueprint generator adopts the same windows so the
    sidecar agrees with what is actually prescribed.

    Returns a list of phase dicts {name, family, weeks, start, end} (start/end
    are date objects, matching generate-blueprint's internal shape), or [] when
    the athlete has no plan_start/phase_tss/race_date (caller falls back to the
    weeks-to-race auto-derivation).

    Phases present are driven by which *_end_week keys exist — an athlete with no
    `specific_end_week` simply has no Specific phase. Taper runs from the last
    configured phase end to race day.
    """
    if not plan_start or not phase_tss or not race_date:
        return []

    phases: list[dict] = []
    cursor_week = 0
    for family, name, key in _PHASE_SPEC:
        end_wk = phase_tss.get(key)
        if end_wk is None:
            continue
        start = plan_start + timedelta(weeks=cursor_week)
        end = plan_start + timedelta(weeks=end_wk) - timedelta(days=1)
        phases.append({
            "name": name, "family": family,
            "weeks": end_wk - cursor_week, "start": start, "end": end,
        })
        cursor_week = end_wk

    if not phases:
        return []

    taper_start = phases[-1]["end"] + timedelta(days=1)
    if taper_start <= race_date:
        weeks = max(1, round((race_date - taper_start).days / 7))
        phases.append({
            "name": "Taper", "family": "taper",
            "weeks": weeks, "start": taper_start, "end": race_date,
        })
    return phases


def phase_structure(weeks: int) -> list[dict]:
    """Auto-derive ordered phase dicts (name, weeks) from weeks-to-race.

    Used for athletes with no plan_start/phase_tss config in athletes.json — the
    fallback when canonical_phases() returns []. The mesocycle shape scales with
    the runway: a 12-week build has no Specific phase, a 24-week one has three
    Base blocks. Dates are added by assign_dates().
    """
    if weeks >= 24:
        return [
            {"name": "Base1",  "weeks": 6},
            {"name": "Base2",  "weeks": 4},
            {"name": "Base3",  "weeks": 4},
            {"name": "Build1", "weeks": 4},
            {"name": "Build2", "weeks": 4},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 24, 3)},
        ]
    elif weeks >= 20:
        return [
            {"name": "Base1",  "weeks": 6},
            {"name": "Base2",  "weeks": 4},
            {"name": "Build1", "weeks": 4},
            {"name": "Build2", "weeks": 4},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 20, 3)},
        ]
    elif weeks >= 16:
        return [
            {"name": "Base",   "weeks": 6},
            {"name": "Build1", "weeks": 4},
            {"name": "Build2", "weeks": 4},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 16, 2)},
        ]
    elif weeks >= 12:
        return [
            {"name": "Base",   "weeks": 4},
            {"name": "Build",  "weeks": 4},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 12, 2)},
        ]
    elif weeks >= 8:
        return [
            {"name": "Base",   "weeks": 3},
            {"name": "Build",  "weeks": 3},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": min(weeks - 8, 2)},
        ]
    else:
        return [
            {"name": "Build",  "weeks": max(weeks - 4, 1)},
            {"name": "Peak",   "weeks": 2},
            {"name": "Taper",  "weeks": 2},
        ]


def assign_dates(phases: list[dict], start: date) -> list[dict]:
    """Assign contiguous start/end dates to each phase, beginning at `start`."""
    current = start
    for p in phases:
        p["start"] = current
        p["end"] = current + timedelta(weeks=p["weeks"]) - timedelta(days=1)
        current = p["end"] + timedelta(days=1)
    return phases


def resolve_phases(
    plan_start: date | None,
    phase_tss: dict | None,
    race_date: date,
    today: date,
) -> list[dict]:
    """Resolve an athlete's phase windows — the single entry point for BOTH the
    blueprint generator and the planner, so the two never disagree.

    Configured athletes (plan_start + phase_tss in athletes.json) get
    canonical_phases anchored to plan_start. Unconfigured athletes (e.g. calum)
    auto-derive from the race date via phase_structure, anchored to `today`, with
    the final phase extended to race day. This replaces the planner's old
    fallback of pinning everyone to a hardcoded plan_start + 6/10/14/17 weeks —
    which silently put cycling athletes on another athlete's stale calendar.
    """
    phases = canonical_phases(plan_start, phase_tss, race_date)
    if phases:
        return phases
    weeks_to_race = (race_date - today).days / 7
    phases = phase_structure(int(weeks_to_race))
    phases = assign_dates(phases, today)
    if phases:
        phases[-1]["end"] = race_date
    return phases


def validate_blueprint(data: dict) -> list[str]:
    """Return a list of human-readable errors. Empty list == valid.

    Checks presence of required top-level + per-phase keys, that phases is a
    non-empty list, family values are known, and start/end parse as ISO dates
    in order. Intentionally permissive about optional content (distribution,
    fuelling, env_protocols) so partially-specified events (e.g. stubs) still
    validate.
    """
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["blueprint must be a dict"]

    for k in REQUIRED_TOP:
        if k not in data:
            errs.append(f"missing top-level key: {k}")

    if "schema_version" in data and data["schema_version"] != SCHEMA_VERSION:
        errs.append(
            f"schema_version {data['schema_version']} != expected {SCHEMA_VERSION}"
        )

    phases = data.get("phases")
    if not isinstance(phases, list) or not phases:
        errs.append("phases must be a non-empty list")
        return errs

    for i, p in enumerate(phases):
        if not isinstance(p, dict):
            errs.append(f"phase[{i}] must be a dict")
            continue
        for k in REQUIRED_PHASE:
            if k not in p:
                errs.append(f"phase[{i}] missing key: {k}")
        fam = p.get("family")
        if fam is not None and fam not in VALID_FAMILIES:
            errs.append(f"phase[{i}].family invalid: {fam}")
        parsed: dict[str, date] = {}
        for dk in ("start", "end"):
            if dk in p:
                try:
                    parsed[dk] = date.fromisoformat(p[dk])
                except (ValueError, TypeError):
                    errs.append(f"phase[{i}].{dk} not an ISO date: {p.get(dk)!r}")
        if "start" in parsed and "end" in parsed and parsed["end"] < parsed["start"]:
            errs.append(f"phase[{i}] end {p['end']} precedes start {p['start']}")

    return errs


def is_valid(data: dict) -> bool:
    return not validate_blueprint(data)


def current_phase(blueprint: dict, on_date: date) -> dict | None:
    """Return the phase dict whose [start, end] window contains on_date.

    If on_date falls outside every window, clamps to the nearest edge phase
    (before the first → first; after the last → last). Returns None only when
    the blueprint has no parseable phases. Used by the planner/validator to key
    per-phase content (distribution, bricks, fuelling) to the planning window.
    """
    parsed: list[tuple[date, date, dict]] = []
    for ph in (blueprint or {}).get("phases") or []:
        try:
            s = date.fromisoformat(ph["start"])
            e = date.fromisoformat(ph["end"])
        except (KeyError, ValueError, TypeError):
            continue
        parsed.append((s, e, ph))
    if not parsed:
        return None
    for s, e, ph in parsed:
        if s <= on_date <= e:
            return ph
    parsed.sort(key=lambda x: x[0])
    if on_date < parsed[0][0]:
        return parsed[0][2]
    return parsed[-1][2]


# ---------------------------------------------------------------------------
# TSS ceiling — the athlete's hours-based hard weekly load bound.
# Moved here from generate-blueprint.py (5 Jul 2026) so plan_builder can arm
# validate_week's weekly_tss_cap from the SAME source the blueprint displays —
# no dual implementation.
# ---------------------------------------------------------------------------

IF_TARGETS = {
    "base":     0.65,
    "build":    0.68,
    "specific": 0.70,
    "peak":     0.72,
}


def phase_family(name: str) -> str:
    n = (name or "").lower()
    if "base" in n:
        return "base"
    if "specific" in n:
        return "specific"
    if "build" in n:
        return "build"
    if "peak" in n:
        return "peak"
    return "taper"


def content_family(family: str) -> str:
    """Map a structural phase family to the family whose content tables apply.

    Since 2026-06-10 (Jamie sign-off, docs/specific-phase-proposal.md) the
    'specific' family carries its OWN content rows — race-shape conversion:
    more work slightly above race effort, race-rate fuelling on all key
    sessions, race sims split one late-Specific + one Peak. Events without a
    specific row fall back at each lookup site (fuelling default string,
    ctl_range None -> fitness check skipped), so this stays an identity map.
    """
    return family


def tss_ceiling(max_hours: float, phase_name: str) -> float | None:
    """Hard weekly TSS upper bound: max_hours x 100 x IF^2 (None in taper)."""
    fam = content_family(phase_family(phase_name))
    if fam == "taper":
        return None
    IF = IF_TARGETS[fam]
    return round(max_hours * 100 * IF ** 2, 0)
