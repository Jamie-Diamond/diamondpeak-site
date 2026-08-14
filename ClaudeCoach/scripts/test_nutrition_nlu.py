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
      # `meal` joined the item keys, then `stated`, so the shape line moved twice. Asserted
      # on the keys rather than the whole line: the point is that each is asked for.
      and "in_session, at, meal," in NLU.PARSE_PROMPT
      and "stated}" in NLU.PARSE_PROMPT)
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

print("\n--- the meal he NAMED, read out of the message itself ---")
# Meals were a clock inference and nothing else, so "for breakfast I had porridge" typed at
# 13:49 was filed under lunch. The model may now name the meal - but only when the message
# names it, because a meal it asserts stops being questioned downstream.
check("the parse prompt asks for a meal on each item",
      "in_session, at, meal," in NLU.PARSE_PROMPT
      and '"breakfast" | "lunch" | "dinner" | "snacks", or null' in NLU.PARSE_PROMPT)
check("and says a clock time is evidence, not proof",
      "A CLOCK TIME IS EVIDENCE, NOT PROOF" in NLU.PARSE_PROMPT)
check("with both sides of that distinction worked through",
      '"rye bread at 8:30 this morning" -> {"at":"08:30","meal":null}' in NLU.PARSE_PROMPT
      and '"rye bread for breakfast at 8:30" -> {"at":"08:30","meal":"breakfast"}'
      in NLU.PARSE_PROMPT)
check("in-session fuel is told it is not a meal", "never a meal" in NLU.PARSE_PROMPT)

got = NLU.classify("for breakfast I had porridge", False, "claude", "m",
                   log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":[{"text":"porridge","portion_g":null,'
                       '"in_session":false,"at":null,"meal":"breakfast"}]}'))
check("a stated meal reaches the item", got["items"][0]["meal"] == "breakfast")
got = NLU.classify("chicken and rice for supper", False, "claude", "m",
                   log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":[{"text":"chicken and rice",'
                       '"in_session":false,"meal":"supper"}]}'))
check("his own word for it is normalised to a bucket the app renders",
      got["items"][0]["meal"] == "dinner")
got = NLU.classify("some nuts mid-afternoon", False, "claude", "m", log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":[{"text":"nuts","in_session":false,'
                       '"meal":"elevenses"}]}'))
check("a bucket nothing renders is DROPPED, so the clock fallback decides instead",
      got["items"][0]["meal"] == "")
got = NLU.classify("toast at 8:30", False, "claude", "m", log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":[{"text":"toast","in_session":false,'
                       '"at":"8:30","meal":null}]}'))
check("a time with no meal named leaves the meal to the clock",
      got["items"][0]["at"] == "08:30" and got["items"][0]["meal"] == "")
# A gel is fuel, not lunch. The flag wins over any meal word on the same item, exactly as
# it wins over a model-asserted in_session without evidence in the words.
got = NLU.classify("gel on the bike", False, "claude", "m", log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":[{"text":"SiS GO gel",'
                       '"in_session":true,"meal":"lunch"}]}'))
check("in-session fuel carries no meal even when the model names one",
      got["items"][0]["in_session"] is True and got["items"][0]["meal"] == "")
got = NLU.classify("i had a banana", False, "claude", "m", log=lambda *a: None,
                   runner=fixed_runner(
                       '{"intent":"log_food","items":["banana"]}'))
check("a bare string item still has the key, so no caller has to test for it",
      got["items"][0]["meal"] == "")

# interpret() is the parse that usually WINS, so a meal carried only on classify's items
# would be lost in the common case - the same trap the stated time fell into.
plan = NLU.interpret("for breakfast I had porridge", "claude", "m", log=lambda *a: None,
                     runner=fixed_runner(
                         '{"items":[{"canonical_name":"porridge",'
                         '"search_terms":["porridge oats"],"meal":"breakfast"}]}'))
check("interpret carries the meal too", plan["items"][0]["meal"] == "breakfast")
plan = NLU.interpret("gel on the bike", "claude", "m", log=lambda *a: None,
                     runner=fixed_runner(
                         '{"items":[{"canonical_name":"SiS GO gel",'
                         '"search_terms":["sis go gel"],"in_session":true,'
                         '"meal":"lunch"}]}'))
check("and drops it for in-session fuel on that path as well",
      plan["items"][0]["meal"] == "")
check("the interpret prompt asks for the meal on the same terms",
      "A clock time is evidence" in NLU.INTERPRET_PROMPT)

