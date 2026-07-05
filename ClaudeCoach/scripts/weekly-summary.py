#!/usr/bin/env python3
"""
Weekly training summary — fetches IcuSync data directly, calls Claude API, sends to Telegram.
Replaces the MCP-dependent weekly-summary.sh flow.
Run: python3 weekly-summary.py [--athlete jamie]
Also called from weekly-summary.sh for cron compatibility.
"""
import json, ssl, subprocess, sys, urllib.request, urllib.error
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "lib"))

import claude_call
from icu_api import IcuClient
import recovery_score as rs
sys.path.insert(0, str(BASE / "ironman-analysis"))
from primitives.planned_tss import planned_session_tss
from coaching_levels import level_block as _level_block

ATHLETES_CONFIG = BASE / "config/athletes.json"
TG_CONFIG       = BASE / "telegram/config.json"
CLAUDE          = "/usr/bin/claude"
PROJECT_DIR     = str(BASE.parent)
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
TOOLS           = "Write,Bash"


def _load_client(slug: str):
    cfg = json.loads(ATHLETES_CONFIG.read_text())
    a = cfg[slug]
    return IcuClient(a["icu_athlete_id"], a["icu_api_key"]), a["chat_id"]


def _tg_send(chat_id: str, text: str):
    try:
        cfg = json.loads(TG_CONFIG.read_text())
        token = cfg["bot_token"]
        cafile = "/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None
        ctx = ssl.create_default_context(cafile=cafile)

        def _post(body: dict):
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10, context=ctx)

        for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
            try:
                _post({"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"})
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    _post({"chat_id": chat_id, "text": chunk})
                else:
                    raise
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)


def _read_file(path: Path, default="(not found)") -> str:
    try:
        return path.read_text()
    except Exception:
        return default


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else []


