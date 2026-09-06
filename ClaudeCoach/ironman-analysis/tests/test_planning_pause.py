"""Tracking-only mode (lib/planning_pause.py).

An athlete asked to keep ClaudeCoach as a tracker and drop the coaching: keep the
ride descriptions and the chat, lose the prescribed week and — the actual
complaint — lose being told he had missed targets during a week he had chosen to
take off. `active: false` could not express that; it silences the activity
watcher too.

Pins BOTH halves: paused means no prescribing and no judging, and it also means
the tracking surfaces stay on.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import planning_pause as pp  # noqa: E402


@pytest.fixture
def paused_file(tmp_path):
    p = tmp_path / "planning-paused.json"
    p.write_text(json.dumps({
        "_README": "not an athlete",
        "calum": {"paused": True, "since": "2026-09-06", "reason": "tracker only"},
        "kathryn": {"paused": False, "reason": "resumed"},
    }))
    return p


class TestFlagResolution:
    def test_paused_athlete(self, paused_file):
        assert pp.is_paused("calum", path=paused_file) is True
        assert pp.reason("calum", path=paused_file) == "tracker only"
        assert pp.since("calum", path=paused_file) == "2026-09-06"

    def test_unpausing_is_deleting_or_setting_false(self, paused_file):
        assert pp.is_paused("kathryn", path=paused_file) is False
        assert pp.is_paused("jamie", path=paused_file) is False

    def test_readme_key_is_not_an_athlete(self, paused_file):
        assert pp.paused_slugs(path=paused_file) == ["calum"]
        assert pp.is_paused("_README", path=paused_file) is False

    def test_shorthand_true(self, tmp_path):
        f = tmp_path / "p.json"
        f.write_text(json.dumps({"calum": True}))
        assert pp.is_paused("calum", path=f) is True
        assert pp.reason("calum", path=f)

    def test_athletes_json_flag_also_pauses(self, tmp_path):
        f = tmp_path / "empty.json"
        f.write_text("{}")
        assert pp.is_paused("jamie", {"planning_paused": True}, path=f) is True
        assert pp.is_paused("jamie", {"active": True}, path=f) is False

    def test_a_broken_pause_file_fails_open(self, tmp_path):
        """It must never be able to take the activity watcher or the bot down."""
        f = tmp_path / "bad.json"
        f.write_text("{not json")
        assert pp.is_paused("calum", path=f) is False
        assert pp.paused_slugs(path=f) == []

    def test_missing_file_fails_open(self, tmp_path):
        assert pp.is_paused("calum", path=tmp_path / "nope.json") is False


class TestPromptBlock:
    def test_only_for_paused_athletes(self, paused_file):
        assert pp.prompt_block("jamie", "Jamie", path=paused_file) == ""

    def test_forbids_prescribing_and_chasing(self, paused_file):
        block = pp.prompt_block("calum", "Calum", path=paused_file)
        assert "Calum" in block
        low = block.lower()
        for must in ("do not", "missed", "catch up", "tracker"):
            assert must in low
        # It has to actually override the coaching instructions above it in the prompt.
        assert "OVERRIDES" in block


class TestLiveConfig:
    """The shipped config/planning-paused.json — the file that actually deploys."""

    def test_is_valid_json_and_readable(self):
        data = json.loads((REPO / "config" / "planning-paused.json").read_text())
        assert isinstance(data, dict)
        for slug, entry in data.items():
            if slug.startswith("_"):
                continue
            assert isinstance(entry, (bool, dict)), slug
            if isinstance(entry, dict):
                assert entry.get("reason"), f"{slug} pause has no stated reason"


class TestScheduledScriptsRespectIt:
    """Every surface that PRESCRIBES or JUDGES has to consult the pause, and the
    tracking surfaces have to keep working. Asserted against the source because
    these scripts need a VM, an ICU key and Claude to run."""

    PLANNING = [
        "scripts/morning-checkin.py", "scripts/evening-checkin.py",
        "scripts/night-before-brief.py", "scripts/daily-prescription.py",
        "scripts/session-sync.py", "scripts/watchdog.py",
        "scripts/stage1-plan.py", "scripts/generate-blueprint.py",
        "scripts/ops-digest.py", "scripts/weekly-plan.sh",
        "scripts/weekly-summary.sh",
        "lib/plan_audit.py", "lib/macro_projection.py", "lib/rule_conflicts.py",
    ]

    def test_planning_surfaces_check_the_pause(self):
        missing = [f for f in self.PLANNING
                   if "planning_pause" not in (REPO / f).read_text()]
        assert not missing, f"these can still prescribe to a paused athlete: {missing}"

    def test_the_weekly_plan_loop_is_not_hardcoded(self):
        src = (REPO / "scripts" / "weekly-plan.sh").read_text()
        assert "for A in jamie kathryn calum" not in src

    def test_activity_tracking_is_not_gated_on_it(self):
        """The watcher still runs for a paused athlete — that IS the tracker."""
        src = (REPO / "scripts" / "activity-watcher.py").read_text()
        assert "planning_pause.is_paused" not in src
        # ...but its debrief prompt is told not to chase a plan.
        assert "planning_pause.prompt_block" in src

    def test_the_chat_prompt_carries_the_block(self):
        assert "planning_pause" in (REPO / "lib" / "engine.py").read_text()

    def test_shell_surfaces_still_parse(self):
        for f in ("scripts/weekly-plan.sh", "scripts/weekly-summary.sh"):
            subprocess.run(["bash", "-n", str(REPO / f)], check=True)
