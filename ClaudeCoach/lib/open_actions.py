#!/usr/bin/env python3
"""Open actions — ONE store, arithmetic in Python.

Why this exists. Until 28 Jul 2026 the open-actions list lived in two places that
could not agree and neither of which could close an item:

  - `current-state.json` `open_actions[]` — real ISO dates and statuses, read by
    the watchdog's T9 (as a PROMPT instruction: "cross-check open_actions ...").
  - the "Open actions" section of `current-state.md` — hand-maintained prose, read
    by weekly-summary.py Step 4 (also as a prompt instruction).

Every status cell in Jamie's markdown table was still its template placeholder
(`[pending / booked / done]`), and the weekly prompt ruled that a placeholder counts
as not done. So by construction nothing the coach read weekly could ever be marked
done, and the same nine lines rendered every week for three months. Two of Jamie's
items had by then been superseded (FTP retest, CSS test) and one dropped (Dorney) in
the JSON store without the markdown ever noticing. Kathryn's markdown carried four
items her JSON did not carry at all — her JSON had no `open_actions` key.

The fix follows the lesson already written at lib/plan_builder.py:5-7 — "the
single-LLM generator ignores instructions ... take the mechanical parts out of the
LLM's hands" — and copies the T12 pattern in scripts/watchdog.py: compute in Python,
inject RENDERED FACTS into the prompt, and tell the model not to recompute. The two
surfaces (weekly card, watchdog T9) now call the same function, so they cannot
disagree again.

Schema — current-state.json "open_actions": [ ... ]

    {
      "action":  "Book Precision Hydration sweat-sodium test",  # required, short label
      "due":     "2026-05-31",       # ISO date, or null for genuinely open-ended
      "due_label": "rolling",        # optional display text, only when due is null
      "status":  "pending",          # enum, see STATUSES below
      "status_note": "replaced by eFTP 297W, per Jamie 2026-06-05",
                                     # optional prose tail — provenance, never a date to parse
      "gates_race_decision": true,   # optional, default false. True ONLY for items named
                                     # in reference/decision-points.md
      "raised":  "2026-05-25",       # optional ISO date the item was first raised. Only useful
                                     # on an open-ended item: it is the one thing that lets an
                                     # item with no due date escalate on age instead of sitting
                                     # invisible forever (Calum's and all three of Kathryn's
                                     # items are open-ended, so without it nothing about them
                                     # ever gets worse). Never inferred — set it or leave it.
      "closed":  "2026-07-28",       # optional ISO date, set by set_status() on a close
      "closed_by": "jamie via telegram",
                                     # optional, set by set_status() on a close. WHO closed
                                     # it, so a closed item is distinguishable from one that
                                     # was never captured at all: both read as "not open",
                                     # and only this field says the difference. Additive —
                                     # scripts/refresh-site-data.py:718 passes open_actions[]
                                     # straight through and iterates no keys.
      "owner":   "Jamie"             # optional
    }

`action`, `due` and `status` are the three fields that existed before; they are never
renamed or removed, because scripts/refresh-site-data.py:718 and the athlete *.html
dashboards read them. Everything else here is additive.

`due` is ISO-or-null. There is no free-text date any more: migration gave every item a
real date or an explicit null, which is why the weekly prompt no longer needs (or has)
a paragraph teaching a model how to read "end May" or "before June". A null `due` means
open-ended, and an open-ended item is never reported as overdue — it is reported as
open-ended, which is a different problem.

Statuses. OPEN_STATUSES are in-flight and get surfaced; CLOSED_STATUSES are not
surfaced at all. `noted` is for a line that was recorded as context and was never an
action — it closes on arrival. `booked`/`ordered`/`scheduled` exist because the real
data needs them: an aero helmet that has been ordered is neither pending nor done, and
collapsing that into one of the two is what made the old placeholder cells useless.

Age escalation. Nothing before this distinguished "4 days late" from "abandoned". Bands
are on days overdue: LATE 1-13, SLIPPING 14-41, ABANDONED 42+. `gates_race_decision`
bumps an item up one band, so an item that gates a race decision reads worse at the same
age than one that does not. An IN_FLIGHT status (booked/ordered/scheduled) replaces the
band with `in_flight`: still late, still counted, but no longer abandoned. An OPEN-ENDED item (no due date) escalates on days since
`raised`, when `raised` is set; with no `raised` it cannot age, and the render says so
rather than pretending it is fine.

Closing an item. That is the whole point, and it is now possible:

    python3 ClaudeCoach/lib/open_actions.py --athlete jamie \
        --set-status done --action "sweat-sodium" --note "tested 2026-08-02, 1100 mg/L"

set_status() does the tmp + os.replace atomic write (same as lib/illness.py), because
current-state.json is also written by watchdog.ramp_flags() and read by jobs every few
minutes; a truncated-file window would cost the whole state file. No model is allowed to
edit statuses on the athlete's behalf — see the prompt blocks below.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/

OPEN_STATUSES   = ("pending", "booked", "ordered", "scheduled")
# Open, but action HAS been taken. A booked sweat test is still late, and the day count
# still says so, but "abandoned" is the wrong word for it and the wrong nudge to give.
IN_FLIGHT_STATUSES = ("booked", "ordered", "scheduled")
CLOSED_STATUSES = ("done", "dropped", "noted")
STATUSES        = OPEN_STATUSES + CLOSED_STATUSES

DUE_SOON_DAYS = 14          # matches the window the weekly card has always used
T9_WINDOW_DAYS = 7          # matches the watchdog's long-standing T9 window

# days overdue -> escalation band
LATE_FROM      = 1
SLIPPING_FROM  = 14
ABANDONED_FROM = 42

_BANDS = ("none", "late", "slipping", "abandoned")

MARKERS = {"overdue": "🔴", "due_soon": "⚠️", "open_ended": "📋", "upcoming": "📅"}


# ---------------------------------------------------------------------------
# read + normalise
# ---------------------------------------------------------------------------

def _load_state(athlete_dir) -> dict:
    p = Path(athlete_dir) / "current-state.json"
    try:
        return json.loads(p.read_text()) or {}
    except Exception:
        return {}


def _parse_iso(value):
    """ISO date or None. A malformed date is treated as absent rather than raised:
    one bad cell must not take out the whole open-actions surface on both prompts."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _status_of(raw: dict) -> tuple[str, str]:
    """(status, status_note) from a possibly-legacy entry.

    Legacy statuses were prose carrying provenance — "done — replaced by eFTP 297W;
    no formal test protocol per Jamie 2026-06-05". The enum is the first word; the
    prose tail is preserved, because destroying it destroys the audit trail.
    """
    note = (raw.get("status_note") or "").strip()
    raw_status = (raw.get("status") or "pending").strip()
    head = raw_status.lower()
    for sep in (" — ", " - ", " -- ", ": "):
        if sep in raw_status:
            head, tail = raw_status.split(sep, 1)
            head = head.strip().lower()
            note = note or tail.strip()
            break
    head = head.split()[0].rstrip(".,;:") if head else "pending"
    if head not in STATUSES:
        # unknown value: treat as open, never silently as done
        note = note or raw_status
        head = "pending"
    return head, note


