#!/usr/bin/env python3
"""Catch a reply that writes out a breakdown and then states the wrong total for it.

WHY (18 Aug 2026 incident). Jamie ran an activity check on his own swim. The reply
contained a segment table reading 400 + 400 + 1000 + 400 + 300, and underneath it the
sentence "That's ~2600m". The table is right and sums to 2500. The 2600 has no working
behind it at all: it is a transcription error, a wrong number typed under a correct
table. He caught it by doing the addition himself, which is the part that should not be
his job. It was the third arithmetic mistake he caught in one morning.

Note what is NOT the failure here. The model did not mis-add. It never added. So no
amount of "check your maths" in the prompt addresses it, and no better model removes it,
because there is no reasoning step to improve - there is a claim in the text with nothing
underneath it. The only fix that holds is to do the addition in Python and compare.

Jamie's call was to make this all-sports. A swim breakdown in metres, a bike interval
breakdown in minutes, a run split breakdown in kilometres and a fuelling breakdown in
grams are all the same shape: the text lists components, the text says what they come to,
and those two statements can disagree. Nothing here knows or cares which sport it is
reading.

THE DIRECTION OF RISK, which set every judgement call below.
A missed wrong total leaves things exactly as they were this morning: the athlete has the
components in front of him and can catch it, as he did. A FALSE alarm is a new bug this
module would have invented, and a false CORRECTION is worse again, because it rewrites a
number the athlete never got wrong. So every ambiguous case resolves to silence. This
module deliberately misses real sum claims rather than risk inventing one; the known gaps
are listed at the bottom of this docstring.

WHAT COUNTS AS "THE TEXT CLAIMS THESE SUM TO THAT"
Only two constructions, and both require the text's OWN phrasing to join the components
as a sum. A bare list of numbers that merely happens to be summable is never a claim.

  Shape A, the explicit chain:   400 + 400 + 1000, that's 1800m
      Plus signs. The text itself is doing the addition, so reading it as addition is not
      an interpretation.
  Shape B, the list with an explicit summing verb:   400, 400 and 300, totalling 1100m
      No plus signs, so the summing verb is carrying the whole claim, and the verb set is
      therefore much narrower than Shape A's: "totalling", "for a total of", "adds up to"
      and friends. "That's" and "=" are accepted for Shape A and REFUSED for Shape B, and
      that single distinction is what keeps the per-day case quiet:
          "Tuesday: 71, Wednesday: 103, Friday: 155. That's 329 for the week"
      is a bare list plus a loose demonstrative. Three separate days' loads are not a
      breakdown of one thing, "that's" does not say they were added, and flagging it (or
      worse, "correcting" it) would be a false alarm about a total the athlete never
      claimed. Add an explicit "totalling 329" to the same sentence and it becomes a
      claim, and is checked.

UNITS ARE A REJECTION RULE, NOT A GROUPING HEURISTIC
A triathlon summary can mention swim metres, bike watts and run minutes in one message.
Components are only ever summed when every unit stated in the group is the SAME unit, and
a group with two different units is dropped whole rather than partitioned and part-summed.
No conversion either, not even between km and m: converting is a second place to be wrong
for no gain, since a coach writing "3km + 400m, that's 3.4km" is rare and a wrong answer
there would be self-inflicted. A group may also be entirely unitless ("400 + 400 = 800"),
which is checkable and carries no cross-unit risk.

TOLERANCE: 1% of the true sum, floored at 1 unit, and a mismatch needs to EXCEED it
Real replies round a displayed total, and a checker that demands exact equality would cry
wolf on "12 + 13 + 14, so about 40 minutes". But rounding a total by more than 1% is not
rounding, it is a different number. The floor covers small totals, where 1% is less than
the smallest amount anyone rounds by: the 40-for-39 case is out by exactly 1 and the
comparison is strictly greater-than, so it passes. The incident is out by 100 against a
tolerance of 25, so it is caught with room to spare.

An approximation marker does NOT widen this. It is tempting, because the incident text
says "~2600" and a tilde looks like permission to be vague, but the tilde is what the
model writes when it is guessing, so honouring it would blind the check on exactly the
sentences most likely to be wrong. "~2600" is treated as 2600.

KNOWN GAPS, stated rather than papered over. All are the conservative direction.
  - A bare list summed by a loose demonstrative ("Tuesday: 71, ... . That's 329 for the
    week") is not checked. See above; this is a choice, not an oversight.
  - Anything with a multiplier ("4 x 400m"), a clock time ("1:30"), a rate ("90g/hr"), a
    percentage or a numeric range ("400-500m") is dropped whole. Parsing multipliers is
    the obvious next feature and the obvious next way to get a swim set wrong.
  - Components spread across a Markdown table or separate bullet lines with the total in
    a later paragraph are out of reach: the total has to sit close to the components.
  - Units are matched as written, so "3km + 400m" is dropped, not converted.
  - A total that states NO unit against components that do state one is still checked, so
    a bare number in the total position can in principle be read as a total when it was
    something else ("20 + 20 minutes, that's 3"). The obvious families are already dead -
    a bare "so" is not a total word and a counting noun after the number is vetoed - and
    the conservative closure, demanding the unit on both sides, would cost the ordinary
    "12 + 12 + 12 + 12 minutes = 48". Left open knowingly, as the cheaper of the two.
  - The rival-total check that suppresses a CORRECTION runs to the end of the sentence,
    so an unrelated total in the same sentence blocks a substitution that would have been
    fine. Over-caution, and only ever on correction: detection still reports.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- units ---------------------------------------------------------------------------
#
# Canonical form per alias, so "min"/"mins"/"minutes" are one unit and "m"/"metres" are
# another. Ordered longest-first where it matters: the alternation is built from this
# list by length, so "minutes" is tried before "min" and "min" before "m", and the "m" in
# "minutes" can never be read as metres.
#
# Bare "s" for seconds is deliberately ABSENT. "400s" is as likely to be a careless plural
# as it is to be seconds, and a wrong unit read is a wrong group. "sec"/"secs"/"seconds"
# are unambiguous and are in.
_UNIT_ALIASES = {
    "m": "m", "metre": "m", "metres": "m", "meter": "m", "meters": "m",
    "km": "km", "kilometre": "km", "kilometres": "km",
    "kilometer": "km", "kilometers": "km",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "sec": "sec", "secs": "sec", "second": "sec", "seconds": "sec",
    "w": "W", "watt": "W", "watts": "W",
    "kj": "kJ", "kcal": "kcal", "cal": "kcal", "cals": "kcal",
    "calorie": "kcal", "calories": "kcal",
    "g": "g", "gram": "g", "grams": "g", "kg": "kg",
    "ml": "ml", "l": "l", "litre": "l", "litres": "l",
    "tss": "TSS", "bpm": "bpm", "rpm": "rpm",
}
_UNIT_PAT = "|".join(
    re.escape(a) for a in sorted(_UNIT_ALIASES, key=len, reverse=True)
)
# A number: optional thousands commas, optional decimals. "1,000" and "2500" and "1.5".
_NUM_PAT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
# One component: a number with an OPTIONAL unit glued or spaced onto it.
_COMPONENT_PAT = rf"(?P<num>{_NUM_PAT})\s*(?P<unit>(?:{_UNIT_PAT})\b)?"
# Approximation markers. Parsed so they can be recognised and then ignored, and so that a
# correction can put the number back inside them untouched. See the docstring: these never
# widen the tolerance.
_APPROX_PAT = r"~|≈|about|around|roughly|approx\.?|approximately|circa|c\."

# --- the two component-chain shapes ---------------------------------------------------
#
# Shape A. Two or more components joined by "+". Nothing else joins them, so a chain
# cannot silently run through a "-" (which would be subtraction) or a "," (which is Shape
# B's job and has a much stricter total rule).
_PLUS_CHAIN_RE = re.compile(
    rf"(?:{_NUM_PAT})\s*(?:(?:{_UNIT_PAT})\b)?"
    rf"(?:\s*\+\s*(?:{_NUM_PAT})\s*(?:(?:{_UNIT_PAT})\b)?)+",
    re.IGNORECASE,
)
# Shape B. Comma-separated components, optionally with a final "and". The trailing comma
# is NOT consumed here so the summing verb that follows it is still visible to the total
# scan.
_LIST_CHAIN_RE = re.compile(
    rf"(?:{_NUM_PAT})\s*(?:(?:{_UNIT_PAT})\b)?"
    rf"(?:\s*,\s*(?:{_NUM_PAT})\s*(?:(?:{_UNIT_PAT})\b)?)+"
    rf"(?:\s*,?\s*and\s+(?:{_NUM_PAT})\s*(?:(?:{_UNIT_PAT})\b)?)?",
    re.IGNORECASE,
)
_COMPONENT_RE = re.compile(_COMPONENT_PAT, re.IGNORECASE)

# --- how the text says "and that comes to" --------------------------------------------
#
# Two families. LOOSE markers are enough for Shape A, where the plus signs have already
# proved the text is adding up. STRICT markers name the act of totalling explicitly and
# are the only thing that can turn Shape B's bare comma list into a claim.
_STRICT_TOTAL_WORDS = (
    r"for a total of|totalling|totaling|totals|total of|total:|total|"
    r"adds? up to|add up to|sums? to|sum to|summing to|amounts? to"
)
#
# A BARE "so" IS DELIBERATELY NOT HERE, though it reads like the obvious next entry. "so"
# joins a sum to anything at all, including a count of the components themselves: "20 + 20
# minutes, so 3 sets" would be read as a claim that 20 + 20 is 3, and reported as an error
# of 37. "so that's" and "so that is" are specific enough and are in. Same reasoning keeps
# the tail list short.
_LOOSE_TOTAL_WORDS = (
    rf"{_STRICT_TOTAL_WORDS}|=|≈|→|->|"
    r"so that'?s|so that is|that'?s|that is|thats|"
    r"which (?:is|makes|comes to|gives|adds up to)|"
    r"comes? to|coming to|giving|gives|equals?|call it"
)
# Trailing forms: "1100m total", "1100m overall". Same split, strict versus loose. "total"
# and "overall" are strict because they name the sum; a bare trailing number with no word
# after it is never a claim.
_STRICT_TOTAL_TAIL = r"in total|altogether|combined|in all|overall|total"
_LOOSE_TOTAL_TAIL = rf"{_STRICT_TOTAL_TAIL}|all in"


def _total_claim_re(strict: bool) -> re.Pattern:
    words = _STRICT_TOTAL_WORDS if strict else _LOOSE_TOTAL_WORDS
    tail = _STRICT_TOTAL_TAIL if strict else _LOOSE_TOTAL_TAIL
    return re.compile(
        rf"(?:(?P<lead>{words})\s*(?:(?:{_APPROX_PAT})\s*)?"
        rf"(?P<num>{_NUM_PAT})\s*(?P<unit>(?:{_UNIT_PAT})\b)?)"
        rf"|(?:(?:(?:{_APPROX_PAT})\s*)?(?P<num2>{_NUM_PAT})\s*"
        rf"(?P<unit2>(?:{_UNIT_PAT})\b)?\s*(?P<tail>{tail})\b)",
        re.IGNORECASE,
    )


_STRICT_CLAIM_RE = _total_claim_re(True)
_LOOSE_CLAIM_RE = _total_claim_re(False)

# A number that counts the components is not a total OF the components. "400 + 400, that's
# 32 lengths" and "10 + 10 + 10, that's 3 blocks" are both ordinary coaching sentences, and
# both put a small number exactly where a total goes. Without this veto the swim one is
# reported as 800 mis-stated as 32, which is a loud, confident false alarm about a sentence
# that was completely correct. Checked on the word FOLLOWING the stated total.
_COUNTING_NOUN_RE = re.compile(
    r"^\s*(?:sets?|reps?|rounds?|blocks?|sessions?|efforts?|intervals?|times|laps?|"
    r"lengths?|days?|weeks?|pieces?|bouts?|strides?)\b", re.IGNORECASE)

# How far after the last component the stated total may sit. 80 characters is about one
# short sentence, which is what the incident looks like ("... + 300. That's ~2600m").
#
# THE WINDOW IS NOT THE REAL CONSTRAINT AND MUST NOT BE RELAXED IN ISOLATION. The rule
# that actually stops a total binding to the wrong list is _no_digits_between: there may
# be NO other digit anywhere between the end of the chain and the stated total. Without
# it, "400 + 400. Your FTP is 250W, so that's 800m" would bind the 800 across an unrelated
# sentence, and a longer window would make that steadily more likely. The two work as a
# pair. Deleting the digit rule because it "looks redundant" would quietly turn this
# module into a source of false alarms.
_TOTAL_WINDOW = 80


@dataclass(frozen=True)
class SumClaim:
    """One place where the text adds a list of numbers up and says what they come to."""

    components: tuple           # the numbers as written, in order
    true_sum: float             # what they actually add up to
    stated_total: float         # what the text says they add up to
    unit: str | None            # canonical unit shared by the group, None if unitless
    tolerance: float            # how far apart the two may be before this is a mismatch
    shape: str                  # "plus_chain" or "list_and_verb"
    passage: str                # verbatim text from first component to end of the total
    span: tuple                 # (start, end) of `passage` in the original text
    total_span: tuple           # (start, end) of the stated total's NUMERIC LITERAL only
    total_literal: str          # that literal exactly as written, e.g. "2,600"
    ambiguous: bool = False     # a second, different total also claimed in the window

    @property
    def delta(self) -> float:
        return self.stated_total - self.true_sum

    @property
    def agrees(self) -> bool:
        return abs(self.delta) <= self.tolerance

    def describe(self) -> str:
        """One line for a log or an alert. Says both numbers, because the whole point is
        that the two disagree and a reader needs to see which is which."""
        u = self.unit or ""
        parts = " + ".join(_fmt(c) for c in self.components)
        return (f"{parts} = {_fmt(self.true_sum)}{u}, "
                f"but the text says {_fmt(self.stated_total)}{u}")


def _fmt(value: float) -> str:
    """A number the way a person writes it: no trailing .0 on a whole number."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


