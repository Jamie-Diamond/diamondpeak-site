#!/usr/bin/env python3
"""Morning briefing — polls every 15 min from 06:00–09:00 via VM crontab. Sends once per athlete per day, after Garmin sleep data syncs."""
import fcntl, json, os, re, subprocess, sys, time
from datetime import datetime
from datetime import date, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOCK_FILE       = BASE / ".morning_checkin.lock"  # prevents overlapping cron runs double-sending
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "lib"))
import coaching_prefs  # noqa: E402  heat / fuelling switches
import fuel_basis      # noqa: E402  race vs non-race fuelling figure
import shadow          # noqa: E402  shadow week (Haiku twin, never sent)
import today_note      # noqa: E402  the card lives on Peak's Today tab, not the chat
sys.path.insert(0, str(BASE / "telegram"))
sys.path.insert(0, str(BASE / "ironman-analysis"))
import claude_call
import ask_gate                 # standing-ask backoff (weight, ankle score)
from coaching_levels import level_block as _level_block
import consistency          # missed-session line (lib/consistency.py)
import illness as illness_lib   # structured illness/compromised flag (surfacing gate)
import weekly_availability     # per-week declared hours (Sunday ask + resolver)
from primitives.planned_tss import planned_sessions_block
from primitives.nutrition import fuel_target, recent_avg_g_hr
import ops_log
import planning_pause    # tracking-only athletes: no prescribing, no adherence
import heat as heat_lib
import menstrual as menstrual_lib
import races as races_lib

TOOLS = "Read,Bash"

# The identity this job declares to lib/icu_fetch.py, in every icu_fetch line of the
# prompt. icu_fetch refuses the four WRITE endpoints to this caller: a check-in asks how the
# athlete slept and reads what is already planned. It has no business creating, changing or
# removing a session, but it holds Bash, so until now nothing stopped it.
CALLER = "morning-checkin"


