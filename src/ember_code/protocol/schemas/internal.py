"""Intermediate value objects used by the BE serializer.

These are NOT wire messages — they are typed payloads the BE
serializer builds from Agno events and then unpacks into wire
messages (:class:`~ember_code.protocol.schemas.be_events.ToolCompleted`
etc.). Physically separated from :mod:`.be_events` so a reader
isn't misled that :class:`ToolResultData` appears on the wire.

Consumers: :mod:`ember_code.protocol.agno_tool_formatter`,
:mod:`ember_code.protocol.serializer`,
:mod:`ember_code.backend.hitl_stream_mux`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator


class ToolResultData(BaseModel):
    """Structured payload extracted from an Agno tool-completion event.

    Replaces the pre-refactor ``__slots__`` bag class in the deleted
    ``agno_events.py``. Fields:

    * ``summary`` — the one-line label shown on the collapsed tool
      card.
    * ``full_result`` — the full result text, empty when the diff
      branch owns the display.
    * ``has_markup`` — True when the card should render a Rich
      diff rather than plain text.
    * ``diff_rows`` — serialized ``(text, style)`` rows for the
      diff. Rich Table objects are NOT stored here (they aren't
      Pydantic-serializable); the TUI reconstructs them via
      :meth:`EditDiffRenderer.tables_from` at render time.

    Invariants (enforced by ``_check_markup_invariant``):

    * ``has_markup=True`` ⇒ ``diff_rows`` is a non-empty list AND
      ``full_result == ""``. Callers that violate this used to
      let the ``Error:`` prefix disappear (the v0.5.11 lying-UI
      bug) — see :meth:`from_edit_diff` for the safe constructor.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    summary: str = ""
    full_result: str = ""
    has_markup: bool = False
    # Kept as ``list[tuple[str, str]]`` for the wire so existing
    # ``ToolCompleted.diff_rows`` protocol clients (React web /
    # Tauri / VSCode / JetBrains) continue to deserialize.
    diff_rows: list[tuple[str, str]] | None = None

    @model_validator(mode="after")
    def _check_markup_invariant(self) -> ToolResultData:
        if self.has_markup:
            if not self.diff_rows:
                raise ValueError("ToolResultData: has_markup=True requires diff_rows to be set")
            if self.full_result:
                raise ValueError("ToolResultData: has_markup=True requires full_result to be empty")
        return self

    @classmethod
    def from_edit_diff(
        cls,
        *,
        summary: str,
        diff_rows: list,
    ) -> ToolResultData:
        """Construct the edit-diff variant with the invariant baked in.

        Accepts either the typed ``DiffRow`` list or the legacy
        ``(text, style)`` tuple list — both are coerced to the wire
        tuple form. Guarantees ``has_markup=True`` and
        ``full_result=""`` so the serializer's error-detection path
        can never be sidestepped by a diff.
        """
        wire_rows: list[tuple[str, str]] = []
        for r in diff_rows:
            # Accept DiffRow, plain tuple, or list of two strings.
            if hasattr(r, "as_tuple"):
                wire_rows.append(r.as_tuple())
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                wire_rows.append((str(r[0]), str(r[1])))
            else:  # pragma: no cover - defensive
                continue
        return cls(
            summary=summary,
            full_result="",
            has_markup=True,
            diff_rows=wire_rows,
        )


__all__ = ["ToolResultData"]
