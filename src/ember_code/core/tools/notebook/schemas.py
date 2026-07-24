"""Pydantic schemas for the notebook toolkit.

Types owned here:

- :class:`NotebookCell` — one .ipynb cell, owns its own source/outputs
  and knows how to summarize / update itself. Replaces the free
  ``_get_source`` / ``_set_source`` / ``_format_cell_summary`` /
  ``_make_cell`` helpers that used to sit on :class:`NotebookTools`.
- :class:`NotebookOutput` (+ :class:`NotebookStreamOutput`,
  :class:`NotebookDataOutput`, :class:`NotebookErrorOutput`,
  :class:`UnknownOutput`) — polymorphic output types with
  ``format_preview()``. Replaces the ``if out_type == "stream" / elif
  ...`` chain in ``notebook_read_cell``.
- :class:`NotebookLoadResult` — tagged load-result. Renamed from the
  old private ``_LoadResult`` to a public-package symbol so
  :class:`NotebookDocument` can return it without exposing internals.
- :class:`NotebookOpResult` — Toolkit-facing envelope. Mirrors
  :class:`ember_code.core.tools.edit.EditResult`: agents get a
  ``str(...)`` shaped exactly like the pre-refactor return value,
  callers who want structure inspect the fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from ember_code.core.tools.notebook.document import NotebookDocument

CellType = Literal["code", "markdown", "raw"]
SourceFormat = Literal["list", "string"]

# Constants shared with the tools layer.
CELL_TYPES: tuple[CellType, ...] = ("code", "markdown", "raw")
PREVIEW_LIMIT = 120
OUTPUT_TEXT_LIMIT = 500


# ---------------------------------------------------------------------------
# Output polymorphism (kills the AP4 if/elif chain in notebook_read_cell).
# ---------------------------------------------------------------------------


class NotebookOutput(BaseModel):
    """Base class for one entry in a code cell's ``outputs`` list.

    Every subclass implements :meth:`format_preview` so the reader
    endpoint can dispatch polymorphically instead of branching on
    ``output_type``.
    """

    model_config = ConfigDict(extra="allow")

    output_type: str

    def format_preview(self) -> str:
        """Human-readable single-line preview for the agent."""
        return f"[{self.output_type}] (unrenderable)"


class NotebookStreamOutput(NotebookOutput):
    output_type: Literal["stream"] = "stream"
    name: str = "stdout"
    text: list[str] | str = Field(default_factory=list)

    def format_preview(self) -> str:
        text = "".join(self.text) if isinstance(self.text, list) else self.text
        return f"[stream/{self.name}] {text[:OUTPUT_TEXT_LIMIT]}"


class NotebookDataOutput(NotebookOutput):
    """``execute_result`` or ``display_data`` — both carry a ``data`` map."""

    output_type: Literal["execute_result", "display_data"]
    data: dict[str, Any] = Field(default_factory=dict)

    def format_preview(self) -> str:
        if "text/plain" in self.data:
            raw = self.data["text/plain"]
            text = "".join(raw) if isinstance(raw, list) else str(raw)
            return f"[{self.output_type}] {text[:OUTPUT_TEXT_LIMIT]}"
        return f"[{self.output_type}] keys: {list(self.data.keys())}"


class NotebookErrorOutput(NotebookOutput):
    output_type: Literal["error"] = "error"
    ename: str = ""
    evalue: str = ""

    def format_preview(self) -> str:
        return f"[error] {self.ename}: {self.evalue}"


class UnknownOutput(NotebookOutput):
    """Fallback for vendor-specific ``output_type`` values.

    Colab/VSCode/Deepnote occasionally emit non-standard variants —
    treat them as opaque so parsing never fails on real-world
    notebooks.
    """


def _parse_output(raw: dict[str, Any]) -> NotebookOutput:
    """Route a raw output dict to the right :class:`NotebookOutput` subclass."""
    output_type = raw.get("output_type", "unknown")
    if output_type == "stream":
        return NotebookStreamOutput.model_validate(raw)
    if output_type in ("execute_result", "display_data"):
        return NotebookDataOutput.model_validate(raw)
    if output_type == "error":
        return NotebookErrorOutput.model_validate(raw)
    return UnknownOutput.model_validate(raw)


# ---------------------------------------------------------------------------
# NotebookCell — the four static helpers on the old NotebookTools collapse
# into instance methods here.
# ---------------------------------------------------------------------------


class NotebookCell(BaseModel):
    """One cell in a Jupyter notebook.

    Owns its ``source`` (whether stored as list-of-lines or single
    string), ``metadata``, and — for code cells — ``execution_count``
    plus ``outputs``. Extra vendor fields are preserved via
    ``extra="allow"`` so Colab / VSCode / Deepnote metadata round-trips
    intact.

    The ``_source_was_list`` tag is set at parse time from the wire
    format and read at dump time so on-disk diffs stay stable for
    version-controlled notebooks.
    """

    model_config = ConfigDict(extra="allow")

    cell_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: list[str] | str = Field(default_factory=list)
    execution_count: int | None = None
    outputs: list[dict[str, Any]] = Field(default_factory=list)

    # Preserved-on-parse serialization tag. Not part of the wire
    # format; excluded from ``model_dump`` output.
    _source_was_list: bool = True

    @field_validator("source", mode="before")
    @classmethod
    def _coerce_source(cls, value: Any) -> list[str] | str:
        """Old nbformat 3 files sometimes carry non-list, non-str source.

        Coerce anything unexpected to a string so validation never
        rejects a real-world notebook.
        """
        if isinstance(value, (list, str)):
            return value
        if value is None:
            return ""
        return str(value)

    def model_post_init(self, __context: Any) -> None:
        # Record the wire format so :meth:`set_source_text` can put it
        # back the way Jupyter (and the user's editor) expects.
        object.__setattr__(self, "_source_was_list", isinstance(self.source, list))

    # ---- source helpers (used to be _get_source / _set_source) ----

    def source_text(self) -> str:
        """Return the cell's source as a single string."""
        if isinstance(self.source, list):
            return "".join(self.source)
        return self.source

    def set_source_text(self, text: str) -> None:
        """Update the cell's source, preserving its on-disk shape.

        If the cell was parsed from a list-of-lines source, the new
        text is split back into a list with trailing ``\\n`` on every
        line except the last — matching Jupyter's own writer.
        """
        if self._source_was_list:
            lines = text.split("\n")
            self.source = [line + "\n" for line in lines[:-1]] + [lines[-1]] if lines else []
        else:
            self.source = text

    def summary(self, index: int) -> str:
        """One-line summary for :meth:`NotebookTools.notebook_read`."""
        src = self.source_text()
        preview = src[:PREVIEW_LIMIT].replace("\n", "\\n")
        if len(src) > PREVIEW_LIMIT:
            preview += "..."
        lines = src.count("\n") + 1 if src else 0
        cell_type = self.cell_type or "unknown"
        return f"[{index}] {cell_type} ({lines} lines): {preview}"

    def clear_outputs(self) -> None:
        """Reset outputs + execution count. Standard Jupyter behavior
        for a modified code cell.
        """
        if self.cell_type == "code":
            self.outputs = []
            self.execution_count = None

    def typed_outputs(self) -> list[NotebookOutput]:
        """Return the ``outputs`` list as polymorphic
        :class:`NotebookOutput` instances so
        :meth:`NotebookOutput.format_preview` can dispatch on subclass.
        """
        return [_parse_output(raw) for raw in self.outputs]

    @classmethod
    def new(cls, cell_type: CellType, source: str) -> NotebookCell:
        """Factory for a fresh nbformat 4 cell.

        Code cells receive the mandatory ``execution_count`` /
        ``outputs`` fields; other types get only what the spec
        requires.
        """
        lines = source.split("\n")
        source_list = [line + "\n" for line in lines[:-1]] + [lines[-1]] if lines else []
        return cls(
            cell_type=cell_type,
            metadata={},
            source=source_list,
            execution_count=None,
            outputs=[],
        )

    def to_wire(self) -> dict[str, Any]:
        """Dump the cell in the exact shape Jupyter writes to disk.

        Non-code cells drop ``execution_count`` / ``outputs`` — those
        fields belong only to code cells per the nbformat 4 spec.
        """
        wire: dict[str, Any] = {
            "cell_type": self.cell_type,
            "metadata": self.metadata,
            "source": self.source,
        }
        if self.cell_type == "code":
            wire["execution_count"] = self.execution_count
            wire["outputs"] = self.outputs
        # Preserve unknown vendor keys (Colab, VSCode, Deepnote).
        for key, value in (self.__pydantic_extra__ or {}).items():
            wire.setdefault(key, value)
        return wire


