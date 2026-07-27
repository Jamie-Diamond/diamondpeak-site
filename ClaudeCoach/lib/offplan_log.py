#!/usr/bin/env python3
"""Weekly roll-up of the "Off-plan in last 7 days" log in current-state.md.

That log is written every day by the prescription run and never read back: each
bullet records how a day went against the athlete's OWN persistent rules, and
nothing ever summarises it. This module turns the bullets inside a date window
into counts, so the weekly summary can say in one line how well the athlete held
to their own rules. Counts are computed here, not by the model, for the same
reason the TSS accounting is: the model must never do the arithmetic.

Three athletes, three shapes, all live in the tree today, so the date comes off
the bullet and the verdict comes out of the text:

    jamie    - **2026-07-26 GO** - Dorney Olympic Triathlon ...
    kathryn  - 2026-05-17: run_easy 60min - SKIPPED (sore throat, fatigue)
    calum    - 2026-07-22: Sweetspot Intervals 100min - SWAPPED to easy ...
               2026-07-26 (correction): 2026-07-25 Long Ride - corrected to NOT
               completed (watchdog checked history endpoint ...)

A correction bullet re-states an earlier day, so it is keyed to the day it
corrects and overrides whatever was logged for it at the time.

Stdlib only, no I/O: the caller reads current-state.md.
"""
from __future__ import annotations

import re

HEADING_RE = re.compile(r"^#{2,}\s+Off-plan in last 7 days\s*$", re.M)
_NEXT_HEADING_RE = re.compile(r"^#{1,6}\s+", re.M)
_BULLET_RE = re.compile(r"^-\s+(?:\*\*)?(\d{4}-\d{2}-\d{2})")
_BOLD_TAG_RE = re.compile(r"^-\s+\*\*\d{4}-\d{2}-\d{2}\s+([A-Z]+)\*\*")
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Verdict -> the words used in the athlete-facing line. Order here is the order
# they are reported in.
LABELS = [
    ("as_prescribed", "ran as prescribed"),
    ("modified", "adjusted by your own rules"),
    ("stood_down", "stood down by your own rules"),
    ("not_completed", "not completed"),
    ("rest", "rest day", "rest days"),
    ("logged", "logged without a verdict"),
]

_BOLD_TAG_MAP = {
    "GO": "as_prescribed",
    "MODIFIED": "modified",
    "SWAPPED": "modified",
    "BLOCKED": "stood_down",
    "SKIPPED": "stood_down",
}


def _section(text: str) -> str:
    """The Off-plan section body, or "" if the heading is absent."""
    m = HEADING_RE.search(text or "")
    if not m:
        return ""
    rest = text[m.end():]
    nxt = _NEXT_HEADING_RE.search(rest)
    return rest[:nxt.start()] if nxt else rest


def _verdict(head_line: str, body: str) -> str:
    """Classify one bullet. Bold tag wins where there is one (jamie); otherwise
    read the verdict out of the prose (kathryn, calum)."""
    m = _BOLD_TAG_RE.match(head_line)
    if m:
        return _BOLD_TAG_MAP.get(m.group(1), "logged")
    low = body.lower()
    # Specific phrase on purpose: a GO bullet can mention OTHER days that were
    # "confirmed NOT completed", and must not be reclassified by it.
    if "corrected to not completed" in low:
        return "not_completed"
    if "skipped" in low or "blocked" in low:
        return "stood_down"
    if "swapped" in low or "modified" in low:
        return "modified"
    if "rest day" in low:
        return "rest"
    if "go, execute as planned" in low or re.search(r"[-—–]\s*GO\b", body):
        return "as_prescribed"
    return "logged"


def parse_entries(current_state: str) -> list[dict]:
    """Every bullet in the Off-plan section, newest first as written.

    Each entry: {"date", "verdict", "text", "corrects"}. `corrects` is the date a
    correction bullet re-states (its key), else None.
    """
    body = _section(current_state)
    if not body:
        return []
    entries, head, buf = [], None, []

    def _flush():
        if head is None:
            return
        raw = "\n".join(buf).strip()
        corrects = None
        if "(correction)" in head.lower():
            # "- 2026-07-26 (correction): 2026-07-25 Long Ride - corrected to ..."
            dates = _ISO_RE.findall(raw)
            if len(dates) > 1:
                corrects = dates[1]
        entries.append({
            "date": _BULLET_RE.match(head).group(1),
            "verdict": _verdict(head, raw),
            "text": raw,
            "corrects": corrects,
        })

    for line in body.splitlines():
        if _BULLET_RE.match(line):
            _flush()
            head, buf = line, [line]
        elif head is not None and line.strip() and not line.startswith("- "):
            buf.append(line)
    _flush()
    return entries


def week_rollup(current_state: str, start: str, end: str) -> dict | None:
    """Roll the log up over [start, end] (ISO dates, inclusive), or None if the
    window holds no entries — an athlete whose log is silent gets no line at all,
    because "0 days logged" reads as "you did nothing this week"."""
    by_date: dict[str, dict] = {}
    for e in parse_entries(current_state):
        key = e["corrects"] or e["date"]
        if not (start <= key <= end):
            continue
        if e["corrects"]:                      # later knowledge wins
            by_date[key] = e
        else:
            by_date.setdefault(key, e)

    if not by_date:
        return None

    counts: dict[str, int] = {}
    for e in by_date.values():
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1

    parts = []
    for label in LABELS:
        key, singular = label[0], label[1]
        plural = label[2] if len(label) > 2 else singular
        n = counts.get(key, 0)
        if n:
            parts.append(f"{n} {singular if n == 1 else plural}")

    days = len(by_date)
    line = (f"Rule adherence {start} to {end}, from the Off-plan log: "
            f"{days} day{'' if days == 1 else 's'} logged — " + ", ".join(parts) + ".")

    # Breaches the log itself already names, so the model surfaces what was
    # flagged rather than inventing a judgement (e.g. jamie 2026-07-22: a stray
    # 15-min run against his own 40-min run minimum, persistent-rules #11).
    breaches = []
    for d in sorted(by_date, reverse=True):
        for m in re.finditer(r"breach\w*", by_date[d]["text"], re.I):
            s = max(0, m.start() - 200)
            breaches.append(f"{d}: …{by_date[d]['text'][s:m.end() + 200].strip()}…")

    return {
        "line": line,
        "counts": counts,
        "days": days,
        "breaches": breaches,
        "entries": [by_date[d] for d in sorted(by_date, reverse=True)],
    }


def prompt_block(rollup: dict | None) -> str:
    """The pre-computed block for the weekly-summary prompt."""
    if not rollup:
        return "(no Off-plan log entries inside this week — omit the rule-adherence line entirely)"
    out = [rollup["line"]]
    if rollup["breaches"]:
        out.append("Breaches the log itself names:")
        out += [f"  - {b}" for b in rollup["breaches"]]
    else:
        out.append("The log names no breach of the athlete's own rules this week.")
    out.append("Day entries in the window:")
    out += [f"  - {e['date']} [{e['verdict']}] {e['text']}" for e in rollup["entries"]]
    return "\n".join(out)
