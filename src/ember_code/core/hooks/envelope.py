"""Wire-format parser for hook return values.

Split out of :mod:`.schemas` so schemas.py stays domain-Pydantic-
only and this module owns the "raw dict off the wire" adapter.
Sits next to :mod:`.matcher` / :mod:`.merger` — each of the three
files owns one concern that participates in the hook fan-out
pipeline.

``HookEnvelope`` normalises the four documented result shapes so
the parser lives in ONE place instead of being cloned across
handlers:

* ``{"continue": false, "systemMessage": "blocked"}`` →
  :class:`HookResult` with ``should_continue=False`` and the
  message set.
* ``{"hookSpecificOutput": {"permissionDecision": "allow"}}`` →
  :class:`HookResult` with :class:`PermissionDecision.ALLOW`.
* ``{"permissionDecision": "deny"}`` (bare, legacy fallback).
* Anything else (str, None, list, etc.) — handled by the
  module-level fallback in ``executor``, not here (the
  :meth:`HookEnvelope.from_raw` classmethod returns ``None`` so
  callers can branch explicitly).

Now a Pydantic BaseModel with ``populate_by_name=True`` +
camelCase aliases + explicit ``field_validator`` coercions.
Keeps the tolerance the previous plain-class version had (0/1
for ``continue``, None-tolerant ``systemMessage``, non-dict
``hookSpecificOutput``) via typed validators instead of ad-hoc
``.get(..., default) or ""`` splatter.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ember_code.core.hooks.schemas import HookResult, PermissionDecision


class HookEnvelope(BaseModel):
    """CC-compatible hook-return envelope.

    Constructed from a dict via :meth:`from_raw` (which returns
    ``None`` for non-dict inputs — the None-branch is explicitly
    load-bearing per the executor's fallback semantics). Converted
    to a :class:`HookResult` via :meth:`to_result`.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )

    should_continue: bool = Field(default=True, alias="continue")
    system_message: str = Field(default="", alias="systemMessage")
    # Nested wrapper — the recommended CC shape. May be missing,
    # None, or (defensively) a non-dict garbage value; validator
    # normalises to an empty dict when it's not a dict.
    hook_specific_output: dict[str, Any] = Field(default_factory=dict, alias="hookSpecificOutput")
    # Bare fallback — legacy top-level shape kept for ergonomics.
    bare_permission_decision: str = Field(default="", alias="permissionDecision")

    @field_validator("should_continue", mode="before")
    @classmethod
    def _coerce_continue(cls, v: Any) -> bool:
        """Accept 0/1/bool/None. Missing defaults to True upstream;
        this validator only fires when the key is present."""
        if v is None:
            return True
        return bool(v)

    @field_validator("system_message", mode="before")
    @classmethod
    def _coerce_system_message(cls, v: Any) -> str:
        """``None`` must NOT render as the literal string ``"None"`` —
        matches the pre-refactor ``... or ""`` guard."""
        if v is None:
            return ""
        return str(v)

    @field_validator("hook_specific_output", mode="before")
    @classmethod
    def _coerce_hook_specific_output(cls, v: Any) -> dict[str, Any]:
        """Non-dict values (a string, a list, None) fall back to
        the empty-dict shape so the bare permissionDecision path
        can still fire."""
        if isinstance(v, dict):
            return v
        return {}

    @field_validator("bare_permission_decision", mode="before")
    @classmethod
    def _coerce_bare_permission_decision(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    @classmethod
    def from_raw(cls, raw: Any) -> HookEnvelope | None:
        """Return an envelope for dict inputs, ``None`` otherwise.

        The None-branch is explicit at the callsite so handlers
        can fall through to their own str/None handling (e.g.
        ``mcp_tool`` stringifies non-dict returns into
        ``message``) without hiding the branch inside a tolerant
        ``.to_result()``.
        """
        if not isinstance(raw, dict):
            return None
        return cls.model_validate(raw)

    @property
    def permission_decision(self) -> PermissionDecision:
        """Resolve the permission decision honouring the
        documented precedence: nested wins over bare.

        Returns :class:`PermissionDecision.DEFER` when neither
        source is set OR when the source string isn't one of the
        four recognised values (a defensive fallback — a hook
        that emits garbage in this slot doesn't break the pipeline).
        """
        nested = str(self.hook_specific_output.get("permissionDecision", "") or "")
        candidate = nested or self.bare_permission_decision
        return PermissionDecision.from_wire(candidate)

    def to_result(self) -> HookResult:
        """Translate to a :class:`HookResult`."""
        return HookResult(
            should_continue=self.should_continue,
            message=self.system_message,
            permission_decision=self.permission_decision,
        )