# --- "what should I eat" leads with NAMED MEALS, not numbers -------------------------
# 13 Aug 2026, Jamie: a good answer to "what should I eat?" opens with food he could put
# on a plate, each option tagged with the gap it closes, and only then the figures. The old
# section asked for "two or three CONCRETE options" but said nothing about ORDER, and the
# replies opened with the day's remaining kcal and worked down to a suggestion - a budget
# report with food at the bottom. What the model does with the instruction cannot be tested
# offline; that the instruction is there, and in the right order, can.
print("\n--- WHAT TO EAT: named meals first, numbers second ---")
_wte = NLU.CONVERSE_PROMPT[NLU.CONVERSE_PROMPT.index("WHAT TO EAT"):]
check("the section is headed named-meals-first",
      "NAMED MEALS FIRST" in NLU.CONVERSE_PROMPT)
check("it demands named options with nothing before them",
      "nothing before them" in _wte and "No\n  preamble" in _wte)
check("each option must carry a one-clause why naming the gap",
      "one-clause WHY" in _wte and "the gap or the demand it answers" in _wte)
check("the why is sourced from demand_ahead and the basis strings, not invented",
      all(k in _wte for k in ("demand_ahead", "carb_basis", "fat_basis")))
check("the gap fields are named so their size never has to be worked out",
      "gap_to_low_g" in _wte and "room_to_high_g" in _wte)
check("the sign of each gap field is stated, so 'over' is not read as 'short'",
      "how much more is wanted" in _wte and "negative\n  when he is past it" in _wte)
# A negative room_to_high_g is NEW information: before this the model could not compute an
# overage, because arithmetic is banned. On protein it means he has cleared a floor, which
# is the thing that was wanted - so the same field that helps on carbohydrate is a fresh
# invitation to moralise on protein unless it is tied to `bias`.
check("a negative room figure is tied to the bias, so clearing a floor is not a breach",
      "only means\n  something where that macro's `bias` is a ceiling or a band" in _wte
      and "being past the top is exactly right" in _wte)
check("and no smaller option is offered because a floor was cleared",
      "never\n  offer a smaller option because of it" in _wte)
check("options come from his own foods before anything invented",
      "`foods_he_actually_eats` BEFORE anything invented" in _wte)
check("the new eating_levers fields are named so they can be used to CHOOSE",
      "`lean`" in _wte and "`usual_meal`" in _wte)
check("training fuel is not offered as dinner",
      "in_session_fuel" in _wte and "never as dinner" in _wte)
check("numbers are capped at one line and explicitly follow the food",
      "at most ONE line of numbers" in _wte
      and "Numbers support the choice; they never lead it." in _wte)
check("and the coach still says which one he would pick",
      "which single option you would pick" in _wte)

# The rules the rewrite had to carry across rather than replace.
check("the estimate-labelling rule survived the rewrite",
      "are ESTIMATES and must be labelled as such" in _wte)
check("the city/delivery rule survived the rewrite",
      "His city is in the facts" in _wte and "rather than naming a chain" in _wte)
check("the fibre PHASE anchor survived the rewrite",
      "fibre PHASE" in _wte and "the ceiling has expired" in _wte)
check("no arithmetic, no moralising and in_session protection are still in force",
      "NEVER do arithmetic" in NLU.CONVERSE_PROMPT
      and "Never moralise about food, never use restriction" in NLU.CONVERSE_PROMPT
      and "Never suggest cutting anything marked in_session" in NLU.CONVERSE_PROMPT)

# Option debates arrive through converse(), NOT through advise(): debate() resolves the
# options and then calls converse with `options_on_the_table`. A reply shape fixed only in
# ADVICE_PROMPT would change nothing he sees, so the pick-first shape has to be here too.
check("converse handles a resolved option debate in its own right",
      "options_on_the_table" in NLU.CONVERSE_PROMPT and "if_eaten" in NLU.CONVERSE_PROMPT)
check("and that debate leads with the pick and the gap, not the arithmetic",
      "Lead\nwith your PICK and the gap it closes" in NLU.CONVERSE_PROMPT)

# ADVICE_PROMPT is currently reached by nothing but these tests; fixed anyway so the two
# do not diverge if it is ever wired up again.
check("the advice prompt leads with the pick, in the first sentence",
      "Your PICK, named, in the first sentence" in NLU.ADVICE_PROMPT)
