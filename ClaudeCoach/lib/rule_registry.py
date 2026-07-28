#!/usr/bin/env python3
"""
Rule registry — stable IDENTITY and TYPE for coaching rules, held in a SIDECAR file
so no rule's own prose is ever touched.

Why a sidecar rather than an inline [id:...] tag
------------------------------------------------
Two live mechanisms hash a rule's LINE, not its meaning:

  * rules_lint.rule_hash() keys athletes/<slug>/reference/rules-lint-accepted.json —
    the reviewed-exception register. Its hash includes the [perm] tag, so injecting an
    inline identifier changes the hash of every rule and every accepted exception
    silently re-fires as a new finding.
  * rules_capture._digit_runs() requires every run of digits in a removed rule to
    survive into the rule that absorbed it. A digit-bearing inline ID becomes a figure
    the fold guard must preserve, so it can abort folds that pass today.

A sidecar keyed by a CONTENT hash has neither hazard: zero bytes change in
persistent-rules.md, the lint register keeps working, and the fold guard is untouched.
The registry lives beside the rules it describes (athletes/<slug>/reference/, which is
gitignored) and stores a token FINGERPRINT rather than a copy of the prose.

Identity through rewording
--------------------------
An ID must survive the coach folding a refinement into a rule (rules_capture's whole
point), which changes the content hash. So an unmatched line REBINDS to the best
unmatched registry entry by token-fingerprint similarity, under the same
numbers-must-survive discipline the fold guard uses. Only when nothing rebinds is a new
ID minted. IDs are never reused and never deleted; a rule that leaves the file is marked
status=missing and keeps its ID.

Types
-----
Five types, validated against the real corpus (see docs/rule-lifecycle.md). A rule may
carry several — that is a finding about the rules, not a defect here — so the registry
stores a list plus a primary. Auto-classification is a PRE-FILL for coach review:
classified_by='auto' until a human sets it, and 'auto' is never treated as authoritative.
A type='constraint' entry with no enforced_by is reported as UNENFORCEABLE: it claims to
bind a plan but names no code path that could stop a breach.

CLI (read-only unless --write):
  python3 lib/rule_registry.py --base-dir <dir> --all --report
  python3 lib/rule_registry.py --base-dir <dir> --slug jamie --write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules_capture as rc

RULE_TYPES = ("constraint", "reference", "method", "format", "philosophy")
UNCLASSIFIED = "unclassified"

# Rule files a registry covers, in scan order.
RULE_FILES = ("persistent-rules.md",)

# Rebinding threshold. Deliberately COVERAGE of the old rule's content words by the new
# line, not similarity between the two: a fold ADDS words, so a symmetric measure (Jaccard)
# falls as the fold gets richer and would refuse exactly the edit the capture guard exists
# to allow. The budget is the fold guard's own — rules_capture.FOLD_PROSE_LOSS_PCT — so
# identity and the guard agree on how much rewording is still the same rule.
REBIND_MIN_COVERAGE = 1.0 - rc.FOLD_PROSE_LOSS_PCT / 100.0
# ...plus an absolute floor, so a short generic rule cannot be swallowed by an unrelated
# longer one that happens to contain its two remaining words.
REBIND_MIN_TOKENS = 3


# Weekday cue shared by the classifier and the enforcement map. Must match the full day
# name as well as the abbreviation: a rule that says WEDNESDAY is exactly the kind of day
# anchor that has to be recognised as an enforceable constraint.
_DAY_CUE = re.compile(
    r"\b(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?s?\b", re.I)


def _perm_lines(text: str):
    """[(lineno, tag, line)] for every standing/expiring rule line in a rule file."""
    out = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if rc._PERM_RE.match(line):
            out.append((i, "perm", line))
        elif rc._EXPIRES_RE.match(line):
            out.append((i, rc._EXPIRES_RE.match(line).group(0).strip(), line))
    return out


def content_hash(line: str) -> str:
    """Hash of the rule's normalised CONTENT (tag stripped) — reuses rules_capture's
    own content key so identity and the capture guard agree on what a rule is."""
    return hashlib.sha256(rc._content_key(line).encode("utf-8")).hexdigest()[:12]


def fingerprint(line: str) -> dict:
    """Identity fingerprint: the digit runs that must not drift plus the content words.
    A token bag, not a copy of the rule — enough to rebind an ID through a rewording."""
    return {"digits": sorted(rc._digit_runs(line)),
            "tokens": sorted(rc._content_tokens(line))}


def _coverage(old_fp: dict, new_fp: dict) -> float:
    """Share of the OLD rule's content words still present in the new line."""
    told, tnew = set(old_fp.get("tokens") or []), set(new_fp.get("tokens") or [])
    if not told:
        return 0.0
    kept = told & tnew
    if len(kept) < min(REBIND_MIN_TOKENS, len(told)):
        return 0.0
    return len(kept) / len(told)


