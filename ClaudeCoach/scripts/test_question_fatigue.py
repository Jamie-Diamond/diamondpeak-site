#!/usr/bin/env python3
"""Offline tests for the watcher question-fatigue fixes (13 Aug 2026).
Run: python3 ClaudeCoach/scripts/test_question_fatigue.py

WHAT BROKE. The watchers cannot see the conversation, so they re-asked what was
already answered: RPE re-asked after it was given (7 Jul, "log as a bug repeated
asks"; duplicate swim and ankle asks 8-9 Jul), weight asked four mornings running
with no backoff, and two questions in one message ("RPE for the run? (1-10) — and
how did the ankle feel this morning?", plus the run debrief's compound "Injury
pain during and this morning?", where the morning half is morning-checkin's job).

WHAT IS TESTED HERE. The decisions, which are the parts that can be wrong
silently: the asked-and-answered check against both the session log and the
transcript, the backoff arithmetic (trips at two, resets on an answer, passive
line at most weekly), the one-question picker, and the debrief filter that folds
a known value in instead of asking for it. The send paths themselves need a live
Telegram loop and are verified by reading the call sites.

Writes only to a tmpdir; never touches a real athlete directory, never calls a
model and never opens a socket.
"""
import importlib.util
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
import ask_gate as G

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