def _build_prompt(slug, first_name, race_name, race_date, days_to_race, injuries, recovery=None, wellness_line=None, heat_protocol=True, coaching_level="mid", planned_block="", cycle=None, fuel_target_g_hr=60, nutrition_race=90, nutrition_label="race target", heat_accl_pct=None, heat_accl_trend="", long_run_cap_km=None, wellness_finalized=True, ask_morning_pain=False, race_block="", ask_weight=False, ask_ankle=True, prescription_note="", post_race_recovery=False, watchdog_flags=None, off_normal=""):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    # No countdown when there is no race to count down to. The footer used to be
    # unconditional, so after an event it printed a negative number of days to a race
    # already run; "?" days would be no better. Say what is actually true instead.
    countdown_line = (f"_{days_to_race} days to {race_name}_"
                      if isinstance(days_to_race, int)
                      else "_No upcoming race set._")

    cycle_block = ""
    cycle_card_line = ""
    cycle_question = ""
    if cycle:
        if cycle.get("phase"):
            cycle_block = (
                f"\n## Menstrual cycle (pre-computed — authoritative)\n"
                f"Cycle day {cycle['day']} — {cycle['phase']} phase: {cycle['cue']}\n"
            )
        if cycle.get("phase") in ("menstrual", "luteal"):
            cycle_card_line = (
                "[🌸 One plain-English line from the cycle block above — what today may "
                "feel like and how to approach targets. No cycle-day numbers, no jargon.]\n"
            )
        if cycle.get("overdue"):
            cycle_question = (
                "- FIRST PRIORITY question (overrides the ones below): the cycle anchor is "
                "overdue — ask \"Has your period started? Reply 'period started' (or "
                "'period started yesterday') so cycle-aware planning stays accurate.\"\n"
            )

    # WHICH QUESTION, DECIDED IN PYTHON. `ask_weight` and `ask_ankle` arrive already
    # gated by lib/ask_gate: eligibility (a weight reading older than 3 days, a run
    # stub still missing its morning score) AND the unanswered-twice backoff. The
    # staleness check used to be the model's job — it read current-state.json and
    # decided — which meant the backoff counter would have been counting a condition
    # nothing could verify. Nothing here re-checks it.
    morning_pain_question = ""
    if injuries and ask_ankle and ask_morning_pain:
        morning_pain_question = (
            "- FIRST PRIORITY (overrides the questions below): ask \"Ankle score this morning? (0-10)\" "
            "— record the answer as injury_pain_next_morning on yesterday's run in session-log.json.\n"
        )
    injury_question = ""
    if injuries and ask_ankle and not ask_morning_pain:
        # Suppressed when the morning-score question fires: at most ONE ankle question per card.
        injury_question = (
            "- If a run is planned today AND ankle.pain_next_morning in current-state.json is >0 "
            "(do NOT use pain_during — that is a run-specific score, not a morning score): "
            "ask \"Injury pain score before heading out? (0-10)\"\n"
        )
    if ask_weight:
        # "Else" only when there is something to be else to: with the ankle branches
        # gated off, a bare "- Else:" is an instruction with no antecedent.
        _lead = "Else: ask" if (morning_pain_question or injury_question) else "Ask"
        injury_question += (
            f"- {_lead} \"Weight this morning?\" (the last-3-days staleness check is "
            "ALREADY done — do not re-check it and do not skip the question because "
            "current-state.json looks recent to you)"
        )
    # ONE question, picked here in priority order (12 Oct 2026: with the cycle question
    # marked "FIRST PRIORITY" in a list beside weight, Sonnet asked Kathryn her weight and
    # Haiku asked about her period). Only the winner reaches the prompt. The run-day ankle
    # question still depends on today's run, so weight stays as its stated fallback.
    if cycle_question:
        injury_question = cycle_question.replace("FIRST PRIORITY question (overrides the ones below): ", "")
    elif morning_pain_question:
        injury_question = morning_pain_question.replace("FIRST PRIORITY (overrides the questions below): ", "")

    injuries_note = (
        "; ".join(
            f"{i.get('location','unknown')}: {i.get('description','')}"
            + (f" — {i.get('protocol','')}" if i.get("protocol") else "")
            for i in injuries
        )
        if injuries else "None"
    )

    recovery_block = ""
    if recovery:
        score  = recovery.get("score", "?")
        label  = recovery.get("label", "?")
        rec    = recovery.get("recommendation", "")
        sigs   = recovery.get("signals", {})
        hrv_r  = sigs.get("hrv",   {}).get("ratio")
        tsb_v  = sigs.get("tsb",   {}).get("value")
        slp_v  = sigs.get("sleep", {}).get("value")
        pain_v = sigs.get("pain",  {}).get("value")
        parts  = []
        if hrv_r  is not None: parts.append(f"HRV ratio {hrv_r:.2f}")
        if tsb_v  is not None: parts.append(f"Form {tsb_v:+.1f}")
        if slp_v  is not None: parts.append(f"sleep {slp_v:.1f}h")
        if pain_v is not None and pain_v > 0: parts.append(f"pain {pain_v}/10")
        recovery_block = (
            f"\n## Recovery score (pre-computed)\n"
            f"Score: {score}/100 — {label}. {rec}\n"
            f"Signals: {', '.join(parts) if parts else 'no data'}.\n"
            f"Use this to modulate session prescription: GREEN = train as planned; "
            f"AMBER = note and monitor; ORANGE = reduce intensity or volume; RED = flag for easy day.\n"
            + ("Today's Form is still recalculating on Intervals.icu and can shift several "
               "points as activities finish syncing — treat any Form-based flag as provisional, "
               "don't state today's Fitness/Fatigue/Form as final.\n"
               if tsb_v is not None and not wellness_finalized else "")
        )

    wellness_block = (
        f"\n## Today's wellness (pre-fetched — for your judgement only; do not print these "
        f"numbers: the only wellness numbers on the card are the 'Off your normal' line "
        f"below, when there is one)\n{wellness_line}\n"
        + (f"\n## Off your normal (pre-computed — copy EXACTLY as one card line)\n{off_normal}\n"
           if off_normal else "")
        if wellness_line else
        "\n## Today's wellness\nNot yet synced — say nothing about sleep, HRV or resting HR.\n"
    )

    planned_section = (
        f"\n## Today's planned sessions (pre-computed — authoritative)\n{planned_block}\n"
        if planned_block else ""
    )

    heat_block = ""
    # Post-race recovery (plan_tools.in_post_race_recovery): Form is high BECAUSE the
    # athlete is recovering, so "fresh legs, good day for quality" is exactly wrong
    # (sent to Jamie on 25 Sep, six days after IM Italy).
    if post_race_recovery:
        form_line_rule = ("[Form line — OMIT ENTIRELY: post-race recovery. High Form is "
                          "expected and is never a cue for quality work.]")
    elif off_normal:
        # "Fresh legs, good day for quality" beside "resting HR off your normal" told
        # Kathryn two opposite things on one card (shadow week, 14 Oct 2026). Training
        # freshness says nothing about recovery when wellness is off, so no green line.
        form_line_rule = """[Form line — only include if notable:
  · Form < −20: ⚠️ Heavy load today — keep intensity in check
  · Otherwise omit entirely: wellness is off normal today, so never call it a good day for quality]"""
    else:
        form_line_rule = """[Form line — only include if notable:
  · Form < −20: ⚠️ Heavy load today — keep intensity in check
  · Form > +10: 🟢 Fresh legs — good day for quality work
  · Form −1 to −20: omit entirely, that's normal training]"""
    heat_card_line = ""
    if heat_protocol:
        # The acclimation score is decision-support for the model, never card copy:
        # §6.2 is explicit that the card carries no score, label, ratio or internal
        # metric, and a bare "62% ↑" is unrankable by the athlete anyway (R5). The
        # actionable half — bath due, N this week against a 2–3x target — is the
        # whole message, so the card line is the same whether or not a score exists.
        if heat_accl_pct is not None:
            heat_block = (
                f"\n## Heat acclimation (pre-computed — authoritative)\n"
                f"Score: {heat_accl_pct}%{heat_accl_trend}. Use this ONLY to decide whether to "
                f"flag heat work — do NOT show the score, the percentage or the trend arrow to "
                f"the athlete, and do not recompute it.\n"
            )
        heat_card_line = ("[If sessions_this_week < 2 AND today is Wednesday or later: "
                          "🌡️ Heat bath due — [N] this week (target 2–3×)]")

    # Pre-verified in Python against the calendar fetched JUST NOW for this send (see
    # run_athlete) — never against the events the 05:00 run saw. The file is written
    # hours before this card goes out and the wait-for-Garmin-sync retry can push the
    # send later still, so a same-day edit (swim moved, session swapped) between the
    # 05:00 write and now leaves the file naming a session that is no longer today's
    # plan. Passed in pre-checked rather than as a file path so the model cannot
    # trust a stale line just because its date stamp still says today.
    prescription_block = (
        f"\n## 05:00 prescription check (pre-verified against today's live calendar — authoritative)\n"
        f"{prescription_note}\n"
        if prescription_note else ""
    )

    flags_block = ("\n## Watchdog flags to show (pre-selected — authoritative; show each, no others)\n"
                   + ("\n".join(f"- {f}" for f in watchdog_flags) if watchdog_flags else "- none today")
                   + "\n")

    long_run_cap_block = ""
    if long_run_cap_km is not None:
        long_run_cap_block = (
            f"\n## Long-run cap (pre-computed — authoritative)\n"
            f"Ceiling: {long_run_cap_km:.1f} km — do not reference or target a distance above this for today's long run.\n"
        )

    return f"""\
You are generating the morning briefing for {first_name}'s training day.

{_level_block(coaching_level)}
{illness_lib.prompt_block(slug, first_name=first_name)}
{recovery_block}{wellness_block}{flags_block}{cycle_block}{planned_section}{race_block}{long_run_cap_block}{heat_block}{prescription_block}
Step 1 — Fetch data via Bash. EVERY icu_fetch.py call you make must carry
`--caller morning-checkin`. This job reads what is planned and asks how the athlete is; it
does not write the calendar, and that flag is what makes the refusal automatic rather than a
rule you have to remember:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --caller morning-checkin --endpoint events --start {today} --end {today}

Step 2 — Read:
- ClaudeCoach/athletes/_shared/persistent-rules.md (GLOBAL coaching rules - apply to every athlete)
- ClaudeCoach/athletes/{slug}/persistent-rules.md (permanent coaching rules — these override defaults and MUST be followed)
- ClaudeCoach/athletes/{slug}/current-state.md (open actions and travel blocks; watchdog flags are already chosen in the block below — do not pick others from this file)
- ClaudeCoach/athletes/{slug}/current-state.json (weight_readings, injury pain scores)
{"- ClaudeCoach/athletes/" + slug + "/heat-log.json (count entries in current ISO week to get sessions_this_week)" if heat_protocol else ""}
- ClaudeCoach/athletes/{slug}/session-log.json — only if today's planned event is a Ride or Brick >90 min: extract the last 4 entries with sport Ride/GravelRide/Brick, duration_min ≥ 90, and nutrition_g_carb set. Compute each g_per_hr and the avg.

Step 3 — The question. AT MOST ONE in the whole card, and ONLY from this list — the
list is already filtered for what this athlete has recently answered and for questions
they have stopped answering, so anything absent from it must NOT be asked, invented or
reworded back in:
{injury_question or "- (none authorised today)"}
- Else: no question. A card with no question is a complete card.

Step 4 — Output the Today note in Markdown (no preamble, no sign-off). It sits at the top
of the athlete's Today tab in Peak, directly above today's session, the date and the race
countdown, so it never repeats them as headings (Jamie, 15 Oct 2026: "just describe how
training's going, for example you've got a big session today but you're fresh from the
week, this is what to think about").

Use the recovery score and signals ONLY to decide what to flag — do NOT show the score, label, HRV ratio, or any internal metric to the athlete. Write like a coach talking, not a dashboard.

[Opening: TWO sentences at most, in a coach's voice. How training is going right now (the
 week so far, how fresh or loaded they are) and what that means for today's session: the one
 thing to think about. e.g. "Big ride today, and you're fresh after an easy week: hold the
 first hour back and finish strong." Any session name, duration or Load comes ONLY from the
 pre-computed planned-sessions block above, verbatim; never estimate Load. Rest day: say what
 the rest is doing for them, in one sentence.]

{form_line_rule}
[If the "Off your normal" block above is present: 📉 that line, word for word]
[If recovery ORANGE or RED: ⚠️ [one plain-English sentence on what to do differently — no scores]]
[For EACH flag in the "Watchdog flags to show" block: ⚠️ [that flag in plain English, one line, keeping its numbers]. None listed: no flag line.]
[If the pre-verified 05:00 prescription check block above is present and says today's session was modified or swapped: 🔁 [what changed and why — one plain line, e.g. "Swapped to easy spin — HRV low". If it confirmed the session as planned, or the block is absent, say nothing]]
{cycle_card_line}
[If today's session is Ride or Brick >90 min: 🍌 Nutrition — target {fuel_target_g_hr}g/hr (progress toward {nutrition_race}g/hr {nutrition_label}) · eat at 15 min then every 25 min]
[If any travel block, race, or constraint from current-state.md "Travel & training blocks" STARTS or ENDS within 5 days, or a training plan or goal block starts within 5 days: 📌 [what] in [N] days — [one-line impact], e.g. "Back from your trip in 2 days. Monday's hamstring strength starts the week."]
[If open action is due within 3 days: 📌 [action] due [date]]
{heat_card_line}

[Question if applicable — one line, LAST]

ORDER is fixed: the opening, the flag/📌/🍌 lines, then the question (if any) last. No
greeting, no date line, no "Today:" line, no countdown: Peak shows those right beside it.

Rules:
- SHORT (Jamie, 15 Oct 2026: "it's like an essay"): the card is read in five seconds.
  Every ⚠️ / 📌 / 🔁 line is ONE sentence of at most 18 words: what to do, then the number
  that says why. No reasoning, no dates of individual readings, no "I'd offer". At most TWO
  ⚠️ lines: keep the two that change what the athlete does today; drop the rest.
  e.g. "⚠️ HRV is down 8% on last week, so keep today easy."
  e.g. "⚠️ Two sessions missed this week; don't make them up."
- Sleep/HRV/RHR: use ONLY the pre-fetched wellness line above — never infer or estimate values yourself.
- Omit any section that has nothing to say — do not pad with dashes or "N/A".
- Never ask for subjective mood, fatigue, or motivation scores.
- Never put two questions in the card, and never re-ask something the athlete already
  told you — if a value is in the data above, state it or leave it, don't ask for it.

Wrap your entire output in <telegram> and </telegram> tags. Output nothing outside those tags — no preamble, no reasoning, no tool commentary."""


