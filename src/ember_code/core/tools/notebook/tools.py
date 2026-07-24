"""NotebookTools — Toolkit wiring for the notebook sub-package.

Every endpoint is a thin ``load -> op -> save`` pipeline. The
subject-less staticmethod helpers that used to sit here now live on
:class:`NotebookCell` (source/preview/factory) and
:class:`NotebookDocument` (load/save/bounds/mutation). Output preview
dispatch has moved to :class:`NotebookOutput` subclasses so
``notebook_read_cell`` no longer branches on ``output_type``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agno.tools import Toolkit

from ember_code.core.tools.notebook.document import NotebookDocument
from ember_code.core.tools.notebook.schemas import (
    CELL_TYPES,
    NotebookCell,
    NotebookOpResult,
)


class NotebookTools(Toolkit):
    """Read and edit individual cells in Jupyter notebooks (.ipynb).

    Operates on the notebook's JSON structure directly — no nbformat
    dependency required. Preserves all metadata, outputs, and
    formatting.
    """

    def __init__(
        self,
        base_dir: str | None = None,
        *,
        requires_confirmation_tools: list[str] | None = None,
        **toolkit_kwargs: Any,
    ):
        """Construct the notebook toolkit.

        Args:
            base_dir: Working directory for relative-path notebook
                edits. Defaults to the current working directory.
            requires_confirmation_tools: Tool names that should gate
                on human-in-the-loop confirmation. Threaded through to
                Agno's ``requires_confirmation`` flag on both sync and
                async registries.
            **toolkit_kwargs: Forwarded verbatim to
                :class:`agno.tools.Toolkit` (``name``, ``tool_hooks``,
                etc.). Explicit passthrough — no silent kwarg drop.
        """
        super().__init__(name="ember_notebook", **toolkit_kwargs)
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.register(self.notebook_read)
        self.register(self.notebook_read_cell)
        self.register(self.notebook_edit_cell)
        self.register(self.notebook_add_cell)
        self.register(self.notebook_remove_cell)
        if requires_confirmation_tools:
            self.requires_confirmation_tools = requires_confirmation_tools
            # Agno routes async callables into ``async_functions`` and
            # sync into ``functions``. Skip either dict and the HITL
            # gate silently disables — sibling ``EmberEditTools`` walks
            # both; match that here.
            for registry in (self.functions, self.async_functions):
                for name, func in registry.items():
                    if name in requires_confirmation_tools:
                        func.requires_confirmation = True

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to ``base_dir``."""
        p = Path(path)
        if not p.is_absolute():
            p = self.base_dir / p
        return p

    # ---- endpoints ----

    def notebook_read(self, file_path: str) -> str:
        """Read a Jupyter notebook and return a summary of all cells.

        Args:
            file_path: Path to the .ipynb file.

        Returns:
            Cell index, type, line count, and source preview for each cell.
        """
        path = self._resolve_path(file_path)
        loaded = NotebookDocument.load(path)
        if not loaded.ok:
            return loaded.error
        assert loaded.document is not None
        doc = loaded.document

        if not doc.cells:
            return f"Notebook {path} has no cells."

        lines = [
            f"Notebook: {path} ({len(doc.cells)} cells, kernel: {doc.kernel_display_name()})",
            "",
        ]
        for i, cell in enumerate(doc.cells):
            lines.append(cell.summary(i))
        return "\n".join(lines)

    def notebook_read_cell(self, file_path: str, cell_index: int) -> str:
        """Read a specific cell's full source and outputs.

        Args:
            file_path: Path to the .ipynb file.
            cell_index: Zero-based index of the cell to read.

        Returns:
            Cell type, full source, and outputs (for code cells).
        """
        path = self._resolve_path(file_path)
        loaded = NotebookDocument.load(path)
        if not loaded.ok:
            return loaded.error
        assert loaded.document is not None
        doc = loaded.document

        cell = doc.at(cell_index)
        if cell is None:
            return doc.out_of_range_message(cell_index)

        parts = [
            f"Cell [{cell_index}] ({cell.cell_type or 'unknown'}):",
            "",
            cell.source_text(),
        ]

        if cell.cell_type == "code" and cell.outputs:
            parts.append("")
            parts.append(f"--- Outputs ({len(cell.outputs)}) ---")
            for output in cell.typed_outputs():
                parts.append(output.format_preview())

        return "\n".join(parts)

    def notebook_edit_cell(self, file_path: str, cell_index: int, new_source: str) -> str:
        """Replace a cell's source content.

        Clears outputs for code cells (standard Jupyter behavior for
        modified cells).

        Args:
            file_path: Path to the .ipynb file.
            cell_index: Zero-based index of the cell to edit.
            new_source: The new source content for the cell.

        Returns:
            Success or error message.
        """
        path = self._resolve_path(file_path)
        loaded = NotebookDocument.load(path)
        if not loaded.ok:
            return loaded.error
        assert loaded.document is not None
        doc = loaded.document

        if doc.replace_source(cell_index, new_source) is None:
            return doc.out_of_range_message(cell_index)

        doc.save()
        return str(
            NotebookOpResult(
                ok=True,
                message=f"Successfully edited cell [{cell_index}] in {path}",
                path=str(path),
                cell_index=cell_index,
            )
        )

    def notebook_add_cell(
        self, file_path: str, cell_index: int, cell_type: str, source: str
    ) -> str:
        """Insert a new cell at the given index.

        Args:
            file_path: Path to the .ipynb file.
            cell_index: Position to insert at (0 = beginning). Use -1 to append.
            cell_type: One of "code", "markdown", or "raw".
            source: The cell source content.

        Returns:
            Success or error message.
        """
        if cell_type not in CELL_TYPES:
            return f"Error: Invalid cell_type '{cell_type}'. Must be 'code', 'markdown', or 'raw'."

        path = self._resolve_path(file_path)
        loaded = NotebookDocument.load(path)
        if not loaded.ok:
            return loaded.error
        assert loaded.document is not None
        doc = loaded.document

        new_cell = NotebookCell.new(cell_type, source)  # type: ignore[arg-type]
        idx = doc.insert(new_cell, cell_index)
        if idx == -1:
            return f"Error: Cell index {cell_index} out of range (0-{len(doc.cells)})."

        doc.save()
        return str(
            NotebookOpResult(
                ok=True,
                message=f"Successfully added {cell_type} cell at [{idx}] in {path}",
                path=str(path),
                cell_index=idx,
            )
        )

    def notebook_remove_cell(self, file_path: str, cell_index: int) -> str:
        """Remove a cell by index.

        Args:
            file_path: Path to the .ipynb file.
            cell_index: Zero-based index of the cell to remove.

        Returns:
            Success or error message.
        """
        path = self._resolve_path(file_path)
        loaded = NotebookDocument.load(path)
        if not loaded.ok:
            return loaded.error
        assert loaded.document is not None
        doc = loaded.document

        removed = doc.remove(cell_index)
        if removed is None:
            return doc.out_of_range_message(cell_index)

        doc.save()
        return str(
            NotebookOpResult(
                ok=True,
                message=(
                    f"Successfully removed {removed.cell_type or 'unknown'} "
                    f"cell [{cell_index}] from {path}"
                ),
                path=str(path),
                cell_index=cell_index,
            )
        )


__all__ = ["NotebookTools"]
