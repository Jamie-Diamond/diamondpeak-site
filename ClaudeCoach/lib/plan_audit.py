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

A HARD fail also alerts the coach on Telegram, once per distinct finding — see
notify_hard_fail(). Soft findings stay log-only. Until 17 Aug 2026 everything
here was log-only, which is how three correct hard fails on a live calendar
produced no consequence for ten days.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "ironman-analysis"))
sys.path.insert(0, str(BASE / "lib"))

from primitives.validate_plan import validate_week, escalate_repeats  # noqa: E402
from primitives.blueprint import current_phase                # noqa: E402
from primitives.nutrition import fuel_target, recent_avg_g_hr  # noqa: E402
from primitives.planned_tss import name_intensity_mismatch     # noqa: E402
import plan_tools as pt                                        # noqa: E402
from plan_builder import _weekly_tss_cap                       # noqa: E402
# IMPORTED, not restated: which of the hours ceiling / ramp-permitted maximum is the
# lower bound is computed in exactly one place. See its docstring for why.
from macro_projection import binding_constraint                # noqa: E402
import ops_log                                                 # noqa: E402
import coach_alert                                             # noqa: E402
import day_overrides                                           # noqa: E402
import weekly_availability                                     # noqa: E402

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
# Which hard findings the coach has ALREADY been told about, keyed on the finding's
# own identity (athlete + week + rule code) and not on a timestamp. This is what
# stops the 06:25 run alerting every morning about the same unfixed week while
# still alerting the moment a finding is new or changed. Under LOG_DIR, alongside
# coach-alert-state.json: it is per-machine send state, not configuration, and it
# must not produce a commit every day.
ALERTED = ops_log.LOG_DIR / "plan-audit-alerted.json"


def _load_alerted(slug: str) -> dict:
    try:
        return (json.loads(ALERTED.read_text()).get("athletes", {}).get(slug) or {})
    except Exception:
        return {}


def _save_alerted(slug: str, ids: dict) -> None:
    try:
        blob = json.loads(ALERTED.read_text()) if ALERTED.exists() else {}
    except Exception:
        blob = {}
    blob.setdefault("_note", "Hard plan_audit findings already alerted to the coach, "
                             "per athlete: identity -> when first alerted. An identity "
                             "no longer on the calendar is dropped, so a finding that is "
                             "fixed and later re-broken alerts again.")
    blob.setdefault("athletes", {})[slug] = ids
    try:
        ALERTED.parent.mkdir(parents=True, exist_ok=True)
        ALERTED.write_text(json.dumps(blob, indent=1, sort_keys=True) + "\n")
    except Exception:
        pass


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


# The only categories that BLOCK. Everything else (fuelling, weekly load, soft
# distribution, skipped checks, coach-directed deviations) is a warning: it is reported
# and fingerprinted, but does not fail the audit. Kept as a named function so the
# grading is testable rather than an inline any() nobody can reach.
_HARD_CATEGORIES = ("STRUCTURE", "LONG_RIDE", "RULES")


