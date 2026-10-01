#!/usr/bin/env python3
"""Offline end-to-end test of Telegram onboarding into the baseline block (27 Sep 2026).
Run: python3 ClaudeCoach/scripts/test_onboarding_baseline.py

Drives telegram/bot.py's real onboarding state machine, message by message, with
Telegram, Intervals.icu, the race lookup, git and subprocesses replaced by fakes, and
every path pointed at a tmpdir. Checks what a new athlete is asked and what lands on
disk, then the /approve hand-off and the result-confirm button.

WHAT IT GUARDS:
  - the new questions are asked, in order, and only when they apply (no power question
    for someone ICU already shows riding with power; no swim question for a runner)
  - a "no" to a threshold question is an answer, not a threshold
  - the answers become the right baseline state: which sports are tested, estimated,
    missing, and which get a test
  - /approve starts the block, and the confirm button writes Intervals.icu
Never touches a real athlete directory or the network.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve().parent
CC = _here.parent
sys.path.insert(0, str(CC / "lib"))
sys.path.insert(0, str(CC / "telegram"))
import bot as B   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp(prefix="ob-test-"))
(tmp / "telegram").mkdir()
(tmp / "config").mkdir()
(tmp / "athletes").mkdir()
shutil.copytree(CC / "onboarding", tmp / "onboarding")

B.BASE = tmp / "telegram"
B.PENDING_FILE = tmp / "config/pending.json"
B.ONBOARDING_FILE = tmp / "config/onboarding_state.json"
B.ATHLETES_CONFIG = tmp / "config/athletes.json"
B.baseline_lib.BASE = tmp

SENT = []
B.send = lambda token, chat_id, text, **kw: SENT.append((str(chat_id), text, kw.get("reply_markup")))
PHOTOS = []
B.send_photo = lambda token, chat_id, data, *a, **k: PHOTOS.append((str(chat_id), len(data)))
B._outbox = None
ISSUES = []                        # what _icu_setup_issues reports, one list per call
B._icu_id_for_key = lambda key: "i123"
REAL_SETUP_ISSUES = B._icu_setup_issues
B._icu_setup_issues = lambda i, k, *a: ISSUES.pop(0) if ISSUES else []
KEY = "abcdefghij0123456789abcde"
B._git_commit = lambda *a, **k: None
B.load_config = lambda: {"admin_chat_id": "999"}
B.log = lambda *a, **k: None


class _NoPopen:
    def __init__(self, *a, **k):
        pass


RUNS = []


class _Ran:
    returncode, stdout, stderr = 0, "{}", ""


def _fake_run(args, **kw):
    RUNS.append(args)
    return _Ran()


B.subprocess.Popen = _NoPopen
B.subprocess.run = _fake_run


def fake_icu(icu, race):
    B._fetch_icu_data = lambda i, k: (dict(icu), "*Found in Intervals.icu:*")
    B._lookup_race = lambda name, d: dict(race)


TRI = {"race_type": "70.3 Triathlon", "swim_km": 1.9, "bike_km": 90, "run_km": 21.1}
ICU_TRI = {"ftp_watts": 250, "run_threshold_pace_per_km": None, "swim_css_per_100m": None,
           "weight_kg": 70, "lthr": None, "has_power": False}


def onboard(chat, answers):
    """Send each answer; return the questions asked (text of each bot message)."""
    B.save_pending([chat])
    asked = []
    B.handle_onboarding("t", chat, "hi")
    for a in answers:
        n = len(SENT)
        B.handle_onboarding("t", chat, a)
        asked.extend(t for c, t, _ in SENT[n:] if c == chat)
    return asked


# ── 1. a 70.3 athlete: wrist HR, rides with power, one tested swim number ──────
fake_icu(ICU_TRI, TRI)
asked = onboard("123", ["Sam Smith", "70.3 Test, 2027-06-01", "yes", KEY,
                        "sub 5", "3 years, longest a marathon", "none", "10",
                        "7",             # invalid HR answer
                        "2",             # wrist
                        "yes",           # power
                        "0",             # FTP 250 was not a test
                        "about 230",     # rough FTP, since the bike gets tested
                        "no",            # run threshold: not tested
                        "5k 24:30 in August",   # rough run figure
                        "1:45",          # swim CSS: tested in last 6 weeks
                        "my last race",  # comparison race without a date: asked again
                        "Ironman Wales, Sept 2025",   # comparison race for the fitness chart
                        "no",            # heat training
                        "yes",           # fuelling coaching
                        "2",             # coaching level: mid
                        "sam"])
blob = "\n".join(asked)
check("asks what they wear for heart rate", "What do you wear for *heart rate*" in blob)
check("rejects an HR answer outside 1-4", "number from 1 to 4" in blob)
check("asks about power when ICU shows none", "ride with *power*" in blob)
check("asks whether the ICU FTP was a real test", "Bike FTP 250 W" in blob and "last 6 weeks" in blob)
check("experience question is not triathlon-only", "full-distance triathlons" not in blob)
_setup = next((i for i, t in enumerate(asked) if "Setting up your profile" in t), None)
check("handle is the last question before setup",
      _setup is not None and "account handle" in asked[_setup - 1], asked[-3:])
st = json.loads((tmp / "athletes/sam/baseline.json").read_text())
prof = json.loads((tmp / "athletes/sam/profile.json").read_text())
ss = st["sports_state"]
check("baseline state is pending until approved", st["status"] == "pending")
check("sports from the race distances", st["sports"] == ["swim", "bike", "run"], st["sports"])
check("hr_source wrist in state and profile", st["hr_source"] == "wrist" and prof["hr_source"] == "wrist")
check("power yes recorded", st["has_power"] is True)
check("ICU FTP not tested = estimated, gets the ramp",
      ss["bike"]["confidence"] == "estimated" and ss["bike"]["test"]["protocol"] == "bike_ramp", ss["bike"])
check("run 'no' = missing, gets the 30-min TT",
      ss["run"]["confidence"] == "missing" and ss["run"]["test"]["protocol"] == "run_tt", ss["run"])
check("typed tested CSS = tested, no swim test, remembered as the athlete's number",
      ss["swim"]["confidence"] == "tested" and ss["swim"]["test"] is None
      and ss["swim"]["value_source"] == "athlete" and ss["swim"]["values"] == {"css": "1:45"}, ss["swim"])
check("'no' is not written as a run threshold", prof.get("run_threshold_pace_per_km") in (None, ""),
      prof.get("run_threshold_pace_per_km"))
check("level asked just before the handle", "How much *detail*" in asked[_setup - 2], asked[_setup - 3:_setup])
check("coaching level saved", prof.get("coaching_level") == "mid", prof.get("coaching_level"))
check("a comparison race without a date is asked again", "Add when it was" in blob)
check("comparison race saved for the fitness chart",
      prof.get("prev_race") == {"name": "Ironman Wales", "date": "2025-09-15", "approx": True}, prof.get("prev_race"))
check("heat and fuelling asked before the level", "*heat training*" in blob and "*fuelling coaching*" in blob)
check("heat no / fuelling yes saved", prof.get("heat_protocol") is False and prof.get("fuelling_coaching") is True)
check("athlete ID comes from the key, never asked",
      prof["icu_athlete_id"] == "i123" and "athlete ID" not in blob)
check("rough figure asked right after each sport is settled as 'to test'",
      "Roughly what's your FTP" in blob and "recent race or hard run" in blob
      and "100m or 400m" not in blob, blob[-600:])
check("rough figures saved as expected ranges, only for tested-in-week-one sports",
      ss["bike"].get("expected", {}).get("unit") == "w" and ss["run"].get("expected", {}).get("unit") == "s_km"
      and "expected" not in ss["swim"], {f: ss[f].get("expected") for f in ss})
check("the ramp starts from the rough FTP when there is no tested one",
      B.baseline_lib.expected_mid(ss["bike"]["expected"]) == 230)
check("athletes.json entry inactive until approved",
      json.loads(B.ATHLETES_CONFIG.read_text())["sam"]["active"] is False)

# ── 2. /approve hands off to baseline-week.py start ─────────────────────────
RUNS.clear()
SENT.clear()
B.handle_admin_command("t", "999", "/approve sam", {"admin_chat_id": "999"})
check("/approve runs baseline-week.py start for the athlete",
      any("baseline-week.py" in " ".join(map(str, r)) and "start" in r and "sam" in r for r in RUNS), RUNS)
check("admin is told the baseline week was scheduled",
      any(c == "999" and "Baseline week scheduled" in t for c, t, _ in SENT), SENT)

# ── 3. a marathon runner: no bike or swim questions at all ───────────────────
fake_icu({"ftp_watts": None, "run_threshold_pace_per_km": "4:30", "swim_css_per_100m": None,
          "weight_kg": 60, "has_power": False},
         {"race_type": "Running Marathon", "run_km": 42.2})
asked = onboard("124", ["Ali Run", "London Marathon, 2027-04-25", "y", KEY,
                        "sub 3:30", "5 years", "none", "6", "1", "1", "no", "yes", "no", "beginner", "ali"])
blob = "\n".join(asked)
check("runner: no power question", "ride with *power*" not in blob)
check("runner: no FTP or CSS gap question", "FTP" not in blob and "CSS" not in blob, blob[-400:])
st = json.loads((tmp / "athletes/ali/baseline.json").read_text())
check("runner: run only", st["sports"] == ["run"], st["sports"])
check("runner: stated recent test of the ICU pace = tested, no test",
      st["sports_state"]["run"]["confidence"] == "tested" and st["sports_state"]["run"]["test"] is None,
      st["sports_state"]["run"])

# ── 3b. no Intervals.icu yet: set-up walk-through, taps, the set-up check ────
fake_icu(ICU_TRI, TRI)
PHOTOS.clear()
ISSUES[:] = [["Planned workouts aren't set to go to your *Garmin*."], []]
B.save_pending(["125"])
B.handle_onboarding("t", "125", "hi")
for a in ["Pat Lee", "70.3 Test, 2027-06-01"]:
    B.handle_onboarding("t", "125", a)
n = len(SENT)
B.handle_onboarding("t", "125", "ob:icu_has:no")
setup_msgs = [t for c, t, _ in SENT[n:] if c == "125"]
check("'No' tap gives the set-up steps", any("Sign up free at intervals.icu" in t for t in setup_msgs), setup_msgs)
check("set-up steps come with screenshots (settings, Garmin)", len(PHOTOS) == 2, PHOTOS)
check("set-up asks for the history", any("Download old data" in t for t in setup_msgs), setup_msgs)
check("set-up says Strava isn't needed, and never asks for a Strava import",
      any("Strava isn't needed" in t for t in setup_msgs)
      and not any("Import all Strava data" in t for t in setup_msgs), setup_msgs)
check("set-up ends on an I've done it button",
      SENT[-1][2] and SENT[-1][2]["inline_keyboard"][0][0]["callback_data"] == "ob:icu_setup:done", SENT[-1])
n = len(SENT)
B.handle_onboarding("t", "125", "ob:icu_has:yes")              # the old question's button
check("a tap on an earlier question is ignored", len(SENT) == n)
B.handle_onboarding("t", "125", "ob:icu_setup:done")
check("then the API key steps, with their screenshots",
      any("API key" in t for c, t, _ in SENT[n:]) and len(PHOTOS) == 4, (PHOTOS, SENT[n:]))
n = len(SENT)
B.handle_onboarding("t", "125", "not a key")
check("a non-key is refused", any("doesn't look like an API key" in t for c, t, _ in SENT[n:]))
n = len(SENT)
B.handle_onboarding("t", "125", KEY)
fix = [(t, m) for c, t, m in SENT[n:] if c == "125"]
check("set-up problems are listed with Check again", any("workout upload" in t or "Garmin" in t for t, _ in fix)
      and fix[-1][1] and fix[-1][1]["inline_keyboard"][0][0]["callback_data"] == "ob:icu_fix:again", fix)
n = len(SENT)
B.handle_onboarding("t", "125", "ob:icu_fix:again")
check("Check again, now fixed, carries on", any("All connected" in t for c, t, _ in SENT[n:])
      and any("A goal" in t for c, t, _ in SENT[n:]), SENT[n:])
for a in ["sub 5", "2 years", "none", "8", "1", "yes", "0", "dunno", "no", "10k 50:00", "no", "don't know",
          "no", "ob:heat:yes", "ob:fuel:no"]:
    B.handle_onboarding("t", "125", a)
n = len(SENT)
B.handle_onboarding("t", "125", "ob:level:pro")
check("level tap moves on to the handle", any("account handle" in t for c, t, _ in SENT[n:]), SENT[n:])
B.handle_onboarding("t", "125", "pat")
prof = json.loads((tmp / "athletes/pat/profile.json").read_text())
check("tapped level saved", prof.get("coaching_level") == "pro", prof.get("coaching_level"))
check("heat yes / fuelling no saved as the switches",
      prof.get("heat_protocol") is True and prof.get("fuelling_coaching") is False, prof)
check("tap from a sign-up goes to onboarding, not athlete handlers",
      B.dispatch_callback("t", "126", "ob:icu_has:yes", 1, {}, {}) is True)

# ── 3c. the set-up check itself, on made-up Intervals.icu accounts ──────────
import types
from datetime import date as _d, timedelta as _td


def _ago(n):
    return (_d.today() - _td(days=n)).isoformat() + "T07:00:00"


def setup_check(profile, acts, well):
    class FakeClient:
        def __init__(self, *a):
            pass

        def fetch_all(self, *specs):
            return [profile, acts, well]
    sys.modules["icu_api"] = types.SimpleNamespace(IcuClient=FakeClient)
    try:
        return REAL_SETUP_ISSUES("i1", "k")
    finally:
        sys.modules.pop("icu_api", None)


SLEPT = [{"hrv": 60, "sleepSecs": 25000}]
LONG = [{"source": "GARMIN_CONNECT", "start_date_local": _ago(390)},
        {"source": "GARMIN_CONNECT", "start_date_local": _ago(2)}]
check("all set: nothing to fix",
      setup_check({"icu_garmin_upload_workouts": True}, LONG, SLEPT) == [])
got = setup_check({"strava_authorized": True}, [{"source": "STRAVA", "start_date_local": _ago(3)}], [])
check("Strava only: told to connect the watch directly",
      len(got) == 1 and "Strava" in got[0] and "Apple Watch" in got[0], got)
got = " ".join(setup_check({"icu_garmin_upload_workouts": False},
                           [{"source": "GARMIN_CONNECT", "start_date_local": _ago(20)}], []))
check("Garmin: missing wellness, upload and history each named",
      "Download wellness data" in got and "Upload planned workouts" in got
      and "last *2 weeks*" in got and "Import all Garmin data" in got, got)
check("Apple Watch via an app: no brand settings to nag about",
      setup_check({}, [{"source": "OAUTH_CLIENT", "start_date_local": _ago(380)}], SLEPT) == [])
got = " ".join(setup_check({}, [{"source": "ZWIFT", "start_date_local": _ago(380)}], SLEPT))
check("Zwift only: asked for the watch too", "indoor sessions" in got, got)

# ── 3d. a Peak sign-up with no watch data: Connect Strava, then copying on ────
import outbox as OB  # noqa: E402
OB.BASE, OB.ATHLETES_CONFIG, OB.SIGNUP_DIR = tmp, B.ATHLETES_CONFIG, tmp / "config/web-signup"
OB._CACHE.update(mtime=None, data={})
B._outbox = OB
WEB = "web-00aa11bb22"
ISSUES[:] = [["Your activities only come in through *Strava*, and Strava hides them from me."], []]
B.save_pending([WEB])
for a in ["hi", "Robin Apple", "70.3 Test, 2027-06-01", "ob:icu_has:yes"]:
    B.handle_onboarding("t", WEB, a)
n = len(SENT)
B.handle_onboarding("t", WEB, KEY)
fix = [(t, m) for c, t, m in SENT[n:] if c == WEB]
rows = (fix[-1][1] or {}).get("inline_keyboard", [[]])
check("no watch data: the fix step offers Connect Strava",
      rows[0][0].get("url", "").endswith("/api/strava/connect") and any("Connect Strava" in t for t, _ in fix), rows)
(OB.SIGNUP_DIR / WEB).mkdir(parents=True, exist_ok=True)
(OB.SIGNUP_DIR / WEB / "strava_tokens.json").write_text('{"refresh_token": "r"}')
check("with Strava connected, nothing is left to fix",
      REAL_SETUP_ISSUES("i1", "k", WEB) == [])
B.handle_onboarding("t", WEB, "ob:icu_fix:again")
for a in ["sub 5", "2 years", "none", "8", "2", "yes", "0", "220", "no", "half 1:52", "no", "400m 8:30",
          "no", "no", "no", "ob:level:beginner", "robin"]:
    B.handle_onboarding("t", WEB, a)
ath = json.loads(B.ATHLETES_CONFIG.read_text()).get("robin", {})
check("signed up through Strava: copying switched on and the tokens moved in",
      ath.get("strava_bridge") is True and (tmp / "athletes/robin/strava_tokens.json").exists()
      and not (OB.SIGNUP_DIR / WEB).exists(), ath)
check("a Telegram sign-up is never offered the Peak-only Strava link",
      B._strava_offer("555", ["Your activities only come in through *Strava*"]) is False)
B._outbox = None

# ── 4. race sports from name when the lookup has no distances ────────────────
check("gran fondo -> bike", B._race_sports({"race_type": "Cycling Gran Fondo"}, "") == ["bike"])
check("duathlon -> bike, run", B._race_sports({}, "Powerman Duathlon") == ["bike", "run"])
check("unknown -> triathlon default", B._race_sports({}, "Something 2027") == ["swim", "bike", "run"])

# ── 5. the confirm button writes Intervals.icu ───────────────────────────────
st = json.loads((tmp / "athletes/sam/baseline.json").read_text())
st["status"] = "active"
st["sports_state"]["bike"]["test"].update(status="result_pending",
                                          pending={"ftp": 240, "best1m_w": 320})
B.baseline_lib.save("sam", st)


class FakeICU:
    puts = []

    def _put(self, path, payload):
        FakeICU.puts.append((path, payload))


B._icu_client = lambda slug: FakeICU()
B.edit_keyboard_confirm = lambda *a, **k: None
B._append_capture_history = lambda *a, **k: None
SENT.clear()
handled = B._handle_baseline_confirm("t", "123", "bl:yes:sam:bike", 5, {"123": {"slug": "sam"}})
check("confirm tap is handled", handled)
check("confirm writes FTP 240 to Intervals.icu", FakeICU.puts == [("sport-settings/Ride", {"ftp": 240})],
      FakeICU.puts)
check("confirm reply goes to the athlete", SENT and SENT[-1][0] == "123" and "zones set" in SENT[-1][1], SENT)
check("another athlete's tap is ignored",
      B._handle_baseline_confirm("t", "555", "bl:yes:sam:bike", 5, {"555": {"slug": "ali"}}) is False)

shutil.rmtree(tmp, ignore_errors=True)
# ── 6. no race: a goal instead (lib/goals.py, 1 Oct 2026) ─────────────────────
fake_icu(ICU_TRI, {})
LOOKUPS = []
B._lookup_race = lambda name, d: LOOKUPS.append(name) or {}
SENT.clear()
asked = onboard("606", ["Tess Goal", "none",
                        "banana",          # not a goal: asked again
                        "ob:goal:ftp",     # the Raise my FTP button
                        "2 3",             # bike and run
                        "yes", KEY,
                        "2 years riding", "none", "8",
                        "1",               # strap
                        "yes",             # power
                        "0",               # FTP 250 was not a test
                        "about 240",       # rough FTP
                        "no",              # run threshold: not tested
                        "5k 25:00",        # rough run figure
                        "no", "no", "no",  # comparison race, heat, fuelling
                        "2", "tess"])
blob = "\n".join(asked)
check("race question offers 'none'", any("Not training for a race? Reply _none_" in t
                                         for c, t, _ in SENT if c == "606"))
check("no race asks the goal, with buttons", "What's the *goal* instead?" in blob
      and any(m and "Raise my FTP" in json.dumps(m) for c, t, m in SENT if c == "606"))
check("a non-goal answer is asked again", "Tap one of the goals" in blob)
check("then which sports", "Which sports do you want in your plan?" in blob)
check("no race is looked up", LOOKUPS == [], LOOKUPS)
check("no 'A goal for <race>' question", "*A goal* for" not in blob)
check("no swim questions for a bike + run plan", "100m or 400m" not in blob and "swim CSS" not in blob)
_ath = json.loads(B.ATHLETES_CONFIG.read_text()).get("tess") or {}
_prof = json.loads((tmp / "athletes/tess/profile.json").read_text())
_g = _ath.get("goal") or {}
check("athletes.json carries the goal, no race",
      _g.get("type") == "ftp" and _g.get("sports") == ["bike", "run"]
      and not _ath.get("race_date") and not _ath.get("race_name"), _ath)
check("block 1 waits for the baseline week (start not pinned yet)", "start" not in _g, _g)
check("profile: no race, the goal is the A goal, bike + run",
      not _prof.get("race_date") and _prof.get("a_goal") == "Raise my FTP"
      and _prof.get("sports") == ["bike", "run"], {k: _prof.get(k) for k in ("race_date", "a_goal", "sports")})
check("baseline tests bike and run only",
      json.loads((tmp / "athletes/tess/baseline.json").read_text())["sports"] == ["bike", "run"])
check("coach notice names the goal", any("Goal: Raise my FTP" in t for c, t, _ in SENT if c == "999"))

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
