#!/usr/bin/env python3
"""Acknowledgement triggers — the five §8.3 rows that nothing watched for.

Why this exists. Counted on the live tree on 27 Jul 2026, the only spontaneous
positive message the whole system could produce was a Strava segment PB
(`activity-watcher.py:861`). Everything else it says fires on a problem: the
watchdog on a trigger, the ops digest on a failure, the evening check-in on
missing data, the weekly triggers on a breach. `docs/tone-of-voice-guide.md`
§8.1 puts the count on it — over the 30-message window spanning Jamie's Dorney
race, 7 messages asked him for an RPE score and 0 contained "well done", "good
luck" or "nice work". §8.3 lists nine triggers; five were marked "Needs
trigger", and those five are what this module evaluates.

The design rule is the one lib/plan_builder.py:5-7 already learned the hard
way — "the single-LLM generator ignores instructions ... take the mechanical
parts out of the LLM's hands". A model deciding *when* to praise will praise
wrongly, and §8.3's whole value depends on it never doing that. So the
conditions are evaluated here in Python and the caller is handed finished text,
exactly as lib/open_actions.py:337 `weekly_block()` and lib/illness.py:186
`prompt_block_from_dir()` do it.

FAIL CLOSED, EVERY TRIGGER. False praise is worse than silence: congratulating
an athlete on a week they missed, or calling a ride their longest when it was
not, discredits every other thing the coach says. So each evaluator returns
None the moment its evidence is incomplete, ambiguous, or too shallow to judge,
and there is no "probably" branch anywhere in this file. The specific hazards
this was built against, all measured on the live tree on 29 Jul 2026:

  * TRIGGERS 3 AND 4 READ THE ICU HISTORY FEED, NEVER `session-log.json`.
    §12 Step 3 names the history endpoint, and the feed is the source the log's
    stub rows are created FROM, so the log is a derived copy. Measured on
    29 Jul 2026, the log agrees with the feed on which DAYS were trained —
    within each log's own window, zero training days present in the feed are
    absent from the log, for all three athletes — so the log is not missing
    sessions. What it is missing is DEPTH and DISTANCES, and both gates here
    depend on exactly those:
      - Reach. The feed returns ~12 months (Jamie 445, Kathryn 345, Calum 45
        activities from 2025-07-29). The logs start 2026-03-14, 2026-05-09 and
        2026-04-23. Trigger 3's span gate and trigger 4's coverage gate are
        judgements about how far back the evidence goes, so off the log nothing
        before those dates could be assessed at all, and Calum — 18 rows — could
        never clear either gate.
      - Distances. Stub rows carry a null `distance_km` (3 of Jamie's 174, 9 of
        Kathryn's 87, 3 of Calum's 18). Trigger 3 treats one unknown distance in
        a sport's history as an unknown maximum and goes silent, so off the log
        it would be permanently silent for all three.
    An earlier draft of this comment claimed the log's gaps were recording gaps
    and that Calum would have been congratulated on breaks he never took. That
    was wrong — it misread the weekly buckets in `intensity-history.json` as
    daily totals, and the ICU gaps turn out to match his log's gaps exactly
    (9, 10, 8, 11, 5, 23, 7 and 8 days). The feed is still the right source, for
    the two reasons above; it is not the right source for that reason.

  * A SHORT FEED LOOKS EXACTLY LIKE A LONG GAP. An empty or recently-starting
    feed cannot be told apart from a real break unless you check how far back
    the data actually extends, so trigger 4 requires the feed to reach back
    past the gap it is claiming (`_COMEBACK_COVERAGE_DAYS`), and trigger 3
    requires real span and real depth for that sport before it will use the
    word "longest".

  * "THIS YEAR" IS A CLAIM ABOUT THE WINDOW, NOT THE ATHLETE.
    `IcuClient.get_training_history` computes `oldest` as `today - days`, so a
    365-day request is a rolling year and may still return only three months
    (Kathryn's log starts 2026-05-09, Calum's 2026-04-23). §8.3's suggested
    wording "Longest ride you've done this year" would therefore be a false
    statement for two of three athletes. The span phrase is derived from the
    EARLIEST DATE ACTUALLY PRESENT in the returned feed, never from what was
    requested.

REGISTER. `lib/coaching_levels.py` makes Calum `beginner`: no zone numbers, no
TSS, NP or IF, "or any other training metrics", while "Duration, distance, HR,
run pace, and swim pace are fine". So §8.3's own example sentence — "three
weeks, 2,340 Load, Fitness up 11 points" — cannot go to him as written, and
`_BEGINNER_BANNED_RE` asserts that it does not. Note this is deliberately NOT
`races.py:_carries_a_figure`: that guard bans every digit and number word
because it protects race eve, where §8.6's rule is "no numbers unless the
athlete asks". Reusing it here would strip the durations and distances
`coaching_levels.py` explicitly permits at beginner level, which is a different
rule for a different surface.

WARMTH NEVER BUYS SOFTNESS (§8.5, W5). Nothing here suppresses, softens or
reorders a finding. The rendered text is a leading clause; whatever the surface
was going to say still says it, at full strength, immediately after. W3 is
enforced the other way round: triggers 1 and 2 will not fire on a week that
missed its target, because praise on a bad week reads as sarcasm or
inattention.

ONCE PER OCCURRENCE. Keys identify the OCCURRENCE, not the day, so a streak
cannot re-congratulate weekly and a comeback cannot fire for every session in
the week:

    block:<week_end>            the week the block ended
    streak:<first_week_end>     the week the streak STARTED — so extending a
                                3-week streak to 4 keeps the same key
    first:<family>:<value>      sport + the distance claimed
    comeback:<activity_date>    the date of the first session back
    ill:<activity_id>           the session itself

Store: `athletes/<slug>/acknowledgement-state.json`, covered by the
`ClaudeCoach/athletes/` line in `.gitignore` (verified with `git check-ignore`),
holding `fired` (key -> ISO date) and `weeks` (week_end -> compliance %, or
null for a week with no plan). Both are pruned on every write so the record
cannot accumulate silently.

EVALUATION IS PURE; ONLY `mark_fired_in`/`record_week_in` WRITE. The already-
fired set and the compliance history are arguments, so the whole module can be
evaluated against real athlete data without touching a byte of it — which is
what makes a read-only audit against the production tree possible at all, and
mirrors why `open_actions.evaluate_from_dir` and `illness.state_from_dir`
exist (a git worktree has no `athletes/`).
"""

