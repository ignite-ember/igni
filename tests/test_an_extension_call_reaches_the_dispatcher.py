"""The ``ext.*`` namespace at the dispatch seam.

``tests/test_an_extension_can_call_its_own_service.py`` covers the
decision. This covers the wiring either side of it: that an ``ext.*``
method never reaches the base table, that a refusal comes back as a
failed ``RPCResponse`` rather than an exception out of the receive
loop, and that a backend built without extension support says so
instead of ``Unknown RPC method``.

Worth its own file because the bug being prevented is a routing one.
``Unknown RPC method`` is what the old path said about every
extension call, and it is indistinguishable from a typo — which is
why the gap went unnoticed long enough to be documented as a
limitation in three separate places.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ember_code.backend.message_dispatcher import MessageDispatcher
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "examples" / "reference-extension" / "manifest.json"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _Request:
    def __init__(self, method: str, args: dict, caller: str | None = None) -> None:
        self.method = method
        self.args = args
        self.caller = caller


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def offers(self, tool: str) -> bool:
        return True

    async def call(self, method: str, args: dict) -> Any:
        self.calls.append((method, args))
        return {"ok": True, "method": method}


def _dispatcher(
    transport: _Transport, table: dict, extensions: ExtensionRpc | None
) -> MessageDispatcher:
    """A dispatcher with only the pieces the RPC path touches."""
    dispatcher = MessageDispatcher.__new__(MessageDispatcher)
    dispatcher._transport = transport  # type: ignore[attr-defined]
    dispatcher._rpc_table = table  # type: ignore[attr-defined]
    dispatcher._extensions = extensions  # type: ignore[attr-defined]
    return dispatcher


@pytest.fixture
def extensions() -> tuple[ExtensionRpc, _Service]:
    registry = ExtensionRegistry()
    registry.install(ExtensionManifest.load(REFERENCE), state=ExtensionState.READY)
    rpc = ExtensionRpc(registry)
    service = _Service()
    rpc.bind("acme.scanner", service)
    return rpc, service


@pytest.mark.asyncio
async def test_a_granted_call_comes_back_as_a_result(
    extensions: tuple[ExtensionRpc, _Service],
) -> None:
    rpc, service = extensions
    transport = _Transport()
    dispatcher = _dispatcher(transport, {}, rpc)

    await dispatcher._on_rpc_request(
        _Request("ext.acme.scanner.list", {"severity": "high"}, caller="acme.scanner"),
        "req-1",
    )

    (response,) = transport.sent
    assert response.error is None
    assert response.result == {"ok": True, "method": "list"}
    assert service.calls == [("list", {"severity": "high"})]


@pytest.mark.asyncio
async def test_a_refusal_comes_back_as_a_failure_not_an_exception(
    extensions: tuple[ExtensionRpc, _Service],
) -> None:
    rpc, service = extensions
    transport = _Transport()
    dispatcher = _dispatcher(transport, {}, rpc)

    await dispatcher._on_rpc_request(_Request("ext.acme.scanner.list", {}, caller=None), "req-2")

    (response,) = transport.sent
    assert response.error is not None
    assert "did not say which extension" in response.error
    assert service.calls == []


@pytest.mark.asyncio
async def test_an_extension_method_never_reaches_the_base_table(
    extensions: tuple[ExtensionRpc, _Service],
) -> None:
    """A base handler shadowing an extension's namespace would be the
    worst shape a routing bug can take: an extension silently served
    by igni's own code."""
    rpc, _ = extensions
    reached = []
    table = {"ext.acme.scanner.list": lambda args: reached.append(args)}
    transport = _Transport()
    dispatcher = _dispatcher(transport, table, rpc)

    await dispatcher._on_rpc_request(
        _Request("ext.acme.scanner.list", {}, caller="acme.scanner"), "req-3"
    )

    assert reached == []
    assert transport.sent[0].error is None


@pytest.mark.asyncio
async def test_a_malformed_extension_name_is_not_an_unknown_method(
    extensions: tuple[ExtensionRpc, _Service],
) -> None:
    rpc, _ = extensions
    transport = _Transport()
    dispatcher = _dispatcher(transport, {}, rpc)

    await dispatcher._on_rpc_request(_Request("ext.acme", {}, caller="acme.scanner"), "req-4")

    (response,) = transport.sent
    assert "Unknown RPC method" not in (response.error or "")
    assert "is not an extension method" in response.error


@pytest.mark.asyncio
async def test_a_backend_without_extension_support_says_so() -> None:
    """Not every backend has a registry. Falling through to the base
    table would produce ``Unknown RPC method``, which says nothing
    about why."""
    transport = _Transport()
    dispatcher = _dispatcher(transport, {}, None)

    await dispatcher._on_rpc_request(
        _Request("ext.acme.scanner.list", {}, caller="acme.scanner"), "req-5"
    )

    (response,) = transport.sent
    assert "no extension support" in response.error


@pytest.mark.asyncio
async def test_a_base_method_is_unaffected(
    extensions: tuple[ExtensionRpc, _Service],
) -> None:
    """The namespace check runs first, so this is the guard that it
    does not swallow everything else on the way past."""
    rpc, _ = extensions
    transport = _Transport()
    dispatcher = _dispatcher(transport, {"get_status": lambda args: "fine"}, rpc)

    await dispatcher._on_rpc_request(_Request("get_status", {}), "req-6")

    (response,) = transport.sent
    assert response.result == "fine"


@pytest.mark.asyncio
async def test_a_request_without_a_caller_field_is_tolerated(
    extensions: tuple[ExtensionRpc, _Service],
) -> None:
    """``caller`` is a new optional field, so an older client's
    request object may not carry it at all. That must be a refusal
    with a reason, not an ``AttributeError`` out of the receive loop.
    """
    rpc, _ = extensions
    transport = _Transport()
    dispatcher = _dispatcher(transport, {}, rpc)

    class _Old:
        method = "ext.acme.scanner.list"
        args: dict = {}

    await dispatcher._on_rpc_request(_Old(), "req-7")

    (response,) = transport.sent
    assert "did not say which extension" in response.error