def _log_to_history(slug: str, message: str) -> None:
    """Append an outbound notification to the athlete's Telegram history so the bot has context for replies."""
    history_file = BASE / "athletes" / slug / "telegram" / "history.json"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(history_file.read_text()) if history_file.exists() else []
    except Exception:
        history = []
    history.append({"user": "", "assistant": message,
                    "ts": datetime.now().isoformat(timespec="seconds")})
    history_file.write_text(json.dumps(history[-30:], indent=2))


def notify(msg, chat_id, slug=""):
    """Send via notify.py, retry once. Returns True only if the send succeeded —
    the caller uses this to decide whether to write the sent-today sentinel."""
    for _attempt in (1, 2):
        try:
            # --no-history: this script appends to history itself after the send
            r = subprocess.run(
                ["python3", str(NOTIFY), "--no-history", "--chat-id", str(chat_id), msg],
                cwd=PROJECT_DIR, timeout=15,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    ops_log.alert("morning-checkin", "Telegram send failed after retry — card not delivered",
                  athlete=slug)
    return False


_CHART_SPORT_MAP = {
    "VirtualRide": "Ride", "GravelRide": "Ride", "MountainBikeRide": "Ride",
    "EBikeRide": "Ride", "Cycling": "Ride", "TrailRun": "Run",
    "VirtualRun": "Run", "OpenWaterSwim": "Swim",
    "WeightTraining": "Strength", "Workout": "Strength", "Elliptical": "Strength",
}


def _build_load_chart_payload(today, wellness_rows, history_acts, events):
    """Pure payload builder for the morning load chart (±8 days) — extracted from
    the send path so the day construction is testable. The today-bar rule lives
    here: today shows completed activities PLUS planned sessions whose sport has
    not been completed yet (at a 06:30 send nothing is completed, which is why an
    earlier completed-only version rendered today permanently empty)."""
    seed_ctl = seed_atl = None
    if wellness_rows:
        w = wellness_rows[-1]
        seed_ctl = round(float(w.get("ctl") or 0), 1)
        seed_atl = round(float(w.get("atl") or 0), 1)

    tsb_by_date = {}
    for w in wellness_rows or []:
        d = (w.get("id") or "")[:10]
        if d:
            tsb_by_date[d] = round((w.get("ctl") or 0) - (w.get("atl") or 0), 1)

    acts_by_date = {}
    for act in (history_acts or []):
        d = (act.get("start_date_local") or "")[:10]
        if not d:
            continue
        sport = _CHART_SPORT_MAP.get(act.get("type", ""), act.get("type", "Other"))
        tss = round(float(act.get("icu_training_load") or 0), 1)
        dur = round((act.get("moving_time") or 0) / 60)
        acts_by_date.setdefault(d, []).append(
            {"sport": sport, "tss": tss, "dur": dur, "status": "completed"}
        )

    # Planned events by date (today and future)
    plans_by_date = {}
    for ev in (events or []):
        d = (ev.get("start_date_local") or "")[:10]
        if not d or d < today.isoformat():
            continue
        raw   = ev.get("type") or ev.get("category") or ""
        sport = _CHART_SPORT_MAP.get(raw, raw) or "Other"
        tss = round(float(ev.get("icu_training_load") or ev.get("load_target") or 0), 1)
        dur = round((ev.get("moving_time") or 0) / 60)
        plans_by_date.setdefault(d, []).append(
            {"sport": sport, "tss": tss, "dur": dur, "status": "planned"}
        )

    days = []
    for i in range(-8, 8):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        is_future = d > today
        if is_future:
            acts = plans_by_date.get(d_str, [])
        elif d == today:
            done = acts_by_date.get(d_str, [])
            done_sports = {a["sport"] for a in done}
            acts = done + [p for p in plans_by_date.get(d_str, [])
                           if p["sport"] not in done_sports]
        else:
            acts = acts_by_date.get(d_str, [])
        days.append({
            "date": d_str,
            "tsb": None if is_future else tsb_by_date.get(d_str),
            "activities": acts,
        })

    return {
        "today": today.strftime("%m-%d"),
        "seed_ctl": seed_ctl,
        "seed_atl": seed_atl,
        "days": days,
    }


def _chart_note(payload) -> str:
    """What the morning load chart shows, in words, for the coach's history (notify.py
    --note): an athlete can ask about the chart, and the coach only saw "[chart sent]"."""
    def day(d):
        acts = d.get("activities") or []
        tot = round(sum(a.get("tss") or 0 for a in acts))
        sports = "+".join(sorted({a.get("sport") or "?" for a in acts}))
        return f"{date.fromisoformat(d['date']).strftime('%a %d')} {tot} ({sports})" if acts else None
    today = date.today().isoformat()
    done = [x for x in (day(d) for d in payload["days"] if d["date"] < today) if x]
    plan = [x for x in (day(d) for d in payload["days"] if d["date"] >= today) if x]
    form = next((d.get("tsb") for d in reversed(payload["days"])
                 if d["date"] <= today and d.get("tsb") is not None), None)
    return ("Morning training-load chart: daily Load bars by sport, last 8 days done (solid) "
            "and next 8 planned (faded), with a Form line. Done: " + ("; ".join(done) or "none")
            + ". Planned: " + ("; ".join(plan) or "none")
            + (f". Form today {form:+.1f}." if form is not None else "."))


def _send_morning_load_chart(chat_id, slug, wellness_rows, coaching_level="mid"):
    """Generate and send the training load chart (±8 days) as part of the morning brief."""
    try:
        import charts as _charts
        from icu_api import IcuClient

        athletes_cfg = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes_cfg[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])

        today = date.today()
        end_date = (today + timedelta(days=8)).isoformat()

        history_acts, events = client.fetch_all(
            ("get_training_history", 10),
            ("get_events", today.isoformat(), end_date),
        )

        payload = _build_load_chart_payload(today, wellness_rows, history_acts, events)
        png = _charts.load_chart(payload, coaching_level=coaching_level)
        if png:
            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tf.write(png)
                tmp_path = tf.name
            try:
                subprocess.run(
                    ["python3", str(NOTIFY), "--chat-id", str(chat_id), "--note",
                     _chart_note(payload), "--photo", tmp_path],
                    cwd=PROJECT_DIR, timeout=60,
                )
            finally:
                os.unlink(tmp_path)
    except Exception as e:
        print(f"[morning chart] {e}", file=sys.stderr)


