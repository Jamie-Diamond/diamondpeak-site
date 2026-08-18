"""Breakdown-versus-total reconciliation in lib/arithmetic_reconcile.py.

WHAT BROKE (18 Aug 2026). Jamie ran an activity check on his own swim. The reply laid out
a segment table reading 400 + 400 + 1000 + 400 + 300 and then said "That's ~2600m"
underneath it. The table is correct and sums to 2500. In his words: "There's no working
behind the 2600, I just typed a wrong total under a correct table." He caught it by adding
the numbers up himself. It was the third arithmetic error he caught that morning.

The model did not mis-add. It never added. That is why this is a pytest file and not a
prompt change: there is no reasoning step to improve, only a claim in the text with
nothing underneath it, and the only thing that catches that reliably is doing the sum in
Python.

This is a pytest file for the same reason test_write_verify.py gives for itself:
arithmetic_reconcile is a lib/ module, and lib/ modules are covered by this suite, the one
that gates the repo.

THE DIRECTION OF RISK RUNS THROUGH EVERY TEST HERE, and it is not symmetric.
  A missed wrong total leaves things exactly as they were on 18 Aug. The components are
  still on the athlete's screen and he can still catch it, as he did.
  A FALSE alarm is a bug this module would have invented, about a sentence that was
  correct.
  A false CORRECTION is worse again: it rewrites a number the athlete never got wrong, and
  it destroys the visible disagreement that is the only reason the incident was ever found.
So sections 3 and 4 - the things that must NOT fire - are the load-bearing half of this
file, and they outnumber the detection tests on purpose.
"""
import ast
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import arithmetic_reconcile as A  # noqa: E402

# The verbatim shape of the reply that caused the incident: a correct table, and a wrong
# total typed underneath it with a tilde in front.
INCIDENT_TEXT = (
    "Main set: 400 + 400 + 1000 + 400 + 300. That's ~2600m for the session."
)
INCIDENT_COMPONENTS = (400.0, 400.0, 1000.0, 400.0, 300.0)
INCIDENT_TRUE_SUM = 2500.0
INCIDENT_STATED = 2600.0


# --- 1) the incident itself ------------------------------------------------------------

def test_the_incident_is_caught():
    """The exact failure of 18 Aug 2026, pinned number by number."""
    found = A.find_mismatches(INCIDENT_TEXT)
    assert len(found) == 1
    m = found[0]
    assert m.components == INCIDENT_COMPONENTS
    assert m.true_sum == INCIDENT_TRUE_SUM
    assert m.stated_total == INCIDENT_STATED
    assert m.unit == "m"
    assert not m.agrees


def test_the_incident_reports_where_it_happened():
    """A caller needs more than 'something is wrong'. It needs the passage and the offsets
    of the wrong number, or it can neither quote it to the athlete nor fix it."""
    m = A.find_mismatches(INCIDENT_TEXT)[0]
    assert m.passage == "400 + 400 + 1000 + 400 + 300. That's ~2600m"
    assert INCIDENT_TEXT[m.span[0]:m.span[1]] == m.passage
    assert INCIDENT_TEXT[m.total_span[0]:m.total_span[1]] == "2600"
    assert m.total_literal == "2600"


def test_the_incident_describes_itself_with_both_numbers():
    assert (A.find_mismatches(INCIDENT_TEXT)[0].describe()
            == "400 + 400 + 1000 + 400 + 300 = 2500m, but the text says 2600m")


def test_the_tilde_does_not_excuse_the_incident():
    """THE MOST TEMPTING WRONG FIX. "~2600" looks like the model asking for slack, so it
    looks reasonable to widen the tolerance when an approximation marker is present. The
    incident text has a tilde. Honouring it would blind the check on exactly the sentences
    most likely to be wrong, because the tilde is what the model writes when it is
    guessing. "~2600" is 2600."""
    assert A.find_mismatches("400 + 400 + 1000 + 400 + 300. That's ~2600m")
    assert A.find_mismatches("400 + 400 + 1000 + 400 + 300. That's about 2600m")
    assert A.find_mismatches("400 + 400 + 1000 + 400 + 300. That's roughly 2600m")


