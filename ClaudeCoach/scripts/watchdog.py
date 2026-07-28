#!/usr/bin/env python3
"""
Daily watchdog — fires a Telegram notification only if a trigger trips.
Runs via VM crontab at 05:30 daily. Loops over all active athletes.
Safe to run manually: python3 ClaudeCoach/scripts/watchdog.py
"""
import json, subprocess, sys, tempfile, os, time
from datetime import date, timedelta
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/
sys.path.insert(0, str(BASE / "lib"))
import claude_call
import ops_log
import heat as heat_lib
import open_actions as oa_lib   # T9: single store, arithmetic in Python
CLAUDE      = "/usr/bin/claude"
NOTIFY      = BASE / "telegram/notify.py"
CONFIG      = BASE / "config/athletes.json"
LOG_DIR     = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE    = LOG_DIR / "watchdog.log"

TOOLS = "Read,Write,Edit,Bash"


def trim_log(path: Path, max_lines: int = 5000):
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


def load_profile(slug: str) -> dict:
    p = BASE / "athletes" / slug / "profile.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


# -- T12: REALISED CTL ramp ---------------------------------------------------
# max_ctl_ramp_per_week is enforced at BUILD time, on a PLANNED week
# (plan_builder.py:155-163). Nothing watched what actually happened, so Calum's
# off-plan riding took him CTL 5.6 (28 Jun) -> 30.3 (21 Jul) 2026 - a +9.4/wk
# week against a 5.0/wk cap - and it passed unremarked for three weeks.
#
# Deliberately deterministic Python, not another prompt trigger: a check the
# model evaluates cannot be tested, and "fires on this week, silent on that one"
# is the only evidence a load guard works.
#
# SOURCE. intervals.icu's own CTL is authoritative and is used whenever it can be
# fetched. Reconstructing CTL from session-log.json is the OFFLINE FALLBACK only,
# because that log carries planned-event projections that never happened (Calum's
# 2026-07-05 "Long ride", 138 TSS, stub=true, against ctlLoad=0 in ICU) and
# entries with tss=null (Aosta, 2026-07-20). Measured against ICU over 29 Jun -
# 19 Jul 2026 the reconstruction over-fires one week (+8.3 vs +4.7 actual) and
# under-fires another (+4.9 vs +5.3), so a fallback flag is marked approximate.
CTL_TIME_CONSTANT_DAYS = 42   # standard impulse-response CTL constant (as intervals.icu)
RAMP_TOLERANCE = 0.3          # fallback source only: noise margin on a reconstructed series
CTL_LOOKBACK_DAYS = 60


def _daily_tss(log: list[dict]) -> dict[str, float]:
    daily: dict[str, float] = {}
    for e in log or []:
        d = (e.get("date") or "")[:10]
        if not d:
            continue
        try:
            daily[d] = daily.get(d, 0.0) + float(e.get("tss") or 0)
        except (TypeError, ValueError):
            continue
    return daily


def ctl_from_log(log: list[dict], upto: date | None = None,
                 tc: int = CTL_TIME_CONSTANT_DAYS) -> dict[str, float]:
    """Realised CTL per day, reconstructed from logged TSS. Fallback source."""
    daily = _daily_tss(log)
    if not daily:
        return {}
    d = date.fromisoformat(min(daily))
    end = upto or date.fromisoformat(max(daily))
    ctl, out = 0.0, {}
    while d <= end:
        ctl += (daily.get(d.isoformat(), 0.0) - ctl) / tc
        out[d.isoformat()] = round(ctl, 1)
        d += timedelta(days=1)
    return out


def ctl_from_icu(slug: str, days: int = CTL_LOOKBACK_DAYS) -> dict[str, float]:
    """Authoritative CTL per day from the intervals.icu fitness endpoint."""
    try:
        r = subprocess.run(
            ["python3", "ClaudeCoach/lib/icu_fetch.py", "--athlete", slug,
             "--endpoint", "fitness", "--days", str(days)],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=60,
        )
        if r.returncode != 0:
            return {}
        out = {}
        for e in json.loads(r.stdout) or []:
            d = e.get("date") or e.get("id")        # the endpoint keys the day either way
            if d and e.get("ctl") is not None:
                out[str(d)[:10]] = round(float(e["ctl"]), 1)
        return out
    except Exception:
        return {}


