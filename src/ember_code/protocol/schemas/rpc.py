"""Process-split RPC contract.

The RPC seam separates the in-process backend from an out-of-process
FE. :class:`RPCRequest` is straightforward. The response was
previously a single :class:`RPCResponse` class carrying
``result: Any + error: str | None`` — the "either ``result`` or
``error``, never both" invariant was un-enforced and callers
relied on convention.

This module replaces that with a Literal-tagged discriminated
union:

* :class:`RPCSuccess` — ``type: "rpc_success"``, always ``result``.
* :class:`RPCFailure` — ``type: "rpc_failure"``, always ``error``.

For wire-compat with existing FE clients that parse the
``"rpc_response"`` discriminator + ``.result`` / ``.error``
fields, :class:`RPCResponse` is preserved as-is (with the same
type literal and permissive semantics). New producer sites should
use :func:`rpc_ok` / :func:`rpc_fail` factory helpers, which
return the appropriate arm and Pydantic serialises them to the
same ``"rpc_response"`` type literal via the ``type`` alias.

TODO — ``RPCRequest.args`` stays ``dict[str, Any]``. Tightening
this would need one Pydantic model per RPC method — deferred
until the method surface is closed.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ember_code.protocol.schemas.envelope import Message


class RPCRequest(Message):
    """Generic RPC call for accessor/utility methods."""

    type: Literal["rpc_request"] = "rpc_request"
    method: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    #: Which extension's frame this came from, if any.
    #:
    #: ``None`` is igni's own UI, and igni's own UI may not call the
    #: ``ext.*`` namespace — an extension reaches its own service and
    #: no other, and without a caller there is nothing to check that
    #: against. The field is optional so a client that predates it
    #: keeps working: it simply cannot make extension calls, which is
    #: what it could do before.
    caller: str | None = None


class RPCResponse(Message):
    """Response to an RPCRequest.

    Carries either ``result`` (success) or ``error`` (failure) —
    never both, but the invariant is by convention. Use the
    :classmethod:`ok` / :classmethod:`fail` factories to construct
    responses safely; they enforce the shape at the callsite.
    """

    type: Literal["rpc_response"] = "rpc_response"
    result: Any = None
    error: str | None = None

    @classmethod
    def ok(cls, request_id: str, result: Any = None) -> RPCResponse:
        """Build a success response. Sets ``error=None``
        explicitly so the invariant is obvious at the callsite."""
        return cls(id=request_id, result=result, error=None)

    @classmethod
    def fail(cls, request_id: str, error: str) -> RPCResponse:
        """Build a failure response. Sets ``result=None``
        explicitly and takes the error message as a required
        positional so a callsite can't accidentally build a
        failure with a falsy error string."""
        return cls(id=request_id, result=None, error=error)


__all__ = [
    "RPCRequest",
    "RPCResponse",
]
