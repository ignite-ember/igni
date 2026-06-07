"""Tests for the plugin-related wrapper methods on
:class:`BackendClient`.

These are 1-line forwards to ``self._rpc(RpcMethod.X, **args)``, so
the test surface is narrow: the right ``RpcMethod`` enum value goes
out, the right keyword args are forwarded, and the return value
flows back unchanged (with ``msg.Info`` boxing for the action
methods that may return raw text).

Worth covering because a single-letter typo in the enum or arg name
would ship silently — the slash-command / backend tests don't catch
it since they don't go through the RPC layer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from ember_code.frontend.tui.backend_client import BackendClient
from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod

# ── Helpers ─────────────────────────────────────────────────────────


def _make_client_with_mock_rpc() -> tuple[BackendClient, AsyncMock]:
    """Construct a BackendClient that doesn't open a real socket and
    whose ``_rpc`` is a recording mock. Returns (client, rpc_mock)."""
    # __init__ opens transport machinery we don't need — bypass it.
    client = BackendClient.__new__(BackendClient)
    rpc = AsyncMock()
    client._rpc = rpc  # type: ignore[assignment]
    return client, rpc


def _run(coro):
    return asyncio.run(coro)


# ── Forwarding contract ─────────────────────────────────────────────


def test_get_plugin_details_forwards_to_rpc() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = [{"name": "alpha"}]
    result = _run(client.get_plugin_details())
    rpc.assert_awaited_once_with(RpcMethod.GET_PLUGIN_DETAILS)
    assert result == [{"name": "alpha"}]


def test_get_plugin_details_returns_empty_when_rpc_returns_none() -> None:
    """``_rpc`` can return ``None`` on transport hiccup; the wrapper
    normalizes to ``[]`` so callers don't need to None-check."""
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = None
    assert _run(client.get_plugin_details()) == []


def test_set_plugin_enabled_forwards_args() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="ok")
    result = _run(client.set_plugin_enabled("foo", False))
    rpc.assert_awaited_once_with(
        RpcMethod.SET_PLUGIN_ENABLED,
        name="foo",
        enabled=False,
    )
    assert isinstance(result, msg.Info)


def test_set_plugin_enabled_wraps_non_info() -> None:
    """If the backend returns a plain string (older protocol or
    serialization quirk), the wrapper boxes it in ``msg.Info`` so
    callers see a consistent type."""
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = "raw text"
    result = _run(client.set_plugin_enabled("foo", True))
    assert isinstance(result, msg.Info)
    assert result.text == "raw text"


def test_install_plugin_forwards_ref_and_install_ref() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="installed foo")
    _run(client.install_plugin("https://x/y.git", install_ref="v1.2.0"))
    rpc.assert_awaited_once_with(
        RpcMethod.INSTALL_PLUGIN,
        ref="https://x/y.git",
        install_ref="v1.2.0",
    )


def test_install_plugin_default_install_ref_none() -> None:
    """No ``install_ref`` means the wrapper forwards ``None`` —
    backend distinguishes "no pin requested" from "empty pin"."""
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="ok")
    _run(client.install_plugin("https://x/y.git"))
    rpc.assert_awaited_once_with(
        RpcMethod.INSTALL_PLUGIN,
        ref="https://x/y.git",
        install_ref=None,
    )


def test_update_plugin_forwards_args() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="updated")
    _run(client.update_plugin("foo", install_ref="dev"))
    rpc.assert_awaited_once_with(
        RpcMethod.UPDATE_PLUGIN,
        name="foo",
        install_ref="dev",
    )


def test_remove_plugin_forwards_name() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="removed")
    _run(client.remove_plugin("foo"))
    rpc.assert_awaited_once_with(RpcMethod.REMOVE_PLUGIN, name="foo")


def test_get_marketplaces_forwards() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = [{"name": "m1"}]
    result = _run(client.get_marketplaces())
    rpc.assert_awaited_once_with(RpcMethod.GET_MARKETPLACES)
    assert result == [{"name": "m1"}]


def test_get_marketplaces_normalizes_none_to_empty_list() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = None
    assert _run(client.get_marketplaces()) == []


def test_add_marketplace_forwards_url() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="added")
    _run(client.add_marketplace("https://m/k.git"))
    rpc.assert_awaited_once_with(
        RpcMethod.ADD_MARKETPLACE,
        url="https://m/k.git",
    )


def test_remove_marketplace_forwards_name() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="removed")
    _run(client.remove_marketplace("m1"))
    rpc.assert_awaited_once_with(
        RpcMethod.REMOVE_MARKETPLACE,
        name="m1",
    )


def test_refresh_marketplaces_named() -> None:
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="refreshed")
    _run(client.refresh_marketplaces("m1"))
    rpc.assert_awaited_once_with(
        RpcMethod.REFRESH_MARKETPLACES,
        name="m1",
    )


def test_refresh_marketplaces_all() -> None:
    """``refresh_marketplaces()`` with no name forwards ``name=None``
    — the backend differentiates "refresh one" from "refresh all"."""
    client, rpc = _make_client_with_mock_rpc()
    rpc.return_value = msg.Info(text="refreshed all")
    _run(client.refresh_marketplaces())
    rpc.assert_awaited_once_with(
        RpcMethod.REFRESH_MARKETPLACES,
        name=None,
    )


# ── Return-shape robustness ────────────────────────────────────────


def test_all_info_returning_wrappers_box_non_info() -> None:
    """Every wrapper that returns ``msg.Info`` should box raw text
    when the backend returns a string instead of an Info instance.
    A single regression in any wrapper would cause the TUI to print
    ``Info(text=...)`` instead of the actual message."""
    cases = [
        ("set_plugin_enabled", ["foo", True]),
        ("install_plugin", ["https://x.git"]),
        ("update_plugin", ["foo"]),
        ("remove_plugin", ["foo"]),
        ("add_marketplace", ["https://m.git"]),
        ("remove_marketplace", ["m1"]),
        ("refresh_marketplaces", []),
    ]
    for method_name, args in cases:
        client, rpc = _make_client_with_mock_rpc()
        rpc.return_value = "raw text"
        result = _run(getattr(client, method_name)(*args))
        assert isinstance(result, msg.Info), f"{method_name} did not box raw text into Info"
        assert result.text == "raw text", method_name