check("the advice prompt sources the why from the demand and the gap",
      "demand_ahead" in NLU.ADVICE_PROMPT and "gap_to_low_g" in NLU.ADVICE_PROMPT)
check("the advice prompt puts numbers last",
      "Numbers LAST" in NLU.ADVICE_PROMPT)
check("the advice prompt keeps its in_session protection and its no-moralising rule",
      "Never suggest reducing anything marked in_session" in NLU.ADVICE_PROMPT
      and "do not use restriction language" in NLU.ADVICE_PROMPT)

print("\n--- his own figures are law, not a starting point (14 Aug 2026) ---")
# He pasted a complete macro table for a stir-fry and every row was re-searched, re-pricing
# a 980 kcal meal at 2,400. stated_macros' whole contract is pass-through-or-refuse.
_table = {"kcal": 980, "protein_g": 44, "carb_g": 98, "fat_g": 44,
          "components": ["Egg noodles (300g cooked) 380 kcal", "Steak (100g) 220 kcal"]}
_st = NLU.stated_macros(_table)
check("a stated total survives verbatim", _st["kcal"] == 980.0)
check("stated macros survive verbatim",
      (_st["protein_g"], _st["carb_g"], _st["fat_g"]) == (44.0, 98.0, 44.0))
check("his own rows are kept as text, not as lookups", len(_st["components"]) == 2)
check("his own reckoning is an estimate unless he says label",
      _st["basis"] == "estimate"
      and NLU.stated_macros({"kcal": 300, "basis": "label"})["basis"] == "label")
# The model's field names vary; dropping one silently loses HIS data, and there is no
# invention risk in accepting a synonym for a number he supplied.
_alias = NLU.stated_macros({"calories": 500, "protein": 30, "carbs": 60, "fat": 12,
                            "fiber": 5, "sodium_mg": 800})
check("field-name synonyms are mapped rather than dropped",
      _alias["kcal"] == 500.0 and _alias["protein_g"] == 30.0
      and _alias["carb_g"] == 60.0 and _alias["fibre_g"] == 5.0
      and _alias["dietary_sodium_mg"] == 800.0)
check("no energy figure is not a specification, so it goes back on the ladder",
      NLU.stated_macros({"protein_g": 40}) is None
      and NLU.stated_macros({"kcal": 0}) is None
      and NLU.stated_macros(None) is None and NLU.stated_macros("980 kcal") is None)
check("a mis-typed decimal point is refused, never clamped",
      NLU.stated_macros({"kcal": 98000}) is None
      and "protein_g" not in NLU.stated_macros({"kcal": 500, "protein_g": -4}))
# The parse path rebuilds items from an allowlist, so a field it does not name is dropped
# in silence - the same hand-off bug that lost the photo hint and the species score.
_stated_parse = NLU.parse_with_model(
    "large stir-fry bowl, about 980 kcal, 44P 98C 44F", "claude", "m",
    log=lambda *a: None,
    runner=fixed_runner('{"intent":"log_food","items":[{"text":"large stir-fry bowl",'
                        '"portion_g":null,"stated":{"kcal":980,"protein_g":44,'
                        '"carb_g":98,"fat_g":44}}]}'))
check("parse_with_model carries the stated block through to the item",
      (_stated_parse["items"][0].get("stated") or {}).get("kcal") == 980.0)
# A macro table quotes sodium in mg, which tiny_dose_mg reads as a supplement dose - so
# classify would have called a 980 kcal dinner nutritionally negligible.
_stated_cls = NLU.classify(
    "stir fry, 980 kcal, 44P 98C 44F, sodium 800mg", False, "claude", "m",
    log=lambda *a: None,
    runner=fixed_runner('{"intent":"log_food","items":[{"text":"stir fry",'
                        '"stated":{"kcal":980,"protein_g":44}}]}'))
check("a message stating figures stays food and is never called a trivial dose",
      _stated_cls["intent"] == "log_food" and _stated_cls.get("stated") is True
      and not _stated_cls.get("nutritionally_trivial"))
check("the parse prompt carries the pasted-table worked example",
      "A PASTED TABLE IS ONE MEAL, NOT N LOOKUPS" in NLU.PARSE_PROMPT
      and "Large stir-fry bowl ~980 kcal" in NLU.PARSE_PROMPT
      and '"stated":{"kcal":980' in NLU.PARSE_PROMPT)
check("and tells it his headline total beats the sum of his rows",
      "HIS HEADLINE TOTAL WINS" in NLU.PARSE_PROMPT
      and "kcal is 980" in NLU.PARSE_PROMPT)