def test_the_incident_with_a_correct_total_is_silent():
    """Same sentence, right number. The single most important negative in the file: if
    this ever fires, the module is worse than useless on every correct reply."""
    assert A.find_mismatches("Main set: 400 + 400 + 1000 + 400 + 300. That's 2500m.") == []


# --- 2) other sports, because the fix is not swim-specific ------------------------------

def test_cycling_intervals_in_minutes_correct_total_is_silent():
    assert A.find_mismatches("Intervals: 12 + 12 + 12 + 12 = 48 minutes at threshold.") == []


def test_cycling_intervals_in_minutes_wrong_total_is_caught():
    m = A.find_mismatches("Intervals: 12 + 12 + 12 + 12 = 60 minutes at threshold.")[0]
    assert (m.true_sum, m.stated_total, m.unit) == (48.0, 60.0, "min")


def test_running_splits_in_km_correct_total_is_silent():
    assert A.find_mismatches("Splits 5 + 5 + 2.5, that is 12.5km overall.") == []


def test_running_splits_in_km_wrong_total_is_caught():
    m = A.find_mismatches("Splits 5 + 5 + 2.5, that is 15km overall.")[0]
    assert (m.true_sum, m.stated_total, m.unit) == (12.5, 15.0, "km")


def test_running_splits_in_metres_wrong_total_is_caught():
    m = A.find_mismatches("Reps of 800 + 800 + 600 metres, totalling 2400 metres.")[0]
    assert (m.true_sum, m.stated_total, m.unit) == (2200.0, 2400.0, "m")


def test_fuelling_in_grams_wrong_total_is_caught():
    m = A.find_mismatches("Fuel plan: 60g + 60g + 30g, for a total of 180g.")[0]
    assert (m.true_sum, m.stated_total, m.unit) == (150.0, 180.0, "g")


@pytest.mark.parametrize("text,unit", [
    ("Efforts of 250 + 260 + 240, that's 750W.", "W"),
    ("Sessions at 40 + 55 + 65, totalling 160 TSS.", "TSS"),
    ("Bottles of 500 + 500 + 750, that is 1750ml.", "ml"),
])
def test_a_correct_total_in_any_unit_is_silent(text, unit):
    assert A.find_mismatches(text) == []


# --- 3) rounding: what must NOT be called an error --------------------------------------
#
# Tolerance is 1% of the true sum, floored at 1 unit, and the comparison is strictly
# greater-than. A displayed total is often rounded and that is not a mistake; rounding by
# more than 1% is not rounding, it is a different number.

def test_a_total_rounded_within_tolerance_is_not_flagged():
    """830 + 830 + 837 is 2497 and the reply says 2500. That is a display rounding of 3
    against a tolerance of about 25, and calling it an error would be crying wolf."""
    assert A.find_mismatches("830 + 830 + 837, so that is about 2500m.") == []


def test_the_floor_protects_small_totals():
    """12 + 13 + 14 is 39 and "about 40 minutes" is how a person writes it. 1% of 39 is
    0.39, far below the amount anyone rounds by, so the floor of 1 unit carries this and
    the strictly-greater-than comparison lets an out-by-exactly-1 through."""
    assert A.find_mismatches("12 + 13 + 14, that is about 40 minutes.") == []


def test_a_total_just_outside_tolerance_is_flagged():
    """2500 with a tolerance of 25: 2540 is out by 40. Not a rounding, a wrong number."""
    m = A.find_mismatches("400 + 400 + 1000 + 400 + 300, that is 2540m.")[0]
    assert (m.true_sum, m.stated_total) == (2500.0, 2540.0)


def test_the_tolerance_boundary_is_inclusive():
    """Exactly on the tolerance passes, just past it does not. Pinned because the incident
    (out by 100 against 25) has plenty of room and would not notice this flipping."""
    assert A.tolerance_for(2500) == 25.0
    assert A.tolerance_for(39) == 1.0        # the floor, not the percentage
    assert A.find_mismatches("1000 + 1000 + 500, that is 2525m.") == []   # out by 25
    assert A.find_mismatches("1000 + 1000 + 500, that is 2526m.")         # out by 26


