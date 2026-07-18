"""NotebookDocument — owns a parsed .ipynb file as an object.

The old free helpers ``_load_notebook`` / ``_save_notebook`` on
:class:`NotebookTools` took a ``Path`` as their first arg and reached
into the returned dict directly. That's the classic
"free-function-with-state-first-arg" audit offender; here the file
*is* the object and every operation (``at``, ``insert``, ``remove``,
``replace_source``) lives on it as an instance method.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.tools.notebook.schemas import (
    NotebookCell,
    NotebookLoadResult,
)


class NotebookDocument(BaseModel):
    """One parsed notebook, plus every operation the toolkit needs.

    ``extra="allow"`` on the model keeps forward-compat vendor keys
    (Colab, VSCode, Deepnote inject occasional top-level fields) so a
    load -> save round-trip does not silently strip them.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    path: Path
    cells: list[NotebookCell] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    nbformat: int = 4
    nbformat_minor: int = 5

    # ---- constructors ----

    @classmethod
    def load(cls, path: Path) -> NotebookLoadResult:
        """Load, validate, and parse a notebook.

        Returns a tagged :class:`NotebookLoadResult` — callers branch
        on ``ok`` instead of catching exceptions.
        """
        if not path.exists():
            return NotebookLoadResult.fail(f"Error: File not found: {path}")
        if path.suffix != ".ipynb":
            return NotebookLoadResult.fail(f"Error: Not a notebook file: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return NotebookLoadResult.fail(f"Error: Invalid notebook JSON: {exc}")

        document = cls(
            path=path,
            cells=[NotebookCell.model_validate(cell) for cell in raw.get("cells", [])],
            metadata=raw.get("metadata", {}),
            nbformat=raw.get("nbformat", 4),
            nbformat_minor=raw.get("nbformat_minor", 5),
        )
        # Preserve any unexpected top-level keys so load->save stays lossless.
        known = {"cells", "metadata", "nbformat", "nbformat_minor"}
        for key, value in raw.items():
            if key not in known:
                setattr(document, key, value)
        return NotebookLoadResult.success(document)

    # ---- persistence ----

    def save(self) -> None:
        """Write the notebook back to disk in Jupyter's canonical shape.

        Byte-matches the pre-refactor output:
        ``json.dumps(nb, indent=1, ensure_ascii=False) + "\\n"``.
        """
        wire: dict[str, Any] = {
            "cells": [cell.to_wire() for cell in self.cells],
            "metadata": self.metadata,
            "nbformat": self.nbformat,
            "nbformat_minor": self.nbformat_minor,
        }
        # Round-trip any vendor top-level keys captured on load.
        for key, value in (self.__pydantic_extra__ or {}).items():
            wire.setdefault(key, value)
        self.path.write_text(
            json.dumps(wire, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ---- accessors ----

    def kernel_display_name(self) -> str:
        """Read ``metadata.kernelspec.display_name``, defaulting to
        ``"unknown"`` (matches the pre-refactor summary line).
        """
        return self.metadata.get("kernelspec", {}).get("display_name", "unknown")

    def at(self, index: int) -> NotebookCell | None:
        """Bounds-checked cell accessor — returns ``None`` on
        out-of-range so endpoints can format their own error string
        with the exact expected wording.
        """
        if index < 0 or index >= len(self.cells):
            return None
        return self.cells[index]

    def out_of_range_message(self, index: int) -> str:
        """Format the standard ``"out of range"`` message. Kept on the
        document so the wording lives with the bounds logic.
        """
        return f"Error: Cell index {index} out of range (0-{len(self.cells) - 1})."

    # ---- mutations ----

    def insert(self, cell: NotebookCell, at: int) -> int:
        """Insert ``cell`` at index ``at`` (``-1`` appends). Returns
        the effective index, or ``-1`` if ``at`` was out of range.
        """
        if at == -1:
            self.cells.append(cell)
            return len(self.cells) - 1
        if at < 0 or at > len(self.cells):
            return -1
        self.cells.insert(at, cell)
        return at

    def remove(self, index: int) -> NotebookCell | None:
        """Pop the cell at ``index``; return ``None`` if out of range."""
        if index < 0 or index >= len(self.cells):
            return None
        return self.cells.pop(index)

    def replace_source(self, index: int, text: str) -> NotebookCell | None:
        """Set cell ``index``'s source, clearing outputs for code cells.

        Returns the modified cell, or ``None`` if ``index`` is out of
        range. The endpoint handles the "out of range" error message.
        """
        cell = self.at(index)
        if cell is None:
            return None
        cell.set_source_text(text)
        cell.clear_outputs()
        return cell


# NotebookLoadResult forward-refs NotebookDocument; resolve now that
# both classes exist.
NotebookLoadResult.model_rebuild()


__all__ = ["NotebookDocument"]