def is_hard(fails: dict) -> bool:
    """True when a blocking category has anything in it."""
    return any(fails.get(k) for k in _HARD_CATEGORIES)


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
    # DIRECTED is a fourth non-failure category, alongside SKIPPED: a day_rules
    # deviation the COACH ASKED FOR (athletes/<slug>/reference/day-rules-overrides.json).
    # Reported, never hard — day_rules describe the normal pattern and the coach
    # overrides them conversationally. An UNDIRECTED deviation is still a hard RULES
    # failure, and DRIFT_THRESHOLD directed hits on one weekday raise a hard
    # day_rules_drifted. Counted in `ok`, so a run carrying overrides is visibly not
    # clean, and listed in plan-audit-baseline.json (a populated category with no
    # baseline entry alerts every run — the 28 Jul SKIPPED bug).
    # Informational, never fingerprinted (see counts()): observations that need to be
    # visible daily without moving the baseline or spending an alert.
    notes: list[str] = []
    # DISTRIBUTION (4 Aug 2026) is the SOFT per-zone intensity-distribution signal.
    # Like SKIPPED and DIRECTED it MUST carry a baseline entry even at 0, or
    # get(cat,-1) rejects every run and alerts daily.
    fails = {"STRUCTURE": [], "FUELLING": [], "LONG_RIDE": [], "WEEKLY_LOAD": [], "RULES": [],
             "DISTRIBUTION": [], "SKIPPED": [], "DIRECTED": []}
    # Stable identity per HARD finding, built where the finding is raised rather than
    # re-parsed out of the prose afterwards. Deliberately NOT the counts fingerprint
    # counts()/signature() build: the baseline needs a key that does not move week to
    # week, and the alert dedup needs the opposite — the week IS the identity, so next
    # week's recurrence of the same rule is a new finding and alerts again.
    hard_ids: list[str] = []

    for e in events:
        sport = e.get("type") or ""
        nm = (e.get("name") or "")[:48]
        day = (e.get("start_date_local") or "")[:10]
        steps = len((e.get("workout_doc") or {}).get("steps") or [])
        desc = e.get("description") or ""
        dur = _dur_min(e)
        if sport in _STRUCTURED_SPORTS and steps == 0:
            fails["STRUCTURE"].append(f"{nm} ({sport}) — no structured steps")
            hard_ids.append(f"STRUCTURE:{day}:no_structured_steps:{nm}")
        # Named hard, built easy. validate_week blocks this at plan time, but a
        # session can also reach the calendar by chat edit or a hand push, so the
        # daily audit checks what is actually ON the calendar (4 Aug 2026).
        mm = name_intensity_mismatch(sport, nm, desc)
        if mm:
            fails["STRUCTURE"].append(
                f"{nm} ({sport}) — name claims {mm['claim']} but hardest step is "
                f"{mm['found']}%, short of {mm['required']}%")
            hard_ids.append(f"STRUCTURE:{day}:name_intensity_mismatch:{nm}")
        if sport in _FUEL_SPORTS and dur >= 90:
            g = re.findall(r"(\d+)\s*g\s*(?:CHO\s*)?/?\s*hr", desc, re.I)
            if not g:
                fails["FUELLING"].append(f"{nm} — no fuelling stated (expect {fuel} g/hr)")
            elif not any(abs(int(x) - fuel) <= _FUEL_TOLERANCE_G_HR for x in g):
                fails["FUELLING"].append(f"{nm} — states {g} g/hr, expected {fuel}")

    # Per-week: load vs target + validate_week rules.
    for wk in range(weeks):
        ws = win_start + timedelta(days=7 * wk)
        wk_evs = [e for e in events if ws.isoformat() <= (e.get("start_date_local") or "")[:10]
                  <= (ws + timedelta(days=6)).isoformat()]
        total = sum(int(e.get("icu_training_load") or e.get("load_target") or 0) for e in wk_evs)
        req = None
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
        # The day rules for THIS week, declaration included. Same resolution as
        # plan_builder, for the same reason `week_start=ws` is required on the cap below:
        # the audit must resolve what the GENERATOR resolved. Auditing a declared week
        # against the standing config retro-flags a week that was built exactly as the
        # athlete asked, and a permanently red check is one nobody reads.
        dr, _ = weekly_availability.effective_day_rules(
            slug, ws, cfg.get("day_rules"),
            run_limited=((cfg.get("run_protocol") or {}).get("quality_allowed") is False))
        phase = current_phase(pt._load_blueprint(slug), ws) or {}
        # ARM the three hard checks that were never given an input (28 Jul 2026).
        # weekly_tss_cap / weekly_tss_floor / run_weekly_volume were reported SKIPPED
        # on every run for every athlete since this file was written, i.e. Layer 4 has
        # never once checked a week's total load or its run volume. Each input comes
        # from the SAME function the generation path uses (plan_builder.build_week /
        # plan_tools.cmd_validate), so the audit can never disagree with the generator
        # about where the limit is:
        #   cap   — plan_builder._weekly_tss_cap: the hours the athlete DECLARED for
        #           this week (weekly_availability), else profile.max_hours_per_week,
        #           else the blueprint phase's tss_ceiling. None on a taper (no source
        #           carries one) — a legitimate skip, still reported as skipped.
        #           `week_start=ws` is REQUIRED, not decorative: without it the audit
        #           would resolve a declared week against the static config ceiling and
        #           hard-flag a week the generator built entirely correctly. Passing ws
        #           is also why declarations EXPIRE by named week rather than by
        #           deletion — this loop audits the current and next week every morning.
        #   floor — required_tss()['weekly_tss_floor'], the same key the builder uses.
        #           That function returns 0 for a deload / taper / manual easy week, so
        #           an INTENTIONAL down-week scores no violation; it must not be
        #           replaced with recommended_weekly_tss, which would call every deload
        #           under-training. None (no CTL, or a branch that omits the key) skips.
        #   run   — plan_tools.run_caps()['weekly_min_cap']: max(recent 4-week max,
        #           the run_protocol floor) x the protocol ramp. All-None on a history
        #           fetch failure, which the validator reports as skipped.
        # run_long_min_cap is deliberately NOT passed: arming run_long_volume is a
        # separate check and validate_week emits no skip line for it, so it would go
        # from invisible to hard-failing with no before-state to compare against.
        tss_floor = req.get("weekly_tss_floor") if req else None
        # A WEEK THAT HAS NOT BEEN BUILT YET IS NOT AN UNDER-TRAINED WEEK (17 Aug 2026).
        # scripts/weekly-plan.sh runs on cron at Sunday 18:00 and builds only the COMING
        # week, so the second week of this 14-day window is legitimately empty from Monday
        # morning until that Sunday-evening run. The armed floor had no notion of "not
        # generated yet", so every 06:25 run scored that empty future week as
        # "UNDER-TRAINING: planned 0 TSS ... this week DETRAINS the athlete": the week of
        # 2026-08-17 was flagged on 16 consecutive runs and is now fully planned, and one
        # run flagged all three athletes for the week of 24 Aug. Since eb5b2dbe a hard fail
        # messages Jamie, so the artefact would page him once per athlete per future week,
        # for ever, on the same channel that carries the real hard fails - which is exactly
        # the "alarm the reader learns to dismiss" that notify_hard_fail() exists to avoid.
        # So a FUTURE week with ZERO planned sessions declares no floor (0 is the
        # primitive's own "explicitly no floor"; validate_plan stays pure and knows nothing
        # about generation timing) and says so in a note instead. Deliberately NOT in
        # `fails`: counts()/signature() fingerprint `fails`, so an advisory in there would
        # raise the baseline and spend an alert on a week nobody has built yet.
        # Narrow on purpose. The CURRENT week is checked exactly as before, because an
        # empty current week at Monday 06:25 is a REAL failure twelve hours after the
        # generator should have run, and a future week that HAS sessions summing under the
        # floor still hard-fails. Only the zero-session case is suppressed.
        if tss_floor and not wk_evs and ws > win_start:      # win_start IS this Monday
            notes.append(
                f"week {ws}: NOT GENERATED YET - zero planned sessions, so the "
                f"{tss_floor:.0f} TSS floor is not asserted against it. The weekly "
                f"generator builds this week on Sunday evening; if it is still empty "
                f"after that run, that is the failure worth reporting")
            tss_floor = 0
        try:
            run_cap = pt.run_caps(client, ws,
                                  run_protocol=cfg.get("run_protocol")).get("weekly_min_cap")
        except Exception:
            run_cap = None
        # ONE cap value, used both by validate_week below and by the binding-constraint
        # note — resolving it twice is how the ceiling and the thing reporting on the
        # ceiling drift apart.
        wk_cap = _weekly_tss_cap(slug, phase, week_start=ws)
        # WHICH CONSTRAINT GOVERNS. Not a failure and deliberately NOT in `fails`:
        # counts()/signature() fingerprint `fails` only, and a standing configuration
        # conflict would otherwise raise the baseline for five consecutive weeks and
        # alert on a fact the coach already knows. It is a note the 06:25 run surfaces
        # every morning; lib/macro_projection.py is the full block-level treatment.
        if req and req.get("ramp_capped_weekly_tss") and wk_cap:
            _b = binding_constraint(wk_cap, req.get("ramp_capped_weekly_tss"))
            if _b["binding"] == "hours":
                notes.append(
                    f"week {ws}: HOURS-BOUND — the hours-derived ceiling "
                    f"{_b['ceiling']:.0f} TSS is {_b['gap_tss']:.0f} below the "
                    f"{_b['ramp_permitted']:.0f} TSS this athlete's own "
                    f"+{cfg.get('max_ctl_ramp_per_week')}/wk CTL-ramp cap permits, so "
                    f"available time and not fatigue is limiting the week. Either the "
                    f"athlete is genuinely time-limited (the CTL target must come down) "
                    f"or the hours figure is stale — confirm with the athlete; no "
                    f"setting is changed here")
            else:
                notes.append(
                    f"week {ws}: ramp-bound ({_b['ramp_permitted']:.0f} TSS permitted "
                    f"under a {_b['ceiling']:.0f} ceiling) — working as designed, "
                    f"fatigue accumulation is the limiter")
        rep = validate_week(wk_evs, ws, day_rules=dr,
                            day_overrides=day_overrides.load(slug, BASE), ctl_today=ctl,
                            weekly_tss_cap=wk_cap,
                            weekly_tss_floor=tss_floor,
                            run_week_min_cap=run_cap,
                            ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
                            strength_max=(dr or {}).get("strength_max"),
                            distribution=phase.get("distribution"),
                        # Was an inline LONG_RIDE check here; validate_week now owns it
                        # so plan time and calendar time cannot disagree, and a breach is
                        # reported once. `or 300` keeps the absolute ceiling armed for an
                        # athlete with no race bike split (else it would land in SKIPPED
                        # and alert daily).
                        long_ride_max_min=(lr_ceiling or 300))
        # Escalate soft advisories that keep recurring. Only the CURRENT week updates the
        # streak store: the next-week audit re-runs against the same plan a week later and
        # would otherwise double-count a single occurrence.
        viols = rep.violations
        if wk == 0:
            # Pass the WEEK: the streak counts distinct weeks, not audit runs, so this
            # daily job cannot escalate an unchanged plan just by looking at it again.
            viols, new_streaks = escalate_repeats(rep.violations, _load_streaks(slug),
                                                  week=ws.isoformat())
            _save_streaks(slug, new_streaks)
        for v in viols:
            # Prefix match: the distribution check now emits per-zone ceiling codes and a
            # "_persistent" escalation alongside plain intensity_distribution.
            if v.code.endswith("_directed_day"):
                fails["DIRECTED"].append(f"week {ws}: {v}")
            elif v.severity == "hard":
                fails["RULES"].append(f"week {ws}: {v}")
                hard_ids.append(f"RULES:{ws}:{v.code}")
            elif v.code.startswith("intensity_distribution"):
                # SOFT distribution signals belong in their own warn category. They
                # used to be appended to RULES to make them visible, but `hard` is
                # any(RULES), so a violation the validator itself marked [soft] came
                # back as hard_fail — a distribution warning blocked, against the
                # standing rule that warnings are not hard rules. Found 4 Aug 2026
                # only because canonicalising Kathryn's step bands let the check
                # ASSERT instead of landing in SKIPPED, so it had never fired.
                fails["DISTRIBUTION"].append(f"week {ws}: {v}")
        # rep.skipped covers EVERY hard check validate_week couldn't run this week
        # (weekly_tss_cap/floor, ctl_ramp, run_weekly_volume, intensity_distribution
        # gates, ...) — surface all of it, not just the distribution reasons.
        for s in rep.skipped:
            fails["SKIPPED"].append(f"week {ws}: {s}")

    hard = is_hard(fails)
    return {"athlete": slug, "window": f"{win_start}..{win_end}", "fuel_target": fuel,
            "long_ride_ceiling_min": lr_ceiling, "ok": not any(fails.values()),
            "hard_fail": hard, "fails": {k: v for k, v in fails.items() if v},
            "hard_ids": sorted(hard_ids), "notes": notes}


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


