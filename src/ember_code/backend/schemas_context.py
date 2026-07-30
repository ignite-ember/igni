"""Typed view models + wire schemas for the context / compaction /
history-truncation surface.

Extracted out of :mod:`ember_code.backend.cmd_context` and
:mod:`ember_code.backend.server_context` — the wire types the
:class:`ContextController` RPCs return live here now, alongside
the pre-existing chat-view models. Same naming + purpose pattern
as the sibling :mod:`schemas_codeindex` module already in
``backend/``.

Wire schemas (RPC-returned Pydantic models):

* :class:`TruncateHistoryResult` — ``ContextController.truncate_history``
  result: ``removed`` run-count + optional ``error`` string.
* :class:`PendingMessage` — one pre-persisted user message row
  surfaced by ``ContextController.get_pending_messages``.
  ``role`` is always ``"user"`` — encapsulated via
  :meth:`PendingMessage.from_pending_row`.
* :data:`PENDING_STALENESS_SECONDS` — how stale a pending row
  must be before we surface it as "interrupted".

Chat-view models (slash-command output):

* :class:`OutputStylesListView` — the ``/output-style list``
  chat card. Wraps the raw ``styles`` mapping plus the currently
  active style name and renders the ``**Output styles**``
  markdown list (or the empty-state info card).
* :class:`OutputStyleStatusView` — the ``/output-style status``
  / ``show`` info card, with a ``(none)`` sentinel for the
  empty-string case.
* :class:`ContextBreakdownView` — the ``/ctx`` markdown card.
  Owns the 7-line template plus the ``runs/total*100``
  percentage calculation; the domain
  :class:`ContextBreakdown` in
  :mod:`ember_code.core.session.schemas` stays presentation-
  free (no ``CommandResult`` import in the domain layer).

``OutputStyle`` (a ``@dataclass``) and ``ContextBreakdown`` (a
``BaseModel``) are both imported lazily under ``TYPE_CHECKING``
so this module stays import-cheap. Views that wrap the
``OutputStyle`` dataclass set ``arbitrary_types_allowed=True``,
matching the treatment :class:`CodeIndexStatusView` gives to the
``ResolvedRepository`` dataclass.

The domain :class:`~ember_code.core.session.pending_messages.PendingMessage`
dataclass — the storage-row type this module's wire model wraps —
is imported under ``TYPE_CHECKING`` as ``PendingMessageRow`` to
avoid shadowing the wire-model class name at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from ember_code.backend.command_result import CommandResult
from ember_code.core.output_styles import OutputStyle

if TYPE_CHECKING:
    from ember_code.core.session.pending_messages import (
        PendingMessage as PendingMessageRow,
    )
    from ember_code.core.session.schemas import ContextBreakdown


# How stale a pending-message row must be before we surface it
# as an "interrupted" banner. A fresh pending row almost always
# means "Agno is still finishing its post-stream tail" (it can
# take 15-30 s). The banner is meant for actual crashes across
# BE restarts — 60 s makes a reload during the tail stay quiet.
PENDING_STALENESS_SECONDS = 60


class TruncateHistoryResult(BaseModel):
    """Wire shape for :meth:`ContextController.truncate_history` —
    ``removed`` is the count of runs dropped (0 on any failure);
    ``error`` is empty on success."""

    removed: int
    error: str = ""


class PendingMessage(BaseModel):
    """One pre-persisted user message row surfaced by
    :meth:`ContextController.get_pending_messages`. ``role`` is
    always ``"user"`` — only user turns get pre-persisted, so this
    is a constant on the wire; declared as a plain field (not a
    discriminator) for FE-parity.

    Build one from a storage-row dataclass via
    :meth:`from_pending_row` so the controller doesn't have to
    hardcode the ``role="user"`` constant at every call site.
    """

    role: str
    content: str
    received_at: int
    message_id: str

    @classmethod
    def from_pending_row(cls, row: PendingMessageRow) -> PendingMessage:
        """Project a domain :class:`PendingMessage` storage row
        (the ``@dataclass`` in
        :mod:`ember_code.core.session.pending_messages`) onto the
        wire model. ``role`` is fixed to ``"user"`` — the pending
        table only holds user turns."""
        return cls(
            role="user",
            content=row.text,
            received_at=row.received_at,
            message_id=row.message_id,
        )


class OutputStylesListView(BaseModel):
    """Wraps the discovered ``styles`` mapping + the active style
    name for the ``/output-style list`` chat card.

    Empty-styles branch renders the "no output styles configured"
    info card (with a one-liner nudge to drop a markdown file
    into ``.ember/output-styles/``). The populated branch renders
    a sorted bullet list, marking the active style with
    ``(active)`` and falling back to ``_(no description)_`` when
    a style leaves its ``description`` blank.

    ``OutputStyle`` is a plain ``@dataclass`` (not a Pydantic
    model), so we opt into ``arbitrary_types_allowed=True`` — same
    treatment :class:`CodeIndexStatusView` gives its
    ``ResolvedRepository`` field. Wrapping the raw mapping keeps
    this view thin; the coordinator doesn't have to pre-project
    into ``(name, description, active)`` triples.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    styles: dict[str, OutputStyle] = Field(default_factory=dict)
    active: str = ""

    def to_command_result(self) -> CommandResult:
        if not self.styles:
            return CommandResult.info(
                "No output styles configured. Drop a markdown file at "
                "`.ember/output-styles/<name>.md` (frontmatter: `name`, "
                "`description`; body is the system-prompt extension)."
            )
        lines = ["**Output styles**", ""]
        for name in sorted(self.styles):
            marker = " (active)" if name == self.active else ""
            desc = self.styles[name].description or "_(no description)_"
            lines.append(f"- `{name}`{marker} — {desc}")
        lines.append("")
        lines.append("Switch with `/output-style <name>`.")
        return CommandResult.markdown("\n".join(lines))


