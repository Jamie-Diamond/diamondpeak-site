#!/usr/bin/env python3
"""Offline tests for lib/nutrition_gate.py. No network, no model, no Telegram.
Run: python3 ClaudeCoach/scripts/test_nutrition_gate.py

What is actually at risk in a verifier is not whether it can say "block". It is that a
blocked reply becomes silence, that a broken verifier takes the bot down with it, or that
the verifier turns into a second route for invented macros. Those three are what is tested
here; the wiring is tested in test_nutrition_bot.py.
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

import nutrition_gate as NG      # noqa: E402
import nutrition_nlu as NLU      # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


def runner_saying(reply, capture=None):
    """A stub CLI that returns `reply`, recording the prompt it was handed."""
    def run(cmd, input=None, **kwargs):
        if capture is not None:
            capture.append({"cmd": cmd, "input": input, "kwargs": kwargs})
        return type("P", (), {"stdout": reply, "stderr": ""})()
    return run


def quiet(*a, **k):
    pass


CTX = {"reply_kind": "offer",
       "figures_behind_this_reply": [{"name": "large stir fry", "kcal": 447}]}

print("--- a block replaces the reply and says why ---")
seen = []
got = NG.verify_reply(
    "large stir fry with steak and noodles",
    "*Large stir fry* 447 kcal. Log it?", CTX, "claude", log=quiet,
    runner=runner_saying(
        '{"verdict":"block","reason_class":"magnitude",'
        '"reason":"447 kcal is far too low for that meal",'
        '"fallback":"I could not produce a sensible answer for that - tell me the '
        'portions and I will redo it."}', seen))
check("a block is reported as a block", got["verdict"] == "block")
check("with its class and reason kept for the log",
      got["reason_class"] == "magnitude" and "too low" in got["reason"])
check("and a fallback for the bot to send instead",
      got["fallback"].startswith("I could not produce"))
check("every decision carries the milliseconds it cost", isinstance(got["ms"], int))

# The prompt is the whole contract with the model, so what is IN it is asserted rather
# than assumed: a gate shown the reply but not the message cannot judge coherence, and one
# shown neither the numbers nor the offer cannot judge magnitude.
prompt = seen[0]["input"]
check("the prompt carries what he actually said",
      "large stir fry with steak and noodles" in prompt)
check("and the exact text about to be sent", "447 kcal. Log it?" in prompt)
check("and the figures behind it", '"kcal": 447' in prompt)
check("and it asks for a verdict, not a rewrite",
      '"verdict"' in prompt and "never supply or correct a figure" in prompt.lower())
check("the gate runs on the best model available, not the fast one",
      "claude-opus-5" in seen[0]["cmd"])
check("with no tools, so it cannot go looking anything up",
      "--allowedTools" not in seen[0]["cmd"])
check("and a timeout, so a hung verifier cannot hold the reply",
      seen[0]["kwargs"]["timeout"] == NG.GATE_TIMEOUT_S)

print("\n--- a send passes the original through untouched ---")
got = NG.verify_reply("porridge and a flat white", "*Porridge* 250 kcal. Log it?", CTX,
                      "claude", log=quiet,
                      runner=runner_saying('{"verdict":"send","reason":"fine"}'))
check("a send is a send", got["verdict"] == "send")
check("and never carries a fallback for the caller to prefer", got["fallback"] is None)

print("\n--- fail open: the bot must never go mute because the gate is down ---")


def boom(*a, **k):
    raise FileNotFoundError("no claude binary")


got = NG.verify_reply("x", "y", CTX, "claude", log=quiet, runner=boom)
check("an unreachable gate sends", got["verdict"] == "send" and got["unverified"])
check("and says why in the reason", "unreachable" in got["reason"])


def timeout(*a, **k):
    raise subprocess.TimeoutExpired(cmd="claude", timeout=NG.GATE_TIMEOUT_S)


got = NG.verify_reply("x", "y", CTX, "claude", log=quiet, runner=timeout)
check("a timed-out gate sends", got["verdict"] == "send" and got["unverified"])

for bad, why in (("no json here at all", "prose"),
                 ("{not json}", "unparseable JSON"),
                 ('{"verdict":"maybe"}', "a verdict that is not one of the two"),
                 ('["block"]', "a JSON array"),
                 ("", "an empty reply")):
    got = NG.verify_reply("x", "y", CTX, "claude", log=quiet,
                          runner=runner_saying(bad))
    check(f"{why} fails open",
          got["verdict"] == "send" and got.get("unverified") is True)

got = NG.verify_reply("x", "y", CTX, "claude", log=quiet,
                      runner=runner_saying("Invalid API key · 401 authentication_error"),
                      model_unavailable=NLU.model_unavailable)
check("an auth failure is reported as unavailable rather than as a bad reply",
      got["verdict"] == "send" and got["unverified"]
      and "unavailable" in got["reason"])

print("\n--- the gate judges; it never supplies a figure ---")
# The whole reason this is a verdict-and-reason contract rather than a rewrite: a verifier
# allowed to edit the text would be a fresh back door for estimated macros, which is the one
# thing every layer of this system refuses.
got = NG.verify_reply(
    "large stir fry", "*Stir fry* 447 kcal. Log it?", CTX, "claude", log=quiet,
    runner=runner_saying(
        '{"verdict":"block","reason_class":"magnitude","reason":"too low",'
        '"corrected_reply":"*Stir fry* 980 kcal. Log it?",'
        '"kcal":980,"protein_g":44,'
        '"fallback":"That should be about 980 kcal - tell me the portions."}'))
check("a corrected reply is not even read", "corrected_reply" not in got)
check("nor are macros it tried to hand back",
      "kcal" not in got and "protein_g" not in got)
check("a fallback carrying a figure is dropped rather than sent", got["fallback"] is None)
check("so the caller falls back to an honest built line with no numbers in it",
      NG.built_fallback(got["reason_class"]).startswith("I could not produce")
      and not NG.fallback_invents_figures(NG.built_fallback(got["reason_class"])))
check("every built line is figure-free",
      not any(NG.fallback_invents_figures(v) for v in NG.FALLBACK_BY_CLASS.values()))
for bad in ("that should be about 980 kcal", "roughly 44g protein", "try 300 g of rice",
            "protein is only 12 g", "about 2,400 calories"):
    check(f"a figure-shaped fallback is caught: {bad!r}",
          NG.fallback_invents_figures(bad))
for fine in ("tell me the portions and I will redo it",
             "I could not produce a sensible answer for that",
             "say it again and I will answer the actual question"):
    check(f"a plain fallback is allowed: {fine!r}",
          not NG.fallback_invents_figures(fine))

print("\n--- a fallback on a send, and an unknown class, degrade rather than leak ---")
got = NG.verify_reply("x", "y", CTX, "claude", log=quiet,
                      runner=runner_saying(
                          '{"verdict":"send","fallback":"send this instead"}'))
check("a fallback attached to a send is discarded", got["fallback"] is None)
got = NG.verify_reply("x", "y", CTX, "claude", log=quiet,
                      runner=runner_saying(
                          '{"verdict":"block","reason_class":"vibes","reason":"no"}'))
check("an unrecognised reason class becomes 'other', not a crash",
      got["reason_class"] == "other" and got["verdict"] == "block")
check("and a block with no reason still logs something",
      NG.verify_reply("x", "y", CTX, "claude", log=quiet,
                      runner=runner_saying('{"verdict":"block"}'))["reason"]
      == "no reason given")

print("\n--- the context block is capped, because the gate is on every reply ---")
seen = []
NG.verify_reply("x", "y", {"junk": ["z" * 200] * 200}, "claude", log=quiet,
                runner=runner_saying('{"verdict":"send"}', seen))
# MEASURED AGAINST THE PROMPT ITSELF, not against a constant that happened to be bigger
# than it. The slack used to be a flat 4,000 characters, so adding a paragraph to
# GATE_PROMPT failed a check about trimming the CONTEXT - which is a false alarm about the
# wrong thing, and the kind that gets a real assertion deleted.
check("an oversized context is trimmed rather than sent whole",
      len(seen[0]["input"]) < len(NG.GATE_PROMPT) + NG.GATE_CONTEXT_CHARS + 400
      and "context trimmed" in seen[0]["input"])
seen = []
NG.verify_reply("x", "y", {"unserialisable": object()}, "claude", log=quiet,
                runner=runner_saying('{"verdict":"send"}', seen))
check("and a context that will not serialise does not take the reply down with it",
      len(seen) == 1)

print("\n--- the prompt biases to send, or the gate gets turned off ---")
# The realistic failure of an LLM judge is not missing the stir fry, it is objecting to a
# terse-but-correct confirmation until every real answer is replaced by an apology.
low = " ".join(NG.GATE_PROMPT.lower().split())      # unwrapped, so a line break in the
#                                                    prompt cannot fail a check about it
check("it names the five blocking classes and nothing else",
      all(c in low for c in ("magnitude", "off_topic", "contradicts_input",
                             "stale_context", "false_claim")))
check("it says plainly not to block for style or brevity",
      "do not block for" in low and "brevity" in low and "tone" in low)
check("and that a marginal case is a send", "marginal case is a send" in low)
# A barcode photo or a tapped button names no food, so "does it address what he said" has
# no answer and off_topic would be available on every photo-derived offer. It is judged on
# its FIGURES and its ACTION CLAIMS: a photographed label that corrects an entry is one of
# these turns, so exempting them from false_claim would exempt the path that most needs it.
check("a marker message is judged on its figures and its claims",
      "when his message is a marker" in low
      and "magnitude or false_claim" in low)

print("\n--- REPLAY 15:25, 14 Aug 2026: a reply that claimed a removal that never happened ---")
# "You've added the pizza twice" produced "duplicate noted and removed" while both copies sat
# in the log. Every figure in it was plausible, which was all this gate was judging - so it
# passed, and the store had to be deduped by hand. The fix is not a better reading of the
# sentence: it is the LIST of what the code actually did, beside the reply.
check("the prompt explains what an empty action list means",
      "actions_this_turn" in low and "empty list means the log was not touched" in low)
check("and says to block a claim the list does not contain, not one it fails to prove",
      "claims to have done something to his log that is absent" in low
      and "block when the reply claims an action the list does not contain" in low)
check("with the reported sentence named as the example",
      "duplicate noted and removed" in low)
seen = []
INCIDENT_CTX = {"reply_kind": "correction", "actions_this_turn": [],
                "figures_behind_this_reply": [{"name": "Chianti beef pizza", "kcal": 964}]}
got = NG.verify_reply(
    "you've added the pizza twice",
    "Duplicate noted and removed. You are on 3,050 kcal for the day.", INCIDENT_CTX,
    "claude", log=quiet,
    runner=runner_saying('{"verdict":"block","reason_class":"false_claim",'
                         '"reason":"claims a removal that is not in actions_this_turn",'
                         '"fallback":null}', seen))
check("the gate is shown that nothing was done", '"actions_this_turn": []' in seen[0]["input"])
check("and the claim it has to judge", "Duplicate noted and removed" in seen[0]["input"])
check("a false claim is a first-class block, not coerced into 'other'",
      got["verdict"] == "block" and got["reason_class"] == "false_claim")
check("and it has an honest line of its own to send instead",
      "false_claim" in NG.FALLBACK_BY_CLASS
      and "nothing has been changed" in NG.built_fallback("false_claim"))
# The other half of the same rule: a confirmation of work that DID happen must go out, or
# the athlete is apologised to for a correction the bot made correctly.
seen = []
got = NG.verify_reply(
    "you've added the pizza twice",
    "Removed the duplicate *Chianti beef pizza* (1,147 kcal) and kept the label figures.",
    {"reply_kind": "correction",
     "actions_this_turn": ["removed entry 2026-08-14-004 Chianti beef pizza (1147 kcal) "
                           "as a duplicate"]},
    "claude", log=quiet, runner=runner_saying('{"verdict":"send","reason":"fine"}', seen))
check("a claim the list substantiates is sent", got["verdict"] == "send")
check("and the gate could see the action it names",
      "removed entry 2026-08-14-004" in seen[0]["input"])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
