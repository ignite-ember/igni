"""Base class + registration decorator for per-domain RPC handlers.

Every handler class subclasses :class:`RpcHandler` and tags each
method with :func:`rpc` — the decorator stashes the :class:`RpcMethod`
enum entry on the function object. :meth:`RpcHandler.methods` walks
the class at instance-init time and returns a
``dict[RpcMethod, Callable[[dict], Any]]`` — real bound-method refs
that mypy/pyright see through, replacing the stringly-typed
``getattr(self, name)`` lookup the old router used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.protocol.rpc import RpcMethod

# Attribute name the decorator uses to tag handler methods with
# their :class:`RpcMethod`. Private + underscored so a regular method
# name collision is impossible.
_RPC_METHOD_ATTR = "_ember_rpc_method"

_F = TypeVar("_F", bound=Callable[..., Any])


def rpc(method: RpcMethod) -> Callable[[_F], _F]:
    """Tag a handler method as the implementation of one
    :class:`RpcMethod` value. The decorator returns the function
    unchanged — the tag lives as an attribute on the function object,
    which :meth:`RpcHandler.methods` walks to build the dispatch
    subtable."""

    def decorator(fn: _F) -> _F:
        setattr(fn, _RPC_METHOD_ATTR, method)
        return fn

    return decorator


@dataclass(frozen=True)
class RpcHandlerContext:
    """Bundle of collaborators every handler subclass needs.

    A frozen dataclass rather than a Pydantic model because the
    fields are runtime objects (backend, transport, coordinators)
    that aren't Pydantic-friendly. Passed to every subclass'
    constructor as a single ``ctx=`` arg so the constructor shape
    stays uniform — subclasses that only need a subset just read
    the fields they care about.
    """

    backend: Any
    transport: Any
    login: LoginCoordinator
    push: PushNotificationBridge
    # Panel-cluster services — the handlers that need these read
    # them; others ignore them.
    file_completion: Any = None
    shell_runner: Any = None


class RpcHandler:
    """Base class for per-domain RPC handlers.

    Subclasses declare methods tagged with :func:`rpc` and receive
    an :class:`RpcHandlerContext` in their constructor. The context
    is stored as ``self._ctx`` so subclasses reach ``self._ctx.backend``
    for the RPC target — no reach-back into a router or another
    handler's private state.
    """

    def __init__(self, ctx: RpcHandlerContext) -> None:
        self._ctx = ctx

    def methods(self) -> dict[RpcMethod, Callable[[dict], Any]]:
        """Return ``{RpcMethod.X: self._x}`` for every ``@rpc``-tagged
        method on this class. Walks the class (not the instance) so
        inherited tags come through the MRO correctly.
        """
        table: dict[RpcMethod, Callable[[dict], Any]] = {}
        for name in dir(self):
            # Skip dunders + explicit private attrs that aren't
            # handler candidates (avoids the cost of getattr on
            # ``_ctx`` etc.).
            if name.startswith("__"):
                continue
            attr = getattr(self.__class__, name, None)
            if attr is None or not callable(attr):
                continue
            method = getattr(attr, _RPC_METHOD_ATTR, None)
            if method is None:
                continue
            bound = getattr(self, name)
            if method in table:
                raise RuntimeError(f"duplicate @rpc({method!r}) binding in {type(self).__name__}")
            table[method] = bound
        return table
