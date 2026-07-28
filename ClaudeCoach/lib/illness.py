#!/usr/bin/env python3
"""Illness / compromised-state flag — structured, so code can act on it.

Why this exists. On 26 Jul 2026 Kathryn, on antibiotics recovering from tonsillitis,
rode 76 minutes and reported her fuelling. The reply opened "You rode 76min at zero
carbs" and closed "works against you", with no acknowledgement that she had trained at
all. Nothing malfunctioned: her system prompt says to confirm session feedback in one
line, her coaching level says "matter-of-fact, never gushing", and her front-load-carbs
standing rule makes fuelling a thing to flag. Her tonsillitis existed only as PROSE in
current-state.md, which no prompt reads. There was no illness gate anywhere in code.

Three layers, mirroring lib/heat.py and lib/menstrual.py:
  - current-state.json `illness` block — the state (this module's schema, below)
  - `training_gate` inside that block — the ONLY thing here that may reduce a plan
  - everything else is a SURFACING gate: it changes what the coach says, not what the
    plan or any model computes

Schema — current-state.json "illness":

    "illness": {
      "status":         "active",             # active | recovering | resolved
      "condition":      "tonsillitis",        # short label, optional
      "started":        "2026-07-24",         # ISO date, required
      "expected_until": "2026-08-02",         # ISO date, optional
      "note":           "on antibiotics",     # free text, optional
      "training_gate":  "none",               # none | no_quality | no_training
      "logged":         "2026-07-28T09:14:03" # set by set_illness(), audit only
    }

Lifecycle. `status` active/recovering both suppress; `resolved` (or an absent block)
suppresses nothing. A flag with no `expected_until` runs until it is explicitly
cleared, but `needs_review` goes True after REVIEW_AFTER_DAYS so the coach asks rather
than suppressing silently for a month. A flag WITH `expected_until` keeps suppressing
past that date (recovery slips; the athlete should not be scolded on the day a guess
expires) and only lapses STALE_GRACE_DAYS later — after which it stops suppressing,
because a forgotten flag must not soften the coaching indefinitely.

What an ACTIVE flag suppresses (see SUPPRESSES) and, deliberately, what it does NOT
(see NOT_SUPPRESSED) is enumerated as data, so both the prompt block and the tests read
one list. The short version: it suppresses criticism, never safety. An injury hard-gate
(lib/injury.py, physio cap 0), a load ceiling, a deload, the acute pain gate and any
"see a doctor" escalation all still apply at full strength. And an illness flag on its
own does NOT zero a quality slice — that needs `training_gate`, which is the structured
illness gate _AUTHORITY_RULE(3) in lib/engine.py has always pointed at and which, until
now, did not exist.
"""
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # ClaudeCoach/

STATUSES        = ("active", "recovering", "resolved")
ACTIVE_STATUSES = ("active", "recovering")
# none = suppress criticism only (the default, and the common case).
# no_quality / no_training = an explicit plan gate the coach set deliberately.
TRAINING_GATES  = ("none", "no_quality", "no_training")

# Ask for a status check after this long on an open-ended flag, while still suppressing.
REVIEW_AFTER_DAYS = 10
# An expected_until in the past keeps suppressing for this long, then lapses.
STALE_GRACE_DAYS  = 7

SUPPRESSES = (
    "fuelling / carb-intake flags and any nutrition criticism",
    "plan-adherence and compliance criticism (missed, shortened or easier sessions)",
    "progression nagging (volume, ramp rate, weekly Load shortfall)",
    "body-composition and weight nudges",
)
NOT_SUPPRESSED = (
    "injury hard-gates (a physio clearance of 0 still blocks that zone)",
    "load ceilings, deloads and taper maths",
    "the acute pain gate (modulation R1)",
    "medical escalation — say plainly when something needs a doctor",
    "recording facts: session-log / heat-log writes happen exactly as normal",
    "safety-critical corrections (heat stacking, hydration in real heat, over-reaching)",
)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def _state_file_in(athlete_dir) -> Path:
    return Path(athlete_dir) / "current-state.json"


