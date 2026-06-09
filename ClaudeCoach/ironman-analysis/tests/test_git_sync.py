"""Tests for lib/git_sync.py — stepwise git transaction safety.

Hermetic: git itself is never invoked; a fake runner scripts each step's
return code and records the call sequence.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import git_sync  # noqa: E402


class FakeGit:
    """callable(args, timeout) — returncode per git subcommand, calls recorded."""

    def __init__(self, rc_by_subcommand=None):
        self.rc = rc_by_subcommand or {}
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(args)
        return SimpleNamespace(returncode=self.rc.get(args[1], 0), stdout="", stderr="boom")

    def subcommands(self):
        return [c[1] for c in self.calls]


@pytest.fixture
def alerts(monkeypatch):
    captured = []
    monkeypatch.setattr(git_sync, "alert",
                        lambda script, msg, athlete="": captured.append(msg))
    return captured


def _sync(fake):
    return git_sync.sync_commit_push(
        ["ClaudeCoach/athletes/x/current-state.md"], "msg", script="test", run=fake)


class TestSyncCommitPush:
    def test_nothing_staged_skips_commit_and_push(self, alerts):
        fake = FakeGit({"diff": 0})   # index clean
        assert _sync(fake) is True
        assert "commit" not in fake.subcommands()
        assert "push" not in fake.subcommands()
        assert alerts == []

    def test_happy_path_runs_full_sequence(self, alerts):
        fake = FakeGit({"diff": 1})   # changes staged
        assert _sync(fake) is True
        assert fake.subcommands() == ["add", "diff", "commit", "fetch", "rebase", "push"]
        assert alerts == []

    def test_failed_commit_skips_push(self, alerts):
        fake = FakeGit({"diff": 1, "commit": 1})
        assert _sync(fake) is False
        assert "push" not in fake.subcommands()
        assert "fetch" not in fake.subcommands()
        assert any("commit failed" in a for a in alerts)

    def test_failed_fetch_skips_push(self, alerts):
        fake = FakeGit({"diff": 1, "fetch": 1})
        assert _sync(fake) is False
        assert "push" not in fake.subcommands()
        assert any("fetch failed" in a for a in alerts)

    def test_rebase_conflict_aborts_and_skips_push(self, alerts):
        fake = FakeGit({"diff": 1, "rebase": 1})
        assert _sync(fake) is False
        # the failed rebase is followed by an explicit abort, never a push
        rebase_calls = [c for c in fake.calls if c[1] == "rebase"]
        assert ["git", "rebase", "--abort"] in [c[:3] for c in rebase_calls] or \
               any("--abort" in c for c in rebase_calls)
        assert "push" not in fake.subcommands()
        assert any("rebase conflict" in a for a in alerts)

    def test_failed_push_is_alerted(self, alerts):
        fake = FakeGit({"diff": 1, "push": 1})
        assert _sync(fake) is False
        assert any("push failed" in a for a in alerts)

    def test_missing_pathspec_does_not_stop_staging(self, alerts):
        # add fails (e.g. no swim-log.json) but the sequence continues
        fake = FakeGit({"add": 128, "diff": 1})
        assert git_sync.sync_commit_push(
            ["a.json", "b.json"], "msg", script="test", run=fake) is True
        assert fake.subcommands().count("add") == 2
        assert "push" in fake.subcommands()

    def test_runner_exception_is_caught_and_alerted(self, alerts):
        def explode(args, timeout):
            raise RuntimeError("git went away")
        assert git_sync.sync_commit_push(
            ["a.json"], "msg", script="test", run=explode) is False
        assert any("git sync error" in a for a in alerts)