_MAX_ALERT_FINDINGS = 6
_MAX_FINDING_CHARS = 320


def hard_lines(report: dict) -> list[str]:
    """The BLOCKING findings verbatim, in the validator's own words.

    Only _HARD_CATEGORIES, so the soft categories a warning lives in (DISTRIBUTION,
    FUELLING, WEEKLY_LOAD, SKIPPED, DIRECTED) can never reach a message: the whole
    point of the 4 Aug split was that a warning does not block, and a warning that
    interrupts the coach is the same defect wearing a different coat.
    """
    out = []
    for cat in _HARD_CATEGORIES:
        for item in (report.get("fails") or {}).get(cat, []):
            # "[hard]" is the validator's severity marker for the log; the message
            # already says these are the blocking ones.
            txt = item.replace("[hard] ", "")
            out.append(txt if txt.startswith("week ") else f"{cat.lower()}: {txt}")
    return out


def alert_text(report: dict) -> str:
    """The coach-facing message: which athlete, which week, which rules.

    A bare "audit failed" is what a log line already is. This names the thing to
    change, because the athlete is following the calendar literally every morning
    until someone changes it.
    """
    slug = str(report.get("athlete") or "?")
    lines = hard_lines(report)
    head = (f"⚠️ {slug.title()}'s live plan breaks {len(lines)} hard "
            f"planning rule{'s' if len(lines) != 1 else ''}. That calendar is what "
            f"they are training to now, so it needs a plan change, not a log entry.")
    shown = [ln if len(ln) <= _MAX_FINDING_CHARS else ln[:_MAX_FINDING_CHARS - 1] + "…"
             for ln in lines[:_MAX_ALERT_FINDINGS]]
    body = "\n".join(f"• {ln}" for ln in shown)
    if len(lines) > _MAX_ALERT_FINDINGS:
        body += f"\n• and {len(lines) - _MAX_ALERT_FINDINGS} more"
    tail = "Full detail: plan-audit.log on the VM."
    # Coach-facing text: house style is no em-dashes, and the validator's details
    # are written for the log.
    return f"{head}\n{body}\n{tail}".replace("—", "-").replace("–", "-")


