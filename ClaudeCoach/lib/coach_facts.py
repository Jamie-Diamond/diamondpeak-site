#!/usr/bin/env python3
"""FACTS block — the small set of figures the model is allowed to make a claim about.

WHY (13 Aug 2026 full-log audit, three months of chat logs). The model does not just
mis-remember, it GENERATES recall with the same confidence as a lookup, and every
documented instance was a claim about a figure or a record:

  * "your CSS is 1:39" when the live value was 1:41 (a stale number from the prompt);
  * "that's the highest fuelling on your file" for a session that ranked TENTH;
  * "you've done four straight bike days" — no such run existed in the log;
  * a gels history, a swim and an RPE 8 that were never logged at all.

The fix that has held before is compute-and-inject (hand-rolled TSS arithmetic, 15 Jun;
the CTL figure that moved four times in 19 minutes, 3 Aug): the number is computed in
Python, injected into the turn, and the model quotes it. This module is that treatment
applied to the class of claim the audit found — superlatives, records, thresholds and
run-of-days claims — so a "highest on file" assertion is CHECKABLE AT GENERATION TIME
against a ranked list that is right there in the context.

OWNERSHIP, and why that matters here. Every figure has exactly ONE owner in the turn
context. Two blocks both stating FTP — one cached, one fresh — recreates the CSS
1:39/1:41 failure with extra steps, so this block OWNS the thresholds and
prefetch_context is reduced to PREFETCH_THRESHOLD_POINTER (which deliberately contains
no digits). It does NOT restate this week's Load: /week sums session-log.json while the
deterministic planning block sums ICU activities, and the two legitimately differ, so
the weekly figure keeps its existing owner and this block only cites it.

Files only, no network on the happy path: everything here is read from the athlete
directory each turn (so a session logged mid-conversation shows up), except the
thresholds dict, which the caller passes in from the one live resolve per turn.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

# Reduced prefetch line: prefetch_context must NOT restate a threshold figure, or the
# turn carries two numbers for the same thing. Asserted digit-free by the tests.
PREFETCH_THRESHOLD_POINTER = (
    "CURRENT THRESHOLDS: stated in the FACTS block below (resolved live this turn) — "
    "use those, never a number from this prompt or from earlier in the conversation.")

# The ONE rule. Deliberately a single imperative sentence: this prompt surface has a
# rule-bloat history (67 rules / 45KB before the last cull), so the block's other lines
# are labels and data, never further instruction.
FACTS_RULE = (
    "RULE: state a superlative, a record, a threshold or an \"N straight days\" claim "
    "ONLY if it appears in or follows arithmetically from the FACTS below — otherwise "
    "say what you would need to check.")

_BIKE_WORDS = ("ride", "bike", "cycl", "turbo", "spin", "zwift")
_RUN_WORDS = ("run", "jog", "treadmill")
_SWIM_WORDS = ("swim", "pool", "openwater", "open water")

# Recent-days strip length. Ten days covers any plausible "N straight days" claim
# (the invented one was four) without pushing the block over its size budget.
_STRIP_DAYS = 10
# Top-N per sport. Five is enough to show a claimed "highest" sitting mid-table.
_TOP_N = 5
# Size budget. The block is injected on EVERY turn, so it is capped in characters
# (~4 chars/token, so ~2400 chars ≈ 600 tokens — the budget the brief sets).
MAX_CHARS = 2400


def _family(sport: str) -> str | None:
    """Sport family for a session-log `sport` value. Mirrors bot._SPORT_TYPES' intent
    (VirtualRide is a bike day) but works on free-text sport names, which is what the
    session log actually stores."""
    s = (sport or "").lower()
    if any(w in s for w in _BIKE_WORDS):
        return "bike"
    if any(w in s for w in _RUN_WORDS):
        return "run"
    if any(w in s for w in _SWIM_WORDS):
        return "swim"
    return None


def load_session_log(athlete_dir: Path) -> list:
    """session-log.json, or [] if absent/unreadable. Never raises: a missing log must
    degrade the block, never lose the turn."""
    f = Path(athlete_dir) / "session-log.json"
    try:
        data = json.loads(f.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _load_profile(athlete_dir: Path) -> dict:
    try:
        return json.loads((Path(athlete_dir) / "profile.json").read_text())
    except Exception:
        return {}


def fuelling_ranking(entries: list, family: str, top_n: int = _TOP_N) -> list:
    """Sessions ranked by carbohydrate RATE, highest first, over the WHOLE log.

    Two traps this exists to avoid, both of which produced the "highest on your file"
    error: `nutrition_g_carb` is a session TOTAL in grams, not a rate, so the rate is
    total ÷ hours; and a 90-day window would have called the tenth-ranked session a
    record, so the ranking is all-time with no cutoff. Entries with no duration are
    EXCLUDED rather than divided — a rate that cannot be computed is not a datapoint.
    Returns dicts with date and duration so two sessions that round to the same g/hr
    stay distinguishable."""
    rows = []
    for e in entries:
        if _family(e.get("sport")) != family:
            continue
        carbs = e.get("nutrition_g_carb")
        dur = e.get("duration_min")
        try:
            carbs = float(carbs)
            dur = float(dur)
        except (TypeError, ValueError):
            continue
        if carbs <= 0 or dur <= 0:
            continue
        rows.append({
            "date": e.get("date") or "?",
            "g_hr": round(carbs / (dur / 60)),
            "g_total": round(carbs),
            "duration_min": round(dur),
        })
    rows.sort(key=lambda r: (-r["g_hr"], r["date"]))
    return rows[:top_n]


def _fuel_line(label: str, rows: list) -> str:
    if not rows:
        return f"{label} fuelling g/hr, all-time top {_TOP_N}: none on file (no session has both carbs and a duration)"
    bits = [f"{i}) {r['g_hr']}g/hr {r['date']} ({r['g_total']}g/{r['duration_min']}min)"
            for i, r in enumerate(rows, 1)]
    return f"{label} fuelling g/hr, all-time top {_TOP_N} (highest first): " + "  ".join(bits)


def today_sessions(entries: list, today: date) -> list:
    """Sessions logged for today, in log order."""
    iso = today.isoformat()
    return [e for e in entries if (e.get("date") or "") == iso]


def day_strip(entries: list, today: date, days: int = _STRIP_DAYS) -> list:
    """(date, [families]) for each of the last `days` days, oldest first — the evidence
    an "N straight days" claim needs. A day with no session is an explicit gap, which is
    what makes the invented "four straight bike days" visible as invented."""
    by_day: dict[str, list] = {}
    for e in entries:
        d = e.get("date") or ""
        fam = _family(e.get("sport")) or (e.get("sport") or "other").lower()
        by_day.setdefault(d, [])
        if fam not in by_day[d]:
            by_day[d].append(fam)
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        out.append((d, by_day.get(d, [])))
    return out


def _strip_line(strip: list) -> str:
    # Declarative, not imperative: the ONE rule is the block's only instruction (this
    # prompt surface has a rule-bloat history), so every other line is a labelled fact.
    bits = [f"{d[5:]}:{'+'.join(f) if f else '-'}" for d, f in strip]
    return (f"Last {len(strip)} days, sport logged per day ('-' = nothing logged; any run "
            f"of consecutive days is countable from this): " + " ".join(bits))


def thresholds_line(t: dict | None, lthr=None) -> str:
    """The single authoritative threshold line. `t` is a lib/thresholds.get_thresholds
    dict — the same resolver the plan engine and session library read, so the block
    cannot drift from what the planner used. LTHR is not in ICU sport-settings; it
    comes from the athlete's profile.json, the source generate-race-plan.py uses."""
    if not t:
        # Declarative: the ONE rule already forbids stating a threshold that is not here.
        base = ("Thresholds: UNRESOLVED this turn — no live FTP, CSS or run threshold is "
                "available, and no figure elsewhere in this context is current.")
        return base if not lthr else base[:-1] + f"; LTHR {lthr} bpm from profile."
    bits = []
    ftp = t.get("ftp_watts")
    if ftp:
        src = {"eftp": "live eFTP", "static": "ICU configured",
               "config": "config fallback"}.get(t.get("ftp_source"), t.get("ftp_source") or "?")
        bit = f"FTP {ftp}W ({src}"
        if t.get("ftp_source") == "eftp" and t.get("static_ftp") and int(t["static_ftp"]) != int(ftp):
            bit += f"; ICU configured {int(t['static_ftp'])}W"
        bits.append(bit + ")")
    if t.get("run_threshold_per_km"):
        bits.append(f"run threshold {t['run_threshold_per_km']}")
    if t.get("swim_css_per_100m"):
        bits.append(f"swim CSS {t['swim_css_per_100m']}")
    if lthr:
        bits.append(f"LTHR {lthr} bpm (profile)")
    line = "Thresholds (resolved live this turn, AUTHORITATIVE): " + " · ".join(bits or ["none resolvable"])
    missing = [n for n in (t.get("notes") or []) if "no " in n]
    if missing:
        line += "  [" + "; ".join(missing) + "]"
    return line


