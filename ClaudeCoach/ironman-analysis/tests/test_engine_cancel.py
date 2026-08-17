"""Cancelling an in-flight coach turn (bug #30, 17 Aug 2026).

Kathryn said "2.5 hours" meaning one day, the bot read it as a week-wide constraint
and began replanning her whole week, and she had to sit through it. Turns measured
100 to 500s that day, so there was no way out of a wrong answer for minutes at a
time. lib/engine.py owns the subprocess, so it owns cancellation; the transports
only ask.

What these tests pin, and why each one exists:

  1. A cancelled run reports ('cancelled', partial), NOT ('final', ...). The
     transport has to be able to tell "the athlete stopped this" from "it broke",
     because a cancelled turn must not be posted and must not be written to
     history, while a crash still needs its error path.
  2. Nobody cancelling changes NOTHING. This ships ahead of its own UI and will sit
     in production unused for a while, so the no-cancel path is the one that has to
     be provably untouched.
  3. THE RACE. A cancel can arrive late: the turn the athlete wanted stopped
     finishes on its own, a new turn for the same chat starts, and the stale cancel
     lands on the new one. Killing the athlete's fresh, correct request silently is
     worse than the bug being fixed. Run ids are unique and never reused and
     cancel_run matches exactly, so a stale cancel is a no-op.
  4. A cancelled turn does not roll into stream_claude's fallbacks. A killed
     process looks exactly like a dead resume, and spawning a replacement is the
     one thing the athlete was trying to stop.
  5. The registry does not leak. bot.py runs for weeks.

Every test drives a fake process. Nothing here spawns the real `claude` CLI.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))

import engine  # noqa: E402


# ---------------------------------------------------------------------------
# A fake `claude` process: a scripted stream-json stdout, an optionally
# hanging tail, and terminate/kill that behave like the real thing.
# ---------------------------------------------------------------------------

SCRIPT = [
    json.dumps({"type": "system", "session_id": "sess-1"}),
    json.dumps({"type": "assistant",
                "message": {"content": [{"type": "text", "text": "half an answer"}]}}),
    json.dumps({"type": "result", "result": "the whole answer",
                "session_id": "sess-1"}),
]

# Just the opening two lines: a turn that is still generating when it is stopped.
PARTIAL_SCRIPT = SCRIPT[:2]


class _FakeStdin:
    def __init__(self, raises=None):
        self.written = []
        self.closed = False
        self._raises = raises

    def write(self, text):
        if self._raises:
            raise self._raises
        self.written.append(text)

    def close(self):
        self.closed = True


class FakeProc:
    """Mimics the parts of Popen the stream loop touches.

    `hang=True` keeps stdout open after the scripted lines, the way a real CLI
    holds the pipe open while it is still thinking - that is what gives a test a
    window in which to cancel. The pipe closes when the process dies, which is
    exactly how a kill ends the engine's read loop.
    """

    def __init__(self, lines, *, hang=False, ignore_terminate=False,
                 stdin_raises=None, hang_timeout=5.0, exit_code=0):
        self.pid = 4242
        self.returncode = None
        self.exit_code = exit_code
        self.stdin = _FakeStdin(stdin_raises)
        self.ignore_terminate = ignore_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.first_line_out = threading.Event()   # test sync: streaming has begun
        self._exited = threading.Event()
        self.stdout = self._emit(list(lines), hang, hang_timeout)

    def _emit(self, lines, hang, hang_timeout):
        for line in lines:
            if self._exited.is_set():
                break
            yield line + "\n"
            self.first_line_out.set()
        if hang and not self._exited.is_set():
            # Still "generating". Ends when someone kills us, or on the timeout
            # so a broken test fails rather than hanging the suite.
            self._exited.wait(hang_timeout)
        if not self._exited.is_set():
            self.returncode = self.exit_code
            self._exited.set()

    def terminate(self):
        self.terminate_calls += 1
        if self.ignore_terminate:
            return
        if not self._exited.is_set():
            self.returncode = -15
            self._exited.set()

    def kill(self):
        self.kill_calls += 1
        if not self._exited.is_set():
            self.returncode = -9
            self._exited.set()

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return self.returncode


@pytest.fixture
def athlete(tmp_path):
    """A minimal athlete tree. scoped_env derives the slug from this path and
    _plan_session reads/writes .chat_session.json next to it."""
    a = tmp_path / "athletes" / "tester"
    a.mkdir(parents=True)
    (a / "system_prompt.txt").write_text("You are a coach.\n")
    return a / "system_prompt.txt"


@pytest.fixture
def cfg(tmp_path):
    return {"project_dir": str(tmp_path)}


@pytest.fixture
def spawn(monkeypatch):
    """Hand out fake processes in order and record what was spawned."""
    made = []

    def _install(*procs):
        queue = list(procs)

        def _popen(*_a, **_k):
            proc = queue.pop(0) if queue else FakeProc(SCRIPT)
            made.append(proc)
            return proc

        monkeypatch.setattr(subprocess, "Popen", _popen)
        return made

    return _install


@pytest.fixture(autouse=True)
def _registry_is_clean():
    """Every test starts and ends with an empty registry, so a leak in one test
    cannot be mistaken for a pass in the next."""
    assert engine.active_run_ids() == []
    yield
    assert engine.active_run_ids() == [], "a run was left registered"


def drain(gen):
    return list(gen)


# ---------------------------------------------------------------------------
# 1. Nobody cancels: nothing changes
# ---------------------------------------------------------------------------

class TestUncancelledIsUnchanged:
    """The non-negotiable. This lands ahead of the UI that drives it, so the
    dormant case is the one that has to be provably identical."""

    def test_plain_run_yields_chunk_then_final(self, athlete, cfg, spawn):
        spawn(FakeProc(SCRIPT))
        events = drain(engine.stream_claude(
            "hello", cfg, [], system_prompt_file=athlete, athlete_name="Tester"))
        assert events == [("chunk", "half an answer"),
                          ("final", "the whole answer")]

    def test_plain_run_still_persists_its_session(self, athlete, cfg, spawn):
        spawn(FakeProc(SCRIPT))
        drain(engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                   athlete_name="Tester"))
        st = json.loads((athlete.parent / ".chat_session.json").read_text())
        assert st["session_id"] == "sess-1"
        assert st["turns"] == 1

    def test_a_run_id_alone_changes_nothing(self, athlete, cfg, spawn):
        """Wiring a run id in must be inert until someone actually cancels."""
        spawn(FakeProc(SCRIPT))
        events = drain(engine.stream_claude(
            "hello", cfg, [], system_prompt_file=athlete, athlete_name="Tester",
            run_id="r-inert", run_owner=42))
        assert events == [("chunk", "half an answer"),
                          ("final", "the whole answer")]
        assert (athlete.parent / ".chat_session.json").exists()

    def test_no_cancelled_event_ever_appears(self, athlete, cfg, spawn):
        spawn(FakeProc(SCRIPT))
        events = drain(engine.stream_claude(
            "hello", cfg, [], system_prompt_file=athlete, run_id="r-inert2"))
        assert all(e[0] != "cancelled" for e in events)

    def test_crash_still_ends_in_final_not_cancelled(self, athlete, cfg, monkeypatch):
        """A spawn failure is NOT a cancellation. The two need different handling
        and the whole point of the new event is telling them apart."""
        def _boom(*_a, **_k):
            raise OSError("no such binary")
        monkeypatch.setattr(subprocess, "Popen", _boom)
        events = drain(engine.stream_claude(
            "hello", cfg, [], system_prompt_file=athlete, run_id="r-crash"))
        assert events == [("final", "(no response)")]


# ---------------------------------------------------------------------------
# 2. A cancelled run reports as cancelled
# ---------------------------------------------------------------------------

class TestCancelledRunReportsCancelled:

    def test_cancel_mid_stream_ends_in_cancelled_event(self, athlete, cfg, spawn):
        proc = FakeProc(PARTIAL_SCRIPT, hang=True)
        spawn(proc)
        events = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-1", run_owner=42):
            events.append(ev)
            if ev[0] == "chunk":
                assert engine.cancel_run("r-1", owner=42, grace=0.05) is True
        assert events[0] == ("chunk", "half an answer")
        assert events[-1][0] == "cancelled", events
        assert all(e[0] != "final" for e in events), "a cancelled run must not look finished"
        assert proc.terminate_calls >= 1

    def test_cancelled_event_carries_the_partial_for_logging(self, athlete, cfg, spawn):
        spawn(FakeProc(PARTIAL_SCRIPT, hang=True))
        events = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-2"):
            events.append(ev)
            if ev[0] == "chunk":
                engine.cancel_run("r-2", grace=0.05)
        assert events[-1] == ("cancelled", "half an answer")

    def test_cancelled_run_writes_no_session_state(self, athlete, cfg, spawn):
        """A killed CLI session is not something to resume from, and the turn must
        leave nothing behind that the next turn would build on."""
        spawn(FakeProc(PARTIAL_SCRIPT, hang=True))
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-3"):
            if ev[0] == "chunk":
                engine.cancel_run("r-3", grace=0.05)
        assert not (athlete.parent / ".chat_session.json").exists()

    def test_a_cancel_that_lands_on_a_finished_turn_still_suppresses_it(
            self, athlete, cfg, spawn):
        """A judgement call, pinned because it is one. The kill can land in the
        last moments of a turn: the CLI exits 0 with a complete answer before the
        signal reaches it. The athlete still asked for that answer to be dropped,
        so we drop it - the flag decides, not the return code. The cost is a
        session whose turn counter did not advance, which only rotates it a turn
        early; the alternative is the wall of text they were trying to stop.
        A long grace keeps the escalation out of the way: the process is exiting
        on its own."""
        proc = FakeProc(SCRIPT, ignore_terminate=True)
        spawn(proc)
        events = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-photo-finish"):
            events.append(ev)
            if ev[0] == "chunk":
                engine.cancel_run("r-photo-finish", grace=30.0)
        assert proc.returncode == 0, "this test needs a CLEAN exit to be meaningful"
        assert events[-1] == ("cancelled", "the whole answer")
        assert not (athlete.parent / ".chat_session.json").exists(), \
            "a cancelled turn persisted its session"

    def test_cancel_from_another_thread(self, athlete, cfg, spawn):
        """The real shape of it: bot.py streams on a worker thread and the cancel
        arrives on the poll loop, so the two never share a thread."""
        proc = FakeProc(PARTIAL_SCRIPT, hang=True)
        spawn(proc)
        events = []

        def _consume():
            events.extend(engine.stream_claude(
                "hello", cfg, [], system_prompt_file=athlete,
                run_id="r-thread", run_owner=7))

        worker = threading.Thread(target=_consume)
        worker.start()
        assert proc.first_line_out.wait(5), "the fake never started streaming"
        assert engine.cancel_run("r-thread", owner=7, grace=0.05) is True
        worker.join(timeout=5)
        assert not worker.is_alive(), "the stream did not end when the process died"
        assert events[-1][0] == "cancelled", events

    def test_cancel_before_the_spawn_never_starts_a_process(self, athlete, cfg,
                                                            spawn, monkeypatch):
        """Prompt assembly reads a dozen files, so there is a real window between
        registering the run and spawning. A process started after "stop" is a
        process nobody is waiting for."""
        made = spawn(FakeProc(SCRIPT))
        real_plan = engine._plan_session

        def _slow_plan(*a, **k):
            out = real_plan(*a, **k)
            engine.cancel_run("r-early")     # lands while we are still assembling
            return out
        monkeypatch.setattr(engine, "_plan_session", _slow_plan)

        events = drain(engine.stream_claude("hello", cfg, [],
                                            system_prompt_file=athlete,
                                            run_id="r-early"))
        assert made == [], "a process was spawned after the run was cancelled"
        assert events == [("cancelled", "")]

    def test_cancel_between_spawn_and_publish_still_kills(self, athlete, cfg,
                                                          monkeypatch):
        """The nastiest window: the cancel reads run.proc as None a moment before
        Popen returns. The re-check under the lock is what stops the process it
        was meant to kill running to completion."""
        proc = FakeProc(PARTIAL_SCRIPT, hang=True)

        def _popen(*_a, **_k):
            engine.cancel_run("r-window", grace=0.05)   # no proc published yet
            return proc
        monkeypatch.setattr(subprocess, "Popen", _popen)

        events = drain(engine.stream_claude("hello", cfg, [],
                                            system_prompt_file=athlete,
                                            run_id="r-window"))
        assert proc.terminate_calls >= 1, "the missed cancel was never re-applied"
        assert events[-1][0] == "cancelled", events


# ---------------------------------------------------------------------------
# 3. THE RACE: a late cancel must not kill the next turn
# ---------------------------------------------------------------------------

class TestLateCancelCannotKillTheNextRun:
    """If the registry were keyed by chat id, or cancel_run fell back to "whatever
    is running for this owner", this is the sequence that would silently kill an
    athlete's fresh, correct request: turn A ends, turn B starts for the same
    chat, A's cancel lands. Run ids are unique and never reused, so A's id matches
    nothing once A is done."""

    def test_stale_id_does_not_stop_the_new_run(self, athlete, cfg, spawn):
        spawn(FakeProc(SCRIPT), FakeProc(SCRIPT))

        # Turn A: same chat, runs to completion. The athlete's "stop" is already
        # in flight at this point but has not been actioned yet.
        first = drain(engine.stream_claude("first", cfg, [],
                                           system_prompt_file=athlete,
                                           run_id="run-A", run_owner=42))
        assert first[-1][0] == "final"

        # Turn B: same chat, new run, already streaming when A's cancel lands.
        events = []
        for ev in engine.stream_claude("second", cfg, [], system_prompt_file=athlete,
                                       run_id="run-B", run_owner=42):
            events.append(ev)
            if ev[0] == "chunk":
                assert engine.cancel_run("run-A", owner=42) is False, \
                    "a finished run's id matched something"

        assert events[-1] == ("final", "the whole answer"), \
            "the late cancel killed the NEW run: " + repr(events)
        assert all(e[0] != "cancelled" for e in events)

    def test_the_new_run_is_untouched_not_merely_unreported(self, athlete, cfg, spawn):
        """Assert on the process, not just on the event: a defence that returns
        False while still signalling something would pass the test above."""
        proc_a, proc_b = FakeProc(SCRIPT), FakeProc(SCRIPT)
        spawn(proc_a, proc_b)
        drain(engine.stream_claude("first", cfg, [], system_prompt_file=athlete,
                                   run_id="run-A2", run_owner=42))
        for ev in engine.stream_claude("second", cfg, [], system_prompt_file=athlete,
                                       run_id="run-B2", run_owner=42):
            if ev[0] == "chunk":
                engine.cancel_run("run-A2", owner=42)
        assert proc_b.terminate_calls == 0, "the new run's process was signalled"
        assert proc_b.kill_calls == 0

    def test_owner_mismatch_is_refused(self, athlete, cfg, spawn):
        """Belt and braces on top of the unique id: naming the right run from the
        wrong chat does nothing."""
        proc = FakeProc(SCRIPT)
        spawn(proc)
        events = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-owned", run_owner=42):
            events.append(ev)
            if ev[0] == "chunk":
                assert engine.cancel_run("r-owned", owner=99) is False
        assert events[-1][0] == "final"
        assert proc.terminate_calls == 0

    def test_ids_are_unique(self):
        assert len({engine.new_run_id() for _ in range(1000)}) == 1000

    def test_a_duplicate_id_does_not_hijack_the_first_run(self, athlete, cfg, spawn):
        """A caller reusing an id is their bug, but it must not make one turn's
        cancel land on another's process."""
        first = engine._register_run("dup", owner=1)
        second = engine._register_run("dup", owner=2)
        try:
            assert second.run_id != "dup", "the second run took the first one's id"
            assert engine.active_run_ids(owner=2) == [second.run_id]
        finally:
            engine._deregister_run(first)
            engine._deregister_run(second)