import json
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import illness as illness_lib      # noqa: E402  structured illness flag — consumed, not restated
import plan_tools as _pt           # noqa: E402  required_tss() -> week_type

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/

STATE_FILENAME = "acknowledgement-state.json"

# -- thresholds -------------------------------------------------------------
# §8.3 fixes the first two; the rest are the fail-closed floors this module adds.

STREAK_WEEKS = 3                 # §8.3: "3+ consecutive weeks at >=90% compliance"
STREAK_MIN_PCT = 90
BLOCK_MIN_COMPLIANCE = 80        # W3 / §8.4: no praise on a week that missed its target.
                                 # 80 is the SOLID floor the weekly card already uses.
COMEBACK_GAP_DAYS = 5            # §8.3: ">=5 days with no training"
# To assert a 5-day gap you need to see the days BEFORE it. 12 = 5 + a week of
# margin: if the feed does not reach back this far, a genuine break and a feed
# that simply starts there are indistinguishable, so trigger 4 says nothing.
_COMEBACK_COVERAGE_DAYS = COMEBACK_GAP_DAYS + 7
FIRST_MIN_SPAN_DAYS = 120        # shortest history that supports the word "longest"
FIRST_MIN_SPORT_SESSIONS = 8     # and the shallowest per-sport record that does
FIRST_MIN_MARGIN = 1.02          # beat the old max by 2%, so "longest by 30 m" stays quiet

_MAX_FIRED_KEYS = 120
_MAX_WEEKS = 26

# Build-type weeks. A block "finishes" when one of these is followed by a week
# that steps down; a deload or taper week is not a block ending, it IS the step down.
_BUILD_WEEK_TYPES = ("base", "build", "specific", "peak")
_STEPDOWN_WEEK_TYPES = ("deload", "taper")

