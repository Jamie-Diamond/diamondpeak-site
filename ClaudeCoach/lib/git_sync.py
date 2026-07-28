"""Stepwise git add → commit → rebase → push for the cron scripts.

Replaces the broad try/except blocks that ran five git commands blind: each
step checks its return code, a failed commit skips the push, and failures land
in the ops alert log so the evening digest surfaces them.

Order matters: commit BEFORE syncing with origin. Merging/rebasing with a dirty
index fails, which is why the old add → fetch → merge → commit sequence never
actually merged anything.

Race-hardened 2026-07-27. Several cron jobs push this ONE working tree
(activity-watcher every 5 min, daily-prescription, refresh-site-data,
refresh-public-data) alongside two shell jobs. Two pushing in the same second
made GitHub reject the loser ("cannot lock ref 'refs/heads/main': is at X but
expected Y", 6 times 24-27 Jul 2026) and two touching the index at once gave
".git/index.lock: File exists" (26 Jul). So:
  * the whole transaction runs under a repo-wide flock (same lock file as
    scripts/lib_git_alert.sh's git_lock, so shell and Python jobs serialise
    against each other) — a lock we cannot get is a SKIP, not a failure, since
    the next tick redoes the work;
  * a rejected push gets ONE bounded fetch + rebase --autostash + push retry
    before it counts as a failure;
  * a push that fails even after the retry is recorded (flag file + ops alert),
    matching lib_git_alert.sh's git_sync_fail.

Log-only from 27 Jul 2026: git failures no longer Telegram the coach. Both this
module and lib_git_alert.sh hand the failure to ops_log.sync_failure, which owns
the single decision about how loud it should be — first failure logged as
transient, later ones in the evening digest, Telegram only for a stuck sync.
"""
import fcntl
import os
import subprocess
import time
from pathlib import Path

import ops_log
from ops_log import record_run, sync_ok

PROJECT_DIR = str(Path(__file__).resolve().parent.parent.parent)  # diamondpeak-site/

# Same lock file and flag file as scripts/lib_git_alert.sh — keep the two in step.
LOCK_PATH = os.environ.get("DP_GIT_LOCK", "/var/lock/dp-git.lock")
LOCK_WAIT = int(os.environ.get("DP_GIT_LOCK_WAIT", "120"))
FLAG_FILE = Path.home() / "Library/Logs/ClaudeCoach/git-sync-FAILED.flag"


def _run(args, timeout):
    return subprocess.run(args, cwd=PROJECT_DIR, capture_output=True,
                          text=True, timeout=timeout)


def _stderr(r) -> str:
    return ((r.stderr or "") + (r.stdout or "")).strip()[-300:]


class _RepoLock:
    """flock on LOCK_PATH, polled so we can give up after LOCK_WAIT seconds.
    `held` is False when another job kept the lock the whole time (caller skips)
    and True when we hold it — or when the lock file itself is unusable, in
    which case we proceed unlocked rather than block the workflow."""

    def __init__(self):
        self.held = False
        self._fh = None

    def __enter__(self):
        try:
            self._fh = open(LOCK_PATH, "w")
        except Exception:
            self.held = True          # fail-soft: no lock available, carry on
            return self
        deadline = time.time() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return self
            except OSError:
                if time.time() >= deadline:
                    return self
                time.sleep(1)

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except Exception:
                pass
            self._fh.close()
        return False


def alert(script: str, message: str, athlete: str = "") -> None:
    """Record a git failure through the shared consecutive-failure gate.

    Deliberately NOT ops_log.alert: that writes an unconditional ok=False entry,
    and a first failure that heals on the next tick is not worth reporting. The
    counter is keyed on `script`, so pass a stable one."""
    who = f"[{athlete}] " if athlete else ""
    ops_log.sync_failure(script, f"{who}{message}")


def _side_channels(script: str, message: str) -> None:
    """The out-of-process half of loud_fail (the standing flag file). Separate
    so tests can no-op it and stay hermetic — the ops record itself still goes
    through `alert` and is asserted on. The Telegram send that used to live here
    is gone; escalation of a genuinely stuck sync is ops_log.sync_failure's job
    now, so it happens once per episode instead of once per tick."""
    try:
        FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FLAG_FILE, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {script}: {message}\n")
    except Exception:
        pass