def _load_state_in(athlete_dir) -> dict:
    f = _state_file_in(athlete_dir)
    try:
        return json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        return {}


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def normalise(raw: dict, today: date | None = None) -> dict | None:
    """One illness block in canonical shape, or None if it carries no usable state.

    A block with an unparseable / missing `started` is dropped rather than defaulted:
    a suppression window of unknown length is worse than no flag, because nothing
    would ever lapse it.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    today = today or date.today()
    status = str(raw.get("status") or "").strip().lower()
    if status not in STATUSES:
        status = "active" if raw.get("started") or raw.get("condition") else ""
    if not status:
        return None
    started = _as_date(raw.get("started") or raw.get("start"))
    if not started:
        return None
    until = _as_date(raw.get("expected_until"))
    gate = str(raw.get("training_gate") or "none").strip().lower()
    if gate not in TRAINING_GATES:
        gate = "none"

    days_in = (today - started).days
    lapsed = bool(until and today > until + timedelta(days=STALE_GRACE_DAYS))
    active = status in ACTIVE_STATUSES and started <= today and not lapsed
    if until:
        needs_review = today > until
    else:
        needs_review = days_in >= REVIEW_AFTER_DAYS
    out = {
        "status": status,
        "condition": (raw.get("condition") or "").strip() or None,
        "started": started.isoformat(),
        "expected_until": until.isoformat() if until else None,
        "note": (raw.get("note") or "").strip() or None,
        "training_gate": gate,
        "active": active,
        "lapsed": lapsed,
        "needs_review": bool(active and needs_review),
        "days_in": days_in,
    }
    if raw.get("logged"):
        out["logged"] = raw["logged"]
    return out


def state_from_dir(athlete_dir, today: date | None = None) -> dict | None:
    """The athlete's illness block from their current-state.json, or None."""
    return normalise(_load_state_in(athlete_dir).get("illness") or {}, today)


def state(slug: str, today: date | None = None) -> dict | None:
    return state_from_dir(BASE / "athletes" / slug, today)


def is_active(slug: str, today: date | None = None) -> bool:
    st = state(slug, today)
    return bool(st and st["active"])


def _label(st: dict) -> str:
    """'tonsillitis (recovering, day 5)' — the human handle for a flag."""
    bits = [st["condition"] or "illness"]
    detail = [st["status"]]
    if st["days_in"] >= 0:
        detail.append(f"day {st['days_in'] + 1}")
    return f"{bits[0]} ({', '.join(detail)})"


# ---------------------------------------------------------------------------
# prompt surface
# ---------------------------------------------------------------------------

def prompt_block_from_dir(athlete_dir, today: date | None = None,
                          first_name: str = "") -> str:
    """The illness instruction block for prompt injection — "" when no flag is active.

    Deliberately an instruction block, not a data line: every surface that could scold
    an ill athlete assembles free prose from a model, so the gate has to be stated as a
    rule the model follows, alongside an explicit list of what is NOT softened.
    """
    st = state_from_dir(athlete_dir, today)
    if not st or not st["active"]:
        return ""
    who = first_name or "the athlete"
    lines = [
        "## ILLNESS / COMPROMISED STATE — ACTIVE (structured flag, current-state.json)",
        f"{who} is currently {_label(st)}, since {st['started']}"
        + (f", expected until {st['expected_until']}" if st["expected_until"] else "")
        + ".",
    ]
    if st["note"]:
        lines.append(f"Note: {st['note']}")
    lines.append(
        "WHILE THIS FLAG IS ACTIVE — HARD RULES: "
        "(1) ACKNOWLEDGE THE TRAINING FIRST. If they trained at all, the FIRST clause of "
        "your reply credits that they got out and did it while unwell. Never open on a "
        "number they fell short on. "
        "(2) DO NOT RAISE, in any form — as advice, as a reminder, as a standing rule, or "
        "as an aside: " + "; ".join(SUPPRESSES) + ". A standing rule on any of these is "
        "SUSPENDED, not deleted — it returns when the flag clears. "
        "(3) Log the data exactly as normal. Suppression is about what you SAY, never "
        "about what you record. "
        "(4) STILL FULLY IN FORCE — being ill does not soften any of these: "
        + "; ".join(NOT_SUPPRESSED) + ". "
        "(5) If they ASK about fuelling or compliance, answer straight — this gates "
        "unprompted criticism, not a direct question."
    )
    if st["training_gate"] == "no_quality":
        lines.append("TRAINING GATE: no quality work while this flag is active — Z1–2 "
                     "only. This is the structured illness gate, so it overrides the "
                     "blueprint's quality slice for the duration.")
    elif st["training_gate"] == "no_training":
        lines.append("TRAINING GATE: no training while this flag is active. Rest is the "
                     "prescription; do not offer an alternative session.")
    else:
        lines.append("TRAINING GATE: none set — the blueprint still governs the plan. Do "
                     "NOT zero or reduce a required session or zone slice on the basis of "
                     "this flag alone.")
    if st["needs_review"]:
        lines.append(
            "STATUS CHECK DUE: this flag is past its expected window"
            if st["expected_until"] else
            f"STATUS CHECK DUE: this flag has been open {st['days_in']} days")
        lines.append("Ask once, plainly, how they are now — and if they are better, say "
                     "you will clear the flag and run: "
                     f"python3 ClaudeCoach/lib/illness.py clear --athlete <slug>")
    return "\n".join(lines)


def prompt_block(slug: str, today: date | None = None, first_name: str = "") -> str:
    return prompt_block_from_dir(BASE / "athletes" / slug, today, first_name)