print("\n--- corrections are decided against the WHOLE pending meal ---")
# decide_correction was shown ONE item: with a four-component meal pending, the caller had
# no rule for which, so it showed the last COMMITTED entry - a brookie - and the model was
# asked what "it was a whole meal" meant about a biscuit.
_batch = [{"resolved_name": "Noodles, egg, dried, raw", "kcal": 169.0,
           "per_100g": {"kcal": 338.0}},
          {"resolved_name": "Beef, rump steak, raw, lean", "kcal": 125.0,
           "per_100g": {"kcal": 125.0}},
          {"resolved_name": "Soy sauce", "kcal": 43.0, "per_100g": {"kcal": 43.0}},
          {"resolved_name": "Vegetables, stir-fried", "kcal": 52.0,
           "per_100g": {"kcal": 52.0}}]
bsink = []
NLU.decide_correction("it was a whole meal", {}, "claude", "m", log=lambda *a: None,
                      runner=fixed_runner('{"kind":"unclear"}', bsink), batch=_batch)
_bp = bsink[0] or ""
check("every component reaches the prompt, numbered",
      '"index": 0' in _bp and '"index": 3' in _bp
      and "Noodles, egg, dried, raw" in _bp and "Vegetables, stir-fried" in _bp)
check("and each one says whether there is a basis to scale it from",
      _bp.count("has_per_100g_basis") == 4)
check("the summaries are compact, not the resolved items",
      "attempts" not in _bp and "source_rung" not in _bp)
check("the prompt explains that an array is a meal awaiting confirmation",
      "A JSON ARRAY is a whole meal awaiting his" in NLU.CORRECTION_PROMPT
      and "valid ONLY when you are shown" in NLU.CORRECTION_PROMPT)

_all = NLU.decide_correction("do all of that x1.5", {}, "claude", "m",
                             log=lambda *a: None,
                             runner=fixed_runner('{"kind":"rescale_all","factor":1.5}'),
                             batch=_batch)
check("rescale_all passes through", _all == {"kind": "rescale_all", "factor": 1.5})
_items = NLU.decide_correction(
    "make the noodles, steak and sauce 1.5x and the vegetables 3x", {}, "claude", "m",
    log=lambda *a: None,
    runner=fixed_runner('{"kind":"rescale_items","items":['
                        '{"index":0,"factor":1.5},{"index":1,"factor":1.5},'
                        '{"index":2,"factor":1.5},{"index":3,"factor":3}]}'),
    batch=_batch)
check("rescale_items passes through per component",
      _items["kind"] == "rescale_items"
      and [s["factor"] for s in _items["items"]] == [1.5, 1.5, 1.5, 3.0])
_mp = NLU.decide_correction(
    "it was a whole meal, work it out", {}, "claude", "m", log=lambda *a: None,
    runner=fixed_runner('{"kind":"meal_portions","items":['
                        '{"index":0,"grams":300},{"index":1,"grams":150},'
                        '{"index":2,"grams":40},{"index":3,"grams":200}]}'),
    batch=_batch)
check("meal_portions passes through as grams per component",
      _mp["kind"] == "meal_portions"
      and [s["grams"] for s in _mp["items"]] == [300.0, 150.0, 40.0, 200.0])

# GRAMS ONLY. The model may size a portion; it may never price one, because a kcal figure
# it supplied would overwrite one scaled from a real basis and read back as sourced data.
for why, reply in (
        ("a component carrying kcal is refused",
         '{"kind":"meal_portions","items":[{"index":0,"grams":300,"kcal":900}]}'),
        ("a component carrying macros is refused",
         '{"kind":"rescale_items","items":[{"index":0,"factor":2,"protein_g":30}]}'),
        ("a decision carrying macros at the top level is refused",
         '{"kind":"rescale_all","factor":1.5,"kcal":1400}')):
    check(why, NLU.decide_correction("x", {}, "claude", "m", log=lambda *a: None,
                                     runner=fixed_runner(reply),
                                     batch=_batch) == {"kind": "unclear"})
