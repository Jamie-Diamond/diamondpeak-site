"""Stepwise git add → commit → rebase → push for the cron scripts.

Replaces the broad try/except blocks that ran five git commands blind: each
step checks its return code, a failed commit skips the push, and failures land
in the ops alert log so the evening digest surfaces them.

Order matters: commit BEFORE syncing with origin. Merging/rebasing with a dirty
index fails, which is why the old add → fetch → merge → commit sequence never
actually merged anything.
"""
import subprocess
from pathlib import Path

from ops_log import alert

PROJECT_DIR = str(Path(__file__).resolve().parent.parent.parent)  # diamondpeak-site/


def _run(args, timeout):
    return subprocess.run(args, cwd=PROJECT_DIR, capture_output=True,
                          text=True, timeout=timeout)


def _stderr(r) -> str:
    return ((r.stderr or "") + (r.stdout or "")).strip()[-300:]


def sync_commit_push(paths, message, script, athlete="", run=None) -> bool:
    """Stage `paths`, commit, rebase onto origin/main, push. Returns True if
    there was nothing to commit or the push succeeded. `run` is injectable for
    tests: callable(args, timeout) -> CompletedProcess-like."""
    run = run or _run
    try:
        # Stage individually — a missing pathspec (e.g. an athlete with no
        # swim-log.json) must not abort staging of the files that do exist.
        for p in paths:
            run(["git", "add", "--", p], 15)

        staged = run(["git", "diff", "--cached", "--quiet"], 15)
        if staged.returncode == 0:
            return True  # nothing to commit

        r = run(["git", "commit", "-m", message], 15)
        if r.returncode != 0:
            alert(script, f"git commit failed — push skipped: {_stderr(r)}", athlete=athlete)
            return False

        r = run(["git", "fetch", "origin"], 30)
        if r.returncode != 0:
            alert(script, f"git fetch failed — commit is local only: {_stderr(r)}", athlete=athlete)
            return False

        r = run(["git", "rebase", "--autostash", "origin/main"], 30)
        if r.returncode != 0:
            run(["git", "rebase", "--abort"], 15)
            alert(script, f"git rebase conflict — aborted, commit is local only: {_stderr(r)}",
                  athlete=athlete)
            return False

        r = run(["git", "push", "origin", "main"], 30)
        if r.returncode != 0:
            alert(script, f"git push failed — commit is local only: {_stderr(r)}", athlete=athlete)
            return False
        return True
    except Exception as e:
        alert(script, f"git sync error: {e}", athlete=athlete)
        return False
