"""Per-domain RPC handler classes.

Every RPC in :class:`RpcMethod` is implemented by a method on one of
these :class:`RpcHandler` subclasses. :class:`RpcRouter` composes the
subclasses at construction and merges their :meth:`RpcHandler.methods`
submaps into the flat ``dict[str, Callable[[dict], Any]]`` dispatch
table the receive loop calls through.
"""

from __future__ import annotations

from ember_code.backend.rpc_handlers.auth import AuthRpcHandler
from ember_code.backend.rpc_handlers.base import RpcHandler, RpcHandlerContext, rpc
from ember_code.backend.rpc_handlers.codeindex import CodeIndexRpcHandler
from ember_code.backend.rpc_handlers.hooks import HooksRpcHandler
from ember_code.backend.rpc_handlers.knowledge import KnowledgeRpcHandler
from ember_code.backend.rpc_handlers.loop import LoopRpcHandler
from ember_code.backend.rpc_handlers.mcp import McpRpcHandler
from ember_code.backend.rpc_handlers.models import ModelsRpcHandler
from ember_code.backend.rpc_handlers.panels import PanelRpcHandler
from ember_code.backend.rpc_handlers.plan import PlanRpcHandler
from ember_code.backend.rpc_handlers.plugins import PluginsRpcHandler
from ember_code.backend.rpc_handlers.pool_guard import (
    POOL_LEVEL_RPCS,
    PoolGuardRpcHandler,
)
from ember_code.backend.rpc_handlers.scheduler import SchedulerRpcHandler
from ember_code.backend.rpc_handlers.session import SessionRpcHandler
from ember_code.backend.rpc_handlers.session_history import SessionHistoryRpcHandler
from ember_code.backend.rpc_handlers.skills import SkillsRpcHandler

__all__ = [
    "POOL_LEVEL_RPCS",
    "AuthRpcHandler",
    "CodeIndexRpcHandler",
    "HooksRpcHandler",
    "KnowledgeRpcHandler",
    "LoopRpcHandler",
    "McpRpcHandler",
    "ModelsRpcHandler",
    "PanelRpcHandler",
    "PlanRpcHandler",
    "PluginsRpcHandler",
    "PoolGuardRpcHandler",
    "RpcHandler",
    "RpcHandlerContext",
    "SchedulerRpcHandler",
    "SessionHistoryRpcHandler",
    "SessionRpcHandler",
    "SkillsRpcHandler",
    "rpc",
]
