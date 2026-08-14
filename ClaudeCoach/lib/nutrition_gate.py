#!/usr/bin/env python3
"""nutrition_gate.py - the pre-send gate. Nothing leaves the nutrition bot until the
best model available has read it against what the athlete actually said.

Jamie's instruction, 14 Aug 2026: "everything should be verified by opus to make sure the
output is sensible against the input and makes sense as a reply and isn't just crap."

WHAT IT IS FOR, FROM THE LOGS RATHER THAN FROM THEORY. Four failures in one evening, each
of which every deterministic guard in this codebase passed:
  - "a large stir fry with steak" offered at 447 kcal, because the ladder had priced raw
    100 g parts. Every figure was sourced; the answer was absurd.
  - a reply proposing 2,400 kcal for a meal he had himself pasted at 980.
  - an answer about a brookie to a message about a stir-fry: a correction decided against
    the wrong target, and the reply reads perfectly if you cannot see the input.
  - "Fair, I'll stop asking", followed by advice about a run that had happened two days
    earlier.
None of those is a bad number in isolation. Each is a reply that does not FIT its input,
which is a judgement no amount of range-checking makes, and it is the last thing a person
can be asked to do for a bot that is meant to save him effort.

THE LINE, AND IT IS THE SAME LINE THE REST OF THIS SYSTEM DRAWS. The gate judges
PLAUSIBILITY and COHERENCE only. It never rewrites a stored figure, never supplies a macro
and never hands back a corrected reply: the only things read off its answer are a verdict,
a reason and - on a block - a short honest sentence with no figures in it. A verifier
allowed to edit the text would be a fresh back door for estimated numbers wearing a
confident sentence, which is precisely what nutrition_nlu exists to keep shut.

FAIL OPEN, ALWAYS. An unreachable or unparseable verifier sends the original and says so in
the log. The bot going mute because a second model call failed would be a worse bug than
any it catches: he is standing in a kitchen with his dinner going cold.

BIAS TO SEND. The realistic failure of an LLM judge is not missing the 447 kcal stir-fry,
it is objecting to a terse-but-correct confirmation until the whole gate gets turned off.
The prompt therefore names the four classes worth blocking and says, in as many words, not
to block for tone, brevity or a missing detail.
"""

import json
import re
import subprocess
import time

# One constant, because it is the knob that will need turning. UNVALIDATED against real
# Opus-via-CLI latency: the ms on every gate log line is the instrument for setting it, and
# a cap set too tight is the bad outcome - all of the latency and none of the benefit, with
# only the log to say so.
GATE_TIMEOUT_S = 20

# The whole context block is trimmed to this before it is sent. A gate call is on the path
# of every reply, so its input has to stay small: the day's facts dict is thousands of
# tokens and none of it helps decide whether a sentence answers a question.
GATE_CONTEXT_CHARS = 4000

# A closed set, so the log is greppable and the built fallback has something to name. The
# four are the catalogued failures above; `other` exists so an honest block is never forced
# into the wrong bucket.
REASON_CLASSES = ("magnitude", "off_topic", "contradicts_input", "stale_context", "other")

# What to say when the gate blocked something and gave no usable fallback of its own. Per
# class, because "I could not produce a sensible answer" with no hint of why is the kind of
# apology that teaches him to ignore the bot.
FALLBACK_BY_CLASS = {
    "magnitude": ("I could not produce a sensible answer for that - the figures I came up "
                  "with do not look right for what you described. Tell me the rough kcal, "
                  "or the portions, and I will use yours."),
    "off_topic": ("I could not produce a sensible answer for that - what I had drifted onto "
                  "something else. Say it again and I will answer the actual question."),
    "contradicts_input": ("I could not produce a sensible answer for that - what I had "
                          "contradicted the figures you gave me. Your numbers win; send "
                          "them again and I will log them as they are."),
    "stale_context": ("I could not produce a sensible answer for that - I was answering an "
                      "older part of the conversation. Ask me again and I will answer on "
                      "today."),
    "other": ("I could not produce a sensible answer for that. Tell me again and I will "
              "have another go."),
}