# ---------------------------------------------------------------------------
# 4. Cancels that should do nothing, and cancels that repeat
# ---------------------------------------------------------------------------

class TestHarmlessCancels:

    def test_cancelling_a_finished_run_is_a_no_op(self, athlete, cfg, spawn):
        spawn(FakeProc(SCRIPT))
        events = drain(engine.stream_claude("hello", cfg, [],
                                            system_prompt_file=athlete,
                                            run_id="r-done", run_owner=42))
        assert events[-1][0] == "final"
        assert engine.cancel_run("r-done", owner=42) is False
        assert engine.cancel_run("r-done") is False

    def test_cancelling_an_unknown_run_is_a_no_op(self):
        assert engine.cancel_run("never-existed") is False
        assert engine.cancel_run(None) is False
        assert engine.cancel_run("") is False

    def test_two_cancels_for_the_same_run(self, athlete, cfg, spawn):
        """The athlete presses stop twice, or taps a button that double-fires.
        Both calls succeed, neither raises, and the turn still ends cancelled."""
        proc = FakeProc(PARTIAL_SCRIPT, hang=True)
        spawn(proc)
        events = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-twice"):
            events.append(ev)
            if ev[0] == "chunk":
                assert engine.cancel_run("r-twice", grace=0.05) is True
                assert engine.cancel_run("r-twice", grace=0.05) is True
        assert events[-1][0] == "cancelled"

    def test_a_cancel_does_not_take_the_caller_down(self, athlete, cfg, spawn):
        """cancel_run runs on the poll loop. A process that throws on terminate
        must not raise into it."""
        class _Angry(FakeProc):
            def terminate(self):
                raise OSError("no such process")

        proc = _Angry(PARTIAL_SCRIPT, hang=True, hang_timeout=1.0)
        spawn(proc)
        events = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-angry"):
            events.append(ev)
            if ev[0] == "chunk":
                assert engine.cancel_run("r-angry", grace=0.05) is True
        assert events[-1][0] == "cancelled"

    def test_broken_pipe_on_the_prompt_write_is_survivable(self, athlete, cfg, spawn):
        """A kill mid-prompt breaks the stdin write. That is an ordinary outcome
        of cancelling, not a fault, and must stay silent."""
        spawn(FakeProc(SCRIPT, stdin_raises=BrokenPipeError("gone")))
        events = drain(engine.stream_claude("hello", cfg, [],
                                            system_prompt_file=athlete,
                                            run_id="r-pipe"))
        assert events[-1][0] == "final"