# --- things that make a passage unparseable, so it is dropped whole -------------------
#
# Every one of these is a construction whose ARITHMETIC MEANING this module does not
# implement. Dropping the group is the honest response; guessing is not. "4 x 400m" is the
# one that matters most, because that is how a swim set is actually written and reading it
# as a bare 400 would understate the true sum by 1200 and then report the correct total as
# an error, which is the exact false alarm this module must never produce.
_UNSAFE_PATTERNS = (
    # A multiplier, in any of the three ways it gets typed. The lookarounds on the letter
    # "x" keep it from firing on words that merely contain one ("max 250W", "Box 3").
    (re.compile(r"\d\s*[×*]|[×*]\s*\d"), "a multiplier"),
    (re.compile(r"(?<![a-z])x\s*\d|\d\s*x(?![a-z])", re.I), "a multiplier"),
    # A clock time. "1:30" is ninety seconds or ninety minutes and nothing here can tell.
    # Matched TIGHT, with no spaces: "Session 3: 400 + 400" is a heading and a breakdown,
    # not a time, and a spaced pattern would throw it away.
    (re.compile(r"\d:\d"), "a clock time"),
    # A rate or a fraction. Catches "90g/hr" and "3/4", and cheaply, because a slash has no
    # business inside a plain sum in the first place.
    (re.compile(r"/"), "a rate or fraction"),
    (re.compile(r"\d\s*%"), "a percentage"),
    # A numeric range, "400-500m". Matched TIGHT, with no spaces allowed around the
    # hyphen, on purpose: this repo writes its prose with a spaced hyphen instead of an
    # em-dash, so "400 + 400 - that's 800m" is an ordinary sentence and a looser pattern
    # would throw away a large share of real, correct breakdowns.
    (re.compile(r"\d[-–]\d"), "a numeric range"),
)