def _escalation(days_overdue: int, gates: bool) -> str:
    if days_overdue < LATE_FROM:
        band = 0
    elif days_overdue < SLIPPING_FROM:
        band = 1
    elif days_overdue < ABANDONED_FROM:
        band = 2
    else:
        band = 3
    if gates and band:
        band = min(band + 1, 3)
    return _BANDS[band]


def evaluate_from_dir(athlete_dir, today: date | None = None) -> list[dict]:
    """Every entry in open_actions[], normalised, with the arithmetic already done.

    Returns one dict per entry (closed ones included, flagged `open: False`) in the
    file's own order. Missing key or unreadable file -> [] rather than an exception:
    Kathryn had no open_actions key at all, and both callers are prompt builders that
    must not die on it.
    """
    today = today or date.today()
    out = []
    for raw in (_load_state(athlete_dir).get("open_actions") or []):
        if not isinstance(raw, dict):
            continue
        status, note = _status_of(raw)
        due = _parse_iso(raw.get("due"))
        gates = bool(raw.get("gates_race_decision"))
        item = {
            "action": (raw.get("action") or "(unnamed action)").strip(),
            "status": status,
            "status_note": note,
            "due": due.isoformat() if due else None,
            "due_label": (raw.get("due_label") or "").strip() or None,
            "owner": raw.get("owner") or None,
            "gates_race_decision": gates,
            "closed": raw.get("closed") or None,
            "closed_by": raw.get("closed_by") or None,
            "raised": None,
            "days_open": None,
            "open": status in OPEN_STATUSES,
            "days_overdue": 0,
            "days_until": None,
            "bucket": None,
            "escalation": "none",
        }
        if not item["open"]:
            item["bucket"] = "closed"
            out.append(item)
            continue
        raised = _parse_iso(raw.get("raised"))
        if raised:
            item["raised"] = raised.isoformat()
            item["days_open"] = max((today - raised).days, 0)
        if due is None:
            item["bucket"] = "open_ended"
            if status in IN_FLIGHT_STATUSES:
                item["escalation"] = "in_flight"
            elif item["days_open"]:
                item["escalation"] = _escalation(item["days_open"], gates)
        elif due < today:
            item["bucket"] = "overdue"
            item["days_overdue"] = (today - due).days
            item["escalation"] = ("in_flight" if status in IN_FLIGHT_STATUSES
                                  else _escalation(item["days_overdue"], gates))
        else:
            item["days_until"] = (due - today).days
            item["bucket"] = "due_soon" if item["days_until"] <= DUE_SOON_DAYS else "upcoming"
        out.append(item)
    return out