# --- 4) what is NOT a claim that these numbers sum to that ------------------------------
#
# The conservative rule: the text's OWN phrasing must join the components as a sum, with
# plus signs (Shape A) or an explicit summing verb (Shape B). A list of numbers that merely
# happens to be summable is never a claim. Every test here is a false alarm that would have
# been a new bug, worse than the one being fixed.

def test_a_bare_list_of_per_day_loads_is_not_a_sum_claim():
    """Three days' loads are three separate facts, not a breakdown of one thing. Nothing in
    the sentence says they were added."""
    assert A.find_mismatches("Tuesday: 71, Wednesday: 103, Friday: 155. Solid week.") == []


def test_a_bare_list_with_a_loose_demonstrative_is_still_not_a_sum_claim():
    """THE RULE THAT MATTERS. "That's" is accepted after a plus chain, where the plus signs
    have already proved the text is adding up, and refused after a bare comma list, where
    it would be carrying the whole claim on its own. Both forms below are left alone even
    though the numbers happen to add to 329, because the athlete never claimed they did and
    "correcting" a total nobody stated is the worst thing this module could do."""
    assert A.find_mismatches(
        "Tuesday: 71, Wednesday: 103, Friday: 155. That's 329 for the week.") == []
    assert A.find_mismatches(
        "Your loads were 71, 103, 155. That's 329 for the week.") == []


def test_the_same_list_with_an_explicit_summing_verb_IS_checked():
    """The other side of the same rule, so the conservatism is a line and not a blanket.
    Add "totalling" and the text has claimed the sum, so a wrong one is caught and a right
    one is left alone."""
    assert A.find_mismatches("Splits of 71, 103 and 155, totalling 329 TSS.") == []
    m = A.find_mismatches("Splits of 71, 103 and 155, totalling 350 TSS.")[0]
    assert (m.true_sum, m.stated_total, m.shape) == (329.0, 350.0, "list_and_verb")


def test_a_multiplier_drops_the_whole_group():
    """"4 x 400m" is how a swim set is actually written. Reading the 400 as a lone
    component would understate the true sum by 1200 and then report a CORRECT total as an
    error. Dropping the group is the honest answer; parsing multipliers is the obvious next
    feature and the obvious next way to get a swim set wrong."""
    assert A.find_mismatches("4 x 400m + 200m, that is 1800m.") == []
    assert A.find_mismatches("400m + 200m x 4, that is 1200m.") == []


def test_a_multiplier_veto_does_not_fire_on_words_containing_x():
    """The veto is anchored so it cannot eat an ordinary sentence."""
    assert A.find_mismatches("Max 250 + 260 + 240, that is 800W.")


@pytest.mark.parametrize("text", [
    "1:30 + 1:30, that is 3:00 of work.",              # a clock time
    "Fuelling 60 + 30, that is 100g/hr.",              # a rate
    "Zones 40 + 30 + 20, that is 100%.",               # a percentage
    "Efforts 400-500 + 400, that is 1000m.",           # a numeric range
])
def test_constructions_this_module_does_not_implement_are_dropped_whole(text):
    """Each of these has an arithmetic meaning the parser does not model. Guessing at one
    produces a confident wrong answer, so the group goes in the bin instead.

    Every case here is deliberately one whose numbers do NOT agree, so the test fails if
    the veto stops working. An earlier draft used "60 + 30, that is 90g/hr", which passed
    for the wrong reason: 60 + 30 really is 90, so it would have stayed green with the rate
    veto deleted entirely."""
    assert A.find_mismatches(text) == []


def test_a_clock_time_veto_does_not_eat_a_numbered_heading():
    """"Session 3: 400 + 400" is a heading and a breakdown, not a time. The clock pattern is
    matched tight, with no spaces, so the colon in a heading is left alone."""
    m = A.find_mismatches("Session 3: 400 + 400 + 300, that is 1200m.")[0]
    assert (m.true_sum, m.stated_total) == (1100.0, 1200.0)


