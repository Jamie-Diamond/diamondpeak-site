"""Tests for claim detection in lib/write_verify.py (17 Aug 2026).

WHAT BROKE. On 16 Aug at 21:57 Jamie asked for a bug to be logged. The reply reassured him
about a week pushed in an EARLIER turn - "The week I pushed earlier is still on the calendar
and is correct" - and _ICU_CLAIM_RE read that as a fresh calendar write. No write had been
attempted: the turn's own tool summary was "Checked your data, saved your data", a file read
and a local file write. The verifier found no new events, said so, retried, and appended two
false lines to the reply: "That didn't actually save to your calendar" and "Treat your
calendar as unchanged". His calendar was correct throughout.

WHAT IS TESTED HERE. The claim detector's tense sense: retrospective and persistence prose
raises no claim, a fresh write still does, and the pre-existing forward-looking suppression
is untouched. Plus the tool-derived gate, which settles the same question from the tools
that actually ran rather than from prose. Pure functions, no network, no athlete files.

There was no dedicated test file for this module before; the verdict maths is covered by
scripts/test_coach_facts.py. This is a pytest file rather than another standalone script
because write_verify is a lib/ module, and lib/ modules are covered by this suite (see
test_plan_tools.py and the other 50-odd files here) - the suite that gates the repo.
"""
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import write_verify as V  # noqa: E402


# --- 1) the incident: retrospective prose is not a claim about this turn -----------------

INCIDENT_SENTENCE = ("The week I pushed earlier is still on the calendar and is correct")
INCIDENT_REPLY = (
    "The week I pushed earlier is still on the calendar and is correct; the failure was "
    "the overnight generator re-running against the old day rules."
)


def test_incident_sentence_raises_no_calendar_claim():
    """The verbatim sentence that produced two false messages to Jamie on 16 Aug."""
    assert V.claim_kinds(INCIDENT_SENTENCE) == set()


def test_incident_reply_raises_no_calendar_claim():
    assert V.claim_kinds(INCIDENT_REPLY) == set()


def test_incident_is_also_caught_by_the_tool_summary_gate():
    """Independent of the prose: that turn ran a file read and a local file write, so no
    external write was possible whatever the reply said."""
    assert V.tool_summary_kinds("Checked your data, saved your data") == set()
    assert V.claim_kinds("Pushed the whole week to your calendar.",
                         tool_summary="Checked your data, saved your data") == set()


@pytest.mark.parametrize("reply", [
    "That session is already on your calendar.",
    "Thursday's swim is still on the calendar from earlier.",
    "Your calendar remains as it was.",
    "I pushed that to your calendar yesterday.",
    "The ride I added previously is still there.",
    "Nothing has moved - the week stays on your calendar as it was.",
    "I already pushed the plan to intervals.icu, so nothing to do.",
    "As I said in my last message, I pushed the week to your calendar.",
    "The calendar is unchanged from last night's push.",
    # The counterpart to test_a_retracted_claim_and_a_fresh_one_in_one_sentence: same "and",
    # but nothing new is claimed after it, so the window must span the whole sentence.
    "I pushed the week to your calendar and it's still there.",
])
def test_retrospective_calendar_prose_raises_no_claim(reply):
    assert "icu" not in V.claim_kinds(reply)


@pytest.mark.parametrize("reply", [
    "The description I wrote to Strava yesterday is still there.",
    "I already updated the Strava description for that ride.",
    "The Strava write-up I added earlier is unchanged.",
])
def test_retrospective_strava_prose_raises_no_claim(reply):
    assert "strava" not in V.claim_kinds(reply)


# --- 2) genuine fresh writes must still be verified --------------------------------------
# This is the failure the fix must not cause. Over-suppression is silent: verification just
# stops happening, and the module exists precisely because an unverified claim is a lie
# waiting to be told.