def evaluate(slug: str, today: date | None = None) -> list[dict]:
    return evaluate_from_dir(BASE / "athletes" / slug, today)


def open_items(items: list[dict]) -> list[dict]:
    """Open items only, worst first: overdue (oldest first), due soon, upcoming, open-ended."""
    order = {"overdue": 0, "due_soon": 1, "upcoming": 2, "open_ended": 3}
    return sorted(
        [i for i in items if i["open"]],
        key=lambda i: (order.get(i["bucket"], 9), -i["days_overdue"],
                       -(i["days_open"] or 0),
                       i["days_until"] if i["days_until"] is not None else 999),
    )


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

_ESCALATION_TEXT = {
    "late": "LATE",
    "slipping": "SLIPPING",
    "abandoned": "ABANDONED — treat as not happening unless it is actioned this week",
    "in_flight": "LATE BUT IN FLIGHT — action taken; chase the outcome, not the booking",
}


def render_line(item: dict) -> str:
    """One line of finished text. All arithmetic already resolved — no model recomputes it."""
    bits = [f"{MARKERS.get(item['bucket'], '•')} *{item['action']}*"]
    if item["bucket"] == "overdue":
        d = item["days_overdue"]
        bits.append(f"due {item['due']} — {d} day{'s' if d != 1 else ''} OVERDUE")
        esc = _ESCALATION_TEXT.get(item["escalation"])
        if esc:
            bits.append(esc)
        if item["gates_race_decision"]:
            bits.append("gates a race decision")
    elif item["bucket"] == "due_soon":
        d = item["days_until"]
        bits.append(f"due {item['due']} — {'today' if d == 0 else f'in {d} day' + ('s' if d != 1 else '')}")
        if item["gates_race_decision"]:
            bits.append("gates a race decision")
    elif item["bucket"] == "upcoming":
        bits.append(f"due {item['due']} — in {item['days_until']} days")
    else:
        label = item["due_label"] or "no due date"
        if item["days_open"] is not None:
            bits.append(f"NO DUE DATE, open {item['days_open']} days since {item['raised']} ({label})")
        else:
            bits.append(f"open-ended ({label}) — no due date and no raised date, so it can "
                        f"never come due and cannot age; set one or drop it")
        esc = _ESCALATION_TEXT.get(item["escalation"])
        if esc:
            bits.append(esc)
        if item["gates_race_decision"]:
            bits.append("gates a race decision")
    if item["status"] != "pending":
        bits.append(f"status {item['status']}")
    if item["status_note"]:
        bits.append(item["status_note"])
    return " — ".join(bits)


def render_lines(items: list[dict]) -> list[str]:
    return [render_line(i) for i in open_items(items)]


def counts(items: list[dict]) -> dict:
    c = {"open": 0, "overdue": 0, "due_soon": 0, "upcoming": 0, "open_ended": 0,
         "closed": 0, "abandoned": 0}
    for i in items:
        if not i["open"]:
            c["closed"] += 1
            continue
        c["open"] += 1
        c[i["bucket"]] = c.get(i["bucket"], 0) + 1
        if i["escalation"] == "abandoned":
            c["abandoned"] += 1
    return c


# ---------------------------------------------------------------------------
# prompt blocks — the ONLY thing either surface is allowed to use
# ---------------------------------------------------------------------------

