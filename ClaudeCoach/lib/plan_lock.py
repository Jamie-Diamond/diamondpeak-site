"""Per-athlete lock around a plan build, so two builds cannot race one calendar.

WHY. The France week, 22 Jun 2026: "pressed replan twice and it destroyed the agreed
week". There is no overwrite mechanism that does that, but concurrency does.
`_PENDING_REPLAN.pop()` stops a double tap on ONE card; it does not stop typing `replan`
twice, and each confirm launches a detached `stage1-plan.py --push`. Two builds then run
against one calendar and `plan_builder.push` interleaves like this:

  1. A captures old_ids = [X1..Xn]; B captures the SAME old_ids (neither has deleted yet).
  2. A pushes its 7 events. B pushes its 7 DIFFERENT events (separate LLM attempts).
  3. A deletes X1..Xn. B tries to delete them too — already gone, and every failure was
     swallowed silently (now logged, plan_builder.push).

Result: 14 events for one week drawn from two incoherent plans, and the agreed content
gone because both builds generated fresh. flock is the fix, and it must be held across
the WHOLE build — generation and push. Wrapping only the push still lets both builds
generate and both push, which is exactly the failure above.

flock, not a pidfile: a `timeout 2700` SIGTERM closes the fd and releases the lock for
free, whereas a stale pidfile would strand the athlete's next build forever.

THREE OUTCOMES, not two. lib/git_sync._RepoLock collapses "could not open the lock file"
and "someone else holds it" into one `held` flag; here they must diverge, because an
unusable lock file must NOT stop the Sunday cron (fail-soft, build unprotected) while a
lock genuinely held by another build MUST stop this one (exit BUSY_EXIT).
"""
import fcntl
import os
from pathlib import Path

# /var/lock on the Linux VM. Overridable so tests can use a tmpdir — with the path
# hardcoded, every run on a machine without /var/lock silently falls through to
# "proceed unlocked" and a test would prove nothing about the real path.
LOCK_DIR = os.environ.get("CC_PLAN_LOCK_DIR", "/var/lock")

# stage1-plan.py's exit code for "a build is already running". Distinct from 1 (built
# nothing) and 3 (built a week I would not push): this run destroyed nothing, changed
# nothing, and the build that holds the lock will deliver the week.
BUSY_EXIT = 4

HELD = "held"          # we hold the flock — safe to build
BUSY = "busy"          # another build for this athlete holds it — caller must stand down
UNLOCKED = "unlocked"  # lock file unusable; proceeding WITHOUT protection (fail-soft)


def lock_path(slug: str) -> Path:
    return Path(LOCK_DIR) / f"cc-plan-{slug}.lock"


class PlanLock:
    """Non-blocking per-athlete build lock. Use as a context manager and read .state:

        with PlanLock(slug) as lk:
            if lk.state == plan_lock.BUSY:
                sys.exit(plan_lock.BUSY_EXIT)
            ...build and push...

    Non-blocking on purpose: a queued second build would produce a second full week
    45 minutes later and push it over the first. Standing down is the correct answer.
    """

    def __init__(self, slug: str):
        self.slug = slug
        self.state = UNLOCKED
        self._fh = None

    def __enter__(self):
        try:
            self._fh = open(lock_path(self.slug), "w")
        except Exception:
            self.state = UNLOCKED      # fail-soft: never block the Sunday build
            return self
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state = HELD
        except OSError:
            self.state = BUSY
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            if self.state == HELD:
                try:
                    fcntl.flock(self._fh, fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        return False


def build_running(slug: str) -> bool:
    """Is a plan build holding this athlete's lock right now?

    ADVISORY ONLY, and deliberately so: the answer is true at this instant, and the
    caller (bot.py, arming a replan confirm card) launches the build up to a minute later
    after a tap. The real guard is the flock inside stage1-plan's own run; this only lets
    the bot say something honest instead of arming a second card.

    Acquires and releases immediately — it must NEVER keep the fd, or the child it goes on
    to spawn would find the lock held by its own parent and exit BUSY_EXIT every time.

    False on any error: a probe that cannot read the lock must not block a replan.
    """
    try:
        with PlanLock(slug) as lk:
            return lk.state == BUSY
    except Exception:
        return False
