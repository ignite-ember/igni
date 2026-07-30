"""Hooks system — pre/post tool execution hooks."""

from ember_code.core.hooks.envelope import HookEnvelope
from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.loader import HookLoader
from ember_code.core.hooks.registry import HookRegistry
from ember_code.core.hooks.schemas import (
    GenericHookPayload,
    HookDefinition,
    HookDefinitionBase,
    HookLoadResult,
    HookLoadWarning,
    HookPayload,
    HookPayloadBase,
    HookResult,
    HookType,
    MergeStrategy,
    PermissionDecision,
)

__all__ = [
    "HookLoader",
    "HookLoadResult",
    "HookLoadWarning",
    "HookDefinition",
    "HookDefinitionBase",
    "HookRegistry",
    "HookType",
    "HookResult",
    "HookPayload",
    "HookPayloadBase",
    "GenericHookPayload",
    "HookEnvelope",
    "HookExecutor",
    "HookEvent",
    "MergeStrategy",
    "PermissionDecision",
]