_CLOSE_PATH = (
    "An item is closed by writing the store, never by editing prose: "
    "`python3 ClaudeCoach/lib/open_actions.py --athlete {slug} --set-status done "
    "--action \"<substring>\" --note \"<what happened>\"` "
    "(statuses: " + ", ".join(STATUSES) + "). "
    "You must NOT run that command yourself on the athlete's behalf — report the item and, "
    "if the week's data shows it is finished, say in one clause that its status needs setting."
)


def weekly_block(slug: str, today: date | None = None) -> str:
    """The pre-rendered open-actions section for the weekly card.

    Deliberately finished text plus a reproduce-verbatim instruction, not data plus
    instructions to compute: HEAD 81db419 tried teaching the model date arithmetic in
    prose and it kept re-rendering the same nine placeholder rows.
    """
    items = evaluate(slug, today)
    lines = render_lines(items)
    head = ["## OPEN ACTIONS — PRE-COMPUTED, REPRODUCE VERBATIM",
            "ALREADY EVALUATED in Python before this prompt ran, from the single store "
            "(current-state.json open_actions[]) via lib/open_actions.py. Do NOT recompute a "
            "date, a day count or an overdue judgement, do NOT read the 'Open actions' section "
            "of current-state.md (it is a pointer now, not a store), and do NOT invent an item "
            "that is not listed here."]
    if not lines:
        head.append("NO open actions outstanding. Omit the Open actions section from the card "
                    "entirely — do not write 'none' and do not pad it.")
        return "\n".join(head) + "\n"
    head.append(
        f"Reproduce all {len(lines)} lines below in the card under a bold **Open actions** "
        "heading, one line each, in this order, wording and day counts unchanged. Every line "
        "appears: no sampling, no summarising, no truncating, no merging, however long the list "
        "runs. You may append at most one short nudge clause per line in the athlete's voice; "
        "you may not soften or drop a flag on their behalf. " + _CLOSE_PATH.format(slug=slug))
    return "\n".join(head + [""] + lines) + "\n"


# The old T9 prompt also told the model to read reference/decision-points.md and
# cross-check it against the store. Dropping that outright would lose real coverage:
# Jamie's decision-points calendar names items (bike LTHR test, bike gearing decision,
# long brick rehearsal, wetsuit ruling) that have never had an open_actions entry, so
# nothing would chase them. It is kept as a COVERAGE check only — is the item in the
# store, yes or no — with no date arithmetic, because free text like "Mid July
# (~15 July)" is exactly what the model kept getting wrong.
_COVERAGE_CHECK = """  T9b (coverage, Tier 2): read {ref} if it exists.
  If it names a decision point with NO matching line in the T9 list above, fire once:
  "open-actions store is missing '<item>' from decision-points.md — add it with a real due
  date via lib/open_actions.py". Do NOT estimate how overdue it is, do NOT convert its free-text
  date into a calendar date, and do NOT add it to the list above. The only claim you may make is
  that it is absent from the store.
"""


