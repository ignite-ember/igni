"""RPC dispatch surface for the backend.

:class:`RpcRouter` is the composer that turns the per-domain
:class:`RpcHandler` subclasses into the flat
``dict[str, Callable[[dict], Any]]`` the receive loop calls through.
Every handler subclass owns one domain (MCP, plan mode, plugins,
etc.); this class' only job is to construct them, merge their
submaps, and validate exhaustiveness against
:class:`~ember_code.protocol.rpc.RpcMethod`.

Related-service classes (folder picker, dir scanner, shell runner,
file-completion service) live in sibling modules — see
:mod:`ember_code.backend.folder_picker`,
:mod:`ember_code.backend.dir_scanner`,
:mod:`ember_code.backend.captured_shell_runner`,
:mod:`ember_code.backend.file_completion_service`.

The router preserves the legacy ``dict[str, Callable]`` shape so
existing callers (``BackendApp``, ``SessionOrchestrator``, and the
tests that reach in with ``table[RpcMethod.X](args)``) keep working
unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ember_code.backend.captured_shell_runner import CapturedShellRunner
from ember_code.backend.file_completion_service import FileCompletionService

# Re-exports so existing callers that reach into rpc_router for the
# picker or the pool-level constants don't need to update imports.
from ember_code.backend.folder_picker import NativeFolderPicker  # noqa: F401
from ember_code.backend.login_coordinator import LoginCoordinator
from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.rpc_handlers import (
    POOL_LEVEL_RPCS,
    AuthRpcHandler,
    CodeIndexRpcHandler,
    HooksRpcHandler,
    KnowledgeRpcHandler,
    LoopRpcHandler,
    McpRpcHandler,
    ModelsRpcHandler,
    PanelRpcHandler,
    PlanRpcHandler,
    PluginsRpcHandler,
    PoolGuardRpcHandler,
    RpcHandler,
    RpcHandlerContext,
    SchedulerRpcHandler,
    SessionHistoryRpcHandler,
    SessionRpcHandler,
    SkillsRpcHandler,
)
from ember_code.protocol.rpc import RpcMethod, validate_rpc_table

logger = logging.getLogger(__name__)

# Re-exported at module level so ``session_orchestrator`` and any
# other module that needs the "which RPCs must be intercepted before
# per-runtime dispatch" invariant reads it from one place.
__all__ = ["POOL_LEVEL_RPCS", "NativeFolderPicker", "RpcRouter"]


class RpcRouter:
    """Compose per-domain :class:`RpcHandler` subclasses into the
    flat dispatch table the receive loop calls through.

    Handler instances are constructed once at :meth:`__init__` with a
    shared :class:`RpcHandlerContext` bundle; :meth:`build_table`
    walks the handler list and merges each one's
    :meth:`RpcHandler.methods` submap into a single ``dict`` keyed by
    the enum's wire-string value. :func:`validate_rpc_table` then
    checks the keyset covers every :class:`RpcMethod` entry so an
    added-enum-but-forgot-to-register mistake surfaces at boot.
    """

    #: Which RPCs the :class:`SessionOrchestrator` must intercept
    #: before per-runtime dispatch. Registered on the table as a
    #: shared "you should never reach here" guard so a regression in
    #: the intercept path fails loud instead of silently mis-routing.
    POOL_LEVEL_RPCS: frozenset[RpcMethod] = POOL_LEVEL_RPCS

    def __init__(
        self,
        *,
        backend: Any,
        transport: Any,
        login: LoginCoordinator,
        push: PushNotificationBridge,
    ) -> None:
        # Shared services the panel-cluster handlers depend on.
        # Cached here so per-runtime routers each get their own
        # warm ``FileIndex`` instead of sharing one across sessions
        # (a stale index would list files from the wrong project).
        file_completion = FileCompletionService(project_dir=backend.project_dir)
        shell_runner = CapturedShellRunner(project_dir=backend.project_dir)

        ctx = RpcHandlerContext(
            backend=backend,
            transport=transport,
            login=login,
            push=push,
            file_completion=file_completion,
            shell_runner=shell_runner,
        )
        # Order is descriptive, not load-bearing — the merge below
        # raises on duplicate keys so mis-registration surfaces
        # immediately.
        self._handlers: list[RpcHandler] = [
            McpRpcHandler(ctx),
            LoopRpcHandler(ctx),
            AuthRpcHandler(ctx),
            SessionRpcHandler(ctx),
            SessionHistoryRpcHandler(ctx),
            SchedulerRpcHandler(ctx),
            SkillsRpcHandler(ctx),
            ModelsRpcHandler(ctx),
            HooksRpcHandler(ctx),
            KnowledgeRpcHandler(ctx),
            CodeIndexRpcHandler(ctx),
            PluginsRpcHandler(ctx),
            PlanRpcHandler(ctx),
            PanelRpcHandler(ctx),
            PoolGuardRpcHandler(ctx),
        ]

    def build_table(self) -> dict[str, Callable[[dict], Any]]:
        """Produce the legacy dispatch shape
        ``{method_wire_string: handler(args)}``. Validated for
        exhaustiveness against the :class:`RpcMethod` enum before
        returning."""
        table: dict[str, Callable[[dict], Any]] = {}
        for handler in self._handlers:
            for method, fn in handler.methods().items():
                if method in table:
                    raise RuntimeError(f"duplicate RPC binding for {method!r} across handlers")
                # ``RpcMethod`` is a ``StrEnum`` — the wire key is
                # the enum's ``.value``, which is also what
                # ``validate_rpc_table`` expects to see.
                table[method] = fn
        validate_rpc_table(table.keys())
        return table
