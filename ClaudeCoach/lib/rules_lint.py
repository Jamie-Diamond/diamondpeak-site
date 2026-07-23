"""Rules-lint: flag prose coaching rules that zero-out or forbid an intensity slice
the training blueprint requires.

Why this exists
---------------
On ~15 Jul 2026 the engine flipped (Phase 5.6) to actively HIT each sport's phase
intensity distribution. A 13-14 Jul `[perm]` rule telling the coach to "hold run
quality back" survived that flip and kept suppressing the run's required Z4-5 slice,
so the bot served Kathryn a plan that violated her blueprint. A one-off sweep fixed
that rule, but a sweep is not durable: the NEXT methodology change can leave another
prose rule stale the same way.

This lint is the durable guard. For each athlete it reads the blueprint's per-phase,
per-sport intensity distribution, works out which (sport, slice) the blueprint
REQUIRES (share > 0 in any phase), then scans the athlete's prose rule files for
lines that SUPPRESS a required slice (a suppression cue + a sport cue + an
intensity-slice cue, and not already marked superseded/reconciled). Matches are
FLAGGED for human review - high-recall by design, never auto-edited.

It is deterministic and side-effect-free (returns findings); the caller
(scripts/bug-fixer.py) decides how to surface them (ops_log alert + a coach card).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# ---- intensity-slice vocabulary --------------------------------------------
# canonical slices used in the blueprint distribution strings
SLICES = ("easy", "z3", "high")  # easy = Z1-2, z3 = Z3 tempo, high = Z4-5

# words that point at a QUALITY slice (only the quality slices matter here; a rule
# holding back easy volume is not a blueprint-distribution risk).
_QUALITY_WORDS = r"(?:hard|quality|tempo|threshold|interval|intervals|vo2|v[o0]2|speed[- ]?work|sprint|race[- ]pace)"
_Z3_ONLY = re.compile(r"\b(?:tempo|z3)\b")
_HIGH_WORDS = re.compile(r"\b(?:hard|quality|threshold|interval|intervals|vo2|v[o0]2|speed[- ]?work|sprint|race[- ]pace)\b")

# sport cues -> canonical sport key used in the blueprint distribution dict
_SPORT_CUES = {
    "Run": (r"\brun(?:ning|s)?\b", r"\bjog"),
    "Bike": (r"\bbike\b", r"\bcycl", r"\brid(?:e|es|ing)\b"),
    "Swim": (r"\bswim(?:ming|s)?\b",),
}

# HIGH-PRECISION withholding patterns. A rule matches only when a suppression verb
# is bound (within one SENTENCE - `[^.]` never crosses a full stop) to the quality
# STIMULUS itself, i.e. it tells the coach to withhold / not-program / cap quality
# work. This deliberately does NOT fire on the many rules where "no/never/do not"
# attaches to HR-zone wording, pace/CSS storage, fuelling, calendar data or jargon.
_WITHHOLD = tuple(re.compile(p) for p in (
    r"hold[^.]{0,30}" + _QUALITY_WORDS + r"[^.]{0,15}back",
    r"hold back[^.]{0,30}" + _QUALITY_WORDS,
    r"(?:no|never|avoid|omit|skip|drop|don'?t|do not)[^.]{0,30}" + _QUALITY_WORDS
        + r"[^.]{0,12}(?:run|runs|running|ride|rides|riding|bike|swim|swims|session|sessions|work|rep|reps|effort|efforts|block|blocks|stimulus|training|day|days)",
    r"(?:do not|don'?t|never|avoid|no need to)[^.]{0,20}(?:add|adding|schedule|scheduling|program|programming|prescrib|includ|introduc|recommend)[^.]{0,30}" + _QUALITY_WORDS,
    r"keep[^.]{0,20}(?:run|running|ride|riding|bike|cycling|swim|swimming|it|everything|all)[^.]{0,20}(?:easy|z1|z2|aerobic|zone 1|zone 2)",
    r"all[^.]{0,10}(?:runs?|rides?|swims?)[^.]{0,6}easy",
    r"(?:hard efforts?|quality|intensity)[^.]{0,40}(?:are|is)[^.]{0,12}(?:sufficient|enough|not (?:needed|required)|unnecessary)",
    r"(?:cap|limit|reduce|minimi[sz]e|restrict)[^.]{0,25}" + _QUALITY_WORDS,
))

# lines that are reconciled, soft/conditional, or actually PROTECT quality - skip.
_SKIP_CUES = tuple(re.compile(p) for p in (
    r"supersed", r"overrode", r"overrid", r"corrected", r"is required",
    r"hit (?:these|the) (?:targets|distribution)", r"build to it",
    r"\[expires:",                          # time-boxed; session-sync prunes it
    r"without an explicit request", r"\bunless\b", r"by default",  # soft/conditional
    r"do not cut[^.]{0,40}quality", r"protect[^.]{0,20}quality",    # protects quality
    r"do not escalate", r"harder than planned",                    # about not OVER-doing
    r"walk[- ]?(?:run|interval|break)", r"run-walk",               # run-walk format, not quality intervals
    r"achievable at easy", r"execute on time",                     # session-rescue conditional, not a distribution withhold
))


def _norm(text: str) -> str:
    return text.replace("–", "-").replace("—", "-").lower()


def parse_distribution(dist: dict) -> dict:
    """{'Run': '83% Z1-2 / 12% Z3 / 5% Z4-5', ...} -> {'Run': {'easy':83,'z3':12,'high':5}}."""
    out: dict = {}
    for sport, s in (dist or {}).items():
        s = _norm(s)
        slot = {"easy": 0, "z3": 0, "high": 0}
        # capture "NN% Z1", "NN% Z3", "NN% Z4"
        for pct, zone in re.findall(r"(\d+)\s*%\s*z\s*([1-5])", s):
            z = int(zone)
            key = "easy" if z <= 2 else ("z3" if z == 3 else "high")
            slot[key] = max(slot[key], int(pct))
        out[sport] = slot
    return out


def required_slices(blueprint: dict) -> dict:
    """{'Run': {'z3','high'}, ...} - slices the blueprint requires (>0 in any phase)."""
    req: dict = {}
    for phase in (blueprint.get("phases") or []):
        for sport, slots in parse_distribution(phase.get("distribution") or {}).items():
            req.setdefault(sport, set())
            for slice_name, pct in slots.items():
                if pct > 0:
                    req[sport].add(slice_name)
    return req


def _sports_in(text: str) -> set:
    return {sport for sport, cues in _SPORT_CUES.items()
            if any(re.search(c, text) for c in cues)}


def _matched_slices(text: str) -> set:
    slices = set()
    if _HIGH_WORDS.search(text):
        slices.add("high")
    if _Z3_ONLY.search(text):
        slices.add("z3")
    return slices


def lint_rules_text(text: str, req: dict) -> list[dict]:
    """Scan prose rule lines; return findings where a rule WITHHOLDS a quality slice
    the blueprint requires. High precision: only the bound withholding patterns fire."""
    findings: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = _norm(line)
        if any(p.search(low) for p in _SKIP_CUES):
            continue
        matched = [p.pattern for p in _WITHHOLD if p.search(low)]
        if not matched:
            continue
        slices = _matched_slices(low) & {"z3", "high"}
        if not slices:
            slices = {"high"}  # a withhold pattern fired but named no zone -> assume quality/high
        sports = _sports_in(low) or set(req.keys())
        for sport in sports:
            hit = slices & req.get(sport, set())
            if hit:
                findings.append({
                    "sport": sport,
                    "slices": sorted(hit),
                    "rule": line[:280],
                    "reason": (f"rule may withhold {sport} {'/'.join(sorted(hit))} work, which the "
                               f"blueprint requires (>0%) - confirm it is intentional or time-box it"),
                })
    return findings


def _accepted_path(slug: str, base_dir) -> Path:
    # human-reviewed, accepted withholding rules (keyed by a stable content hash).
    # A finding here is a CONFIRMED intentional rule and is suppressed - but if the
    # rule TEXT changes, its hash changes and it fires again. New withholding rules
    # (e.g. one a methodology change leaves stale) are never pre-accepted, so they
    # always surface. This keeps the guard quiet-when-clean without going blind.
    return Path(base_dir) / "athletes" / slug / "reference" / "rules-lint-accepted.json"


def rule_hash(slug: str, file: str, rule: str) -> str:
    import hashlib
    key = f"{slug}|{file}|{re.sub(r'[^a-z0-9]+', '', _norm(rule))}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _accepted(slug: str, base_dir) -> dict:
    p = _accepted_path(slug, base_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def lint_athlete(slug: str, base_dir, include_accepted: bool = False) -> list[dict]:
    """Return blueprint-contradiction findings for one athlete (empty = clean).
    Findings whose rule hash is in the athlete's rules-lint-accepted.json are
    suppressed unless include_accepted=True."""
    ath = Path(base_dir) / "athletes" / slug
    bp_path = ath / "reference" / "training-blueprint.json"
    if not bp_path.exists():
        return []
    try:
        blueprint = json.loads(bp_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [{"sport": "-", "slices": [], "rule": "", "reason": f"blueprint unreadable: {e}"}]
    req = required_slices(blueprint)
    if not req:
        return []
    accepted = {} if include_accepted else _accepted(slug, base_dir)
    findings: list[dict] = []
    for rel in ("persistent-rules.md", "reference/rules.md"):
        p = ath / rel
        if p.exists():
            for f in lint_rules_text(p.read_text(encoding="utf-8"), req):
                h = rule_hash(slug, rel, f["rule"])
                if h in accepted:
                    continue
                f["file"] = rel
                f["slug"] = slug
                f["hash"] = h
                findings.append(f)
    return findings


def lint_all(base_dir, slugs: list[str] | None = None) -> dict:
    base = Path(base_dir)
    if slugs is None:
        try:
            slugs = list(json.loads((base / "config" / "athletes.json").read_text()).keys())
        except Exception:
            slugs = []
    return {s: f for s in slugs if (f := lint_athlete(s, base))}


if __name__ == "__main__":
    import sys
    base = sys.argv[1] if len(sys.argv) > 1 else "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach"
    bad = lint_all(base)
    if not bad:
        print("rules-lint: clean — no rule suppresses a blueprint-required intensity slice")
        sys.exit(0)
    print("rules-lint: FINDINGS (human review — not auto-edited):")
    for slug, fs in bad.items():
        for f in fs:
            print(f"  ⚠ [{slug}/{f.get('file')}] {f['reason']}")
            print(f"      rule: {f['rule']}")
    sys.exit(1)