def watchdog_block(slug: str, today: date | None = None,
                   window_days: int = T9_WINDOW_DAYS) -> str:
    """T9 for the watchdog prompt — fires or does not fire, decided here in Python."""
    items = evaluate(slug, today)
    firing = [i for i in open_items(items)
              if i["bucket"] == "overdue"
              or (i["bucket"] == "due_soon" and i["days_until"] <= window_days)
              or i["bucket"] == "open_ended"]
    head = ("T9 (Tier 2): ALREADY EVALUATED in Python before this prompt ran, off the single "
            "store (current-state.json open_actions[]) via lib/open_actions.py — the same call "
            "the weekly card uses, so the two can never disagree. Do NOT recompute any date or "
            "day count, do NOT read open actions out of current-state.md, and do NOT invent an "
            f"item. Window: due within {window_days} days, already overdue, or open-ended.")
    ref = str((BASE / "athletes" / slug / "reference" / "decision-points.md"))
    coverage = _COVERAGE_CHECK.format(ref=ref)
    if not firing:
        return (head + "\n  T9 DOES NOT FIRE — nothing outstanding in the window.\n"
                + coverage)
    body = "\n".join("  " + render_line(i) for i in firing)
    return (head + f"\n  T9 FIRES on {len(firing)} item(s). Log each of these lines verbatim in "
            "the current-state.md Watchdog Log, subject to the usual daily-nag suppression:\n"
            + body + "\n" + coverage)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cs-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_status(slug: str, match: str, status: str, note: str = "",
               due: str | None = None, today: date | None = None,
               athlete_dir=None, raised: str | None = None,
               closed_by: str = "") -> dict:
    """Set one action's status in the single store. Returns the updated entry.

    `match` is a case-insensitive substring of `action` and must hit exactly one entry —
    an ambiguous match raises rather than guessing, because the wrong item silently
    closing is the failure mode this whole module exists to end.
    """
    if status not in STATUSES:
        raise ValueError(f"status {status!r} not in {STATUSES}")
    adir = Path(athlete_dir) if athlete_dir else BASE / "athletes" / slug
    path = adir / "current-state.json"
    state = json.loads(path.read_text())
    entries = state.get("open_actions")
    if not isinstance(entries, list):
        raise ValueError(f"{path} has no open_actions[] to update")
    needle = match.strip().lower()
    hits = [i for i, e in enumerate(entries)
            if isinstance(e, dict) and needle in (e.get("action") or "").lower()]
    if not hits:
        raise ValueError(f"no action matching {match!r} in {path}")
    if len(hits) > 1:
        names = "; ".join((entries[i].get("action") or "") for i in hits)
        raise ValueError(f"{match!r} matches {len(hits)} actions, be more specific: {names}")
    entry = entries[hits[0]]
    entry["status"] = status
    if note:
        entry["status_note"] = note
    if due is not None:
        entry["due"] = due or None
    if raised:
        entry["raised"] = raised
    if status in CLOSED_STATUSES:
        entry["closed"] = (today or date.today()).isoformat()
        # WHO closed it, alongside WHEN. Without this a closed item and an item that was
        # never captured are indistinguishable after the fact — both simply fail to appear
        # — so there is no way to tell a decision from an omission.
        if closed_by:
            entry["closed_by"] = closed_by
    else:
        entry.pop("closed", None)
        entry.pop("closed_by", None)
    _atomic_write(path, state)
    return entry


# ---------------------------------------------------------------------------
# CAPTURE — closing an item from Telegram
# ---------------------------------------------------------------------------
# WHY. Everything above computes an item's state honestly and renders it, and the coach
# still could not mark one thing done from his phone. That is the reason every status cell
# in the markdown table sat as its template placeholder for three months and the same nine
# lines rendered every week: the only writer was a CLI on a VM. Four of Jamie's items are
# 29-59 days overdue and one of them (the sweat-sodium test) gates a race decision.
#
# SHAPE mirrors lib/races.py and lib/illness.py: a narrow detector plus a parser, both
# refusing rather than guessing. The write still goes through `set_status`, which is the
# single store's only writer and already refuses an ambiguous match — nothing here edits
# current-state.json directly.
#
# WHY IT ASKS BEFORE WRITING. Matching free text to one of fourteen entries is a guess by
# nature ("the bike thing" fits a TT bike fit, an ISM saddle order and an aero helmet), and
# closing the WRONG item is the failure mode the whole module exists to end — it makes a
# live action invisible while leaving the real one open. So the parser returns CANDIDATES
# and the caller confirms; a tap costs a second, a wrongly-closed item costs however long
# it takes somebody to notice.

# An intent to change an item's state. Ordered: the first match wins, so the more specific
# phrasings ("not doing it") must precede the looser ones they contain.
_INTENTS = (
    ("dropped",   r"\b(?:drop(?:ping|ped)?(?:\s+it)?|forget\s+(?:it|about)|not\s+doing|"
                  r"never\s+mind|bin\s+(?:it|that)|cancel(?:led|ling)?|scrap(?:ped)?)\b"),
    ("booked",    r"\b(?:booked|booking\s+(?:is\s+)?(?:in|done)|got\s+it\s+booked)\b"),
    ("ordered",   r"\b(?:ordered|on\s+order|bought|purchased)\b"),
    ("scheduled", r"\b(?:scheduled|in\s+the\s+diary|got\s+a\s+date\s+for)\b"),
    ("defer",     r"\b(?:defer(?:red|ring)?|postpone(?:d)?|reschedul(?:e|ed|ing)|"
                  r"put\s+(?:it\s+)?off)\b|"
                  r"\b(?:push(?:ing|ed)?|move|shift|slide)\b(?=.*\b(?:to|back|out|until|till)\b)"),
    ("done",      r"\b(?:done|did\s+(?:it|that|this)|completed?|finished|sorted(?:\s+it)?|"
                  r"ticked\s+off|all\s+good\s+on|had\s+(?:it|the)\s+\w+\s+done|"
                  r"been\s+(?:done|and\s+done))\b"),
)

