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

# The Today note's size, enforced in code by _cap_note (Jamie, 15 Oct 2026: "ensure it
# really is concise"; restated 16 Oct 2026 after the shadow review found "the opening plus
# at most THREE of the labelled lines and the question" read two ways).
NOTE_MAX_WORDS = 45
NOTE_MAX_LABELLED = 2

# The card's one question, word for word. ask_gate.asked_in_text records an ask from these.
Q_CYCLE = "Has your period started yet? Just tell me the day when it does."
Q_MORNING_PAIN = "Ankle score this morning? (0-10)"
Q_RUN_PAIN = "Injury pain score before heading out? (0-10)"
Q_WEIGHT = "Weight this morning?"


def _the_question(*, cycle_overdue=False, injuries=(), ask_ankle=False, ask_morning_pain=False,
                  run_today=False, ankle_pain_next_morning=0, ask_weight=False) -> str:
    """The ONE question on today's card, or "" (Jamie, 16 Oct 2026: shadow review). Was an
    If/Else chain the model resolved ("run planned AND ankle score above 0, else weight").
    Priority as before (12 Oct 2026): the overdue cycle anchor, then yesterday's run's
    morning ankle score, then the run-day pain score (a run still to do today and a morning
    ankle score above 0), then weight. `ask_ankle` / `ask_weight` arrive already gated by
    lib/ask_gate (eligibility and the unanswered-twice backoff)."""
    if cycle_overdue:
        return Q_CYCLE
    if injuries and ask_ankle and ask_morning_pain:
        return Q_MORNING_PAIN
    try:
        _pain = float(ankle_pain_next_morning or 0)
    except (TypeError, ValueError):
        _pain = 0
    if injuries and ask_ankle and run_today and _pain > 0:
        return Q_RUN_PAIN
    if ask_weight:
        return Q_WEIGHT
    return ""


def _form_block(form, *, post_race_recovery=False, off_normal="", finalized=True):
    """(prompt block, card-line slot) for Form, decided here (Jamie, 16 Oct 2026: shadow
    review). Form never reached the prompt (lib/recovery_score._parse_wellness read keys
    Intervals.icu does not send), yet the opening asked "how fresh or loaded they are", so
    the model judged freshness from nothing. Now it gets Peak's word (recovery_score.
    form_word, the same rule as the Today tab's Form figure) and what to do with it."""
    head = "\n## Form (pre-computed — the ONLY source for how fresh or loaded they are)\n"
    if post_race_recovery:
        # Form is high BECAUSE the athlete is recovering, so "fresh legs, good day for
        # quality" is exactly wrong (sent to Jamie on 25 Sep, six days after IM Italy).
        return (head + "Say nothing about freshness: post-race recovery. High Form is expected "
                "and is never a cue for quality work.\n", "")
    if not form:
        return (head + "No Form figure today: say NOTHING about how fresh, tired or loaded "
                "they are.\n", "")
    soft = ("" if finalized else
            " Today's figure is still settling on Intervals.icu: say it softly, never as final.")
    if form == "very tired":
        return (head + f"Form: very tired (a heavy run of training).{soft}\n",
                "[**Load:** a heavy run of training, so keep intensity in check]\n")
    if form == "fresh":
        if off_normal:
            # "Fresh legs, good day for quality" beside "resting HR off your normal" told
            # Kathryn two opposite things on one card (shadow week, 14 Oct 2026).
            return (head + "Form: fresh from training, but wellness is off normal today: never "
                    f"call it a good day for quality. Say nothing about freshness.{soft}\n", "")
        return (head + "Form: fresh. Say it in the opening, not its own line: fresh legs, a "
                f"good day for quality work if quality is planned.{soft}\n", "")
    if form == "balanced":
        return (head + "Form: balanced (fresh enough for the plan). At most a few words in the "
                f"opening; no line of its own.{soft}\n", "")
    return (head + "Form: building, tired (the normal tiredness of a training block). At most "
            "a few words in the opening; no line of its own, and never on its own a reason to "
            f"go easy.{soft}\n", "")


def _hm(minutes) -> str:
    m = int(round(minutes or 0))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _fam(t) -> str:
    t = str(t or "")
    return next((f for f in ("Ride", "Run", "Swim") if f in t), t)