class OutputStyleStatusView(BaseModel):
    """Wraps the active-style name for the ``/output-style status``
    (or ``show``) info card.

    ``active`` is the raw string returned by
    :attr:`Session.active_output_style`; the empty-string sentinel
    the session emits when no style has been picked yet is
    rendered as ``(none)`` in the info card, preserving the
    original ``active or '(none)'`` behavior.
    """

    active: str | None = None

    def to_command_result(self) -> CommandResult:
        return CommandResult.info(f"Active output style: **{self.active or '(none)'}**")


class ContextBreakdownView(BaseModel):
    """Wraps a domain :class:`ContextBreakdown` for the ``/ctx``
    markdown card.

    Owns the 7-line ``**Context breakdown**`` template plus the
    ``runs/total*100`` percentage calculation, so the domain
    model in :mod:`core.session.schemas` stays render-free.
    Constructed via :meth:`from_domain` so the coordinator never
    has to know about the field names — it just hands over the
    ``ContextBreakdown`` it got from
    :meth:`Session.context_breakdown`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    breakdown: ContextBreakdown

    @classmethod
    def from_domain(cls, breakdown: ContextBreakdown) -> ContextBreakdownView:
        return cls(breakdown=breakdown)

    def to_command_result(self) -> CommandResult:
        b = self.breakdown
        total = b.total
        runs = b.runs
        floor = b.floor
        pct = (runs / total * 100.0) if total else 0.0
        lines = [
            "**Context breakdown**",
            "",
            f"- **Total:** {total:,} tokens",
            f"- **Conversation (runs):** {runs:,} tokens ({pct:.1f}% of total)",
            f"- **Floor (system + tools + rules + memories + summary):** {floor:,} tokens",
            "",
            "`/compact` only clears the conversation portion — the floor "
            "is rebaked into every prompt and cannot be compacted away.",
        ]
        return CommandResult.markdown("\n".join(lines))


__all__ = [
    "PENDING_STALENESS_SECONDS",
    "TruncateHistoryResult",
    "PendingMessage",
    "OutputStylesListView",
    "OutputStyleStatusView",
    "ContextBreakdownView",
]
