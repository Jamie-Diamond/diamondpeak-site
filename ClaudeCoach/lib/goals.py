"""Goals without a race (Jamie, 1 Oct 2026: "make sure if an athlete has the ask to just
maintain fitness, or improve FTP or something non race related you know what to do").

An athlete with no A race ahead trains in repeating 6-week GOAL BLOCKS toward one goal:

    maintain  Keep my fitness  Fitness held in a band around where they started
    ftp       Raise my FTP     bike sweetspot / threshold / VO2; FTP test ends each block
    run       Run faster       threshold and short fast reps; 5k time trial ends each block
    swim      Swim faster      CSS and speed sets; CSS test ends each block
    fitter    Get fitter       Fitness rises slowly; no test, the Fitness trend is the measure

Each block is load, load, easier, load, load, easier + test (BLOCK_SHAPE). The weekly
load comes from plan_tools.required_tss (its goal branch), the session menu from the
library's non-race ("offseason") menu with this goal's zone split, and the test from a
booking (plan_tools.goal_test_bookings), so the validator insists on it exactly as on a
hand-booked test.

athletes.json `goal`:
    {"type": "ftp", "set": "2026-10-01",       the day it was chosen
     "start": "2026-10-12",                    Monday block 1 starts (optional, see below)
     "sports": ["bike", "run"],                what the plan covers (else profile.sports)
     "ctl_band": [44, 50],                     maintain only: pinned when known
     "start_values": {"ctl": 47, "ftp": 231}}  for the progress line on the Goals page

When `start` is missing the block starts the Monday after the sign-up baseline week (a
new athlete is tested first), else the Monday on or after `set`.

A race takes over: plan_tools.goal_active() is False while an A race is ahead and in the
recovery weeks after one. Afterwards the goal restarts at block 1 (plan_tools.goal_start;
it is what the athlete does between races too), unless an off-season block is
configured, which wins.

This module holds the definitions and pure helpers. It must not import plan_tools at
module level (plan_tools imports it).
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/

BLOCK_WEEKS = 6
BLOCK_SHAPE = ("load", "load", "easy", "load", "load", "test")
MAINTAIN_HALF_BAND = 3.0          # maintain: Fitness held within +/- this of the start
DEFAULT_RAMP_CAP = 3.0            # Fitness per week when the athlete has no ramp cap set

# Zone splits [Z1-2 / Z3 / Z4-5] in the library's own row format (plan_tools
# OFFSEASON_DISTRIBUTION). The goal sport gets the top-end-heavy row; the others are
# kept mostly easy so the quality lands where the goal is.
_FOCUS_ROW = {"bike": "70% Z1–2 / 12% Z3 / 18% Z4–5",
              "run": "75% Z1–2 / 10% Z3 / 15% Z4–5",
              "swim": "65% Z1–2 / 10% Z3 / 25% Z4–5"}
_SUPPORT_ROW = {"bike": "85% Z1–2 / 10% Z3 / 5% Z4–5",
                "run": "88% Z1–2 / 7% Z3 / 5% Z4–5",
                "swim": "80% Z1–2 / 10% Z3 / 10% Z4–5"}
_MAINTAIN_ROW = {"bike": "80% Z1–2 / 12% Z3 / 8% Z4–5",
                 "run": "85% Z1–2 / 8% Z3 / 7% Z4–5",
                 "swim": "75% Z1–2 / 12% Z3 / 13% Z4–5"}
_LIB_SPORT = {"bike": "Bike", "run": "Run", "swim": "Swim"}

GOALS = {
    "maintain": {
        "label": "Keep my fitness", "sport": None, "ramp": 0.0, "easy_factor": 0.85,
        "test": None, "rows": _MAINTAIN_ROW,
        "emphasis": ["threshold", "tempo", "css", "strides"],
        "focus": ("keep the habit and the fitness: one or two quality sessions a week spread "
                  "across their sports, everything else easy, and enough variety that it "
                  "stays enjoyable"),
    },
    "ftp": {
        "label": "Raise my FTP", "sport": "bike", "ramp": 1.5, "easy_factor": 0.70,
        "test": {"sport": "Ride", "name": "FTP test", "match": "FTP test"},
        "emphasis": ["sweetspot", "threshold", "over_under", "vo2"],
        "focus": ("FTP is the goal: two bike quality sessions a week, sweetspot and threshold "
                  "early in the block, threshold / over-unders and VO2 later; one longer easy "
                  "ride; other sports easy"),
    },
    "run": {
        "label": "Run faster", "sport": "run", "ramp": 1.5, "easy_factor": 0.70,
        "test": {"sport": "Run", "name": "5k time trial", "match": "5k"},
        "emphasis": ["cruise_intervals", "threshold", "reps", "strides"],
        "focus": ("running speed: two run quality sessions a week, threshold / cruise "
                  "intervals and short fast reps toward 5k pace, strides after an easy run, "
                  "one longer easy run; other sports easy"),
    },
    "swim": {
        "label": "Swim faster", "sport": "swim", "ramp": 1.0, "easy_factor": 0.70,
        "test": {"sport": "Swim", "name": "CSS test", "match": "CSS test"},
        "emphasis": ["css", "speed", "technique"],
        "focus": ("swim speed: CSS and speed sets in two or three swims a week with "
                  "technique in every swim; other sports easy"),
    },
    "fitter": {
        "label": "Get fitter", "sport": None, "ramp": 2.0, "easy_factor": 0.70,
        "test": None, "rows": _SUPPORT_ROW,
        "emphasis": ["tempo", "sweetspot", "strides"],
        "focus": ("build aerobic fitness steadily: mostly easy endurance, one quality "
                  "session a week, one longer session"),
    },
}
GOAL_ORDER = ("maintain", "ftp", "run", "swim", "fitter")

# Sign-up wording; the answers are buttons (telegram/bot.py _OB_BUTTONS). A typed
# 1-5 is still read as the GOAL_ORDER position.
SIGNUP_QUESTION = "No race -- no problem. What's the *goal* instead?"
SPORTS_QUESTION = "Which sports do you want in your plan?"

_NO_RACE = re.compile(
    r"^\s*(no|none|nope|n/?a|nothing|no race|no races|not yet|not really|no target|"
    r"no target race|nothing booked|nothing planned|no plans|not racing|"
    r"not training for (a|one|any)( race)?|just (training|fitness|for fun)|-)\s*[.!]*\s*$",
    re.I)


def is_no_race(text: str) -> bool:
    """True when the race answer says there is no race."""
    return bool(_NO_RACE.match(text or ""))


def parse_goal(text: str) -> str | None:
    """The goal key from a sign-up / chat answer: a number from SIGNUP_QUESTION or words."""
    t = (text or "").strip().lower()
    m = re.match(r"^\s*([1-5])\b", t)
    if m:
        return GOAL_ORDER[int(m.group(1)) - 1]
    for key, pat in (("ftp", r"\bftp\b|power|watts|cycl|bike|ride"),
                     ("swim", r"swim|\bcss\b|pool"),
                     ("run", r"\brun|5k|10k|parkrun|pace"),
                     ("maintain", r"maintain|keep|hold|stay"),
                     ("fitter", r"fit|weight|health|general|aerobic|base")):
        if re.search(pat, t):
            return key
    return None


def parse_sports(text: str) -> list:
    """Sports from the sports question, in swim/bike/run order. [] when unreadable."""
    t = (text or "").lower()
    got = set()
    for d in re.findall(r"[1-3]", t):
        got.add(("swim", "bike", "run")[int(d) - 1])
    for sp, pat in (("swim", r"swim"), ("bike", r"bike|cycl|ride"), ("run", r"\brun")):
        if re.search(pat, t):
            got.add(sp)
    return [s for s in ("swim", "bike", "run") if s in got]


def goal_cfg(cfg: dict) -> dict | None:
    """The athlete's goal block config, or None when they have none (or it is malformed)."""
    g = (cfg or {}).get("goal")
    if isinstance(g, dict) and g.get("type") in GOALS:
        return g
    return None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _on_or_after_monday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7)


