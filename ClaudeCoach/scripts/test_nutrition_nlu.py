#!/usr/bin/env python3
"""Offline tests for lib/nutrition_nlu.py's converse() turn. No network: the model call
is a stubbed runner.

converse() replied about the athlete as "he" and continued a two-day-old thread as if it
were live, because the transcript it built had no timestamps and no notion of the current
time. These checks are about the prompt actually carrying that information, not about
what a real model does with it - that cannot be tested offline.
Run: python3 ClaudeCoach/scripts/test_nutrition_nlu.py
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "nutrition_nlu.py").exists():
        sys.path.insert(0, str(cand))
        break
import nutrition_nlu as NLU

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


def capturing_runner(sink):
    """Records the prompt it was sent and replies with a fixed string, so the test can
    inspect what converse() actually built rather than guess at it."""
    def run(cmd, input=None, **kwargs):
        sink.append(input)
        return type("P", (), {"stdout": "Right, here is what I'd do.", "stderr": ""})()
    return run

# 1) The transcript carries each turn's OWN timestamp, not a flat "role: text" - a
#    two-day-old exchange has to be visibly two days old, not indistinguishable from one
#    that just happened.
sent = []
history = [{"role": "athlete", "text": "should I have the pasta or the rice?",
            "at": "2026-08-11T19:30"},
           {"role": "coach", "text": "Rice, you are short on carbs.",
            "at": "2026-08-11T19:31"}]
NLU.converse("what about now?", {"day_type": "standard"}, history, "claude", "m",
             log=lambda *a: None, runner=capturing_runner(sent),
             now_iso="2026-08-13T09:00")
prompt = sent[0]
check("each history line carries its own timestamp",
      "2026-08-11T19:30 athlete: should I have the pasta or the rice?" in prompt
      and "2026-08-11T19:31 coach: Rice, you are short on carbs." in prompt)
check("the current time is injected as a NOW line", "NOW: 2026-08-13T09:00" in prompt)

# 2) A turn missing "at" (old chat.json rows, before this fix) degrades to plain
#    "role: text" rather than crashing the prompt build.
sent2 = []
NLU.converse("hi", {}, [{"role": "athlete", "text": "no timestamp on this one"}],
             "claude", "m", log=lambda *a: None, runner=capturing_runner(sent2),
             now_iso="2026-08-13T09:00")
check("a turn with no timestamp still renders, untimed",
      "athlete: no timestamp on this one" in sent2[0])

# 3) now_iso is a PARAMETER, not read from the clock inside nlu - a test has no way to
#    fake datetime.now() through a stubbed runner, so the caller has to be able to hand
#    the current time in.
sent3 = []
NLU.converse("hi", {}, [], "claude", "m", log=lambda *a: None,
             runner=capturing_runner(sent3), now_iso="2099-01-01T00:00")
check("a supplied now_iso is used verbatim", "NOW: 2099-01-01T00:00" in sent3[0])
sent4 = []
NLU.converse("hi", {}, [], "claude", "m", log=lambda *a: None, runner=capturing_runner(sent4))
check("an omitted now_iso still produces a NOW line", "NOW: " in sent4[0])

# 4) The prompt tells the model the timestamps are real and to treat an old exchange as
#    stale background rather than a live thread.
check("the prompt instructs staleness handling by time",
      "stale" in NLU.CONVERSE_PROMPT and "hours or days old" in NLU.CONVERSE_PROMPT)

# 5) SECOND PERSON. The prompt described the athlete as "he/him" throughout with nothing
#    telling the model who it is actually talking to, and it replied in the third person -
#    "He's not answering anything, so I'm not asking anything else."
check("the prompt states he is being addressed directly",
      'address him directly as "you"' in NLU.CONVERSE_PROMPT)
check("the prompt explicitly forbids the third person",
      "third person" in NLU.CONVERSE_PROMPT)
check("the prompt explains the output is sent to him verbatim",
      "verbatim" in NLU.CONVERSE_PROMPT)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")


# --- decide_correction: the model decides, the code executes -------------------------
# (13 Aug 2026: regex detectors registered '100g' as an excluded food and re-searched a
# label the bot was holding. The decision is now the model's; these checks are that the
# prompt carries the item basis, that valid decisions pass through, and that garbage or
# an unavailable model degrade to None so the deterministic fallback can run.)

def fixed_runner(reply, sink=None):
    def run(cmd, input=None, **kwargs):
        if sink is not None:
            sink.append(input)
        return type("P", (), {"stdout": reply, "stderr": ""})()
    return run


_item = {"resolved_name": "Spanish omelette", "kcal": 120.0, "carb_g": 10.0,
         "per_100g": {"kcal": 120.0, "carb_g": 10.0}, "portion_used_g": 100.0,
         "pack_g": 380.0}

sink = []
got = NLU.decide_correction("That's 100g I had 160g", _item, "claude", "m",
                            log=lambda *a: None,
                            runner=fixed_runner('{"kind":"rescale","grams":160}', sink))
check("rescale decision passes through", got == {"kind": "rescale", "grams": 160})
check("prompt carries the per-100g basis", "per_100g" in (sink[0] or ""))
check("prompt carries the pack weight", "pack_g" in (sink[0] or ""))
check("prompt forbids the model computing macros",
      "Never return macros" in (sink[0] or ""))

got = NLU.decide_correction("not peanut butter, plain butter", _item, "claude", "m",
                            log=lambda *a: None,
                            runner=fixed_runner(
                                '{"kind":"reidentify","text":"salted butter",'
                                '"exclusions":["peanut butter"]}'))
check("reidentify decision passes through", got and got["kind"] == "reidentify"
      and got["exclusions"] == ["peanut butter"])

got = NLU.decide_correction("whatever", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner("I think he means more food?"))
check("prose instead of JSON degrades to None", got is None)

got = NLU.decide_correction("whatever", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner('{"kind":"invent_numbers"}'))
check("unknown decision kind degrades to None", got is None)


# --- label_to_item keeps the basis that makes corrections arithmetic ------------------
lbl = NLU.label_to_item({"per": "100g", "portion_g": None, "kcal": 120, "carb_g": 10,
                         "fat_g": 5, "pack_g": 380, "product": "Spanish omelette"})
check("label item keeps per-100g basis", lbl.get("per_100g", {}).get("kcal") == 120.0)
check("label item keeps pack weight", lbl.get("pack_g") == 380.0)

lbl = NLU.label_to_item({"per": "portion", "portion_g": 40, "kcal": 100, "carb_g": 26,
                         "product": "bar"})
check("per-portion label derives per-100g basis",
      lbl.get("per_100g", {}).get("kcal") == 250.0)
check("per-portion label records the portion", lbl.get("portion_used_g") == 40.0)


# --- a scoop of a macro-carrying drink mix is food, not a dose -------------------------
got = NLU.classify("1 scoop sis rego chocolate", False, "claude", "m",
                   log=lambda *a: None,
                   runner=fixed_runner('{"intent":"log_food","items":'
                                       '[{"text":"sis rego chocolate","portion_g":null,'
                                       '"in_session":false}]}'))
check("REGO stays food despite the scoop", got.get("intent") == "log_food")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED"); sys.exit(1)
print("all checks passed")


# --- the log-editing verbs (13 Aug 2026) ----------------------------------------------
# Four edits Jamie had to route through a human operator, because the bot had no verb for
# any of them: move an entry's time, correct its name without re-resolving it, remember a
# lasting fact about a product, and log something at a time he states. The model decides
# which of those a message is; these checks are that each decision survives the trip.

print("\n--- retime: 'the initial rye bread was 830am' ---")
got = NLU.decide_correction("the initial rye bread was 830am", _item, "claude", "m",
                            log=lambda *a: None,
                            runner=fixed_runner(
                                '{"kind":"retime","time":"08:30",'
                                '"which":"initial rye bread"}'))
check("retime decision passes through",
      got == {"kind": "retime", "time": "08:30", "which": "initial rye bread"})
got = NLU.decide_correction("it was at 1350", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner('{"kind":"retime","time":"1350"}'))
check("a bare HHMM time is normalised, and no `which` means the latest",
      got == {"kind": "retime", "time": "13:50", "which": ""})
check("a retime with an impossible time degrades to None",
      NLU.decide_correction("x", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner('{"kind":"retime","time":"29:99"}'))
      is None)
check("a retime with no time at all degrades to None",
      NLU.decide_correction("x", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner('{"kind":"retime","which":"toast"}'))
      is None)

print("\n--- rename: 'the 160g was a pack of bbq chicken' ---")
got = NLU.decide_correction("the 160g was a pack of bbq chicken", _item, "claude", "m",
                            log=lambda *a: None,
                            runner=fixed_runner(
                                '{"kind":"rename","name":"BBQ chicken, pack",'
                                '"which":"the 160g"}'))
check("rename decision passes through",
      got == {"kind": "rename", "name": "BBQ chicken, pack", "which": "the 160g"})
check("a rename with no name degrades to None",
      NLU.decide_correction("x", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner('{"kind":"rename","which":"the 160g"}'))
      is None)
# The model needs the entry's confidence to choose rename over reidentify: his own label
# figures survive a renaming, a lookup's figures do not.
sink2 = []
NLU.decide_correction("that was bbq chicken", dict(_item, confidence="label"), "claude",
                      "m", log=lambda *a: None,
                      runner=fixed_runner('{"kind":"rename","name":"BBQ chicken"}',
                                          sink2))
check("the prompt carries the item's confidence", '"confidence": "label"' in sink2[0])
check("the prompt says a non-label entry means reidentify, not rename",
      "reidentify instead" in NLU.CORRECTION_PROMPT)

print("\n--- remember: 'a rego scoop is half a portion' ---")
got = NLU.decide_correction("a rego scoop is half a portion", {}, "claude", "m",
                            log=lambda *a: None,
                            runner=fixed_runner(
                                '{"kind":"remember","product":"SiS REGO",'
                                '"field":"scoop_g","value":25}'))
check("remember decision passes through, product key lowercased",
      got == {"kind": "remember", "product": "sis rego", "field": "scoop_g",
              "value": 25.0})
# The REGO message ALSO fixes the entry in front of him, which is one decision, not two.
got = NLU.decide_correction("a rego scoop is half a portion, so that was 25g", _item,
                            "claude", "m", log=lambda *a: None,
                            runner=fixed_runner(
                                '{"kind":"remember_and_rescale","product":"sis rego",'
                                '"field":"scoop_g","value":25,"grams":25}'))
check("remember_and_rescale carries both the fact and the amount",
      got == {"kind": "remember_and_rescale", "product": "sis rego",
              "field": "scoop_g", "value": 25.0, "grams": 25.0})
check("remember_and_rescale with no usable grams keeps the FACT and drops the rescale",
      NLU.decide_correction("x", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner(
                                '{"kind":"remember_and_rescale","product":"sis rego",'
                                '"field":"scoop_g","value":25,"grams":"loads"}'))
      == {"kind": "remember", "product": "sis rego", "field": "scoop_g", "value": 25.0})
got = NLU.decide_correction("sis choco is the go energy choco fudge bar", {}, "claude",
                            "m", log=lambda *a: None,
                            runner=fixed_runner(
                                '{"kind":"remember","product":"sis choco",'
                                '"field":"means","value":"SiS GO Energy Choco Fudge bar"}'))
check("a means alias passes through as text",
      got and got["field"] == "means"
      and got["value"] == "SiS GO Energy Choco Fudge bar")
# This file is PERMANENT and consulted deterministically, unlike the day's exclusions, so
# a wobble here is a wrong answer for ever rather than for a day.
for bad, why in (
        ('{"kind":"remember","product":"sis rego","field":"kcal","value":80}',
         "an unknown field is refused"),
        ('{"kind":"remember","product":"sis rego","field":"scoop_g","value":"a scoop"}',
         "a non-numeric weight is refused"),
        ('{"kind":"remember","product":"sis rego","field":"scoop_g","value":0}',
         "a zero weight is refused"),
        ('{"kind":"remember","product":"","field":"scoop_g","value":25}',
         "a fact about no product is refused"),
        ('{"kind":"remember","product":"sis choco","field":"means","value":"x"}',
         "a one-character alias is refused")):
    check(why, NLU.decide_correction("x", {}, "claude", "m", log=lambda *a: None,
                                     runner=fixed_runner(bad)) is None)

print("\n--- an empty item is still a valid thing to decide about ---")
# The bot used to skip the model entirely when nothing was logged, so a fact about a
# product on an empty day fell through to "nothing logged today to correct".
sink3 = []
got = NLU.decide_correction("a rego scoop is half a portion", {}, "claude", "m",
                            log=lambda *a: None,
                            runner=fixed_runner('{"kind":"remember","product":"sis rego",'
                                                '"field":"scoop_g","value":25}', sink3))
check("decide_correction survives an empty item", got and got["kind"] == "remember")
check("and the prompt says an empty item is expected",
      "EMPTY, which means nothing is logged yet" in NLU.CORRECTION_PROMPT)

print("\n--- stated times on NEW logs ---")
for raw, want in (("13:50", "13:50"), ("1350", "13:50"), ("830", "08:30"),
                  ("8:30", "08:30"), ("0830", "08:30"), ("23:59", "23:59")):
    check(f"{raw!r} normalises to {want}", NLU.normalise_hhmm(raw) == want)
for bad in ("24:00", "13:60", "half seven", "", None, "1", "12345", "13.50"):
    check(f"{bad!r} is dropped rather than repaired", NLU.normalise_hhmm(bad) is None)

got = NLU.classify("add second slice of toast with butter at 1350", False, "claude", "m",
                   log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":[{"text":"slice of toast with '
                       'butter","portion_g":null,"in_session":false,"at":"13:50"}]}'))
check("food eaten at a stated time is still a log", got.get("intent") == "log_food")
check("and the item carries the time", got["items"][0]["at"] == "13:50")
got = NLU.classify("i had a banana", False, "claude", "m", log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":[{"text":"banana",'
                       '"portion_g":120,"in_session":false}]}'))
check("no stated time leaves `at` empty, so the logger stamps it now",
      got["items"][0]["at"] is None)
got = NLU.classify("toast this morning", False, "claude", "m", log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":[{"text":"toast","portion_g":null,'
                       '"in_session":false,"at":"this morning"}]}'))
check("a vague time is dropped, never guessed at", got["items"][0]["at"] is None)
check("the parse prompt forbids guessing a time",
      "NEVER guess" in NLU.PARSE_PROMPT
      and "in_session, at}" in NLU.PARSE_PROMPT)
check("the parse prompt keeps already-eaten-with-a-time as a log",
      "STILL log_food" in NLU.PARSE_PROMPT)
check("the parse prompt routes flat statements about the log to correction",
      "the initial rye bread was 830am" in NLU.PARSE_PROMPT
      and "a rego scoop is half a portion" in NLU.PARSE_PROMPT)
# interpret() is a SECOND model call on the same message, and the items it returns are the
# ones actually resolved when it succeeds - a time carried only on classify's items would
# be lost on that path.
plan = NLU.interpret("second slice of toast at 1350", "claude", "m",
                     log=lambda *a: None,
                     runner=fixed_runner(
                         '{"items":[{"canonical_name":"toast with butter",'
                         '"search_terms":["toast with butter"],"at":"1350"}]}'))
check("interpret carries a stated time too", plan["items"][0]["at"] == "13:50")

print("\n--- the two exemplar messages reach the model at all ---")
# Neither must be swallowed by a fast path: a bare 3-digit number in the 40-200 range is a
# plausible weigh-in, and a retime that routed to log_weight would write a weight of 130 kg.
for phrase in ("the initial rye bread was 830am", "a rego scoop is half a portion",
               "the 160g was a pack of bbq chicken"):
    check(f"{phrase!r} is not read as a weigh-in",
          NLU.looks_like_weight(phrase) is None)
    check(f"and {phrase!r} is handed to the model",
          NLU.fast_intent(phrase, False) is None)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED)); sys.exit(1)
print("all checks passed")