def _week_so_far(events, acts, today: date) -> str:
    """How the week is going, as one labelled fact (Jamie, 16 Oct 2026: shadow review: the
    opening asked "how the week is going" with no data behind it). Monday to yesterday; on
    a Monday, last week. Planned = WORKOUT events; one counts as done when Intervals.icu
    paired it, or an activity of the same sport was done that day (as watchdog T5)."""
    mon = today - timedelta(days=today.weekday())
    if today.weekday() == 0:
        lo, hi, label = mon - timedelta(days=7), mon - timedelta(days=1), "Last week"
    else:
        lo, hi, label = mon, today - timedelta(days=1), "Week so far"
    span = (f"{lo:%a %d %b}" if lo == hi else f"{lo:%a %d %b} to {hi:%a %d %b}")

    def _day(x):
        try:
            return date.fromisoformat(str(x.get("start_date_local") or "")[:10])
        except ValueError:
            return None
    done = [a for a in acts or [] if (d := _day(a)) and lo <= d <= hi]
    plan = [e for e in events or [] if (d := _day(e)) and lo <= d <= hi
            and (e.get("category") or "WORKOUT").upper() == "WORKOUT"]
    spare = [a for a in done if str(a.get("id")) not in
             {str(e.get("paired_activity_id")) for e in plan if e.get("paired_activity_id")}]
    hit = 0
    for e in plan:
        if e.get("paired_activity_id"):
            hit += 1
            continue
        m = next((a for a in spare if _day(a) == _day(e) and _fam(a.get("type")) == _fam(e.get("type"))), None)
        if m:
            spare.remove(m)
            hit += 1
    done_min = sum((a.get("moving_time") or 0) for a in done) / 60
    plan_min = sum((e.get("moving_time") or 0) for e in plan) / 60
    if not done and not plan:
        return f"{label} ({span}): no sessions done and none planned."
    out = f"{label} ({span}): "
    out += (f"{hit} of {len(plan)} planned sessions done" if plan
            else f"{len(done)} session{'s' if len(done) != 1 else ''} done, none planned")
    if done_min:
        out += f"; {_hm(done_min)} of training"
        if plan_min:
            out += f" against {_hm(plan_min)} planned"
    return out + "."


def _done_today_line(acts) -> str:
    """"Already done today" for the planned block: sport and duration, the name only when
    there is one (Jamie, 16 Oct 2026: shadow review: str(None) printed "None")."""
    out = []
    for a in acts or []:
        sport = _CHART_SPORT_MAP.get(str(a.get("type") or ""), str(a.get("type") or "")).strip()
        mins = round((a.get("moving_time") or 0) / 60)
        name = str(a.get("name") or "").strip()
        bits = ", ".join(b for b in (sport, _hm(mins) if mins else "") if b)
        if name and name.lower() != "none":
            bits = f"{bits} ({name})" if bits else name
        if bits:
            out.append(bits)
    return "; ".join(out)


def _cap_note(text: str, question: str = "") -> tuple[str, list]:
    """Hold the model's note to the opening + NOTE_MAX_LABELLED labelled lines and
    NOTE_MAX_WORDS minus the question's words (Jamie, 16 Oct 2026: shadow review).

    Whole lines are dropped, last first; nothing is ever cut mid-line or rewritten. That is
    the least risky enforcement: a dropped line cannot put a wrong number or half a sentence
    in front of the athlete, where a regenerate costs a second model run, can drift from the
    first and can overrun again. The prompt asks for the labelled lines most important first,
    so the last is the one to lose. Any question line the model wrote anyway goes too (the
    question is the code's, _the_question). The opening is never dropped."""
    lines = (text or "").split("\n")
    content = [i for i, l in enumerate(lines) if l.strip()]
    if not content:
        return text, []
    first_lab = next((i for i in content if i != content[0]
                      and _is_labelled(lines[i])), None)
    opening = [i for i in content if first_lab is None or i < first_lab]
    labelled = [i for i in content if i not in opening]
    drop = [i for i in labelled if lines[i].rstrip().endswith("?")]
    labelled = [i for i in labelled if i not in drop]
    drop += labelled[NOTE_MAX_LABELLED:]
    labelled = labelled[:NOTE_MAX_LABELLED]
    budget = NOTE_MAX_WORDS - len((question or "").split())
    words = lambda idx: sum(len(lines[i].split()) for i in idx)
    while labelled and words(opening + labelled) > budget:
        drop.append(labelled.pop())
    if not drop:
        return text, []
    kept = "\n".join(l for i, l in enumerate(lines) if i not in drop)
    kept = re.sub(r"\n{3,}", "\n\n", kept).strip()
    return kept, [lines[i].strip() for i in sorted(drop)]