def _iso(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _baseline_end(slug: str | None) -> date | None:
    if not slug:
        return None
    try:
        st = json.loads((BASE / "athletes" / slug / "baseline.json").read_text())
        return _iso((st.get("window") or {}).get("end"))
    except Exception:
        return None


def block_start(cfg: dict, slug: str | None = None) -> date | None:
    """Monday block 1 starts: the pinned `start`, else the Monday after the sign-up
    baseline week, else the Monday on or after the day the goal was set."""
    g = goal_cfg(cfg)
    if not g:
        return None
    pinned = _iso(g.get("start"))
    if pinned:
        return _monday(pinned)
    b_end = _baseline_end(slug)
    if b_end:
        return _monday(b_end) + timedelta(days=7)
    set_d = _iso(g.get("set"))
    return _on_or_after_monday(set_d) if set_d else None


def position(start: date | None, day: date) -> dict:
    """Where `day` falls: block (1-based), week in block (1-6), its kind, that week's
    Monday, and the training week counted from block 1. A day before block 1 reads as
    block 1 week 1 (the lead-in is planned like the first week)."""
    wk = _monday(day)
    n = max(0, (wk - start).days // 7) if start else 0
    return {"block": n // BLOCK_WEEKS + 1, "week": n % BLOCK_WEEKS + 1,
            "kind": BLOCK_SHAPE[n % BLOCK_WEEKS], "week_start": wk.isoformat(),
            "training_week": n + 1}


def sports_for(cfg: dict, profile: dict | None = None) -> list:
    """What the plan covers: the goal's own list, else the profile's, else the sports with
    standing days in day_rules, else the goal sport, else all three."""
    g = goal_cfg(cfg) or {}
    sp = [s for s in (g.get("sports") or (profile or {}).get("sports") or [])
          if s in _LIB_SPORT]
    if sp:
        return sp
    dr = (cfg or {}).get("day_rules") or {}
    sp = [s for s in ("swim", "bike", "run") if dr.get(f"{s}_days")]
    if sp:
        return sp
    focus = GOALS.get(g.get("type"), {}).get("sport")
    return [focus] if focus else ["swim", "bike", "run"]


def distribution(cfg: dict, profile: dict | None = None) -> dict:
    """{Bike|Run|Swim: 'x% Z1–2 / y% Z3 / z% Z4–5'} for the athlete's sports."""
    g = goal_cfg(cfg) or {}
    if isinstance(g.get("distribution"), dict) and g["distribution"]:
        return g["distribution"]                     # a per-athlete override
    gd = GOALS[g.get("type", "maintain")]
    focus = gd.get("sport")
    out = {}
    for sp in sports_for(cfg, profile):
        if focus:
            row = _FOCUS_ROW[sp] if sp == focus else _SUPPORT_ROW[sp]
        else:
            row = gd["rows"][sp]
        out[_LIB_SPORT[sp]] = row
    return out


def band(cfg: dict, ctl_today) -> tuple | None:
    """maintain: (lo, hi) Fitness band, pinned in config or centred on today's Fitness."""
    g = goal_cfg(cfg) or {}
    raw = g.get("ctl_band")
    try:
        lo, hi = float(raw[0]), float(raw[1])
        if 0 < lo < hi:
            return lo, hi
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    if ctl_today:
        c = float(ctl_today)
        return round(c - MAINTAIN_HALF_BAND, 1), round(c + MAINTAIN_HALF_BAND, 1)
    return None


def week_note(g: dict, pos: dict, kind: str, target, floor, ctl, why: str = "",
              band_lo_hi=None, test_name: str | None = None) -> str:
    """The planner's instruction for the week (the LLM reads it; never shown verbatim)."""
    gd = GOALS[g["type"]]
    head = (f"GOAL BLOCK (no race): \"{gd['label']}\". Block {pos['block']}, week "
            f"{pos['week']} of {BLOCK_WEEKS}. ")
    tail = (" There is no race: never mention a countdown, race prep or race pace. "
            "Call it their goal block, not an off-season.")
    if kind == "load":
        bit = f"Prescribe ~{target} TSS this week, not under ~{floor}. "
        if band_lo_hi:
            bit += (f"Keep Fitness (CTL) between {band_lo_hi[0]:g} and {band_lo_hi[1]:g} "
                    f"(now {float(ctl):g}). ")
        return (head + bit + f"Focus: {gd['focus']}. This is a TRAINING week: the quality "
                "goes where the goal is, everything else genuinely easy, no two hard "
                "sessions on consecutive days." + (" " + why if why else "") + tail)
    if kind == "test":
        tn = test_name or "test"
        return (head + f"TEST WEEK, end of block {pos['block']}: ~{target} TSS, an easier week "
                f"built around the {tn}. Book it mid-to-late week on fresh legs, named with "
                f"'{(gd.get('test') or {}).get('match', '')}', with nothing hard the day "
                "before; everything else easy. The result sets the zones for the next "
                "block." + tail)
    return (head + f"EASIER WEEK{(' (' + why + ')') if why else ''}: ~{target} TSS. Keep "
            "session frequency and one short quality touch in the goal sport; cut volume. "
            "Adaptation happens in the unload." + tail)


# ── setup (plan_tools goal-setup) ──────────────────────────────────────────────

def _profile(slug: str) -> dict:
    try:
        return json.loads((BASE / "athletes" / slug / "profile.json").read_text())
    except Exception:
        return {}


def start_values(profile: dict, ctl=None) -> dict:
    """The numbers progress is measured from, as known now."""
    out = {}
    if ctl:
        out["ctl"] = round(float(ctl), 1)
    for k, src in (("ftp", "ftp_watts"), ("run_threshold", "run_threshold_pace_per_km"),
                   ("css", "swim_css_per_100m")):
        if profile.get(src):
            out[k] = profile[src]
    return out


def make(goal_type: str, *, today: date | None = None, start: date | None = None,
         sports: list | None = None, ctl=None, profile: dict | None = None) -> dict:
    """A fresh `goal` config entry."""
    if goal_type not in GOALS:
        raise ValueError(f"unknown goal '{goal_type}' (one of {', '.join(GOAL_ORDER)})")
    today = today or date.today()
    g = {"type": goal_type, "set": today.isoformat(), "block_weeks": BLOCK_WEEKS}
    if start:
        g["start"] = _monday(start).isoformat()
    if sports:
        g["sports"] = [s for s in ("swim", "bike", "run") if s in sports]
    if goal_type == "maintain" and ctl:
        c = float(ctl)
        g["ctl_band"] = [round(c - MAINTAIN_HALF_BAND, 1), round(c + MAINTAIN_HALF_BAND, 1)]
    sv = start_values(profile or {}, ctl)
    if sv:
        g["start_values"] = sv
    return g


def pin_start(slug: str, config_path: Path | None = None, ctl=None,
              today: date | None = None) -> dict | None:
    """Fix block 1's Monday (and the maintain band / start values) once it is known: at the
    end of the sign-up baseline week, when the tested numbers are in. No-op when the
    athlete has no goal or it is already pinned."""
    p = Path(config_path or (BASE / "config" / "athletes.json"))
    data = json.loads(p.read_text())
    cfg = data.get(slug) or {}
    g = goal_cfg(cfg)
    if not g or g.get("start"):
        return None
    start = block_start(cfg, slug) or _on_or_after_monday(today or date.today())
    g["start"] = start.isoformat()
    prof = _profile(slug)
    sv = start_values(prof, ctl)
    if sv:
        g["start_values"] = sv            # the tested numbers beat the sign-up guesses
    if g["type"] == "maintain" and ctl and not g.get("ctl_band"):
        c = float(ctl)
        g["ctl_band"] = [round(c - MAINTAIN_HALF_BAND, 1), round(c + MAINTAIN_HALF_BAND, 1)]
    p.write_text(json.dumps(data, indent=2) + "\n")
    return g


# ── what the athlete sees (Goals page) and what the coach is told (chat) ───────

def _pace_s(v) -> float | None:
    m = re.match(r"^\s*(\d+):(\d{2})", str(v or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def progress(cfg: dict, profile: dict, ctl_now=None) -> list:
    """[{label, start, now, better}] for the goal's measure. `better` is True / False /
    None (no start value to compare)."""
    g = goal_cfg(cfg) or {}
    sv = g.get("start_values") or {}
    rows = []

    def row(label, start, now, higher_is_better, unit=""):
        if now in (None, ""):
            return
        better = None
        if start not in (None, ""):
            if isinstance(start, str) or isinstance(now, str):
                a, b = _pace_s(start), _pace_s(now)
                better = None if a is None or b is None or a == b else (b < a)
            elif float(now) != float(start):
                better = (float(now) > float(start)) == higher_is_better
        rows.append({"label": label, "start": start, "now": now, "unit": unit,
                     "better": better})

    t = g.get("type")
    if t == "ftp":
        row("FTP", sv.get("ftp"), profile.get("ftp_watts"), True, "W")
    elif t == "run":
        row("Threshold pace", sv.get("run_threshold"),
            profile.get("run_threshold_pace_per_km"), False, "/km")
    elif t == "swim":
        row("CSS", sv.get("css"), profile.get("swim_css_per_100m"), False, "/100m")
    if ctl_now is not None:
        row("Fitness", sv.get("ctl"), round(float(ctl_now), 1), True)
    return rows


def view(cfg: dict, profile: dict, slug: str | None = None, today: date | None = None,
         ctl_now=None) -> dict | None:
    """The Goals page's goal card: label, block / week, this week's kind, the next test
    and progress. None when the athlete has no goal."""
    g = goal_cfg(cfg)
    if not g:
        return None
    import plan_tools as _pt                 # lazy: plan_tools imports this module
    today = today or date.today()
    start = _pt.goal_start(cfg, slug, today)
    pos = position(start, today)
    gd = GOALS[g["type"]]
    next_test = None
    for b in _pt.goal_test_bookings(cfg, slug, today=today):
        ws = _iso(b["week_start"])
        if ws and ws + timedelta(days=6) >= today:
            next_test = {"name": gd["test"]["name"], "week_start": b["week_start"]}
            break
    lo_hi = band(cfg, None) if g["type"] == "maintain" else None
    return {"type": g["type"], "label": gd["label"],
            "started": start.isoformat() if start else None,
            "notStarted": bool(start and today < start),
            "block": pos["block"], "week": pos["week"], "blockWeeks": BLOCK_WEEKS,
            "kind": pos["kind"], "nextTest": next_test,
            "band": list(lo_hi) if lo_hi else None,
            "progress": progress(cfg, profile, ctl_now)}


_PROMPT = (
    "GOAL WITHOUT A RACE: {name} has no A race; their goal is \"{label}\". The plan runs "
    "6-week goal blocks (load, load, easier, load, load, easier + test); today is block "
    "{block}, week {week}{test}. Quote the block and the goal, never a race countdown or "
    "race prep, and don't call it an off-season.")
_SETUP = (
    "\nGOAL SETUP: if {name} has no race and wants to keep fit, raise their FTP, run or swim "
    "faster or just get fitter, or changes that goal, run `python3 "
    "ClaudeCoach/lib/plan_tools.py goal-setup --athlete {slug} --goal "
    "<maintain|ftp|run|swim|fitter>` (a PREVIEW), tell them in a few lines what the blocks "
    "and tests look like, and ONLY on their OK run it again with `--apply`. A booked A race "
    "takes over from the goal on its own; the goal resumes after the race's recovery weeks.")


def prompt_block(slug: str, first_name: str = "", path=None, today: date | None = None) -> str:
    """System-prompt lines for the coach: the goal block when one is running, and always
    how to set one up."""
    name = first_name or slug.title()
    try:
        cfg = (json.loads(Path(path or (BASE / "config" / "athletes.json")).read_text())
               or {}).get(slug) or {}
    except Exception:
        return ""
    out = ""
    g = goal_cfg(cfg)
    if g:
        try:
            import plan_tools as _pt
            active = _pt.goal_active(cfg, today)
        except Exception:
            active = False
        if active:
            today = today or date.today()
            pos = position(_pt.goal_start(cfg, slug, today), today)
            v = view(cfg, {}, slug, today) or {}
            nt = v.get("nextTest")
            test = (f"; next test: {nt['name']}, week of {nt['week_start']}" if nt else "")
            out = _PROMPT.format(name=name, label=GOALS[g["type"]]["label"],
                                 block=pos["block"], week=pos["week"], test=test)
    return out + _SETUP.format(name=name, slug=slug)