# An index the model was not shown means it was not reading the batch, so nothing it said
# about that batch is usable - and a PARTIAL application would leave him confirming a total
# that is wrong in a way he cannot see.
for why, reply in (
        ("an out-of-range index refuses the whole decision",
         '{"kind":"rescale_items","items":[{"index":0,"factor":2},{"index":9,"factor":2}]}'),
        ("an index with neither grams nor factor refuses it",
         '{"kind":"rescale_items","items":[{"index":0}]}'),
        ("meal_portions with a ratio instead of grams refuses it",
         '{"kind":"meal_portions","items":[{"index":0,"factor":3}]}'),
        ("an implausible factor refuses it",
         '{"kind":"rescale_all","factor":400}'),
        ("an implausible portion refuses it",
         '{"kind":"meal_portions","items":[{"index":0,"grams":9000}]}')):
    check(why, NLU.decide_correction("x", {}, "claude", "m", log=lambda *a: None,
                                     runner=fixed_runner(reply),
                                     batch=_batch) == {"kind": "unclear"})
# UNVALIDATED IS NOT UNAVAILABLE. None means "the model could not be reached" and sends the
# caller to its regex fallback, which for "noodles 1.5x, veg 3x" applies one wrong number
# to one wrong item. A batch kind with no batch has to say `unclear` instead.
check("a batch decision with nothing pending degrades to unclear, not to None",
      NLU.decide_correction("x", {}, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner('{"kind":"rescale_all","factor":1.5}'))
      == {"kind": "unclear"})
check("the single-item kinds are unaffected by the batch argument",
      NLU.decide_correction("half of it", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner('{"kind":"rescale_factor","factor":0.5}'))
      == {"kind": "rescale_factor", "factor": 0.5})
check("the prompt names grams as the only quantity the model may estimate",
      "THIS IS THE ONE PLACE YOU MAY ESTIMATE A QUANTITY" in NLU.CORRECTION_PROMPT
      and "GRAMS ONLY" in NLU.CORRECTION_PROMPT)

print("\n--- a meal he cooked is costed whole, by one capable model (14 Aug 2026) ---")
# Jamie: "I literally went on a generic Opus 5 and told it what I ate and it gave me that
# table... we have access to any Claude model and we can't do shit". The composition tables
# hold ingredients, not dinners, so a described meal is costed in ONE call and the ladder
# never runs on it.
MEAL_TABLE = """{"meal_name":"Large beef stir-fry with egg noodles",
 "components":[
  {"name":"egg noodles, cooked","portion_g":300,"portion_basis":"a large bowl",
   "kcal":420,"protein_g":14,"carb_g":80,"fat_g":4,"fibre_g":4},
  {"name":"rump steak, grilled","portion_g":120,"portion_basis":"a small steak",
   "kcal":210,"protein_g":37,"carb_g":0,"fat_g":7,"fibre_g":0},
  {"name":"soy, ginger and garlic sauce","portion_g":45,"portion_basis":"2 tbsp",
   "kcal":60,"protein_g":2,"carb_g":10,"fat_g":1,"fibre_g":0},
  {"name":"stir-fried mixed vegetables","portion_g":200,"portion_basis":"a handful",
   "kcal":110,"protein_g":4,"carb_g":12,"fat_g":5,"fibre_g":5},
  {"name":"vegetable oil for the pan","portion_g":15,"portion_basis":"1 tbsp",
   "kcal":135,"protein_g":0,"carb_g":0,"fat_g":15,"fibre_g":0}],
 "total":{"kcal":935,"protein_g":57,"carb_g":102,"fat_g":32,"fibre_g":9},
 "error_band_pct":18,
 "plants":["wheat","garlic","ginger","soya","onion","red pepper","broccoli"],
 "assumptions":["Large bowl taken as 300g cooked noodles","1 tbsp oil in the pan"]}"""

msink = []
meal = NLU.describe_meal(
    "a large stir fry with egg noodles, a small steak, soy ginger garlic sauce and veg",
    "claude", "claude-opus-5", log=lambda *a: None,
    runner=fixed_runner(MEAL_TABLE, msink))
check("the whole meal comes back as one table", meal and len(meal["components"]) == 5)
check("each component carries a portion and its own figures",
      [c["portion_g"] for c in meal["components"]] == [300.0, 120.0, 45.0, 200.0, 15.0]
      and all(c.get("kcal") for c in meal["components"]))
# THE ARITHMETIC IS STILL THE CODE'S. A model's addition is not a source of truth, and the
# components are what a correction is applied to, so the entry total has to be their sum.
check("the total is the SUM of the components, computed here",
      meal["total"]["kcal"] == 935.0
      and meal["total"]["kcal"] == sum(c["kcal"] for c in meal["components"]))