def _race_focus(slug, profile, athlete_cfg):
    """Focus points for the pre-race message: a curated profile.json race_focus list if
    there is one, otherwise derived from the athlete's own standing rules and logged
    fuelling (lib/races.py derive_focus). Every failure mode degrades to NO focus points
    rather than to a guess — an empty list simply drops that sentence from the message."""
    try:
        rules_f = BASE / f"athletes/{slug}/persistent-rules.md"
        rules = rules_f.read_text() if rules_f.exists() else ""
        avg = None
        try:
            log_f = BASE / f"athletes/{slug}/session-log.json"
            if log_f.exists():
                avg = recent_avg_g_hr(json.loads(log_f.read_text()))
        except Exception:
            pass
        return races_lib.focus_for(profile, rules, avg,
                                   athlete_cfg.get("nutrition_target_g_hr"))
    except Exception as exc:
        print(f"[{slug}] race focus derivation failed: {exc}", file=sys.stderr)
        return []


_FLAG_RE = None


def _recent_flags(adir: Path, today: date, days: int = 3, cap: int = 2,
                  skip: frozenset = frozenset()) -> list[str]:
    """Watchdog flags the card shows, chosen here (11 Oct 2026). "If watchdog flag active"
    was left to the model: on the same card Haiku showed Kathryn's real T10 run-volume flag
    and Sonnet left it out. Flags dated within `days`, minus any whose own text says no
    action is needed (Jamie's paused-heat T7) and T9 (the open-actions list: internal
    housekeeping, and the card has its own open-action line), newest first then Tier 1,
    at most `cap`."""
    import re
    global _FLAG_RE
    _FLAG_RE = _FLAG_RE or re.compile(
        r"^- \*\*(\d{4}-\d{2}-\d{2}) — (T\d+) \(Tier (\d)\):\*\*\s*(.+)$")
    try:
        text = (adir / "current-state.md").read_text()
    except OSError:
        return []
    sec = text.split("## Watchdog flags", 1)
    if len(sec) < 2:
        return []
    body = sec[1].split("\n## ", 1)[0]
    out = []
    for line in body.splitlines():
        m = _FLAG_RE.match(line.strip())
        if not m:
            continue
        try:
            age = (today - date.fromisoformat(m.group(1))).days
        except ValueError:
            continue
        if (not 0 <= age <= days or "no action" in m.group(4).lower() or m.group(2) == "T9"
                or m.group(2) in skip):
            continue
        out.append((int(m.group(3)), m.group(1), f"{m.group(2)}: {m.group(4).strip()}"))
    out.sort(key=lambda x: (x[1], -x[0]), reverse=True)     # newest date, then Tier 1
    return [t for _, _, t in out[:cap]]


BASELINE_DAYS = 28


def _baseline_rows(slug: str) -> list:
    """BASELINE_DAYS + today of wellness, for _off_normal_line only: the recovery score
    keeps its own short window (it reads every row it is given)."""
    try:
        from icu_api import IcuClient
        a = json.loads(ATHLETES_CONFIG.read_text())[slug]
        return IcuClient(a["icu_athlete_id"], a["icu_api_key"]).get_wellness(BASELINE_DAYS + 1)
    except Exception:
        return []


def _off_normal_line(wellness_rows, today: date) -> str:
    """The wellness numbers the card shows: only those off THIS athlete's normal, with the
    normal beside them (Jamie, 12 Oct 2026: "show only when off normal"). Normal = the mean
    of the previous BASELINE_DAYS days with data (needs half of them; Jamie: "7 day isn't
    enough for a normal baseline"). HRV / resting HR off = lib/wellness_normal.py (SD-based,
    morning or 7-day trend); sleep off = 1h+ short AND under 7h. "" on a normal day or before last night's sleep syncs."""
    rows = {(r.get("id") or "")[:10]: r for r in wellness_rows or []}
    t = rows.get(today.isoformat())
    if not t or t.get("sleepSecs") is None:
        return ""
    prev = [rows[k] for k in sorted(rows) if k < today.isoformat()][-BASELINE_DAYS:]

    def base(key, f=lambda v: v):
        vals = [f(r[key]) for r in prev if r.get(key) is not None]
        return sum(vals) / len(vals) if len(vals) >= BASELINE_DAYS // 2 else None
    # HRV and resting HR: SD-based, against the athlete's own spread, on the 7-day
    # average as well as the morning (lib/wellness_normal.py; Jamie, 14 Oct 2026, after
    # the literature: replaces the flat "HRV 10% below / resting HR 6% above").
    import wellness_normal as WN
    out = []
    for key, name in (("hrv", "HRV"), ("restingHR", "resting HR")):
        a = WN.assess(wellness_rows, key, today)
        if not a:
            continue
        if a["day_off"]:
            out.append(f"{name} {a['today']:.0f} (usual {a['usual']:.0f})")
        elif a["trend_off"]:
            out.append(f"{name} 7-day average {a['avg7']:.0f} (usual {a['usual']:.0f})")
    sl, sb = t["sleepSecs"] / 3600, base("sleepSecs", lambda v: v / 3600)
    if sb and sl <= sb - 1 and sl < 7:
        out.append(f"sleep {sl:.1f}h (usual {sb:.1f}h)")
    return ("Off your normal: " + " · ".join(out)) if out else ""