# ---------------------------------------------------------------------------
# 5. Terminate, then kill
# ---------------------------------------------------------------------------

class TestEscalation:

    def test_a_process_that_ignores_terminate_is_killed(self, athlete, cfg, spawn):
        proc = FakeProc(PARTIAL_SCRIPT, hang=True, ignore_terminate=True)
        spawn(proc)
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-stubborn"):
            if ev[0] == "chunk":
                engine.cancel_run("r-stubborn", grace=0.05)
        assert proc.terminate_calls >= 1
        assert proc.kill_calls >= 1, "SIGTERM was ignored and nothing escalated"

    def test_a_process_that_obeys_terminate_is_not_killed(self, athlete, cfg, spawn):
        """The polite path stays polite: no SIGKILL on a CLI that went quietly."""
        proc = FakeProc(PARTIAL_SCRIPT, hang=True)
        spawn(proc)
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-polite"):
            if ev[0] == "chunk":
                engine.cancel_run("r-polite", grace=0.5)
        assert proc.terminate_calls >= 1
        assert proc.kill_calls == 0

    def test_cancel_returns_without_waiting_out_the_grace(self, athlete, cfg, spawn):
        """The caller is a poll loop. Escalation happens on a daemon thread, so
        cancel_run returns immediately however long the grace is."""
        import time
        proc = FakeProc(PARTIAL_SCRIPT, hang=True, ignore_terminate=True,
                        hang_timeout=2.0)
        spawn(proc)
        elapsed = None
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-quick"):
            if ev[0] == "chunk":
                t0 = time.time()
                engine.cancel_run("r-quick", grace=30.0)
                elapsed = time.time() - t0
                proc.kill()          # tidy up: nothing else will, with grace=30
        assert elapsed is not None and elapsed < 1.0, elapsed


