"""Cancelling reaches the CLI's CHILDREN, not just the CLI (bug #30, second half,
17 Aug 2026).

The Stop button shipped this morning. It killed the claude CLI, and only the claude
CLI: proc.terminate() and proc.kill() signal one pid. The coach's work does not all
happen in that pid. The model holds a Bash tool and its own authority rule tells it
to shell out to plan_distribution.py for calendar writes, so the thing an athlete
most wants stopped mid-replan is running in a grandchild. Measured on the day: the
parent went down with rc -15 and a Bash-spawned grandchild carried on and finished
its work about five seconds later. The athlete had already been told "Stopped."

The fix is two lines of intent. Spawn the CLI with start_new_session=True so it and
everything under it share a process group of their own, and signal the GROUP.

WHY THIS FILE IS ALL REAL PROCESSES. test_engine_cancel.py drives a FakeProc, which
is the right tool for the state machine around cancellation and the wrong tool for
this: a fake with terminate() and kill() methods will agree with whatever the code
does, and the entire content of this change is what the kernel does with a signal.
A mock cannot fail the way production failed. So everything here forks something
real and asks the OS. Nothing here spawns the claude CLI or touches the network:
the stand-in is a /bin/sh script that emits one stream-json line and then behaves
like a CLI in the middle of a tool call.

The four claims, one test class each:

  1. Cancelling kills the grandchild. This is the test that would have caught the
     gap this morning. The grandchild writes a sentinel file a few seconds in; a
     cancelled turn is one where that file never appears.
  2. A run nobody cancels is untouched, and its child really is in a session of its
     own (which is the half of the change no mock can observe).
  3. The safety rails: an already-dead group, and a process that is in OUR group.
  4. Escalation to SIGKILL still works at group level, against a child and
     grandchild that both ignore SIGTERM, AND against the one that actually bit
     while this was being built: a process forked into the group after the
     killpg, which therefore never received the SIGTERM at all.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import engine  # noqa: E402


pytestmark = pytest.mark.skipif(
    not hasattr(os, "killpg") or not hasattr(os, "getpgid"),
    reason="process groups are a POSIX concept and so is this whole deployment",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ASSISTANT_LINE = json.dumps(
    {"type": "assistant",
     "message": {"content": [{"type": "text", "text": "working on it"}]}})
RESULT_LINE = json.dumps(
    {"type": "result", "result": "the whole answer", "session_id": "sess-1"})


def _write_script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _pid_alive(pid: int) -> bool:
    """Is there still a process with this pid? Signal 0 checks permission and
    existence without delivering anything.

    A zombie answers yes, which is why no test here rests on this alone: when the
    stand-in CLI dies its child is reparented and reaped, and there is a moment in
    between where a killed grandchild still 'exists'. Every test pairs this with a
    sentinel file, which is a claim about work DONE and cannot go stale."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, deadline_s: float = 8.0) -> bool:
    end = time.time() + deadline_s
    while time.time() < end:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _read_pid(pidfile: Path, deadline_s: float = 5.0) -> int:
    """The grandchild's pid, once the script has written it. Polled rather than
    read once: the script has to get as far as forking, and on a loaded box that
    is not instant."""
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            text = pidfile.read_text().strip()
            if text:
                return int(text)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError(f"the stand-in CLI never recorded a grandchild pid in {pidfile}")


@pytest.fixture
def athlete(tmp_path):
    a = tmp_path / "athletes" / "tester"
    a.mkdir(parents=True)
    (a / "system_prompt.txt").write_text("You are a coach.\n")
    return a / "system_prompt.txt"


@pytest.fixture
def cfg(tmp_path):
    return {"project_dir": str(tmp_path)}


@pytest.fixture
def fake_cli(monkeypatch):
    """Point claude_cmd at a shell script instead of the real CLI.

    Patching claude_cmd rather than subprocess.Popen is the point: the Popen call
    in _stream_once, with its real kwargs, is under test. A test that replaced
    Popen could not tell whether start_new_session=True was passed at all."""
    def _install(script: Path):
        monkeypatch.setattr(engine, "claude_cmd",
                            lambda model, extra=None: ["/bin/sh", str(script)])
    return _install


