#!/usr/bin/env python3
"""Nightly: restart claudecoach-api (Peak's backend) at midnight.

Why (4 Oct 2026, Jamie: "schedule the API to restart at least once a day at
midnight"): cc-gitpull pulls new code twice an hour but never restarts the API, so
a merged fix only went live when someone remembered to bounce the service. One
restart a day also clears whatever a long-running process has built up.

A chat reply runs as a `claude` child process inside the service's cgroup, and a
restart kills it mid-answer. So the job waits until the cgroup holds only the
uvicorn main process (nothing in flight), checking every POLL_SECS, for at most
MAX_WAIT_SECS. After that it restarts anyway: "at least once a day" beats a reply
that never ends. Startup takes about a second.

Success writes a heartbeat (coach_alert DELIVERABLES: "api-restart"). A restart
that leaves the service down is an ops-alerts line, never an athlete message.
"""
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
sys.path.insert(0, str(BASE / "lib"))
import ops_log          # noqa: E402

SCRIPT        = "api-restart"
SERVICE       = "claudecoach-api"
CGROUP_PROCS  = Path(f"/sys/fs/cgroup/system.slice/{SERVICE}.service/cgroup.procs")
HEALTH_URL    = "http://127.0.0.1:8787/"               # 307 to /coach/app.html
POLL_SECS     = 30
MAX_WAIT_SECS = 30 * 60
UP_WAIT_SECS  = 60


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main_pid() -> str:
    r = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", SERVICE],
                       capture_output=True, text=True)
    return r.stdout.strip()


def busy_pids() -> list:
    """PIDs in the service's cgroup other than uvicorn itself: replies in flight."""
    try:
        pids = CGROUP_PROCS.read_text().split()
    except OSError:
        return []
    main = main_pid()
    return [p for p in pids if p != main]


def wait_for_idle(sleep=time.sleep, clock=time.monotonic) -> bool:
    """True once nothing is in flight, False if MAX_WAIT_SECS ran out first."""
    deadline = clock() + MAX_WAIT_SECS
    while True:
        busy = busy_pids()
        if not busy:
            return True
        if clock() >= deadline:
            return False
        log(f"reply in flight (pids {' '.join(busy)}), waiting {POLL_SECS}s")
        sleep(POLL_SECS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def is_up() -> bool:
    if subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode != 0:
        return False
    req = urllib.request.Request(HEALTH_URL, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        code = opener.open(req, timeout=5).status
    except urllib.error.HTTPError as e:
        code = e.code
    except OSError:
        return False
    return code < 500


def wait_until_up(sleep=time.sleep, clock=time.monotonic) -> bool:
    deadline = clock() + UP_WAIT_SECS
    while clock() < deadline:
        if is_up():
            return True
        sleep(2)
    return is_up()


def main() -> int:
    idle = wait_for_idle()
    if not idle:
        log(f"still busy after {MAX_WAIT_SECS // 60} min, restarting anyway")
    r = subprocess.run(["systemctl", "restart", SERVICE], capture_output=True, text=True)
    if r.returncode != 0:
        msg = f"systemctl restart {SERVICE} exited {r.returncode}: {r.stderr.strip()[:200]}"
        log(msg)
        ops_log.alert(SCRIPT, msg)
        return 1
    if not wait_until_up():
        msg = f"{SERVICE} not answering {UP_WAIT_SECS}s after the nightly restart: Peak is down"
        log(msg)
        ops_log.alert(SCRIPT, msg)
        return 1
    log(f"{SERVICE} restarted and answering"
        + ("" if idle else " (a reply in flight was cut off)"))
    ops_log.record_run(SCRIPT, ok=True, detail="restarted ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