# ---------------------------------------------------------------------------
# 6. The registry must not leak
# ---------------------------------------------------------------------------

class TestRegistryLifetime:
    """bot.py runs for weeks. An entry left behind is both a slow leak and a run
    that can never be cancelled again under that id."""

    def test_entry_is_gone_after_a_clean_run(self, athlete, cfg, spawn):
        spawn(FakeProc(SCRIPT))
        drain(engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                   run_id="r-clean"))
        assert engine.active_run_ids() == []

    def test_entry_is_gone_when_the_run_raises(self, athlete, cfg, monkeypatch):
        """_stream_once swallows everything, so the raise has to come from the
        setup around it - which is exactly where an unguarded leak would live."""
        def _boom(*_a, **_k):
            raise RuntimeError("prompt assembly blew up")
        monkeypatch.setattr(engine, "_plan_session", _boom)
        with pytest.raises(RuntimeError):
            drain(engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-raise"))
        assert engine.active_run_ids() == []

    def test_entry_is_gone_when_the_consumer_walks_away(self, athlete, cfg, spawn):
        """bot.py breaks out of its loop on ('final',...) instead of draining, so
        the generator is closed rather than exhausted. That is the NORMAL path."""
        spawn(FakeProc(SCRIPT))
        gen = engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                   run_id="r-abandoned")
        for ev in gen:
            if ev[0] == "final":
                break
        gen.close()
        assert engine.active_run_ids() == []

    def test_entry_is_gone_after_a_cancel(self, athlete, cfg, spawn):
        spawn(FakeProc(PARTIAL_SCRIPT, hang=True))
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-gone"):
            if ev[0] == "chunk":
                engine.cancel_run("r-gone", grace=0.05)
        assert engine.active_run_ids() == []

    def test_a_run_is_visible_while_it_is_in_flight(self, athlete, cfg, spawn):
        spawn(FakeProc(SCRIPT))
        seen = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-visible", run_owner=42):
            if ev[0] == "chunk":
                seen = engine.active_run_ids(owner=42)
        assert seen == ["r-visible"]

    def test_an_unnamed_run_is_registered_under_a_generated_id(self, athlete, cfg, spawn):
        """No run id from the caller still means a tracked run, so ops can see
        what is in flight - it just has no name anyone outside can cancel by."""
        spawn(FakeProc(SCRIPT))
        seen = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete):
            if ev[0] == "chunk":
                seen = engine.active_run_ids()
        assert len(seen) == 1 and seen[0]


