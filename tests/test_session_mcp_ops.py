"""Unit tests for ``session/mcp_ops.py``.

Extracted in iter 139. Session integration coverage lives in
``test_plugins_session_integration.py``; these tests pin the
free-function contract in isolation — most importantly the
"sequential iteration" invariant (parallel would race MCP
handshakes / stack N modal approval prompts) and the
"rebuild_mcp only when something actually connected /
disconnected" optimisation.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.session.mcp_ops import auto_connect_mcps, disconnect_removed_mcps


def _bare_session():
    """Session-shaped stub carrying only what mcp_ops reads."""
    session = SimpleNamespace()
    session.mcp_manager = SimpleNamespace()
    session.mcp_manager.disconnect_one = AsyncMock(return_value=True)
    session.mcp_manager.connect = AsyncMock(return_value=MagicMock(functions={"t1": lambda: None}))
    session.rebuild_mcp = MagicMock()
    return session


class TestDisconnectRemovedMcps:
    @pytest.mark.asyncio
    async def test_disconnects_each_server(self):
        s = _bare_session()
        await disconnect_removed_mcps(s, {"srv-a", "srv-b"})
        # Sequential iteration through both.
        assert s.mcp_manager.disconnect_one.call_count == 2
        # ``rebuild_mcp`` fired because at least one disconnect succeeded.
        s.rebuild_mcp.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_rebuild_when_nothing_actually_disconnected(self):
        # ``disconnect_one`` returns False when the server wasn't
        # connected in the first place — the config-removal branch.
        # No rebuild needed since the tool surface didn't change.
        s = _bare_session()
        s.mcp_manager.disconnect_one = AsyncMock(return_value=False)
        await disconnect_removed_mcps(s, {"srv-never-connected"})
        s.rebuild_mcp.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_one_does_not_stop_others(self):
        # A crash disconnecting srv-a shouldn't prevent srv-b from
        # being cleaned up — best-effort cleanup.
        s = _bare_session()

        async def _flaky(name):
            if name == "srv-a":
                raise RuntimeError("boom")
            return True

        s.mcp_manager.disconnect_one = AsyncMock(side_effect=_flaky)
        # No raise even though srv-a crashes.
        await disconnect_removed_mcps(s, {"srv-a", "srv-b"})
        # Still fired at rebuild for srv-b's successful disconnect.
        s.rebuild_mcp.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_set_is_noop(self):
        s = _bare_session()
        await disconnect_removed_mcps(s, set())
        s.mcp_manager.disconnect_one.assert_not_called()
        s.rebuild_mcp.assert_not_called()

    @pytest.mark.asyncio
    async def test_iteration_is_sorted(self):
        # Sorted iteration keeps the debug logs stable — makes it
        # easier to reproduce timing bugs from log traces.
        s = _bare_session()
        called_order: list[str] = []

        async def _record(name):
            called_order.append(name)
            return True

        s.mcp_manager.disconnect_one = AsyncMock(side_effect=_record)
        await disconnect_removed_mcps(s, {"c", "a", "b"})
        assert called_order == ["a", "b", "c"]


class TestAutoConnectMcps:
    @pytest.mark.asyncio
    async def test_connects_each_server(self):
        s = _bare_session()
        await auto_connect_mcps(s, {"srv-a", "srv-b"})
        assert s.mcp_manager.connect.call_count == 2
        s.rebuild_mcp.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_rebuild_when_all_connections_failed(self):
        # If every connect returned None (denied / policy blocked /
        # transport error), skip the rebuild — the tool surface
        # didn't gain anything.
        s = _bare_session()
        s.mcp_manager.connect = AsyncMock(return_value=None)
        await auto_connect_mcps(s, {"srv-a"})
        s.rebuild_mcp.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_one_does_not_stop_others(self):
        s = _bare_session()
        client = MagicMock(functions={"t1": lambda: None})

        async def _flaky(name):
            if name == "srv-a":
                raise RuntimeError("boom")
            return client

        s.mcp_manager.connect = AsyncMock(side_effect=_flaky)
        await auto_connect_mcps(s, {"srv-a", "srv-b"})
        # srv-b succeeded → rebuild.
        s.rebuild_mcp.assert_called_once()

    @pytest.mark.asyncio
    async def test_sequential_not_parallel(self):
        # Sequential is a required invariant: first-use approval
        # is a modal UI element; parallel connect would stack N
        # dialogs on the user simultaneously.
        s = _bare_session()
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
            return MagicMock(functions={})

        s.mcp_manager.connect = AsyncMock(side_effect=_tracking)
        await auto_connect_mcps(s, {"a", "b", "c"})
        # Never more than one connect in flight at a time.
        assert max_in_flight == 1