def _consistency_line(slug, adir, athlete_cfg, wellness_rows, card) -> str | None:
    """The consistency case to append to today's card (lib/consistency.py), or None."""
    today = date.today()
    if not consistency.due(adir, today) or _post_race_recovery(athlete_cfg):
        return None
    if illness_lib.state_from_dir(adir, today):
        return None
    from icu_api import IcuClient
    a = json.loads(ATHLETES_CONFIG.read_text())[slug]
    client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
    start = today - timedelta(days=consistency.LOOKBACK_MISS + consistency.LOOKBACK_OVER)
    events = client.get_events(start.isoformat(), (today - timedelta(days=1)).isoformat())
    acts = client.get_training_history(days=(today - start).days + 1)
    r = consistency.assess(events, acts, wellness_rows, today)
    if not r:
        return None
    if r["case"] == "ask" and any(l.rstrip().endswith("?") for l in card.splitlines()):
        return None                     # one question per card: try again tomorrow
    return r["case"]


def _flag_skip(athlete_cfg, wellness_rows) -> frozenset:
    """Triggers the card must not show this week: T6 (drift) outside a durability-building
    week, the same rule the watchdog applies (lib/watchdog_checks.durability_phase), so a
    T6 logged before the rule existed is not shown either."""
    try:
        import watchdog_checks as wc
        ctl = next((w.get("ctl") for w in reversed(wellness_rows or [])
                    if w.get("ctl") is not None), None)
        return frozenset() if wc.durability_phase(athlete_cfg, ctl, date.today()) \
            else frozenset({"T6"})
    except Exception:
        return frozenset()


def _post_race_recovery(athlete_cfg) -> bool:
    try:
        import plan_tools as _pt
        return _pt.in_post_race_recovery(athlete_cfg or {})
    except Exception:
        return False


def _block_fact(athlete_cfg, today):
    """One true, already-computed fact about the work behind the race (§8.6 "name the
    work behind it"). Weeks since plan_start is the only such fact available for every
    athlete without inventing anything; no plan_start means no claim is made."""
    ps = athlete_cfg.get("plan_start")
    if not ps:
        return ""
    try:
        weeks = (today - date.fromisoformat(ps)).days // 7
    except Exception:
        return ""
    if weeks < 2:
        return ""
    return f"You have {weeks} weeks of work behind you for this one"


