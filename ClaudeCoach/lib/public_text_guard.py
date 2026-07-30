"""Hard guard on text that leaves the system for a PUBLIC destination (Strava).

Private-athlete content leaked into public Strava descriptions three times before this
existed (25 Jul pain-check question; 30 Jul RPE + injury score in the metrics line;
30 Jul the whole `feel` field). Each time the protection in place was a PROMPT
instruction — "Never state RPE or any injury/pain score — those are private" — asked of
a model. An instruction is a request, not a guarantee, and the third leak happened
because a *different* field carried the same content.

So: allowlist the fields that may be published, and scan the final string for private
patterns before it goes out. Deny by default. Any new athlete-authored or free-text
field is private until someone explicitly adds it to PUBLISHABLE_FIELDS.
"""

import re

# Session-log fields that may appear in a public description. Anything absent is
# private — including every free-text field the athlete writes for the coach.
PUBLISHABLE_FIELDS = frozenset({
    "tss", "norm_power", "avg_power", "duration_min", "distance_km",
    "nutrition_g_carb", "hydration_ml", "sport", "name", "date", "activity_id",
})

# Never publishable, listed explicitly so the intent is greppable.
PRIVATE_FIELDS = frozenset({
    "rpe", "rpe_expected", "rpe_confirmed", "feel", "notes",
    "injury_pain_during", "injury_pain_next_morning",
})

# Patterns that must not survive into public text, whatever field they came from.
_PRIVATE_PATTERNS = [
    (re.compile(r"\brpe\b", re.I), "RPE mentioned"),
    (re.compile(r"\b\d{1,2}\s*/\s*10\b"), "an x/10 score (RPE or pain)"),
    (re.compile(r"\bankle\b", re.I), "ankle (injury detail)"),
    (re.compile(r"\bpain\b", re.I), "pain"),
    (re.compile(r"\binjur", re.I), "injury"),
    (re.compile(r"\bniggle", re.I), "niggle (injury detail)"),
]


def private_leaks(text: str, exempt: str | None = None) -> list[str]:
    """Human-readable descriptions of every private pattern found in `text`.

    `exempt` is text that is already public and must not count as a leak — in practice
    the activity's own name. Jamie has sessions titled "Ankle Training" and "Ankle
    stability + lower body mobility": he chose those titles and they are already visible
    on Strava, so matching the word "ankle" in them would block every description for
    those sessions forever while protecting nothing.
    """
    body = text or ""
    if exempt:
        body = body.replace(str(exempt), " ")
    return [why for pat, why in _PRIVATE_PATTERNS if pat.search(body)]


def public_entry(entry: dict) -> dict:
    """`entry` reduced to publishable fields only. Use this to build public text."""
    return {k: v for k, v in (entry or {}).items() if k in PUBLISHABLE_FIELDS}


def assert_publishable(text: str, exempt: str | None = None) -> str:
    """Return `text` if it is safe to publish, else raise ValueError.

    Fails CLOSED: a description is not worth publishing at the cost of leaking an
    athlete's pain score. Callers should let this raise rather than catching it — a
    missing Strava description is a nuisance, a leaked one is a breach of trust.
    """
    leaks = private_leaks(text, exempt)
    if leaks:
        raise ValueError(
            "refusing to publish: text contains private athlete content — "
            + "; ".join(leaks)
        )
    return text
