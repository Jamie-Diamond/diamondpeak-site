"""Tests for lib/rule_registry.py — stable rule IDENTITY and typing.

The failures these pin come from the real corpus (148 standing rules, 28 Jul 2026):
lib/progression.py's docstring cites \"rule 96\" for an athlete with 56 rules, and
current-state.md cites rules 59/57/60/10/12/33 — all of them LINE POSITIONS in files that
gain and lose lines daily. An ID must therefore be assigned once, survive a rewording, and
never be reused; and it must be assigned WITHOUT touching a rule's prose, because two live
mechanisms (rules_lint.rule_hash's accepted-exception register and rules_capture._digit_runs'
fold guard) key off the rule LINE.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import rule_registry as rr                                   # noqa: E402
import rules_lint                                            # noqa: E402

R_DAY = "[perm] Long run = WEDNESDAY anchor (locked; non-negotiable); progress 10-15%/week."
R_FACT = "[perm] Home pool is 50m - scale set structures in 100m units."
R_METHOD = "[perm] Judge session intensity from the zone distribution, never from IF alone."


def _athlete(tmp_path: Path, slug: str, *rules: str) -> Path:
    d = tmp_path / "athletes" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "persistent-rules.md").write_text(
        "# header comment, not a rule\n" + "".join(r + "\n" for r in rules))
    return tmp_path


class TestIdentity:
    def test_ids_minted_once_and_stable_across_runs(self, tmp_path):
        base = _athlete(tmp_path, "a", R_DAY, R_FACT)
        first = rr.sync(base, "a", write=True)
        assert len(first["minted"]) == 2
        ids = sorted(first["registry"]["rules"])
        second = rr.sync(base, "a", write=True)
        assert second["minted"] == [] and second["matched"] == 2
        assert sorted(second["registry"]["rules"]) == ids

    def test_reordering_the_file_does_not_change_ids(self, tmp_path):
        base = _athlete(tmp_path, "a", R_DAY, R_FACT)
        before = rr.sync(base, "a", write=True)["registry"]
        day_id = next(i for i, e in before["rules"].items() if e["line"] == 2)
        _athlete(tmp_path, "a", R_FACT, R_DAY)          # swap the two rules over
        after = rr.sync(base, "a", write=True)
        assert after["minted"] == [] and after["matched"] == 2
        assert after["registry"]["rules"][day_id]["line"] == 3

    def test_id_survives_a_fold_that_rewords_the_rule(self, tmp_path):
        """A refinement folded into an existing rule keeps that rule's ID — otherwise every
        fold (the whole point of rules_capture) would orphan the review history."""
        base = _athlete(tmp_path, "a", R_DAY)
        rid = next(iter(rr.sync(base, "a", write=True)["registry"]["rules"]))
        folded = ("[perm] Long run = WEDNESDAY anchor (locked; non-negotiable); progress "
                  "10-15%/week, validated against last week's actual synced baseline.")
        _athlete(tmp_path, "a", folded)
        rep = rr.sync(base, "a", write=True)
        assert rep["minted"] == [], rep
        assert [r["id"] for r in rep["rebound"]] == [rid]

    def test_a_dropped_figure_is_a_different_rule_not_a_rebind(self, tmp_path):
        """Same discipline as the fold guard: if the numbers changed it is not the same
        rule, so it must not silently inherit the old rule's ID and review state."""
        base = _athlete(tmp_path, "a", R_FACT)
        rr.sync(base, "a", write=True)
        _athlete(tmp_path, "a", "[perm] Home pool is 25m - scale set structures in 100m units.")
        rep = rr.sync(base, "a", write=True)
        assert len(rep["minted"]) == 1 and rep["rebound"] == []

    def test_a_removed_rule_is_marked_missing_and_keeps_its_id(self, tmp_path):
        base = _athlete(tmp_path, "a", R_DAY, R_FACT)
        reg = rr.sync(base, "a", write=True)["registry"]
        ids = set(reg["rules"])
        _athlete(tmp_path, "a", R_DAY)
        rep = rr.sync(base, "a", write=True)
        assert set(rep["registry"]["rules"]) == ids            # no ID ever disappears
        assert len(rep["missing"]) == 1
        assert rep["registry"]["rules"][rep["missing"][0]]["status"] == "missing"

    def test_ids_are_never_reused(self, tmp_path):
        base = _athlete(tmp_path, "a", R_DAY)
        rr.sync(base, "a", write=True)
        _athlete(tmp_path, "a")                                # empty the file
        rr.sync(base, "a", write=True)
        _athlete(tmp_path, "a", R_METHOD)
        rep = rr.sync(base, "a", write=True)
        assert rep["minted"][0]["id"] == "a-002"