@pytest.fixture
def spawned(monkeypatch):
    """Record every real Popen the engine makes, with its process group read at
    the moment of the spawn, while the child is certainly not yet reaped."""
    seen = []
    real_popen = subprocess.Popen

    def _popen(*a, **k):
        proc = real_popen(*a, **k)
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
        seen.append({"proc": proc, "pgid": pgid,
                     "start_new_session": k.get("start_new_session")})
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return seen


@pytest.fixture(autouse=True)
def _registry_is_clean():
    assert engine.active_run_ids() == []
    yield
    assert engine.active_run_ids() == [], "a run was left registered"


@pytest.fixture(autouse=True)
def _no_strays():
    """Belt and braces for a test that fails half way: never leave a 30-second
    sleeper behind in someone's test run.

    Append ONLY the pid of a process spawned with start_new_session, i.e. a
    session leader whose pid is its own group id. Appending anything else would
    have this teardown SIGKILL the pytest runner's group, which is the very
    mistake _signalable_group exists to prevent."""
    born = []
    yield born
    own = os.getpgrp()
    for pid in born:
        try:
            pgid = os.getpgid(pid)
        except Exception:
            continue
        if pgid <= 0 or pgid == own:
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 1. The grandchild
# ---------------------------------------------------------------------------

class TestCancellingReachesTheGrandchild:
    """The gap, stated as a test. Before this change the sentinel appeared."""

    def test_a_cancelled_turn_does_not_let_its_grandchild_finish(
            self, athlete, cfg, tmp_path, fake_cli, spawned, capsys, _no_strays):
        sentinel = tmp_path / "the-calendar-write-landed"
        pidfile = tmp_path / "grandchild.pid"
        script = _write_script(tmp_path / "cli.sh", f"""#!/bin/sh
# Stand-in for the claude CLI part way through a Bash tool call. One stream-json
# line so the engine yields a chunk and the test knows generation has begun, then
# a detached-looking child doing the slow thing (the calendar write), then a long
# wait, which is the CLI still 'thinking'.
echo '{ASSISTANT_LINE}'
sh -c 'sleep 3; : > "{sentinel}"' &
echo $! > "{pidfile}"
sleep 30
""")
        fake_cli(script)

        grandchild = None
        for ev in engine.stream_claude("replan my week", cfg, [],
                                       system_prompt_file=athlete,
                                       run_id="r-grandchild"):
            if ev[0] == "chunk" and grandchild is None:
                grandchild = _read_pid(pidfile)
                _no_strays.append(spawned[0]["proc"].pid)
                assert _pid_alive(grandchild), "the grandchild never started"
                print(f"\ngrandchild pid {grandchild} alive before the cancel: True")
                engine.cancel_run("r-grandchild", grace=0.5)

        assert grandchild is not None, "the stand-in CLI produced no chunk to cancel on"

        gone = _wait_gone(grandchild)
        print(f"grandchild pid {grandchild} alive after the cancel:  {_pid_alive(grandchild)}")

        # The load-bearing assertion. The sentinel is due 3s in; give it 5 so a
        # slow box cannot pass this test by being slow.
        time.sleep(5.0)
        assert not sentinel.exists(), (
            f"the grandchild (pid {grandchild}) survived the cancel and finished its "
            f"work - this is bug #30's second half, exactly as observed on 17 Aug")
        assert gone, f"grandchild pid {grandchild} was still alive 8s after the cancel"

    def test_the_engine_spawned_into_its_own_session(self, athlete, cfg, tmp_path,
                                                     fake_cli, spawned):
        """The mechanism behind the test above, asserted directly: the child is a
        session leader (pgid == its own pid) and its group is not ours. Without
        this there is no group to signal and _signalable_group returns None, so
        the fix silently degrades to exactly the old broken behaviour."""
        script = _write_script(tmp_path / "cli.sh",
                               f"#!/bin/sh\necho '{ASSISTANT_LINE}'\necho '{RESULT_LINE}'\n")
        fake_cli(script)
        list(engine.stream_claude("hello", cfg, [], system_prompt_file=athlete))

        assert len(spawned) == 1
        rec = spawned[0]
        assert rec["start_new_session"] is True
        assert rec["pgid"] == rec["proc"].pid, "the child is not a session leader"
        assert rec["pgid"] != os.getpgrp(), "the child is still in the bot's group"


