"""Hook handlers — one class per hook type + a registry."""

from ember_code.core.hooks.handlers.base import HookHandler
from ember_code.core.hooks.handlers.command import CommandHookHandler
from ember_code.core.hooks.handlers.http import HttpHookHandler
from ember_code.core.hooks.handlers.mcp_tool import (
    McpInvocationArgs,
    MCPResolver,
    McpToolHookHandler,
)
from ember_code.core.hooks.handlers.prompt import PromptHookHandler
from ember_code.core.hooks.handlers.registry import HookHandlerRegistry

__all__ = [
    "HookHandler",
    "CommandHookHandler",
    "HttpHookHandler",
    "PromptHookHandler",
    "McpToolHookHandler",
    "McpInvocationArgs",
    "MCPResolver",
    "HookHandlerRegistry",
]