# A question is never an instruction — the lesson races.looks_like_race_statement learned.
# "Is the sweat test done?" must not close the sweat test.
_ACTION_QUESTION_RE = re.compile(
    r"^\s*(?:is|are|was|were|has|have|did|do|does|should|shall|can|could|would|when|what|"
    r"which|why|how|any)\b|\bstill\s+(?:open|outstanding|to\s+do)\b", re.I)

# Words that carry no discriminating power when matching a message to an action label.
_MATCH_STOP = {
    "the", "a", "an", "my", "that", "this", "it", "is", "was", "and", "or", "for", "to",
    "of", "on", "in", "at", "i", "ive", "we", "have", "has", "had", "get", "got", "do",
    "did", "done", "all", "now", "just", "been", "be", "am", "with", "from", "off", "up",
    "out", "back", "test", "thing", "one", "finally", "them", "then", "so", "but", "yet",
}
# `test` is in the stop list on purpose: five of Jamie's fourteen entries contain it, so it
# selects nothing while making a one-word match look confident.

_DEFER_DAYS = {"tomorrow": 1, "next week": 7, "a week": 7, "fortnight": 14,
               "two weeks": 14, "next month": 30, "a month": 30}

# Session data is not an action instruction. "It's completed. 7.96k 38.46.2, 148bpm ave,
# av power 405" matches the `done` intent on the word "completed" and then matches any
# open action containing "run" — which is how a manual session report got answered with a
# tick-off picker instead of a debrief. Two or more metric markers means the athlete is
# reporting numbers from a session, so the intent verb belongs to that session.
_METRIC_MARKERS = (
    r"\b\d+(?:\.\d+)?\s*(?:bpm|k|km|kg|ml|w|watts|g/hr|g)\b",
    r"\b(?:gap|pace|rpe|hr|bpm|watts|cadence|decoupling|np|if|tss|load)\b",
    r"\b(?:avg?|ave|average|max)\b",
    r"\b\d{1,2}[:.]\d{2}(?:[:.]\d{1,2})?\b",
)


def _metric_marker_count(text: str) -> int:
    return sum(1 for rx in _METRIC_MARKERS if re.search(rx, text or "", re.I))


def _match_tokens(text: str) -> set:
    """Discriminating lower-case words of 3+ characters."""
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
            if w not in _MATCH_STOP}