def run_athlete(slug, athlete_cfg, early=False):
    """early=True is the just-after-midnight run: the Today note from the plan and the
    load, before any wellness exists. The morning runs then rewrite it once sleep / HRV /
    resting HR land, and notify (lib/today_note.py)."""
    adir = BASE / f"athletes/{slug}"
    chat_id = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "morning-checkin.log"
    if not chat_id:
        print(f"[{slug}] SKIP: no chat_id in athletes.json", file=sys.stderr)
        return

    today_str = date.today().isoformat()

    # Sentinel: skip if card already sent today
    sentinel = LOG_DIR / f"morning-sent-{slug}-{today_str}.flag"
    if sentinel.exists():
        return
    if early and today_note.read(slug):
        return

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    race_name = profile.get("race_name") or athlete_cfg.get("race_name", "your race")
    race_date_str = profile.get("race_date") or athlete_cfg.get("race_date", "")
    injuries = [_i for _i in profile.get("injuries", []) if str(_i.get("status") or "").lower() not in ("cleared", "resolved")]   # cleared = not current
    # Morning heat nudges only once the formal race−4wk block has begun; before
    # that the watchdog's maintenance-dose check owns heat visibility.
    # "surface", not "in_protocol_window": profile heat_silent suppresses the
    # proactive nudge without touching the dose model or the score.
    heat_protocol = heat_lib.state(slug, profile)["surface"]

    # Pre-compute 0–100% acclimation score so the card shows it without Claude guessing.
    heat_accl_pct = None
    heat_accl_trend = ""
    if heat_protocol and (adir / "heat-log.json").exists():
        try:
            _score_now = heat_lib.acclimation_score(slug)
            _score_7d  = heat_lib.acclimation_score(slug, date.today() - timedelta(days=7))
            _delta = _score_now - _score_7d
            heat_accl_pct = round(_score_now)
            if abs(_delta) > 5:
                heat_accl_trend = " ↑" if _delta > 0 else " ↓"
        except Exception:
            pass

    # Race-aware branch. Until now `race_date` was structured data that nothing
    # branched on: on race morning this function rendered the ordinary training card
    # with a 0 in the countdown (docs/tone-of-voice-guide.md §8.6/§8.7). On race day
    # and the days just after, the card is REPLACED by a deterministic message rather
    # than generated — §8.6's first rule is "do not introduce anything new", and a
    # generative prompt on race morning is precisely how a new pacing target arrives
    # at 06:30. Race-week and race-eve stay generative and only get a prompt block,
    # because those days still carry a real session.
    _races = races_lib.load_races(slug, {slug: athlete_cfg})
    _phase = races_lib.race_phase(_races, date.today())
    if _phase["phase"] in ("race_day", "race_completed"):
        if _phase["phase"] == "race_day":
            msg = races_lib.render_pre_race(
                _phase["race"], first_name, phase="race_day",
                block_fact=_block_fact(athlete_cfg, date.today()),
                # Focus points must already be in the agreed race plan (§8.6: "every one
                # must be something already trained and already agreed"), so they are
                # READ from the profile and never generated. No athlete has this key yet,
                # so nothing renders until someone curates it.
                focus=_race_focus(slug, profile, athlete_cfg))
        else:
            # good_day is left unknown on purpose: nothing in the system yet tells us
            # whether the race went well, and §8.6 splits the good day from the bad one
            # before writing a word. Unknown means ASK, not assume.
            msg = races_lib.render_post_race(_phase["race"], first_name, good_day=None)
        if notify(msg, chat_id, slug=slug):
            sentinel.touch()
            if _phase["phase"] == "race_completed":
                # Mark AFTER a successful send so a failed send retries next run, and so
                # the question is asked once rather than every morning in the window.
                try:
                    races_lib.mark_post_race_sent(slug, _phase["race"]["date"])
                except Exception as exc:
                    print(f"[{slug}] could not mark post-race sent: {exc}", file=sys.stderr)
            ops_log.record_run("morning-checkin", athlete=slug, ok=True,
                               detail=f"{_phase['phase']} message sent")
            try:
                _log_to_history(slug, msg)
            except Exception:
                pass
        return

    # Countdown to the NEXT race, not to the legacy race_date. Off the raw field this
    # went negative the day after the event and the card footer read "-12 days to <race>"
    # until someone repointed race_date by hand (races_lib.countdown).
    days_to_race, _next_name = races_lib.countdown(slug, athlete_cfg)
    if days_to_race is None:
        days_to_race = "?"
    elif _next_name:
        race_name = _next_name

    # Pre-compute recovery score and extract today's wellness values directly
    recovery = None
    wellness_line = None
    wellness_rows = []
    baseline_rows = []
    wellness_finalized = True  # False = today's CTL/ATL/Form still recalculating on ICU
    has_sleep_device = False  # True if athlete has ever had sleep data
    try:
        from icu_api import IcuClient
        import recovery_score as rs
        athletes_cfg = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes_cfg[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        wellness_rows = client.get_wellness(8)
        has_sleep_device = any(r.get("sleepSecs") is not None for r in wellness_rows)
        for row in wellness_rows:
            if (row.get("id") or "")[:10] == today_str:
                wellness_finalized = row.get("wellness_finalized", True)
                break
        # ICU/Garmin stores last night's sleep under the WAKING day = today's date
        sleep_date = date.today().isoformat()
        print(f"[{slug}] querying sleep data for {sleep_date}", file=sys.stderr)
        for row in wellness_rows:
            if (row.get("id") or "")[:10] == sleep_date:
                sleep_secs = row.get("sleepSecs")
                hrv_val    = row.get("hrv") or row.get("hrvSdnn")
                rhr_val    = row.get("restingHR")
                # Only use the row if sleep has actually synced — RHR alone is not enough
                if sleep_secs is not None:
                    sleep_score = row.get("sleepScore")
                    steps_val   = row.get("steps")
                    parts = [f"Sleep: {sleep_secs/3600:.1f}h"]
                    if sleep_score is not None: parts.append(f"Score: {int(sleep_score)}")
                    if hrv_val    is not None: parts.append(f"HRV: {int(hrv_val)}")
                    if rhr_val    is not None: parts.append(f"RHR: {int(rhr_val)} bpm")
                    if steps_val  is not None: parts.append(f"Steps: {int(steps_val):,}")
                    wellness_line = " · ".join(parts)
                break
        if wellness_line is None:
            # Guard: if today's sleep hasn't synced yet, check whether yesterday has data
            # so we can warn rather than silently appear as "not yet synced"
            prev_day = (date.today() - timedelta(days=1)).isoformat()
            for row in wellness_rows:
                if (row.get("id") or "")[:10] == prev_day and row.get("sleepSecs") is not None:
                    print(
                        f"[{slug}] WARNING: no sleep data for {sleep_date}; "
                        f"stale data exists for {prev_day} — not using it",
                        file=sys.stderr,
                    )
                    break
        # 28-day HRV baseline (rs.BASELINE_DAYS): its own fetch, so everything else that
        # reads wellness_rows keeps its 8 days. Same rows feed the off-normal line.
        baseline_rows = _baseline_rows(slug) or wellness_rows
        hrv_t, hrv_b, tsb, sleep, sleep_score = rs._parse_wellness(baseline_rows)
        pain = 0
        state_f = adir / "current-state.json"
        if state_f.exists():
            ankle = json.loads(state_f.read_text()).get("ankle", {})
            # Use today's resting pain if logged today; otherwise use next-morning pain.
            # Never use pain_during — that's an in-run score and is not morning-relevant.
            resting_today = ankle.get("pain_today_resting_date") == today_str
            pain = (ankle.get("pain_today_resting", 0) if resting_today
                    else ankle.get("pain_next_morning", 0)) or 0
        recovery = rs.compute(hrv_t, hrv_b, tsb, sleep, pain,
                              in_taper=rs.in_taper(slug), sleep_score=sleep_score)
    except Exception:
        pass  # score is optional — morning card still sends without it

    # Does yesterday's run stub still need the next-morning pain score?
    needs_morning_pain_ask = False
    if injuries:
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()
        try:
            cs_f = adir / "current-state.json"
            cs_data = json.loads(cs_f.read_text()) if cs_f.exists() else {}
            already_asked = cs_data.get("ankle", {}).get("pain_next_morning_asked_date") == today_str
            if not already_asked:
                sl_f = adir / "session-log.json"
                if sl_f.exists():
                    for e in json.loads(sl_f.read_text()):
                        if (e.get("date") == yesterday_str
                                and e.get("sport") in ("Run", "TrailRun", "VirtualRun")
                                and e.get("injury_pain_next_morning") is None):
                            needs_morning_pain_ask = True
                            break
        except Exception:
            pass

    # STANDING-ASK BACKOFF (lib/ask_gate). Weight was asked four mornings running with
    # nothing coming back (13 Aug audit) because nothing counted the silence: the card
    # is regenerated from scratch each day and the only gate was "no reading in 3 days",
    # which an athlete who has stopped weighing in satisfies forever.
    #
    # Eligibility is computed here, in Python, so the counter has something real to
    # count; the backoff record is persisted NOW, before the send, because the reset and
    # the miss are facts about mornings that have already happened. What is deliberately
    # NOT recorded yet is that the ask went out — the card is generative and the model can
    # still drop the question, so `last_asked` is written after the send, from the text.
    _cs_data, _sl_data = {}, []
    try:
        _cs_f = adir / "current-state.json"
        if _cs_f.exists():
            _cs_data = json.loads(_cs_f.read_text())
    except Exception:
        pass
    try:
        _sl_f2 = adir / "session-log.json"
        if _sl_f2.exists():
            _sl_data = json.loads(_sl_f2.read_text())
    except Exception:
        pass

    ask_weight = False
    ask_ankle = bool(injuries)
    passive_lines = []
    try:
        _gate_state = ask_gate.load_state_from_dir(adir)
        for _q, _eligible in ((ask_gate.WEIGHT,
                               ask_gate.weight_reading_due(_cs_data, date.today(),
                                                           wellness_rows=wellness_rows)),
                              (ask_gate.ANKLE_SCORE, bool(injuries))):
            _gate_state, _dec = ask_gate.decide(
                _gate_state, _q, today=date.today(),
                answered_on=ask_gate.answer_date(_q, current_state=_cs_data,
                                                 session_log=_sl_data,
                                                 wellness_rows=wellness_rows))
            if _q == ask_gate.WEIGHT:
                ask_weight = _eligible and _dec["ask"]
            else:
                ask_ankle = _eligible and _dec["ask"]
            # The passive line only makes sense for a question we would otherwise be
            # asking: no point telling an athlete with no injury that ankle scores have
            # gone quiet.
            if _eligible and not _dec["ask"] and _dec["passive"]:
                passive_lines.append((_q, _dec["passive"]))
        ask_gate.save_state_in(adir, _gate_state)
    except Exception as exc:
        # Fail OPEN: an unreadable gate must not silence the card's only question.
        print(f"[{slug}] standing-ask gate failed ({exc}) — asking as normal",
              file=sys.stderr)
        ask_weight = ask_gate.weight_reading_due(_cs_data, date.today(),
                                                 wellness_rows=wellness_rows)
        ask_ankle = bool(injuries)
        passive_lines = []

    # Wait for Garmin sync only if the athlete has a sleep device and it hasn't synced yet.
    # Athletes with no sleep device (sleepSecs always null) send immediately.
    if not early and wellness_line is None and has_sleep_device and datetime.now().hour < 9:
        print(f"[{slug}] wellness not yet synced — will retry", file=sys.stderr)
        return

    # Pre-compute today's planned-session TSS in Python — the 11 Jun card guessed
    # "~35 TSS" for a swim whose plan event carried load_target=60.
    planned_block = ""
    _events = []  # kept for long-run cap computation below
    try:
        from icu_api import IcuClient as _Icu
        _a = json.loads(ATHLETES_CONFIG.read_text())[slug]
        _cl = _Icu(_a["icu_athlete_id"], _a["icu_api_key"])
        _events = _cl.get_events(today_str, today_str)
        # A session already ridden or run this morning is DONE, not today's plan (Fred,
        # 15 Oct 2026: the 08:00 card announced a cut threshold session he'd finished at 07:00).
        _fam = lambda t: next((f for f in ("Ride", "Run", "Swim") if f in str(t or "")), str(t or ""))
        _acts_today = [x for x in (_cl.get_training_history(days=1) or [])
                       if str(x.get("start_date_local") or "")[:10] == today_str]
        _done_fams = {_fam(x.get("type")) for x in _acts_today}
        _done_names = [str(x.get("name") or x.get("type")) for x in _acts_today]
        _events = [e for e in _events
                   if not ((e.get("category") or "WORKOUT").upper() == "WORKOUT"
                           and (e.get("paired_activity_id") or _fam(e.get("type")) in _done_fams))]
        planned_block = planned_sessions_block(_events)
        if _done_names:
            planned_block = ((planned_block + "\n") if planned_block else "") + (
                "Already done today (say so in one line if relevant; never present it as still to do "
                "or describe changes to it): " + "; ".join(_done_names))
    except Exception as exc:
        print(f"[{slug}] planned-TSS prefetch failed: {exc}", file=sys.stderr)

    # The 05:00 prescription check (daily-prescription.py) named a session hours
    # before this card sends, and the Garmin-sync wait above can push the send
    # later still — a same-day calendar edit in that window (session swapped,
    # moved, or the day rebuilt) leaves the file talking about a session that is
    # no longer on today's calendar. Cross-check it against `_events`, the fetch
    # this run JUST made, rather than trusting the model to notice a mismatch.
    prescription_note = ""
    try:
        presc_f = adir / "daily-prescription-latest.md"
        if presc_f.exists():
            presc_text = presc_f.read_text()
            date_m = re.match(r"date:\s*(\d{4}-\d{2}-\d{2})", presc_text)
            if date_m and date_m.group(1) == today_str:
                summary = presc_text.split("\n\n", 1)[1].strip() if "\n\n" in presc_text else ""
                presc_session = summary.split(":", 1)[0].strip().lower()
                today_names = [str(e.get("name") or "").lower() for e in _events
                               if (e.get("category") or "WORKOUT").upper() == "WORKOUT"]
                if summary and presc_session and any(presc_session in n for n in today_names):
                    prescription_note = summary
                elif summary:
                    print(f"[{slug}] 05:00 prescription named '{presc_session}' — no longer "
                          f"on today's calendar, dropping from card", file=sys.stderr)
    except Exception as exc:
        print(f"[{slug}] prescription cross-check failed: {exc}", file=sys.stderr)

    coaching_level = profile.get("coaching_level", "mid")

    # Menstrual-cycle phase (tracking athletes only) — same wellness rows give the
    # same-day ICU override if the athlete also logs the phase in intervals.icu.
    cycle = None
    try:
        cycle = menstrual_lib.phase_for(slug, profile=profile, wellness=wellness_rows)
    except Exception:
        pass

    # Deterministic fuelling target (gap-closing ramp, never the old avg+10 guess).
    try:
        _sl = json.loads((adir / "session-log.json").read_text()) if (adir / "session-log.json").exists() else []
        _fuel_target_g_hr = fuel_target(recent_avg_g_hr(_sl), fuel_basis.ceiling(athlete_cfg))
    except Exception:
        _fuel_target_g_hr = int(athlete_cfg.get("nutrition_alert_threshold_g_hr") or 60)

    # Pre-compute long-run distance cap — only fires when today's calendar has a
    # long-run WORKOUT event. See lib/progression.py for the cap rule itself.
    _long_run_cap_km = None
    try:
        from primitives.modulation import classify_session_type as _lr_classify
        from progression import long_run_cap_km as _lr_cap
        _lr_sl_path = adir / "session-log.json"
        _lr_sl = json.loads(_lr_sl_path.read_text()) if _lr_sl_path.exists() else []
        _long_run_cap_km = _lr_cap(_events, _lr_sl, _lr_classify)
    except Exception as exc:
        print(f"[{slug}] long-run cap pre-compute failed: {exc}", file=sys.stderr)

    prompt = _build_prompt(slug, first_name, race_name, race_date_str, days_to_race, injuries, recovery,
                           wellness_line=wellness_line, heat_protocol=heat_protocol,
                           coaching_level=coaching_level, planned_block=planned_block,
                           race_block=races_lib.prompt_block(_races, date.today()),
                           cycle=cycle,
                           fuel_target_g_hr=_fuel_target_g_hr,
                           nutrition_race=fuel_basis.ceiling(athlete_cfg),
                           nutrition_label=fuel_basis.label(athlete_cfg),
                           ask_morning_pain=needs_morning_pain_ask,
                           ask_weight=ask_weight, ask_ankle=ask_ankle,
                           heat_accl_pct=heat_accl_pct, heat_accl_trend=heat_accl_trend,
                           long_run_cap_km=_long_run_cap_km,
                           wellness_finalized=wellness_finalized,
                           prescription_note=prescription_note,
                           post_race_recovery=_post_race_recovery(athlete_cfg),
                           watchdog_flags=_recent_flags(adir, date.today(), skip=_flag_skip(
                               athlete_cfg, wellness_rows)),
                           off_normal=_off_normal_line(baseline_rows, date.today()))
    prompt += coaching_prefs.prompt_note(slug)

    with open(log_file, "a") as lf:
        result = claude_call.run_claude(
            prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
            allowed_tools=TOOLS, stderr=lf, cwd=PROJECT_DIR, timeout=180, label=slug,
            # Bound to this athlete for the same reason as the caller flag above: this
            # run holds Bash, and the loop serves every athlete in turn, so a per-spawn
            # env (never os.environ) keeps one athlete's scope out of the next one's run.
            env={**os.environ, "CC_ATHLETE_SCOPE": slug},
        )

    raw = (result.stdout or "").strip()
    if not early:
        shadow.launch("morning", slug, prompt, raw)     # Haiku twin, never sent (lib/shadow.py)
    import re as _re
    m = _re.search(r"<telegram>(.*?)</telegram>", raw, _re.DOTALL)
    output = m.group(1).strip() if m else ""

    # Atomic sentinel claim: write BEFORE notifying so a second cron instance
    # that slips past the 20-min process lock (slow multi-athlete run) can never
    # also deliver. If Claude failed, skip — allow retry on next poll.
    if result.returncode == 0 and not early:
        _sl_lock_path = LOG_DIR / f"morning-sent-{slug}-{today_str}.flag.lock"
        with open(_sl_lock_path, "w") as _sl_fd:
            fcntl.flock(_sl_fd, fcntl.LOCK_EX)
            if sentinel.exists():
                return
            sentinel.touch()

    # WEEKLY HOURS ASK (Sunday only) — appended DETERMINISTICALLY to the card rather
    # than asked for in the prompt, because the Sunday build at 18:00 derives the
    # week's Load ceiling from the answer and a dropped question means the build
    # silently falls back to the standing config figure. weekday() == 6 is Sunday;
    # the week asked about is tomorrow's Monday. weekly_availability owns the copy,
    # the already-answered gate and the illness gate. The existing per-athlete daily
    # sentinel already guarantees once-per-day, so this needs no idempotence of its
    # own. Zero extra pushes: the morning card was going out anyway.
    _hours_ask_appended = False
    if output and not early and date.today().weekday() == 6:
        try:
            _ask = weekly_availability.sunday_hours_ask(
                slug, date.today() + timedelta(days=1), coaching_level=coaching_level)
            if _ask:
                output = f"{output}\n\n{_ask}"
                _hours_ask_appended = True
                # Record that the ask went out, so telegram/bot.py can attribute a later
                # BARE number ("14") to it. The bot's contextual tier must rest on a
                # recorded send rather than inferring one from the calendar: this branch
                # is skipped entirely when the athlete has already declared or the
                # illness flag is up, and in those states an unexplained number is
                # answering one of the card's OTHER questions (ankle score 0-10, weight).
                # Best-effort by contract — note_ask_sent never raises, because failing
                # to record the send must not stop the card sending.
                weekly_availability.note_ask_sent(
                    slug, date.today() + timedelta(days=1))
        except Exception as _e:
            # The card must still send if the ask cannot be built — but it must NOT do so
            # silently. This is the one question the Sunday 18:00 ceiling derives from, so
            # a swallowed failure here means the build quietly falls back to the standing
            # config figure and nobody finds out until the plan is already pushed. Alert,
            # then carry on. (ops_log is log-only since 27 Jul; the evening digest picks
            # it up, which is well inside the nine-hour answer window.)
            ops_log.alert("morning-checkin",
                          f"Sunday weekly-hours ask could NOT be built "
                          f"({type(_e).__name__}: {_e}) — the 18:00 build will fall back "
                          f"to profile.max_hours_per_week for this athlete",
                          athlete=slug)

    # BACKED-OFF ASK -> ONE PASSIVE LINE, appended in Python for the same reason the
    # Sunday ask above is: the bookkeeping says it was said, so it has to actually be
    # said. It is a statement, not a question — nothing is expected back, and an answer
    # arriving anyway resets the counter through `answer_date` like any other.
    # At most one per card, and never on a Sunday that already carries the hours ask:
    # two appended lines is the pile-up this whole change exists to stop.
    _passive_sent_for = None
    if output and not early and passive_lines and not _hours_ask_appended:
        _passive_sent_for, _passive_text = passive_lines[0]
        output = f"{output}\n\n_{_passive_text}_"

    # CONSISTENCY LINE (lib/consistency.py, Jamie 14 Oct 2026): a planned session missed
    # in the last few days gets ONE line, at most once a week, picked by its likely cause
    # (an overshoot before it, wellness off normal, or neither -> one open question).
    # Appended in Python like the lines above, so the model can neither nag nor guess.
    # Never alongside the other appended lines, never during illness or post-race.
    _cons_case = None
    if output and not early and not _hours_ask_appended and not _passive_sent_for:
        try:
            _cons_case = _consistency_line(slug, adir, athlete_cfg, baseline_rows, output)
            # Not when the card has already raised the misses itself (15 Oct: both said it),
            # and above the countdown, which stays the last line.
            if _cons_case and re.search(r"\bmiss(ed)?\b", output, re.I):
                _cons_case = None
            if _cons_case:
                _body, _sep, _last = output.rpartition("\n\n")
                _line = consistency.LINES[_cons_case]
                # Above the question, which stays the last line.
                output = (f"{_body}\n\n{_line}\n\n{_last}"
                          if _sep and (_last.startswith("_") or _last.rstrip().endswith("?"))
                          else f"{output}\n\n{_line}")
        except Exception as exc:
            print(f"[{slug}] consistency check failed: {exc}", file=sys.stderr)
            _cons_case = None

    # TODAY NOTE, not a chat message (Jamie, 15 Oct 2026). The midnight version is silent;
    # the morning one replaces it and sends the day's one notification. The chat gets the
    # note only when the athlete taps it (api/server.py /api/today/open).
    if output and early:
        today_note.write(slug, output, "plan")
        ops_log.record_run("morning-checkin", athlete=slug, ok=True, detail="today note (plan)")
        return
    sent = False
    if output:
        try:
            today_note.write(slug, output, "wellness" if wellness_line else "final")
            today_note.ping(slug, chat_id, output)
            sent = True
        except Exception as exc:
            ops_log.alert("morning-checkin", f"today note not written: {exc}", athlete=slug)
        if sent and _cons_case:
            consistency.mark(adir, date.today(), _cons_case)
        if sent:
            # RECORD FROM THE TEXT, NOT THE INTENT. The card is generative: Python
            # authorising a question is not the same as the question shipping, and
            # recording an ask that was never sent would count a miss against silence
            # we caused ourselves and trip the backoff on an athlete who answers.
            for _q in ask_gate.STANDING_QUESTIONS:
                if ask_gate.asked_in_text(_q, output):
                    try:
                        ask_gate.note_asked_in(adir, _q, date.today())
                    except Exception as exc:
                        print(f"[{slug}] could not record {_q} ask: {exc}", file=sys.stderr)
            if _passive_sent_for:
                try:
                    ask_gate.note_passive_in(adir, _passive_sent_for, date.today())
                except Exception as exc:
                    print(f"[{slug}] could not record passive line: {exc}", file=sys.stderr)
            ops_log.record_run("morning-checkin", athlete=slug, ok=True, detail="card sent")
    elif result.returncode == 0:
        ops_log.alert("morning-checkin", "claude produced no card output", athlete=slug)
    else:
        ops_log.alert("morning-checkin",
                      f"claude CLI exit {result.returncode} — no card generated", athlete=slug)


def main():
    early = "--early" in sys.argv
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] morning-checkin starting", file=sys.stderr)

    # Lock: morning-checkin had no lock, so a slow 06:00 run still going when 06:30
    # fired could pass the per-athlete sentinel check and BOTH send — the 17 Jun
    # duplicate-card bug. Skip if another run is active (stale after 20 min).
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 1200:
        print(f"[{ts}] another morning-checkin run is active — skipping", file=sys.stderr)
        return
    LOCK_FILE.touch()
    try:
        try:
            athletes = json.loads(ATHLETES_CONFIG.read_text())
        except Exception as e:
            print(f"[{ts}] Failed to load athletes config: {e}", file=sys.stderr)
            sys.exit(1)

        for slug, cfg in athletes.items():
            if not cfg.get("active", True):
                continue
            # Tracking-only: this athlete keeps the activity watcher and the bot, but is
            # prescribed nothing and judged on nothing (lib/planning_pause.py).
            if planning_pause.is_paused(slug, cfg):
                print(planning_pause.skip_line(slug, "morning-checkin", cfg), file=sys.stderr)
                continue
            try:
                run_athlete(slug, cfg, early=early)
            except Exception as exc:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] morning-checkin error: {exc}", file=sys.stderr)
                ops_log.alert("morning-checkin", f"exception: {exc}", athlete=slug)
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