def loud_fail(script: str, message: str, athlete: str = "") -> None:
    """A failure worth a record: ops alert (evening digest) + a standing flag
    file. Mirrors lib_git_alert.sh's git_sync_fail so a Python job's git failure
    is exactly as visible as a shell job's."""
    alert(script, message, athlete=athlete)
    _side_channels(script, message)


def _push_with_retry(run, script, athlete) -> bool:
    """Push; on a rejected push do ONE bounded fetch + rebase --autostash + push.

    Cannot commit anything the caller did not already stage: it runs no `git add`
    and no `git commit`, and the caller has already committed by the time we get
    here. --autostash only shelves and restores UNCOMMITTED worktree files (the
    routine config/*.enc bot churn) — it cannot promote them into the commit
    being pushed.

    A secret gate runs BEFORE the first push (28 Jul 2026), the same one
    scripts/lib_git_alert.sh git_push_retry calls, so the Python and shell pushers
    cannot drift. Fail CLOSED: a missing gate blocks the push. It never prints a
    secret value."""
    gate = os.path.join(PROJECT_DIR, "ClaudeCoach/scripts/check-no-public-secrets.sh")
    if not os.access(gate, os.X_OK):
        loud_fail(script, f"secret gate missing or not executable at {gate} — push blocked",
                  athlete=athlete)
        return False
    r = run([gate, "tree"], 60)
    if r.returncode != 0:
        loud_fail(script, f"secret gate BLOCKED the push: {_stderr(r)}", athlete=athlete)
        return False

    r = run(["git", "push", "origin", "main"], 30)
    if r.returncode == 0:
        sync_ok(script)
        return True

    r = run(["git", "fetch", "origin"], 30)
    if r.returncode != 0:
        loud_fail(script, f"git push failed and retry fetch failed — commit is local only: "
                          f"{_stderr(r)}", athlete=athlete)
        return False
    r = run(["git", "rebase", "--autostash", "origin/main"], 30)
    if r.returncode != 0:
        run(["git", "rebase", "--abort"], 15)
        loud_fail(script, f"git push failed and retry rebase conflicted — commit is local only: "
                          f"{_stderr(r)}", athlete=athlete)
        return False
    r = run(["git", "push", "origin", "main"], 30)
    if r.returncode != 0:
        loud_fail(script, f"git push failed after one retry — commit is local only: "
                          f"{_stderr(r)}", athlete=athlete)
        return False
    sync_ok(script)
    return True


def sync_commit_push(paths, message, script, athlete="", run=None) -> bool:
    """Stage `paths`, commit, rebase onto origin/main, push. Returns True if
    there was nothing to commit, the push succeeded, or the repo lock was busy
    (the next tick redoes the work). `run` is injectable for tests:
    callable(args, timeout) -> CompletedProcess-like."""
    run = run or _run
    try:
        with _RepoLock() as lock:
            if not lock.held:
                # A wait-out is a SKIP, not a failure: the next tick redoes the work.
                # Deliberately NOT `alert` — that writes an ok=False run-status entry,
                # which the 21:30 ops digest reports as a failure, and lock contention
                # between the */5 and hourly pushers would then be its own noise source.
                record_run(script, athlete=athlete, ok=True,
                           detail=f"git sync skipped — {LOCK_PATH} busy >{LOCK_WAIT}s")
                return True

            # Stage individually — a missing pathspec (e.g. an athlete with no
            # swim-log.json) must not abort staging of the files that do exist.
            for p in paths:
                run(["git", "add", "--", p], 15)

            staged = run(["git", "diff", "--cached", "--quiet"], 15)
            if staged.returncode == 0:
                sync_ok(script)
                return True  # nothing to commit

            r = run(["git", "commit", "-m", message], 15)
            if r.returncode != 0:
                alert(script, f"git commit failed — push skipped: {_stderr(r)}", athlete=athlete)
                return False

            r = run(["git", "fetch", "origin"], 30)
            if r.returncode != 0:
                alert(script, f"git fetch failed — commit is local only: {_stderr(r)}",
                      athlete=athlete)
                return False

            r = run(["git", "rebase", "--autostash", "origin/main"], 30)
            if r.returncode != 0:
                run(["git", "rebase", "--abort"], 15)
                alert(script, f"git rebase conflict — aborted, commit is local only: {_stderr(r)}",
                      athlete=athlete)
                return False

            return _push_with_retry(run, script, athlete)
    except Exception as e:
        alert(script, f"git sync error: {e}", athlete=athlete)
        return False