def weekly_card_line(slug: str, today: date | None = None) -> str:
    """One line for the weekly summary — the owner's steer is that the weekly card
    carries the illness ONCE, as context for the week's numbers, not as a theme."""
    st = state(slug, today)
    if not st or not st["active"]:
        return ""
    span = f"since {st['started']}"
    if st["expected_until"]:
        span += f", expected until {st['expected_until']}"
    return (f"Illness flag ACTIVE: {_label(st)}, {span}. Mention it ONCE as the reason "
            f"the week's volume/quality/fuelling numbers read as they do, then move on. "
            f"Do not criticise adherence or fuelling for this week.")


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, payload: dict) -> None:
    """Write current-state.json without a truncated-file window. This is the one write
    a chat model can trigger, so a crash mid-write must not cost the whole state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cs-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_illness_in(athlete_dir, *, condition: str = "", status: str = "active",
                   started=None, expected_until=None, note: str = "",
                   training_gate: str = "none", today: date | None = None) -> dict:
    """Write the illness block, leaving every other key in current-state.json alone.

    Validates rather than coerces: a bad status, gate or date raises, so a mistyped
    model-issued command fails loudly instead of writing a flag that never lapses.
    """
    today = today or date.today()
    status = (status or "active").strip().lower()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    training_gate = (training_gate or "none").strip().lower()
    if training_gate not in TRAINING_GATES:
        raise ValueError(f"training_gate must be one of {TRAINING_GATES}, "
                         f"got {training_gate!r}")
    start_d = _as_date(started) or today
    if start_d > today:
        raise ValueError(f"started {start_d} is in the future")
    until_d = _as_date(expected_until) if expected_until else None
    if expected_until and not until_d:
        raise ValueError(f"could not parse expected_until {expected_until!r}")
    if until_d and until_d < start_d:
        raise ValueError(f"expected_until {until_d} is before started {start_d}")

    block = {"status": status, "started": start_d.isoformat(),
             "training_gate": training_gate,
             "logged": datetime.now().isoformat(timespec="seconds")}
    if condition:
        block["condition"] = condition.strip()
    if until_d:
        block["expected_until"] = until_d.isoformat()
    if note:
        block["note"] = note.strip()

    path = _state_file_in(athlete_dir)
    st = _load_state_in(athlete_dir)
    st["illness"] = block
    st["last_updated"] = today.isoformat()
    _atomic_write(path, st)
    return normalise(block, today)


def clear_illness_in(athlete_dir, today: date | None = None) -> bool:
    """Mark the flag resolved (keeping the record) — True if there was one to clear.

    Resolved rather than deleted: an illness that shaped a fortnight of training is
    history worth keeping, and `resolved` suppresses nothing.
    """
    today = today or date.today()
    st = _load_state_in(athlete_dir)
    block = st.get("illness")
    if not isinstance(block, dict) or not block:
        return False
    if str(block.get("status") or "").lower() == "resolved":
        return False
    block["status"] = "resolved"
    block["resolved_on"] = today.isoformat()
    st["illness"] = block
    st["last_updated"] = today.isoformat()
    _atomic_write(_state_file_in(athlete_dir), st)
    return True


def set_illness(slug: str, **kw) -> dict:
    return set_illness_in(BASE / "athletes" / slug, **kw)


def clear_illness(slug: str, today: date | None = None) -> bool:
    return clear_illness_in(BASE / "athletes" / slug, today)


# ---------------------------------------------------------------------------
# conversational capture (mirrors lib/races.py looks_like_/parse_)
# ---------------------------------------------------------------------------

_ILLNESS_TERMS = (
    "ill", "unwell", "sick", "poorly", "flu", "man flu", "cold", "chest infection",
    "sinus", "tonsillitis", "throat infection", "sore throat", "covid", "norovirus",
    "stomach bug", "food poisoning", "d&v", "fever", "high temperature", "antibiotics",
    "infection", "virus", "bronchitis", "laryngitis", "shingles", "glandular fever",
)
# First person only, and NOT bare "been"/"got": "it has been a cold week" is not an
# illness report, and a false positive here writes a flag that silently softens the
# coaching. A false negative costs one clarifying exchange.
_FIRST_PERSON = re.compile(
    r"\b(i(?:'m| am| have| ve|'ve)|i\b|my|myself|me|i've (?:been|got)|"
    r"(?:i|i've|ive) (?:am |have )?down with|coming down with)\b", re.I)
_RECOVERING = re.compile(r"\b(recover\w*|on the mend|getting better|tail end|almost better)\b", re.I)
_RESOLVED   = re.compile(r"\b(all better|fully better|back to normal|over it|"
                         r"cleared up|all clear|fine now)\b", re.I)
_NEGATED    = re.compile(r"\b(not (?:ill|sick|unwell)|wasn'?t (?:ill|sick)|"
                         r"no (?:cold|flu|fever)|if i (?:get|was))\b", re.I)


def _matched_term(text: str) -> str:
    low = text.lower()
    hits = [t for t in _ILLNESS_TERMS if re.search(rf"\b{re.escape(t)}\b", low)]
    # Longest match wins, so "sore throat" beats "throat infection"'s prefix and
    # "tonsillitis" is not reported as the vaguer "ill".
    return max(hits, key=len) if hits else ""


def looks_like_illness_statement(text: str) -> bool:
    """True if this reads like the athlete reporting being ill, right now, about
    themselves. Conservative in the same way races.looks_like_race_statement is: a
    false negative costs one clarifying exchange, a false positive writes a flag that
    silently softens the coaching."""
    if not text or len(text) > 400:
        return False
    if _NEGATED.search(text):
        return False
    if not _matched_term(text):
        return False
    return bool(_FIRST_PERSON.search(text))


_DAYS_AGO   = re.compile(r"\b(?:since|for(?: the last)?|past)\s+(\d+)\s+day", re.I)
_SINCE_WORD = re.compile(r"\bsince\s+(yesterday|the weekend|monday|tuesday|wednesday|"
                         r"thursday|friday|saturday|sunday)\b", re.I)
_WEEKDAYS   = ("monday", "tuesday", "wednesday", "thursday", "friday",
               "saturday", "sunday")
_ANTIBIOTIC_DAYS = re.compile(r"\b(\d+)\s*[- ]?day\s+(?:course|of antibiotics)", re.I)


def parse_illness_message(text: str, today: date | None = None) -> dict:
    """{condition, status, started, expected_until, note} from a chat statement.

    Only fields the message actually states are filled. `started` falls back to today
    — an athlete saying "I'm ill" without a date is ill today, and today is the
    conservative choice because it makes the window shorter, never longer.
    """
    today = today or date.today()
    condition = _matched_term(text)
    status = "recovering" if _RECOVERING.search(text) else "active"
    if _RESOLVED.search(text):
        status = "resolved"

    started = today
    m = _DAYS_AGO.search(text)
    if m:
        started = today - timedelta(days=min(int(m.group(1)), 60))
    else:
        m = _SINCE_WORD.search(text)
        if m:
            word = m.group(1).lower()
            if word == "yesterday":
                started = today - timedelta(days=1)
            elif word == "the weekend":
                started = today - timedelta(days=(today.weekday() + 2) % 7 or 7)
            else:
                back = (today.weekday() - _WEEKDAYS.index(word)) % 7
                started = today - timedelta(days=back or 7)

    expected_until = None
    m = _ANTIBIOTIC_DAYS.search(text)
    if m:
        expected_until = (started + timedelta(days=min(int(m.group(1)), 30))).isoformat()

    return {"condition": condition, "status": status,
            "started": started.isoformat(), "expected_until": expected_until,
            "note": text.strip()[:160]}


# ---------------------------------------------------------------------------
# CLI — the coach's setter. In-repo precedent: lib/plan_tools.py, which the
# engine's _ACCURACY_RULE already has the model shell out to. No bot change needed
# to SET the flag; see docs for the optional ask-and-confirm follow-up.
# ---------------------------------------------------------------------------

def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Illness / compromised-state flag.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set", help="set or update the flag")
    s.add_argument("--athlete", required=True)
    s.add_argument("--condition", default="", help='e.g. "tonsillitis"')
    s.add_argument("--status", default="active", choices=list(STATUSES))
    s.add_argument("--started", default=None, help="ISO date; default today")
    s.add_argument("--expected-until", default=None, help="ISO date; optional")
    s.add_argument("--note", default="")
    s.add_argument("--training-gate", default="none", choices=list(TRAINING_GATES),
                   help="none = suppress criticism only (default)")

    c = sub.add_parser("clear", help="mark the flag resolved")
    c.add_argument("--athlete", required=True)

    sh = sub.add_parser("show", help="print the flag and the prompt block")
    sh.add_argument("--athlete", required=True)

    a = ap.parse_args(argv)
    adir = BASE / "athletes" / a.athlete
    if not adir.exists():
        raise SystemExit(f"no such athlete dir: {adir}")

    if a.cmd == "set":
        st = set_illness_in(adir, condition=a.condition, status=a.status,
                            started=a.started, expected_until=a.expected_until,
                            note=a.note, training_gate=a.training_gate)
        print(json.dumps(st, indent=2))
    elif a.cmd == "clear":
        print("cleared" if clear_illness_in(adir) else "nothing to clear")
    else:
        st = state_from_dir(adir)
        print(json.dumps(st, indent=2) if st else "no illness flag")
        block = prompt_block_from_dir(adir)
        if block:
            print("\n" + block)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
