"""Typed schemas for the HITL resolution pipeline.

Extracted from :mod:`ember_code.backend.hitl_controller` — the
previous free-function module carried the ``RunRequirement``
protocol, the ``StreamFactory`` type alias, and a stringly-typed
``decision.action`` compare inline. Every one of those seams lives
here as an enum / Pydantic model / Protocol so the wire↔domain
boundary is a real conversion, not an implicit ``str`` compare.

Sibling convention: mirrors :mod:`schemas_pause` /
:mod:`schemas_run` — one schemas module per top-level pipeline
(pause / run / hitl).

Consumers:

* :class:`RunRequirement` — structural type for the opaque Agno
  requirement object. Only three attributes are actually touched
  (``tool_execution``, ``confirm()``, ``reject(note)``).
* :class:`StreamFactory` — type alias for the callback that wraps
  a live team-stream in a HITL-aware multiplexer.
* :class:`HitlAction` — domain-side enum replacing the stringly-
  typed ``decision.action`` compare in the old
  ``_apply_agno_decision``. The wire keeps ``Literal["confirm",
  "reject"]``; the enum is domain-only.
* :class:`ToolCallArgs` — typed replacement for the raw
  ``dict[str, Any]`` ``tool_args`` payload at the RPC wire seam.
  ``extra='allow'`` because tool arg keys vary per tool.
* :class:`PersistChoiceResult` — return type of
  :meth:`HitlController.maybe_persist_choice`. Replaces the
  previous ``None``-return + ``contextlib.suppress`` at the caller.
* :class:`ApplyDecisionResult` — return type of
  :meth:`AgnoDecisionApplier.apply`. Replaces the ``msg.Error |
  None`` return of the old ``_apply_agno_decision`` so the
  wire/domain split is real (wire ``msg.Error`` is built at the
  caller, not inside the applier).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from ember_code.core.config.tool_permissions import PermissionLevel
from ember_code.protocol import messages as msg


class RunRequirement(Protocol):
    """Structural type for the Agno HITL requirement objects the
    controller handles.

    Agno owns the real class (a private module symbol); this
    ``Protocol`` names the three attributes we actually touch so
    callers get real static coverage without importing an internal
    Agno symbol that could move.
    """

    tool_execution: Any

    def confirm(self) -> None: ...

    def reject(self, note: str) -> None: ...


# Type alias for the stream-factory callback: takes a live
# team-stream AsyncIterator, returns an AsyncIterator of protocol
# messages. Used to inject a HITL-aware multiplexer over the raw
# team.acontinue_run stream.
StreamFactory = Callable[[AsyncIterator[Any]], AsyncIterator[msg.Message]]


class HitlAction(str, Enum):
    """Domain-side enum for the resolver action.

    The wire ``msg.HITLDecision.action`` is a ``str`` field —
    producers may pass :class:`ember_code.protocol.schemas.enums.HITLAction`
    members for autocompletion, but the wire preserves the raw
    literal ``"confirm"``/``"reject"`` for forward-compat. The
    applier converts to this domain-internal enum at the domain
    boundary so downstream dispatch is
    ``HitlAction(decision.action) is HitlAction.CONFIRM`` rather
    than a stringly-typed compare.

    Kept distinct from :class:`msg.HITLAction` (which carries an
    ``UNKNOWN`` safety valve for wire ingest) — the domain enum
    is strict: any un-matched action raises ``ValueError`` at
    ``AgnoDecisionApplier.apply``, and the caller yields an
    ``msg.Error`` frame.
    """

    CONFIRM = "confirm"
    REJECT = "reject"


class ToolCallArgs(BaseModel):
    """Typed replacement for the raw ``dict[str, Any]``
    ``tool_args`` payload at the RPC wire seam.

    Each tool defines its own arg keys (``command`` for bash,
    ``file_path`` for read/write, ``url`` for fetch, …); we don't
    enumerate every one here because the set grows with the tool
    registry. ``extra='allow'`` preserves every provided key at the
    Pydantic seam so the downstream ``ToolPermissions.check`` reads
    them unchanged. Named fields are the common ones — they give
    static coverage for the handful of well-known keys without
    forcing the schema to know every tool.
    """

    model_config = ConfigDict(extra="allow")

    command: str = ""
    file_path: str = ""
    url: str = ""
    query: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Lower back to a plain ``dict`` for callees that still
        take a ``dict[str, Any]`` (``ToolPermissions.check`` and
        the rule builders). Empty string defaults are dropped so
        the callee sees the same shape it did pre-refactor."""
        data = self.model_dump()
        return {k: v for k, v in data.items() if v not in ("", None)}


class PersistChoiceResult(BaseModel):
    """Return type of :meth:`HitlController.maybe_persist_choice`.

    Replaces the previous ``None``-return + ``contextlib.suppress``
    at the caller. ``ok=False`` means we hit a malformed
    requirement or an unrecognised choice — the caller logs the
    reason at debug and moves on; a genuine bug still bubbles from
    a distinct ``TypeError``/``AttributeError`` outside the guarded
    section.
    """

    ok: bool
    rule: str = ""
    level: PermissionLevel | None = None
    reason: str = ""


class ApplyDecisionResult(BaseModel):
    """Return of :meth:`AgnoDecisionApplier.apply`.

    ``ok=False`` means the underlying ``req.confirm()`` /
    ``req.reject()`` raised — the caller
    (:meth:`HitlController.resolve_batch`) yields a wire
    ``msg.Error`` built from ``reason`` and moves on. Keeping the
    wire-message construction at the caller (not inside the
    applier) preserves the wire/domain split: the applier is pure
    domain logic; ``msg.Error`` is wire.
    """

    ok: bool
    reason: str = ""