# THE DANGEROUS PART OF THE CONSTRUCTION IS OFTEN OUTSIDE THE PASSAGE (found by the tests,
# 18 Aug 2026). The component chain starts at the first NUMBER it can match, so in
# "4 x 400m + 200m" the chain begins at 400 and the "4 x " that changes everything sits one
# character before it. Same at the other end: in "40 + 30 + 20, that is 100%" the passage
# stops at the 100 and the "%" is the next character along. Scanning only the passage let
# all three of these through, each as a confident false alarm.
#
# So the two boundaries are checked with their own ANCHORED patterns rather than by
# widening the passage. Anchoring is the point: a blanket margin of a few characters either
# side would start bailing on innocent nearby text ("Your 5km/week base - 400 + 400 ..."),
# and trading one false alarm for one false silence is not progress. These only fire when
# the construction is literally touching the sum.
_LEAD_UNSAFE_RE = re.compile(r"(?:\d\s*[x×*]\s*|\d[-–])$", re.IGNORECASE)
_TRAIL_UNSAFE_RE = re.compile(r"^\s*[%/]")


def _unsafe_reason(text: str, start: int, end: int) -> str | None:
    """Why this passage cannot be read as a plain sum, or None if it can."""
    for pat, why in _UNSAFE_PATTERNS:
        if pat.search(text[start:end]):
            return why
    if _LEAD_UNSAFE_RE.search(text[max(0, start - 8):start]):
        return "a multiplier or range immediately before the list"
    if _TRAIL_UNSAFE_RE.match(text[end:end + 4]):
        return "a percentage or rate immediately after the total"
    return None


