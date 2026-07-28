#!/usr/bin/env python3
"""Plan audit (Layer 4) — the self-check / trust mechanism.

Asserts the planning invariants against an athlete's LIVE intervals.icu calendar,
so the SYSTEM catches drift instead of the athlete finding it in a session. Run
standalone (cron) or right after a generation/replan.

Invariants (per the planning architecture doc):
  STRUCTURE   every Swim/Run/Ride/Brick session has structured workout steps
              (workout_doc.steps non-empty) — i.e. it will sync to Garmin as a
              follow-along workout, not a prose note.
  FUELLING    every >90-min ride/brick states the deterministic fuel-target g/hr.
  LONG_RIDE   no ride exceeds the event-anchored long-ride ceiling.
  WEEKLY_LOAD weekly planned TSS is within tolerance of the phase target.
  RULES       day_rules / CTL ramp / strength cap / intensity distribution
              (delegated to validate_week — the same backstop the generator uses).

Usage:
  python3 plan_audit.py --athlete jamie            # current + next week
  python3 plan_audit.py --all                      # every active athlete
Exit code 0 = clean, 1 = at least one hard invariant failed (for cron alerting).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "ironman-analysis"))
sys.path.insert(0, str(BASE / "lib"))

from primitives.validate_plan import validate_week, escalate_repeats  # noqa: E402
from primitives.blueprint import current_phase                # noqa: E402
from primitives.nutrition import fuel_target, recent_avg_g_hr  # noqa: E402
import plan_tools as pt                                        # noqa: E402
import ops_log                                                 # noqa: E402

ATHLETES = BASE / "config" / "athletes.json"
# Known-baseline signatures (committed, athlete-slug keyed). This check currently
# hard-fails for all three athletes BY DESIGN — docs/plan-audit-status.md lists the
# four real defects behind it. Unconditional ops_log.alert() therefore wrote three
# ok=False entries into run-status.jsonl EVERY DAY, i.e. it poisoned the very
# heartbeat store the failure alarm reads: a permanently-failing job trains the
# reader to ignore ✗ lines. So a failure whose category signature MATCHES the
# baseline is recorded as a success with the signature in the detail; a signature
# that DIFFERS is still a real alert. New breakage is still visible; the known
# backlog is not re-reported daily.
BASELINE = BASE / "config" / "plan-audit-baseline.json"
# Consecutive-run counters for recurring SOFT advisories, so a flag pushed past every
# week escalates instead of sitting inside the count-keyed baseline forever (see
# validate_plan.escalate_repeats). Deliberately NOT under athletes/ - stage1-plan.py
# owns that directory's sidecars. Best-effort: a missing/corrupt file just means no
# streaks yet, never a failed audit.
STREAKS = BASE / "config" / "plan-audit-streaks.json"


def _load_streaks(slug: str) -> dict:
    try:
        return (json.loads(STREAKS.read_text()).get("athletes", {}).get(slug) or {})
    except Exception:
        return {}


def _save_streaks(slug: str, streaks: dict) -> None:
    try:
        blob = json.loads(STREAKS.read_text()) if STREAKS.exists() else {}
    except Exception:
        blob = {}
    blob.setdefault("_note", "Consecutive-run counts per soft violation code, per "
                             "athlete. Written by plan_audit; read by "
                             "validate_plan.escalate_repeats to escalate a flag that "
                             "recurs. A clean run drops the code and breaks the streak.")
    blob.setdefault("athletes", {})[slug] = streaks
    try:
        STREAKS.write_text(json.dumps(blob, indent=1, sort_keys=True) + "\n")
    except Exception:
        pass
_STRUCTURED_SPORTS = {"Swim", "Run", "Ride", "GravelRide", "VirtualRide", "Brick"}
_FUEL_SPORTS = {"Ride", "GravelRide", "VirtualRide", "Brick"}
_LOAD_TOLERANCE = 0.15
_FUEL_TOLERANCE_G_HR = 5  # fuel_target() rounds to the nearest 5 g/hr; match that granularity


def _client(cfg):
    from icu_api import IcuClient
    return IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])


def _dur_min(ev) -> int:
    """Best-effort planned duration in minutes: moving_time, else parse the name."""
    mt = ev.get("moving_time")
    if mt:
        return int(mt / 60)
    name = (ev.get("name") or "") + " " + (ev.get("description") or "")
    m = re.search(r"(\d+)\s*h(?:r|our)?s?\s*(?:(\d+)\s*m)?", name, re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"(\d+)\s*min", name, re.I)
    return int(m.group(1)) if m else 0


def audit_athlete(slug: str, cfg: dict, weeks: int = 2) -> dict:
    client = _client(cfg)
    today = date.today()
    win_start = today - timedelta(days=today.weekday())
    win_end = win_start + timedelta(days=7 * weeks - 1)
    events = [e for e in client.get_events(win_start.isoformat(), win_end.isoformat())
              if e.get("category") == "WORKOUT"]
    wellness = client.get_wellness(days=3)
    ctl = round(float(wellness[-1].get("ctl") or 0), 1) if wellness else None
    sl_path = BASE / "athletes" / slug / "session-log.json"
    fuel = fuel_target(recent_avg_g_hr(json.loads(sl_path.read_text()) if sl_path.exists() else []),
                       int(cfg.get("nutrition_target_g_hr") or 90))
    bike_min = (cfg.get("race_target_splits") or {}).get("bike_min")
    lr_ceiling = min(int(round(bike_min * 1.15 / 15) * 15), 300) if bike_min else None

    # SKIPPED is a distinct, non-failure category: checks that could not run at all
    # (missing input, insufficient data) surfaced separately from RULES violations,
    # so "not checked" never reads as "checked and passed". Never counts toward
    # hard_fail — a skip is not a failure — but IS counted in `ok`, same as the
    # other soft/warn categories below: a run with only skips is visibly not clean.
    fails = {"STRUCTURE": [], "FUELLING": [], "LONG_RIDE": [], "WEEKLY_LOAD": [], "RULES": [],
             "SKIPPED": []}

    for e in events:
        sport = e.get("type") or ""
        nm = (e.get("name") or "")[:48]
        steps = len((e.get("workout_doc") or {}).get("steps") or [])
        desc = e.get("description") or ""
        dur = _dur_min(e)
        if sport in _STRUCTURED_SPORTS and steps == 0:
            fails["STRUCTURE"].append(f"{nm} ({sport}) — no structured steps")
        if sport in _FUEL_SPORTS and dur >= 90:
            g = re.findall(r"(\d+)\s*g\s*(?:CHO\s*)?/?\s*hr", desc, re.I)
            if not g:
                fails["FUELLING"].append(f"{nm} — no fuelling stated (expect {fuel} g/hr)")
            elif not any(abs(int(x) - fuel) <= _FUEL_TOLERANCE_G_HR for x in g):
                fails["FUELLING"].append(f"{nm} — states {g} g/hr, expected {fuel}")
        if sport in _FUEL_SPORTS and lr_ceiling and dur > lr_ceiling:
            fails["LONG_RIDE"].append(f"{nm} — {dur}min > {lr_ceiling}min event ceiling")

    # Per-week: load vs target + validate_week rules.
    for wk in range(weeks):
        ws = win_start + timedelta(days=7 * wk)
        wk_evs = [e for e in events if ws.isoformat() <= (e.get("start_date_local") or "")[:10]
                  <= (ws + timedelta(days=6)).isoformat()]
        total = sum(int(e.get("icu_training_load") or e.get("load_target") or 0) for e in wk_evs)
        if ctl:
            # For the CURRENT week pass last week's actual load so a miss-triggered
            # recovery week audits against the same reduced target the generator
            # used; future weeks can only know the deterministic cadence deloads.
            lw = pt.last_week_actual_tss(client) if ws <= date.today() <= ws + timedelta(days=6) else None
            req = pt.required_tss(cfg, ctl, today=ws, last_week_tss=lw)
            tgt = req.get("recommended_weekly_tss")
            if tgt and abs(total - tgt) > tgt * _LOAD_TOLERANCE:
                fails["WEEKLY_LOAD"].append(
                    f"week {ws}: {total} TSS vs target ~{tgt} (>{int(_LOAD_TOLERANCE*100)}% off)")
        dr = cfg.get("day_rules")
        phase = current_phase(pt._load_blueprint(slug), ws) or {}
        rep = validate_week(wk_evs, ws, day_rules=dr, ctl_today=ctl,
                            ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
                            strength_max=(dr or {}).get("strength_max"),
                            distribution=phase.get("distribution"))
        # Escalate soft advisories that keep recurring. Only the CURRENT week updates the
        # streak store: the next-week audit re-runs against the same plan a week later and
        # would otherwise double-count a single occurrence.
        viols = rep.violations
        if wk == 0:
            viols, new_streaks = escalate_repeats(rep.violations, _load_streaks(slug))
            _save_streaks(slug, new_streaks)
        for v in viols:
            # Prefix match: the distribution check now emits per-zone ceiling codes and a
            # "_persistent" escalation alongside plain intensity_distribution.
            if v.severity == "hard" or v.code.startswith("intensity_distribution"):
                fails["RULES"].append(f"week {ws}: {v}")
        # rep.skipped covers EVERY hard check validate_week couldn't run this week
        # (weekly_tss_cap/floor, ctl_ramp, run_weekly_volume, intensity_distribution
        # gates, ...) — surface all of it, not just the distribution reasons.
        for s in rep.skipped:
            fails["SKIPPED"].append(f"week {ws}: {s}")

    hard = any(fails[k] for k in ("STRUCTURE", "LONG_RIDE", "RULES"))  # fuelling/load/skipped = warn
    return {"athlete": slug, "window": f"{win_start}..{win_end}", "fuel_target": fuel,
            "long_ride_ceiling_min": lr_ceiling, "ok": not any(fails.values()),
            "hard_fail": hard, "fails": {k: v for k, v in fails.items() if v}}


def counts(report: dict) -> dict:
    """Failure counts per category — the fingerprint the baseline is keyed on.

    Category names + counts, never the per-item strings: those embed week dates
    ("week 2026-08-03: 0 TSS vs ...") so they change every week even when the
    underlying defect is identical, and a fingerprint that moves weekly is no
    baseline at all.
    """
    if report.get("error"):
        return {f"error:{report['error'].split(':')[0]}": 1}
    return {k: len(v) for k, v in sorted((report.get("fails") or {}).items()) if v}


def signature(report: dict) -> str:
    c = counts(report)
    return ",".join(f"{k}={n}" for k, n in c.items()) or "none"


def _load_baseline() -> dict:
    try:
        return json.loads(BASELINE.read_text()).get("athletes", {})
    except Exception:
        return {}


def within_baseline(report: dict, accepted: dict | None) -> bool:
    """True when today's failures are all covered by the accepted baseline.

    Counts, not equality: a category dropping BELOW its accepted count is an
    improvement and must not alert, while a NEW category or a HIGHER count is
    something that got worse today and must. Equality would alert every time a
    defect was partly fixed — a fix that pages you is a fix nobody ships.
    """
    if not accepted:
        return False
    return all(n <= int(accepted.get(cat, -1)) for cat, n in counts(report).items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--write-baseline", action="store_true",
                    help="record the current signatures as the accepted baseline")
    args = ap.parse_args()
    athletes = json.loads(ATHLETES.read_text())
    slugs = ([s for s, c in athletes.items() if c.get("active", True)] if args.all
             else [args.athlete])
    baseline = _load_baseline()
    reports, any_hard = [], False
    for slug in slugs:
        try:
            r = audit_athlete(slug, athletes[slug], args.weeks)
        except Exception as e:
            r = {"athlete": slug, "error": f"{type(e).__name__}: {e}", "hard_fail": True}
        any_hard = any_hard or r.get("hard_fail")
        sig = signature(r)
        r["signature"] = sig
        reports.append(r)
        # Log-only alerting (ops chatter is log-only from 27 Jul 2026; no Telegram
        # routing here — an ops-alerts.log entry is picked up by the evening digest).
        # Baseline-gated from 28 Jul 2026: a hard fail matching the accepted
        # baseline signature is a KNOWN outstanding defect, not today's news, so it
        # records ok=True and does not spend an alert. Anything else — a different
        # signature, a new category, a higher count, or a clean-to-broken flip —
        # still alerts.
        if r.get("hard_fail"):
            detail = r.get("error") or "; ".join(
                f"{k}: {len(v)}" for k, v in (r.get("fails") or {}).items() if v)
            if within_baseline(r, baseline.get(slug)):
                ops_log.record_run("plan_audit", athlete=slug, ok=True,
                                   detail=f"known baseline fail [{sig}] — see "
                                          f"docs/plan-audit-status.md")
            else:
                was = baseline.get(slug) or "(no baseline)"
                ops_log.alert("plan_audit", f"{detail or 'hard invariant failed'} "
                                            f"[signature {sig}, baseline {was}]",
                              athlete=slug)
        else:
            ops_log.record_run("plan_audit", athlete=slug, ok=True)
    if args.write_baseline:
        BASELINE.write_text(json.dumps(
            {"_note": "Accepted plan_audit failure counts per category. A run at or "
                      "below its athlete's accepted counts logs a success; a new "
                      "category or a higher count alerts. Shrink these as the defects "
                      "in docs/plan-audit-status.md are fixed (or regenerate with "
                      "plan_audit.py --all --write-baseline); an empty map for an "
                      "athlete means every hard fail alerts.",
             "written": date.today().isoformat(),
             "athletes": {r["athlete"]: counts(r) for r in reports}},
            indent=1) + "\n")
        print(f"baseline written to {BASELINE}", file=sys.stderr)
    print(json.dumps(reports, indent=1, ensure_ascii=False))
    sys.exit(1 if any_hard else 0)


if __name__ == "__main__":
    main()
