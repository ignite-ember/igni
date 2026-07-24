"""Typed Pydantic payloads for every hook the ``ToolEventHook``
fires. Replaces six raw-``dict[str, Any]`` payload literals that
previously lived inline in :mod:`ember_code.core.hooks.tool_hook`.

Each payload is a subclass of :class:`ToolHookEventPayload` and
carries only the fields it actually needs — the model type IS the
contract. :meth:`ToolHookEventPayload.to_wire_dict` collapses the
typed instance back to the ``dict[str, Any]`` shape the
:class:`HookExecutor` still accepts at its public boundary, so
no callsite downstream of ``_fire`` has to move.

Wire keys — pinned by ``tests/test_hook_events_new.py``:

* PreToolUse / PostToolUse / PostToolUseFailure / PermissionDenied
  / PermissionRequest — ``tool_name`` + ``tool_args`` (+ optional
  ``reason`` / ``error`` / ``result_preview``) + injected
  ``session_id``.
* InstructionsLoaded — ``source`` + ``files`` + ``bytes`` +
  injected ``session_id``.

The ``session_id`` field is deliberately declared on the base
model with a ``""`` default. :class:`HookFirer` injects the
session_id at the seam so per-payload constructors don't have to
plumb it, but the field exists so ``.to_wire_dict()`` includes
the key the tests read directly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.hooks.payload_sanitizer import PayloadSanitizer


class ToolHookEventPayload(BaseModel):
    """Shared base for every ``ToolEventHook``-emitted payload.

    ``extra='allow'`` matches the tolerance of the underlying
    :class:`HookPayload` in ``schemas.py``: hook receivers built
    for a slightly-older schema still parse newer payloads.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str = ""
    """Populated by :class:`HookFirer` at the wire boundary — kept
    as a field so ``.to_wire_dict()`` emits the key regardless of
    caller-side plumbing."""

    def to_wire_dict(self) -> dict[str, Any]:
        """Return the dict form the :class:`HookExecutor` expects.

        ``exclude_none=False`` on purpose: hook receivers do
        ``payload.get("error", "")`` and expect the key to exist.
        """
        return self.model_dump(mode="json", by_alias=False)


class PreToolUsePayload(ToolHookEventPayload):
    """Fired BEFORE the tool runs. A subscribed hook may set
    :attr:`HookResult.permission_decision` to allow / deny / ask
    the call."""

    tool_name: str
    tool_args: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_call(cls, tool_name: str, args: dict[str, Any]) -> PreToolUsePayload:
        """Convenience factory — the tool_hook boundary passes
        raw args, we sanitize them here so callers can't forget."""
        return cls(tool_name=tool_name, tool_args=PayloadSanitizer.safe_args(args))


class PostToolUsePayload(ToolHookEventPayload):
    """Fired AFTER the tool succeeds — carries a truncated
    ``result_preview`` for observers that want to log outputs
    without ballooning the audit trail."""

    tool_name: str
    tool_args: dict[str, str] = Field(default_factory=dict)
    result_preview: str = ""

    @classmethod
    def from_result(cls, tool_name: str, args: dict[str, Any], result: Any) -> PostToolUsePayload:
        return cls(
            tool_name=tool_name,
            tool_args=PayloadSanitizer.safe_args(args),
            result_preview=PayloadSanitizer.preview(result),
        )


class PostToolUseFailurePayload(ToolHookEventPayload):
    """Fired AFTER the tool raises — ``error`` is the stringified
    exception, matching the pre-refactor wire shape."""

    tool_name: str
    tool_args: dict[str, str] = Field(default_factory=dict)
    error: str = ""

    @classmethod
    def from_exception(
        cls, tool_name: str, args: dict[str, Any], error: BaseException
    ) -> PostToolUseFailurePayload:
        return cls(
            tool_name=tool_name,
            tool_args=PayloadSanitizer.safe_args(args),
            error=str(error),
        )


class PermissionDeniedPayload(ToolHookEventPayload):
    """Fired when the permission pipeline blocks a tool call —
    ``reason`` names the pipeline stage (``pre_tool_use_hook`` or
    ``permission_evaluator``)."""

    tool_name: str
    tool_args: dict[str, str] = Field(default_factory=dict)
    reason: str = ""

    @classmethod
    def from_call(
        cls, tool_name: str, args: dict[str, Any], reason: str
    ) -> PermissionDeniedPayload:
        return cls(
            tool_name=tool_name,
            tool_args=PayloadSanitizer.safe_args(args),
            reason=reason,
        )


class PermissionRequestPayload(ToolHookEventPayload):
    """Fired when the permission pipeline returns ASK — Agno's
    HITL dialog has already resolved by the time this fires, so
    the event is purely for observability."""

    tool_name: str
    tool_args: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_call(cls, tool_name: str, args: dict[str, Any]) -> PermissionRequestPayload:
        return cls(
            tool_name=tool_name,
            tool_args=PayloadSanitizer.safe_args(args),
        )


class InstructionsLoadedPayload(ToolHookEventPayload):
    """Fired when the rules index surfaces new subdirectory rules
    for the paths a tool just touched. ``files`` are paths
    relative to the project dir; ``bytes`` is the total UTF-8
    byte count of the surfaced content."""

    source: str = "rules_index"
    files: list[str] = Field(default_factory=list)
    bytes: int = 0