# ---------------------------------------------------------------- classification ----
# Cue sets, ordered by specificity. Each is a (type, compiled pattern) pair; a rule takes
# EVERY type whose cue fires, so multi-type rules surface rather than being forced into one.
_CUES = (
    ("constraint", re.compile(
        r"\b(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?s?\b"
        r"|\banchor\b|\block(?:ed)?\b|non-negotiable|\bcap\b|\bcapped\b|\bceiling\b"
        r"|\bfloor\b|\bat least\b|\bat most\b|\bminimum\b|\bmaximum\b|\bmax\b"
        r"|\bper week\b|\bevery week\b|\bweekday\b|\bweekend\b|do not exceed"
        r"|time-boxed|shorter than|\bmust include\b|sessions per week|\bnever schedule\b"
        r"|do not schedule|\bno formal\b|\bpermitted\b")),
    ("reference", re.compile(
        r"\d+\s*(?:g|mg|ml|bpm|w|km|m|kg|%|h|hr|min)\b|\bg/hr\b|\bml/hr\b|\blthr\b"
        r"|\bftp\b|\bcss\b|\bpool is\b|\bcapacity\b|carb content|\bglossary\b"
        r"|\bper tab\b|\bmeans\b|\bequals\b|\bbaseline is\b|\d+:\d+\s*/\s*km")),
    ("method", re.compile(
        r"\bnever from\b|\bsource of truth\b|\bfetch\b|\brun the\b|\btool\b|\bcompute\b"
        r"|cross-check|\bverify\b|\breconcile\b|\bderive[ds]?\b|\bcheck \b|\bjudge\b"
        r"|\barithmetic\b|\bstreams?\b|\blap splits\b|\bbefore attributing\b|\bnever hand\b"
        r"|\bsanity-check\b|\blook(?:ed)? up\b|\bplan_tools\b|\bicu_fetch\b|\bnever estimate\b"
        r"|\bshow the working\b|\bcalculat\w+\b|\bmeasur\w+\b")),
    ("format", re.compile(
        r"\bdescriptions?\b|\bdebrief\b|\bformat\b|\bwording\b|\btable row\b|\blabel\b"
        r"|\btitle\b|\btone\b|\baddress\b|\breply\b|\bmessage\b|\bpresent(?:ing)?\b"
        r"|\bstate\b|\bshow\b|\bsay\b|\bnever tell\b|\bwrite-?ups?\b|\bsignature\b"
        r"|\bplain language\b|\bjargon\b")),
    ("philosophy", re.compile(
        r"\bprioritis\w+\b|\bpriority\b|\bprefer\w*\b|\bdoes not want\b|\bdislikes?\b"
        r"|\bstance\b|\bphilosophy\b|\bapproach\b|\brather than\b|\bover \b|\bideal\b"
        r"|\bsufficient\b|\bintent\b|\bpurpose\b|\btrade\b|\bbad trade\b|\bwins\b")),
)

# Priority when several cues fire: the most CONSEQUENTIAL job a rule does decides where it
# must live. A rule that both binds the plan and states a figure has to be enforced, so it
# is primarily a constraint; a lookup that also tells you how to source it is a reference.
_PRIMARY_ORDER = ("constraint", "reference", "method", "format", "philosophy")


def classify(line: str) -> tuple[str, list]:
    """(primary, all_types) for a rule line. UNCLASSIFIED when no cue fires — those are
    the rules the coach must type by hand, and the count of them is the honest measure of
    how far a mechanical migration can get."""
    low = rc._TAG_RE.sub("", line).lower().replace("–", "-").replace("—", "-")
    hits = [t for t, pat in _CUES if pat.search(low)]
    if not hits:
        return UNCLASSIFIED, []
    primary = next(t for t in _PRIMARY_ORDER if t in hits)
    return primary, sorted(set(hits))