def build_facts_block(athlete_dir, thresholds: dict | None = None,
                      today: date | None = None, max_chars: int = MAX_CHARS) -> str:
    """The FACTS block for one turn. `athlete_dir` is injectable so this is testable
    against a fixture directory (the real athlete files are VM-only).

    Never raises: on any unexpected shape the block degrades to the rule plus whatever
    resolved, because a turn with no FACTS must still be a turn."""
    adir = Path(athlete_dir)
    today = today or date.today()
    entries = load_session_log(adir)
    prof = _load_profile(adir)

    lines = [
        f"=== FACTS (computed from your files this turn — {today.isoformat()}) ===",
        FACTS_RULE,
        thresholds_line(thresholds, prof.get("lthr")),
    ]

    ts = today_sessions(entries, today)
    if ts:
        bits = []
        for e in ts:
            b = f"{e.get('sport', '?')}"
            if e.get("duration_min"):
                b += f" {round(float(e['duration_min']))}min"
            if e.get("tss"):
                b += f" {round(float(e['tss']))} Load"
            bits.append(b + (" [awaiting feedback]" if e.get("stub") else ""))
        lines.append("Logged today: " + "; ".join(bits))
    else:
        lines.append("Logged today: nothing yet.")
    # This week's Load is NOT restated here on purpose — see the module docstring.
    lines.append("This week's Load: stated in the DETERMINISTIC PLANNING NUMBERS block, "
                 "which owns that figure.")

    lines.append(_strip_line(day_strip(entries, today)))
    lines.append(_fuel_line("Bike", fuelling_ranking(entries, "bike")))
    lines.append(_fuel_line("Run", fuelling_ranking(entries, "run")))

    out = "\n".join(lines)
    if len(out) > max_chars:
        # Truncating is honest as long as the model is told the list is cut: a silently
        # shortened ranking would let a mid-table session look like a record again.
        out = out[:max_chars - 40].rstrip() + "\n[FACTS truncated — ask before ranking]"
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Print the FACTS block for an athlete directory.")
    ap.add_argument("--dir", required=True, help="path to athletes/<slug>")
    args = ap.parse_args()
    print(build_facts_block(args.dir))