check("and the macros total the same way",
      (meal["total"]["protein_g"], meal["total"]["carb_g"], meal["total"]["fat_g"])
      == (57.0, 102.0, 32.0))
check("the model's own error band survives", meal["error_band_pct"] == 18)
check("the plants it named are carried for the diversity count",
      "ginger" in meal["plants"] and "garlic" in meal["plants"])
check("and every assumption is carried, because that is what he corrects",
      len(meal["assumptions"]) == 2
      and "300g cooked noodles" in meal["assumptions"][0])
_mp = msink[0] or ""
check("the prompt asks for the food AS EATEN",
      "COOK THE FOOD" in _mp and "Dried noodles are" in _mp)
check("and for the size to come from his words",
      "SIZE IT FROM HIS WORDS" in _mp and "300 g cooked" in _mp)
check("and for what cooking adds, without padding the meal",
      "INCLUDE WHAT COOKING ADDS" in _mp and "DO NOT PAD THE MEAL" in _mp)
check("and for the assumptions and an honest band",
      "SAY WHAT YOU ASSUMED" in _mp and "+/-15-20%" in _mp)
check("and it is his own description that is sent, not a cleaned-up query",
      "a large stir fry with egg noodles, a small steak" in _mp)

# A table whose own total is far from the sum of its rows is REFUSED, not reconciled: it is
# not a table any single line of which can be corrected.
check("an internally inconsistent table is refused",
      NLU.describe_meal("x", "claude", "m", log=lambda *a: None,
                        runner=fixed_runner(
                            '{"meal_name":"x","components":[{"name":"rice","kcal":200}],'
                            '"total":{"kcal":900}}')) is None)
check("a table with no usable component is refused",
      NLU.describe_meal("x", "claude", "m", log=lambda *a: None,
                        runner=fixed_runner('{"meal_name":"x","components":[]}')) is None
      and NLU.describe_meal("x", "claude", "m", log=lambda *a: None,
                            runner=fixed_runner(
                                '{"components":[{"name":"rice"}]}')) is None)
check("prose instead of a table is refused",
      NLU.describe_meal("x", "claude", "m", log=lambda *a: None,
                        runner=fixed_runner("That sounds like about 900 calories."))
      is None)
check("an outage is refused rather than logged as a meal",
      NLU.describe_meal("x", "claude", "m", log=lambda *a: None,
                        runner=fixed_runner("API Error: 401 OAuth token has expired"))
      is None)
check("an absurd figure is dropped rather than believed",
      NLU.describe_meal("x", "claude", "m", log=lambda *a: None,
                        runner=fixed_runner(
                            '{"meal_name":"x","components":[{"name":"rice","kcal":90000},'
                            '{"name":"peas","kcal":80}]}'))["total"]["kcal"] == 80.0)
# A band of zero is a claim of precision this rung does not have.
check("the error band has a floor and a ceiling",
      NLU.describe_meal("x", "claude", "m", log=lambda *a: None,
                        runner=fixed_runner(
                            '{"meal_name":"x","error_band_pct":0,'
                            '"components":[{"name":"rice","kcal":200}]}')
                        )["error_band_pct"] == 10
      and NLU.describe_meal("x", "claude", "m", log=lambda *a: None,
                            runner=fixed_runner(
                                '{"meal_name":"x","error_band_pct":90,'
                                '"components":[{"name":"rice","kcal":200}]}')
                            )["error_band_pct"] == 40)

# THE ROUTING. A meal nobody published figures for goes to the model; everything with a real
# published figure keeps the deterministic path, where the ladder genuinely wins.
check("the parse prompt asks which messages are composed meals",
      "A COMPOSED MEAL IS ONE MEAL, AND NO DATABASE KNOWS IT" in NLU.PARSE_PROMPT
      and "composed_meal: (log_food only)" in NLU.PARSE_PROMPT)
check("and lists what is NOT one, so branded and single foods keep the ladder",
      "composed_meal is FALSE for" in NLU.PARSE_PROMPT
      and "a branded or packaged product" in NLU.PARSE_PROMPT
      and "a single whole food" in NLU.PARSE_PROMPT
      and "a restaurant or takeaway order" in NLU.PARSE_PROMPT)
check("and says the whole meal goes in ONE item, in his own words",
      "put the WHOLE meal in ONE item's text" in NLU.PARSE_PROMPT)
_composed = NLU.classify(
    "a large stir fry with egg noodles, a small steak, soy ginger garlic and veg",
    False, "claude", "m", log=lambda *a: None,
    runner=fixed_runner('{"intent":"log_food","composed_meal":true,'
                        '"items":[{"text":"large stir fry with egg noodles, steak, '
                        'soy ginger garlic sauce and veg"}]}'))