def _fingerprint(ids: list[str]) -> str:
    return hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()[:12]


def notify_hard_fail(report: dict) -> str:
    """Alert the coach about a hard fail, at most once per distinct finding.

    Dedup is on the findings' IDENTITIES (athlete + week + rule), not on when they
    were seen: the 06:25 cron re-detects an unfixed week every morning, and an alarm
    that repeats daily is one the reader learns to dismiss — which is how a real
    hard fail (Kathryn, 17 Aug 2026) went ten days without a human seeing it.
    A NEW or CHANGED finding has an identity nobody has been told about, so it
    alerts immediately.

    Identities no longer on the calendar are DROPPED, so a finding that is fixed
    and later re-broken is new again rather than silenced for ever. Nothing is
    recorded on "send-failed": a Telegram error must leave the finding unreported
    so the next run retries it, the same reasoning as coach_alert.send()'s own
    refusal to bank a cooldown for a message that did not go.

    Returns the action taken, or "known" when every current finding has already
    been alerted.
    """
    slug = report["athlete"]
    # An audit that could NOT RUN carries hard_fail=True so the cron exit code stays
    # honest, but it is not a breached rule: there is nothing for the coach to change,
    # and an intervals.icu blip or a DNS hiccup would message him for nothing. A run
    # that failed to do its job is FAILURE-class digest material, which the
    # ops_log.alert above this call has already written.
    if report.get("error"):
        return "error"
    ids = sorted(report.get("hard_ids") or [])
    if not ids:
        # hard_fail with no identity recorded: a hard category gaining items without
        # an id. Fall back to the signature so a blocking finding can never be
        # dropped by the dedup itself.
        ids = [f"signature:{report.get('signature')}"]
    seen = _load_alerted(slug)
    if all(i in seen for i in ids):
        _save_alerted(slug, {i: seen[i] for i in ids})
        return "known"
    action = coach_alert.send(coach_alert.PLAN_HARD_FAIL, alert_text(report),
                              key=f"{slug}|{_fingerprint(ids)}")
    if action in ("sent", "dry-run", "cooldown"):
        now = datetime.now().isoformat(timespec="seconds")
        _save_alerted(slug, {i: seen.get(i, now) for i in ids})
    return action


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
        # Then, and separately, TELL SOMEONE. Everything above this point writes to
        # disk; on 17 Aug 2026 that was the entire consequence of three correct hard
        # fails on Kathryn's live calendar. Deliberately NOT gated on the baseline
        # signature: the baseline is a count per category, so a hard breach inside an
        # accepted count would stay silent, and the identity dedup below is the
        # anti-spam mechanism the baseline was being stretched to provide.
        if r.get("hard_fail"):
            try:
                action = notify_hard_fail(r)
                print(f"coach-alert plan_hard_fail:{slug}: {action}", file=sys.stderr)
            except coach_alert.TelegramSendBlocked:
                # The guard that fails a test which would have messaged the coach for
                # real. Never swallowed.
                raise
            except Exception as e:
                # The audit's own output is the primary artefact: alerting must not be
                # able to break it. "coach-alert" means the alarm could not reach
                # Jamie, which is what this is.
                ops_log.alert("coach-alert", f"plan_audit hard fail for {slug} could "
                                             f"NOT be alerted: {type(e).__name__}: {e}",
                              athlete=slug)
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