# Plain-English block names. §4a bans internal identifiers in athlete text, and
# these are the phase keys plan_tools uses, so they are mapped rather than printed.
_BLOCK_NAMES = {"base": "base block", "build": "build block",
                "specific": "race-specific block", "peak": "peak block"}

# Sports §8.3 names for the distance trigger, mapped to the word a coach says.
# Anything not here (Strength, Golf, Yoga, Workout, Transition) never claims a
# "longest": a longest golf round is not a training milestone.
_SPORT_FAMILY = {
    "Ride": "ride", "VirtualRide": "ride", "GravelRide": "ride", "MountainBikeRide": "ride",
    "Run": "run", "VirtualRun": "run", "TrailRun": "run",
    "Swim": "swim", "OpenWaterSwim": "swim",
}

# Beginner register (lib/coaching_levels.py): duration, distance, HR and pace are
# fine; training metrics are not. Asserted, not trusted — this is what stops a
# later edit quietly sending Calum a Load figure.
_BEGINNER_BANNED_RE = re.compile(
    r"\b(TSS|Load|Fitness|Fatigue|Form|CTL|ATL|TSB|NP|IF|VI|watts?|zone)\b", re.I)

_ORDINALS = {3: "Third", 4: "Fourth", 5: "Fifth", 6: "Sixth", 7: "Seventh",
             8: "Eighth", 9: "Ninth", 10: "Tenth", 11: "Eleventh", 12: "Twelfth"}


def _ordinal(n: int) -> str:
    return _ORDINALS.get(n, f"{n}th")


def _as_date(v):
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def _act_date(a: dict):
    """Activity date from the ICU feed, tolerating either key."""
    return _as_date(a.get("start_date_local") or a.get("start_date") or a.get("date"))


def _span_phrase(days: int) -> str:
    """How far back the evidence actually goes, in words a coach would say.

    Derived from the feed, never from the request window — see the module
    docstring on why "this year" is a claim about the window.
    """
    if days >= 330:
        return "in the last twelve months"
    months = int(round(days / 30.0))
    return f"in the last {months} months"


def _distance_text(family: str, metres: float) -> str:
    if family == "swim":
        return f"{int(round(metres))} m"
    return f"{metres / 1000.0:.1f} km"


# ---------------------------------------------------------------------------
# state — the ONLY writing part of this module
# ---------------------------------------------------------------------------

def _state_path(athlete_dir) -> Path:
    return Path(athlete_dir) / STATE_FILENAME


def load_state_from_dir(athlete_dir) -> dict:
    """Read the trigger-state record. Missing or corrupt reads as empty, which
    fails closed: an unknown history cannot satisfy a streak."""
    p = _state_path(athlete_dir)
    try:
        raw = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        raw = {}
    fired = raw.get("fired") if isinstance(raw.get("fired"), dict) else {}
    weeks = raw.get("weeks") if isinstance(raw.get("weeks"), dict) else {}
    return {"fired": fired, "weeks": weeks}