def _parse_components(chunk: str):
    """The numbers in a component chain, plus the one unit they all share.

    Returns (values, unit, ok). `ok` is False when the group must be dropped: two
    different units in one chain is the "never sum across units" rule, and it is a
    rejection rather than a split, because half of a breakdown is not a breakdown."""
    values, units = [], set()
    for m in _COMPONENT_RE.finditer(chunk):
        values.append(float(m.group("num").replace(",", "")))
        if m.group("unit"):
            units.add(_UNIT_ALIASES[m.group("unit").lower()])
    if len(units) > 1:
        return values, None, False
    return values, (next(iter(units)) if units else None), True


def _no_digits_between(text: str, start: int, end: int) -> bool:
    return not re.search(r"\d", text[start:end])


def _rival_total(text: str, claim_re: re.Pattern, after: int, limit: int,
                 stated: float) -> bool:
    """Is a SECOND, different total claimed for the same components?

    Scanned separately from the binding pass above, and this is not tidiness, it is the
    only way to see it. The binding pass stops at the first intervening digit, which the
    rival total IS, so a passage like "400 + 400, that is 900m, 1000m overall" looks
    perfectly unambiguous from inside that loop. It is not: correcting the 900 to 800 would
    leave the 1000 standing and contradicting the number just written for it.

    Bounded by the end of the sentence, because a total belonging to the NEXT sentence is a
    different subject and not a rival. Only ever suppresses a correction; detection still
    reports the mismatch, so an over-eager rival costs silence, never a false alarm."""
    stop = re.search(r"[.!?\n]", text[after:limit])
    tail = text[after:after + stop.start()] if stop else text[after:limit]
    return any(abs(float((m.group("num") or m.group("num2")).replace(",", "")) - stated)
               > 1e-9 for m in claim_re.finditer(tail))