# ---------------------------------------------------------------------------
# 6b. The premise everything else rests on
# ---------------------------------------------------------------------------

class TestKillingAProcessEndsTheReadLoop:
    """Every other test here drives a fake whose stdout I wrote to stop when the
    fake is killed - which is my own fake agreeing with me. The design rests on
    an OS fact: kill the child and its stdout pipe closes, which ends
    `for line in proc.stdout` with no polling and no timeout. This is the one
    test that checks the fact rather than the fake.

    A real pipe and a real signal, but a trivial python child, not the claude
    CLI: nothing here spawns a model or touches the network."""

    def test_a_real_pipe_closes_when_the_child_is_killed(self):
        import time
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c",
             "print('first line', flush=True)\nimport time; time.sleep(30)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            lines = []
            killer = threading.Timer(0.5, lambda: engine._terminate_then_kill(
                proc, grace=0.5))
            killer.start()
            t0 = time.time()
            for line in proc.stdout:          # would sit here for 30s otherwise
                lines.append(line.strip())
            elapsed = time.time() - t0
            killer.cancel()
            assert lines == ["first line"]
            assert elapsed < 10, f"the read loop did not end on the kill ({elapsed:.1f}s)"
            assert proc.wait(timeout=5) != 0, "the child was not signalled"
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()


