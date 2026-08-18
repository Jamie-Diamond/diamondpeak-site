"""Claim detection, the tool-summary gate and the failure copy in lib/write_verify.py.

WHAT BROKE (16 Aug 2026, 21:57). Jamie asked for a bug to be logged. The reply reassured
him about a week pushed in an EARLIER turn - "The week I pushed earlier is still on the
calendar and is correct" - and _ICU_CLAIM_RE read that as a fresh calendar write. No write
had been attempted: the turn's own tool summary was "Checked your data, saved your data", a
file read and a local file write. The verifier found no new events, said so, "retried"
(which re-asks the model to PERFORM the write, so the guard can mutate a calendar that was
already right) and appended two false lines to the reply: "That didn't actually save to
your calendar" and "Treat your calendar as unchanged". His calendar was correct throughout.

TWO INDEPENDENT DEFENCES, and the tests below are organised around keeping both:

  1. THE PROSE. _RETRO_RE / _asserts_now read the tense of the claim. This is the one that
     is live in production and the one that works on every caller, including the voice path
     and the non-streaming fallback, neither of which can supply a tool summary. Section 1
     is its regression suite.
  2. THE TOOLS. The bot already knows which tools it invoked, and no regex over English can
     match that evidence. Sections 5 to 8. It is the SECOND line, not a replacement: every
     gate test that suppresses has a sibling proving the prose alone would have caught it.

Plus, after the retry, the same failure on the other side of the fence: reading "I could not
check" as "it failed" (sections 9 and 10).

Note the direction of risk in every test here. A gate that fails open costs a verification
we would have liked to run. A gate that wrongly suppresses costs a silent skip. A gate that
wrongly SPEAKS costs Jamie's trust in every confirmation the bot has ever given him, and
that is the only one of the three that does not heal.

The verdict maths is covered by scripts/test_coach_facts.py, which runs on the VM before a
deploy. This is a pytest file because write_verify is a lib/ module, and lib/ modules are
covered by this suite - the one that gates the repo.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import write_verify as V  # noqa: E402

BOT_PY = Path(__file__).resolve().parents[2] / "telegram/bot.py"

if str(BOT_PY.parent) not in sys.path:
    sys.path.insert(0, str(BOT_PY.parent))
# A guarded import and a skipif on each CLASS that needs it, not pytest.importorskip.
# bot.py is the transport and pulls in the whole telegram side, so it is the plausible
# thing to become unimportable in a bare checkout - and importorskip at module scope raises
# during COLLECTION, which skips the entire file. That would silently stop the ~100 pure
# tests here from gating the repo, which is the exact failure this module exists to
# prevent. Imported at the top rather than beside section 12 (moved 17 Aug 2026) because
# section 8 now calls _classify_tool for real: source-reading alone could prove where the
# branches SIT but never what they RETURN, and the compound-command hole was a bug about
# what they return.
try:
    import bot                                     # noqa: E402
except Exception:                                  # pragma: no cover - environment only
    bot = None

# The exact reply that caused the incident, and the exact collapse summary of that turn:
# a Read and a Write of Jamie's own files, no network write anywhere.
INCIDENT_SENTENCE = "The week I pushed earlier is still on the calendar and is correct"
INCIDENT_REPLY = (
    "The week I pushed earlier is still on the calendar and is correct; the failure was "
    "the overnight generator re-running against the old day rules."
)
INCIDENT_SUMMARY = "Checked your data, saved your data"
# A genuine fresh claim, used wherever a test needs the prose to raise a claim so that the
# GATE's behaviour is what is under test.
FRESH_CLAIM = "Pushed Friday's swim to your calendar."


# --- 1) the prose defence: retrospective prose is not a claim about this turn ------------

def test_incident_sentence_raises_no_calendar_claim():
    """The verbatim sentence that produced two false messages to Jamie on 16 Aug."""
    assert V.claim_kinds(INCIDENT_SENTENCE) == set()


def test_incident_reply_raises_no_calendar_claim():
    assert V.claim_kinds(INCIDENT_REPLY) == set()


def test_the_prose_alone_catches_the_incident_with_no_tool_summary_at_all():
    """DEFENCE IN DEPTH, stated as its own test because it is the property most likely to
    be lost in a future refactor. Every caller that cannot build a tool summary - the voice
    path, the non-streaming fallback, the capture-retry path - reaches claim_kinds with
    tool_summary=None, and must still be protected."""
    assert V.claim_kinds(INCIDENT_REPLY, tool_summary=None) == set()
    assert V.claim_kinds(INCIDENT_SENTENCE, tool_summary=None) == set()


def test_the_gate_catches_the_incident_independently_of_the_prose():
    """And the other way round: that turn ran a file read and a local file write, so no
    external write was possible whatever the reply said."""
    assert V.tool_summary_kinds(INCIDENT_SUMMARY) == set()
    assert V.claim_kinds(FRESH_CLAIM, tool_summary=INCIDENT_SUMMARY) == set()


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
    # The gate is what recovers this case: the tools that ran prove the write, whatever the
    # prose reads like. It does not un-suppress the claim (the gate only ever subtracts),
    # but it does mean the evidence is on record for anyone reading the log.
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


def test_a_reply_claiming_both_writes_reports_both():
    reply = ("Updated the Strava description for Sunday's ride and pushed Thursday's swim "
             "to your calendar.")
    assert V.claim_kinds(reply) == {"icu", "strava"}


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


# --- 5) the gate suppresses only on proof ------------------------------------------------

class TestTheGateSuppressesOnlyOnProof:

    def test_every_fragment_local_means_no_external_write_happened(self):
        assert V.tool_summary_kinds("Checked your wellness, read your plan") == set()
        assert V.tool_summary_kinds("Checked your session log, saved your preference") == set()

    def test_a_zero_tool_turn_cannot_have_written_anything(self):
        # "Thought for Ns" is bot.call_claude_streaming's collapse line when no tool ran at
        # all. The purest form of the incident: a write claimed on a turn that invoked
        # nothing.
        for s in ("Thought for 7s", "Thought for 143s", "thought for 1s", "Thought for 8 s"):
            assert V.tool_summary_kinds(s) == set(), s
        assert V.claim_kinds(FRESH_CLAIM, tool_summary="Thought for 7s") == set()

    def test_an_icu_write_fragment_is_reported_as_an_icu_write(self):
        assert V.tool_summary_kinds("Checked your activities, updated intervals.icu") == {"icu"}

    def test_a_strava_write_fragment_is_reported_as_a_strava_write(self):
        assert V.tool_summary_kinds("Read your fitness, updated it on Strava") == {"strava"}

    def test_a_summary_can_carry_both_kinds(self):
        assert V.tool_summary_kinds(
            "Updated it on Strava, updated intervals.icu") == {"strava", "icu"}

    def test_plan_generation_counts_as_a_calendar_write(self):
        """generate-blueprint.py:340 calls IcuClient.push_workout, so a plan rebuild really
        does write the week to the calendar. Treating it as local would disable
        verification on the biggest writes there are."""
        assert V.tool_summary_kinds("Rebuilt your plan") == {"icu"}

    def test_log_strength_counts_as_an_icu_write(self):
        """A real trap: the status line reads like a local note, but plan_tools
        log-strength (lib/plan_tools.py:1266) calls IcuClient.create_manual_activity."""
        assert V.tool_summary_kinds("Logged your strength work") == {"icu"}

    def test_render_workout_is_never_treated_as_proof_that_nothing_was_written(self):
        """The subcommand really is a pure renderer (lib/plan_tools.py:1276 returns a
        description plus a how_to_push note; primitives/planned_tss.py:348 says the result
        is pushed separately). It is listed as a write anyway, so the gate fails open: the
        athlete is told "Writing the workout to intervals.icu" and the model is instructed
        to follow it with push_workout. Whichever reading you take, it must not suppress."""
        assert V.tool_summary_kinds("Wrote the workout") != set()


# --- 6) everything the gate cannot settle falls back to the prose ------------------------

class TestTheGateFailsOpen:

    @pytest.mark.parametrize("summary", [
        None,
        "",
        "   ",
        "Crunched the numbers",              # any unmapped tool, incl. a bare Bash command
        "Checked intervals.icu",             # unrecognised icu_fetch subcommand
        "Crunched your plan",                # unrecognised plan_tools subcommand
        "Built your race plan",              # may push
        "Synced your log",                   # may push
        "Checked your data, danced a jig",   # bot._classify_tool wording drifted
    ])
    def test_an_unsettled_summary_leaves_the_prose_claim_standing(self, summary):
        """None means "cannot rule a write out", and the caller must fall back to the
        prose. Drift in bot._classify_tool's wording must degrade to today's behaviour,
        not to silence."""
        assert V.tool_summary_kinds(summary) is None
        assert V.claim_kinds(FRESH_CLAIM, tool_summary=summary) == {"icu"}

    def test_one_unknown_fragment_poisons_an_otherwise_local_summary(self):
        # The whole point of splitting on the comma rather than substring-searching the
        # line: "all fragments local" and "one local, one unknown" must not collapse
        # together.
        assert V.tool_summary_kinds("Read your plan") == set()
        assert V.tool_summary_kinds("Read your plan, did something new") is None

    def test_a_write_fragment_wins_over_an_unrecognised_one(self):
        # Both fail open, so this is about the honesty of the reported value, not about
        # behaviour: a summary that names a writer should say WHICH, for the log.
        assert V.tool_summary_kinds("Updated intervals.icu, did something new") == {"icu"}

    def test_a_write_fragment_never_suppresses_even_beside_local_ones(self):
        assert V.claim_kinds(
            FRESH_CLAIM,
            tool_summary="Checked your data, updated intervals.icu, saved your data",
        ) == {"icu"}

    def test_the_gate_never_intersects_kinds_with_the_prose(self):
        # A Strava-only tool run beside an ICU prose claim must NOT suppress the ICU claim.
        # _classify_tool is coarse string matching over a command hint truncated to 80
        # chars, and an intersection would let one misclassified fragment silence a true
        # claim. Only set() ever suppresses.
        assert V.claim_kinds(FRESH_CLAIM, tool_summary="Updated it on Strava") == {"icu"}
        assert V.claim_kinds("Updated the Strava description for Sunday's ride.",
                             tool_summary="Updated intervals.icu") == {"strava"}

    def test_the_gate_cannot_invent_a_claim_the_prose_never_made(self):
        # A tool wrote, but the reply says nothing about it. The gate only ever subtracts.
        assert V.claim_kinds("Here's how your week looks.",
                             tool_summary="Updated intervals.icu") == set()
        assert V.claim_kinds("Nice work on that ride.",
                             tool_summary="Updated intervals.icu") == set()


# --- 7) parsing the collapse line --------------------------------------------------------

class TestSummaryParsing:

    def test_the_leading_capital_bot_adds_does_not_break_the_lookup(self):
        # bot builds ", ".join(pasts) then upper-cases character zero.
        assert V.tool_summary_kinds("Checked your data") == set()
        assert V.tool_summary_kinds("checked your data") == set()
        assert V.tool_summary_kinds("Updated intervals.icu") == {"icu"}
        assert V.tool_summary_kinds("updated intervals.icu") == {"icu"}

    def test_a_trailing_full_stop_does_not_break_the_lookup(self):
        assert V.tool_summary_kinds("Read your plan.") == set()
        assert V.tool_summary_kinds("Checked your data, saved your data.") == set()

    def test_capitalised_fragments_from_classify_tool_still_match(self):
        # _classify_tool emits "read the session Load" and "worked out your Load target"
        # with an internal capital. The lookup lower-cases, so the lists stay lower-case.
        assert V.tool_summary_kinds("Read the session Load") == set()
        assert V.tool_summary_kinds("Worked out your Load target, worked out the Load") == set()

    def test_a_non_string_summary_does_not_raise(self):
        for junk in (0, 12.5, [], {}, object()):
            V.tool_summary_kinds(junk)      # must not raise; the verdict is don't-care


# --- 8) the coupling to bot._classify_tool -----------------------------------------------

class TestCouplingToClassifyTool:
    """_TOOL_LOCAL / _TOOL_WRITES are coupled to bot._classify_tool by string literal.

    That is fragile and known to be. The trade was made because the alternative is a fourth
    element on every one of _classify_tool's ~40 return sites, for a guard that has to stay
    easy to reason about. These tests are the price of the trade: a reworded status line
    fails here rather than silently turning the gate into a no-op."""

    @staticmethod
    def _classify_tool_pasts():
        """Every past-tense fragment _classify_tool can return, read out of the source."""
        fn = [n for n in ast.walk(ast.parse(BOT_PY.read_text()))
              if isinstance(n, ast.FunctionDef) and n.name == "_classify_tool"]
        assert len(fn) == 1, "_classify_tool moved or was duplicated"
        out = set()
        for node in ast.walk(fn[0]):
            if (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
                    and len(node.value.elts) == 3):
                third = node.value.elts[2]
                if isinstance(third, ast.Constant) and isinstance(third.value, str):
                    out.add(third.value.lower())
        return out

    def test_every_literal_we_gate_on_is_still_a_fragment_bot_can_emit(self):
        live = self._classify_tool_pasts()
        assert live, "could not read any past-tense fragments out of _classify_tool"
        stale = sorted((set(V._TOOL_LOCAL) | set(V._TOOL_WRITES)) - live)
        assert not stale, (
            "these fragments are gated on but _classify_tool no longer emits them, so the "
            f"gate has silently stopped recognising them: {stale}")

    def test_the_two_incident_fragments_are_still_emitted_and_still_local(self):
        live = self._classify_tool_pasts()
        for frag in ("checked your data", "saved your data"):
            assert frag in live, f"_classify_tool no longer emits {frag!r}"
            assert frag in V._TOOL_LOCAL

    def test_a_new_unlisted_fragment_fails_open_rather_than_suppressing(self):
        # The safety property that makes the coupling tolerable: anything _classify_tool
        # gains that nobody adds here yields None, which is today's behaviour.
        for frag in self._classify_tool_pasts():
            if frag in V._TOOL_LOCAL or frag in V._TOOL_WRITES:
                continue
            assert V.tool_summary_kinds(frag) is None, frag

    def test_the_deliberately_omitted_fallbacks_are_still_omitted(self):
        # Branch fallbacks reached by a subcommand or endpoint nobody enumerated, or whole
        # scripts not audited for a push. Adding any of them to _TOOL_LOCAL is the single
        # edit that could make this gate harmful.
        for frag in ("crunched the numbers", "crunched your plan", "checked intervals.icu",
                     "synced your log", "built your race plan"):
            assert frag not in V._TOOL_LOCAL, frag
            assert V.tool_summary_kinds(frag) is None, frag

    def test_the_two_fragments_that_look_local_but_write(self):
        # Both audited to source. "logged your strength work" is the dangerous one: it
        # reads exactly like a local note.
        for frag in ("logged your strength work", "wrote the workout"):
            assert frag not in V._TOOL_LOCAL, frag
            assert V.tool_summary_kinds(frag) == {"icu"}, frag

    def test_local_means_no_external_write_not_no_network(self):
        # The intervals.icu READ fragments are local on purpose, as is the heat script
        # (scripts/heat_accl.py reads the heat log and lib/heat.py's only network calls are
        # open-meteo GETs). A reader who takes LOCAL to mean "offline" will strip these and
        # quietly halve the gate's coverage.
        for frag in ("checked your wellness", "read your fitness", "read the session detail",
                     "checked your activities", "checked your heat dose"):
            assert frag in V._TOOL_LOCAL, frag

    def test_the_local_fragment_at_the_heart_of_the_hazard_is_still_local(self):
        """"Built the session" really IS local, and stays gated as such. The compound-command
        bug (fixed below) was never that this fragment is wrong; it was that a chained
        command which also pushed to the calendar EMITTED it."""
        assert V.tool_summary_kinds("Built the session") == set()
        # The prose defence, which carried this case on its own while the ordering hole was
        # open, and which still has to hold: a RETROSPECTIVE claim is caught by section 1
        # whatever the gate says.
        assert V.claim_kinds(INCIDENT_REPLY, tool_summary="Built the session") == set()


# --- 8b) compound Bash commands: a write marker anywhere wins ----------------------------
# THE BUG (found 17 Aug 2026, fixed 17 Aug 2026). bot._classify_tool was first-match-wins
# over one flat blob and tested "plan_tools" ahead of the intervals.icu branch, so a single
# chained command that calculated AND pushed collapsed to the local fragment "built the
# session". The gate would then have suppressed a claim of a calendar write that really
# happened. It is pinned here by CALLING the classifier, not by reading the source: the old
# test asserted only where the branches sat, and a bug about what a function returns needs a
# test about what it returns.

@pytest.mark.skipif(bot is None,
                    reason="telegram/bot.py is not importable in this environment; the "
                           "pure write_verify tests above still run and still gate")
class TestACompoundBashCommandCannotHideItsWrite:

    # (marker, the past-tense fragment it must force). These are the five write markers
    # lifted into _classify_tool's pre-pass, one per key of _TOOL_WRITES.
    MARKERS = [
        ("icu_fetch.py push_workout --athlete jamie", "updated intervals.icu"),
        ("icu_fetch.py edit_workout --id 1", "updated intervals.icu"),
        ("icu_fetch.py delete_workout --id 1", "updated intervals.icu"),
        ("icu_fetch.py update_activity --id 1", "updated intervals.icu"),
        ("scripts/strava-update-activity.py --icu-id i1", "updated it on Strava"),
        ("scripts/generate-blueprint.py --athlete jamie", "rebuilt your plan"),
        ("scripts/stage1-plan.py --athlete jamie", "rebuilt your plan"),
        ("plan_tools.py log-strength --athlete jamie", "logged your strength work"),
        ("plan_tools.py render-workout --athlete jamie", "wrote the workout"),
    ]
    # The local command the write used to hide behind, and its fragment.
    LOCAL = "plan_tools.py session-for-load --athlete jamie"

    @pytest.mark.parametrize("joiner", [" && ", "; ", " | ", " || "])
    @pytest.mark.parametrize("write_cmd,fragment", MARKERS)
    def test_the_write_wins_whichever_segment_it_sits_in(self, write_cmd, fragment, joiner):
        """Both orders, because first-match-wins was the whole bug: a fix that only handled
        `write && local` would leave the original incident shape (`local && write`) open."""
        for hint in (self.LOCAL + joiner + write_cmd, write_cmd + joiner + self.LOCAL):
            got = bot._classify_tool("Bash", hint)[2]
            assert got == fragment, f"{hint!r} classified as {got!r}"
            assert V.tool_summary_kinds(got), (
                f"{hint!r} yields {got!r}, which does not disarm the suppressing gate")

    def test_the_incident_shape_no_longer_suppresses_a_true_claim(self):
        """The concrete case the old test accepted as a limitation. A genuinely fresh claim
        of a calendar write, on a turn whose one chained command really did push."""
        hint = "python3 lib/plan_tools.py session-for-load && icu_fetch.py push_workout"
        frag = bot._classify_tool("Bash", hint)[2]
        summary = frag[0].upper() + frag[1:]      # exactly how bot builds the collapse line
        assert V.tool_summary_kinds(summary) == {"icu"}
        assert V.claim_kinds(FRESH_CLAIM, tool_summary=summary) == {"icu"}

    def test_every_write_the_gate_knows_about_has_a_marker(self):
        """Self-maintaining, and the point of the whole exercise. A future _TOOL_WRITES
        entry with no marker in _classify_tool's pre-pass silently reopens the hole for
        that destination, so it fails here instead."""
        # Lower-cased on both sides: _TOOL_WRITES is keyed lower-case because
        # tool_summary_kinds folds case, while _classify_tool returns display text
        # ("updated it on Strava").
        covered = {frag.lower() for _cmd, frag in self.MARKERS}
        assert covered == set(V._TOOL_WRITES), (
            "these gated writes have no compound-command marker, so a chained command can "
            f"still hide them: {sorted(set(V._TOOL_WRITES) - covered)}")

    def test_claudes_own_file_tools_are_not_dragged_into_a_write(self):
        """The name gate, which is correctness and not tidiness. _TOOL_LOCAL calls the
        read/grep/glob branches its strongest proof BECAUSE the tool name forecloses the
        network, so a marker sitting in a search pattern must not override it."""
        for name in ("Read", "Grep", "Glob", "Write", "Edit", "MultiEdit"):
            got = bot._classify_tool(name, "render-workout")[2]
            assert V.tool_summary_kinds(got) == set(), f"{name}: {got!r}"

    def test_a_plain_local_command_is_untouched(self):
        """The fix must not make everything look like a write: that would cost the gate its
        entire suppressing power, which is the other way to break it."""
        for hint, frag in [(self.LOCAL, "built the session"),
                           ("plan_tools.py week-tss --athlete jamie", "added up your week"),
                           ("icu_fetch.py --endpoint wellness", "checked your wellness")]:
            assert bot._classify_tool("Bash", hint)[2] == frag
            assert V.tool_summary_kinds(frag) == set()

    def test_the_residual_truncation_hole_is_pinned_not_claimed_closed(self):
        """WHAT IS STILL OPEN (17 Aug 2026), and it is NOT this file's to fix.

        engine._tool_input_summary (lib/engine.py) truncates the hint to 80 characters
        before _classify_tool ever sees it. The realistic incident command is 99, so
        `push_workout` is cut off upstream and the fragment is local again.

        The rule that survives is CHARACTER OFFSET, not segment position - see the sibling
        test below, where a marker in the FIRST segment is lost behind a long absolute
        path. Segment order no longer matters at all; a marker is caught iff it lands
        inside the first 80 characters. This test asserts the hole is exactly that shape,
        so the day engine.py stops truncating mid-chain it fails and gets deleted rather
        than sitting here as a stale warning."""
        full = ("python3 lib/plan_tools.py session-for-load --athlete jamie && "
                "python3 lib/icu_fetch.py push_workout")
        assert len(full) > 80, "the sample command no longer overruns the truncation"
        assert bot._classify_tool("Bash", full)[2] == "updated intervals.icu"
        # ... and the same command as engine actually delivers it.
        assert "push_workout" not in full[:80]
        assert bot._classify_tool("Bash", full[:80])[2] == "built the session"
        # Survivable for the same reason it always was: the prose defence is independent and
        # does not read the tool summary at all.
        assert V.claim_kinds(FRESH_CLAIM) == {"icu"}

    def test_reversing_the_long_chain_does_survive_the_truncation(self):
        """The other half of the honest statement: put the write inside the first 80
        characters and the cut cannot reach it, however long the rest of the chain is."""
        full = ("python3 lib/icu_fetch.py push_workout --athlete jamie && "
                "python3 lib/plan_tools.py session-for-load --athlete jamie")
        assert len(full) > 80
        assert bot._classify_tool("Bash", full[:80])[2] == "updated intervals.icu"

    def test_the_residual_is_character_offset_and_not_segment_position(self):
        """The correction that stops the pin above being read too kindly (17 Aug 2026).

        "a marker in a late segment" is the intuitive statement of the residual and it is
        WRONG, which would leave somebody believing a single un-chained command is always
        safe. It is not: the marker below sits in the FIRST and only segment, at index 99,
        behind an absolute path of the length this repo actually uses, and the truncation
        eats it just the same. Asserted here so the residual is pinned as the one condition
        it really is - the marker must land inside the first 80 characters."""
        one_segment = ("python3 /Users/diamondpeakconsulting/conductor/repos/"
                       "diamondpeak-site/ClaudeCoach/lib/icu_fetch.py push_workout "
                       "--athlete jamie")
        assert "&&" not in one_segment and ";" not in one_segment
        assert one_segment.index("push_workout") > 80
        assert bot._classify_tool("Bash", one_segment)[2] == "updated intervals.icu"
        assert V.tool_summary_kinds(
            bot._classify_tool("Bash", one_segment[:80])[2]) != {"icu"}


# --- 9) the bot wiring -------------------------------------------------------------------

class TestBotWiring:
    """Source-level, because the invariant IS about where the code sits."""

    @staticmethod
    def _src():
        return BOT_PY.read_text()

    def test_exactly_two_call_sites_and_exactly_one_is_gated(self):
        src = self._src()
        assert len(re.findall(r"extra = _verify_external_writes\(", src)) == 2
        assert len(re.findall(r"tool_summary=gate_summary", src)) == 1

    def test_the_gated_call_site_comes_after_the_gate_summary_is_bound(self):
        """The voice path's `summary` is not merely unset there, it is unbindable: it is
        first assigned further down the same function, which makes it a function-local, so
        naming it at the voice site raises UnboundLocalError. Asserting the ORDER rather
        than just the absence means a future reorder fails this test instead of shipping a
        crash."""
        src = self._src()
        summary_bound_at = src.index("        summary = None\n")
        gate_bound_at = src.index("        gate_summary = summary if clean ==")
        gated_at = src.index("tool_summary=gate_summary")
        assert summary_bound_at < gate_bound_at < gated_at, (
            "the gated call site now appears BEFORE its inputs are executed, which makes "
            "it an UnboundLocalError at runtime, not a silent no-op")

    def test_the_voice_call_site_passes_no_tool_summary(self):
        src = self._src()
        voice_at = src.index('log(f"[{slug}] Out (voice):')
        call_at = src.index("extra = _verify_external_writes(", voice_at)
        end = src.index("history.append(", call_at)
        assert "tool_summary" not in src[call_at:end], (
            "the voice path has no tool summary to give and `summary` is unbindable there")

    def test_the_helper_defaults_tool_summary_to_none(self):
        # So the non-streaming fallback and the voice path keep today's behaviour without
        # either call site having to say so.
        assert "tool_summary=None) -> str:" in self._src()

    def test_the_gate_summary_is_dropped_when_the_reply_text_is_replaced(self):
        """THE CAPTURE-RETRY HOLE, fixed rather than accepted (17 Aug 2026).

        _verify_logged_reply can REPLACE `clean` with text from a SECOND call_claude (the
        capture retry), whose tools never appear in `summary`. That replacement text can
        itself claim an external write, and gating it on the first call's summary could
        suppress a claim that is verified correctly today. So the summary stops counting as
        evidence the moment it stops describing the text."""
        src = self._src()
        assert "pre_capture_guard = clean" in src
        assert "gate_summary = summary if clean == pre_capture_guard else None" in src
        # The overlap is real, not hypothetical: this is text the capture retry can return
        # (it matches bot._CAPTURE_CONFIRM_RE and is under 200 chars) and it claims a write.
        overlap = "Noted - and I pushed Friday swim to your calendar."
        assert re.match(r"^\W*(logged|saved|noted|recorded|captured)\b", overlap, re.I)
        assert len(overlap) <= 200
        assert V.claim_kinds(overlap) == {"icu"}
        # With the fix that text reaches the verifier ungated, so it is still checked.
        assert V.claim_kinds(overlap, tool_summary=None) == {"icu"}

    def test_the_incident_reply_is_still_gated_despite_that_fix(self):
        """The fix must not cost the gate its main case. The 872-char incident reply cannot
        match _CAPTURE_CONFIRM_RE (it starts with "The", and is far over the 140-char
        anchor), so _verify_logged_reply returns it untouched and gate_summary keeps the
        real summary."""
        assert not re.match(r"^\W*(logged|saved|noted|recorded|captured)\b",
                            INCIDENT_REPLY, re.I)
        assert V.claim_kinds(INCIDENT_REPLY, tool_summary=INCIDENT_SUMMARY) == set()


class TestBotDoesNotFoldUnknownInWithAbsent:

    @staticmethod
    def _code_lines():
        """bot.py with comment-only lines dropped. The regression comment in bot.py QUOTES
        the offending old line verbatim, as such comments should, so a naive substring
        search over the whole file matches the explanation and not the code."""
        return [ln for ln in BOT_PY.read_text().splitlines()
                if not ln.lstrip().startswith("#")]

    def test_the_calendar_branch_captures_the_verdict_before_reducing_it_to_a_bool(self):
        offending = 'ok = _verify_icu_calendar_claim(slug) == "ok"'
        assert not [ln for ln in self._code_lines() if offending in ln], (
            "the post-retry verdict is being discarded again, so an unknown reads as a "
            "proved absence and the athlete gets accused on a hiccup")
        assert any("post = _verify_icu_calendar_claim(slug)" in ln
                   for ln in self._code_lines())

    def test_the_calendar_branch_passes_its_verdict_to_the_copy(self):
        assert "verdict=post" in BOT_PY.read_text()

    def test_the_strava_branch_keeps_its_verdict_too(self):
        """WAS the scope-out, now the fix (17 Aug 2026). This test used to assert the
        opposite - that the Strava branch was deliberately left on a bool and therefore
        hedged on everything. That was honest but incomplete: the same False carried a
        proved absence and an unproved one, so a Strava failure we HAD established was
        reported as one we could not check. The branch now keeps the verdict, like the
        calendar branch, and the unproved paths say "unknown" rather than fabricating an
        absence."""
        lines = self._code_lines()
        assert any("ok, post = _retry_strava_description(slug, icu_id, desc)" in ln
                   for ln in lines), (
            "the Strava retry's verdict is being discarded again, so a proved absence and "
            "a failed read-back are indistinguishable at the copy and the alert")
        assert not any("ok = _retry_strava_description(" in ln for ln in lines)
        # The default still hedges: a caller with no verdict has proved nothing, and that
        # rule is what made the old scope-out safe. It has to survive the fix.
        assert "Nothing was written" not in V.result_line("strava", False)

    def test_the_unproved_paths_of_the_strava_retry_say_unknown_not_absent(self):
        """Source-level companion to the behavioural tests in section 12. The two paths
        named when this was found - the read-back failing and the 120s subprocess timeout -
        must not reach strava_desc_verdict at all, because feeding it a description it
        never received scores "absent" on nothing."""
        src = BOT_PY.read_text()
        fn = src[src.index("def _retry_strava_description("):
                 src.index("def _icu_client(")]
        body = [ln for ln in fn.splitlines() if not ln.lstrip().startswith("#")]
        assert any("if desc is None:" in ln for ln in body), (
            "the read-back's None is no longer short-circuited, so it will be scored as a "
            "proved absence again")
        assert sum('return (False, "unknown")' in ln for ln in body) == 3
        assert not any('return (False, "absent")' in ln for ln in body)


# --- 10) the athlete-facing failure copy -------------------------------------------------

class TestTheFailureCopyNeverOverstatesItsEvidence:

    def test_the_default_failure_line_does_not_assert_what_it_cannot_prove(self):
        """Reached by the Strava branch, whose bool folds a failed read-back in with a
        proved absence. The unqualified line has to hold under both readings."""
        for kind in ("strava", "icu"):
            line = V.result_line(kind, False)
            assert "couldn't confirm" in line
            assert "Treat your calendar as unchanged" not in line
            assert "Nothing was written" not in line

    def test_an_explicit_unknown_verdict_does_not_claim_the_write_failed(self):
        for kind in ("strava", "icu"):
            line = V.result_line(kind, False, verdict="unknown")
            assert "Treat your calendar as unchanged" not in line
            assert "Nothing was written" not in line

    def test_an_unknown_verdict_does_not_claim_the_write_succeeded_either(self):
        line = V.result_line("icu", False, verdict="unknown")
        assert line != V._RESULT_OK["icu"]
        assert "Saved to your calendar" not in line

    def test_an_unproved_failure_says_plainly_that_it_could_not_check(self):
        for kind in ("icu", "strava"):
            for verdict in (None, "unknown"):
                line = V.result_line(kind, False, verdict=verdict)
                assert "couldn't" in line.lower() or "could not" in line.lower(), line

    def test_a_proved_failure_still_gets_the_absolute_wording(self):
        # The strong wording is correct when the failure really was established, and
        # weakening it everywhere would be its own dishonesty.
        for verdict in ("absent", "unchanged"):
            assert "Nothing was written" in V.result_line("strava", False, verdict=verdict)
            assert "Treat your calendar as unchanged" in V.result_line(
                "icu", False, verdict=verdict)

    def test_the_success_line_is_unchanged_and_ignores_the_verdict(self):
        for verdict in (None, "ok", "unknown", "absent"):
            assert V.result_line("icu", True, verdict=verdict) == \
                "Saved to your calendar this time."
            assert V.result_line("strava", True, verdict=verdict) == \
                "Saved to Strava this time."

    def test_every_failure_line_is_one_line(self):
        # Athlete-facing, each sent as its own Telegram message.
        for kind in ("icu", "strava"):
            for verdict in (None, "unknown", "absent", "unchanged"):
                assert "\n" not in V.result_line(kind, False, verdict=verdict)


@pytest.mark.parametrize("verdict", ["absent", "unchanged", "unknown", "ok", None])
def test_result_line_is_total_over_every_verdict_it_can_see(verdict):
    """Never raise on the athlete-facing path, whatever the verdict turns out to be."""
    for kind in ("icu", "strava", "something else"):
        for ok in (True, False):
            assert isinstance(V.result_line(kind, ok, verdict=verdict), str)
            assert V.result_line(kind, ok, verdict=verdict)


# --- 11) inputs that must never raise ----------------------------------------------------

@pytest.mark.parametrize("reply", [None, "", "   ", "\n\n"])
def test_empty_replies_claim_nothing(reply):
    assert V.claim_kinds(reply) == set()
    assert V.claim_kinds(reply, tool_summary="Updated intervals.icu") == set()
    assert V.claim_kinds(reply, tool_summary=INCIDENT_SUMMARY) == set()


# --- 12) end to end through bot._verify_external_writes ----------------------------------
# The unit tests above prove the parts. This drives the real function with the network and
# Telegram stubbed, because the thing that actually went wrong on 16 Aug was two messages
# being SENT, and "sends nothing" is the property Jamie cares about.

@pytest.mark.skipif(bot is None,
                    reason="telegram/bot.py is not importable in this environment; the "
                           "pure write_verify tests above still run and still gate")
class TestEndToEndThroughVerifyExternalWrites:

    @staticmethod
    def _run(monkeypatch, reply, tool_summary, calendar_verdicts,
             alerts=None, strava_claim=None, strava_retry=None):
        """Drive _verify_external_writes with every side effect captured.

        `calendar_verdicts` is the sequence _verify_icu_calendar_claim returns, so a test
        can set the pre-retry verdict and the post-retry verdict independently. Returns
        (messages_sent, retries_invoked, appended_transcript).

        `alerts`, `strava_claim` and `strava_retry` were added 17 Aug 2026 when the Strava
        branch started carrying a verdict: the ops alert is the thing that branch most
        obviously got wrong (it hedged on proved failures too), and an alert nobody captures
        is an alert nobody can assert on. All three are optional so the calendar tests above
        read exactly as they did."""
        sent, calls = [], {"retry": 0, "verdicts": list(calendar_verdicts)}
        monkeypatch.setattr(bot, "send",
                            lambda *a, **k: sent.append(a[2]) or 1)
        monkeypatch.setattr(bot, "log", lambda *a, **k: None)
        monkeypatch.setattr(
            bot.ops_log, "alert",
            lambda *a, **k: (alerts.append(a[1]) if alerts is not None else None))
        monkeypatch.setattr(bot, "_verify_icu_calendar_claim",
                            lambda slug: calls["verdicts"].pop(0))
        monkeypatch.setattr(bot, "_verify_strava_claim",
                            lambda slug, reply: strava_claim or ("unknown", None, None))
        if strava_retry is not None:
            monkeypatch.setattr(bot, "_retry_strava_description",
                                lambda slug, icu_id, desc: strava_retry)

        def _retry():
            calls["retry"] += 1

        appended = bot._verify_external_writes(
            "token", 1, "jamie", reply, icu_retry=_retry, tool_summary=tool_summary)
        return sent, calls["retry"], appended

    def test_the_incident_turn_sends_nothing_and_never_retries(self, monkeypatch):
        """The real reply plus the real summary of that turn. Nothing goes to Jamie, and
        the calendar-mutating retry is never invoked."""
        sent, retries, appended = self._run(
            monkeypatch, INCIDENT_REPLY, INCIDENT_SUMMARY, ["absent", "absent"])
        assert sent == []
        assert retries == 0
        assert appended == ""

    def test_the_incident_turn_sends_nothing_with_no_summary_either(self, monkeypatch):
        """DEFENCE IN DEPTH. Same reply, tool_summary=None, so the gate cannot help: the
        prose fix alone has to catch it. This is the assertion that proves the two defences
        are independent."""
        sent, retries, appended = self._run(
            monkeypatch, INCIDENT_REPLY, None, ["absent", "absent"])
        assert sent == []
        assert retries == 0
        assert appended == ""

    def test_a_genuine_absent_claim_still_speaks_retries_and_reports(self, monkeypatch):
        """The guard must still do its job. absent -> retry -> absent is the proved case."""
        sent, retries, _ = self._run(
            monkeypatch, FRESH_CLAIM, "Checked your data, updated intervals.icu",
            ["absent", "absent"])
        assert retries == 1
        assert sent == ["That didn't actually save to your calendar. Retrying now.",
                        "Still not saving to your calendar. I've logged it for a fix. "
                        "Treat your calendar as unchanged."]

    def test_an_unknown_read_back_after_the_retry_does_not_accuse(self, monkeypatch):
        """absent -> retry -> unknown. The retry may well have landed, so the copy must
        assert neither outcome. This is the accusation-on-unknown the module forbids."""
        sent, retries, _ = self._run(
            monkeypatch, FRESH_CLAIM, "Checked your data, updated intervals.icu",
            ["absent", "unknown"])
        assert retries == 1
        assert "Treat your calendar as unchanged" not in sent[1]
        assert "couldn't confirm the change landed" in sent[1]

    def test_a_successful_retry_says_so(self, monkeypatch):
        sent, retries, _ = self._run(
            monkeypatch, FRESH_CLAIM, None, ["absent", "ok"])
        assert retries == 1
        assert sent[1] == "Saved to your calendar this time."

    def test_an_unknown_pre_retry_verdict_never_speaks(self, monkeypatch):
        """Unchanged behaviour, restated here because the gate must not have disturbed it:
        only ACTIONABLE verdicts ever reach the athlete."""
        sent, retries, _ = self._run(monkeypatch, FRESH_CLAIM, None, ["unknown"])
        assert sent == []
        assert retries == 0

    # --- the Strava branch's verdict, end to end (17 Aug 2026) ---------------------------

    STRAVA_CLAIM = "Updated the Strava description for that ride."

    def _strava(self, monkeypatch, retry_result):
        alerts = []
        sent, _retries, _appended = self._run(
            monkeypatch, self.STRAVA_CLAIM, None, [],
            alerts=alerts,
            strava_claim=("absent", "i123", ""),
            strava_retry=retry_result)
        return sent, alerts

    def test_a_proved_strava_absence_now_says_so_to_the_athlete_and_to_ops(self, monkeypatch):
        """The case the old bool could not express. The retry ran, the description read
        back empty, so the failure is ESTABLISHED and both the copy and the alert are
        entitled to say so."""
        sent, alerts = self._strava(monkeypatch, (False, "absent"))
        assert sent[0] == "That didn't actually save to Strava. Retrying now."
        assert "Nothing was written" in sent[1]
        assert "couldn't confirm" not in sent[1]
        assert alerts and "did not land and the retry failed" in alerts[0]
        assert "could not be confirmed" not in alerts[0]

    def test_an_unknown_strava_outcome_still_refuses_to_accuse(self, monkeypatch):
        """The read-back failed or the updater timed out. The write may well have landed,
        so neither the athlete copy nor the alert may assert that it did not."""
        sent, alerts = self._strava(monkeypatch, (False, "unknown"))
        assert "Nothing was written" not in sent[1]
        assert "couldn't confirm the description landed" in sent[1]
        assert alerts and "could not be confirmed after the retry" in alerts[0]
        assert "did not land" not in alerts[0]

    def test_the_two_outcomes_are_actually_worded_differently(self, monkeypatch):
        """The whole point of ticket 2, asserted as one comparison rather than inferred
        from two separate tests: if these ever collapse back to the same string, the
        distinction has been lost again whatever the verdict says."""
        proved_sent, proved_alerts = self._strava(monkeypatch, (False, "absent"))
        unknown_sent, unknown_alerts = self._strava(monkeypatch, (False, "unknown"))
        assert proved_sent[1] != unknown_sent[1]
        assert proved_alerts[0] != unknown_alerts[0]

    def test_an_unchanged_strava_description_is_proved_too(self, monkeypatch):
        sent, alerts = self._strava(monkeypatch, (False, "unchanged"))
        assert "Nothing was written" in sent[1]
        assert "did not land and the retry failed" in alerts[0]

    def test_a_successful_strava_retry_says_so_and_raises_no_alert(self, monkeypatch):
        sent, alerts = self._strava(monkeypatch, (True, "unknown"))
        assert sent[1] == "Saved to Strava this time."
        assert alerts == []


# --- 13) what the Strava retry actually knows, path by path ------------------------------
# TICKET 2 (17 Aug 2026). _retry_strava_description returned a bare bool, so the Strava
# branch above could never tell "proved nothing is there" from "we could not tell". Section
# 12 asserts the wording that falls out of the verdict; this section drives the real
# function and asserts the verdict itself, because a table of hand-fed (ok, verdict) pairs
# proves nothing about which pair the function actually produces.

@pytest.mark.skipif(bot is None,
                    reason="telegram/bot.py is not importable in this environment; the "
                           "pure write_verify tests above still run and still gate")
class TestTheStravaRetryReportsWhatItActuallyKnows:

    @staticmethod
    def _drive(monkeypatch, read_back, before_desc="Aim: Z2 ride.\nOld text.",
               run=None, allowed=True):
        """Run the real _retry_strava_description with the subprocess and both reads
        stubbed. `read_back` is what _read_strava_description returns; `run` replaces
        subprocess.run so a test can make it raise the way the real one does."""
        monkeypatch.setattr(bot, "log", lambda *a, **k: None)
        monkeypatch.setattr(bot, "_description_allowed", lambda slug, icu_id: allowed)
        monkeypatch.setattr(bot.subprocess, "run",
                            run or (lambda *a, **k: None))
        monkeypatch.setattr(bot, "_read_strava_description",
                            lambda slug, icu_id: read_back)
        monkeypatch.setattr(bot, "_STRAVA_DESC_SEEN", {})
        return bot._retry_strava_description("jamie", "i123", before_desc)

    def test_a_failed_read_back_is_unknown_and_not_a_fabricated_absence(self, monkeypatch):
        """THE BUG. _read_strava_description returns (None, None) when the ICU or Strava
        read raises, and when the activity has no strava_id at all. That None used to go
        straight into strava_desc_verdict, which scored "absent" purely because text we
        never received is empty. write_verify still scores a literal None that way - that
        behaviour is correct and is asserted in scripts/test_coach_facts.py - so the fix
        has to be here, at the only place that knows the read failed."""
        assert self._drive(monkeypatch, (None, None)) == (False, "unknown")
        # And the proof that this is the path being tested rather than a coincidence.
        assert V.strava_desc_verdict(None, before="Aim: Z2 ride.\nOld text.") == "absent"

    def test_the_subprocess_timeout_is_unknown_because_it_may_have_written(self, monkeypatch):
        """A real subprocess.TimeoutExpired, so the test proves the actual exception type
        the 120s timeout raises is caught. The updater POSTs near the end of its run, so a
        process killed on the timeout may already have written."""
        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="strava-update-activity.py", timeout=120)
        ok, verdict = self._drive(monkeypatch, ("s1", ""), run=_timeout)
        assert (ok, verdict) == (False, "unknown")

    def test_any_other_subprocess_failure_is_unknown_too(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("python3 not found")
        assert self._drive(monkeypatch, ("s1", ""), run=_boom) == (False, "unknown")

    def test_a_genuine_empty_description_is_still_a_proved_absence(self, monkeypatch):
        """THE TRUE NEGATIVE, which the fix must not break. The read SUCCEEDED and what came
        back was empty, so the write really did not land and the copy is entitled to the
        assertive wording."""
        assert self._drive(monkeypatch, ("s1", "")) == (False, "absent")

    def test_a_byte_identical_description_is_a_proved_unchanged(self, monkeypatch):
        before = "Aim: Z2 ride.\nHeld Z2."
        assert self._drive(monkeypatch, ("s1", before), before_desc=before) \
            == (False, "unchanged")

    def test_a_description_that_moved_counts_as_a_success(self, monkeypatch):
        """`ok` is unchanged from the bool era on this path: the retry re-ran our own
        writer and the text is no longer what it was. The verdict stays "unknown" because
        we never knew the exact text that was meant to land, and result_line ignores the
        verdict when ok is True anyway."""
        ok, verdict = self._drive(monkeypatch, ("s1", "Aim: Z2 ride.\nNew text."))
        assert ok is True
        assert verdict == "unknown"
        assert V.result_line("strava", ok, verdict=verdict) == "Saved to Strava this time."

    def test_a_no_description_sport_is_unknown_rather_than_absent(self, monkeypatch):
        """Defensive: _verify_strava_claim already returns "unknown" for these, so the
        caller never gets this far. "absent" would unlock "Nothing was written, so don't
        treat that description as updated" - telling the athlete to expect a description on
        a sailing activity that must never carry one."""
        assert self._drive(monkeypatch, ("s1", ""), allowed=False) == (False, "unknown")

    def test_a_failed_read_back_does_not_poison_the_baseline(self, monkeypatch):
        """_STRAVA_DESC_SEEN is the "before" for the NEXT claim on this activity. Writing a
        None or an empty into it on a failed read would make the next comparison lie."""
        monkeypatch.setattr(bot, "log", lambda *a, **k: None)
        monkeypatch.setattr(bot, "_description_allowed", lambda slug, icu_id: True)
        monkeypatch.setattr(bot.subprocess, "run", lambda *a, **k: None)
        monkeypatch.setattr(bot, "_read_strava_description", lambda slug, icu_id: (None, None))
        seen = {}
        monkeypatch.setattr(bot, "_STRAVA_DESC_SEEN", seen)
        bot._retry_strava_description("jamie", "i123", "Aim: Z2 ride.")
        assert seen == {}

    @pytest.mark.parametrize("read_back,before,expected", [
        ((None, None), "old", (False, "unknown")),
        (("s1", ""), "old", (False, "absent")),
        (("s1", "same"), "same", (False, "unchanged")),
        (("s1", "new"), "old", (True, "unknown")),
    ])
    def test_the_ok_bool_is_unchanged_on_every_path_it_used_to_reach(
            self, monkeypatch, read_back, before, expected):
        """REGRESSION SAFETY, and the reason this ticket could ship on its own. The verdict
        is new information alongside the outcome, not a redefinition of it: `ok` is what
        decides whether the athlete is told "Saved to Strava this time", and the old
        formula's answer has to survive verbatim."""
        got = self._drive(monkeypatch, read_back, before_desc=before)
        assert got == expected
        _sid, desc = read_back
        legacy = (V.strava_desc_verdict(desc, before=before) not in V.ACTIONABLE
                  and bool(desc))
        assert got[0] == legacy, "the retry's success/failure call has moved"

    def test_the_verdict_is_always_one_write_verify_understands(self, monkeypatch):
        """Vocabulary check. A verdict outside this set silently falls to the hedge in
        result_line, which is safe but would quietly undo the whole ticket."""
        known = {"ok", "absent", "unchanged", "unknown"}
        for read_back in [(None, None), ("s1", ""), ("s1", "same"), ("s1", "new")]:
            _ok, verdict = self._drive(monkeypatch, read_back, before_desc="same")
            assert verdict in known, verdict