GATE_PROMPT = """You are the pre-send check on a personal nutrition bot's outgoing \
message. You are the last thing between a proposed reply and a real person's phone.

ONE QUESTION: is the proposed reply a sensible, coherent, plausible thing to send THIS
person in answer to THIS message? Right order of magnitude, addresses what he actually
said, consistent with the figures in front of him, and not a non-sequitur.

WHAT HE SAID:
%s

CONTEXT (recent conversation with timestamps, the offer on the table and the figures
behind the proposed reply, where there are any). The timestamps are real: an exchange
hours or days old is stale background, not a live thread:
%s

THE EXACT TEXT ABOUT TO BE SENT:
%s

BLOCK it only for one of these, which are the failures this check exists for:
  - magnitude: a figure that is not a plausible order of magnitude for what he described.
    A large stir fry with steak and noodles offered at 447 kcal. A single banana at
    900 kcal. A day total that could not have come from the food listed.
  - off_topic: the reply answers something else. He asked about a stir-fry and the reply
    is about a brookie. He asked how much protein he has had and the reply discusses
    tomorrow's ride instead.
  - contradicts_input: the reply restates or re-prices figures HE supplied in his own
    message. He pasted a meal at 980 kcal and the reply says 2,400.
  - stale_context: the reply treats a finished or old event as live - advice about a run
    that happened two days ago, or asking again for something he has already answered or
    just told you to stop asking about.

SEND it otherwise. Bias hard to send. In particular do NOT block for:
  - tone, style, brevity, phrasing, formatting, markdown or house voice
  - being terse, or not explaining itself, or lacking a macro breakdown
  - a figure you would have estimated differently but which is the right magnitude
  - an estimate honestly labelled as an estimate, or a message that says plainly that
    something could not be looked up
  - asking him ONE thing he has not already answered
A blocked reply costs him a real answer and replaces it with an apology, so a marginal
case is a send.

WHEN HIS MESSAGE IS A MARKER rather than words - "[sent a photo of a barcode]", "[tapped
Log it]" - he named nothing, so there is nothing for the reply to be off-topic about or to
contradict. Judge the FIGURES only: block such a reply for magnitude alone, and send it
otherwise.

YOU NEVER SUPPLY OR CORRECT A FIGURE. Do not rewrite the message, do not offer better
macros, do not restate his totals. Your entire output is the JSON below.

Reply with ONLY this JSON object, no prose:
  {"verdict":"send"|"block",
   "reason_class":"magnitude"|"off_topic"|"contradicts_input"|"stale_context"|"other",
   "reason":"<one short sentence, for the log>",
   "fallback":"<optional. On a block only: a short honest sentence to send him INSTEAD,
                naming what is needed from him. NO NUMBERS OF ANY KIND in it.>"}
On a send, reason_class is "other" and fallback is null.
"""


# A figure in a fallback is the one thing that would let the gate write nutrition data into
# a message. Shape-based rather than a blanket digit ban: a blanket ban is the rule somebody
# later loosens to allow a clock time, and loosening it re-opens the hole.
_FIGURE_SHAPE = re.compile(
    r"\d[\d.,]*\s*(?:kcal|cals?|calories|kj|kgs?|g|mg|ml|p\b|c\b|f\b)\b"
    r"|\b(?:kcal|calories|protein|carb\w*|fat|fibre|fiber|sodium|salt)\b[^.]{0,14}\d",
    re.I)


def fallback_invents_figures(text: str) -> bool:
    """True when a proposed fallback carries a nutrition-shaped number.

    The gate is allowed to say "tell me the portions"; it is not allowed to say "that
    should be about 900 kcal". The second is a macro figure from a model that was handed
    no source, and in the chat it would be indistinguishable from a resolved one."""
    return bool(_FIGURE_SHAPE.search(text or ""))