# ---------------------------------------------------------------------------
# 2. Nobody cancels
# ---------------------------------------------------------------------------

class TestAnUncancelledRunIsUnaffected:
    """Almost every turn is this one. The 36 mock tests in test_engine_cancel.py
    cover the event contract; what they cannot cover is a real spawn with the new
    kwarg on it, so this runs one end to end against a real process."""

    def test_a_real_uncancelled_run_still_returns_its_whole_answer(
            self, athlete, cfg, tmp_path, fake_cli, spawned):
        script = _write_script(tmp_path / "cli.sh",
                               f"#!/bin/sh\necho '{ASSISTANT_LINE}'\necho '{RESULT_LINE}'\n")
        fake_cli(script)

        events = list(engine.stream_claude("hello", cfg, [],
                                           system_prompt_file=athlete,
                                           run_id="r-untouched"))
        assert ("chunk", "working on it") in events
        assert events[-1] == ("final", "the whole answer")
        assert not any(e[0] == "cancelled" for e in events)
        assert spawned[0]["proc"].returncode == 0, "the child was signalled by something"

    def test_an_uncancelled_run_leaves_its_children_alone(
            self, athlete, cfg, tmp_path, fake_cli, spawned):
        """The mirror of the grandchild test. Nobody presses stop, so the tool
        subprocess must be allowed to finish - the new signalling must not fire
        on the ordinary path."""
        sentinel = tmp_path / "tool-work-done"
        pidfile = tmp_path / "grandchild.pid"
        script = _write_script(tmp_path / "cli.sh", f"""#!/bin/sh
echo '{ASSISTANT_LINE}'
sh -c 'sleep 0.5; : > "{sentinel}"' &
echo $! > "{pidfile}"
wait
echo '{RESULT_LINE}'
""")
        fake_cli(script)
        events = list(engine.stream_claude("hello", cfg, [],
                                           system_prompt_file=athlete))
        assert events[-1] == ("final", "the whole answer")
        assert sentinel.exists(), "an uncancelled run's tool subprocess was cut short"


# ---------------------------------------------------------------------------
# 3. The safety rails on _signalable_group
# ---------------------------------------------------------------------------

class TestSignallingIsRefusedWhenItWouldBeWrong:

    def test_a_process_in_our_own_group_is_refused(self, tmp_path):
        """THE guard. A process spawned without start_new_session shares the bot's
        process group, and killpg on it would SIGTERM the bot - or, right here,
        the pytest runner. Anything that spawns without a new session must fall
        back to signalling the single pid.

        That this test finishes at all is part of what it asserts."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            assert os.getpgid(proc.pid) == os.getpgrp(), "fixture assumption broken"
            assert engine._signalable_group(proc) is None
            # And the fallback still does the job it always did.
            engine._terminate_then_kill(proc, grace=0.2)
            assert proc.wait(timeout=5) != 0
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_a_group_that_is_already_gone_does_not_raise(self, tmp_path):
        """os.killpg against a dead group raises ProcessLookupError. cancel_run
        promises never to raise, and a cancel landing microseconds after the CLI
        exited on its own is an ordinary Tuesday, not a fault."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"],
                                start_new_session=True)
        pgid = os.getpgid(proc.pid)
        proc.wait(timeout=5)
        assert _wait_gone(pgid, 5.0), "the fixture process was never reaped"

        engine._signal_group(pgid, signal.SIGTERM)      # must not raise
        engine._signal_group(pgid, signal.SIGKILL)      # must not raise
        assert engine._signalable_group(proc) is None
        engine._terminate_then_kill(proc, grace=0.05)   # must not raise

    def test_a_cancel_on_a_finished_real_process_is_silent(self, athlete, cfg,
                                                           tmp_path, fake_cli):
        """The same thing through the front door: the turn ends, then the tap
        arrives. cancel_run finds no run and returns False, and nothing anywhere
        throws."""
        script = _write_script(tmp_path / "cli.sh",
                               f"#!/bin/sh\necho '{RESULT_LINE}'\n")
        fake_cli(script)
        list(engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                  run_id="r-late"))
        assert engine.cancel_run("r-late") is False