def _scan(text: str, chain_re: re.Pattern, claim_re: re.Pattern, shape: str):
    """Every sum claim of one shape. See the module docstring for what qualifies."""
    out = []
    for chain in chain_re.finditer(text):
        values, unit, ok = _parse_components(chain.group(0))
        if not ok or len(values) < 2:
            continue
        window_end = min(len(text), chain.end() + _TOTAL_WINDOW)
        window = text[chain.end():window_end]
        claims = []
        for c in claim_re.finditer(window):
            num = c.group("num") or c.group("num2")
            lit_start = chain.end() + (
                c.start("num") if c.group("num") is not None else c.start("num2"))
            lit_end = chain.end() + (
                c.end("num") if c.group("num") is not None else c.end("num2"))
            if not _no_digits_between(text, chain.end(), lit_start):
                # Something else numeric intervenes, so this total is about something
                # else. Stop rather than keep looking: everything further out is further
                # from the components and even less likely to be theirs.
                break
            # A KEYWORD ARGUMENT IS NOT AN EQUATION (18 Aug 2026, found by running this
            # detector over all 247 .py and .md files in the repo). The one and only false
            # positive in that sweep was scripts/refresh-site-data.py, where "90 + 365)"
            # on one line bound to "timedelta(days=365" on the next and was reported as a
            # total of 455 mis-stated as 365. Prose puts a space or a digit before an "="
            # sign; "days=365" puts a letter there. Reply text is not expected to contain
            # code, but a guard that only works on the inputs you imagined is not a guard.
            if (c.group("lead") or "").strip() == "=":
                before = text[:chain.end() + c.start("lead")]
                if before and before[-1].isalpha():
                    continue
            unit_txt = c.group("unit") or c.group("unit2")
            claim_end = chain.end() + c.end()
            if _COUNTING_NOUN_RE.match(text[claim_end:claim_end + 24]):
                continue          # counts the components, does not total them
            claims.append((float(num.replace(",", "")), num, lit_start, lit_end,
                           _UNIT_ALIASES[unit_txt.lower()] if unit_txt else None,
                           claim_end))
        if not claims:
            continue
        stated, literal, lit_start, lit_end, claim_unit, claim_end = claims[0]
        # A total that names a DIFFERENT unit from the components is not their total.
        # "400m + 400m, that's 13 minutes" is a true statement about a swim, and reading
        # 13 as a bad sum of 800 would be the module inventing an error.
        if claim_unit and unit and claim_unit != unit:
            continue
        passage = text[chain.start():claim_end]
        if _unsafe_reason(text, chain.start(), claim_end):
            continue
        group_unit = unit or claim_unit
        true_sum = sum(values)
        # Two different totals claimed for one list. Reported, because something IS wrong
        # in there, but never corrected: which one was meant is exactly the guess this
        # module refuses to make.
        ambiguous = (any(abs(c[0] - stated) > 1e-9 for c in claims[1:])
                     or _rival_total(text, claim_re, claim_end, window_end, stated))
        out.append(SumClaim(
            components=tuple(values), true_sum=true_sum, stated_total=stated,
            unit=group_unit, tolerance=tolerance_for(true_sum), shape=shape,
            passage=passage, span=(chain.start(), claim_end),
            total_span=(lit_start, lit_end), total_literal=literal,
            ambiguous=ambiguous))
    return out