check("classify carries the composed-meal flag out to the caller",
      _composed.get("composed_meal") is True and _composed["intent"] == "log_food")
check("and a branded product does not carry it",
      not NLU.classify("a nakd bar", False, "claude", "m", log=lambda *a: None,
                       runner=fixed_runner('{"intent":"log_food","composed_meal":false,'
                                           '"items":[{"text":"nakd bar"}]}')
                       ).get("composed_meal"))

print("\n--- a described meal is planned in the state he ate it (14 Aug 2026) ---")
# The FALLBACK path, for when the meal model cannot be reached: still cooked states and
# as-eaten portions, which is a poor second to a costed table and far better than refusing
# to log his dinner.
# Four components came back at per-100g with no portion and from the RAW and DRIED rows -
# dried noodles, raw steak - and were offered as 447 kcal for a ~980 kcal dinner.
check("the interpret prompt demands the as-eaten state",
      "SEARCH FOR THE STATE HE ATE IT IN" in NLU.INTERPRET_PROMPT
      and '"egg noodles, cooked"' in NLU.INTERPRET_PROMPT
      and "THE STATE HE ATE IT IN IS THE ONLY STATE THAT EVER BELONGS IN A FOOD LOG"
      in NLU.INTERPRET_PROMPT)
check("and a portion on every component, scaled to the size he described",
      "GIVE EVERY COMPONENT A PORTION" in NLU.INTERPRET_PROMPT
      and "portion_estimated" in NLU.INTERPRET_PROMPT
      and "noodles/pasta/rice 300 g cooked" in NLU.INTERPRET_PROMPT)
check("with oil kept to what a stir-fry actually uses",
      "never a 100 g portion of oil" in NLU.INTERPRET_PROMPT)
_plan = NLU.interpret(
    "a large stir fry with egg noodles, a small steak, soy ginger garlic sauce and veg",
    "claude", "m", log=lambda *a: None,
    runner=fixed_runner('{"items":[{"canonical_name":"egg noodles, cooked",'
                        '"search_terms":["egg noodles, cooked"],"portion_g":300,'
                        '"portion_estimated":true},'
                        '{"canonical_name":"rump steak, grilled",'
                        '"search_terms":["rump steak, grilled"],"portion_g":100,'
                        '"portion_estimated":true}]}'))
check("interpret carries the estimated-portion flag through its item rebuild",
      _plan["items"][0]["portion_g"] == 300
      and _plan["items"][0]["portion_estimated"] is True
      and _plan["items"][1]["portion_estimated"] is True)
check("and a portion the athlete stated is not flagged as a guess",
      NLU.interpret("300g of egg noodles", "claude", "m", log=lambda *a: None,
                    runner=fixed_runner('{"items":[{"canonical_name":"egg noodles, '
                                        'cooked","search_terms":["egg noodles"],'
                                        '"portion_g":300}]}')
                    )["items"][0]["portion_estimated"] is False)

print("\n--- a photographed label can be a CORRECTION, not a second dinner (14 Aug 2026) ---")
# He logged "Coop Chianti beef pizza" by name at a web figure of 1,147 kcal, then sent the
# pack's label to fix it. Every label was offered as a NEW item, so the pizza went in twice.
_CANDS = [{"entry_id": "2026-08-14-003", "name": "Coop Chianti beef pizza", "kcal": 1147.0,
           "figures_from": "web", "state": "logged"},
          {"entry_id": "2026-08-14-002", "name": "Porridge with blueberries", "kcal": 320.0,
           "figures_from": "cofid", "state": "logged"}]
_LABEL = {"resolved_name": "Chianti beef pizza, stone baked", "kcal": 482.0,
          "protein_g": 22.0, "per_100g": {"kcal": 241.0}, "portion_used_g": 200.0,
          "pack_g": 400.0}
sink = []
got = NLU.decide_label_target(_LABEL, _CANDS, "claude", "m", log=lambda *a: None,
                              runner=fixed_runner(
                                  '{"kind":"replace","entry_id":"2026-08-14-003"}', sink))
check("a label that matches a logged item is a replacement",
      got == {"kind": "replace", "entry_id": "2026-08-14-003"})
