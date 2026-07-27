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
    # Keep the suite hermetic: loud_fail's flag file and Telegram are real side
    # effects. The ops alert itself still runs and is what the tests assert on.
    monkeypatch.setattr(git_sync, "_side_channels", lambda script, msg: None)
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


class TestPushRaceRetry:
    """One bounded retry absorbs a push lost to a concurrent pusher — the
    "cannot lock ref 'refs/heads/main'" rejection seen 6 times 24-27 Jul 2026."""

    def test_rejected_push_retries_once_and_succeeds(self, alerts):
        class RaceGit(FakeGit):
            """First push is rejected (someone else won the race), second wins."""
            def __call__(self, args, timeout):
                self.calls.append(args)
                rc = 0
                if args[1] == "diff":
                    rc = 1
                elif args[1] == "push":
                    rc = 1 if len([c for c in self.calls if c[1] == "push"]) == 1 else 0
                return SimpleNamespace(returncode=rc, stdout="", stderr="cannot lock ref")

        fake = RaceGit()
        assert _sync(fake) is True
        assert fake.subcommands() == ["add", "diff", "commit", "fetch", "rebase",
                                      "push", "fetch", "rebase", "push"]
        assert alerts == []          # a race absorbed by the retry must be SILENT

    def test_retry_never_stages_or_commits(self, alerts):
        """The retry must not be able to sweep unrelated dirty files (config/*.enc
        bot churn) into the pushed commit: it runs no add and no commit."""
        fake = FakeGit({"diff": 1, "push": 1})
        _sync(fake)
        after_first_push = fake.calls[fake.subcommands().index("push") + 1:]
        assert [c[1] for c in after_first_push] == ["fetch", "rebase", "push"]

    def test_push_failing_twice_is_loud(self, alerts):
        fake = FakeGit({"diff": 1, "push": 1})
        assert _sync(fake) is False
        assert any("after one retry" in a for a in alerts)


class TestRepoLock:
    """The flock that fixes the .git/index.lock class (two local git processes in
    the same tree, 26 Jul 2026). A lock we cannot get is a SKIP, not a failure."""

    def test_busy_lock_skips_without_touching_git(self, alerts, monkeypatch):
        runs = []
        monkeypatch.setattr(git_sync, "record_run",
                            lambda script, athlete="", ok=True, detail="": runs.append((ok, detail)))

        class Busy:
            held = False
            def __enter__(self): return self
            def __exit__(self, *exc): return False
        monkeypatch.setattr(git_sync, "_RepoLock", Busy)
        fake = FakeGit({"diff": 1})
        assert _sync(fake) is True          # skip is not a failure
        assert fake.calls == []             # no git ran at all
        # A wait-out must NOT be alerted: ops_log.alert writes an ok=False
        # run-status entry and the 21:30 digest would report routine lock
        # contention as a failure — the same noise class this work removes.
        assert alerts == []
        assert runs and runs[0][0] is True and "skipped" in runs[0][1]

    def test_lock_is_released_even_when_a_step_fails(self, alerts, tmp_path,
                                                     monkeypatch):
        """A failed sync must not leave the repo lock held, or every later job
        would wait it out and skip."""
        monkeypatch.setattr(git_sync, "LOCK_PATH", str(tmp_path / "dp-git.lock"))
        fake = FakeGit({"diff": 1, "commit": 1})
        assert _sync(fake) is False
        with git_sync._RepoLock() as lock:
            assert lock.held is True
