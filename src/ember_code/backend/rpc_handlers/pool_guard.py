"""Pool-level RPC guard handler.

The pool-level RPCs (``attach_session``, ``{get,set,delete}_client_state``)
are intercepted by :class:`SessionOrchestrator` BEFORE per-runtime
dispatch. If any code path skips that guard and dispatches to these
methods through the RPC table, the guard raises a loud
:class:`RuntimeError` rather than silently succeeding.

Replaces four identical ``_pool_*_stub`` methods on the old router
with one guard method registered under all four keys — collapsing the
duplication while preserving the defensive raise.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ember_code.backend.rpc_handlers.base import RpcHandler
from ember_code.protocol.rpc import RpcMethod

# Frozen so callers (session_orchestrator + rpc_router) can `in`-check
# against it without worrying about accidental mutation.
POOL_LEVEL_RPCS: frozenset[RpcMethod] = frozenset(
    {
        RpcMethod.ATTACH_SESSION,
        RpcMethod.GET_CLIENT_STATE,
        RpcMethod.SET_CLIENT_STATE,
        RpcMethod.DELETE_CLIENT_STATE,
    }
)


class PoolGuardRpcHandler(RpcHandler):
    """Single ``_pool_level_guard`` method registered under every
    pool-level RPC key. Preserves the "these must never reach here"
    invariant with one method instead of four."""

    def methods(self) -> dict[RpcMethod, Callable[[dict], Any]]:
        return {method: self._pool_level_guard for method in POOL_LEVEL_RPCS}

    def _pool_level_guard(self, args: dict) -> None:
        raise RuntimeError(
            "pool-level RPC dispatched through per-runtime table — "
            "SessionOrchestrator must intercept ATTACH_SESSION + "
            "{GET,SET,DELETE}_CLIENT_STATE before this point"
        )