@pytest.mark.parametrize("reply", [
    "Pushed the swim to your calendar for Thursday.",
    "Added Friday's ride to intervals.icu at 95 TSS.",
    "Moved Saturday's long run to Sunday on your calendar.",
    "Deleted the duplicate planned session from your calendar.",
    "Rescheduled the brick to Wednesday in intervals.icu.",
    "Shortened Thursday's workout to 45 minutes on the calendar.",
])
def test_fresh_calendar_claims_are_still_detected(reply):
    assert "icu" in V.claim_kinds(reply)


@pytest.mark.parametrize("reply", [
    "Updated the Strava description for Sunday's ride.",
    "Wrote the session notes to Strava.",
    "Pushed the write-up to Strava just now.",
])
def test_fresh_strava_claims_are_still_detected(reply):
    assert "strava" in V.claim_kinds(reply)


@pytest.mark.parametrize("reply", [
    # The bot reports duration and load in the same breath as a write, so a bare `still`
    # or `unchanged` marker would silence these genuine claims.
    "Pushed Thursday's swim to your calendar - still a 40 minute session.",
    "Moved Friday's ride to Saturday on your calendar, load unchanged.",
    "Added the brick to your calendar; it starts earlier than Tuesday's session.",
    "Moved Thursday's ride earlier in the day on your calendar.",
    # Possessives name a session, they do not date the action.
    "Moved yesterday's ride onto Thursday in your calendar.",
    "Pushed last week's missed swim to Thursday on your calendar.",
])
def test_markers_inside_a_genuine_claim_do_not_over_suppress(reply):
    assert "icu" in V.claim_kinds(reply)


def test_the_accepted_cost_of_the_bare_earlier_marker():
    """The known over-suppression, pinned so the cost stays visible rather than being
    rediscovered. "earlier" is excluded before in/than/to/at/start/finish, which covers
    "earlier in the day" and "earlier than Tuesday", but a bare adverbial "earlier"
    describing a time SHIFT reads as retrospective and silences a genuine write. Accepted:
    the loss is a silent skip, the alternative is a false accusation plus a model-driven
    write to a calendar that was already correct."""
    assert V.claim_kinds("Moved the ride 30 minutes earlier on your calendar.") == set()
    # The tool-derived gate is what recovers this case once it is wired: the tools that ran
    # prove the write, whatever the prose reads like.
    assert V.tool_summary_kinds("Updated intervals.icu") == {"icu"}


def test_a_retracted_claim_and_a_fresh_one_in_one_sentence():
    """Only the second half is a claim about this turn, and it must survive."""
    reply = ("The week I pushed earlier is still on the calendar and I've now added "
             "Thursday's swim to your calendar.")
    assert "icu" in V.claim_kinds(reply)


def test_a_fresh_claim_beside_an_aside_about_untouched_state():
    reply = "Pushed Thursday's swim to your calendar; Tuesday's ride is unchanged."
    assert "icu" in V.claim_kinds(reply)


def test_a_fresh_claim_in_a_later_sentence_of_a_retrospective_reply():
    reply = ("The week I pushed earlier is still on the calendar and is correct. "
             "I've moved Friday's ride to Saturday on your calendar.")
    assert V.claim_kinds(reply) == {"icu"}


# --- 3) the pre-existing forward-looking suppression is untouched ------------------------

@pytest.mark.parametrize("reply", [
    "Shall I push that to your calendar?",
    "Do you want me to move Friday's ride on the calendar?",
    "I can add Thursday's swim to intervals.icu if you like.",
    "I'll update the Strava description once the ride syncs.",
    "Would you like me to write that to Strava?",
    "I could push the whole week to your calendar.",
])
def test_forward_looking_language_still_raises_no_claim(reply):
    assert V.claim_kinds(reply) == set()


# --- 4) name-only Strava claims stay excluded (sailing hard rule) ------------------------

def test_a_rename_only_claim_is_still_not_a_description_claim():
    assert "strava" not in V.claim_kinds("Renamed the activity on Strava to Saturday Sail.")


def test_a_rename_plus_description_claim_is_still_a_description_claim():
    assert "strava" in V.claim_kinds("Renamed it and updated the Strava description.")


# --- 5) the tool-derived gate ------------------------------------------------------------

