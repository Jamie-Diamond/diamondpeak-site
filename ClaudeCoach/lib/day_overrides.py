#!/usr/bin/env python3
"""day_overrides.py — the register of COACH-DIRECTED day-rule deviations.

WHY THIS EXISTS. `day_rules.{swim,bike,run}_days` describe an athlete's normal
weekly pattern, and the coach overrides them conversationally: "I told it this week
to swim on wed, so we swim on wed, rules are guidelines." Jamie's
`swim_days=["Tue","Thu"]` is a true description of behaviour (Tue x7, Thu x7, Wed x0
since 1 Jun), so it must NOT be widened to include Wed — that would throw away the
signal. But `validate_plan` classified the directed Wednesday swim as a HARD
invariant breach, which makes the audit permanently red on `day_rules`, and a
permanently red check is one nobody reads.

So: a deviation the coach DIRECTED is recorded here and downgrades to a soft
`{sport}_directed_day` advisory that still appears in the report. A deviation
NOBODY asked for stays a hard `{sport}_forbidden_day` — a generator quietly
drifting off the pattern is a real defect and is exactly what day_rules exist to
catch.

GRANULARITY: PER SESSION (sport family + calendar date). Argued against the two
alternatives:
  * per-week  — one entry excuses every deviation of that sport in the week, so a
                second, UNDIRECTED move hides behind the directed one.
  * standing  — an amendment with no end date is how Calum's Saturday exception
                became permanent (see `<key>_days_expires` in validate_plan). It
                recreates the problem it is meant to solve.
A dated key is also self-expiring: an entry for a past date can never excuse a
future deviation, so the register cannot silently accumulate permissions.

ANTI-DRIFT: overrides cannot be used to move the pattern by stealth.
`validate_plan` counts entries per sport+weekday over a rolling window and raises a
HARD `day_rules_drifted` once the same weekday has been directed
DRIFT_THRESHOLD times — "this is not an exception any more, amend the config."

SHAPE mirrors the existing reviewed-exception register
`athletes/<slug>/reference/rules-lint-accepted.json` (flat map, stable id -> prose
recording what was accepted and when it was reviewed):

    {"swim:2026-07-29": "Coach-directed ... Recorded 2026-07-28."}

Key = "<family>:<YYYY-MM-DD>", family in {swim, bike, run} (the day_rules key
without its `_days` suffix, so Ride/VirtualRide/GravelRide share one family).
Value = free prose. A non-string or empty value is IGNORED, i.e. a malformed
register fails CLOSED: the deviation stays hard. Nothing can silence a check by
being broken.

The register lives under `athletes/` which is GITIGNORED, so it never appears in a
diff — same as rules-lint-accepted.json. Schema is documented in
docs/rule-lifecycle.md.

HOW AN ENTRY GETS IN. Today: the coach hand-edits the JSON, or runs the CLI below.
The instructions themselves arrive in Telegram, so the durable fix is a call to
`record()` from the bot at the moment of instruction — see docs/rule-lifecycle.md
for the exact follow-up.

CLI (the only writer today):
  python3 lib/day_overrides.py --base-dir ClaudeCoach --slug jamie --list
  python3 lib/day_overrides.py --base-dir ClaudeCoach --slug jamie \
      --sport swim --date 2026-07-29 --note "Coach-directed in Telegram 27 Jul."
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# day_rules key -> register family. Kept in step with validate_plan._SPORT_RULE:
# several ICU activity types collapse onto one family.
FAMILIES = ("swim", "bike", "run")
_KEY_RE = re.compile(r"^(swim|bike|run):(\d{4}-\d{2}-\d{2})$")


def register_path(slug: str, base) -> Path:
    return Path(base) / "athletes" / slug / "reference" / "day-rules-overrides.json"


def load(slug: str, base) -> dict[str, str]:
    """Valid entries only. A missing, unreadable or corrupt register yields {} —
    never an exception, and never a permission: every dropped entry means the
    deviation it would have excused stays a hard failure."""
    p = register_path(slug, base)
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(blob, dict):
        return {}
    out = {}
    for k, v in blob.items():
        if k.startswith("_"):          # allow a _note key, like the other registers
            continue
        if not _KEY_RE.match(str(k)):
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        out[str(k)] = v.strip()
    return out


def has_override(slug: str, base, sport_or_family: str, on: date | str) -> bool:
    """True when this sport+date is ALREADY on the register.

    Added 2026-08-13 after the bot asked the athlete to confirm `bike:2026-08-13` twice
    inside three minutes (07:08:56, 07:11:33) and recorded it twice. A second ask is not
    just noise: the athlete has already answered, so being asked again reads as the bot
    not having heard them, and the repeat write pushes the same sport+weekday towards
    validate_plan's `day_rules_drifted` count for a deviation that was directed ONCE.

    Reads through `load`, so it inherits fail-closed: an unreadable register yields {} and
    this returns False, i.e. the ask happens again rather than being silently suppressed.
    Suppressing an ask on a register we could not read would be the wrong way round —
    a duplicate question is cheap, a lost permission is not."""
    return key(sport_or_family, on) in load(slug, base)


def key(sport_or_family: str, on: date | str) -> str:
    fam = str(sport_or_family).strip().lower()
    fam = {"ride": "bike", "virtualride": "bike", "gravelride": "bike",
           "cycling": "bike"}.get(fam, fam)
    d = on if isinstance(on, str) else on.isoformat()
    return f"{fam}:{d[:10]}"


def record(slug: str, base, sport: str, on: date | str, note: str) -> str:
    """Add/replace one override. Returns the key written.

    This is the function the Telegram bot should call when the coach directs a
    session onto an off-pattern day; it is deliberately the whole write surface, so
    the follow-up is one call site and no new format."""
    k = key(sport, on)
    if not _KEY_RE.match(k):
        raise ValueError(f"bad override key {k!r} — family must be one of {FAMILIES}")
    if not (note or "").strip():
        raise ValueError("an override must carry a note saying who directed it and when")
    p = register_path(slug, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(blob, dict):
            blob = {}
    except Exception:
        blob = {}
    blob[k] = note.strip()
    p.write_text(json.dumps(blob, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return k


# ---------------------------------------------------------------------------
# CAPTURE — recognising a DIRECTED deviation in chat
# ---------------------------------------------------------------------------
# The register works, and until now the only way into it was a human editing the JSON or
# running the CLI above: Jamie's directed Wednesday swim is on file because an agent
# hand-wrote it. The instructions themselves arrive in Telegram ("I told it this week to
# swim on wed"), so this is the parser that lets one land at the moment it is said.
#
# FAIL-CLOSED IS THE WHOLE POINT, and it is stricter here than in any other capture in the
# tree. Every other parser risks writing something wrong that a human then sees. This one
# risks SILENCING A CHECK: a wrongly-recorded override turns a hard `*_forbidden_day`
# failure into a soft advisory, so a generator genuinely drifting off an athlete's pattern
# stops being reported. A missed capture costs one hand-edit. A wrong one costs the check.
# So every one of these must hold, and any doubt records NOTHING:
#
#   1. A SPORT the register has a family for.
#   2. A DIRECTIVE, not a question ("should I swim Wednesday?") and not a REPORT of what
#      already happened ("I swam on Wednesday"). The report case is the dangerous one: it
#      would retro-excuse a deviation NOBODY directed, which is exactly the fail-open the
#      register exists to prevent.
#   3. AN UNAMBIGUOUS DATE. A bare weekday already past in the current week could mean
#      that day or the same day next week, and guessing writes a permission for a day the
#      athlete did not name. Refused.
#   4. THE DAY MUST ACTUALLY BE OFF-PATTERN for that sport. An override for a day already
#      in `{sport}_days` excuses nothing, and it is not harmless: validate_plan counts
#      entries per sport+weekday and raises `day_rules_drifted` at DRIFT_THRESHOLD, so
#      redundant entries would push a real pattern towards a false drift alarm.
#
# The caller confirms with the athlete before writing on top of all this, which is what
# makes "if you cannot be confident, record nothing" true by construction rather than by
# the parser being clever enough.

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday")
_WEEKDAY_ABBR = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Tue/Tues, Thu/Thur/Thurs all work; the pattern is anchored so "sunday" cannot match "sun"
# inside "sunny".
_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(f"{a}(?:{n[3:]})?|{a}s?" for a, n in zip(_WEEKDAY_ABBR, _WEEKDAY_NAMES))
    + r")\b", re.I)

_SPORT_WORDS = {
    "swim": "swim", "swimming": "swim", "swims": "swim", "pool": "swim", "ows": "swim",
    "bike": "bike", "biking": "bike", "ride": "bike", "riding": "bike", "cycle": "bike",
    "cycling": "bike", "turbo": "bike", "spin": "bike",
    "run": "run", "running": "run", "runs": "run", "jog": "run",
}
_SPORT_RE = re.compile(r"\b(" + "|".join(sorted(_SPORT_WORDS, key=len, reverse=True)) + r")\b", re.I)

# PAST TENSE / REPORT. Checked before anything else: "I swam on Wednesday" is a statement
# about history, and turning it into a directed-day permission would excuse a deviation
# nobody asked for.
_REPORT_RE = re.compile(
    r"\b(?:swam|ran|rode|cycled|did|done|completed|finished|managed|logged|"
    r"ended\s+up|went\s+for|just\s+got\s+back)\b|\b(?:yesterday|last\s+week)\b", re.I)

# A question, or a request for advice — never an instruction.
_DAY_QUESTION_RE = re.compile(
    r"^\s*(?:should|shall|can|could|would|do|does|did|is|are|was|when|what|which|why|how|"
    r"any)\b|\bshould\s+i\b|\bwhat\s+(?:if|about)\b|\bor\s+(?:should|shall)\b", re.I)

# A DIRECTIVE. Either an imperative ("swim Wednesday", "move the swim to Wed") or an
# explicit statement of intent ("I'm swimming Wednesday", "let's swim Wed", "swap the
# swim to Wednesday"). Deliberately a positive requirement rather than "not a question":
# an unrecognised phrasing must fall through to nothing, not to a write.
_DIRECTIVE_RE = re.compile(
    r"\b(?:i\s+told\s+(?:it|you|him|her|them|the\s+\w+)|"
    r"move|moving|shift|swap|switch|put|make\s+it|do|let'?s|lets|"
    r"i'?m\s+(?:going\s+to\s+)?(?:swim|swimming|bike|biking|riding|ride|run|running)|"
    r"i\s+(?:will|shall)|we'?re|we\s+(?:will|shall)|this\s+week\s+(?:i|we))\b"
    r"|^\s*(?:swim|bike|ride|run|cycle|turbo)\b"
    r"|\b(?:instead|rather\s+than)\b", re.I)

_THIS_WEEK_RE = re.compile(r"\bthis\s+week\b", re.I)
_NEXT_WEEK_RE = re.compile(r"\bnext\s+week\b", re.I)


def _weekday_index(token: str):
    t = token.lower().rstrip("s")
    for i, (abbr, name) in enumerate(zip(_WEEKDAY_ABBR, _WEEKDAY_NAMES)):
        if t == abbr or name.startswith(t[:3]) and (t == name or t.startswith(abbr)):
            return i
    return None


def resolve_directed_date(text: str, today: date | None = None):
    """The single calendar date a directed-day instruction names, or None when it is not
    unambiguous.

    NOT lib/races.resolve_date, on purpose. That helper rolls a bare weekday FORWARD and
    turns a weekday said ON that weekday into +7 - correct for "I'm racing on Saturday",
    wrong here, where it would write a permission for a date a week away from the one the
    athlete meant. The rules instead:

      * an explicit ISO date is taken as given;
      * "today" / "tomorrow" resolve directly;
      * "<weekday> this week" is that weekday inside the CURRENT Monday-Sunday week, and
        is refused if it lands outside it;
      * "<weekday> next week" is that weekday in the following week;
      * a BARE weekday resolves only when it is today or still to come this week. Already
        past in this week it could mean that day or the same day next week, and a guess
        writes a permission for a day nobody named, so it is refused.
    """
    t = (text or "").strip()
    if not t:
        return None
    base = today or date.today()

    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None

    low = t.lower()
    monday = base - timedelta(days=base.weekday())
    if re.search(r"\btomorrow\b", low):
        return base + timedelta(days=1)
    if re.search(r"\btoday\b", low):
        return base

    wm = _WEEKDAY_RE.search(t)
    if not wm:
        return None
    # More than one weekday named is not one session — "swim Wed or Thu" is a choice, and
    # the register's granularity is one sport on one date.
    if len(_WEEKDAY_RE.findall(t)) > 1:
        return None
    idx = _weekday_index(wm.group(1))
    if idx is None:
        return None

    if _NEXT_WEEK_RE.search(low):
        return monday + timedelta(days=7 + idx)
    cand = monday + timedelta(days=idx)
    if _THIS_WEEK_RE.search(low):
        # Explicitly this week. `cand` is by construction inside it, but keep the assertion
        # so a future change to the arithmetic cannot silently widen the window.
        return cand if monday <= cand <= monday + timedelta(days=6) else None
    # Bare weekday: only today or later this week.
    return cand if cand >= base else None


# A relative-day vocabulary resolve_directed_date does not need but named_dates does:
# "tonight" and "this evening" are today, and a message can name several of these at once.
_RELATIVE_DAYS = (
    (re.compile(r"\byesterday\b", re.I), -1),
    (re.compile(r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening|arvo))\b", re.I), 0),
    (re.compile(r"\btomorrow\b", re.I), 1),
)


def named_dates(text: str, today: date | None = None) -> set:
    """EVERY calendar date this message names, as ISO strings. Never raises.

    Added 18 Aug 2026 for bug #30 part (b) — the scope check that catches a whole-week
    replan the athlete never asked for (Kathryn, 16 Aug: "2.5 hours" meant one day; her
    week was rebuilt). That check compares the days the MESSAGE named against the days the
    calendar diff actually touched, and undoes the excess if nobody answers the card. So
    this function's errors are not symmetric:

      * naming a day the athlete did not mean only SUPPRESSES a card, and the athlete is
        left exactly where they are today;
      * MISSING a day they did name lets a timer delete work they explicitly asked for.

    Every judgement below therefore breaks towards naming more days, which is the opposite
    of resolve_directed_date's contract and the reason this is a separate function rather
    than a loop around it. resolve_directed_date exists to write a PERMISSION into a
    register that outlives the conversation, so it refuses anything less than one
    unambiguous date - two weekdays, or a bare weekday already gone by, both return None.
    Reusing it here would read "move Wed and Fri" as authorising nothing at all.

    Same tokens either way: this shares _WEEKDAY_RE, _weekday_index and the this/next-week
    framing with resolve_directed_date, so a message the register understands and a message
    the scope check understands can never disagree about which Thursday is meant.

    The one genuine ambiguity - a BARE weekday already past in this week, which could be
    that day or the same day next week - yields BOTH dates rather than neither. Two dates
    out of a 21-day planning window is still a scope, and it cannot cost the athlete a
    session they asked for.
    """
    out: set = set()
    t = (text or "").strip()
    if not t:
        return out
    base = today or date.today()

    for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", t):
        try:
            out.add(date.fromisoformat(m.group(1)).isoformat())
        except ValueError:
            continue

    for rx, delta in _RELATIVE_DAYS:
        if rx.search(t):
            out.add((base + timedelta(days=delta)).isoformat())

    monday = base - timedelta(days=base.weekday())
    low = t.lower()
    next_week = bool(_NEXT_WEEK_RE.search(low))
    this_week = bool(_THIS_WEEK_RE.search(low))
    for tok in _WEEKDAY_RE.findall(t):
        idx = _weekday_index(tok)
        if idx is None:
            continue
        if next_week:
            out.add((monday + timedelta(days=7 + idx)).isoformat())
            continue
        cand = monday + timedelta(days=idx)
        out.add(cand.isoformat())
        if not this_week and cand < base:
            # Bare weekday, already gone by. Both readings, for the reason in the docstring.
            out.add((cand + timedelta(days=7)).isoformat())
    return out


def parse_directed_day(text: str, today: date | None = None) -> dict:
    """A directed-day instruction, or a refusal with the reason.

    Returns {"family", "date", "refused"}. `family`/`date` are both set only when every
    test passed; otherwise `refused` says which one did not. The caller must ALSO check the
    day is off-pattern (see `is_off_pattern`) and confirm with the athlete before writing.
    """
    out = {"family": None, "date": None, "refused": ""}
    t = (text or "").strip()
    if not t:
        out["refused"] = "empty"
        return out
    if t.endswith("?") or _DAY_QUESTION_RE.search(t):
        out["refused"] = "question, not a direction"
        return out
    if _REPORT_RE.search(t):
        # The dangerous case: retro-excusing a deviation nobody directed.
        out["refused"] = "reports what already happened, not a direction"
        return out
    if not _DIRECTIVE_RE.search(t):
        out["refused"] = "no directive phrasing"
        return out

    sm = _SPORT_RE.search(t)
    if not sm:
        out["refused"] = "no sport named"
        return out
    if len({_SPORT_WORDS[s.lower()] for s in _SPORT_RE.findall(t)}) > 1:
        out["refused"] = "more than one sport named"
        return out
    fam = _SPORT_WORDS[sm.group(1).lower()]

    d = resolve_directed_date(t, today)
    if d is None:
        out["refused"] = "no unambiguous single date"
        return out

    out["family"] = fam
    out["date"] = d.isoformat()
    return out


def is_off_pattern(day_rules: dict | None, family: str, on: date | str) -> bool:
    """True when `family` on that date is NOT one of the athlete's normal days, i.e. there
    is a hard `{family}_forbidden_day` for the override to downgrade.

    False when the day is already on-pattern (nothing to excuse) AND when `day_rules` has
    no list for that sport at all — in that case validate_plan raises no forbidden-day
    failure to begin with, so an override would be a permission with nothing to permit.
    Both cases must record nothing: entries are counted per sport+weekday for the
    `day_rules_drifted` alarm, so redundant ones push a real pattern towards a false alarm.
    """
    key = f"{family}_days"
    days = (day_rules or {}).get(key)
    if not isinstance(days, list) or not days:
        return False
    d = date.fromisoformat(on) if isinstance(on, str) else on
    abbr = _WEEKDAY_ABBR[d.weekday()].capitalize()
    return abbr not in {str(x)[:3].capitalize() for x in days}


def capture_note(source: str = "Telegram", on_date: date | None = None) -> str:
    """The note stored with an override. `record` requires one, because an entry with no
    provenance is indistinguishable from a permission somebody granted by accident."""
    return (f"Coach-directed in {source}, confirmed by the athlete. "
            f"Recorded {(on_date or date.today()).isoformat()}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-dir", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sport", help="swim | bike | run (or an ICU type: Ride, GravelRide…)")
    ap.add_argument("--date", help="YYYY-MM-DD — the session date being excused")
    ap.add_argument("--note", help="who directed it and when")
    a = ap.parse_args()
    if a.list or not (a.sport and a.date and a.note):
        reg = load(a.slug, a.base_dir)
        if not reg:
            print(f"{a.slug}: no day-rule overrides on record")
        for k, v in sorted(reg.items()):
            stale = "" if k.split(":")[1] >= date.today().isoformat() else "  (past)"
            print(f"{k}{stale}: {v}")
        return 0
    k = record(a.slug, a.base_dir, a.sport, a.date, a.note)
    print(f"recorded {k} for {a.slug} -> {register_path(a.slug, a.base_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