def _load_watcher():
    """Import activity-watcher.py under a legal module name (the filename has a
    hyphen). Its module body only defines things and reads config paths lazily,
    so importing it is safe and gives the debrief filter a real test."""
    spec = importlib.util.spec_from_file_location(
        "activity_watcher_under_test", _here / "activity-watcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TODAY = date(2026, 8, 13)
NOW = datetime(2026, 8, 13, 18, 30)


# --- 1) asked and answered: the session-log entry ---------------------------------------
answered = {"activity_id": "1", "date": "2026-08-13", "sport": "Run",
            "duration_min": 45, "rpe": 7, "feel": None,
            "logged_at": "2026-08-13T16:00:00"}
blank = {"activity_id": "2", "date": "2026-08-13", "sport": "Run",
         "duration_min": 45, "rpe": None, "feel": None,
         "logged_at": "2026-08-13T16:00:00"}

check("an RPE in the session log counts as answered",
      G.session_answered(answered, G.RPE))
check("a null RPE does not", not G.session_answered(blank, G.RPE))
check("feel prose alone answers 'how did it feel'",
      G.session_answered({"rpe": None, "feel": "flat but fine"}, G.RPE))
check("an empty feel string is not an answer",
      not G.session_answered({"rpe": None, "feel": ""}, G.RPE))
check("a during-score answers the ankle question",
      G.session_answered({"injury_pain_during": 0}, G.ANKLE))
check("zero is an answer, not a missing value",
      G.session_answered({"injury_pain_during": 0}, G.ANKLE))
check("carbs logged answers the fuelling question",
      G.session_answered({"nutrition_g_carb": 62}, G.NUTRITION))
check("the ankle question is not answered by an RPE",
      not G.session_answered(answered, G.ANKLE))


# --- 2) asked and answered: volunteered in the transcript -------------------------------
hist = [
    {"user": "how's my week looking?", "assistant": "...",
     "ts": "2026-08-13T09:00:00"},
    {"user": "that run felt rough, legs were dead", "assistant": "...",
     "ts": "2026-08-13T17:10:00"},
]
check("volunteered after the activity synced suppresses the ask",
      G.volunteered_since(hist, G.RPE, since="2026-08-13T16:00:00"))
check("the same words BEFORE the sync do not suppress it",
      not G.volunteered_since(hist, G.RPE, since="2026-08-13T17:30:00"))
check("a different question is not answered by it",
      not G.volunteered_since(hist, G.NUTRITION, since="2026-08-13T16:00:00"))
check("volunteered ankle pain suppresses the ankle ask",
      G.volunteered_since([{"user": "ankle was a 2 today", "assistant": "",
                            "ts": "2026-08-13T17:00:00"}],
                          G.ANKLE, since="2026-08-13T16:00:00"))
check("volunteered fuelling suppresses the fuelling ask",
      G.volunteered_since([{"user": "took 3 gels and two bottles", "assistant": "",
                            "ts": "2026-08-13T17:00:00"}],
                          G.NUTRITION, since="2026-08-13T16:00:00"))
# Design rule 3: the watchers' own outbound appends carry no ts and an empty user.
check("an unstamped turn is not evidence",
      not G.volunteered_since([{"user": "felt great", "assistant": ""}],
                              G.RPE, since="2026-08-13T16:00:00"))
check("the coach's own words are never the athlete's answer",
      not G.volunteered_since([{"user": "", "assistant": "RPE and how did it feel?",
                                "ts": "2026-08-13T17:00:00"}],
                              G.RPE, since="2026-08-13T16:00:00"))
check("an unknown sync time yields no chat evidence, so the ask still goes out",
      not G.volunteered_since(hist, G.RPE, since=None))
check("already_answered combines both sources",
      G.already_answered(blank, G.RPE, history=hist, since="2026-08-13T16:00:00")
      and G.already_answered(answered, G.RPE, history=[], since=None))
check("neither source means ask",
      not G.already_answered(blank, G.RPE, history=[], since=None))
check("the sync floor comes off logged_at",
      G.entry_synced_at(blank) == datetime(2026, 8, 13, 16, 0, 0))
check("a stub with no logged_at falls back to midnight on its date",
      G.entry_synced_at({"date": "2026-08-13"}) == datetime(2026, 8, 13, 0, 0, 0))


# --- 3) folding the known value in instead of asking ------------------------------------
check("a known RPE folds into a statement, not a question",
      G.known_value_line({"rpe": 7}, G.RPE) == "RPE 7 logged."
      and "?" not in G.known_value_line({"rpe": 7}, G.RPE))
check("RPE plus feel folds both",
      G.known_value_line({"rpe": 7, "feel": "Solid."}, G.RPE)
      == "RPE 7 logged, felt solid.")
check("a known during-score folds",
      G.known_value_line({"injury_pain_during": 2}, G.ANKLE) == "Ankle 2/10 during, logged.")
check("known fuelling folds",
      G.known_value_line({"nutrition_g_carb": 62}, G.NUTRITION)
      == "Fuelling logged at 62g carbs/hr.")
check("nothing quotable folds to nothing", G.known_value_line({}, G.RPE) == "")


# --- 4) the one-question picker ---------------------------------------------------------
quality = {"duration_min": 62}
long_ride = {"duration_min": 195}
check("a short session asks RPE, not fuelling",
      G.pick_question(quality, [G.RPE, G.NUTRITION]) == G.RPE)
check("a long session asks fuelling, not RPE",
      G.pick_question(long_ride, [G.RPE, G.NUTRITION]) == G.NUTRITION)
check("exactly 90 min is long",
      G.pick_question({"duration_min": 90}, [G.RPE, G.NUTRITION]) == G.NUTRITION)
check("89 min is not",
      G.pick_question({"duration_min": 89}, [G.RPE, G.NUTRITION]) == G.RPE)
check("on an injury run the during-score outranks a bare RPE",
      G.pick_question(quality, [G.ANKLE, G.RPE]) == G.ANKLE)
# The during-score also outranks the DURATION rule. Ranked below it, a 2-hour
# injury run asked about gels and neither surface ever asked about the ankle:
# the debrief filter and the follow-up nudge both call this and would agree.
check("a long injury run still asks the ankle score, not fuelling",
      G.pick_question({"duration_min": 120}, [G.ANKLE, G.RPE, G.NUTRITION]) == G.ANKLE)
check("with the ankle answered, the long session goes back to asking fuelling",
      G.pick_question({"duration_min": 120}, [G.RPE, G.NUTRITION]) == G.NUTRITION)
check("if only fuelling is unanswered, fuelling is asked even on a short session",
      G.pick_question(quality, [G.NUTRITION]) == G.NUTRITION)
check("nothing unanswered means no question", G.pick_question(quality, []) is None)
check("a missing duration does not crash the picker",
      G.pick_question({}, [G.RPE, G.NUTRITION]) == G.RPE)


# --- 5) classifying and de-compounding the debrief's own asks ---------------------------
check("the fuelling ask is recognised",
      G.classify_question("Nutrition — g carbs/hr, bottles, and sodium? (recent avg: 54g/hr)")
      == G.NUTRITION)
check("the ankle ask is recognised",
      G.classify_question("Injury pain score during and this morning? (0-10)") == G.ANKLE)
check("the RPE ask is recognised",
      G.classify_question("RPE and how did it feel?") == G.RPE)
check("a data line is not a question",
      G.classify_question("NP 218W · IF 0.82 · decoupling 3.1%") is None)
check("a statement containing the words is not a question",
      G.classify_question("Ankle 2/10 during, logged.") is None)
check("the morning half is cut from the compound ankle ask",
      G.strip_morning_half("Injury pain score during and this morning? (0-10)")
      == "Injury pain score during? (0-10)")
check("the during half survives intact",
      "during" in G.strip_morning_half("Injury pain during and this morning? (0-10)")
      and "morning" not in G.strip_morning_half(
          "Injury pain during and this morning? (0-10)"))
check("a plain during-only ask is left alone",
      G.strip_morning_half("Injury pain score during? (0-10)")
      == "Injury pain score during? (0-10)")


# --- 6) the backoff --------------------------------------------------------------------
def _run_mornings(days, answers=(), asked_when_allowed=True):
    """Walk N consecutive mornings through the gate the way morning-checkin does:
    evaluate, then record what actually shipped — the ask if one was asked, the
    passive line if one was appended (note_asked_in / note_passive_in in the real
    caller). `answers` is the set of dates on which the athlete answered."""
    state = {"asks": {}}
    log = []
    for i in range(days):
        d = TODAY + timedelta(days=i)
        answered_on = max((a for a in answers if a <= d), default=None)
        state, dec = G.decide(state, G.WEIGHT, today=d, answered_on=answered_on)
        log.append((d, dec["ask"], dec["passive"], dec["misses"]))
        rec = dict(state["asks"][G.WEIGHT])
        if dec["ask"] and asked_when_allowed:
            rec["last_asked"] = d.isoformat()
        if dec["passive"]:
            rec["last_passive"] = d.isoformat()
        state["asks"][G.WEIGHT] = rec
    return log, state


log, _ = _run_mornings(5)
check("morning 1 asks", log[0][1] is True)
check("morning 2 asks (one miss so far)", log[1][1] is True and log[1][3] == 1)
check("morning 3 stops asking (two consecutive misses)",
      log[2][1] is False and log[2][3] == 2)
check("it stays stopped", log[3][1] is False and log[4][1] is False)
check("the passive line goes out with the first backed-off card",
      log[2][2] == G.PASSIVE_LINES[G.WEIGHT])
check("the passive line is not repeated the next day", log[3][2] is None)
check("the passive line is a statement, not a question",
      "?" not in G.PASSIVE_LINES[G.WEIGHT])

log, _ = _run_mornings(12)
passives = [d for (d, _a, p, _m) in log if p]
check("the passive line is at most weekly",
      len(passives) == 2 and (passives[1] - passives[0]).days == G.PASSIVE_EVERY_DAYS)

# An answer on morning 2 resets the counter, so backoff never trips on four mornings
# that would otherwise have reached it. The counter then restarts from the NEXT
# unanswered ask — one miss on morning 4, not a resumption of the old count.
log, st = _run_mornings(4, answers={TODAY + timedelta(days=1)})
check("an answer resets the miss counter",
      all(a is True for (_d, a, _p, _m) in log)
      and [m for (_d, _a, _p, m) in log] == [0, 0, 0, 1])
check("the answer date is remembered",
      st["asks"][G.WEIGHT]["last_answer_seen"] == (TODAY + timedelta(days=1)).isoformat())

# An answer AFTER backoff has tripped brings the question back.
state = {"asks": {G.WEIGHT: {"last_asked": TODAY.isoformat(), "misses": 2,
                             "last_miss_date": TODAY.isoformat(),
                             "last_passive": TODAY.isoformat()}}}
_s, dec = G.decide(state, G.WEIGHT, today=TODAY + timedelta(days=1),
                   answered_on=TODAY + timedelta(days=1))
check("an answer after backoff resumes asking", dec["ask"] is True and dec["misses"] == 0)

# An unprompted answer counts, even with no ask on record.
_s, dec = G.decide({"asks": {}}, G.WEIGHT, today=TODAY, answered_on=TODAY)
check("weighing in unprompted counts as an answer", dec["misses"] == 0)

# A stale answer does NOT clear a fresh ask.
state = {"asks": {G.WEIGHT: {"last_asked": TODAY.isoformat(), "misses": 0}}}
_s, dec = G.decide(state, G.WEIGHT, today=TODAY + timedelta(days=1),
                   answered_on=TODAY - timedelta(days=4))
check("an answer older than the ask does not clear it", dec["misses"] == 1)

# Design rule 2/3: the same morning re-evaluated (a failed Claude call, 15-min poll)
# must not count two misses.
state = {"asks": {G.WEIGHT: {"last_asked": TODAY.isoformat(), "misses": 0}}}
tomorrow = TODAY + timedelta(days=1)
for _ in range(4):
    state, dec = G.decide(state, G.WEIGHT, today=tomorrow, answered_on=None)
check("re-evaluating one morning counts one miss, not four", dec["misses"] == 1)

# Never asked at all -> no miss can accrue.
_s, dec = G.decide({"asks": {}}, G.WEIGHT, today=TODAY, answered_on=None)
check("a question never asked accrues no miss",
      dec["misses"] == 0 and dec["ask"] is True)


# --- 7) recording the ask from the text that shipped ------------------------------------
card = ("*Good morning — Thu 13 Aug*\n\n*Today:* Z2 ride — 90 min · 65 Load\n\n"
        "Weight this morning?\n\n_38 days to Outlaw_")
check("a weight ask in the card is recorded", G.asked_in_text(G.WEIGHT, card))
check("an ankle ask is not, when it isn't there", not G.asked_in_text(G.ANKLE_SCORE, card))
check("the ankle morning ask is recorded",
      G.asked_in_text(G.ANKLE_SCORE, "Ankle score this morning? (0-10)"))
check("the before-you-go variant is recorded",
      G.asked_in_text(G.ANKLE_SCORE, "Injury pain score before heading out? (0-10)"))
check("a card whose question the model dropped records nothing",
      not G.asked_in_text(G.WEIGHT,
                          "*Good morning*\n\n*Today:* Rest day\n\n_38 days to Outlaw_"))
check("a flag mentioning ankle scores is not an ask",
      not G.asked_in_text(G.ANKLE_SCORE,
                          "⚠️ Ankle scores rising three readings running, drop run volume."))


# --- 8) answer sources, derived not recorded -------------------------------------------
cs = {"weight_readings": [{"date": "2026-08-05", "kg": 82.0},
                          {"date": "2026-08-11", "kg": 81.6}],
      "ankle": {"history": [{"date": "2026-08-09", "score": 2}],
                "pain_today_resting_date": "2026-08-12"}}
check("the latest weight reading is the weight answer date",
      G.answer_date(G.WEIGHT, current_state=cs) == date(2026, 8, 11))
check("the ankle answer date takes the latest of every ankle source",
      G.answer_date(G.ANKLE_SCORE, current_state=cs) == date(2026, 8, 12))
check("a next-morning score on a run also answers the ankle ask",
      G.answer_date(G.ANKLE_SCORE, current_state=cs,
                    session_log=[{"date": "2026-08-13",
                                  "injury_pain_next_morning": 1}]) == date(2026, 8, 13))
check("no readings at all means no answer date",
      G.answer_date(G.WEIGHT, current_state={}) is None)
check("a weight 4 days old is due", G.weight_reading_due(cs, date(2026, 8, 15)))
check("a weight 2 days old is not", not G.weight_reading_due(cs, date(2026, 8, 13)))
check("no weight ever recorded is due", G.weight_reading_due({}, TODAY))


# --- 9) state file round-trip ----------------------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="askgate-test-"))
check("a missing state file reads as empty, so the question is asked",
      G.load_state_from_dir(tmp) == {"asks": {}})
G.note_asked_in(tmp, G.WEIGHT, TODAY)
check("the state file lands next to the athlete's other state files",
      (tmp / "standing-ask-state.json").exists())
check("the ask date round-trips",
      G.load_state_from_dir(tmp)["asks"][G.WEIGHT]["last_asked"] == TODAY.isoformat())
G.note_passive_in(tmp, G.WEIGHT, TODAY)
_rt = G.load_state_from_dir(tmp)["asks"][G.WEIGHT]
check("recording the passive line preserves the ask date",
      _rt["last_passive"] == TODAY.isoformat() and _rt["last_asked"] == TODAY.isoformat())
check("the schema is exactly the documented keys",
      set(_rt) == {"last_asked", "misses", "last_miss_date",
                   "last_answer_seen", "last_passive"})
(tmp / "standing-ask-state.json").write_text("{ not json")
check("a corrupt state file fails OPEN (asks) rather than silencing the card",
      G.load_state_from_dir(tmp) == {"asks": {}}
      and G.decide(G.load_state_from_dir(tmp), G.WEIGHT, today=TODAY)[1]["ask"] is True)


# --- 10) the debrief filter, end to end ------------------------------------------------
W = _load_watcher()

# `history` is injected, so the filter is exercised without an athlete tree.
ride = {"activity_id": "9", "date": "2026-08-13", "sport": "Ride",
        "duration_min": 62, "rpe": None, "nutrition_g_carb": None,
        "logged_at": "2026-08-13T16:00:00"}

out = W._filter_debrief_questions(
    "Solid Z2. Form held to the end.\nNP 218W · IF 0.78\n"
    "Nutrition — g carbs/hr, bottles, and sodium?", ride, "tester", history=[])
check("an unanswered fuelling ask on a short ride survives when it is the only ask",
      "carbs/hr" in out and "NP 218W" in out)

out = W._filter_debrief_questions(
    "Solid Z2.\nNP 218W · IF 0.78\nRPE and how did it feel?\n"
    "Nutrition — g carbs/hr, bottles, and sodium?", ride, "tester", history=[])
check("two asks on a short session keep RPE only",
      "how did it feel" in out and "carbs/hr" not in out)
check("the surviving debrief keeps its data lines", "NP 218W" in out)

out = W._filter_debrief_questions(
    "Long one done.\nNP 205W · IF 0.71 · decoupling 4.2%\nRPE and how did it feel?\n"
    "Nutrition — g carbs/hr, bottles, and sodium?",
    dict(ride, duration_min=195), "tester", history=[])
check("two asks on a long session keep fuelling only",
      "carbs/hr" in out and "how did it feel" not in out)

out = W._filter_debrief_questions(
    "Solid Z2.\nNP 218W · IF 0.78\nRPE and how did it feel?",
    dict(ride, rpe=7), "tester", history=[])
check("an already-answered ask is folded in, not asked",
      "RPE 7 logged." in out and "?" not in out)

out = W._filter_debrief_questions(
    "Solid Z2.\nNP 218W · IF 0.78\nRPE and how did it feel?", ride, "tester",
    history=[{"user": "that felt easy, RPE 4", "assistant": "",
              "ts": "2026-08-13T17:00:00"}])
check("an ask the athlete already volunteered in chat is dropped",
      "?" not in out and "NP 218W" in out)
check("dropping the ask does not empty the debrief", out.strip().startswith("Solid Z2."))

out = W._filter_debrief_questions(
    "Easy run, HR sat under the cap.\n% time HR ≤150: 94% | Injury pain score during "
    "and this morning? (0-10)",
    {"activity_id": "9", "date": "2026-08-13", "sport": "Run", "duration_min": 45,
     "injury_pain_during": None, "logged_at": "2026-08-13T16:00:00"}, "tester",
    history=[])
check("the compound ankle ask keeps the during half",
      "during" in out and "0-10" in out)
check("the compound ankle ask loses the morning half, which the card owns",
      "this morning" not in out)
check("a merged line keeps the data that shared it with the question",
      "94%" in out)

out = W._filter_debrief_questions(
    "Easy run.\n% time HR ≤150: 94% | RPE and how did it feel?",
    dict(ride, sport="Run", rpe=6), "tester", history=[])
check("folding into a merged line keeps both halves",
      "94%" in out and "RPE 6 logged." in out and "?" not in out)

out = W._filter_debrief_questions("30 min steady. Nothing to flag.", ride, "tester",
                                  history=[])
check("a debrief with no question is returned untouched",
      out == "30 min steady. Nothing to flag.")
out = W._filter_debrief_questions("", ride, "tester", history=[])
check("an empty debrief is returned untouched", out == "")


if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
