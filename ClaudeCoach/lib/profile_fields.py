"""Safe readers for profile fields that may carry prose alongside a value.

Some profile fields have accumulated coaching caveats inside the value itself. The
worst case, live on 2026-07-30, was Kathryn's `run_threshold_pace_per_km`:

    "5:00/km — UNCONFIRMED working estimate (given by Kathryn 2026-07-22), NOT
     field-tested. Treat as derived/provisional: do NOT prescribe threshold-interval
     work as if this is a validated threshold. Pending a field test."

activity-watcher interpolated that straight into her debrief as
"vs threshold ({threshold_pace}/km)", so she received the entire internal note —
instructions to the coach and all — with a mangled "/km)" stuck on the end.

Read these fields through `pace()` / `numeric()` so only the machine-readable part can
reach the athlete, and through `caveat()` when the coach genuinely needs the note.
"""

import re

# First "M:SS" or "MM:SS" in the field. Deliberately does NOT match bare integers, so
# a stray year or rep count in the prose cannot be mistaken for a pace.
_PACE_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def pace(value, default: str | None = None) -> str | None:
    """The M:SS pace from a field that may also contain prose. None/default if absent."""
    if value is None:
        return default
    m = _PACE_RE.search(str(value))
    return f"{int(m.group(1))}:{m.group(2)}" if m else default


def numeric(value, default: float | None = None) -> float | None:
    """The first number from a field that may also contain prose."""
    if value is None:
        return default
    m = _NUM_RE.search(str(value))
    if not m:
        return default
    try:
        return float(m.group(0))
    except ValueError:
        return default


def caveat(value) -> str:
    """Whatever prose follows the value — for COACH-FACING context only.

    Never interpolate this into a message to the athlete: it is written as
    instructions to the coach, not as something an athlete should read.
    """
    if value is None:
        return ""
    s = str(value)
    m = _PACE_RE.search(s)
    rest = s[m.end():] if m else s
    return rest.lstrip(" /kmh—-–:").strip()


def is_provisional(value) -> bool:
    """True when the field's own prose says the value is not validated."""
    return bool(re.search(r"unconfirmed|not field-tested|provisional|estimate|pending",
                          str(value or ""), re.IGNORECASE))
