"""The backend died on every Windows start, on one attribute.

``BackendSupervisor.install_signal_handlers`` wraps its
``loop.add_signal_handler`` calls in ``contextlib.suppress(NotImplementedError)``
because asyncio's Windows loops do not implement POSIX signals — the docstring
says exactly that, and it is the right guard for the right reason.

It is one step too late for ``SIGUSR1``. That signal is POSIX-only, so on
Windows it is not a value the loop rejects; it is a name the ``signal`` module
does not have. ``signal.SIGUSR1`` raises ``AttributeError`` while the argument
is being evaluated, before ``add_signal_handler`` is entered, and a suppression
around the call cannot see an exception raised while building its arguments.

The effect was total: ``app.run`` called this, the traceback went up through
``asyncio.run``, the process exited, and the onboarding smoke test reported
"backend exited early" on Windows for **eight consecutive scheduled runs** —
against the published package, not a branch. ``SIGTERM`` and ``SIGINT`` exist on
Windows, which is why only the third line failed and why the symptom reads as a
platform problem rather than as a missing ``getattr`` default.

These tests simulate the platform by removing the attribute rather than by
skipping on POSIX, because a test that only runs on Windows is a test that never
runs here.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

from ember_code.backend.supervisor import BackendSupervisor


class _Loop:
    """An event loop stand-in that records what was registered.

    ``raises`` reproduces the Windows behaviour the existing suppression was
    written for: the loop refuses POSIX signal handlers outright.
    """

    def __init__(self, *, raises: bool = False) -> None:
        self.registered: list[int] = []
        self._raises = raises

    def add_signal_handler(self, sig, handler) -> None:  # noqa: ANN001
        if self._raises:
            raise NotImplementedError("Windows event loops do not do POSIX signals")
        self.registered.append(sig)


def _supervisor(loop: _Loop, tmp_path: Path) -> BackendSupervisor:
    return BackendSupervisor(transport=None, loop=loop, project_dir=tmp_path)


@pytest.fixture
def _no_sigusr1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``signal`` module shaped like Windows'."""
    monkeypatch.delattr(signal, "SIGUSR1", raising=False)


class TestWindows:
    def test_installing_handlers_does_not_raise(self, _no_sigusr1, tmp_path):
        """The regression. Before the fix this raised AttributeError and took
        the whole backend down with it."""
        loop = _Loop(raises=True)

        _supervisor(loop, tmp_path).install_signal_handlers()  # must not raise

    def test_the_terminating_signals_are_still_attempted(self, _no_sigusr1, tmp_path):
        """SIGTERM and SIGINT exist on Windows. The point is that the missing
        one is skipped, not that signal handling is abandoned."""
        loop = _Loop()

        _supervisor(loop, tmp_path).install_signal_handlers()

        assert loop.registered == [signal.SIGTERM, signal.SIGINT]

    def test_suppression_alone_would_not_have_saved_it(self, _no_sigusr1):
        """Pins the actual mechanism, because "but it's wrapped in suppress" is
        the reading that let this survive review. The exception comes from
        resolving the name — which happens while the call's arguments are being
        built, outside anything the ``with`` block can catch."""
        import contextlib

        with pytest.raises(AttributeError), contextlib.suppress(NotImplementedError):
            signal.SIGUSR1  # noqa: B018


class TestPosix:
    def test_sigusr1_is_registered_where_it_exists(self, tmp_path):
        """The gc-probe handler is not collateral damage of the fix — on a
        platform that has the signal, it is still installed."""
        if not hasattr(signal, "SIGUSR1"):  # pragma: no cover — POSIX-only run
            pytest.skip("this platform has no SIGUSR1, which the Windows tests cover")
        loop = _Loop()

        _supervisor(loop, tmp_path).install_signal_handlers()

        assert loop.registered == [signal.SIGTERM, signal.SIGINT, signal.SIGUSR1]

    def test_a_loop_that_refuses_signals_is_tolerated(self, tmp_path):
        """The original guard, still doing its job: a loop that raises
        NotImplementedError for every signal leaves startup unharmed."""
        loop = _Loop(raises=True)

        _supervisor(loop, tmp_path).install_signal_handlers()

        assert loop.registered == []


@pytest.fixture(autouse=True)
def _event_loop_for_supervisor():
    """``BackendSupervisor.__init__`` builds an ``asyncio.Event``, which wants a
    running loop policy. Nothing here awaits, so a plain loop is enough."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()
    asyncio.set_event_loop(None)