def ctl_history(slug: str, log: list[dict] | None = None,
                today: date | None = None) -> tuple[dict[str, float], str]:
    series = ctl_from_icu(slug)
    if series:
        return series, "icu_fitness"
    return ctl_from_log(log or [], upto=today or date.today()), "session_log"


def realised_ramp(series: dict[str, float], cap: float, overreach: float | None = None,
                  today: date | None = None, weeks: int = 4,
                  tolerance: float = 0.0) -> list[dict]:
    """Week-on-week realised CTL ramp over the last COMPLETE Mon-Sun weeks.

    Complete weeks only - the current part-week is excluded, matching T11: a
    part-week reads as a collapse every Monday and a spike every Sunday.
    A breach escalates to Tier 1 above the athlete's ctl_ramp_overreach_threshold.
    Newest breach first.
    """
    if not series:
        return []
    today = today or date.today()
    last_sunday = today - timedelta(days=today.weekday() + 1)   # most recent completed week end
    over = float(overreach) if overreach else None
    breaches = []
    for i in range(weeks):
        wk_end = last_sunday - timedelta(days=7 * i)
        prev_end = wk_end - timedelta(days=7)
        now_ctl, was_ctl = series.get(wk_end.isoformat()), series.get(prev_end.isoformat())
        if now_ctl is None or was_ctl is None:
            continue
        ramp = round(now_ctl - was_ctl, 1)
        if ramp <= cap + tolerance:
            continue
        breaches.append({
            "week_start": (wk_end - timedelta(days=6)).isoformat(),
            "week_end": wk_end.isoformat(),
            "ctl_from": was_ctl, "ctl_to": now_ctl, "ramp": ramp, "cap": cap,
            "tier": 1 if (over and ramp > over) else 2,
            "overreach_threshold": over,
        })
    return breaches


def ramp_flags(slug: str, cfg: dict, today: date | None = None,
               write: bool = True) -> list[dict]:
    """Evaluate T12 for one athlete and (optionally) persist the flags.

    Flags go into current-state.json watchdog_flags, numeric and dateable only,
    never prose: refresh-site-data.py copies that key into the athlete payload and
    public_sanitise.py holds it on FORBIDDEN_KEYS, so free text there is a
    disclosure risk, not a formatting choice. Idempotent per (trigger, week).
    """
    sl = BASE / "athletes" / slug / "session-log.json"
    try:
        log = json.loads(sl.read_text()) if sl.exists() else []
    except Exception:
        log = []
    series, source = ctl_history(slug, log, today=today)
    breaches = realised_ramp(
        series, float(cfg.get("max_ctl_ramp_per_week", 5.0)),
        cfg.get("ctl_ramp_overreach_threshold"), today=today,
        tolerance=0.0 if source == "icu_fitness" else RAMP_TOLERANCE)
    for b in breaches:
        b["source"] = source
    if not breaches or not write:
        return breaches
    csp = BASE / "athletes" / slug / "current-state.json"
    try:
        state = json.loads(csp.read_text()) if csp.exists() else {}
    except Exception:
        return breaches
    flags = state.setdefault("watchdog_flags", [])
    seen = {(f.get("trigger"), f.get("week_end")) for f in flags if isinstance(f, dict)}
    new = [{"trigger": "T12", "week_end": b["week_end"], "week_start": b["week_start"],
            "signal": "realised_ctl_ramp", "ramp": b["ramp"], "cap": b["cap"],
            "ctl_from": b["ctl_from"], "ctl_to": b["ctl_to"], "tier": b["tier"],
            "source": source, "approximate": source != "icu_fitness",
            "logged": (today or date.today()).isoformat()}
           for b in breaches if ("T12", b["week_end"]) not in seen]
    if new:
        state["watchdog_flags"] = flags + new
        tmp = csp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, csp)
    return breaches


