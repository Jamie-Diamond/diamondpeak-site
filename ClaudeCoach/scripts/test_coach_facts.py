#!/usr/bin/env python3
"""Offline tests for the computed-FACTS block and verify-after-write (13 Aug 2026).
Run: python3 ClaudeCoach/scripts/test_coach_facts.py

WHAT BROKE. Three months of chat logs show the model asserting figures it generated
rather than looked up — CSS quoted 1:39 when the live value was 1:41, "the highest
fuelling on your file" for a session that ranked TENTH, an invented "four straight bike
days", invented gels/swim/RPE history — and claiming external writes that never
happened ("did you actually update Strava or just say you did?" → "No", four times).

WHAT IS TESTED HERE. The pure pieces: the ranking (the arithmetic that makes "highest on
file" checkable), the day strip (the evidence a run-of-days claim needs), the threshold
line and its single-owner invariant, the size budget, and the three-valued write verdicts
with stubs standing in for Strava/ICU. No model calls, no network, tmpdir fixtures only —
the real athlete files are VM-only.
"""
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
import coach_facts as F
import write_verify as V

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


TODAY = date(2026, 8, 13)

# A log built so the DOCUMENTED failure is reproducible: the most recent bike session
# (11 Aug, 70g/hr) is NOT the highest — 14 Jun at 92g/hr is — and a 90-day window would
# cut the record out and make the recent one look like a best-ever.
LOG = [
    {"date": "2026-01-20", "sport": "Ride", "duration_min": 240, "nutrition_g_carb": 368, "tss": 200},
    {"date": "2026-06-14", "sport": "VirtualRide", "duration_min": 180, "nutrition_g_carb": 276, "tss": 150},
    {"date": "2026-06-20", "sport": "Ride", "duration_min": 120, "nutrition_g_carb": 160, "tss": 90},
    {"date": "2026-07-02", "sport": "Ride", "duration_min": 300, "nutrition_g_carb": 300, "tss": 210},
    {"date": "2026-07-09", "sport": "Ride", "duration_min": 90, "nutrition_g_carb": 90, "tss": 70},
    {"date": "2026-07-15", "sport": "Ride", "duration_min": 60, "nutrition_g_carb": 30, "tss": 40},
    {"date": "2026-08-11", "sport": "Ride", "duration_min": 120, "nutrition_g_carb": 140, "tss": 95},
    # No duration: a rate cannot be computed, so it must be EXCLUDED, not divided.
    {"date": "2026-08-12", "sport": "Ride", "duration_min": 0, "nutrition_g_carb": 200},
    {"date": "2026-08-05", "sport": "Run", "duration_min": 120, "nutrition_g_carb": 120, "tss": 100},
    {"date": "2026-08-09", "sport": "TrailRun", "duration_min": 60, "nutrition_g_carb": 75, "tss": 55},
    {"date": "2026-08-13", "sport": "Swim", "duration_min": 45, "tss": 35, "stub": True},
]

# --- 1) fuelling ranking — the arithmetic behind "highest on file" ----------------------
bike = F.fuelling_ranking(LOG, "bike")
check("bike ranking is highest-first by RATE, not by date",
      [r["g_hr"] for r in bike] == [92, 92, 80, 70, 60])
check("the all-time record (14 Jun) is in the list, so a 90-day window cannot hide it",
      any(r["date"] == "2026-06-14" and r["g_hr"] == 92 for r in bike))
check("the most recent bike session is NOT top — the documented false claim is now checkable",
      bike[0]["date"] != "2026-08-11")
check("VirtualRide counts as a bike day",
      any(r["date"] == "2026-06-14" for r in bike))
check("g/hr comes from the TOTAL divided by hours (276g over 180min = 92)",
      next(r for r in bike if r["date"] == "2026-06-14")["g_hr"] == 92)
check("a session with no duration is excluded, never divided",
      all(r["date"] != "2026-08-12" for r in bike))
check("rows carry date and duration so two equal rates stay distinguishable",
      {"date", "g_hr", "g_total", "duration_min"} <= set(bike[0]) and bike[0] != bike[1])
check("ties are ordered by date, not arbitrarily",
      [r["date"] for r in bike[:2]] == ["2026-01-20", "2026-06-14"])
check("the list is capped at the top 5", len(bike) == 5)
run = F.fuelling_ranking(LOG, "run")
check("run ranking is separate from bike and includes TrailRun",
      [r["g_hr"] for r in run] == [75, 60])