def tolerance_for(true_sum: float) -> float:
    """How far a stated total may sit from the truth and still be a rounding.

    1% of the sum, floored at 1 unit, and callers compare with a strict greater-than so a
    total exactly on the tolerance passes. See the docstring for the reasoning; the short
    version is that rounding by more than 1% is not rounding."""
    return max(0.01 * abs(float(true_sum)), 1.0)


def find_sum_claims(text: str) -> list:
    """Every breakdown-plus-total claim in `text`, right ones included.

    Shape A is scanned first and wins any overlap, because a chain with plus signs in it
    is the stronger evidence of the two."""
    if not text:
        return []
    claims = _scan(text, _PLUS_CHAIN_RE, _LOOSE_CLAIM_RE, "plus_chain")
    taken = [c.span for c in claims]
    for c in _scan(text, _LIST_CHAIN_RE, _STRICT_CLAIM_RE, "list_and_verb"):
        if not any(c.span[0] < e and s < c.span[1] for s, e in taken):
            claims.append(c)
    claims.sort(key=lambda c: c.span)
    return claims


def find_mismatches(text: str) -> list:
    """THE ENTRY POINT. Every place `text` states a total its own components contradict.

    Pure: no I/O, no athlete data, no network. Returns a list of SumClaim, empty when the
    text is fine, which is the overwhelmingly common case. Each one carries the components,
    the true sum, the stated total, the shared unit, the verbatim passage and the offsets
    of the stated total's numeric literal - enough for a caller to log it, alert on it,
    append a note to the reply, or hand it to correct_totals below."""
    return [c for c in find_sum_claims(text) if not c.agrees]