def test_a_spaced_hyphen_is_punctuation_and_not_a_range():
    """This repo writes its prose with a spaced hyphen instead of an em-dash, so a range
    veto that allowed spaces around the hyphen would throw away a large share of real
    breakdowns. Matched tight, and this is the test that keeps it tight."""
    m = A.find_mismatches("400 + 400 + 300 - that's 1200m.")[0]
    assert (m.true_sum, m.stated_total) == (1100.0, 1200.0)


def test_a_count_of_the_components_is_not_a_total_of_them():
    """"400 + 400, that's 32 lengths" is an ordinary, correct coaching sentence that puts a
    small number exactly where a total goes. Without the counting-noun veto it is reported
    as 800 mis-stated as 32: a loud false alarm about a sentence with nothing wrong in it."""
    assert A.find_mismatches("400 + 400, that's 32 lengths.") == []
    assert A.find_mismatches("10 + 10 + 10, that's 3 blocks of 10.") == []


def test_an_unrelated_number_between_the_list_and_the_total_breaks_the_binding():
    """The rule that stops a total binding across a sentence boundary to a list that is not
    its own. The 800 here belongs to the reps, but a 250 stands between them, so the module
    declines to assume rather than binding over the top of it."""
    assert A.find_mismatches(
        "Reps 400 + 400. Your FTP is 250W, so that is 800m.") == []


def test_a_keyword_argument_is_not_an_equation():
    """THE ONLY FALSE POSITIVE IN THE WHOLE REPO, found by running this detector over all
    247 .py and .md files before shipping it. scripts/refresh-site-data.py has "90 + 365)"
    on one line and "timedelta(days=365" on the next, and the "=" bound the two into a
    claimed total of 455 mis-stated as 365. Prose puts a space or a digit before an "=";
    "days=365" puts a letter there. Reply text should never contain code, but a guard that
    only works on the inputs somebody imagined is not a guard."""
    assert A.find_mismatches(
        "p_from = today - timedelta(days=90 + 365)\n"
        "p_to   = today - timedelta(days=365)") == []
    # ... and an ordinary prose equation still works, which is the point of being narrow.
    assert A.find_mismatches("400 + 400 + 300 = 1200m.")


def test_a_breakdown_laid_out_over_several_lines_is_still_checked():
    """Replies are often formatted as a heading, the components on their own line and the
    total under them. That is exactly how the incident reply read on Jamie's phone."""
    m = A.find_mismatches(
        "Main set:\n400 + 400 + 1000 + 400 + 300\nThat's ~2600m")[0]
    assert (m.true_sum, m.stated_total) == (2500.0, 2600.0)


def test_a_total_stated_in_a_different_unit_is_not_their_total():
    """"400m + 400m, that's 13 minutes" is a true statement about a swim. Reading the 13 as
    a bad sum of 800 would be the module inventing an error out of a unit change."""
    assert A.find_mismatches("400m + 400m, that's 13 minutes of swimming.") == []


# --- 5) units are a rejection rule, never summed across ---------------------------------

def test_components_in_two_different_units_are_never_summed():
    """3km + 400m is 3.4km to a human and 403 to a naive adder. No conversion is attempted,
    not even a correct one: converting is a second place to be wrong for no real gain."""
    assert A.find_mismatches("3km + 400m, that is 3.4km.") == []
    assert A.find_mismatches("3km + 400m, that is 403km.") == []


def test_a_triathlon_reply_sums_each_unit_separately():
    """Metres, watts and kilometres in one message. Each group is summed against its own
    unit and never across, so three wrong totals surface as three findings that each name
    the right unit."""
    found = A.find_mismatches(
        "Swim 400 + 400 = 900m. Bike 250 + 260 + 240 = 800W. Run 5 + 5 = 12km.")
    assert [(m.true_sum, m.stated_total, m.unit) for m in found] == [
        (800.0, 900.0, "m"), (750.0, 800.0, "W"), (10.0, 12.0, "km")]