def ramp_trail(breaches: list[dict]) -> str:
    """One L2 reasoning line per breach, in the watchdog's stdout format."""
    out = []
    for b in breaches:
        tier = "Tier 1, past the overreach threshold" if b["tier"] == 1 else "Tier 2"
        approx = "" if b.get("source") == "icu_fitness" else " [reconstructed, approximate]"
        out.append(
            f"realised CTL {b['ctl_from']} -> {b['ctl_to']} over week "
            f"{b['week_start']}..{b['week_end']} = +{b['ramp']}/wk vs cap "
            f"{b['cap']}/wk{approx} -> T12 realised CTL ramp breach [{tier}] -> hold "
            f"next week's load at or below the week just finished and re-check -> "
            f"ramp back inside {b['cap']}/wk with the CTL gained retained"
        )
    return "\n".join(out)


def build_prompt(slug: str, name: str, race_name: str, race_date: str, chat_id: str, heat: dict | None = None, strength_target: int | None = None) -> str:
    today = date.today().isoformat()
    athlete_dir = BASE / "athletes" / slug

    # Heat triggers are two-stage: before `starts` the formal protocol is paused
    # on ambient exposure, so we check a maintenance dose floor that keeps that
    # pause honest; from `starts` the full race-proximal targets apply.
    heat = heat or {"active": False}
    heat_log_read = ""
    heat_triggers = ""
    # profile heat_silent silences proactive heat triggers entirely (lib/heat.state
    # "silent"). The dose is still logged and scored — nothing nags about it.
    if heat.get("active") and not heat.get("silent"):
        heat_log_read = f"- {athlete_dir}/heat-log.json\n"
        starts = heat.get("starts") or today
        dose_note = ("Dose accounting: sum the dose field over entries in the last 14 days; "
                     "an entry with no dose field counts as 1.0; a missing or empty "
                     "heat-log.json counts as total dose 0.")
        if today < starts:
            # Pre-window checks are opt-in (profile heat_maintenance) — only an
            # athlete who deliberately paused formal heat work on ambient
            # exposure wants that pause policed months before the race.
            if heat.get("maintenance"):
                heat_triggers = (
                    f"T7 (Tier 2): Heat maintenance — the formal heat protocol is PAUSED on ambient "
                    f"exposure until {starts}. {dose_note} If 14-day dose < {heat_lib.MAINTENANCE_DOSE_14D} "
                    f"fire: \"heat maintenance dose low — ambient exposure is not covering the pause; "
                    f"add a sauna/hot-bath session or plan hot-venue training time\".\n"
                )
            else:
                heat_log_read = ""
        else:
            heat_triggers = (
                f"T7 (Tier 1): Formal heat protocol active since {starts}. {dose_note} "
                f"Fire if 14-day dose < {heat_lib.PROTOCOL_DOSE_14D}.\n"
                f"T8 (Tier 2): Most recent date in heat-log.json is >7 days ago (or the log is missing/empty).\n"
            )

    # Injury triggers are athlete-scoped: only an athlete with a structured `ankle`
    # block in current-state.json is in ankle rehab. Unconditional T2 text made the
    # watchdog narrate "ankle still in rehab" for Kathryn, who has no ankle injury.
    has_ankle = False
    try:
        has_ankle = bool((json.loads((athlete_dir / "current-state.json").read_text())
                          or {}).get("ankle"))
    except Exception:
        pass
    t2 = ("T2 (Tier 2): CTL ramp >4/wk while ankle still in rehab (check current-state.md "
          "ankle quality-sessions-resumed field)"
          if has_ankle else
          "T2: skip — this athlete has NO tracked injury rehab; never mention ankle or "
          "rehab status for them")
    t10_ankle = ("  - Also cross-check current-state.json ankle.weekly_run_km_this_week vs "
                 "ankle.weekly_run_km_last_week (if fields exist)\n" if has_ankle else "")

    # T9 is decided in Python, off the single store, by the same call the weekly card
    # makes (lib/open_actions.py). Previously the prompt told the model to cross-check
    # current-state.json open_actions[] itself while the weekly card read a hand-kept
    # markdown table, and the two surfaces silently disagreed for three months.
    t9 = oa_lib.watchdog_block(slug, date.fromisoformat(today))

    t11 = ""
    if strength_target:
        t11 = (
            f"T11 (Tier 2): Strength compliance — target {strength_target}/week.\n"
            f"  Count strength sessions (type WeightTraining, or name containing strength/gym/S&C)\n"
            f"  in the history endpoint for each of the LAST 2 COMPLETED weeks (Mon-Sun, exclude the\n"
            f"  current part-week). If BOTH weeks are below target, fire: \"warning T11: strength X\n"
            f"  and Y sessions in last 2 weeks vs target {strength_target}/wk — schedule the missing\n"
            f"  sessions (Tier C needs no equipment)\".\n"
        )

    return f"""You are running the daily watchdog check for {name}'s {race_name} coaching system.
Run silently — only produce output if a trigger fires.

Read these files (skip any that do not exist):
- {athlete_dir}/current-state.md
- {athlete_dir}/current-state.json
- {athlete_dir}/reference/rules.md
- {athlete_dir}/reference/decision-points.md
- {athlete_dir}/session-log.json
{heat_log_read}
Pull live data via Bash (use today's date {today} for all calculations):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint fitness --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 14

Evaluate these triggers in order (skip any whose required data files are missing):
T1 (Tier 2): ATL > CTL + 25 for 3+ consecutive days
{t2}
T3 (Tier 1): HRV trend down >7% over last 7 days
T4 (Tier 1): Sleep <7h for 3+ days in last 7 (skip if no sleep data available)
T5 (Tier 1): Missed planned sessions >=2 in last rolling 7 days
  Suppression: before sending Telegram, check current-state.md for the most recent T5 entry.
  If T5 fired yesterday (or earlier) and the SAME missed session dates are already logged there, do NOT send a Telegram message — log to current-state.md only. Only send Telegram if there is a new missed session not present in the prior T5 entry.
T6 (Tier 1): Aerobic decoupling >5% on any Z2 ride in last 7 days (check via activity_detail for rides with IF < 0.75):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint activity_detail --activity-id ID
  Suppression: before sending Telegram, check current-state.md for the most recent T6 entry.
  If T6 fired in the last 3 days and all flagged rides are already logged there (same activity dates), do NOT send a Telegram message — log to current-state.md only. Only send Telegram if there is a new Z2 ride with decoupling >5% not present in the prior T6 entry.
{heat_triggers}{t9}T12: ALREADY EVALUATED in Python before this prompt ran (realised week-on-week
  CTL ramp vs the athlete\'s max_ctl_ramp_per_week, off the intervals.icu fitness endpoint).
  Any breach is already written to current-state.json watchdog_flags. Do NOT recompute it
  and do NOT invent one; if watchdog_flags contains a T12 entry dated today, log it in the
  current-state.md Watchdog Log alongside the other triggers, subject to the same
  daily-nag suppression.
T10 (Tier 2): Run weekly km increase >10% week-on-week
  - Sum run distance (km) from history endpoint for Mon–today (current week)
  - Sum run distance for the 7 days prior (last week)
{t10_ankle}  - Fire if this_week_km > last_week_km * 1.10 AND last_week_km > 0
  - Fire message: "warning T10: run km +X% week-on-week ([this]km vs [last]km) — 10% cap applies"
{t11}
If NO triggers fire: output nothing. Silent run.

DO NOT SEND ANY TELEGRAM MESSAGE — ever. The watchdog is silent. Its job is to DETECT and
LOG only. The 06:30 morning card reads current-state.md and surfaces any relevant flag to the
athlete then. A 05:30 ping is exactly what the athlete asked us to stop. NEVER run notify.py.

If ANY trigger fires:
1. DAILY-NAG SUPPRESSION — before logging, read current-state.md and check whether this SAME
   trigger (same trigger name + same underlying item/signal, e.g. the same overdue action) was
   already logged within the last 3 days. If it was, do NOTHING for that trigger — no new entry,
   no commit. Only log a trigger that is NEW or whose signal has materially changed. This stops
   the athlete being reminded of the same unfinished thing every single morning.
2. For genuinely new/changed triggers only: update current-state.md — append to the relevant
   section with today's date and trigger name + signal value. Do not rewrite untouched sections.
3. If you appended anything, commit it by running EXACTLY this one command and nothing else:
   /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/scripts/cc-git-commit-push.sh "watchdog: [trigger list] {today}" ClaudeCoach/athletes/{slug}/current-state.md
   Do NOT run git add, git commit, git push, git pull or git rebase yourself, and do not
   add any other git command before or after this one. This wrapper takes the repo-wide
   lock and retries a push that loses a race; raw git bypasses both and collides with the
   other jobs that write this same repository every few minutes.
4. Output one L2 reasoning trail per trigger to stdout (this goes to the coaching log only — NOT to athletes):
   [signal with real number] -> [rule: T1-T10] -> [suggested adjustment] -> [expected effect]
   Example: "ATL 148 vs CTL 121 for 4 days -> T1 (ATL > CTL +25) -> insert recovery day -> TSB recovers ~8 pts by weekend"
"""


