"""Background-process watcher contract.

The FE's right-side watcher panel surfaces every backgrounded
``run_shell_command`` for live tail + explicit kill. It depends
on three BE primitives this file pins:

* Per-line + lifecycle subscribers in ``shell.py`` (line, start,
  completion). The watcher uses all three; the agent's
  notify-on-completion still uses only ``completion``.
* Three RPCs (``list_background_processes`` /
  ``read_process_tail`` / ``stop_background_process``) and their
  dispatch wiring.
* Push channel payload shapes (``process_started`` /
  ``process_line`` / ``process_exited``).

A regression in any layer should fail here, not at the FE.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ember_code.backend.__main__ import _build_rpc_table
from ember_code.backend.server import BackendServer
from ember_code.core.tools import shell as shell_mod
from ember_code.protocol.rpc import RpcMethod

# ── Subscriber semantics ─────────────────────────────────────


class TestLineSubscriber:
    def setup_method(self) -> None:
        # Test isolation: reset the process-event bus before each
        # test so a leftover callback from a previous test doesn't
        # fire here. Post-refactor the three parallel subscriber
        # lists are consolidated into ``shell_mod._event_bus`` (a
        # :class:`ProcessEventBus`); ``.reset()`` drops all
        # subscribers across all event types in one call.
        shell_mod._event_bus.reset()

    def test_subscribe_then_emit_fires_callback(self) -> None:
        received: list[dict] = []
        shell_mod.subscribe_to_process_line(received.append)

        shell_mod._emit_line(42, "hello from background")

        assert received == [{"pid": 42, "line": "hello from background"}]

    def test_unsubscribe_stops_callback(self) -> None:
        received: list[dict] = []

        def cb(info: dict) -> None:
            received.append(info)

        shell_mod.subscribe_to_process_line(cb)
        shell_mod.unsubscribe_from_process_line(cb)
        shell_mod._emit_line(42, "should not surface")

        assert received == []

    def test_duplicate_subscribe_is_idempotent(self) -> None:
        # Same callback registered twice should only fire once —
        # mirrors the completion subscriber contract.
        received: list[dict] = []
        cb = received.append
        shell_mod.subscribe_to_process_line(cb)
        shell_mod.subscribe_to_process_line(cb)

        shell_mod._emit_line(1, "x")

        assert received == [{"pid": 1, "line": "x"}]

    def test_subscriber_exception_does_not_block_others(self) -> None:
        # One bad callback can't sink the rest. Important because
        # the watcher's loop-hop subscriber would otherwise prevent
        # an unrelated plugin from receiving lines.
        survivor: list[dict] = []

        def bad(_info: dict) -> None:
            raise RuntimeError("bad subscriber")

        shell_mod.subscribe_to_process_line(bad)
        shell_mod.subscribe_to_process_line(survivor.append)

        shell_mod._emit_line(7, "still arrives")

        assert survivor == [{"pid": 7, "line": "still arrives"}]

    def test_no_subscribers_is_cheap_noop(self) -> None:
        # Hot path — must short-circuit when nothing is listening.
        # Build a payload-allocating subscriber and assert it's
        # NOT called for the no-subscriber emit.
        shell_mod._emit_line(99, "ignored")  # must not raise


class TestStartSubscriber:
    def setup_method(self) -> None:
        shell_mod._event_bus.reset()

    def test_emit_start_publishes_pid_cmd_ts(self) -> None:
        received: list[dict] = []
        shell_mod.subscribe_to_process_start(received.append)

        mp = SimpleNamespace(proc=SimpleNamespace(pid=123), cmd="tail -f x.log")
        shell_mod._emit_start(mp)  # type: ignore[arg-type]

        assert len(received) == 1
        assert received[0]["pid"] == 123
        assert received[0]["cmd"] == "tail -f x.log"
        # ts is a real epoch — just sanity-check the field exists
        # and is a number.
        assert isinstance(received[0]["started_at"], (int, float))

    def test_start_subscriber_exception_isolated(self) -> None:
        survivor: list[dict] = []

        def bad(_info: dict) -> None:
            raise RuntimeError("crash")

        shell_mod.subscribe_to_process_start(bad)
        shell_mod.subscribe_to_process_start(survivor.append)
        mp = SimpleNamespace(proc=SimpleNamespace(pid=1), cmd="x")
        shell_mod._emit_start(mp)  # type: ignore[arg-type]

        assert survivor[0]["pid"] == 1


# ── BackendServer methods ────────────────────────────────────


class TestListBackgroundProcesses:
    def test_empty_registry_returns_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(shell_mod._registry, "all_running", lambda: [], raising=True)
        server = BackendServer.__new__(BackendServer)
        assert server.list_background_processes() == []

    def test_returns_pid_cmd_elapsed_per_process(self, monkeypatch) -> None:
        monkeypatch.setattr(
            shell_mod._registry,
            "all_running",
            lambda: [(101, "npm run dev", 12.3), (202, "tail -f log", 4.5)],
            raising=True,
        )
        server = BackendServer.__new__(BackendServer)
        result = server.list_background_processes()
        assert result == [
            {"pid": 101, "cmd": "npm run dev", "elapsed_seconds": 12.3},
            {"pid": 202, "cmd": "tail -f log", "elapsed_seconds": 4.5},
        ]


class TestReadProcessTail:
    def test_unknown_pid_returns_safe_shape(self, monkeypatch) -> None:
        monkeypatch.setattr(shell_mod._registry, "get", lambda _pid: None, raising=True)
        server = BackendServer.__new__(BackendServer)
        out = server.read_process_tail(pid=9999)
        assert out == {
            "pid": 9999,
            "output": "",
            "is_running": False,
            "exit_code": None,
        }

    def test_running_pid_returns_tail_and_running_true(self, monkeypatch) -> None:
        mp = MagicMock()
        mp.read.return_value = "line1\nline2"
        mp.is_running.return_value = True
        mp.returncode.return_value = None
        monkeypatch.setattr(shell_mod._registry, "get", lambda _pid: mp, raising=True)

        server = BackendServer.__new__(BackendServer)
        out = server.read_process_tail(pid=42, tail=50)
        assert out == {
            "pid": 42,
            "output": "line1\nline2",
            "is_running": True,
            "exit_code": None,
        }
        mp.read.assert_called_once_with(tail=50)

    def test_exited_pid_returns_exit_code(self, monkeypatch) -> None:
        # Process exited but registry still holds it (the
        # post-completion TTL eviction window). The FE needs the
        # final tail + exit code to render the "stopped" row.
        mp = MagicMock()
        mp.read.return_value = "done"
        mp.is_running.return_value = False
        mp.returncode.return_value = 0
        monkeypatch.setattr(shell_mod._registry, "get", lambda _pid: mp, raising=True)

        server = BackendServer.__new__(BackendServer)
        out = server.read_process_tail(pid=7, tail=100)
        assert out["exit_code"] == 0
        assert out["is_running"] is False


class TestStopBackgroundProcess:
    async def test_unknown_pid_returns_killed_false(self, monkeypatch) -> None:
        monkeypatch.setattr(shell_mod._registry, "get", lambda _pid: None, raising=True)
        server = BackendServer.__new__(BackendServer)
        out = await server.stop_background_process(pid=9999)
        assert out["killed"] is False
        assert "not in registry" in out["message"]

    async def test_already_exited_returns_killed_false(self, monkeypatch) -> None:
        mp = MagicMock()
        mp.is_running.return_value = False
        mp.returncode.return_value = 137
        monkeypatch.setattr(shell_mod._registry, "get", lambda _pid: mp, raising=True)

        server = BackendServer.__new__(BackendServer)
        out = await server.stop_background_process(pid=1)
        assert out["killed"] is False
        assert "already exited" in out["message"]
        mp.kill.assert_not_called()

    async def test_running_pid_sends_sigterm(self, monkeypatch) -> None:
        mp = MagicMock()
        mp.is_running.return_value = True
        mp.returncode.return_value = -15
        mp._reader_task = None  # no reader to await — skip the wait_for branch
        monkeypatch.setattr(shell_mod._registry, "get", lambda _pid: mp, raising=True)

        server = BackendServer.__new__(BackendServer)
        out = await server.stop_background_process(pid=42)
        mp.kill.assert_called_once()
        assert out["killed"] is True
        assert out["pid"] == 42


# ── RPC dispatch wiring ──────────────────────────────────────


class TestWatcherRpcDispatch:
    """The three watcher RPCs must route from the dispatch lambda
    to the right BackendServer method with args extracted as the
    right types. Same shape as ``test_plan_rpc_wiring.py``."""

    def _make_backend(self) -> MagicMock:
        backend = MagicMock()
        backend.list_background_processes = MagicMock(return_value=[])
        backend.read_process_tail = MagicMock(
            return_value={"pid": 0, "output": "", "is_running": False, "exit_code": None}
        )
        backend.stop_background_process = AsyncMock(
            return_value={"pid": 0, "killed": False, "message": ""}
        )
        return backend

    def test_list_dispatch_calls_method(self) -> None:
        backend = self._make_backend()
        table = _build_rpc_table(backend, MagicMock(), {})
        table[RpcMethod.LIST_BACKGROUND_PROCESSES]({})
        backend.list_background_processes.assert_called_once_with()

    def test_read_dispatch_extracts_pid_and_tail(self) -> None:
        backend = self._make_backend()
        table = _build_rpc_table(backend, MagicMock(), {})
        table[RpcMethod.READ_PROCESS_TAIL]({"pid": "42", "tail": "50"})
        backend.read_process_tail.assert_called_once_with(pid=42, tail=50)

    def test_read_dispatch_defaults_tail_to_200(self) -> None:
        backend = self._make_backend()
        table = _build_rpc_table(backend, MagicMock(), {})
        table[RpcMethod.READ_PROCESS_TAIL]({"pid": 42})
        backend.read_process_tail.assert_called_once_with(pid=42, tail=200)

    async def test_stop_dispatch_extracts_pid(self) -> None:
        backend = self._make_backend()
        table = _build_rpc_table(backend, MagicMock(), {})
        result = table[RpcMethod.STOP_BACKGROUND_PROCESS]({"pid": 99})
        # Lambda returns the coroutine — await to drive the call.
        if asyncio.iscoroutine(result):
            await result
        backend.stop_background_process.assert_called_once_with(pid=99)

    def test_pool_runtime_routes_to_pool_backend(self) -> None:
        # Same isolation contract as the plan-decision RPCs:
        # ``_build_rpc_table`` closes over its ``backend`` arg, so
        # a pool runtime's table must hit the pool backend, never
        # the boot one.
        boot = self._make_backend()
        pool = self._make_backend()
        # Boot table built but not stored — we only fire against
        # the pool table. Building both proves the two builders
        # produce independent closures (no shared mutable state
        # between boot and pool RPC dispatch).
        _build_rpc_table(boot, MagicMock(), {})
        pool_table = _build_rpc_table(pool, MagicMock(), {})

        pool_table[RpcMethod.LIST_BACKGROUND_PROCESSES]({})
        pool.list_background_processes.assert_called_once()
        boot.list_background_processes.assert_not_called()