check("swim sessions with no carbs produce an empty ranking",
      F.fuelling_ranking(LOG, "swim") == [])
check("an empty log ranks nothing rather than raising", F.fuelling_ranking([], "bike") == [])
check("a junk carb value is skipped, not coerced",
      F.fuelling_ranking([{"date": "2026-08-01", "sport": "Ride",
                           "duration_min": 60, "nutrition_g_carb": "lots"}], "bike") == [])

# --- 2) day strip — the evidence an "N straight days" claim needs -----------------------
strip = F.day_strip(LOG, TODAY)
days = dict(strip)
check("the strip covers 10 days ending today",
      len(strip) == 10 and strip[-1][0] == "2026-08-13" and strip[0][0] == "2026-08-04")
check("a day with no session is an explicit gap, so an invented run of days is visible",
      days["2026-08-06"] == [] and days["2026-08-07"] == [])
check("11 Aug shows a bike day and 13 Aug a swim",
      days["2026-08-11"] == ["bike"] and days["2026-08-13"] == ["swim"])
# The invented claim was "four straight bike days". The strip must make the longest
# actual run countable: 11 and 12 Aug are bike days, 13 Aug is a swim, so the run is two.
_runs, _cur = [], 0
for _, fams in strip:
    _cur = _cur + 1 if "bike" in fams else 0
    _runs.append(_cur)
check("'four straight bike days' is refutable — the longest real bike run is two",
      max(_runs) == 2)
line = F._strip_line(strip)
check("the rendered strip marks empty days with a dash", "08-06:-" in line)
check("the rendered strip says a run of consecutive days is countable from it",
      "countable from this" in line)

# --- 3) thresholds — one owner, and honest when unresolved ------------------------------
T = {"ftp_watts": 297, "ftp_source": "eftp", "eftp": 297, "static_ftp": 300,
     "run_threshold_per_km": "4:02.0/km", "swim_css_per_100m": "1:41.0/100m", "notes": []}
tl = F.thresholds_line(T, lthr=165)
check("the threshold line states the live CSS (the 1:39-vs-1:41 failure)", "1:41.0/100m" in tl)
check("the threshold line names FTP, its source, and the configured value it beats",
      "297W" in tl and "live eFTP" in tl and "300W" in tl)
check("LTHR comes through from the profile", "LTHR 165 bpm" in tl)
check("the line is marked authoritative", "AUTHORITATIVE" in tl)
check("FTP appears exactly ONCE in the line the block owns", tl.count("297W") == 1)
missing = F.thresholds_line({"ftp_watts": 250, "ftp_source": "config", "notes":
                             ["no swim CSS set in ICU — swim pace zones unavailable"]})
check("a missing CSS is stated as missing, never filled in",
      "no swim CSS" in missing and "1:" not in missing)
unres = F.thresholds_line(None)
check("an unresolved threshold is declared unresolved rather than guessed at",
      "UNRESOLVED" in unres and "no figure elsewhere in this context is current" in unres
      and not any(c.isdigit() for c in unres))
check("the prefetch pointer contains NO digits, so it cannot state a stale threshold",
      not any(c.isdigit() for c in F.PREFETCH_THRESHOLD_POINTER))
check("the pointer sends the model to the FACTS block",
      "FACTS" in F.PREFETCH_THRESHOLD_POINTER)

# --- 4) the assembled block ------------------------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="facts-test-"))
(tmp / "session-log.json").write_text(json.dumps(LOG))
(tmp / "profile.json").write_text(json.dumps({"name": "Test Athlete", "lthr": 165}))
block = F.build_facts_block(tmp, thresholds=T, today=TODAY)
check("the block is headed FACTS and dated", block.startswith("=== FACTS") and "2026-08-13" in block)
check("the block carries exactly ONE rule", block.count("RULE:") == 1 and F.FACTS_RULE in block)
# Nominal-vs-real: every OTHER line must be a labelled fact, not a second instruction, or
# the block quietly grows into the rule list this prompt surface has a history of.
_imperatives = re.compile(r"\b(do not|don'?t|never|always|must|use these|use the|quote|call the)\b",
                          re.IGNORECASE)
_other = [l for l in block.splitlines()
          if l and not l.startswith("===") and not l.startswith("RULE:")]
check("no line other than the rule issues an instruction",
      [l for l in _other if _imperatives.search(l)] == [])
check("the rule names all four claim types it governs",
      all(w in F.FACTS_RULE for w in ("superlative", "record", "threshold", "straight days")))