# --- correction, and the argument against switching it on by default ------------------
#
# The true sum is a provable fact about numbers already in the text, so substituting it
# for a wrong total is not fabrication in the way inventing a power figure is, and it
# would have made the incident reply correct. It is offered here for that reason.
#
# It is NOT the recommended default, and the argument is the incident itself. Jamie caught
# the wrong total because the reply visibly disagreed with itself. Rewriting the total
# assumes the COMPONENT LIST is the complete and correct side, and when that assumption is
# wrong - a rep dropped from the table, a warm-up left out - correcting the total does not
# fix a thing. It makes a reply that was merely inconsistent into one that is confidently,
# self-consistently wrong, and it removes the disagreement that was the only signal the
# athlete had. That is a bad trade against a failure mode nobody has yet observed being
# caught any other way.
#
# So: detect and log by default, correct only if Jamie decides the noise of a flag is
# worse than that risk, and even then log every substitution, because a correction that
# nobody can see is indistinguishable from the model getting it right.
@dataclass(frozen=True)
class Correction:
    """The outcome of correct_totals. `applied` and `refused` together account for every
    mismatch found, so a caller can log the ones it declined to touch."""

    text: str
    applied: tuple = field(default_factory=tuple)
    refused: tuple = field(default_factory=tuple)   # (SumClaim, why) pairs

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def _render_like(value: float, literal: str) -> str:
    """The corrected number, written the way the wrong one was.

    Keeps the thousands comma if the original had one, and keeps a decimal point only if
    the original had one, so a substitution looks like the rest of the sentence rather
    than like a machine got in."""
    if "." in literal:
        decimals = len(literal.split(".")[1])
        out = f"{value:.{decimals}f}"
        whole, dot, frac = out.partition(".")
    else:
        whole, dot, frac = str(int(round(value))), "", ""
    if "," in literal:
        whole = f"{int(whole):,}"
    return whole + dot + frac


def correct_totals(text: str) -> Correction:
    """Replace provably wrong totals with the sum of their own components.

    CONSERVATIVE BY CONSTRUCTION, in four ways that are the whole value of the function:
      - only the stated total's NUMERIC LITERAL is replaced, by byte offset. The
        components are never touched, and neither is anything around the number: the unit,
        the tilde, the wording and the spacing all survive, so "~2600m" becomes "~2500m".
      - a mismatch flagged `ambiguous` (a second, different total claimed for the same
        components) is refused. Which of the two was meant is a guess.
      - two mismatches whose passages overlap are both refused. Overlap means the same
        numbers are being read two ways, and at most one reading is right.
      - a claim whose stated total names a different unit from its components never
        becomes a mismatch in the first place, so it can never reach here.
    Substitutions are applied right to left so earlier offsets stay valid."""
    claims = find_mismatches(text or "")
    applied, refused = [], []
    for i, c in enumerate(claims):
        others = claims[:i] + claims[i + 1:]
        if c.ambiguous:
            refused.append((c, "more than one total claimed for these components"))
        elif any(c.span[0] < o.span[1] and o.span[0] < c.span[1] for o in others):
            refused.append((c, "overlaps another sum claim"))
        else:
            applied.append(c)
    out = text or ""
    for c in sorted(applied, key=lambda c: c.total_span, reverse=True):
        s, e = c.total_span
        out = out[:s] + _render_like(c.true_sum, c.total_literal) + out[e:]
    return Correction(text=out, applied=tuple(applied), refused=tuple(refused))