class TestNonDestructive:
    def test_sync_never_touches_the_rule_file(self, tmp_path):
        base = _athlete(tmp_path, "a", R_DAY, R_FACT, R_METHOD)
        f = base / "athletes" / "a" / "persistent-rules.md"
        before = f.read_bytes()
        rr.sync(base, "a", write=True)
        assert f.read_bytes() == before

    def test_registry_does_not_disturb_the_lint_accepted_hash(self, tmp_path):
        """The reason IDs live in a sidecar: rules_lint.rule_hash keys the reviewed-exception
        register off the rule LINE, so anything written into the line re-fires every accepted
        exception. Registering a rule must leave that hash untouched."""
        base = _athlete(tmp_path, "a", R_DAY)
        h = rules_lint.rule_hash("a", "persistent-rules.md", R_DAY)
        rr.sync(base, "a", write=True)
        text = (base / "athletes" / "a" / "persistent-rules.md").read_text()
        line = next(l.strip() for l in text.splitlines() if l.startswith("[perm]"))
        assert rules_lint.rule_hash("a", "persistent-rules.md", line) == h

    def test_register_after_write_swallows_a_corrupt_sidecar(self, tmp_path):
        """Identity bookkeeping runs inside the rule WRITERS (session-sync, the live bot).
        A corrupt registry must degrade to doing nothing, never take the writer down."""
        base = _athlete(tmp_path, "a", R_DAY)
        rp = rr.registry_path(base, "a")
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("{not json")
        assert rr.register_after_write(base, "a") == {}
        assert rr.sync(base, "nobody")["count"] == 0        # unknown athlete: no-op


class TestTyping:
    def test_default_type_and_provenance(self, tmp_path):
        base = _athlete(tmp_path, "a", R_DAY)
        e = next(iter(rr.sync(base, "a", write=True)["registry"]["rules"].values()))
        assert e["classified_by"] == "auto"        # never authoritative until reviewed
        assert e["type"] in list(rr.RULE_TYPES) + [rr.UNCLASSIFIED]
        assert e["review_by"] is None and e["last_relevant"] is None

    def test_day_anchor_is_a_constraint_with_an_enforcing_code_path(self, tmp_path):
        primary, types = rr.classify(R_DAY)
        assert primary == "constraint" and "constraint" in types
        assert rr.enforcement_for(R_DAY) == [
            "day_rules.run_days -> validate_plan:run_forbidden_day"]

    def test_a_constraint_with_no_code_path_is_reported_unenforceable(self, tmp_path):
        rule = "[perm] Do not schedule run sessions shorter than 40 min in normal weeks."
        assert rr.classify(rule)[0] == "constraint"
        assert rr.enforcement_for(rule) == []
        base = _athlete(tmp_path, "a", rule)
        reg = rr.sync(base, "a", write=True)["registry"]
        assert len(rr.unenforceable(reg)) == 1

    def test_multi_type_rules_are_surfaced_not_forced_into_one(self, tmp_path):
        rule = ("[perm] Cap swim sessions at 2km maximum; pool is 33m so structure every "
                "set in multiples of 33m.")
        primary, types = rr.classify(rule)
        assert primary == "constraint"
        assert set(types) >= {"constraint", "reference"}

    def test_unclassified_rules_are_counted_not_guessed(self, tmp_path):
        primary, types = rr.classify("[perm] Clarifying questions are allowed.")
        assert primary == rr.UNCLASSIFIED and types == []


class TestRegistryFile:
    def test_written_where_the_rules_live_and_is_valid_json(self, tmp_path):
        base = _athlete(tmp_path, "a", R_DAY)
        rr.sync(base, "a", write=True)
        p = rr.registry_path(base, "a")
        assert p == base / "athletes" / "a" / "reference" / "rule-registry.json"
        assert json.loads(p.read_text())["rules"]

    def test_dry_run_writes_nothing(self, tmp_path):
        base = _athlete(tmp_path, "a", R_DAY)
        rr.sync(base, "a", write=False)
        assert not rr.registry_path(base, "a").exists()