check("the rule tells the model what to do instead of asserting",
      "would need to check" in F.FACTS_RULE)
check("the block states today's logged session", "Logged today: Swim 45min" in block)
check("a stub session is flagged as awaiting feedback", "awaiting feedback" in block)
check("the block does NOT restate this week's Load (single owner)",
      "DETERMINISTIC PLANNING NUMBERS" in block and "which owns that figure" in block)
check("the ranked bike and run lists are both present",
      "Bike fuelling g/hr" in block and "Run fuelling g/hr" in block and "92g/hr 2026-06-14" in block)
check("FTP is stated exactly once in the whole block", block.count("297W") == 1)
check(f"the block fits the size budget (<= {F.MAX_CHARS} chars ~ 600 tokens)",
      len(block) <= F.MAX_CHARS)
check("an oversized block is truncated WITH a warning, never silently shortened",
      "[FACTS truncated" in F.build_facts_block(tmp, thresholds=T, today=TODAY, max_chars=400))

empty = Path(tempfile.mkdtemp(prefix="facts-empty-"))
block2 = F.build_facts_block(empty, thresholds=None, today=TODAY)
check("an athlete directory with no files still yields a rule and no invented figures",
      F.FACTS_RULE in block2 and "UNRESOLVED" in block2 and "none on file" in block2)
check("nothing logged today is said plainly", "Logged today: nothing yet." in block2)
(tmp / "session-log.json").write_text("{not json")
check("a corrupt session log degrades the block instead of losing the turn",
      "none on file" in F.build_facts_block(tmp, thresholds=T, today=TODAY))

# --- 5) claim detection — which replies assert an external write ------------------------
for reply in ("I've updated your Strava description with the session detail.",
              "Strava description refreshed.",
              "Written up on Strava.",
              "Done — pushed the write-up to Strava."):
    check(f"claims a Strava description write: {reply!r}", "strava" in V.claim_kinds(reply))
for reply in ("Pushed Thursday's ride to your calendar.",
              "I've moved the long run to Saturday on intervals.icu.",
              "Deleted the duplicate workout from your calendar.",
              "Shortened tomorrow's planned session on ICU to 60 min."):
    check(f"claims a calendar write: {reply!r}", "icu" in V.claim_kinds(reply))
for reply in ("Shall I push that to your calendar?",
              "I can update your Strava description if you want.",
              "I'll move the ride to Saturday once you confirm.",
              "Want me to update Strava?",
              "Your Strava description already covers it — nothing to change.",
              "Nice ride. 92g/hr is your best fuelling on file."):
    check(f"does NOT claim a write: {reply!r}", V.claim_kinds(reply) == set())
# A rename claim is UNVERIFIABLE (we never know the intended name) and must not be read
# as a description claim: on a sailing activity that would find an empty description, call
# the claim false, and "retry" by writing a description — breaching the water-sports rule.
for reply in ("Renamed it to Sunday Sail on Strava.",
              "Strava title updated.",
              "I've renamed the activity on Strava."):
    check(f"a name-only Strava claim is not verified as a description: {reply!r}",
          "strava" not in V.claim_kinds(reply))
check("a claim naming BOTH name and description is still verified",
      "strava" in V.claim_kinds("Renamed it and updated the Strava description."))
check("a reply claiming both writes reports both",
      V.claim_kinds("Updated your Strava description and pushed Friday's swim to your calendar.")
      == {"strava", "icu"})
check("empty text claims nothing", V.claim_kinds("") == set() and V.claim_kinds(None) == set())

# --- 6) Strava verdicts — absent means retry, unknown means stay quiet -----------------
check("an empty description proves the claim false",
      V.strava_desc_verdict("") == "absent" and V.strava_desc_verdict("   \n ") == "absent")
check("a description claimed again but byte-identical is 'unchanged', not 'absent'",
      V.strava_desc_verdict("Aim: Z2 ride.\nHeld Z2.", before="Aim: Z2 ride.\nHeld Z2.")
      == "unchanged")
check("a changed description is not accused (unknown, stays silent)",
      V.strava_desc_verdict("Aim: Z2 ride.\nNew text.", before="Aim: Z2 ride.\nOld text.") == "unknown")
check("a non-empty description with no prior reading proves nothing",
      V.strava_desc_verdict("Aim: Z2 ride.") == "unknown")
check("our OWN write is verified absolutely against the text we sent",
      V.strava_desc_verdict("Aim: Z2 ride.\nHeld Z2.", expected="Aim: Z2 ride.\nHeld Z2.") == "ok")
