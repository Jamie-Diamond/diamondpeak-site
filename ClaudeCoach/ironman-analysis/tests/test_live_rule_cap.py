"""The LIVE chat capture path must brake on rule length, not just rule count.

On 2026-08-03 a byte budget was added to the nightly triage (scripts/bug-fixer.py).
Hours later, Jamie's rules file went from 67 rules / 45,298 bytes to 69 / 50,380 - about
2,500 bytes per new rule - because the growth had not come from the nightly job at all.
It came through this path: the model editing persistent-rules.md during a chat, guarded
only by a COUNT ceiling of 90 that 69 rules sail under.

Length is the unit that matters. A 2,500-byte "rule" is a case note carrying dates,
quotes and recurrence history; the instruction it contains is one sentence, and the
evidence belongs in feedback-log.json. Existing long rules are deliberately untouched -
the cap applies to APPENDS only, so pruning stays the coach's call.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "ironman-analysis"))

import rules_capture as rc  # noqa: E402

BEFORE = "# Persistent coaching rules\n[perm] An existing short rule.\n"
CAP = rc.bug_fixer.RULE_MAX_TOKENS


def test_short_append_is_kept():
    after = BEFORE + "[perm] Answer the question asked; do not pad the reply.\n"
    text, drops = rc.enforce_rule_guards(BEFORE, after, [])
    assert "Answer the question asked" in text
    assert drops == []


def test_overlong_append_is_dropped_with_a_reason():
    after = BEFORE + "[perm] " + ("Evidence-laden case note. " * 40) + "\n"
    text, drops = rc.enforce_rule_guards(BEFORE, after, [])
    assert "Evidence-laden" not in text, "an over-cap rule must not reach the live file"
    assert drops, "the drop must be reported, not silent"
    assert "per-rule cap" in drops[0][0]


def test_cap_boundary():
    """At the cap is fine; one byte over is not."""
    pad = int(CAP * rc.bug_fixer.BYTES_PER_TOKEN) - len("[perm] ") - 1
    ok = BEFORE + "[perm] " + ("x" * pad) + "\n"
    text, drops = rc.enforce_rule_guards(BEFORE, ok, [])
    assert drops == [], f"a rule of ~{CAP} tokens should be allowed"

    over = BEFORE + "[perm] " + ("y" * (pad + 200)) + "\n"
    text, drops = rc.enforce_rule_guards(BEFORE, over, [])
    assert drops and "per-rule cap" in drops[0][0]


def test_existing_long_rules_are_not_retro_dropped():
    """The cap is for appends. An athlete's existing long rules survive untouched -
    pruning them is a coach decision, not something a guard does silently."""
    long_existing = "# Persistent coaching rules\n[perm] " + ("z" * (int(CAP * rc.bug_fixer.BYTES_PER_TOKEN) + 900)) + "\n"
    after = long_existing + "[perm] A short new rule.\n"
    text, drops = rc.enforce_rule_guards(long_existing, after, [])
    assert ("z" * (int(CAP * rc.bug_fixer.BYTES_PER_TOKEN) + 900)) in text, "an existing long rule must not be dropped"
    assert drops == []
