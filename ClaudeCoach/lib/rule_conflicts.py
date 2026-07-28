#!/usr/bin/env python3
"""
Contradiction detector — mechanically diffs [perm] prose rules against the two things
that actually decide what gets planned: config/athletes.json day_rules, and the live
calendar. READ-ONLY: it never edits a rule, a config or an event.

The gap it fills. rules_lint already catches one class (a prose rule that withholds a
blueprint-required intensity slice). plan_audit already catches another (a calendar
session on a day day_rules forbids). Nothing compares PROSE against CONFIG or against the
CALENDAR, so a rule the coach wrote as permanent can be silently unenforced (config never
learned it) or silently breached (the build ignored it) with no report anywhere.

Five axes, each independently reportable:
  A config_gap        prose names a training day for a sport that day_rules does not
                      permit — or constrains a sport with no day_rules to enforce it.
  B calendar_breach   a planned session sits on a day the prose rule rules out.
  C intensity_breach  a prose rule reserves a named session for a given intensity and the
                      planned session's own text advertises a harder one. ADVISORY: it
                      reads session prose, so it can misfire; the matched cue is printed.
  D accepted_override a withholding rule the coach reviewed and ACCEPTED
                      (rules-lint-accepted.json) while the same week ships a session
                      serving exactly the slice it withholds.
  E expiry_conflict   a dated [expires:] exception contradicting a [perm] rule in the same
                      file — the two are both live, and only one can be true.

CLI (read-only):
  python3 lib/rule_conflicts.py --base-dir <dir> --all              # live calendar read
  python3 lib/rule_conflicts.py --base-dir <dir> --slug jamie --no-calendar
  python3 lib/rule_conflicts.py --base-dir <dir> --slug jamie --events-json events.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules_capture as rc
import rules_lint

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_RE = re.compile(
    r"\b(mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?s?\b", re.I)
_DAY_CANON = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "wednes": 2, "thu": 3,
              "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "satur": 5, "sun": 6}

# prose sport cue -> (day_rules key, ICU activity types)
SPORTS = {
    "swim": ("swim_days", ("Swim",)),
    "bike": ("bike_days", ("Ride", "VirtualRide", "GravelRide")),
    "run":  ("run_days",  ("Run",)),
}
_SPORT_RE = {
    "swim": re.compile(r"\bswim(?:s|ming)?\b", re.I),
    "bike": re.compile(r"\b(?:bike|ride|rides|riding|cycl\w+|turbo)\b", re.I),
    "run":  re.compile(r"\brun(?:s|ning)?\b", re.I),
}

# A rule only makes a DAY CLAIM if it binds the sport to the day. Without one of these the
# weekday is incidental ('confirmed 27 Jul', 'the Wednesday session went well').
_CLAIM_CUES = re.compile(
    r"\banchor\b|\block(?:ed|s)?\b|non-negotiable|\bfixed\b|\bonly\b|\bnever\b"
    r"|\bno \b|\bweekday\w*\b|\bweekend\w*\b|\bdefault\b|\bevery week\b"
    r"|\bdays?\s+(?:are|is)\b|\bpermitted\b|\bcap\b", re.I)
# Cues that make the claim EXCLUSIVE — the named session belongs on the named day(s) and
# nowhere else. Deliberately narrow: a bare 'only' or 'fixed' appears all over these files
# in senses that bind nothing ('no fixed target', 'only raise it if she asks'), and treating
# those as exclusivity turns one real anchor into a breach on every other day of the week.
_EXCLUSIVE_CUES = re.compile(
    r"\banchor\b|\block(?:ed|s)?\b|non-negotiable|weekdays? only|no weekend"
    r"|only on\s|\bdays?\s+(?:are|is)\b", re.I)

# Session KIND. An anchor binds a kind of session, not a whole sport: 'the long ride is
# Friday' does not forbid a Tuesday spin. Without this scoping, one anchor rule reports a
# breach against every other session of that sport in the week — noise that would bury the
# real finding. No qualifier (Calum's 'all cycling on weekdays only') = the whole sport.
_QUALIFIERS = (
    ("long", re.compile(r"\blong\b", re.I)),
    ("brick", re.compile(r"\bbrick\b", re.I)),
    ("css", re.compile(r"\bcss\b", re.I)),
    ("technique", re.compile(r"\btechnique\b|\bdrill\w*\b", re.I)),
    ("quality", re.compile(r"\b(?:quality|threshold|sweet ?spot|vo2|intervals?)\b", re.I)),
)

# Provenance trails ('confirmed 27 Jul 2026 after ...', 'Verified 28 Jul 2026 on the Wed
# CSS push') are audit history, not claims, yet they are full of weekday names and sport
# words. Strip from the reporting verb to the end of its sentence; the verb must be
# followed closely by a date, so a genuine rule clause is never eaten.
_PROVENANCE = re.compile(
    r"\b(?:set|confirmed|agreed|corrected|established|stated|added|reviewed|requested|"
    r"recurred|verified|extended|per rule)\b.{0,30}?\d{1,2}\s*"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[^.]*", re.I)

# Intensity vocabulary for axis C: what a rule RESERVES vs what a session ADVERTISES.
_RESERVES = re.compile(
    r"\bpure z2\b|\bz2 by default\b|\beasy by default\b|\bno sweet ?spot\b"
    r"|\bwithout an explicit request\b|\bno (?:threshold|vo2|intervals?)\b", re.I)
_HARD_SESSION = re.compile(
    r"\bsweet ?spot\b|\bthreshold\b|\bvo2\b|\brace[- ]?if\b|\brace[- ]?pace\b"
    r"|\bintervals?\b|\bhard\b|\bz4\b|\bz5\b|\b\d+\s*[x×]\s*\d+\b", re.I)


def _days_in(text: str) -> set:
    return {_DAY_CANON[m.group(1).lower()] for m in _DAY_RE.finditer(text)}


def _dayname(i: int) -> str:
    return DAYS[i].capitalize()


def _sports_in(text: str) -> set:
    return {s for s, pat in _SPORT_RE.items() if pat.search(text)}


def _sentences(body: str):
    return [x for x in re.split(r"(?<=[.;])\s+", body) if x.strip()]


def _nearest_sport(sentence: str, day_pos: int) -> str | None:
    """Which sport a weekday belongs to, when a sentence names more than one. The day is
    attributed to the CLOSEST sport cue by character distance — attributing it to every
    sport in the line is what turns one real claim into three false ones."""
    best, best_d = None, 10 ** 9
    for sport, pat in _SPORT_RE.items():
        for m in pat.finditer(sentence):
            d = abs(m.start() - day_pos)
            if d < best_d:
                best, best_d = sport, d
    return best


def day_claims(text: str) -> list:
    """Day claims made by a rule file's standing rules.

    [{'sport':'bike','days':{4},'exclusive':True,...,'rule':...}]

    A claim needs, IN ONE SENTENCE: a sport cue, a weekday (or weekday/weekend wording) and
    a binding cue. All three together are what make a weekday a RULE rather than a passing
    mention, and one sentence is the scope — the same discipline rules_lint uses to stop a
    pattern reaching across a full stop. Provenance trails are stripped first, and each day
    is attributed to the nearest sport cue only."""
    claims = []
    for raw in text.splitlines():
        line = raw.strip()
        if not (rc._PERM_RE.match(line) or rc._EXPIRES_RE.match(line)):
            continue
        body = rc._TAG_RE.sub("", line)
        expires = (None if rc._PERM_RE.match(line)
                   else rc._EXPIRES_RE.match(line).group(1))
        for sentence in _sentences(_PROVENANCE.sub(" ", body)):
            if not _CLAIM_CUES.search(sentence):
                continue
            weekday_only = bool(re.search(r"weekdays? only|no weekend", sentence, re.I))
            per_sport: dict = {}
            for m in _DAY_RE.finditer(sentence):
                sport = _nearest_sport(sentence, m.start())
                if sport:
                    per_sport.setdefault(sport, set()).add(_DAY_CANON[m.group(1).lower()])
            if weekday_only:
                for sport in _sports_in(sentence):
                    per_sport.setdefault(sport, set())
            if not per_sport:
                continue
            exclusive = bool(_EXCLUSIVE_CUES.search(sentence)) or weekday_only
            qualifier = next((q for q, pat in _QUALIFIERS if pat.search(sentence)), None)
            for sport, days in per_sport.items():
                claims.append({
                    "sport": sport,
                    "days": days,
                    "weekday_only": weekday_only,
                    "exclusive": exclusive,
                    "qualifier": qualifier,
                    "expires": expires,
                    "rule": body[:220],
                    "clause": sentence.strip()[:180],
                })
    return claims


def _finding(axis, code, severity, detail, rule="", cue=""):
    return {"axis": axis, "code": code, "severity": severity, "detail": detail,
            "rule": rule, "cue": cue}


# ---------------------------------------------------------------- axis A ----
def check_config(claims: list, day_rules: dict | None) -> list:
    out = []
    for c in claims:
        if c["expires"]:
            continue
        key = SPORTS[c["sport"]][0]
        if not day_rules:
            out.append(_finding("A", "config_absent", "hard",
                                f"prose constrains {c['sport']} to "
                                f"{sorted(_dayname(d) for d in c['days']) or 'weekdays'} "
                                f"but the athlete has NO day_rules — nothing can enforce it",
                                c["rule"]))
            continue
        allowed = day_rules.get(key)
        if allowed is None:
            out.append(_finding("A", "config_key_missing", "hard",
                                f"prose constrains {c['sport']} by day but day_rules has no "
                                f"{key} — unenforceable", c["rule"]))
            continue
        allowed_idx = {_DAY_CANON[d[:3].lower()] for d in allowed}
        gap = c["days"] - allowed_idx
        if gap:
            out.append(_finding("A", "config_gap", "hard",
                                f"prose names {sorted(_dayname(d) for d in gap)} for "
                                f"{c['sport']}; day_rules.{key}={allowed} omits it",
                                c["rule"]))
        if c["exclusive"]:
            extra = allowed_idx - c["days"] if c["days"] else \
                    allowed_idx & {5, 6} if c["weekday_only"] else set()
            if extra and c["weekday_only"]:
                out.append(_finding("A", "config_wider", "soft",
                                    f"prose limits {c['sport']} to weekdays; day_rules.{key} "
                                    f"permits {sorted(_dayname(d) for d in extra)}",
                                    c["rule"]))
    return out


# ---------------------------------------------------------------- axis B/C ----
def _ev_date(ev) -> date:
    return date.fromisoformat((ev.get("start_date_local") or "")[:10])


# 'Brick run off long ride' is not the long RUN. Match the session kind on the event NAME
# only, and reject a qualifier that is bound to a different sport's noun in that name.
_QUAL_OTHER_SPORT = re.compile(
    r"\b(?:long|key)\s+(ride|bike|cycl\w+|run|swim)\b", re.I)
_NOUN_SPORT = {"ride": "bike", "bike": "bike", "run": "run", "swim": "swim"}


def _matches_qualifier(ev, qualifier, sport) -> bool:
    if not qualifier:
        return True
    name = ev.get("name") or ""
    if not dict(_QUALIFIERS)[qualifier].search(name):
        return False
    for m in _QUAL_OTHER_SPORT.finditer(name):
        noun = m.group(1).lower()
        owner = _NOUN_SPORT.get(noun, "bike" if noun.startswith("cycl") else None)
        if owner and owner != sport:
            return False
    return True


def dedupe(findings: list) -> list:
    """One line per distinct contradiction. Several clauses of one rule can make the same
    claim (an anchor stated once and re-stated with its intensity), which would otherwise
    report the same breach twice."""
    seen, out = set(), []
    for f in findings:
        k = (f.get("slug"), f["axis"], f["code"], f["detail"])
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def _exception_days(claims: list, sport: str, qualifier, today: date) -> set:
    """Days an UNEXPIRED [expires:] exception adds for this sport/kind. The [expires]
    mechanism is the one part of the current surface that works, so a breach it explicitly
    permits must not be reported as one — otherwise the detector would punish the coach for
    using the tag correctly."""
    days = set()
    for c in claims:
        if not c["expires"] or c["sport"] != sport:
            continue
        if date.fromisoformat(c["expires"]) < today:
            continue
        if c["qualifier"] and qualifier and c["qualifier"] != qualifier:
            continue
        days |= c["days"]
    return days


def check_calendar(claims: list, events: list, today: date = None) -> list:
    """Axis B — a planned session of the claimed KIND sits on a day an exclusive prose
    claim rules out. Axis C (advisory) — a session of the claimed kind advertises an
    intensity the prose rule reserves against, wherever in the week it landed."""
    out = []
    by_sport = {}
    for ev in events:
        if (ev.get("category") or "WORKOUT").upper() != "WORKOUT":
            continue
        t = ev.get("type") or ""
        for sport, (_key, types) in SPORTS.items():
            if t in types:
                by_sport.setdefault(sport, []).append(ev)
    today = today or date.today()
    for c in claims:
        if c["expires"] or not (c["days"] or c["weekday_only"]):
            continue
        permitted = set(c["days"])
        if c["weekday_only"]:
            permitted |= {0, 1, 2, 3, 4}
        permitted |= _exception_days(claims, c["sport"], c["qualifier"], today)
        reserves = bool(_RESERVES.search(c["rule"]))
        for ev in by_sport.get(c["sport"], []):
            if not _matches_qualifier(ev, c["qualifier"], c["sport"]):
                continue
            try:
                d = _ev_date(ev)
            except ValueError:
                continue
            nm = (ev.get("name") or "")[:60]
            kind = f"{c['qualifier']} " if c["qualifier"] else ""
            if c["exclusive"] and d.weekday() not in permitted:
                out.append(_finding(
                    "B", f"{c['sport']}_prose_day_breach", "hard",
                    f"{ev.get('type')} '{nm}' on {d.isoformat()} ({d.strftime('%a')}) — prose "
                    f"reserves the {kind}{c['sport']} for "
                    f"{sorted(_dayname(x) for x in permitted)}", c["rule"]))
            if reserves:
                text = f"{ev.get('name') or ''} {ev.get('description') or ''}"
                m = _HARD_SESSION.search(text)
                if m:
                    out.append(_finding(
                        "C", f"{c['sport']}_intensity_breach", "soft",
                        f"{ev.get('type')} '{nm}' on {d.isoformat()} "
                        f"({d.strftime('%a')}) advertises '{m.group(0)}' but prose reserves the "
                        f"{kind}{c['sport']} for an easier stated intensity",
                        c["rule"], m.group(0)))
    return out


# ---------------------------------------------------------------- axis D ----
def check_accepted_overrides(slug: str, base, events: list) -> list:
    """A withholding rule the coach REVIEWED AND ACCEPTED, while the same week ships a
    session serving the very slice it withholds. Accepting a rule and then overriding it
    in the same build is the sharpest form of the contradiction: both sides are on record."""
    out = []
    accepted = rules_lint._accepted(slug, base)
    if not accepted:
        return out
    findings = rules_lint.lint_athlete(slug, base, include_accepted=True)
    for f in findings:
        if f.get("hash") not in accepted:
            continue
        types = SPORTS.get(f["sport"].lower(), (None, ()))[1]
        for ev in events:
            if (ev.get("type") or "") not in types:
                continue
            text = rules_lint._norm(f"{ev.get('name') or ''} {ev.get('description') or ''}")
            hit = ("high" if rules_lint._HIGH_WORDS.search(text) else
                   "z3" if rules_lint._Z3_ONLY.search(text) else None)
            if hit and hit in f["slices"]:
                d = (ev.get("start_date_local") or "")[:10]
                out.append(_finding(
                    "D", "accepted_rule_overridden", "hard",
                    f"{f['sport']} {hit} withheld by an ACCEPTED rule "
                    f"({f['hash']}), yet '{(ev.get('name') or '')[:60]}' on {d} "
                    f"serves that slice", f["rule"][:220]))
    return out


# ---------------------------------------------------------------- axis E ----
def check_expiry_conflicts(text: str) -> list:
    """A dated [expires:] exception that contradicts a [perm] rule in the same file."""
    out = []
    claims = day_claims(text)
    perms = [c for c in claims if not c["expires"]]
    temps = [c for c in claims if c["expires"]]
    for t in temps:
        for p in perms:
            if t["sport"] != p["sport"]:
                continue
            if p["weekday_only"] and (t["days"] & {5, 6}):
                out.append(_finding(
                    "E", "expiry_conflicts_perm", "soft",
                    f"[expires:{t['expires']}] permits {c_days(t)} for {t['sport']} while a "
                    f"[perm] rule restricts it to weekdays — both are live",
                    t["rule"]))
    return out


def c_days(c) -> str:
    return ", ".join(sorted(_dayname(d) for d in c["days"])) or "weekdays"


# ---------------------------------------------------------------- driver ----
def _load_events(cfg, slug, base, events_json, weeks, use_calendar):
    if events_json:
        return json.loads(Path(events_json).read_text(encoding="utf-8"))
    if not use_calendar:
        return []
    sys.path.insert(0, str(Path(base) / "lib"))
    from icu_api import IcuClient
    client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
    today = date.today()
    ws = today - timedelta(days=today.weekday())
    return client.get_events(ws.isoformat(), (ws + timedelta(days=7 * weeks - 1)).isoformat())


def check_athlete(slug: str, base, cfg: dict | None = None, events: list | None = None) -> list:
    base = Path(base)
    p = base / "athletes" / slug / "persistent-rules.md"
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    claims = day_claims(text)
    out = []
    if cfg is not None:
        out += check_config(claims, cfg.get("day_rules"))
    if events:
        out += check_calendar(claims, events)
        out += check_accepted_overrides(slug, base, events)
    out += check_expiry_conflicts(text)
    for f in out:
        f["slug"] = slug
    return dedupe(out)


def main():
    ap = argparse.ArgumentParser(description="Diff [perm] rules against day_rules and the calendar.")
    ap.add_argument("--base-dir", required=True)
    ap.add_argument("--slug", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--weeks", type=int, default=1)
    ap.add_argument("--events-json", help="read events from a file instead of the API")
    ap.add_argument("--no-calendar", action="store_true", help="axes A and E only")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    base = Path(a.base_dir)
    cfgs = {}
    cfg_path = base / "config" / "athletes.json"
    if cfg_path.exists():
        cfgs = json.loads(cfg_path.read_text(encoding="utf-8"))
    slugs = list(a.slug)
    if a.all:
        slugs = sorted(s for s, c in cfgs.items() if c.get("active", True))
    if not slugs:
        ap.error("give --slug or --all")

    findings = []
    for slug in slugs:
        cfg = cfgs.get(slug)
        events = _load_events(cfg, slug, base, a.events_json, a.weeks,
                              not a.no_calendar and cfg is not None)
        findings += check_athlete(slug, base, cfg=cfg, events=events)

    if a.json:
        print(json.dumps(findings, indent=2))
    else:
        if not findings:
            print("rule-conflicts: clean — no [perm] rule contradicts day_rules or the calendar")
        for f in findings:
            print(f"[{f['severity']}] {f['slug']}/{f['axis']} {f['code']}: {f['detail']}")
            if f.get("rule"):
                print(f"      rule: {f['rule'][:160]}")
    return 1 if any(f["severity"] == "hard" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