def run_summary(slug: str = "jamie") -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "weekly-summary.log"

    adir   = BASE / "athletes" / slug
    pfile  = adir / "profile.json"
    profile = _read_json(pfile, {})

    client, chat_id = _load_client(slug)

    _cfg_all = json.loads(ATHLETES_CONFIG.read_text())
    _cfg = _cfg_all.get(slug, {})
    nutrition_target   = int(_cfg.get("nutrition_target_g_hr", 90))
    nutrition_alert    = int(_cfg.get("nutrition_alert_threshold_g_hr", 75))
    tsb_fresh          = float(_cfg.get("tsb_fresh_threshold", 10))
    tsb_overreach_thr  = float(_cfg.get("tsb_overreach_threshold", -30))
    ctl_ramp_thr       = float(_cfg.get("ctl_ramp_overreach_threshold", 7))

    today      = date.today()
    today_dow  = today.strftime("%A")
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end   = week_start + timedelta(days=6)           # Sunday
    week_date_grid_lines = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        week_date_grid_lines.append(f"  {d.isoformat()} = {d.strftime('%A')}")
    week_date_grid_str = "\n".join(week_date_grid_lines)

    # -- Fetch IcuSync data ----------------------------------------------------
    wellness_14d    = client.get_wellness(14)
    activities_7d   = client.get_training_history(7)

    # Passive run-threshold estimate (audit P1-8): GAP-at-HR fit vs the configured
    # threshold — flags placeholder/stale run thresholds before they seed race
    # targets when quality resumes. Flag-only, never auto-applied.
    run_thr_line = ""
    try:
        from thresholds import estimate_run_threshold_from_gap
        import ops_log as _ops2
        _est = estimate_run_threshold_from_gap(client)
        if _est:
            run_thr_line = (f"Passive run-threshold estimate: {_est['pace']}/km "
                            f"(GAP-at-HR fit, {_est['n_runs']} steady runs, R2 {_est['r2']})")
            _conf = profile.get("run_threshold_pace_per_km")
            if _conf:
                try:
                    _mm, _ss = str(_conf).split(":")
                    _conf_s = int(_mm) * 60 + int(_ss)
                    _diff = (_est["pace_s_per_km"] - _conf_s) / _conf_s * 100
                    run_thr_line += f" vs configured {_conf}/km ({_diff:+.0f}%)"
                    if abs(_diff) > 8:
                        run_thr_line += " — CONFIGURED THRESHOLD LOOKS STALE"
                        _ops2.alert("weekly-summary",
                                    f"run threshold drift: passive estimate {_est['pace']}/km vs "
                                    f"configured {_conf}/km ({_diff:+.0f}%) — review before any "
                                    f"quality-run prescriptions use it",
                                    athlete=slug)
                except Exception:
                    pass
    except Exception:
        pass

    # Realised intensity distribution (audit P1-1): what was DONE vs the phase
    # TID — catches grey-zone drift AND a week collapsing to all-easy.
    realised_tid_line = ""
    try:
        from primitives.realised_tid import realised_tid, tid_verdict
        from primitives.blueprint import current_phase, phase_family
        from session_library import event_key as _ekey
        import ops_log as _ops
        _lthr = profile.get("lthr")
        if not _lthr:
            try:
                _lthr = (client.get_sport_settings("Run") or {}).get("lthr")
            except Exception:
                _lthr = None
        _rt = realised_tid(activities_7d, lthr=_lthr)
        if _rt:
            _lib = _read_json(BASE / "config" / "session-library.json", {})
            _ev = (_lib.get("events") or {}).get(_ekey(_cfg, profile) or "", {})
            _bp = _read_json(adir / "reference" / "training-blueprint.json", {})
            _ph = phase_family((current_phase(_bp, today) or {}).get("name") or "base")
            _tid = (_ev.get("tid") or {}).get(_ph) or (_ev.get("tid") or {}).get("base")
            if _tid:
                _v = tid_verdict(_rt, _tid)
                realised_tid_line = (
                    f"Realised intensity (last 7d, session-level): "
                    f"{_rt['low_pct']}/{_rt['moderate_pct']}/{_rt['high_pct']} low/mod/high "
                    f"vs {_ph} target {_tid[0]}/{_tid[1]}/{_tid[2]}"
                    + (f" — BREACH ({_v['breach'][0]}): {_v['breach'][1]}"
                       if _v["breach"] else " — on distribution"))
                # A deload/taper week is SUPPOSED to be mostly easy — suppress the
                # missing-quality alarm there; excess quality stays valid any week.
                _wk_type = None
                try:
                    import plan_tools as _pt
                    _w = client.get_wellness(3)
                    _ctl = round(float(_w[-1].get("ctl") or 0), 1) if _w else None
                    if _ctl:
                        _wk_type = _pt.required_tss(_cfg, _ctl).get("week_type")
                except Exception:
                    pass
                if _v["breach"] and _v["breach"][0] == "missing_quality" \
                        and _wk_type in ("deload", "taper"):
                    realised_tid_line += f" (expected: {_wk_type} week — no alert)"
                elif _v["breach"]:
                    _ops.alert("weekly-summary",
                               f"realised TID {_v['breach'][0]}: {_v['breach'][1]}",
                               athlete=slug)
    except Exception:
        pass

    # Run aerobic efficiency (power:HR) — weekly means over the last 4 ISO weeks.
    # Higher = more watts per heartbeat = the engine improving. Runs ≥20 min with
    # power only (Garmin running power era, live since Jun 2026).
    run_efficiency_line = ""
    try:
        eff_by_week = {}
        for a in client.get_training_history(28) or []:
            if (a.get("type") or "") not in ("Run", "TrailRun", "VirtualRun"):
                continue
            ph = a.get("icu_power_hr")
            if not ph or (a.get("moving_time") or 0) < 1200:
                continue
            d = date.fromisoformat((a.get("start_date_local") or "")[:10])
            wk = (d - timedelta(days=d.weekday())).isoformat()
            eff_by_week.setdefault(wk, []).append(float(ph))
        weeks = sorted(eff_by_week)[-4:]
        if len(weeks) >= 2:
            vals = [sum(eff_by_week[w]) / len(eff_by_week[w]) for w in weeks]
            pct = (vals[-1] - vals[0]) / vals[0] * 100
            run_efficiency_line = (
                "Run aerobic efficiency (power:HR, weekly mean): "
                + " → ".join(f"{v:.2f}" for v in vals)
                + f" ({pct:+.1f}% over {len(weeks)} wk; higher = better)")
    except Exception as exc:
        print(f"[weekly-summary:{slug}] run efficiency calc failed: {exc}", file=sys.stderr)

    # VO2max + body-composition trends (block-level — both move slowly, so use ~8wk).
    # VO2max is an independent fitness check CTL can't give: rising = build working;
    # falling while CTL climbs = possible non-functional overreaching. Body comp on a
    # weight cut: weight down but body fat flat = losing lean mass, not fat.
    vo2max_line = "Not enough VO2max readings yet."
    composition_line = "No recent body-composition readings."
    try:
        w8 = client.get_wellness(56)
        vo2 = [((r.get("id") or "")[:10], float(r["vo2max"])) for r in w8 if r.get("vo2max")]
        if len(vo2) >= 2:
            (d0, v0), (d1, v1) = vo2[0], vo2[-1]
            dv = round(v1 - v0, 1)
            ctls = [float(r["ctl"]) for r in w8 if r.get("ctl") is not None]
            dctl = round(ctls[-1] - ctls[0]) if len(ctls) >= 2 else 0
            if dv < 0 and dctl >= 3:
                flag = " — ⚠️ VO2max falling while Fitness climbs: watch for non-functional overreaching."
            elif dv > 0:
                flag = " — engine improving, the build is productive."
            else:
                flag = " — stable."
            vo2max_line = (f"VO2max {v0:.0f} ({d0}) → {v1:.0f} ({d1}), {dv:+.1f} over window; "
                           f"Fitness (CTL) {dctl:+d} same window.{flag}")
        elif vo2:
            vo2max_line = f"VO2max {vo2[-1][1]:.0f} (single reading — need 2+ for a trend)."

        wts = [((r.get("id") or "")[:10], float(r["weight"]),
                (float(r["bodyFat"]) if r.get("bodyFat") else None))
               for r in w8 if r.get("weight")]
        if len(wts) >= 2:
            (d0, w0, _f0), (d1, w1, _f1) = wts[0], wts[-1]
            dw = round(w1 - w0, 1)
            target = float(profile.get("race_weight_kg") or profile.get("target_weight_kg") or 79)
            to_go = round(w1 - target, 1)
            comp = (f"Weight {w0:.1f}kg ({d0}) → {w1:.1f}kg ({d1}), {dw:+.1f}kg; "
                    f"{to_go:+.1f}kg to race target {target:.0f}kg.")
            bf_pts = [(d, f) for (d, _w, f) in wts if f is not None]
            if len(bf_pts) >= 2:
                bf0, bf1 = bf_pts[0][1], bf_pts[-1][1]
                dbf = round(bf1 - bf0, 1)
                comp += f" Body fat {bf0:.1f}% → {bf1:.1f}% ({dbf:+.1f}pp)."
                if dw <= -0.5 and dbf >= -0.1:
                    comp += (" ⚠️ Weight down but body fat not falling — likely losing lean mass, "
                             "not fat; check fuelling/protein on the cut.")
                elif dw <= -0.5 and dbf < -0.1:
                    comp += " ✅ Weight and body fat both down — losing fat, lean mass holding."
            composition_line = comp
    except Exception as exc:
        print(f"[weekly-summary:{slug}] vo2max/composition calc failed: {exc}", file=sys.stderr)

    events_this_wk  = client.get_events(week_start.isoformat(), week_end.isoformat())
    athlete_profile = client.get_athlete_profile()
    outlook_end     = (today + timedelta(days=28)).isoformat()
    fitness_outlook = client.get_fitness(7, newest=outlook_end)
    events_4wk      = client.get_events((today + timedelta(days=1)).isoformat(), outlook_end)

    # -- Read local files ------------------------------------------------------
    current_state = _read_file(adir / "current-state.md")
    session_log   = _read_json(adir / "session-log.json")
    heat_log      = _read_json(adir / "heat-log.json")
    blueprint     = _read_file(adir / "reference/training-blueprint.md")

    # Upcoming training plan files (start date > today)
    plan_texts = []
    for p in sorted(adir.glob("training-plan-*.md")):
        # filename: training-plan-YYYY-MM-DD_to_YYYY-MM-DD.md
        parts = p.stem.replace("training-plan-", "").split("_to_")
        if parts and parts[0] >= today.isoformat():
            plan_texts.append(p.read_text())
    upcoming_plans = "\n\n---\n\n".join(plan_texts) if plan_texts else "(none)"

    # Filter logs to this week
    week_sessions = [
        s for s in session_log
        if week_start.isoformat() <= s.get("date", "") <= week_end.isoformat()
    ]
    week_heat = [
        h for h in heat_log
        if week_start.isoformat() <= h.get("date", "") <= week_end.isoformat()
    ]

    # Nutrition history: rides/bricks >90 min with carb data, last 6 weeks
    six_weeks_ago = (today - timedelta(weeks=6)).isoformat()
    nutrition_history = [
        {"date": s["date"], "name": s.get("name",""), "duration_min": s.get("duration_min",0),
         "nutrition_g_carb": s["nutrition_g_carb"],
         "g_per_hr": round(s["nutrition_g_carb"] / s["duration_min"] * 60, 1)}
        for s in session_log
        if s.get("date","") >= six_weeks_ago
        and s.get("sport","") in ("Ride","VirtualRide","GravelRide","Brick")
        and s.get("duration_min", 0) >= 90
        and s.get("nutrition_g_carb") is not None
    ]

    four_weeks_ago = (today - timedelta(weeks=4)).isoformat()
    run_durability_4wk = [
        e for e in _read_json(adir / "run-durability-log.json", [])
        if e.get("date", "") >= four_weeks_ago
    ]

    # Week TSS accounting — pre-computed so compliance is never LLM arithmetic
    # (the old prompt said "or estimate from event duration/IF if not explicit").
    planned_rows, planned_total = [], 0
    for e in events_this_wk or []:
        if (e.get("category") or "WORKOUT").upper() != "WORKOUT":
            continue
        r = planned_session_tss(e)
        planned_total += r["tss"]
        planned_rows.append(
            f"  {(e.get('start_date_local') or '')[:10]}  {r['name']} — {r['tss']} TSS ({r['source']})")
    actual_total = sum(
        round(float(a.get("icu_training_load") or 0))
        for a in activities_7d or []
        if week_start.isoformat() <= (a.get("start_date_local") or "")[:10] <= week_end.isoformat())
    compliance_pct = round(actual_total / planned_total * 100) if planned_total else None
    tss_accounting = (
        f"Planned TSS total: {planned_total}\n"
        f"Actual TSS total: {actual_total}\n"
        f"Compliance: {f'{compliance_pct}%' if compliance_pct is not None else 'n/a (no planned events)'}\n"
        "Per planned session:\n" + ("\n".join(planned_rows) or "  (none)"))

    race_date    = date.fromisoformat(profile.get("race_date", "2026-09-19"))
    days_to_race = (race_date - today).days
    race_name    = profile.get("race_name", "race")
    first_name   = profile.get("name", slug).split()[0]
    ftp          = profile.get("ftp_watts", "unknown")

    # -- Athlete-specific nutrition consequence (for T7 trigger) --------------
    prev_race = profile.get("prev_race", {})
    prev_race_notes = prev_race.get("notes", "")
    prev_race_name  = prev_race.get("name", "")
    if prev_race_notes:
        nutrition_consequence = (
            f"{prev_race_name} post-race note: \"{prev_race_notes}\" — "
            f"underfuelling in training means the gut never adapts to high carb flux under load. "
            f"Every long ride below {nutrition_alert}g/hr is a missed adaptation."
        )
    else:
        nutrition_consequence = (
            "Underfuelling in training means the gut never adapts to high carb flux under load. "
            f"Race-day bonk risk rises sharply when training rides average below {nutrition_alert}g/hr."
        )

    # -- Pre-compute recovery score --------------------------------------------
    recovery = None
    try:
        hrv_t, hrv_b, tsb_v, sleep_v, sleep_score_v = rs._parse_wellness(wellness_14d)
        pain = 0
        state_json = adir / "current-state.json"
        if state_json.exists():
            pain = json.loads(state_json.read_text()).get("ankle", {}).get("pain_during", 0) or 0
        recovery = rs.compute(hrv_t, hrv_b, tsb_v, sleep_v, pain,
                              in_taper=rs.in_taper(slug), sleep_score=sleep_score_v)
    except Exception:
        pass

    recovery_block = ""
    if recovery:
        score  = recovery.get("score", "?")
        label  = recovery.get("label", "?")
        rec    = recovery.get("recommendation", "")
        sigs   = recovery.get("signals", {})
        hrv_r  = sigs.get("hrv",   {}).get("ratio")
        hrv_t_v = sigs.get("hrv",  {}).get("value")
        hrv_b_v = sigs.get("hrv",  {}).get("baseline")
        tsb_sv = sigs.get("tsb",   {}).get("value")
        slp_sv = sigs.get("sleep", {}).get("value")
        pain_v = sigs.get("pain",  {}).get("value")
        avail  = recovery.get("available_signals", [])
        missing = recovery.get("missing_signals", [])
        parts = []
        if hrv_r  is not None: parts.append(f"HRV ratio {hrv_r:.2f} (today {hrv_t_v}, baseline {hrv_b_v})")
        if tsb_sv is not None: parts.append(f"Form {tsb_sv:+.1f}")
        if slp_sv is not None: parts.append(f"sleep {slp_sv:.1f}h")
        if pain_v is not None and pain_v > 0: parts.append(f"pain {pain_v}/10")
        recovery_block = (
            f"\n## Pre-computed recovery score (end of week)\n"
            f"Score: {score}/100 — {label}. {rec}\n"
            f"Signals: {', '.join(parts) if parts else 'no data'}. "
            f"Available: {avail}. Missing: {missing}.\n"
            f"Use this for T1/T8 evaluation — it is already derived from the wellness data below.\n"
        )

    coaching_level = profile.get("coaching_level", "mid")

    # -- Build prompt ----------------------------------------------------------
    prompt = f"""You are generating the weekly training summary for {first_name}'s {race_name} coaching system.
All IcuSync data has been fetched and is embedded below. Do NOT call any fetch commands — work only from the data provided. Use Write and Bash only for the state-file update and git commit at the end.

## DATE ANCHOR — Python-computed, authoritative
Today     : {today} ({today_dow})
Week      : {week_start} (Mon) → {week_end} (Sun)
Day-of-week map:
{week_date_grid_str}
RULE: any session referenced by date must use the day from this map.
If IcuSync current_date_local disagrees with {today}, flag it — do not silently use the wrong date.


{_level_block(coaching_level)}
{recovery_block}
---

## Context

Race: {race_name} | Days to race: {days_to_race} | FTP: {ftp} W
Week: {week_start} → {week_end}

## IcuSync — Wellness (14 days: CTL, ATL, TSB, HRV, sleep, weight, RHR)
{json.dumps(wellness_14d, indent=2)}

## IcuSync — Activities this week (7 days)
{json.dumps(activities_7d, indent=2)}

## IcuSync — Planned events this week ({week_start} → {week_end})
{json.dumps(events_this_wk, indent=2)}

## IcuSync — Athlete profile
{json.dumps(athlete_profile, indent=2)}

## Local — current-state.md
{current_state}

## Local — Session log (this week only)
{json.dumps(week_sessions, indent=2)}

## Local — Heat log (this week only)
{json.dumps(week_heat, indent=2)}

## Training Blueprint (phase structure and TSS targets)
{blueprint}

## IcuSync — Fitness projection (last 7 days + next 28 days)
{json.dumps(fitness_outlook, indent=2)}

## IcuSync — Events next 28 days ({today} → {outlook_end})
{json.dumps(events_4wk, indent=2)}

## Upcoming training plans
{upcoming_plans}

## Local — Week TSS accounting (pre-computed — authoritative)
{tss_accounting}

## Local — Nutrition history (rides/bricks >90 min, last 6 weeks — g/hr computed)
{json.dumps(nutrition_history, indent=2)}

## Local — Run aerobic efficiency trend (pre-computed)
{run_efficiency_line or "Not enough run power:HR data yet."}
{realised_tid_line or "Realised intensity: not enough classifiable activity data this week."}
{run_thr_line or "Run threshold: no passive estimate available (needs 6+ steady runs with GAP+HR)."}

## Local — VO2max trend (block-level, pre-computed)
{vo2max_line}

## Local — Body composition trend (weight + body fat, pre-computed)
{composition_line}

## Local — Run durability log (last 4 weeks: per-run decoupling / cadence fade / cost fade)
{json.dumps(run_durability_4wk, indent=2)}

---

## Step 1 — Compute week metrics

From the data above, extract:
- Total actual TSS, total planned TSS, compliance %: use the pre-computed "Week TSS accounting"
  block above VERBATIM — never sum, estimate, or recompute these yourself
- CTL at start of week vs end of week (from wellness — first and last entries for the week range)
- ATL at end of week
- TSB at end of week (form field in wellness, or ATL - CTL)
- 4-week CTL ramp: (end CTL - CTL 28 days ago) / 4 — use wellness data window available
- Disciplines completed: count by sport_label from activities
- Sessions missed: planned events with no matching activity on same date and sport
- Heat sessions this week: count from heat-log above
- Average sleep: mean hrsSleep from wellness this week
- Fuelling logged this week: sessions with nutrition_g_carb set vs total rides
- Nutrition trend (from nutrition history above):
  - This week avg g/hr (rides >90 min): compute from nutrition_history entries this week
  - 4-week rolling avg g/hr: mean across all entries in nutrition_history
  - Trend direction: compare most recent 3 sessions vs previous 3 — improving / declining / flat
  - Gap to race target: {nutrition_target} − this_week_avg (g/hr)
- Injury pain: ankle_pain_during scores from session-log this week
- Run engine + durability: quote the pre-computed run aerobic efficiency trend line if present; from the run durability log, note any run this week with flags (decoupling >5%, cadence fade, rising cost) and whether long-run durability is trending better or worse across the 4 weeks
- VO2max + body composition: quote the pre-computed VO2max trend and body-composition lines verbatim. If either carries a ⚠️ flag (VO2max falling while Fitness climbs, or weight down without body fat falling), surface it in the Key finding or as a 📌 line — these are the signals the athlete cannot see elsewhere

## Step 2 — Output the summary card

Output the card in Telegram Markdown. Rating = STRONG (≥95% compliance, no flags) / SOLID (80–95%, no major flags) / LIGHT (<80%) / MIXED (compliance ok but flags).

---
**Week ending {week_end} — [STRONG / SOLID / LIGHT / MIXED]**

| Metric | This week | Target/trend |
|---|---|---|
| Load | X (planned Y) | — |
| Compliance | X% | ≥90% |
| Fitness change | +X / −X | phase ramp target |
| Fatigue | X | — |
| Sleep avg | Xh | ≥7h |
| Body comp | X.Xkg (Y.Y% fat) | toward [race target]kg |
| VO2max | NN (trend ±N) | rising/stable |
| Heat sessions | N | — |
| Fuelling (rides >90 min) | Xg/hr this wk (4wk avg: Y) | {nutrition_target}g/hr race target — gap: Zg/hr |

**Completed:** [discipline summaries — e.g. "3 rides, 2 runs, 1 swim"]
**Missed:** [session names, or "none"]

**Key finding:** [one sentence — most important thing from this week]

**Monday focus:** [one sentence — single most important thing for next week's first session]

---

**4-week outlook**

| Week | Constraint | Proj. Fitness | Proj. Form |
|---|---|---|---|
| [Mon dd Mon] | [e.g. travel / full / race] | [Fitness from fitness_outlook] | [Form] |
| [Mon dd Mon] | … | … | … |
| [Mon dd Mon] | … | … | … |
| [Mon dd Mon] | … | … | … |

[For each significant event or constraint in events_4wk / upcoming plans / current-state.md, one line:]
📌 [Date]: [What — e.g. "Travel block begins (no bike)", "Dorney C-race TBC", "Heat protocol target 2×/wk"]

*Race trajectory: projected Fitness [X] on [date 8 weeks out] vs blueprint target [Y] — [on track / behind / ahead].*

---

## Step 3 — Decision triggers (⚡)

Evaluate each trigger using the computed metrics. Output only the ones that FIRE. If none fire, output the all-clear line.

**T1 RECOVERY** — fires if end-of-week TSB < {tsb_overreach_thr}:
⚡ *T1 RECOVERY*: Form at [X] — accumulated fatigue is high.
Options: A) 2-day recovery block (Mon–Tue easy only) | B) Continue as planned | C) Reduce Monday volume 40%

**T2 OVERREACH** — fires if 4-week CTL ramp > {ctl_ramp_thr}/wk:
⚡ *T2 OVERREACH*: 4-week Fitness ramp at [X]/wk — approaching overreach threshold.
Options: A) Cap next week at current Load | B) Insert recovery week now | C) Continue (accept fatigue risk)

**T3 UNDERLOAD** — fires if week compliance < 60%:
⚡ *T3 UNDERLOAD*: Week compliance [X]% — well below minimum threshold.
Availability issue or training fatigue? Reply to clarify and I'll adjust next week's plan.

**T4 FRESH** — fires if end-of-week TSB > {tsb_fresh} AND days to race > 42:
⚡ *T4 FRESH*: Form at [X] with {days_to_race} days to race — you're fresher than the phase requires.
Options: A) Add an extra session | B) Increase intensity on planned sessions | C) Hold (life/fatigue reason)

**T5 PHASE TRANSITION** — fires if current phase (from blueprint) ends within 7 days:
⚡ *T5 PHASE TRANSITION*: [phase name] ends [date] — entering [next phase] next week.
Readiness: [one line on whether athlete is prepared to step up]

**T6 INJURY** — fires if ankle pain avg > 3 this week OR last 3 pain scores are trending up:
⚡ *T6 INJURY*: Ankle pain avg [X]/10 this week [or: trending up — scores X→Y→Z].
Options: A) Drop all runs this week | B) Reduce run volume 50% | C) Continue protocol (accept risk)

**T7 NUTRITION** — fires if this-week avg g/hr < {nutrition_alert} on rides >90 min AND at least 1 such session was logged:
⚡ *T7 NUTRITION*: Avg fuelling [X]g/hr on long rides — [Y]g/hr short of the {nutrition_target}g/hr race target. Trend: [improving / declining / flat] over last 6 sessions.
Race consequence: {nutrition_consequence}
Fix: Eat at 15 min and every 25 min after. This week's long ride target: {nutrition_target}g/hr. Use Maurten 320 + chews if GI allows.

**T8 HRV** — fires if the pre-computed recovery score HRV ratio < 0.90 OR 3+ consecutive days with HRV below the 7-day rolling average in the wellness data:
⚡ *T8 HRV*: HRV ratio [X] vs baseline — accumulated fatigue signal (recovery score: [score]/100 [label]).
Options: A) Flip tomorrow to easy | B) Prioritise sleep tonight | C) Continue (trust your Form)

If no triggers fire:
✅ No decision triggers this week.

---

## Step 4 — Open actions review

From the "Open actions" table in current-state.md, list any actions where status is NOT "done" and:
- Due date ≤ 14 days from today ({today}): flag as ⚠️ DUE SOON
- Due date has already passed: flag as 🔴 OVERDUE
- No due date but status is "pending" for 3+ weeks: flag as 📋 STALE

Format (append after the decision triggers, before the sign-off):

---
**Open actions**
[For each flagged item:]
[⚠️/🔴/📋] *[Action name]* — due [date] ([N days]) — [one-line nudge if overdue]

If no flagged actions: omit this section entirely.

---

## Step 5 — Update current-state.md

Using the Write tool, update ClaudeCoach/athletes/{slug}/current-state.md:
- Change "Last updated" line to today: {today}
- Update or add "Off-plan in last 7 days" with missed sessions (or "none")
- If heat sessions this week > 0: append a row to "Heat acclimation log" table
- If any body weight readings in wellness data: note the latest weight

Then using Bash:
  cd {PROJECT_DIR} && git add ClaudeCoach/athletes/{slug}/current-state.md && git commit -m "weekly: state update week ending {week_end}" && git pull --rebase origin main && git push origin main

## Output

Wrap your entire output in <telegram> and </telegram> tags. Output nothing outside those tags — no preamble, no reasoning, no tool commentary.
"""

    result = claude_call.run_claude(
        prompt, model=claude_call.SONNET, allowed_tools=TOOLS,
        cwd=PROJECT_DIR, timeout=600, label=slug,
    )

    if result.stderr:
        with open(log_file, "a") as f:
            f.write(result.stderr + "\n")

    import re as _re
    raw = result.stdout.strip()
    m = _re.search(r"<telegram>(.*?)</telegram>", raw, _re.DOTALL)
    output = m.group(1).strip() if m else ""
    if output:
        _tg_send(chat_id, output)

    # Regenerate trend aggregates in the background (feeds dashboard chart)
    try:
        trend_script = BASE / "scripts/weekly-trend.py"
        if trend_script.exists():
            subprocess.Popen(
                [sys.executable, str(trend_script), "--athlete", slug],
                cwd=PROJECT_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass

    return output


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--athlete", default="jamie")
    args = p.parse_args()
    out = run_summary(args.athlete)
    print(out)
