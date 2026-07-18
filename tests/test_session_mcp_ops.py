"""Unit tests for ``session/mcp_ops.py``.

Extracted in iter 139. Session integration coverage lives in
``test_plugins_session_integration.py``; these tests pin the
coordinator contract in isolation — most importantly the
"sequential iteration" invariant (parallel would race MCP
handshakes / stack N modal approval prompts) and the
"rebuild_mcp only when something actually connected /
disconnected" optimisation.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.mcp.schemas import MCPConnectResult
from ember_code.core.session.mcp_ops import (
    McpLifecycleCoordinator,
    McpLifecycleDeps,
)


def _deps():
    """Build an :class:`McpLifecycleDeps` stub carrying only what
    the coordinator reads: a fake ``mcp_manager`` with
    ``disconnect_one`` / ``connect`` async methods and a
    :class:`MagicMock` ``rebuild`` callable so tests can assert on
    call count."""
    mcp_manager = SimpleNamespace()
    mcp_manager.disconnect_one = AsyncMock(return_value=True)
    mcp_manager.connect = AsyncMock(
        return_value=MCPConnectResult.success(MagicMock(functions={"t1": lambda: None}))
    )
    rebuild = MagicMock()
    return McpLifecycleDeps(mcp_manager=mcp_manager, rebuild=rebuild)


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnects_each_server(self):
        deps = _deps()
        coord = McpLifecycleCoordinator(deps)
        await coord.disconnect({"srv-a", "srv-b"})
        # Sequential iteration through both.
        assert deps.mcp_manager.disconnect_one.call_count == 2
        # ``rebuild`` fired because at least one disconnect succeeded.
        deps.rebuild.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_rebuild_when_nothing_actually_disconnected(self):
        # ``disconnect_one`` returns False when the server wasn't
        # connected in the first place — the config-removal branch.
        # No rebuild needed since the tool surface didn't change.
        deps = _deps()
        deps.mcp_manager.disconnect_one = AsyncMock(return_value=False)
        coord = McpLifecycleCoordinator(deps)
        await coord.disconnect({"srv-never-connected"})
        deps.rebuild.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_one_does_not_stop_others(self):
        # A crash disconnecting srv-a shouldn't prevent srv-b from
        # being cleaned up — best-effort cleanup.
        deps = _deps()

        async def _flaky(name):
            if name == "srv-a":
                raise RuntimeError("boom")
            return True

        deps.mcp_manager.disconnect_one = AsyncMock(side_effect=_flaky)
        coord = McpLifecycleCoordinator(deps)
        # No raise even though srv-a crashes.
        await coord.disconnect({"srv-a", "srv-b"})
        # Still fired at rebuild for srv-b's successful disconnect.
        deps.rebuild.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_set_is_noop(self):
        deps = _deps()
        coord = McpLifecycleCoordinator(deps)
        await coord.disconnect(set())
        deps.mcp_manager.disconnect_one.assert_not_called()
        deps.rebuild.assert_not_called()

    @pytest.mark.asyncio
    async def test_iteration_is_sorted(self):
        # Sorted iteration keeps the debug logs stable — makes it
        # easier to reproduce timing bugs from log traces.
        deps = _deps()
        called_order: list[str] = []

        async def _record(name):
            called_order.append(name)
            return True

        deps.mcp_manager.disconnect_one = AsyncMock(side_effect=_record)
        coord = McpLifecycleCoordinator(deps)
        await coord.disconnect({"c", "a", "b"})
        assert called_order == ["a", "b", "c"]


class TestConnect:
    @pytest.mark.asyncio
    async def test_connects_each_server(self):
        deps = _deps()
        coord = McpLifecycleCoordinator(deps)
        await coord.connect({"srv-a", "srv-b"})
        assert deps.mcp_manager.connect.call_count == 2
        deps.rebuild.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_rebuild_when_all_connections_failed(self):
        # If every connect returned an ``ok=False`` Result (denied /
        # policy blocked / transport error), skip the rebuild — the
        # tool surface didn't gain anything.
        deps = _deps()
        deps.mcp_manager.connect = AsyncMock(return_value=MCPConnectResult.failure("denied"))
        coord = McpLifecycleCoordinator(deps)
        await coord.connect({"srv-a"})
        deps.rebuild.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_one_does_not_stop_others(self):
        deps = _deps()
        client = MagicMock(functions={"t1": lambda: None})

        async def _flaky(name):
            if name == "srv-a":
                raise RuntimeError("boom")
            return MCPConnectResult.success(client)

        deps.mcp_manager.connect = AsyncMock(side_effect=_flaky)
        coord = McpLifecycleCoordinator(deps)
        await coord.connect({"srv-a", "srv-b"})
        # srv-b succeeded → rebuild.
        deps.rebuild.assert_called_once()

    @pytest.mark.asyncio
    async def test_sequential_not_parallel(self):
        # Sequential is a required invariant: first-use approval
        # is a modal UI element; parallel connect would stack N
        # dialogs on the user simultaneously.
        deps = _deps()
        in_flight = 0
        max_in_flight = 0

        async def _tracking(name):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            # Cheap await to force cooperative scheduling.
            import asyncio as _asyncio

            await _asyncio.sleep(0)
            in_flight -= 1
            return MCPConnectResult.success(MagicMock(functions={"t1": lambda: None}))

        deps.mcp_manager.connect = AsyncMock(side_effect=_tracking)
        coord = McpLifecycleCoordinator(deps)
        await coord.connect({"a", "b", "c"})
        # Never more than one connect in flight at a time.
        assert max_in_flight == 1