check("the prompt shows the label and every candidate it may point at",
      "Chianti beef pizza, stone baked" in (sink[0] or "")
      and "2026-08-14-003" in (sink[0] or "") and "Porridge" in (sink[0] or ""))
check("and says which state each candidate is in, written or awaiting confirmation",
      "awaiting his confirmation" in NLU.LABEL_TARGET_PROMPT
      or "state" in (sink[0] or ""))
check("a label matching nothing is a new item",
      NLU.decide_label_target(_LABEL, _CANDS, "claude", "m", log=lambda *a: None,
                              runner=fixed_runner('{"kind":"new"}')) == {"kind": "new"})
check("an entry_id it was never shown is refused, and becomes a new item",
      NLU.decide_label_target(_LABEL, _CANDS, "claude", "m", log=lambda *a: None,
                              runner=fixed_runner('{"kind":"replace",'
                                                  '"entry_id":"2026-08-14-099"}'))
      == {"kind": "new"})
check("a pending item is a valid target, because he photographs the pack mid-offer",
      NLU.decide_label_target(
          _LABEL, [{"entry_id": "pending:0", "name": "Chianti beef pizza",
                    "state": "awaiting his confirmation, nothing written yet"}],
          "claude", "m", log=lambda *a: None,
          runner=fixed_runner('{"kind":"replace","entry_id":"pending:0"}'))
      == {"kind": "replace", "entry_id": "pending:0"})
check("nothing on the log means nothing to correct, with no model call at all",
      NLU.decide_label_target(_LABEL, [], "claude", "m", log=lambda *a: None,
                              runner=fixed_runner("boom")) == {"kind": "new"})
# None, not `new`: the caller distinguishes "the model says this is new" from "there was no
# model", and both end up offering the label as a new item - which is what happened before
# this function existed, so an outage costs a correction rather than a wrong write.
check("an unreachable model is None, so the caller knows it never decided",
      NLU.decide_label_target(_LABEL, _CANDS, "claude", "m", log=lambda *a: None,
                              runner=fixed_runner(
                                  "API Error: 401 OAuth token has expired")) is None)
check("and prose instead of JSON is None as well",
      NLU.decide_label_target(_LABEL, _CANDS, "claude", "m", log=lambda *a: None,
                              runner=fixed_runner("looks like the pizza to me")) is None)
_lt = " ".join(NLU.LABEL_TARGET_PROMPT.split())   # unwrapped: a line break in the prompt
#                                                  must not fail a check about its wording
check("the prompt refuses a guessed replacement and asks for no figures",
      "never guess `replace` to be helpful" in _lt
      and "Return no figures of any kind" in _lt)

print("\n--- delete_duplicate: 'you've added the pizza twice' (15:25, 14 Aug 2026) ---")
# There was no verb for this, so the decision came back `unclear`, fell into a
# re-resolution, and the reply claimed a removal that never happened.
got = NLU.decide_correction("you've added the pizza twice", _item, "claude", "m",
                            log=lambda *a: None,
                            runner=fixed_runner('{"kind":"delete_duplicate",'
                                                '"which":"the pizza"}'))
check("a duplicate complaint has a verb of its own",
      got == {"kind": "delete_duplicate", "which": "the pizza"})
check("with no words for it, `which` is empty rather than invented",
      NLU.decide_correction("that's in there twice", _item, "claude", "m",
                            log=lambda *a: None,
                            runner=fixed_runner('{"kind":"delete_duplicate"}'))
      == {"kind": "delete_duplicate", "which": ""})
# None here would be read as "the model is down" and would run the quantity regexes against
# the message, which find nothing - and the reply falls through to a fresh offer.
check("a malformed delete_duplicate is never None",
      NLU.decide_correction("x", _item, "claude", "m", log=lambda *a: None,
                            runner=fixed_runner('{"kind":"delete_duplicate",'
                                                '"which":null}')) is not None)
check("the prompt separates a duplicate from a second helping",
      "same food is logged more than once" in NLU.CORRECTION_PROMPT.lower()
      and "I had another one" in NLU.CORRECTION_PROMPT)

print("\n--- the chat model may not claim to have changed the log ---")
# The 15:25 sentence was prose, and prose cannot know what the code did. Corrections,
# deletions and replacements report themselves from the store's own result.
_conv = NLU.CONVERSE_PROMPT
check("converse is told plainly that it cannot change the log",
      "YOU CANNOT CHANGE HIS LOG FROM HERE" in _conv
      and "duplicate noted and removed" in _conv)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED)); sys.exit(1)
print("all checks passed")