def looks_like_action_instruction(text: str) -> bool:
    """True only when the message plainly instructs a state change on an action AND names
    something to match against. Both halves matter: a bare "done" could be about anything
    (a session, a question, the conversation), and without an intent verb an ordinary
    message about the sweat test would close it."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?") or _ACTION_QUESTION_RE.search(t):
        return False
    if _metric_marker_count(t) >= 2:
        return False
    if not any(re.search(rx, t, re.I) for _, rx in _INTENTS):
        return False
    return bool(_match_tokens(_strip_intent(t)))


def _strip_intent(text: str) -> str:
    out = text or ""
    for _, rx in _INTENTS:
        out = re.sub(rx, " ", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip(" ,.;:-–—")


def parse_action_message(text: str, today: date | None = None) -> dict:
    """The intended state change and the words to match an item on.

    Returns {"status", "defer_to", "subject", "note", "refused"}. `status` is one of
    STATUSES, or "" when nothing is instructed; "defer" resolves to a new `defer_to` date
    and leaves the status alone (a deferred item stays open — that is the point of it).
    """
    out = {"status": "", "defer_to": None, "subject": "", "note": "", "refused": ""}
    t = (text or "").strip()
    if not t:
        out["refused"] = "empty"
        return out
    if t.endswith("?") or _ACTION_QUESTION_RE.search(t):
        out["refused"] = "question, not an instruction"
        return out

    intent = next((name for name, rx in _INTENTS if re.search(rx, t, re.I)), "")
    if not intent:
        out["refused"] = "no close/defer/drop instruction"
        return out

    if intent == "defer":
        base = today or date.today()
        low = t.lower()
        days = next((d for phrase, d in _DEFER_DAYS.items() if phrase in low), None)
        if days is None:
            iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
            if iso:
                try:
                    out["defer_to"] = date.fromisoformat(iso.group(1)).isoformat()
                except ValueError:
                    pass
        else:
            out["defer_to"] = (base + timedelta(days=days)).isoformat()
        if not out["defer_to"]:
            # A deferral with no new date is how an item slips forever with nobody
            # noticing — the exact behaviour the escalation bands exist to expose. Refuse
            # it and let the caller ask for a date.
            out["refused"] = "deferral with no new date"
            out["subject"] = _strip_intent(t)
            return out
        out["status"] = "defer"
    else:
        out["status"] = intent

    out["subject"] = _strip_intent(t)
    out["note"] = t.strip()
    return out


def candidates(items: list[dict], subject: str, limit: int = 4) -> list[dict]:
    """Open items the `subject` words plausibly name, best first.

    Scored on how much of the ITEM's label the message accounts for, not the reverse: a
    long message must not out-score a precise one. Returns [] when nothing overlaps, which
    the caller must treat as "say nothing and let the normal reply happen" rather than as
    a reason to pick the closest thing.
    """
    want = _match_tokens(subject)
    if not want:
        return []
    scored = []
    for it in open_items(items):
        have = _match_tokens(it["action"])
        if not have:
            continue
        hits = want & have
        if not hits:
            continue
        ratio = len(hits) / len(have)
        # One common word off a long label ("run" in "Tested run-fuelling protocol in
        # heat") names nothing. Require either a second hit or half the label.
        if len(hits) < 2 and ratio < 0.5:
            continue
        scored.append((ratio, len(hits), it))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    return [it for _, _, it in scored[:limit]]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Open actions — the single store.")
    ap.add_argument("--athlete", help="slug")
    ap.add_argument("--athlete-dir", help="explicit athlete directory (overrides --athlete)")
    ap.add_argument("--all", action="store_true", help="every athlete under athletes/")
    ap.add_argument("--today", help="ISO date override, for testing")
    ap.add_argument("--json", action="store_true", help="dump the computed items as JSON")
    ap.add_argument("--weekly-block", action="store_true", help="print the weekly prompt block")
    ap.add_argument("--watchdog-block", action="store_true", help="print the T9 prompt block")
    ap.add_argument("--set-status", choices=STATUSES)
    ap.add_argument("--action", help="substring of the action to update")
    ap.add_argument("--note", default="", help="status_note to record")
    ap.add_argument("--due", help="also set/clear the due date (ISO, or empty to clear)")
    ap.add_argument("--raised", help="also set the date the item was first raised (ISO)")
    a = ap.parse_args(argv)
    today = _parse_iso(a.today) or date.today()

    if a.set_status:
        if not (a.athlete or a.athlete_dir) or not a.action:
            ap.error("--set-status needs --athlete (or --athlete-dir) and --action")
        entry = set_status(a.athlete or "", a.action, a.set_status, a.note, a.due,
                           today=today, athlete_dir=a.athlete_dir, raised=a.raised)
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return 0

    if a.athlete_dir:
        targets = [(Path(a.athlete_dir).name, Path(a.athlete_dir))]
    elif a.all:
        targets = sorted((p.name, p) for p in (BASE / "athletes").iterdir()
                         if p.is_dir() and not p.name.startswith("_"))
    elif a.athlete:
        targets = [(a.athlete, BASE / "athletes" / a.athlete)]
    else:
        ap.error("need --athlete, --athlete-dir or --all")

    for slug, adir in targets:
        items = evaluate_from_dir(adir, today)
        if a.json:
            print(json.dumps({"athlete": slug, "today": today.isoformat(),
                              "counts": counts(items), "items": items},
                             indent=2, ensure_ascii=False))
            continue
        if a.weekly_block or a.watchdog_block:
            # dir-based override so the blocks can be inspected against any tree
            saved = globals()["BASE"]
            try:
                globals()["BASE"] = adir.parent.parent
                fn = weekly_block if a.weekly_block else watchdog_block
                print(fn(slug, today))
            finally:
                globals()["BASE"] = saved
            continue
        c = counts(items)
        print(f"== {slug} (today {today.isoformat()}) — {c['open']} open: "
              f"{c['overdue']} overdue ({c['abandoned']} abandoned), {c['due_soon']} due soon, "
              f"{c['upcoming']} upcoming, {c['open_ended']} open-ended; {c['closed']} closed")
        for line in render_lines(items) or ["  (nothing open)"]:
            print("   " + line)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
