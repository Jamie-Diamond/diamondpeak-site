"""blueprint.py — structured training-blueprint sidecar validation.

Pure functions, no IO. The blueprint sidecar (athletes/{slug}/reference/
training-blueprint.json) is the machine-readable counterpart to the prose
training-blueprint.md, emitted by generate-blueprint.py and consumed by the
planner/validator (remediation-plan WS B/C/E). This module validates its shape
so a malformed sidecar fails loudly at generation time, not at planning time.
"""
from __future__ import annotations

from datetime import date

SCHEMA_VERSION = 1

REQUIRED_TOP = [
    "schema_version",
    "slug",
    "generated",
    "event_type",
    "race_date",
    "phases",
    "tests",
]
REQUIRED_PHASE = ["name", "family", "start", "end", "weeks"]
VALID_FAMILIES = {"base", "build", "peak", "taper"}


def validate_blueprint(data: dict) -> list[str]:
    """Return a list of human-readable errors. Empty list == valid.

    Checks presence of required top-level + per-phase keys, that phases is a
    non-empty list, family values are known, and start/end parse as ISO dates
    in order. Intentionally permissive about optional content (distribution,
    fuelling, env_protocols) so partially-specified events (e.g. stubs) still
    validate.
    """
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["blueprint must be a dict"]

    for k in REQUIRED_TOP:
        if k not in data:
            errs.append(f"missing top-level key: {k}")

    if "schema_version" in data and data["schema_version"] != SCHEMA_VERSION:
        errs.append(
            f"schema_version {data['schema_version']} != expected {SCHEMA_VERSION}"
        )

    phases = data.get("phases")
    if not isinstance(phases, list) or not phases:
        errs.append("phases must be a non-empty list")
        return errs

    for i, p in enumerate(phases):
        if not isinstance(p, dict):
            errs.append(f"phase[{i}] must be a dict")
            continue
        for k in REQUIRED_PHASE:
            if k not in p:
                errs.append(f"phase[{i}] missing key: {k}")
        fam = p.get("family")
        if fam is not None and fam not in VALID_FAMILIES:
            errs.append(f"phase[{i}].family invalid: {fam}")
        parsed: dict[str, date] = {}
        for dk in ("start", "end"):
            if dk in p:
                try:
                    parsed[dk] = date.fromisoformat(p[dk])
                except (ValueError, TypeError):
                    errs.append(f"phase[{i}].{dk} not an ISO date: {p.get(dk)!r}")
        if "start" in parsed and "end" in parsed and parsed["end"] < parsed["start"]:
            errs.append(f"phase[{i}] end {p['end']} precedes start {p['start']}")

    return errs


def is_valid(data: dict) -> bool:
    return not validate_blueprint(data)
