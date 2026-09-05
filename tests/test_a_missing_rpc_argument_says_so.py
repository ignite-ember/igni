"""A missing RPC argument reached the client as a bare key name.

Handlers read their arguments straight out of the dict —
``args["session_id"]`` — so omitting one raises
``KeyError('session_id')``, and ``str()`` of that is ``"'session_id'"``.
The dispatcher passed exactly that to ``RPCResponse.fail``, so the
whole error a caller received was::

    'session_id'

No method name. No indication it was about an argument rather than a
fault inside the call. Found by sweeping all 104 methods: calling
``check_permission`` gave up its three required keys one refusal at a
time — ``tool_args``, then ``tool_name``, then ``func_name`` — each
round a single quoted word.

Two handlers already answered properly, raising their own
``ValueError`` (``run_workflow: 'name' is required``). The wording here
is theirs; this makes it what every handler does by default instead of
what two of them remembered to do.

Narrowed to ``KeyError`` deliberately. A ``KeyError`` raised inside
genuine logic — a lookup miss deep in a handler — now gets described
as a missing argument, which is wrong. That is the price of not
annotating a hundred handlers, and it is a better trade than the
caller being told only ``'url'``: the mislabelled case still carries
the method name and the key, which is strictly more than before.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember_code.backend.message_dispatcher import MessageDispatcher


class _Transport:
    """Collects what the dispatcher sends back."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _Request:
    def __init__(self, method: str, args: dict) -> None:
        self.method = method
        self.args = args


def _dispatcher(transport: _Transport, table: dict) -> MessageDispatcher:
    """A dispatcher with only the pieces ``_on_rpc_request`` touches."""
    dispatcher = MessageDispatcher.__new__(MessageDispatcher)
    dispatcher._transport = transport  # type: ignore[attr-defined]
    dispatcher._rpc_table = table  # type: ignore[attr-defined]
    return dispatcher


class TestTheErrorNamesTheArgument:
    @pytest.mark.asyncio
    async def test_a_missing_key_is_reported_as_a_missing_argument(self):
        transport = _Transport()
        table = {"get_chat_history": lambda args: args["session_id"]}
        dispatcher = _dispatcher(transport, table)

        await dispatcher._on_rpc_request(_Request("get_chat_history", {}), "req-1")

        assert len(transport.sent) == 1
        error = transport.sent[0].error
        assert "get_chat_history" in error, f"the method is not named: {error!r}"
        assert "session_id" in error, f"the argument is not named: {error!r}"
        assert "required" in error, f"it does not say what is wrong: {error!r}"

    @pytest.mark.asyncio
    async def test_the_bare_key_alone_is_no_longer_the_whole_message(self):
        """The regression, stated as the shape it used to have.

        ``"'session_id'"`` was the entire error. Anything that short
        cannot tell a caller what to do.
        """
        transport = _Transport()
        dispatcher = _dispatcher(transport, {"m": lambda args: args["k"]})

        await dispatcher._on_rpc_request(_Request("m", {}), "req-2")

        assert transport.sent[0].error != "'k'"
        assert len(transport.sent[0].error) > len("'k'")

    @pytest.mark.asyncio
    async def test_other_failures_are_still_reported_verbatim(self):
        """Only ``KeyError`` is reinterpreted.

        A handler that raises with a considered message — the
        ``codeindex_install`` refusal explains where to set `api_url`
        and why — must reach the caller unchanged. Wrapping every
        exception in "is a required argument" would have destroyed the
        one class of error already worth reading.
        """
        transport = _Transport()

        def explodes(_args: dict):
            raise ValueError("api_url is not set. Set it in ~/.igni/config.yaml")

        dispatcher = _dispatcher(transport, {"codeindex_install": explodes})

        await dispatcher._on_rpc_request(_Request("codeindex_install", {}), "req-3")

        assert transport.sent[0].error == "api_url is not set. Set it in ~/.igni/config.yaml"

    @pytest.mark.asyncio
    async def test_an_unknown_method_still_says_so(self):
        transport = _Transport()
        dispatcher = _dispatcher(transport, {})

        await dispatcher._on_rpc_request(_Request("nope", {}), "req-4")

        assert "Unknown RPC method" in transport.sent[0].error
