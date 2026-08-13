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