# ---------------------------------------------------------------------------
# Tagged load result + tool-facing operation envelope.
# ---------------------------------------------------------------------------


class NotebookLoadResult(BaseModel):
    """Tagged result from :meth:`NotebookDocument.load`.

    Callers check ``ok`` — on success ``document`` is populated; on
    failure ``error`` carries the human-readable message the tool
    surfaces to the agent.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool
    error: str = ""
    document: NotebookDocument | None = None

    @classmethod
    def success(cls, document: NotebookDocument) -> NotebookLoadResult:
        return cls(ok=True, document=document)

    @classmethod
    def fail(cls, error: str) -> NotebookLoadResult:
        return cls(ok=False, error=error)


class NotebookOpResult(BaseModel):
    """Envelope for tool endpoints.

    Mirrors :class:`ember_code.core.tools.edit.EditResult` — the
    agent-visible surface is ``str(result)`` which yields the exact
    pre-refactor message; structured fields (``ok``, ``path``,
    ``cell_index``) are available for internal callers.
    """

    ok: bool
    message: str
    path: str = ""
    cell_index: int | None = None

    def __str__(self) -> str:
        return self.message


__all__ = [
    "CELL_TYPES",
    "CellType",
    "NotebookCell",
    "NotebookDataOutput",
    "NotebookErrorOutput",
    "NotebookLoadResult",
    "NotebookOpResult",
    "NotebookOutput",
    "NotebookStreamOutput",
    "SourceFormat",
    "UnknownOutput",
]
