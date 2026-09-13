"""The machine stays awake while the agent is waiting on something.

An agent that backgrounds a twenty-minute build and waits for it loses
the build if the machine idle-sleeps underneath it. The registry holds
a power assertion from the moment a background process is announced
until it completes.

The limit worth knowing, and it is a hard one: this prevents *idle*
sleep. Closing the lid on an Apple laptop sleeps the machine whatever
assertion is held, so these tests pin the refcounting and the pairing,
not a promise the OS will not keep.
"""

from __future__ import annotations

from ember_code.core.tools.process_registry import ProcessRegistry
from ember_code.core.tools.sleep_blocker import SleepBlocker


class _FakeBlocker(SleepBlocker):
    """Counts instead of spawning ``caffeinate``.

    Subclasses the real thing so the registry's calls are type-checked
    against the real surface — a bare mock would keep passing if
    ``acquire``/``release`` were renamed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.acquires = 0
        self.releases = 0
        self.depth = 0

    @property
    def supported(self) -> bool:
        return True

    def acquire(self) -> None:
        self.acquires += 1
        self.depth += 1

    def release(self) -> None:
        self.releases += 1
        self.depth -= 1


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = 0


class _FakeManaged:
    def __init__(self, pid: int) -> None:
        self.proc = _FakeProc(pid)
        self.cmd = f"sleep {pid}"
        self.started_at = 0.0

    def read(self, tail: int = 40) -> str:
        return ""


def _registry(blocker):
    return ProcessRegistry(sleep_blocker=blocker)


def test_a_started_process_holds_the_machine_awake():
    blocker = _FakeBlocker()
    reg = _registry(blocker)

    reg.announce_start(_FakeManaged(101))

    assert blocker.acquires == 1
    assert blocker.depth == 1


def test_the_hold_is_dropped_when_the_process_finishes():
    blocker = _FakeBlocker()
    reg = _registry(blocker)
    mp = _FakeManaged(102)

    reg.announce_start(mp)
    reg.emit_completion(mp)

    assert blocker.depth == 0, "the assertion outlived the process it was taken for"


def test_two_processes_hold_one_assertion_between_them():
    """Refcounted, not a boolean. The first to finish must not wake the
    machine up under the second."""
    blocker = _FakeBlocker()
    reg = _registry(blocker)
    first, second = _FakeManaged(201), _FakeManaged(202)

    reg.announce_start(first)
    reg.announce_start(second)
    reg.emit_completion(first)

    assert blocker.depth == 1, "the surviving process lost its hold"

    reg.emit_completion(second)
    assert blocker.depth == 0


def test_the_real_blocker_clamps_at_zero():
    """A stray release must not drive the count negative — a later
    acquire would then be a no-op and the machine would sleep through
    the work it was supposed to stay awake for."""
    blocker = SleepBlocker()
    if not blocker.supported:  # pragma: no cover - platform dependent
        return
    blocker.release()
    blocker.release()
    blocker.acquire()
    try:
        assert blocker.holding, "acquire after stray releases did not take the assertion"
    finally:
        blocker.release()
    assert not blocker.holding


def test_it_is_a_no_op_where_it_is_not_supported(monkeypatch):
    """Linux, Windows, or a macOS without caffeinate on PATH: the
    registry still has to work, silently."""
    blocker = SleepBlocker()
    monkeypatch.setattr(type(blocker), "supported", property(lambda self: False))

    blocker.acquire()
    assert not blocker.holding
    blocker.release()  # must not raise