check("whitespace normalisation stops a false 'absent' on our own write",
      V.strava_desc_verdict("Aim: Z2 ride.\r\n Held Z2. ", expected="Aim: Z2 ride.\nHeld Z2.") == "ok")
check("a read-back that differs from what we sent is absent",
      V.strava_desc_verdict("something else", expected="Aim: Z2 ride.") == "absent")
check("expecting nothing is unknown, not a pass",
      V.strava_desc_verdict("anything", expected="") == "unknown")
check("a failed read (None) is never treated as proof of a write",
      V.strava_desc_verdict(None) == "absent"
      and V.strava_desc_verdict(None, expected="x") == "absent")

# --- 7) calendar verdicts — never accuse on a stale snapshot ---------------------------
A = V.events_fingerprint([{"id": 1, "start_date_local": "2026-08-14T00:00:00", "type": "Ride",
                           "name": "Z2 ride", "icu_training_load": 90, "moving_time": 5400}])
B = V.events_fingerprint([{"id": 1, "start_date_local": "2026-08-15T00:00:00", "type": "Ride",
                           "name": "Z2 ride", "icu_training_load": 90, "moving_time": 5400}])
check("an unchanged window with a FRESH snapshot proves the claim false",
      V.icu_events_verdict(A, A, snapshot_age_s=30) == "absent"
      and "absent" in V.ACTIONABLE)
check("a moved event shows the write landed", V.icu_events_verdict(A, B, snapshot_age_s=30) == "ok")
check("a renamed event shows the write landed",
      V.icu_events_verdict(A, V.events_fingerprint(
          [{"id": 1, "start_date_local": "2026-08-14T00:00:00", "type": "Ride",
            "name": "Z2 ride +30", "icu_training_load": 90, "moving_time": 5400}]),
          snapshot_age_s=30) == "ok")
check("a load change shows the write landed",
      V.icu_events_verdict(A, V.events_fingerprint(
          [{"id": 1, "start_date_local": "2026-08-14T00:00:00", "type": "Ride",
            "name": "Z2 ride", "icu_training_load": 120, "moving_time": 5400}]),
          snapshot_age_s=30) == "ok")
check("a deletion shows the write landed",
      V.icu_events_verdict(A, V.events_fingerprint([]), snapshot_age_s=30) == "ok")
check("a STALE snapshot never accuses",
      V.icu_events_verdict(A, A, snapshot_age_s=9999) == "unknown")
check("a missing snapshot never accuses",
      V.icu_events_verdict(None, A, snapshot_age_s=10) == "unknown")
check("a failed read-back never accuses",
      V.icu_events_verdict(A, None, snapshot_age_s=10) == "unknown")
check("an event touched inside the turn is proof, whatever the fingerprints say",
      V.icu_events_verdict(A, A, snapshot_age_s=9999, touched_in_turn=True) == "ok")
check("load_target counts as the planned load (push_workout writes that field)",
      V.events_fingerprint([{"id": 2, "load_target": 80}])
      != V.events_fingerprint([{"id": 2, "load_target": 90}]))

# --- 8) honest copy -------------------------------------------------------------------
check("the retry line admits the write did not happen and says what happens next",
      "didn't actually save" in V.retry_line("strava")
      and "Retrying" in V.retry_line("strava"))
# A write of byte-identical text cannot be told apart from no write at all, so the
# "unchanged" copy claims only what the read-back proves.
check("the unchanged verdict gets weaker, outcome-true copy",
      "unchanged from before" in V.retry_line("strava", "unchanged")
      and "didn't actually save" not in V.retry_line("strava", "unchanged"))
check("the unchanged copy exists for the calendar too",
      "unchanged from before" in V.retry_line("icu", "unchanged"))
check("both actionable verdicts are the ones that speak and retry",
      V.ACTIONABLE == ("absent", "unchanged"))
check("the retry line names the destination", "Strava" in V.retry_line("strava")
      and "calendar" in V.retry_line("icu"))
check("a failed retry tells the athlete nothing was written",
      "Nothing was written" in V.result_line("strava", False)
      and "unchanged" in V.result_line("icu", False))
check("a successful retry says so without overclaiming",
      V.result_line("strava", True) == "Saved to Strava this time.")
check("an unknown kind still yields honest copy, never a crash",
      "didn't actually save" in V.retry_line("something")
      and "hasn't changed" in V.retry_line("something", "unchanged")
      and V.result_line("something", True))

if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