def _atomic_write(path: Path, payload: dict) -> None:
    """Same write discipline as open_actions._atomic_write — a half-written
    record would read as 'never fired' and re-congratulate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ack-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune(state: dict) -> dict:
    fired = state.get("fired", {})
    if len(fired) > _MAX_FIRED_KEYS:
        keep = sorted(fired.items(), key=lambda kv: (kv[1] or "", kv[0]))[-_MAX_FIRED_KEYS:]
        state["fired"] = dict(keep)
    weeks = state.get("weeks", {})
    if len(weeks) > _MAX_WEEKS:
        state["weeks"] = dict(sorted(weeks.items())[-_MAX_WEEKS:])
    return state


def mark_fired_in(athlete_dir, keys, today: date | None = None) -> dict:
    """Record that these occurrences have been acknowledged. Idempotent: a key
    already present keeps its original date, so re-running cannot re-fire."""
    today = today or date.today()
    state = load_state_from_dir(athlete_dir)
    for k in keys or []:
        state["fired"].setdefault(k, today.isoformat())
    state = _prune(state)
    _atomic_write(_state_path(athlete_dir), state)
    return state


def record_week_in(athlete_dir, week_end: date, compliance_pct) -> dict:
    """Persist one week's compliance so a streak can be evaluated at all.

    `None` is stored, not skipped: a week with no planned events is a RECORDED
    BREAK in the streak, not a hole to be read over.
    """
    state = load_state_from_dir(athlete_dir)
    state["weeks"][week_end.isoformat()] = (
        int(compliance_pct) if compliance_pct is not None else None)
    state = _prune(state)
    _atomic_write(_state_path(athlete_dir), state)
    return state


# ---------------------------------------------------------------------------
# trigger 1 — hard block finished
# ---------------------------------------------------------------------------

def _block_weeks(cfg: dict, phase: str, training_week: int):
    """How many weeks the finishing block ran. None if it cannot be derived —
    the sentence then simply omits the count rather than guessing one."""
    try:
        ends = _pt._phase_ends(cfg)
    except Exception:
        return None
    order = ["base", "build", "specific", "peak"]
    if phase not in order:
        return None
    prev_end = 0
    for p in order:
        if p == phase:
            break
        prev_end = ends.get(p, prev_end)
    n = int(training_week) - int(prev_end)
    return n if 1 <= n <= 40 else None


def evaluate_block_finished(cfg: dict, ctl_today, week_end: date, *,
                            compliance_pct, coaching_level: str,
                            block_load=None, fitness_delta=None,
                            fired: dict) -> dict | None:
    """This week was a build-type week and next week steps down (§8.3 row 2).

    Scoped to the week boundary rather than to "the final session of the block":
    the week type is what `required_tss` can actually answer, and identifying a
    specific session as the block's last one from the planned-events feed would
    be a guess. `required_tss` is pure and takes `today`, so next week's type is
    a second call — the same bounded technique plan_tools.py:639 already uses on
    itself.

    Fails closed on: a week that missed its target (W3), any `{"error": ...}`
    from either call (no defensible CTL basis — Kathryn has no `phase_ctl` and
    Calum may have neither), a missing `week_type` on either side, a current
    week that is already a step-down, and an occurrence already acknowledged.
    """
    if compliance_pct is None or compliance_pct < BLOCK_MIN_COMPLIANCE:
        return None                     # W3 / §8.4 — no praise on a missed week
    if not ctl_today:
        return None
    try:
        this_wk = _pt.required_tss(cfg, float(ctl_today), today=week_end,
                                   last_week_tss=None)
        next_wk = _pt.required_tss(cfg, float(ctl_today), today=week_end + timedelta(days=7),
                                   last_week_tss=None)
    except Exception:
        return None
    if not isinstance(this_wk, dict) or not isinstance(next_wk, dict):
        return None
    if this_wk.get("error") or next_wk.get("error"):
        return None
    this_type, next_type = this_wk.get("week_type"), next_wk.get("week_type")
    if this_type not in _BUILD_WEEK_TYPES or next_type not in _STEPDOWN_WEEK_TYPES:
        return None

    key = f"block:{week_end.isoformat()}"
    if key in (fired or {}):
        return None

    block = _BLOCK_NAMES.get(this_type)
    if not block:
        return None
    weeks = _block_weeks(cfg, this_type, this_wk.get("training_week") or 0)

    if coaching_level == "beginner":
        # No Load, no Fitness — coaching_levels.py bans training metrics here.
        tail = f" — {weeks} weeks of it" if weeks else ""
        text = f"That's the {block} done{tail}. Next week is easier on purpose."
    else:
        bits = []
        if weeks:
            bits.append(f"{weeks} weeks")
        if block_load:
            bits.append(f"{int(round(float(block_load))):,} Load")
        if fitness_delta and float(fitness_delta) > 0:
            bits.append(f"Fitness up {int(round(float(fitness_delta)))} points")
        tail = f" — {', '.join(bits)}" if bits else ""
        text = f"That's the {block} done{tail}. Next week steps down."
    # block_weeks is returned so the caller can size a single fetch for the two
    # numbers §8.3 asks for ("what it bought") and re-evaluate with them. It is
    # cheaper than making this module fetch: the weekly card already holds an
    # IcuClient, and this function stays pure.
    return {"trigger": "block_finished", "key": key, "text": text,
            "block_weeks": weeks}


# ---------------------------------------------------------------------------
# trigger 2 — streak
# ---------------------------------------------------------------------------

def evaluate_streak(week_end: date, compliance_pct, *, weeks: dict,
                    coaching_level: str, fired: dict) -> dict | None:
    """3+ consecutive weeks at >=90% compliance, once per streak (§8.3 row 4).

    Reads the persisted `weeks` record, because a single weekly run only knows
    its own compliance. A week that is MISSING from the record breaks the count
    rather than being read over, so the first genuine fire is three recorded
    weeks after this lands — a streak reconstructed from backfilled data is
    exactly the claim that must not be made.
    """
    if compliance_pct is None or compliance_pct < STREAK_MIN_PCT:
        return None
    hist = dict(weeks or {})
    hist[week_end.isoformat()] = int(compliance_pct)

    run, cursor, lowest = 0, week_end, None
    while True:
        v = hist.get(cursor.isoformat(), "absent")
        if v == "absent" or v is None or v < STREAK_MIN_PCT:
            break
        run += 1
        lowest = v if lowest is None else min(lowest, v)
        cursor -= timedelta(days=7)
    if run < STREAK_WEEKS:
        return None

    start = week_end - timedelta(days=7 * (run - 1))
    key = f"streak:{start.isoformat()}"       # streak identity = where it began
    if key in (fired or {}):
        return None                            # extending it is not a new occurrence

    if coaching_level == "beginner":
        text = f"{_ordinal(run)} week running you've done what the plan asked."
    else:
        text = (f"{_ordinal(run)} week running you've hit the plan — "
                f"{lowest}% at the lowest.")
    return {"trigger": "streak", "key": key, "text": text}


# ---------------------------------------------------------------------------
# trigger 3 — first time at a distance
# ---------------------------------------------------------------------------

def evaluate_first_at_distance(activity: dict, history: list, *,
                               coaching_level: str, fired: dict) -> dict | None:
    """Longest run/ride/swim in the available history (§8.3 row 5).

    Every gate below is required, and each closes a way of making a false
    "longest" claim:
      * sport is one §8.3 names, and this activity has a real distance;
      * the feed is a list with a usable earliest date;
      * span from that earliest date >= FIRST_MIN_SPAN_DAYS, and the span is
        NAMED from it — never "this year", which the rolling window does not
        support (see module docstring);
      * at least FIRST_MIN_SPORT_SESSIONS prior sessions in that sport, so
        Calum's six real rides can never clear it;
      * EVERY prior same-sport entry carries a distance. One null means the max
        is unknown, and an unknown max cannot be beaten;
      * beats the old max by FIRST_MIN_MARGIN.
    """
    family = _SPORT_FAMILY.get((activity or {}).get("type") or (activity or {}).get("sport") or "")
    if not family:
        return None
    a_date = _act_date(activity or {})
    if not a_date:
        return None
    try:
        dist = float(activity.get("distance") or 0)
    except (TypeError, ValueError):
        return None
    if dist <= 0:
        return None
    if not isinstance(history, list) or not history:
        return None

    dates = [d for d in (_act_date(h) for h in history) if d]
    if not dates:
        return None
    span = (a_date - min(dates)).days
    if span < FIRST_MIN_SPAN_DAYS:
        return None

    a_id = str(activity.get("id") or "")
    prior_max = 0.0
    prior_n = 0
    for h in history:
        if _SPORT_FAMILY.get(h.get("type") or h.get("sport") or "") != family:
            continue
        h_date = _act_date(h)
        if not h_date or h_date > a_date:
            continue
        if a_id and str(h.get("id") or "") == a_id:
            continue
        prior_n += 1
        raw = h.get("distance")
        if raw is None:
            return None                  # unknown max -> silence, not a guess
        try:
            prior_max = max(prior_max, float(raw))
        except (TypeError, ValueError):
            return None
    if prior_n < FIRST_MIN_SPORT_SESSIONS:
        return None
    if prior_max <= 0 or dist < prior_max * FIRST_MIN_MARGIN:
        return None

    value = _distance_text(family, dist)
    key = f"first:{family}:{value}"
    if key in (fired or {}):
        return None
    # Plainly, no superlatives (§8.3).
    text = f"Longest {family} you've done {_span_phrase(span)}, at {value}."
    return {"trigger": "first_at_distance", "key": key, "text": text}


# ---------------------------------------------------------------------------
# trigger 4 — comeback
# ---------------------------------------------------------------------------

def evaluate_comeback(activity: dict, history: list, *, fired: dict) -> dict | None:
    """First completed session after >=5 days with no training (§8.3 row 6).

    Reads the ICU history feed, NOT `session-log.json`. That is the whole point
    of this function: the session log records gaps that are recording gaps, and
    on Calum it would have manufactured a monthly comeback (module docstring).

    Fails closed on: an unusable feed, a feed that does not reach back past the
    gap being claimed, no prior activity at all (nothing to measure a gap from),
    and an occurrence already acknowledged.

    NOTE ON THE INJURY BRANCH. §8.3 also names "first run after an injury-pain
    gap (`current-state.json` injury log)". There is no such store: on 29 Jul
    2026 Jamie carries `ankle` and `niggles`, Kathryn neither (her
    current-state.json holds only `last_updated`, `menstrual_cycle` and
    `open_actions`), Calum `niggles`, and no athlete has an `injury_log` key.
    Only the >=5-day branch is implemented; synthesising an injury history out of
    `niggles` prose to fill the gap is precisely the kind of inference that
    produces a false claim.
    """
    a_date = _act_date(activity or {})
    if not a_date:
        return None
    if not isinstance(history, list) or not history:
        return None
    dates = [d for d in (_act_date(h) for h in history) if d]
    if not dates:
        return None
    if min(dates) > a_date - timedelta(days=_COMEBACK_COVERAGE_DAYS):
        return None                     # short feed is indistinguishable from a break

    a_id = str(activity.get("id") or "")
    prior = [d for h, d in ((h, _act_date(h)) for h in history)
             if d and d < a_date and not (a_id and str(h.get("id") or "") == a_id)]
    if not prior:
        return None
    gap = (a_date - max(prior)).days
    if gap < COMEBACK_GAP_DAYS:
        return None

    key = f"comeback:{a_date.isoformat()}"
    if key in (fired or {}):
        return None
    # The return, not the numbers (§8.3). Days off is not a training metric, so
    # this sentence is the same at every coaching level.
    return {"trigger": "comeback", "key": key,
            "text": f"First one back after {gap} days — that's the hard one done."}


# ---------------------------------------------------------------------------
# trigger 5 — session completed while ill or compromised
# ---------------------------------------------------------------------------

def evaluate_trained_while_ill(athlete_dir, activity: dict, *, fired: dict) -> dict | None:
    """A completed session on a day the illness flag was active (§8.3 row 9).

    Consumes `lib/illness.py`, which owns the flag — this does not restate the
    illness gate, and deliberately does not re-derive the flag from
    `current-state.md` prose. W1 with force: the rendered clause is the fact
    that they trained at all, and it leads.
    """
    a_date = _act_date(activity or {})
    if not a_date:
        return None
    try:
        mins = int(round(float(activity.get("moving_time")
                               or activity.get("elapsed_time") or 0) / 60.0))
    except (TypeError, ValueError):
        return None
    if mins <= 0:
        mins = int(activity.get("duration_min") or 0)
    if mins <= 0:
        return None
    try:
        st = illness_lib.state_from_dir(athlete_dir, today=a_date)
    except Exception:
        return None
    if not st or not st.get("active"):
        return None

    key = f"ill:{activity.get('id') or a_date.isoformat()}"
    if key in (fired or {}):
        return None
    # Composed from the canonical fields, NOT from illness._label(): that renders
    # a handle for a log line ("chest infection (active, day 4)") and dropping it
    # into a sentence produces "done while chest infection (active, day 4)".
    # The condition NAME is deliberately left out — it does not fit an
    # article-free slot for every value ("ill with chest infection" vs "ill with
    # tonsillitis"), and the illness prompt block already carries it, so the
    # analysis that follows has the context. The minutes and the day number are
    # what make this specific to this athlete today (W2).
    day = int(st.get("days_in") or 0) + 1
    if st.get("status") == "recovering":
        text = f"{mins} minutes done while still recovering, on day {day}."
    else:
        text = f"{mins} minutes done while ill, on day {day}."
    return {"trigger": "trained_while_ill", "key": key, "text": text}


# ---------------------------------------------------------------------------
# renderers for the two surfaces
# ---------------------------------------------------------------------------

def _assert_register(hits, coaching_level: str):
    """Drop anything that breaks the athlete's register rather than send it.

    Layer 2, redundant while the per-trigger wording holds — which is the point,
    same reasoning as races.py:458: it is what stops a later edit quietly
    sending Calum a Load figure.
    """
    if coaching_level != "beginner":
        return hits
    return [h for h in hits if not _BEGINNER_BANNED_RE.search(h["text"])]


def filter_register(hits, coaching_level: str) -> list[dict]:
    """Public form of the register guard, for a caller that has re-rendered a
    hit outside `evaluate_*` (the weekly card does, to add the block numbers)."""
    return _assert_register([h for h in hits if h], coaching_level)


def evaluate_weekly_from_dir(athlete_dir, *, cfg: dict, ctl_today, week_end: date,
                             compliance_pct, coaching_level: str = "mid",
                             block_load=None, fitness_delta=None,
                             fired: dict | None = None,
                             weeks: dict | None = None) -> list[dict]:
    """The two week-level triggers, in card order. Pure — writes nothing."""
    st = None
    if fired is None or weeks is None:
        st = load_state_from_dir(athlete_dir)
    fired = fired if fired is not None else st["fired"]
    weeks = weeks if weeks is not None else st["weeks"]

    hits = []
    for hit in (evaluate_block_finished(cfg, ctl_today, week_end,
                                        compliance_pct=compliance_pct,
                                        coaching_level=coaching_level,
                                        block_load=block_load,
                                        fitness_delta=fitness_delta, fired=fired),
                evaluate_streak(week_end, compliance_pct, weeks=weeks,
                                coaching_level=coaching_level, fired=fired)):
        if hit:
            hits.append(hit)
    return _assert_register(hits, coaching_level)


def weekly_block(hits: list[dict]) -> str:
    """Pre-rendered acknowledgement for the weekly card — finished text plus a
    reproduce-verbatim instruction, the open_actions.weekly_block pattern.

    Placement: the weekly card, before the table. §8.3 says the consistent-week
    sentence goes "in the weekly card, before the table, not after" — a warm
    line under a list of flags reads as an afterthought — and these two triggers
    are week-level facts, so the week's own card is where they are true. It adds
    NO new push: the card already sends.
    """
    head = ["## ACKNOWLEDGEMENT — PRE-COMPUTED, REPRODUCE VERBATIM",
            "ALREADY EVALUATED in Python before this prompt ran (lib/acknowledgement.py) "
            "against the persisted compliance record and the plan's week types. Do NOT "
            "recompute a streak, a block boundary or a compliance figure, and do NOT "
            "invent an acknowledgement that is not listed here."]
    if not hits:
        head.append(
            "NO milestone fired this week. Fall back to the acknowledgement instruction in "
            "the card template below: one sentence on a STRONG or SOLID week only, and "
            "nothing at all on a LIGHT or MIXED week. Do NOT claim a streak, a block "
            "ending or a longest-ever — none was earned, and Python checked.")
        return "\n".join(head) + "\n"
    head.append(
        f"Use {'this sentence' if len(hits) == 1 else 'these sentences'} as the "
        "acknowledgement line, wording and numbers unchanged, in the position the card "
        "template marks — before the table, never after. Do not add a second "
        "acknowledgement of your own, and do not soften or drop any finding in the card "
        "to make room for it: the acknowledgement leads, the finding still lands in full.")
    return "\n".join(head + [""] + [f"  {h['text']}" for h in hits]) + "\n"


def evaluate_activity_from_dir(athlete_dir, activity: dict, history: list, *,
                               coaching_level: str = "mid",
                               fired: dict | None = None) -> list[dict]:
    """The three session-level triggers. Pure — writes nothing.

    Returns every trigger that evaluated TRUE, highest-priority first. The
    caller renders only the first (see `activity_prefix`) but should mark them
    all fired: a "longest ride" suppressed today in favour of a comeback line is
    still a fact about today's ride, and letting it re-fire on a later, shorter
    ride would be a false claim.
    """
    if fired is None:
        fired = load_state_from_dir(athlete_dir)["fired"]
    hits = []
    for hit in (evaluate_comeback(activity, history, fired=fired),
                evaluate_trained_while_ill(athlete_dir, activity, fired=fired),
                evaluate_first_at_distance(activity, history,
                                           coaching_level=coaching_level, fired=fired)):
        if hit:
            hits.append(hit)
    return _assert_register(hits, coaching_level)


def activity_prefix(hits: list[dict]) -> str:
    """The single leading clause for the activity debrief, or "".

    Deterministic and prepended, NOT handed to the model, for the reason
    §8.3 exists at all: `activity-watcher._build_prompt` runs before the
    activity is even identified (the model discovers it and emits ACTIVITY_ID),
    so an id-keyed conditional in the prompt would be exactly the
    instruction-following this repo has already documented failing twice
    (plan_builder.py:5-7). Rendering it in Python after the id is known makes
    the acknowledgement unconditional.

    Placement: the activity debrief, which already sends — no new push. These
    are session-level facts and the debrief is the message about that session;
    holding them for the weekly card would deliver them days late.

    AT MOST ONE. §8.4 bans padding, and stacking three warm lines in front of a
    debrief is padding. Priority is by which fact is the bigger one about this
    session: the return outranks the circumstances, which outrank the distance.
    Praise-then-finding is W1 / §8.5 move 0, not the §8.4 sandwich — the
    sandwich is flag-then-praise, and nothing here reorders or softens what the
    debrief goes on to say.
    """
    return f"{hits[0]['text']}\n\n" if hits else ""


# The one thing code cannot guarantee. §8.3's comeback row also asks for "then
# no analysis unless asked", and the analysis is written by the model in the
# same call. This is a standing, unconditional rule for the debrief prompt —
# unconditional because at prompt-build time the activity is not yet known, and
# pre-guessing which activity the model will pick in order to make it
# conditional would risk attaching the acknowledgement to the wrong session.
# It fails safe: ignored, the acknowledgement still leads (that part is code)
# and the worst case is a debrief that analyses a first session back.
PROMPT_NOTE = (
    "## ACKNOWLEDGEMENT — ALREADY PREPENDED IN CODE, DO NOT WRITE YOUR OWN\n"
    "If this session earned one (first session back after a break, a session done while "
    "unwell, a longest-ever distance), a single acknowledging sentence has ALREADY been "
    "evaluated in Python and will be placed as the first line of the message before it is "
    "sent. So: do NOT open your analysis by crediting the effort, do NOT congratulate, and "
    "do NOT restate that they trained while ill — it is already said, and saying it twice "
    "reads as management technique. Write the analysis only. Every finding stays at full "
    "strength: the acknowledgement leads, it never replaces or softens a finding.\n"
    "If this is the athlete's first session after a break of five days or more, keep the "
    "debrief short — acknowledge the return and stop, no analysis unless they ask for it."
)


# ---------------------------------------------------------------------------
# CLI — read-only audit. Deliberately has no write path: the only writers are
# mark_fired_in/record_week_in, called by the two surfaces at run time.
# ---------------------------------------------------------------------------

def _cli(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Evaluate acknowledgement triggers (read-only).")
    p.add_argument("--athlete", required=True)
    p.add_argument("--show-state", action="store_true")
    args = p.parse_args(argv)
    adir = BASE / "athletes" / args.athlete
    st = load_state_from_dir(adir)
    if args.show_state:
        print(json.dumps(st, indent=2, sort_keys=True))
        return 0
    print(f"fired keys: {len(st['fired'])}, weeks recorded: {len(st['weeks'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