# ---------------------------------------------------------------------------
# 7. A cancelled turn must not roll into a fallback
# ---------------------------------------------------------------------------

class TestCancelBeatsTheFallbacks:
    """stream_claude can spawn up to three processes for one turn. A killed
    process is indistinguishable from a dead resume (non-zero rc, no text), so
    without the sticky flag, stopping a turn would immediately start another one
    and the athlete would watch the wrong answer begin again."""

    def _arm_resume(self, athlete):
        """A live, resumable session, so the dead-resume fallback is armed."""
        import time as _time
        engine._save_session(athlete, {
            "session_id": "sess-old", "fp": engine._prompt_fingerprint(athlete),
            "turns": 1, "started": _time.time(), "last_seen": ""})

    def test_a_cancelled_resume_does_not_start_a_fresh_session(self, athlete, cfg,
                                                               monkeypatch):
        # The cancel has to land BEFORE any text streams, because that is the
        # case the two conditions collide on: rc != 0 with nothing streamed is
        # PRECISELY what a dead resume looks like. Cancel after a chunk and the
        # fallback would not have fired anyway, so the test would prove nothing.
        self._arm_resume(athlete)
        made = []
        procs = [FakeProc([], hang=True), FakeProc(SCRIPT)]

        def _popen(*_a, **_k):
            proc = procs[len(made)]
            made.append(proc)
            if len(made) == 1:
                engine.cancel_run("r-resume", grace=0.05)
            return proc
        monkeypatch.setattr(subprocess, "Popen", _popen)

        events = drain(engine.stream_claude("hello", cfg, [],
                                            system_prompt_file=athlete,
                                            run_id="r-resume"))
        assert len(made) == 1, "a second process was spawned for a cancelled turn"
        assert events == [("cancelled", "")]
        # And the fallback's SIDE EFFECT must not have happened either. Entering
        # that branch calls _clear_session, so a cancelled turn would throw away a
        # perfectly good session and make the athlete's NEXT message pay for a
        # full 82KB cold start. This is the assertion that distinguishes the
        # stream_claude guard from _stream_once's refusal to spawn.
        kept = json.loads((athlete.parent / ".chat_session.json").read_text())
        assert kept["session_id"] == "sess-old", "the cancel binned a live session"

    def test_an_uncancelled_dead_resume_still_falls_back(self, athlete, cfg, spawn):
        """The mirror: the guard must not have disabled the fallback for everyone
        else. Same shape of first process, nobody cancelling."""
        self._arm_resume(athlete)
        made = spawn(FakeProc([], exit_code=1), FakeProc(SCRIPT))
        events = drain(engine.stream_claude("hello", cfg, [],
                                            system_prompt_file=athlete,
                                            run_id="r-resume-ok"))
        assert len(made) == 2, "the dead-resume fallback stopped firing"
        assert events[-1] == ("final", "the whole answer")

    def test_a_cancelled_rate_limited_turn_does_not_retry_on_sonnet(self, athlete, cfg,
                                                                    spawn, monkeypatch):
        monkeypatch.setattr(engine, "_is_limit_message", lambda _t: True)
        made = spawn(FakeProc(PARTIAL_SCRIPT, hang=True), FakeProc(SCRIPT))
        events = []
        for ev in engine.stream_claude("hello", cfg, [], system_prompt_file=athlete,
                                       run_id="r-limit"):
            events.append(ev)
            if ev[0] == "chunk":
                engine.cancel_run("r-limit", grace=0.05)
        assert len(made) == 1, "a cancelled turn was retried on another model"
        # The payload is the discriminating assertion. Entering the fallback
        # branch overwrites final/streamed with the second (never-spawned) run's
        # empty result, so the partial we killed would be lost from the log even
        # though no process was started. _stream_once refusing to spawn is the
        # backstop; this guard is what keeps the turn coherent.
        assert events[-1] == ("cancelled", "half an answer"), events

    def test_an_uncancelled_rate_limited_turn_still_falls_back(self, athlete, cfg,
                                                               spawn, monkeypatch):
        """The mirror of the above: the guard must not have disabled the fallback
        for everyone else."""
        calls = {"n": 0}

        def _limited(_t):
            calls["n"] += 1
            return calls["n"] == 1          # only the first turn looks capped
        monkeypatch.setattr(engine, "_is_limit_message", _limited)
        made = spawn(FakeProc(SCRIPT), FakeProc(SCRIPT))
        events = drain(engine.stream_claude("hello", cfg, [],
                                            system_prompt_file=athlete,
                                            run_id="r-limit-ok"))
        assert len(made) == 2, "the Opus to Sonnet fallback stopped firing"
        assert events[-1] == ("final", "the whole answer")