def test_a_triathlon_reply_with_every_total_right_is_silent():
    assert A.find_mismatches(
        "Swim 400 + 400 = 800m. Bike 250 + 260 + 240 = 750W. Run 5 + 5 = 10km.") == []


def test_a_unit_named_only_on_the_total_still_labels_the_group():
    """The incident's own shape: the components are bare and only the total carries "m"."""
    assert A.find_mismatches(INCIDENT_TEXT)[0].unit == "m"


def test_minutes_are_never_read_as_metres():
    """"min" and "m" share a first letter, and the alternation is built longest-first so the
    m in minutes can never be taken for metres. If this breaks, a bike group and a swim
    group merge and the sums become nonsense."""
    m = A.find_mismatches("20 minutes + 20 minutes, that is 50 minutes.")[0]
    assert m.unit == "min"


# --- 6) parsing the numbers themselves --------------------------------------------------

def test_thousands_commas_are_parsed_not_split():
    m = A.find_mismatches("1,000 + 1,000 + 500, that is 2,600m.")[0]
    assert m.components == (1000.0, 1000.0, 500.0)
    assert (m.true_sum, m.stated_total, m.total_literal) == (2500.0, 2600.0, "2,600")


@pytest.mark.parametrize("phrasing", [
    "400 + 400 + 300, that's 1200m.",
    "400 + 400 + 300, that is 1200m.",
    "400 + 400 + 300 = 1200m.",
    "400 + 400 + 300, totalling 1200m.",
    "400 + 400 + 300, for a total of 1200m.",
    "400 + 400 + 300, which adds up to 1200m.",
    "400 + 400 + 300, 1200m in total.",
    "400 + 400 + 300, 1200m overall.",
    "400 + 400 + 300, coming to 1200m.",
])
def test_the_many_ways_a_reply_states_a_total(phrasing):
    """Real replies phrase this a dozen ways. Each one has to reach the same finding, or
    the guard protects only the sentences somebody happened to think of."""
    m = A.find_mismatches(phrasing)[0]
    assert (m.true_sum, m.stated_total) == (1100.0, 1200.0)


def test_a_single_number_is_not_a_breakdown():
    assert A.find_mismatches("The swim was 2500m in total.") == []
    assert A.find_mismatches("That's 2600m.") == []


def test_empty_and_none_input_are_safe():
    assert A.find_mismatches("") == []
    assert A.find_mismatches(None) == []
    assert A.correct_totals(None).text == ""
    assert A.correct_totals("").applied == ()


# --- 7) the correcting function, and what it refuses to touch ---------------------------
#
# Built on find_mismatches and deliberately NOT the recommended default. See the note above
# correct_totals: substituting the sum assumes the component list is the complete side, and
# when it is not, the correction turns a visibly inconsistent reply into a confidently
# self-consistent wrong one, destroying the disagreement Jamie used to catch this.

def test_correction_replaces_only_the_wrong_total():
    """The components, the unit, the tilde and every other character survive. Only the four
    digits of the wrong number change."""
    c = A.correct_totals(INCIDENT_TEXT)
    assert c.text == "Main set: 400 + 400 + 1000 + 400 + 300. That's ~2500m for the session."
    assert c.changed and len(c.applied) == 1
    assert c.applied[0].true_sum == INCIDENT_TRUE_SUM


def test_correction_leaves_a_correct_reply_byte_identical():
    good = "Main set: 400 + 400 + 1000 + 400 + 300. That's 2500m."
    c = A.correct_totals(good)
    assert c.text == good and not c.changed


def test_correction_preserves_the_way_the_number_was_written():
    """A substituted number that suddenly loses its thousands comma reads as a machine got
    in. It keeps the comma because the wrong one had one."""
    assert A.correct_totals("1,000 + 1,000 + 500, that is 2,600m.").text == (
        "1,000 + 1,000 + 500, that is 2,500m.")


