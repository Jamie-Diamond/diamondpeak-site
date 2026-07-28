#!/usr/bin/env python3
"""macro_projection.py — block-level (macro) feasibility projection. READ-ONLY.

Slice 1 of the macro planning layer (docs/macro-planning-layer.md). The Sunday
generator builds exactly six days at a time, so nothing in the system ever asks
the block-level question: *given where the athlete is now, can the weeks that
remain actually reach the phase/race CTL target inside the athlete's own ramp cap
and hours ceiling?* A weekly-only planner cannot notice that it is 40 TSS/week
short for four weeks running, nor that it is overshooting its ceiling every week
because the CTL target it is chasing is unreachable.

This module answers that and nothing else. It writes no files, pushes no plan and
touches no calendar. It introduces NO new load or CTL arithmetic: it walks the
remaining weeks forward by repeatedly calling the SAME engine the Sunday
generator, the weekly brief, the plan audit and the dashboard already use
(`plan_tools.required_tss`), and projects CTL with the SAME primitive
(`primitives.load.compute_projected_ctl`, which is the inverse of the
`compute_required_tss` the engine targets with). It is therefore not a fifth
source of truth — it is the existing single source, iterated.

Purity: `project_block()` is pure. Every impure input is injected — current CTL,
last week's actual load, the per-week ceiling resolver and the heat-block start
date. The CLI below does the IO and injects them.

Flags it raises
  ctl_shortfall      projected CTL at the start of race week is below
                     ctl_targets.race_min — the block as configured does not
                     arrive at the fitness the race needs.
  ceiling_infeasible a week's engine target exceeds the phase TSS ceiling (plus
                     the same tolerance validate_week applies). The weekly
                     generator has only two moves here and both are wrong:
                     obey the target and hard-fail the load cap, or obey the
                     cap and quietly miss the CTL target.
  no_slack           every remaining loading week is already pinned to the ramp
                     cap, so any missed or reduced week is unrecoverable.
  deload_placement   informational: where the down-weeks fall, and whether one
                     is still consuming a late loading week after
                     plan_tools.block_deload_weeks has placed them.
  heat_overlay       the sauna block (from lib/heat.py, injected) overlays weeks
                     that are already at or near the load ceiling.

Usage:
  python3 lib/macro_projection.py --athlete jamie
  python3 lib/macro_projection.py --all --json
Exit code 0 = no hard flag, 1 = at least one hard flag. Not wired to cron.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "ironman-analysis"))
sys.path.insert(0, str(BASE / "lib"))

from primitives.blueprint import current_phase                 # noqa: E402
from primitives.load import compute_projected_ctl              # noqa: E402
from primitives.validate_plan import validate_week             # noqa: E402
# The late-loading window this flag reports on must be the SAME one plan_tools
# places deloads by, or the flag would keep firing on a block the engine has
# already repaired (or go quiet on one it has not). Same anti-drift pattern as
# _cap_tolerance() reading validate_week's own signature.
from plan_tools import LATE_LOADING_WINDOW                    # noqa: E402

ATHLETES_CONFIG = BASE / "config" / "athletes.json"

# The load ceiling this projection tests against must be the SAME line
# validate_week enforces at build time, or the macro layer would flag weeks the
# builder accepts (or worse, pass weeks it rejects). Read the tolerance off
# validate_week's own signature rather than restating 0.10, so the two cannot
# drift apart silently; test_macro_projection pins the coupling.
def _cap_tolerance() -> float:
    import inspect
    p = inspect.signature(validate_week).parameters.get("tss_tolerance")
    return float(p.default) if p is not None and p.default is not inspect._empty else 0.10


# A week is "at the ceiling" for the heat-overlay flag at this fraction of it.
_NEAR_CEILING = 0.90


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def project_block(
    cfg: dict,
    blueprint: dict,
    ctl_now: float,
    today: date | None = None,
    *,
    required_fn=None,
    ceiling_for=None,
    heat_start: date | None = None,
    last_week_tss: float | None = None,
    has_macro_plan: bool = False,
) -> dict:
    """Project every remaining week to race day and report block feasibility.

    Pure. `required_fn(cfg, ctl, today=..., last_week_tss=...)` defaults to
    plan_tools.required_tss (the canonical engine target). `ceiling_for(week_start,
    phase)` returns that week's hard weekly-TSS ceiling or None; it defaults to
    the blueprint phase's own `tss_ceiling`, which is the second half of
    plan_builder._weekly_tss_cap's precedence — the CLI injects the full resolver.

    `last_week_tss` is applied to the FIRST week only, exactly as the plan audit
    does, so a miss-triggered recovery week projects the same reduced load the
    generator would build. Later weeks pass None: a forward projection cannot
    know about a future miss, and required_tss's recovery branch is bounded on
    that argument.
    """
    today = today or date.today()
    if required_fn is None:
        import plan_tools as pt
        required_fn = pt.required_tss
    if ceiling_for is None:
        def ceiling_for(_ws, phase):
            c = (phase or {}).get("tss_ceiling")
            return float(c) if c else None

    race_s = cfg.get("race_date")
    if not race_s:
        return {"error": "no race_date configured — no block to project"}
    if not ctl_now:
        return {"error": "no current CTL — cannot project a block"}
    race = date.fromisoformat(race_s)
    tol = _cap_tolerance()

    ctl = float(ctl_now)
    # Second, STRICT trajectory: the same weeks with every load capped at the
    # ceiling itself rather than the ceiling plus validate_week's tolerance. The
    # tolerant line is what the builder can legally push, so it is the right basis
    # for "will he get there"; but on a ceiling-infeasible block the tolerant line
    # only arrives because every week is built ABOVE cap — weeks the audit
    # hard-fails. Reporting both stops the projection reading "CTL is fine" when
    # the CTL is conditional on weeks that should never ship.
    ctl_strict = float(ctl_now)
    weeks: list[dict] = []
    w = _monday(today)
    race_monday = _monday(race)
    ctl_at_race_week_start = None
    ctl_strict_at_race_week_start = None
    skipped: list[str] = []

    while w <= race_monday:
        if w == race_monday:
            ctl_at_race_week_start = round(ctl, 1)
            ctl_strict_at_race_week_start = round(ctl_strict, 1)
        r = required_fn(cfg, round(ctl, 1), today=w,
                        last_week_tss=(last_week_tss if not weeks else None))
        if r.get("error"):
            return {"error": f"engine target unavailable for week {w}: {r['error']}"}
        rec = r.get("recommended_weekly_tss")
        phase = current_phase(blueprint, w) or {}
        ceiling = ceiling_for(w, phase)
        if rec is None:
            # e.g. the taper branch with no CTL basis. Record it loudly and stop
            # projecting rather than inventing a load for the week.
            skipped.append(f"week {w}: engine returned no weekly target "
                           f"({r.get('note') or r.get('phase')}) — projection truncated")
            break
        rec = int(rec)
        allowed = int(min(rec, round(ceiling * (1 + tol)))) if ceiling else rec
        at_ceiling = int(min(rec, round(ceiling))) if ceiling else rec
        req_uncapped = r.get("required_weekly_tss")
        ramp_capped = r.get("ramp_capped_weekly_tss")
        ramp_limited = bool(req_uncapped and ramp_capped and req_uncapped > ramp_capped)
        weeks.append({
            "week_start": w.isoformat(),
            "phase": phase.get("name") or r.get("phase"),
            "week_type": r.get("week_type"),
            "engine_target_tss": rec,
            "required_uncapped_tss": req_uncapped,
            "ramp_capped_tss": ramp_capped,
            "ramp_limited": ramp_limited,
            "phase_tss_ceiling": ceiling,
            "buildable_tss": allowed,
            "buildable_at_ceiling_tss": at_ceiling,
            # Strictly greater: a week landing exactly ON ceiling x (1 + tolerance)
            # is what the validator tolerates, so it is not reported infeasible —
            # but it IS at the absolute limit, and the strict trajectory below
            # shows what the block reaches without spending that tolerance.
            "ceiling_infeasible": bool(ceiling and rec > round(ceiling * (1 + tol))),
            "phase_target_ctl": r.get("phase_target_ctl"),
            "ctl_start": round(ctl, 1),
        })
        ctl = compute_projected_ctl(ctl, allowed, 1)
        ctl_strict = compute_projected_ctl(ctl_strict, at_ceiling, 1)
        weeks[-1]["ctl_end"] = round(ctl, 1)
        weeks[-1]["ctl_end_at_ceiling"] = round(ctl_strict, 1)
        w += timedelta(days=7)

    if not weeks:
        return {"error": "no weeks between today and race day"}

    loading = [k for k in weeks if k["week_type"] not in ("taper", "deload", "race")]
    down = [k for k in weeks if k["week_type"] in ("deload", "taper")]
    pre_taper = [k for k in weeks if k["week_type"] != "taper"]
    ctl_pre_taper = pre_taper[-1]["ctl_end"] if pre_taper else None
    ctl_pre_taper_strict = pre_taper[-1]["ctl_end_at_ceiling"] if pre_taper else None
    race_min = (cfg.get("ctl_targets") or {}).get("race_min")

    flags: list[dict] = []

    if race_min and ctl_at_race_week_start is not None:
        gap = round(float(race_min) - ctl_at_race_week_start, 1)
        if gap > 0:
            flags.append({
                "code": "ctl_shortfall", "severity": "hard",
                "detail": (f"projected CTL {ctl_at_race_week_start} at the start of race "
                           f"week is {gap} below ctl_targets.race_min {race_min}; "
                           f"{len(loading)} loading week(s) remain and the block cannot "
                           f"close the gap at the configured ramp cap "
                           f"({cfg.get('max_ctl_ramp_per_week')}/wk)")})

    infeasible = [k for k in weeks if k["ceiling_infeasible"]]
    if infeasible:
        worst = max(infeasible, key=lambda k: k["engine_target_tss"] - k["phase_tss_ceiling"])
        strict_note = ""
        if ctl_strict_at_race_week_start is not None:
            strict_note = (f" The projected CTL above spends the +{tol:.0%} tolerance every "
                           f"week, i.e. it assumes weeks the plan audit hard-fails; held "
                           f"strictly at the ceiling the block reaches "
                           f"{ctl_strict_at_race_week_start} at race week")
            if race_min:
                sgap = round(float(race_min) - ctl_strict_at_race_week_start, 1)
                strict_note += (f", {sgap} below race_min {race_min}." if sgap > 0
                                else f", still at or above race_min {race_min}.")
            else:
                strict_note += "."
        flags.append({
            "code": "ceiling_infeasible", "severity": "hard",
            "detail": (f"{len(infeasible)} week(s) ask for more load than the phase "
                       f"ceiling allows — worst is week {worst['week_start']} "
                       f"({worst['phase']}): engine target {worst['engine_target_tss']} TSS "
                       f"vs ceiling {worst['phase_tss_ceiling']:.0f} "
                       f"(+{tol:.0%} = {worst['phase_tss_ceiling'] * (1 + tol):.0f}). The "
                       f"phase CTL target is unreachable inside the athlete's hours; the "
                       f"weekly generator can only overshoot the cap or miss the target."
                       + strict_note),
            "weeks": [k["week_start"] for k in infeasible]})

    if loading and all(k["ramp_limited"] for k in loading):
        flags.append({
            "code": "no_slack", "severity": "warn",
            "detail": (f"all {len(loading)} remaining loading week(s) are pinned to the "
                       f"ramp cap — there is no spare capacity, so any missed or reduced "
                       f"week is unrecoverable before race day")})

    if down:
        late = [k for k in down if k["week_type"] == "deload"
                and len([x for x in loading if x["week_start"] > k["week_start"]])
                <= LATE_LOADING_WINDOW]
        flags.append({
            "code": "deload_placement", "severity": "info",
            "detail": (f"down-weeks fall at: "
                       + ", ".join(f"{k['week_start']} ({k['week_type']})" for k in down)
                       + (f" — {len(late)} deload(s) sit inside the last "
                          f"{LATE_LOADING_WINDOW} loading weeks, which "
                          f"plan_tools.block_deload_weeks could not repair (no free "
                          f"earlier week in the block)" if late else "")),
            "weeks": [k["week_start"] for k in down]})

    if heat_start:
        hot = [k for k in weeks
               if date.fromisoformat(k["week_start"]) + timedelta(days=6) >= heat_start
               and k["phase_tss_ceiling"]
               and k["buildable_tss"] >= _NEAR_CEILING * k["phase_tss_ceiling"]]
        if hot:
            flags.append({
                "code": "heat_overlay", "severity": "warn",
                "detail": (f"the heat block starts {heat_start} and overlays "
                           f"{len(hot)} week(s) already at >={int(_NEAR_CEILING * 100)}% of "
                           f"the load ceiling; the sauna dose is additional stress on the "
                           f"heaviest weeks of the block"),
                "weeks": [k["week_start"] for k in hot]})

    if not has_macro_plan:
        flags.append({
            "code": "no_macro_plan", "severity": "info",
            "detail": ("no macro-plan sidecar exists, so the block's placement decisions "
                       "(race-simulation brick week, long-run peak week and distance, "
                       "which weeks deload) are still improvised week to week — see "
                       "docs/macro-planning-layer.md")})

    return {
        "athlete": cfg.get("name") or None,
        "as_of": today.isoformat(),
        "race_date": race_s,
        "ctl_now": round(float(ctl_now), 1),
        "race_min_ctl": race_min,
        "weeks_projected": len(weeks),
        "loading_weeks_remaining": len(loading),
        "ctl_at_taper_start": ctl_pre_taper,
        "ctl_at_race_week_start": ctl_at_race_week_start,
        # same trajectory with no ceiling tolerance spent — see the note above
        "ctl_at_taper_start_at_ceiling": ctl_pre_taper_strict,
        "ctl_at_race_week_start_at_ceiling": ctl_strict_at_race_week_start,
        "cap_tolerance": tol,
        "weeks": weeks,
        "skipped": skipped,
        "flags": flags,
        "hard_flag": any(f["severity"] == "hard" for f in flags),
    }


# ── CLI (the only impure part) ────────────────────────────────────────────────
def _run(slug: str, cfg: dict, ctl_override: float | None, on_date: date | None) -> dict:
    import plan_tools as pt
    from plan_builder import _weekly_tss_cap

    ctl = ctl_override
    last_week = None
    # --ctl-today makes the whole run offline (no ICU read at all); without it we
    # read wellness for current CTL and, on the same client, last week's actual
    # load so a miss-triggered recovery week projects as the generator would build
    # it. Both are READ-only endpoints.
    if ctl is None:
        client = pt._client(cfg)
        w = client.get_wellness(days=3)
        if not w:
            return {"athlete": slug,
                    "error": "no wellness data for current CTL; pass --ctl-today"}
        ctl = round(float(w[-1].get("ctl") or 0), 1)
        try:
            last_week = pt.last_week_actual_tss(client, today=on_date)
        except Exception:
            last_week = None

    bp = pt._load_blueprint(slug)
    heat_start = None
    try:
        import heat
        hs = (heat.state(slug) or {}).get("starts")
        heat_start = date.fromisoformat(hs) if hs else None
    except Exception:
        heat_start = None
    macro_p = BASE / "athletes" / slug / "reference" / "macro-plan.json"

    out = project_block(cfg, bp, ctl, today=on_date,
                        required_fn=pt.required_tss,
                        ceiling_for=lambda ws, phase: _weekly_tss_cap(slug, phase),
                        heat_start=heat_start, last_week_tss=last_week,
                        has_macro_plan=macro_p.exists())
    out["athlete"] = slug
    return out


def _render(rep: dict) -> str:
    if rep.get("error"):
        return f"{rep.get('athlete')}: ERROR {rep['error']}"
    L = [f"── {rep['athlete']} · race {rep['race_date']} · CTL now {rep['ctl_now']} "
         f"· race_min {rep['race_min_ctl']} ──",
         f"{'week':<12}{'phase':<10}{'type':<9}{'target':>7}{'ceil':>7}{'build':>7}"
         f"{'CTL→':>7}"]
    for k in rep["weeks"]:
        ceil = f"{k['phase_tss_ceiling']:.0f}" if k["phase_tss_ceiling"] else "-"
        mark = "!" if k["ceiling_infeasible"] else ("~" if k["ramp_limited"] else " ")
        L.append(f"{k['week_start']:<12}{(k['phase'] or '')[:9]:<10}"
                 f"{(k['week_type'] or '')[:8]:<9}{k['engine_target_tss']:>7}{ceil:>7}"
                 f"{k['buildable_tss']:>7}{k['ctl_end']:>7}{mark}")
    L.append(f"projected CTL: taper start {rep['ctl_at_taper_start']} · race week "
             f"{rep['ctl_at_race_week_start']} · loading weeks left "
             f"{rep['loading_weeks_remaining']}")
    if rep["ctl_at_race_week_start_at_ceiling"] != rep["ctl_at_race_week_start"]:
        L.append(f"  ...that spends the +{rep['cap_tolerance']:.0%} cap tolerance every "
                 f"week; held strictly at the ceiling: taper start "
                 f"{rep['ctl_at_taper_start_at_ceiling']} · race week "
                 f"{rep['ctl_at_race_week_start_at_ceiling']}")
    for s in rep.get("skipped") or []:
        L.append(f"  SKIPPED {s}")
    for f in rep["flags"]:
        L.append(f"  [{f['severity'].upper()}] {f['code']}: {f['detail']}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Read-only macro (block) feasibility "
                                             "projection. Writes nothing.")
    ap.add_argument("--athlete")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ctl-today", type=float)
    ap.add_argument("--date", help="project as of this date (default today)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    athletes = json.loads(ATHLETES_CONFIG.read_text())
    slugs = ([s for s, c in athletes.items() if c.get("active", True)] if args.all
             else [args.athlete])
    if not slugs or slugs == [None]:
        raise SystemExit("pass --athlete <slug> or --all")
    on_date = date.fromisoformat(args.date) if args.date else None
    reports = []
    for slug in slugs:
        try:
            reports.append(_run(slug, athletes[slug], args.ctl_today, on_date))
        except Exception as e:
            reports.append({"athlete": slug, "error": f"{type(e).__name__}: {e}"})
    if args.json:
        print(json.dumps(reports, indent=1, ensure_ascii=False))
    else:
        print("\n\n".join(_render(r) for r in reports))
    sys.exit(1 if any(r.get("hard_flag") for r in reports) else 0)


if __name__ == "__main__":
    main()