# ---------------------------------------------------------------- enforcement ----
# The ONLY mechanically-enforced constraint surfaces today: day_rules keys in
# config/athletes.json, checked by ironman-analysis/primitives/validate_plan.py, whose
# violation codes are listed alongside. A constraint cue that maps to nothing here is
# UNENFORCEABLE — prose asking the model to remember it, with no check that can fail.
ENFORCEMENT_MAP = (
    (re.compile(r"\bswim\w*\b"), re.compile(
        r"\b(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?s?\b"),
     "day_rules.swim_days -> validate_plan:swim_forbidden_day"),
    (re.compile(r"\b(?:bike|ride|rides|riding|cycl\w+)\b"), re.compile(
        r"\b(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?s?\b"),
     "day_rules.bike_days -> validate_plan:ride_forbidden_day"),
    (re.compile(r"\brun\w*\b"), re.compile(
        r"\b(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?s?\b"),
     "day_rules.run_days -> validate_plan:run_forbidden_day"),
    (re.compile(r"\bstrength\b|\bmobility\b|\bkettlebell\w*\b|\bcrossfit\b"),
     re.compile(r"\bcap\b|\bmax\w*\b|\bper week\b|\bat least\b|\bevery week\b"),
     "day_rules.strength_max -> validate_plan:strength_over_cap"),
    (re.compile(r"\bctl\b|\bramp\b"), re.compile(r"\bcap\b|\bper week\b|\blimit\b"),
     "athletes.json max_ctl_ramp_per_week -> validate_plan:ctl_ramp"),
    (re.compile(r"\bweekly (?:load|tss)\b|\bctl\s*[x×]\s*7\b|\bmaintenance floor\b"),
     re.compile(r"\bfloor\b|\bceiling\b|\bcap\b|\btarget\b"),
     "blueprint tss_ceiling / required_tss -> validate_plan:weekly_tss_cap|weekly_tss_floor"),
    (re.compile(r"\bdistribution\b|\bz4-?5\b|\bintensity distribution\b"),
     re.compile(r"\bslice\b|\bspec\b|\bdistribution\b|\bper-?sport\b"),
     "training-blueprint.json distribution -> validate_plan:intensity_distribution"),
)


def enforcement_for(line: str) -> list:
    """Code paths that could mechanically enforce this rule ([] = unenforceable).

    READ THIS BEFORE QUOTING THE COUNT. A hit names a check that binds this rule's SUBJECT;
    it does not prove the rule's specific assertion is checked. The day-level checks are
    day_rules.<sport>_days, which can express "runs happen on these days" and CANNOT express
    a session KIND: "the LONG run is Wednesday" maps to run_forbidden_day, yet nothing fails
    when the long run lands on a Tuesday that run_days also permits — which is exactly the
    breach rule_conflicts axis B has to find instead. So the unenforceable count this module
    reports is a LOWER BOUND: 14 of the 19 currently counted as enforced rest on a day-level
    or cap check weaker than the rule they are mapped to."""
    low = rc._TAG_RE.sub("", line).lower()
    return [label for subj, qual, label in ENFORCEMENT_MAP
            if subj.search(low) and qual.search(low)]


# ---------------------------------------------------------------- registry I/O ----
def registry_path(base: Path, slug: str) -> Path:
    return Path(base) / "athletes" / slug / "reference" / "rule-registry.json"


def load_registry(base: Path, slug: str) -> dict:
    p = registry_path(base, slug)
    try:
        reg = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        reg = {}
    except Exception as e:
        # RuntimeError, not SystemExit: register_after_write catches Exception, and a
        # corrupt sidecar must never be able to kill the calling writer process.
        raise RuntimeError(f"rule-registry.json unreadable for {slug}: {e}")
    reg.setdefault("version", 1)
    reg.setdefault("next_id", 1)
    reg.setdefault("rules", {})
    return reg


def _mint(slug: str, reg: dict) -> str:
    prefix = "shared" if slug == "_shared" else slug
    rid = f"{prefix}-{reg['next_id']:03d}"
    reg["next_id"] += 1
    return rid