def test_correction_refuses_when_two_totals_are_claimed():
    """AMBIGUOUS, precisely defined: a second, different total is claimed for the same
    components in the same sentence. Correcting the 900 to 800 would leave the 1000
    standing and contradicting the number just written for it, so the text is returned
    untouched and the refusal is reported instead. Detection still fires - the caller is
    told something is wrong, just not silently handed a guess."""
    text = "400 + 400, that is 900m, 1000m overall."
    c = A.correct_totals(text)
    assert c.text == text and not c.changed
    assert len(c.refused) == 1
    assert c.refused[0][1] == "more than one total claimed for these components"
    assert A.find_mismatches(text)[0].ambiguous is True


def test_a_total_in_the_next_clause_also_blocks_the_correction():
    """The rival-total check is bounded by the end of the sentence, not by the claim, so an
    unrelated total sitting in the same sentence ALSO marks the passage ambiguous and stops
    the correction. That is over-caution and it is on purpose: it costs a substitution that
    would probably have been fine, and it buys never rewriting a number in a passage where
    two totals are in play. Detection is unaffected, so the caller still hears about it."""
    text = "400 + 400 = 900m, and your next session is 60 minutes total."
    assert A.find_mismatches(text)[0].ambiguous is True
    c = A.correct_totals(text)
    assert c.text == text and not c.changed and len(c.refused) == 1


def test_a_total_in_the_following_SENTENCE_is_not_a_rival():
    """The other side of that boundary, or the over-caution would swallow every correction
    in a reply that keeps talking. A new sentence is a new subject."""
    text = "400 + 400 = 900m. Your next session is 60 minutes total."
    assert A.find_mismatches(text)[0].ambiguous is False
    assert A.correct_totals(text).text == (
        "400 + 400 = 800m. Your next session is 60 minutes total.")


def test_correction_fixes_every_sport_in_one_reply_and_touches_nothing_else():
    c = A.correct_totals(
        "Swim 400 + 400 = 900m. Bike 250 + 260 + 240 = 800W. Run 5 + 5 = 12km.")
    assert c.text == "Swim 400 + 400 = 800m. Bike 250 + 260 + 240 = 750W. Run 5 + 5 = 10km."
    assert len(c.applied) == 3


def test_correction_never_rewrites_a_component():
    """Stated as its own test because it is the invariant that makes the function safe to
    consider at all. The components are the athlete-facing evidence; the total is the only
    thing derived from them."""
    c = A.correct_totals(INCIDENT_TEXT)
    for part in ("400 + 400 + 1000 + 400 + 300", "Main set:", "for the session."):
        assert part in c.text


def test_every_mismatch_is_accounted_for_as_applied_or_refused():
    """No mismatch may be quietly dropped between detection and correction: a caller that
    logs `refused` has to see everything the corrector declined."""
    for text in (INCIDENT_TEXT,
                 "400 + 400, that is 900m, 1000m overall.",
                 "Swim 400 + 400 = 900m. Bike 250 + 260 + 240 = 800W."):
        c = A.correct_totals(text)
        assert len(c.applied) + len(c.refused) == len(A.find_mismatches(text))


# --- 8) the module stays pure ------------------------------------------------------------

def test_the_module_reaches_nothing_outside_itself():
    """Pure text analysis: no athlete files, no network, no clock. It is wired into the
    reply path, which runs on every turn, and anything it touched would be touched there
    too, on every turn, for every athlete.

    Checked by PARSING the imports rather than grepping the source. The module is most of
    it prose - an 80-line docstring narrating the incident plus a WHY comment on every
    guard - and a substring scan over prose cries wolf on a comment edit: a future note
    explaining how some OTHER module uses datetime would fail a purity test while the
    module stayed perfectly pure. An import is the thing that actually has to be a
    deliberate act, so the import is what is asserted."""
    tree = ast.parse((LIB / "arithmetic_reconcile.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"re", "dataclasses", "__future__"}, (
        f"arithmetic_reconcile must stay pure, but it imports {sorted(imported)}")