def _is_labelled(line: str) -> bool:
    """A labelled line: starts with a **bold label** (optionally as a list item)."""
    return bool(re.match(r"^\s*(?:[-*]\s+)?\*\*", line))


def _build_prompt(slug, first_name, race_name, race_date, days_to_race, injuries, recovery=None, wellness_line=None, heat_protocol=True, coaching_level="mid", planned_block="", cycle=None, fuel_target_g_hr=60, nutrition_race=90, nutrition_label="race target", heat_accl_pct=None, heat_accl_trend="", long_run_cap_km=None, wellness_finalized=True, race_block="", prescription_note="", post_race_recovery=False, watchdog_flags=None, off_normal="", form=None, week_so_far="", question=""):
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
    if cycle:
        if cycle.get("phase"):
            cycle_block = (
                f"\n## Menstrual cycle (pre-computed — authoritative)\n"
                f"Cycle day {cycle['day']} — {cycle['phase']} phase: {cycle['cue']}\n"
            )
        if cycle.get("phase") in ("menstrual", "luteal"):
            cycle_card_line = (
                "[**Cycle:** one plain-English line from the cycle block above — what today may "
                "feel like and how to approach targets. No cycle-day numbers, no jargon.]\n"
            )

    # THE QUESTION IS NOT THE MODEL'S (Jamie, 16 Oct 2026: shadow review). It used to get an
    # If/Else chain ("run planned AND ankle score above 0, else weight") and resolve it
    # itself: Sonnet and Haiku picked different questions off the same data. _the_question
    # picks the one question in Python and run_athlete appends it after generation, the way
    # the Sunday hours ask is added; the prompt only knows its length, for the word budget.
    _q_words = len(question.split()) if question else 0
    _budget = NOTE_MAX_WORDS - _q_words

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
        slp_v  = sigs.get("sleep", {}).get("value")
        pain_v = sigs.get("pain",  {}).get("value")
        parts  = []
        if hrv_r  is not None: parts.append(f"HRV ratio {hrv_r:.2f}")
        # Form is not listed here: it reaches the card only as the word in the Form block.
        if slp_v  is not None: parts.append(f"sleep {slp_v:.1f}h")
        if pain_v is not None and pain_v > 0: parts.append(f"pain {pain_v}/10")
        recovery_block = (
            f"\n## Recovery score (pre-computed)\n"
            f"Score: {score}/100 — {label}. {rec}\n"
            f"Signals: {', '.join(parts) if parts else 'no data'}.\n"
            f"Use this to modulate session prescription: GREEN = train as planned; "
            f"AMBER = note and monitor; ORANGE = reduce intensity or volume; RED = flag for easy day.\n"
        )

    wellness_block = (
        f"\n## Today's wellness (pre-fetched — for your judgement only; do not print these "
        f"numbers: the only wellness numbers on the card are the 'Off your normal' line "
        f"below, when there is one)\n{wellness_line}\n"
        + (f"\n## Off your normal (pre-computed — the fact for the Recovery line; say it in words)\n{off_normal}\n"
           if off_normal else "")
        if wellness_line else
        "\n## Today's wellness\nNot yet synced — say nothing about sleep, HRV or resting HR.\n"
    )

    planned_section = (
        f"\n## Today's planned sessions (pre-computed — authoritative)\n{planned_block}\n"
        if planned_block else ""
    )

    heat_block = ""
    form_block, form_card_line = _form_block(form, post_race_recovery=post_race_recovery,
                                             off_normal=off_normal, finalized=wellness_finalized)
    week_block = (f"\n## Week so far (pre-computed — the fact for \"how the week is going\"; say it in words)\n"
                  f"{week_so_far}\n" if week_so_far else
                  "\n## Week so far\nNot available — say nothing about how the week has gone.\n")
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
                          "**Heat:** a heat bath is due, [N] so far this week (aim for 2 or 3)]")

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

    flags_block = ("\n## Watchdog flags to show (pre-selected — authoritative; show each, no others. "
                   "Each is a dated fact: keep its dates, never call a past day \"today\")\n"
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
{recovery_block}{form_block}{week_block}{wellness_block}{flags_block}{cycle_block}{planned_section}{race_block}{long_run_cap_block}{heat_block}{prescription_block}
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

Step 3 — No question from you. Ask NOTHING: no line ending in "?". The card's one question,
if one is due today, is chosen in code and added below your note after you write it.

Step 4 — Output the Today note in Markdown (no preamble, no sign-off). It sits at the top
of the athlete's Today tab in Peak, directly above today's session, the date and the race
countdown, so it never repeats them as headings (Jamie, 15 Oct 2026: "just describe how
training's going, for example you've got a big session today but you're fresh from the
week, this is what to think about").

Use the recovery score and signals ONLY to decide what to flag — do NOT show the score, label, HRV ratio, or any internal metric to the athlete. Write like a coach talking, not a dashboard.

[Opening: TWO sentences at most, in a coach's voice. How training is going right now and
 what that means for today's session: the one thing to think about. e.g. "Big ride today,
 and you're fresh after an easy week: hold the first hour back and finish strong." Every
 part comes from a block above, never from a guess: the week so far ONLY from the "Week so
 far" block; how fresh or loaded ONLY from the Form block (no Form word = say nothing about
 freshness); long/short and hard/easy from the planned-sessions block. Say it in words: no
 minutes, no Load. Rest day: say what the rest is doing for them, in one sentence.]

{form_card_line}[If the "Off your normal" block above is present: **Recovery:** that fact in plain words and what it means today, e.g. "**Recovery:** HRV has been low this week, so keep today easy."]
[If recovery ORANGE or RED: **Recovery:** [one plain-English sentence on what to do differently — no scores]]
[For EACH flag in the "Watchdog flags to show" block: **[one-word label]:** [that flag in plain English, one line]. None listed: no flag line.]
[If the pre-verified 05:00 prescription check block above is present and says today's session was modified or swapped: **Changed:** [what changed and why — one plain line, e.g. "Swapped to an easy spin because HRV is low". If it confirmed the session as planned, or the block is absent, say nothing]]
{cycle_card_line}
[If today's session is Ride or Brick >90 min: **Fuel:** aim for {fuel_target_g_hr}g/hr (progress toward {nutrition_race}g/hr {nutrition_label}) · eat at 15 min then every 25 min]
[If any travel block, race, or constraint from current-state.md "Travel & training blocks" STARTS or ENDS within 5 days, or a training plan or goal block starts within 5 days: **Coming up:** [what] in [N] days — [one-line impact], e.g. "Back from your trip in 2 days. Monday's hamstring strength starts the week."]
[If open action is due within 3 days: **To do:** [action] by [day]]
{heat_card_line}

ORDER is fixed: the opening, then the labelled lines, most important first (the question,
if any, is added after them in code). No greeting, no date line, no "Today:" line, no
countdown: Peak shows those right beside it.

Rules:
- TONE: a coach texting an amateur athlete who is dyslexic (Jamie, 15 Oct 2026: "imagine a
  human speaking ... long, short, big, small, hard, easy. Not 63m and 47tss", then "it's not
  a ban on numbers, but the tone of voice needs to be a coach to an amateur who's dyslexic").
  Short sentences, everyday words, no jargon or acronyms the athlete wouldn't say themselves
  (no TSS, Load, Form, ATL). Describe sessions and readings in words by default: a long easy
  ride, a short sharp run, HRV a bit low. Use a number only where a coach naturally would and
  it helps the athlete act, e.g. "aim for 60g of carbs an hour", "race in 5 days"; never
  minutes and Load read off the plan.
- NO EMOJI (Jamie, 15 Oct 2026: "too many emojis looks like a kid or an AI writing"). Each
  line after the opening starts with a short **bold label** instead.
- EVERY INSTRUCTION CARRIES ITS REAL REASON (Jamie, 15 Oct 2026, on "you missed sessions,
  keep it easy": "if that's because of a recovery metric it needs to say so"). Never say
  keep it easy / hold back / skip quality without the why in the same sentence, and the why
  must be the actual signal in the data above (low HRV, high resting HR, short sleep, heavy
  load, the plan). Missed sessions are NEVER a reason to go easy; they only mean don't
  make them up.
- Say each thing ONCE: if the opening already mentions it (low HRV, a missed session), no
  flag line repeats it.
- CONCISE (Jamie, 15 Oct 2026: "ensure it really is concise so as not to take over the home
  page"; the cap restated 16 Oct 2026 after the shadow review found it ambiguous): the note
  is opening + 2 labelled lines + question = 4 lines, {NOTE_MAX_WORDS} words max. The
  question is added in code{f" and is {_q_words} words today" if _q_words else " (none today)"}, so YOUR part is
  the opening plus AT MOST {NOTE_MAX_LABELLED} labelled lines, AT MOST {_budget} words. When more
  qualify, keep the ones that change what the athlete does today and drop the rest; never
  merge two into one long line. Anything past this is cut in code, last line first.
- SHORT (Jamie, 15 Oct 2026: "it's like an essay"): the card is read in five seconds.
  Every labelled line is ONE sentence of at most 18 words: what to do, and why in a
  few words. No reasoning, no dates of individual readings, no "I'd offer". At most TWO
  warning lines: keep the two that change what the athlete does today; drop the rest.
  e.g. "**Recovery:** HRV is down on last week, so keep today easy."
  e.g. "**Missed:** two sessions this week; don't make them up."
- Sleep/HRV/RHR: use ONLY the pre-fetched wellness line above — never infer or estimate values yourself.
- Omit any section that has nothing to say — do not pad with dashes or "N/A".
- Never ask for subjective mood, fatigue, or motivation scores.
- Never ask a question (Step 3), and never ask for something the athlete already told
  you — if a value is in the data above, state it or leave it.

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
# A legacy flag line whose own text says it asks nothing ("no action", "no training action
# implied", "no action unless"). Only for lines written before watchdog flags carried
# `actionable` (16 Oct 2026); the exact phrase "no action" let the T12 race-week flag through.
_NO_ACTION_RE = None


def _legacy_fact(text: str) -> str:
    """The signal from an old current-state.md flag line, without the model's adjustment
    sentence after it (which carried the stale wellness reasoning: "Resting HR 55 today").
    The watchdog line is "<signal>. <adjustment>", so the first sentence is the fact."""
    import re
    fact = re.split(r"\.\s+(?=[A-Z])", text.strip(), maxsplit=1)[0].rstrip(". ")
    # T10's old label called a % over the 4-week average "week-on-week".
    if "4-week average" in fact:
        fact = fact.replace(" week-on-week (", " over the 4-week average (")
    return fact


def _recent_flags(adir: Path, today: date, days: int = 3, cap: int = 2,
                  skip: frozenset = frozenset()) -> list[str]:
    """Watchdog flags the card shows, chosen here (11 Oct 2026). "If watchdog flag active"
    was left to the model: on the same card Haiku showed Kathryn's real T10 run-volume flag
    and Sonnet left it out. Flags dated within `days`, newest first then Tier 1, at most `cap`.

    Since 16 Oct 2026 (shadow review) the fact comes from the watchdog's own record
    (athletes/<slug>/watchdog-state.json, watchdog_checks.card_flags): the Python-made
    signal with absolute dates and an `actionable` field, never the model's log sentence,
    which carried day-old wellness reasoning and relative words. Lines in current-state.md
    with no structured record (logged before then) still show, cut to their first sentence.
    Never shown: anything not actionable (Jamie's paused-heat T7, a historical T12), T9 (the
    open-actions list: the card has its own open-action line), T3/T4 (wellness: the card
    takes it ONLY from its off-normal block) and `skip`."""
    import re
    import watchdog_checks as wc
    global _FLAG_RE, _NO_ACTION_RE
    _FLAG_RE = _FLAG_RE or re.compile(
        r"^- \*\*(\d{4}-\d{2}-\d{2}) — (T\d+) \(Tier (\d)\):\*\*\s*(.+)$")
    _NO_ACTION_RE = _NO_ACTION_RE or re.compile(r"\bno\b(?:\s+\w+){0,3}\s+action\b", re.I)
    hidden = {"T9"} | set(wc.WELLNESS_TRIGGERS) | set(skip)
    out, seen = [], set()
    try:
        state = json.loads((adir / wc.STATE_NAME).read_text())
    except (OSError, ValueError):
        state = {}
    for f in wc.card_flags(state, today, days):
        seen.add((f["date"], f["trigger"]))
        if f["trigger"] in hidden or not f.get("actionable", True):
            continue
        out.append((int(f["tier"] or 2), f["date"], f"{f['trigger']}: {f['fact']}"))
    try:
        text = (adir / "current-state.md").read_text()
    except OSError:
        text = ""
    sec = text.split("## Watchdog flags", 1)
    body = sec[1].split("\n## ", 1)[0] if len(sec) == 2 else ""
    # A legacy T12 line shows only with the same test the watchdog now applies
    # (wc.t12_ramp): its structured record in current-state.json is for the week just
    # finished, off Intervals.icu. Fred's 16 Oct line was for a week two weeks gone.
    _sun = (today - timedelta(days=today.weekday() + 1)).isoformat()
    try:
        _t12_now = any(isinstance(f, dict) and f.get("trigger") == "T12"
                       and f.get("week_end") == _sun and f.get("source") == "icu_fitness"
                       for f in json.loads((adir / "current-state.json").read_text())
                       .get("watchdog_flags") or [])
    except (OSError, ValueError, AttributeError):
        _t12_now = False
    for line in body.splitlines():
        m = _FLAG_RE.match(line.strip())
        if not m or (m.group(1), m.group(2)) in seen:
            continue
        try:
            age = (today - date.fromisoformat(m.group(1))).days
        except ValueError:
            continue
        if (not 0 <= age <= days or m.group(2) in hidden
                or (m.group(2) == "T12" and not _t12_now)
                or _NO_ACTION_RE.search(m.group(4)) or "historical" in m.group(4).lower()):
            continue
        out.append((int(m.group(3)), m.group(1), f"{m.group(2)}: {_legacy_fact(m.group(4))}"))
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


def _with_trends(off_normal: str, wellness_rows, today: date) -> str:
    """The off-normal line plus this morning's HRV-trend and short-sleep checks
    (watchdog_checks T3 / T4), worked out fresh from today's rows. Jamie, 17 Oct 2026: the
    16 Oct shadow fix kept the watchdog's T3/T4 sentences off the card (they carried
    day-old wording) and left wellness to this line alone, whose SD rule did not trip on
    HRV 37 / resting HR 54, so the card said "fresh" with no recovery line. The facts come
    back here, computed today, and an off line also stops "fresh, good day for quality"."""
    import watchdog_checks as wc
    extra = []
    for check in (wc.t3_hrv_down, wc.t4_short_sleep):
        try:
            f = check(wellness_rows or [], today)
        except Exception:
            f = None
        if f and f.get("signal"):
            extra.append(f["signal"])
    if not extra:
        return off_normal
    trend = "; ".join(extra)
    if off_normal:
        return f"{off_normal}; {trend}"
    return f"Off your normal: {trend}"


def _off_normal_line(wellness_rows, today: date, exclude=None) -> str:
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
        a = WN.assess(wellness_rows, key, today, exclude)
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
    form = None               # Peak's Form word (recovery_score.form_word), or None
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
        form = rs.form_word(tsb)
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
    week_so_far = ""
    _events = []  # kept for long-run cap computation below
    try:
        from icu_api import IcuClient as _Icu
        _a = json.loads(ATHLETES_CONFIG.read_text())[slug]
        _cl = _Icu(_a["icu_athlete_id"], _a["icu_api_key"])
        _events = _cl.get_events(today_str, today_str)
        # A session already ridden or run this morning is DONE, not today's plan (Fred,
        # 15 Oct 2026: the 08:00 card announced a cut threshold session he'd finished at 07:00).
        # 8 days of history: today's done sessions and the week-so-far fact below.
        _hist = _cl.get_training_history(days=8) or []
        _acts_today = [x for x in _hist if str(x.get("start_date_local") or "")[:10] == today_str]
        _done_fams = {_fam(x.get("type")) for x in _acts_today}
        _events = [e for e in _events
                   if not ((e.get("category") or "WORKOUT").upper() == "WORKOUT"
                           and (e.get("paired_activity_id") or _fam(e.get("type")) in _done_fams))]
        planned_block = planned_sessions_block(_events)
        _done_line = _done_today_line(_acts_today)
        if _done_line:
            planned_block = ((planned_block + "\n") if planned_block else "") + (
                "Already done today (say so in one line if relevant; never present it as still to do "
                "or describe changes to it): " + _done_line)
        try:
            _wk_lo = date.today() - timedelta(days=date.today().weekday()
                                              + (7 if date.today().weekday() == 0 else 0))
            week_so_far = _week_so_far(
                _cl.get_events(_wk_lo.isoformat(), (date.today() - timedelta(days=1)).isoformat()),
                _hist, date.today())
        except Exception as exc:
            print(f"[{slug}] week-so-far fact failed: {exc}", file=sys.stderr)
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

    # The one question, decided here and appended after generation (_the_question).
    # `_events` is today's plan with anything already done taken out, so a run finished
    # before the card is not "before heading out".
    question = _the_question(
        cycle_overdue=bool(cycle and cycle.get("overdue")), injuries=injuries,
        ask_ankle=ask_ankle, ask_morning_pain=needs_morning_pain_ask,
        run_today=any((e.get("category") or "WORKOUT").upper() == "WORKOUT"
                      and "Run" in str(e.get("type") or "") for e in _events),
        ankle_pain_next_morning=(_cs_data.get("ankle") or {}).get("pain_next_morning"),
        ask_weight=ask_weight)

    prompt = _build_prompt(slug, first_name, race_name, race_date_str, days_to_race, injuries, recovery,
                           wellness_line=wellness_line, heat_protocol=heat_protocol,
                           coaching_level=coaching_level, planned_block=planned_block,
                           race_block=races_lib.prompt_block(_races, date.today()),
                           cycle=cycle,
                           fuel_target_g_hr=_fuel_target_g_hr,
                           nutrition_race=fuel_basis.ceiling(athlete_cfg),
                           nutrition_label=fuel_basis.label(athlete_cfg),
                           form=form, week_so_far=week_so_far, question=question,
                           heat_accl_pct=heat_accl_pct, heat_accl_trend=heat_accl_trend,
                           long_run_cap_km=_long_run_cap_km,
                           wellness_finalized=wellness_finalized,
                           prescription_note=prescription_note,
                           post_race_recovery=_post_race_recovery(athlete_cfg),
                           watchdog_flags=_recent_flags(adir, date.today(), skip=_flag_skip(
                               athlete_cfg, wellness_rows)),
                           off_normal=_with_trends(_off_normal_line(
                               baseline_rows, date.today(), profile.get("baseline_exclude_ranges")),
                               baseline_rows, date.today()))
    prompt += coaching_prefs.prompt_note(slug)

    with open(log_file, "a") as lf:
        result = claude_call.run_claude(
            prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
            allowed_tools=TOOLS, stderr=lf, cwd=PROJECT_DIR, timeout=180, label=slug,
            # Bound to this athlete for the same reason as the caller flag above: this
            # run holds Bash, and the loop serves every athlete in turn, so a per-spawn
            # env (never os.environ) keeps one athlete's scope out of the next one's run.
            env={**os.environ, "CC_ATHLETE_SCOPE": slug},
            athlete=slug,
        )

    raw = (result.stdout or "").strip()
    if not early:
        shadow.launch("morning", slug, prompt, raw)     # Haiku twin, never sent (lib/shadow.py)
    import re as _re
    m = _re.search(r"<telegram>(.*?)</telegram>", raw, _re.DOTALL)
    output = m.group(1).strip() if m else ""
    _note_words = len(output.split())
    output, _trimmed = _cap_note(output, question)
    if _trimmed and not early:
        # A prompt finding as well as a fix: log what was cut for the dev inbox.
        ops_log.alert("morning-checkin",
                      f"Today note over the cap ({_note_words} words; cut {len(_trimmed)} line(s): "
                      + " | ".join(t[:60] for t in _trimmed) + ")", athlete=slug)

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
            # The question is appended after this, so count it now: the "ask" case is a
            # question too, and a card carries one.
            _cons_case = _consistency_line(slug, adir, athlete_cfg, baseline_rows,
                                           f"{output}\n\n{question}" if question else output)
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

    # THE QUESTION, LAST (_the_question; Jamie, 16 Oct 2026: shadow review). Appended in
    # Python like the Sunday hours ask, so the model can neither drop it nor pick another.
    # Not on a Sunday that already carries the hours ask: one question per card, and the
    # 18:00 build depends on that one. ask_gate records the ask from the sent text, so a
    # question held back here is not counted as asked.
    if output and question and not _hours_ask_appended:
        output = f"{output}\n\n{question}"

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