def test_local_only_tools_rule_out_every_external_write():
    assert V.tool_summary_kinds("Checked your session log, saved your preference") == set()


def test_no_tools_at_all_rules_out_every_external_write():
    assert V.tool_summary_kinds("Thought for 8s") == set()


def test_a_real_calendar_write_is_visible_in_the_summary():
    assert V.tool_summary_kinds(
        "Checked your activities, updated intervals.icu") == {"icu"}


def test_a_real_strava_write_is_visible_in_the_summary():
    assert V.tool_summary_kinds("Read your plan, updated it on Strava") == {"strava"}


def test_plan_generation_counts_as_a_calendar_write():
    """generate-plan and render-workout both push to intervals.icu, so neither may be
    treated as local - that would disable verification on the biggest writes there are."""
    assert V.tool_summary_kinds("Rebuilt your plan") == {"icu"}
    assert V.tool_summary_kinds("Wrote the workout") == {"icu"}


@pytest.mark.parametrize("summary", [
    None,
    "",
    "Crunched the numbers",              # any unmapped tool, including a bare Bash command
    "Checked intervals.icu",             # unrecognised icu_fetch subcommand
    "Built your race plan",              # may push
    "Synced your log",                   # may push
    "Checked your data, danced a jig",   # bot._classify_tool wording drifted
])
def test_an_unsettled_summary_fails_open(summary):
    """None means "cannot rule a write out", and the caller must fall back to the prose.
    Drift in bot._classify_tool's wording must degrade to today's behaviour, not to
    silence."""
    assert V.tool_summary_kinds(summary) is None
    assert V.claim_kinds("Pushed the swim to your calendar.",
                         tool_summary=summary) == {"icu"}


def test_the_gate_narrows_rather_than_widens():
    """A tool that ran does not manufacture a claim the reply never made."""
    assert V.claim_kinds("Here's how your week looks.",
                         tool_summary="Updated intervals.icu") == set()


def test_the_gate_is_case_insensitive():
    """bot.py upper-cases the first character of the collapse line."""
    assert V.tool_summary_kinds("Updated intervals.icu") == {"icu"}
    assert V.tool_summary_kinds("updated intervals.icu") == {"icu"}


def test_the_gate_drops_only_the_kind_the_tools_rule_out():
    reply = ("Updated the Strava description for Sunday's ride and pushed Thursday's swim "
             "to your calendar.")
    assert V.claim_kinds(reply) == {"icu", "strava"}
    assert V.claim_kinds(reply, tool_summary="Updated it on Strava") == {"strava"}


# --- 6) the athlete-facing failure copy --------------------------------------------------

def test_the_default_failure_line_does_not_assert_what_it_cannot_prove():
    """bot.py folds an "unknown" read-back in with a proved "absent", so the unqualified
    line must hold under both. "Treat your calendar as unchanged" does not."""
    for kind in ("strava", "icu"):
        line = V.result_line(kind, False)
        assert "couldn't confirm" in line
        assert "Treat your calendar as unchanged" not in line
        assert "Nothing was written" not in line


def test_a_proved_failure_still_gets_the_absolute_wording():
    assert "Nothing was written" in V.result_line("strava", False, verdict="absent")
    assert "Treat your calendar as unchanged" in V.result_line("icu", False, verdict="absent")
    assert "Nothing was written" in V.result_line("strava", False, verdict="unchanged")


def test_the_success_line_is_unchanged():
    assert V.result_line("icu", True) == "Saved to your calendar this time."
    assert V.result_line("strava", True) == "Saved to Strava this time."


def test_an_unknown_kind_still_gets_a_sentence():
    assert V.result_line("something", False)
    assert V.result_line("something", False, verdict="absent")
    assert V.result_line("something", True)


# --- 7) inputs that must never raise -----------------------------------------------------

@pytest.mark.parametrize("reply", [None, "", "   ", "\n\n"])
def test_empty_replies_claim_nothing(reply):
    assert V.claim_kinds(reply) == set()
    assert V.claim_kinds(reply, tool_summary="Updated intervals.icu") == set()