# ---------------------------------------------------------------------------
# 4. Escalation, at group level
# ---------------------------------------------------------------------------

class TestEscalationAtGroupLevel:

    def test_a_group_that_ignores_sigterm_is_sigkilled(self, athlete, cfg, tmp_path,
                                                       fake_cli, spawned, _no_strays):
        """SIGTERM first because the CLI flushes and tidies on it; SIGKILL after
        the grace because a process that ignores SIGTERM would otherwise keep the
        athlete waiting forever. Both signals now go to the group, so a child AND
        grandchild that both trap SIGTERM still die."""
        sentinel = tmp_path / "survived-the-term"
        pidfile = tmp_path / "grandchild.pid"
        script = _write_script(tmp_path / "cli.sh", f"""#!/bin/sh
trap '' TERM
echo '{ASSISTANT_LINE}'
sh -c 'trap "" TERM; sleep 3; : > "{sentinel}"' &
echo $! > "{pidfile}"
sleep 30
""")
        fake_cli(script)

        grandchild = None
        for ev in engine.stream_claude("replan my week", cfg, [],
                                       system_prompt_file=athlete,
                                       run_id="r-stubborn-group"):
            if ev[0] == "chunk" and grandchild is None:
                grandchild = _read_pid(pidfile)
                _no_strays.append(spawned[0]["proc"].pid)
                engine.cancel_run("r-stubborn-group", grace=0.3)

        assert grandchild is not None
        child = spawned[0]["proc"]
        assert child.wait(timeout=10) == -signal.SIGKILL, (
            "the child ignored SIGTERM and was never escalated to SIGKILL")
        gone = _wait_gone(grandchild)
        print(f"\nSIGTERM-ignoring grandchild pid {grandchild}: "
              f"alive after escalation = {_pid_alive(grandchild)}")
        time.sleep(3.5)
        assert not sentinel.exists(), (
            "the SIGTERM-ignoring grandchild outlived the SIGKILL escalation")
        assert gone, f"grandchild pid {grandchild} survived the group SIGKILL"

    def test_a_member_forked_after_the_sigterm_is_still_killed(
            self, athlete, cfg, tmp_path, fake_cli, spawned, _no_strays):
        """Signalling a group is not a barrier. A process forked AFTER the killpg
        is in the group but never received the signal, and if the leader then
        exits politely, an escalation keyed on the leader never fires.

        This was not a theory. Cancelling six milliseconds after the first line of
        output, watching the pids: the shell died with rc -15 at t+0.05s, a child
        that first appeared at t+0.053s ran its full 30 seconds, and proc.wait()
        had returned cleanly so nothing escalated. Three runs in four. That window
        is the moment a tool call starts, which is the calendar write the whole
        change exists to stop.

        The race is a few milliseconds wide, so it is CONSTRUCTED here rather than
        raced for. The stand-in traps SIGTERM, so it survives the signal; only then
        does it fork the grandchild, so the grandchild provably never saw it; then
        it exits 0, so proc.wait() returns inside the grace and the leader-only
        trigger provably does not fire. What kills the grandchild can therefore
        only be the group liveness check.

        The timings are load bearing. The leader's own pause is 1s and the grace
        is 3s, so the fork happens well before the grace runs out: the other way
        round, the SIGKILL lands on the leader first, the grandchild is never born
        at all, and the test is green while proving nothing. That is not a
        hypothetical, it is what the first draft of this test did."""
        sentinel = tmp_path / "the-calendar-write-landed"
        pidfile = tmp_path / "grandchild.pid"
        script = _write_script(tmp_path / "cli.sh", f"""#!/bin/sh
trap 'true' TERM
echo '{ASSISTANT_LINE}'
sleep 1
sh -c 'sleep 2; : > "{sentinel}"' &
echo $! > "{pidfile}"
exit 0
""")
        fake_cli(script)

        for ev in engine.stream_claude("replan my week", cfg, [],
                                       system_prompt_file=athlete,
                                       run_id="r-forked-late"):
            if ev[0] == "chunk":
                _no_strays.append(spawned[0]["proc"].pid)
                engine.cancel_run("r-forked-late", grace=3.0)

        grandchild = _read_pid(pidfile)
        print(f"\ngrandchild forked after the SIGTERM: pid {grandchild}")
        assert spawned[0]["proc"].returncode == 0, (
            "the leader was supposed to trap the SIGTERM and exit cleanly, so that "
            "only a group-level escalation could account for the grandchild dying")
        gone = _wait_gone(grandchild)
        time.sleep(3.5)
        assert not sentinel.exists(), (
            f"the grandchild (pid {grandchild}) was forked into the group after the "
            f"SIGTERM, never received it, and finished its work anyway")
        assert gone, f"grandchild pid {grandchild} outlived the group SIGKILL"

    def test_a_group_that_obeys_sigterm_is_not_sigkilled(self, athlete, cfg, tmp_path,
                                                         fake_cli, spawned, _no_strays):
        """The polite path stays polite. Died on the SIGTERM and was never
        SIGKILLed: if that ever flips, the CLI has stopped getting the chance to
        flush and tidy on its way out.

        Either encoding of "SIGTERM did it" is accepted. A shell killed by a
        signal usually shows up as a negative returncode, but some shells report
        128+signum as an ordinary exit status instead, and which /bin/sh is in
        play is not what this test is about."""
        script = _write_script(tmp_path / "cli.sh",
                               f"#!/bin/sh\necho '{ASSISTANT_LINE}'\nsleep 30\n")
        fake_cli(script)

        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-polite-group"):
            if ev[0] == "chunk":
                _no_strays.append(spawned[0]["proc"].pid)
                engine.cancel_run("r-polite-group", grace=2.0)

        rc = spawned[0]["proc"].wait(timeout=10)
        assert rc != -signal.SIGKILL, "escalated to SIGKILL on a group that went quietly"
        assert rc in (-signal.SIGTERM, 128 + signal.SIGTERM), rc

    def test_the_cancel_returns_before_the_grace_elapses(self, athlete, cfg, tmp_path,
                                                         fake_cli, spawned, _no_strays):
        """Unchanged contract, re-checked against a real process now that the
        signalling path is different: cancel_run is called inline from the poll
        loop, so it must never sit out the grace."""
        script = _write_script(tmp_path / "cli.sh",
                               f"#!/bin/sh\ntrap '' TERM\necho '{ASSISTANT_LINE}'\nsleep 30\n")
        fake_cli(script)

        elapsed = None
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-quick-group"):
            if ev[0] == "chunk":
                proc = spawned[0]["proc"]
                _no_strays.append(proc.pid)
                t0 = time.time()
                engine.cancel_run("r-quick-group", grace=20.0)
                elapsed = time.time() - t0
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # tidy up
        assert elapsed is not None and elapsed < 1.0, elapsed


# ---------------------------------------------------------------------------
# A note on what is NOT tested here
# ---------------------------------------------------------------------------
#
# pgid recycling. proc.wait() reaps the leader, so in principle the group id could
# be taken over between that and the SIGKILL. The window is the microseconds
# between two lines and it would need a new process to claim that exact pid AND
# make itself a session leader, so there is nothing here a test could hold still
# long enough to observe. It is written down in _terminate_then_kill instead.
#
# The real claude CLI. Whether its Bash tool passes our stdout pipe through
# to the commands it runs is unknown, and if it does, a killed CLI whose grandchild
# holds the pipe open would leave the engine's `for raw_line in proc.stdout` loop
# blocked. Killing the group fixes that too, but the claim is untested and should
# not be relied on until someone has watched it.