def sync(base, slug: str, today: str = None, write: bool = False) -> dict:
    """Reconcile the registry against the athlete's rule files. Never edits a rule file.

    Returns a report: {'minted': [...], 'rebound': [...], 'missing': [...],
                       'matched': n, 'registry': reg}."""
    base = Path(base)
    today = today or date.today().isoformat()
    reg = load_registry(base, slug)
    rules = reg["rules"]

    seen_lines = []
    for rel in RULE_FILES:
        p = base / "athletes" / slug / rel
        if not p.exists():
            continue
        for lineno, tag, line in _perm_lines(p.read_text(encoding="utf-8")):
            seen_lines.append((rel, lineno, tag, line))

    by_hash = {e["hash"]: rid for rid, e in rules.items()}
    claimed, minted, rebound, matched = set(), [], [], 0

    # Pass 1 — exact content-hash matches.
    pending = []
    for rel, lineno, tag, line in seen_lines:
        h = content_hash(line)
        rid = by_hash.get(h)
        if rid and rid not in claimed:
            claimed.add(rid)
            matched += 1
            e = rules[rid]
            e.update({"last_seen": today, "line": lineno, "file": rel,
                      "tag": tag, "status": "active"})
        else:
            pending.append((rel, lineno, tag, line, h))

    # Pass 2 — rebind a reworded rule to its own ID (deterministic: best score, then ID).
    for rel, lineno, tag, line, h in pending:
        fp = fingerprint(line)
        best, best_score = None, 0.0
        for rid in sorted(rules):
            if rid in claimed:
                continue
            e = rules[rid]
            efp = e.get("fingerprint") or {}
            # Same numbers-must-survive discipline as the fold guard: a rule whose figures
            # are gone is a different rule, however similar the prose.
            if not set(efp.get("digits") or []) <= set(fp["digits"]):
                continue
            s = _coverage(efp, fp)
            if s > best_score:
                best, best_score = rid, s
        if best and best_score >= REBIND_MIN_COVERAGE:
            claimed.add(best)
            rules[best].update({"hash": h, "fingerprint": fp, "last_seen": today,
                                "line": lineno, "file": rel, "tag": tag,
                                "status": "active"})
            rebound.append({"id": best, "coverage": round(best_score, 2), "line": lineno})
            continue
        primary, types = classify(line)
        rid = _mint(slug, reg)
        rules[rid] = {
            "hash": h, "fingerprint": fp, "file": rel, "line": lineno, "tag": tag,
            "type": primary, "types": types, "classified_by": "auto",
            "enforced_by": enforcement_for(line),
            "first_seen": today, "last_seen": today,
            "review_by": None, "last_relevant": None, "status": "active",
        }
        claimed.add(rid)
        minted.append({"id": rid, "line": lineno, "type": primary, "types": types,
                       "enforced_by": rules[rid]["enforced_by"]})

    missing = []
    for rid, e in rules.items():
        if rid not in claimed and e.get("status") != "missing":
            e["status"] = "missing"
            e["missing_since"] = today
            missing.append(rid)

    if write:
        p = registry_path(base, slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {"slug": slug, "minted": minted, "rebound": rebound, "missing": missing,
            "matched": matched, "count": len(seen_lines), "registry": reg}


def unenforceable(reg: dict) -> list:
    """IDs typed constraint with no enforcing code path."""
    return sorted(rid for rid, e in reg.get("rules", {}).items()
                  if e.get("type") == "constraint" and not e.get("enforced_by")
                  and e.get("status") == "active")


def type_counts(reg: dict) -> dict:
    out = {t: 0 for t in RULE_TYPES}
    out[UNCLASSIFIED] = 0
    multi = 0
    for e in reg.get("rules", {}).values():
        if e.get("status") != "active":
            continue
        out[e.get("type", UNCLASSIFIED)] = out.get(e.get("type", UNCLASSIFIED), 0) + 1
        if len(e.get("types") or []) > 1:
            multi += 1
    out["_multi_type"] = multi
    return out


def register_after_write(base, slug: str) -> dict:
    """Hook for the rule WRITERS (session-sync.py, telegram/bot.py): assign IDs and a
    default type to whatever the capture guard has just allowed onto disk. Never raises —
    identity bookkeeping must not be able to block or undo a rule write."""
    try:
        return sync(base, slug, write=True)
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description="Assign stable IDs and types to coaching rules.")
    ap.add_argument("--base-dir", required=True,
                    help="ClaudeCoach directory holding athletes/ and config/")
    ap.add_argument("--slug", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write", action="store_true", help="persist the registry (default: dry run)")
    ap.add_argument("--report", action="store_true", help="print per-type counts")
    a = ap.parse_args()

    base = Path(a.base_dir)
    slugs = list(a.slug)
    if a.all:
        slugs = [p.name for p in sorted((base / "athletes").iterdir()) if p.is_dir()]
    if not slugs:
        ap.error("give --slug or --all")

    total = {t: 0 for t in list(RULE_TYPES) + [UNCLASSIFIED, "_multi_type"]}
    grand = unenf_total = 0
    for slug in slugs:
        rep = sync(base, slug, write=a.write)
        if not rep["count"]:
            continue
        reg = rep["registry"]
        tc = type_counts(reg)
        unenf = unenforceable(reg)
        grand += rep["count"]
        unenf_total += len(unenf)
        for k, v in tc.items():
            total[k] = total.get(k, 0) + v
        print(f"[{slug}] {rep['count']} rules — matched {rep['matched']}, "
              f"minted {len(rep['minted'])}, rebound {len(rep['rebound'])}, "
              f"missing {len(rep['missing'])}")
        if a.report:
            print("        " + "  ".join(f"{k}={tc[k]}" for k in list(RULE_TYPES) + [UNCLASSIFIED]))
            print(f"        multi-type={tc['_multi_type']}  "
                  f"constraints with NO enforcing code path={len(unenf)}")
    if a.report:
        print(f"\nTOTAL {grand} rules  " +
              "  ".join(f"{k}={total[k]}" for k in list(RULE_TYPES) + [UNCLASSIFIED]))
        print(f"multi-type={total['_multi_type']}  unenforceable constraints={unenf_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
