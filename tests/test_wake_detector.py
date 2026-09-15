"""The backend notices that the machine slept.

It did not, before. Local work survives sleep fine — processes are
frozen rather than killed, and exits arrive by SIGCHLD rather than a
timer — but anything with a remote peer does not, because the far end
closes the socket while we are frozen. Nothing knew that had happened,
so the cloud state we held stayed on screen until some later poll
happened to fail.

The detector compares the two clocks rather than asking the OS:
``time.monotonic()` does not advance during sleep and ``time.time()``
does, so the gap between them is the sleep. That works under all four
hosts (Tauri, VS Code, JetBrains, bare CLI) instead of only the one
with a desktop shell to hang an ``NSWorkspaceDidWakeNotification`` on
— and it means these tests need no sleeping, only two numbers.
"""

from __future__ import annotations

import asyncio

from ember_code.backend.wake_detector import WakeDetector


class _FakeClock:
    """Two clocks that move independently, as they do across a sleep."""

    def __init__(self) -> None:
        self._mono = 1000.0
        self._wall = 500_000.0

    def monotonic(self) -> float:
        return self._mono

    def wall(self) -> float:
        return self._wall

    def tick(self, seconds: float) -> None:
        """Time passing with the machine awake: both clocks move."""
        self._mono += seconds
        self._wall += seconds

    def sleep(self, seconds: float) -> None:
        """Time passing with the machine asleep: only the wall clock
        moves. This is the whole premise, and it is what macOS
        actually does."""
        self._wall += seconds


async def test_a_sleep_is_noticed():
    clock = _FakeClock()
    detector = WakeDetector(clock=clock, threshold_seconds=30)
    seen: list[float] = []
    detector.subscribe(lambda slept: _record(seen, slept))

    wall, mono = clock.wall(), clock.monotonic()
    clock.tick(5)  # the poll interval, awake
    clock.sleep(3600)  # an hour with the lid shut

    slept = await detector.check_once(5, wall, mono)

    assert round(slept) == 3600
    assert seen and round(seen[0]) == 3600


async def test_ordinary_time_passing_is_not_a_wake():
    """Both clocks moving together is just the loop running."""
    clock = _FakeClock()
    detector = WakeDetector(clock=clock, threshold_seconds=30)
    seen: list[float] = []
    detector.subscribe(lambda slept: _record(seen, slept))

    wall, mono = clock.wall(), clock.monotonic()
    clock.tick(5)

    assert await detector.check_once(5, wall, mono) == 0.0
    assert not seen


async def test_a_late_loop_is_not_a_wake():
    """A busy event loop, a long GC pause, a debugger breakpoint — the
    tick runs late but the machine was awake throughout, and waking
    subsystems for that would make this a nuisance rather than a
    signal."""
    clock = _FakeClock()
    detector = WakeDetector(clock=clock, threshold_seconds=30)
    seen: list[float] = []
    detector.subscribe(lambda slept: _record(seen, slept))

    wall, mono = clock.wall(), clock.monotonic()
    clock.tick(12)  # twelve seconds late, both clocks agree

    assert await detector.check_once(5, wall, mono) == 0.0
    assert not seen


async def test_a_short_suspend_is_below_the_threshold():
    """Thirty seconds is the line. A two-second suspend is not worth
    dropping cached state for."""
    clock = _FakeClock()
    detector = WakeDetector(clock=clock, threshold_seconds=30)

    wall, mono = clock.wall(), clock.monotonic()
    clock.tick(5)
    clock.sleep(2)

    assert await detector.check_once(5, wall, mono) == 0.0


async def test_one_bad_subscriber_does_not_rob_the_others():
    """A wake fans out to everything that cares. A subsystem that
    throws on wake must not cost the rest their notification — this is
    the backend's boot path, and a wake handler is not important
    enough to take it down."""
    clock = _FakeClock()
    detector = WakeDetector(clock=clock, threshold_seconds=30)
    seen: list[float] = []

    async def _explode(_slept: float) -> None:
        raise RuntimeError("this subscriber is broken")

    detector.subscribe(_explode)
    detector.subscribe(lambda slept: _record(seen, slept))

    wall, mono = clock.wall(), clock.monotonic()
    clock.tick(5)
    clock.sleep(120)

    assert round(await detector.check_once(5, wall, mono)) == 120
    assert seen, "the second subscriber never ran"


async def test_the_loop_starts_and_stops_cleanly():
    """Shutdown must not leave the task pending or raise through
    cancellation."""
    detector = WakeDetector(poll_seconds=0.01, clock=_FakeClock())
    task = detector.start()
    await asyncio.sleep(0.05)

    await detector.stop()

    assert task.done()


async def _record(sink: list[float], slept: float) -> None:
    sink.append(slept)