def run_for_athlete(slug: str, cfg: dict) -> str | None:
    name      = cfg.get("name", slug)
    race_name = cfg.get("race_name", "upcoming race")
    race_date = cfg.get("race_date", "")
    chat_id   = str(cfg.get("chat_id", ""))

    profile = load_profile(slug)
    heat = heat_lib.state(slug, profile)
    strength_target = None
    if profile.get("strength_programme"):
        strength_target = int((cfg.get("day_rules") or {}).get("strength_max", 2))

    prompt = build_prompt(slug, name, race_name, race_date, chat_id, heat=heat,
                          strength_target=strength_target)

    # T12 runs HERE, in Python, before the model is asked anything: the flag is
    # written from the session log whether or not the Claude call succeeds.
    ramp_out = ""
    try:
        breaches = ramp_flags(slug, cfg)
        if breaches:
            ramp_out = ramp_trail(breaches)
            ops_log.record_run("watchdog", athlete=slug, ok=True,
                               detail=f"T12: {len(breaches)} week(s) over the "
                                      f"{cfg.get('max_ctl_ramp_per_week', 5.0)}/wk CTL ramp cap "
                                      f"(source {breaches[0].get('source')})")
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[watchdog:{slug}] T12 ramp check failed: {e}\n")

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="claudecoach_watchdog_", delete=False, suffix=".txt"
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        result = claude_call.run_claude(
            open(prompt_file).read(),
            model=claude_call.SONNET, allowed_tools=TOOLS,
            cwd=PROJECT_DIR, timeout=None, label=slug,
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            with open(LOG_FILE, "a") as lf:
                lf.write(f"[watchdog:{slug}] STDERR: {stderr}\n")
        if result.returncode != 0:
            ops_log.alert("watchdog",
                          f"claude CLI exit {result.returncode}: {stderr[-300:]}", athlete=slug)
            return ramp_out or None
        ops_log.record_run("watchdog", athlete=slug, ok=True,
                           detail="triggered" if output else "silent")
        return "\n".join(x for x in (ramp_out, output) if x) or None
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[watchdog:{slug}] Exception: {e}\n")
        ops_log.alert("watchdog", f"exception: {e}", athlete=slug)
        return ramp_out or None
    finally:
        os.unlink(prompt_file)


ATHLETE_STAGGER_S = int(os.environ.get("ATHLETE_STAGGER_S", "90"))


def main():
    athletes = json.loads(CONFIG.read_text())
    processed = False
    for slug, cfg in athletes.items():
        if not cfg.get("active"):
            continue
        if processed:
            # Space the athletes' Claude runs to avoid bursting the rate limit.
            time.sleep(ATHLETE_STAGGER_S)
        processed = True
        chat_id = str(cfg.get("chat_id", ""))
        output = run_for_athlete(slug, cfg)
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[watchdog:{slug}] {'triggered' if output else 'silent'}\n")
        if output:
            # Log the reasoning trail only — Claude sends the Telegram notification
            # itself via the Bash tool with the injected chat_id. Sending output here
            # would leak the reasoning trail to the athlete.
            print(output, flush=True)
    trim_log(LOG_FILE)


if __name__ == "__main__":
    main()