def _clean_verdict(got: dict, log) -> dict:
    """verdict/reason_class/reason/fallback, and nothing else.

    An ALLOWLIST, in the same spirit as parse_with_model's item rebuild: a key the model
    returns and this function does not copy is dropped in silence. That is what stops a
    `corrected_reply` or a `kcal` from ever reaching the send path - not a rule about them,
    but the absence of any code that would read them."""
    verdict = str(got.get("verdict") or "").strip().lower()
    if verdict not in ("send", "block"):
        log(f"[gate] unusable verdict {got.get('verdict')!r}")
        return None
    cls = str(got.get("reason_class") or "").strip().lower()
    if cls not in REASON_CLASSES:
        cls = "other"
    reason = str(got.get("reason") or "").strip()[:300]
    fallback = str(got.get("fallback") or "").strip()[:400] or None
    if verdict == "send":
        # A fallback on a send is meaningless and would be a live wire for a later edit.
        return {"verdict": "send", "reason_class": "other", "reason": reason,
                "fallback": None}
    if fallback and fallback_invents_figures(fallback):
        log(f"[gate] dropped a fallback carrying figures: {fallback[:80]!r}")
        fallback = None
    return {"verdict": "block", "reason_class": cls,
            "reason": reason or "no reason given", "fallback": fallback}


def built_fallback(reason_class: str) -> str:
    """The honest line to send when the gate blocked and offered nothing usable."""
    return FALLBACK_BY_CLASS.get(reason_class or "other", FALLBACK_BY_CLASS["other"])


def _sent(verdict: str, reason: str, ms: int, **extra) -> dict:
    out = {"verdict": verdict, "reason": reason, "reason_class": "other",
           "fallback": None, "ms": ms}
    out.update(extra)
    return out


def verify_reply(athlete_msg: str, proposed_reply: str, context: dict, claude_bin: str,
                 model: str = "claude-opus-5", log=print, runner=None,
                 timeout: int = GATE_TIMEOUT_S,
                 model_unavailable=None) -> dict:
    """Judge one outgoing message. Always returns a dict; never raises.

    {"verdict": "send"|"block", "reason": str, "reason_class": str,
     "fallback": str|None, "ms": int, "unverified": True (only when it failed open)}

    Every exit is a SEND except an explicit, parseable block. The gate is a safety net, not
    a gatekeeper with a veto by default: a timeout, an auth failure, a missing binary or a
    reply that is not JSON all send the original text with `unverified` set, so the caller
    logs why it went out unchecked rather than the athlete getting silence.

    `model_unavailable` is injected rather than imported so this module stays free of
    nutrition_nlu; the bot passes NLU.model_unavailable, which is the one detector for the
    CLI refusing to run and must not be copied here."""
    runner = runner or subprocess.run
    started = time.monotonic()

    def ms():
        return int((time.monotonic() - started) * 1000)

    try:
        body = json.dumps(context or {}, indent=2, default=str)
    except (TypeError, ValueError):
        body = str(context)[:GATE_CONTEXT_CHARS]
    if len(body) > GATE_CONTEXT_CHARS:
        body = body[:GATE_CONTEXT_CHARS] + "\n... (context trimmed)"
    prompt = GATE_PROMPT % ((athlete_msg or "(nothing - not a reply to a message)"),
                            body, proposed_reply or "(empty)")
    try:
        proc = runner([claude_bin, "--print", "--model", model], input=prompt,
                      capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return _sent("send", f"gate unreachable: {type(exc).__name__}: {exc}", ms(),
                     unverified=True)
    raw = (getattr(proc, "stdout", "") or "").strip()
    if model_unavailable is not None and model_unavailable(raw):
        return _sent("send", "gate model unavailable", ms(), unverified=True)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return _sent("send", f"gate returned no JSON: {raw[:80]!r}", ms(),
                     unverified=True)
    try:
        got = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return _sent("send", f"gate JSON did not parse: {raw[start:start + 80]!r}", ms(),
                     unverified=True)
    if not isinstance(got, dict):
        return _sent("send", "gate returned something that is not an object", ms(),
                     unverified=True)
    clean = _clean_verdict(got, log)
    if clean is None:
        return _sent("send", "gate verdict unusable", ms(), unverified=True)
    clean["ms"] = ms()
    return clean
