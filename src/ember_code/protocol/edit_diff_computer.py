"""Compute styled diff rows from an ``edit_file`` tool's args.

Split out of the old ``protocol/agno_events.py``. The BE serializer
needs the row list (which needs disk access for start-line lookup),
but not the Rich Table objects — those live behind the TUI's own
:class:`EditDiffRenderer`, which the BE has no business importing.

Public surface:

* :class:`DiffRow` — Pydantic (text, style) pair used on the wire
  and as the intermediate row list.
* :class:`FileReader` protocol + :class:`LocalFileReader` — the
  disk seam, injectable so tests supply fake file contents.
* :class:`EditDiffComputer` — the coordinator with
  :meth:`compute` (from tool args) plus the per-opcode dispatch
  table that replaces the pre-refactor if/elif tag-chain.

The wire model is minimal on purpose: the BE serializer never
pulls ``rich`` into its import graph — React clients render
their own diff view from the ``list[DiffRow]`` payload.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel

# ── Wire row schema ──────────────────────────────────────────────


class DiffRow(BaseModel):
    """One line of an edit-diff, ready to render.

    Two fields:

    * ``text`` — display line (``+ ``, ``- ``, ``  `` prefix + line
      number + content), already padded / composed.
    * ``style`` — Rich style string, or ``""`` for unchanged context
      lines. Kept as a plain string so ``DiffRow`` is trivially
      Pydantic-serializable across the wire.
    """

    text: str
    style: str = ""

    @classmethod
    def from_tuple(cls, row: tuple[str, str]) -> DiffRow:
        """Coerce a legacy ``(text, style)`` tuple into a DiffRow."""
        return cls(text=row[0], style=row[1])

    def as_tuple(self) -> tuple[str, str]:
        """Round-trip helper — some callers still speak tuple."""
        return (self.text, self.style)


# ── File-reading collaborator ────────────────────────────────────


class FileReader(Protocol):
    """Just-enough contract for a source-file content reader.

    Injected into :class:`EditDiffComputer` so tests can supply
    canned file contents without touching the filesystem. Any
    exception the reader raises is caught by the computer and
    treated as "file unavailable" — the diff falls back to line 1.
    """

    def read(self, file_path: str) -> str:
        """Return the entire file's text, or raise on error."""
        ...


class LocalFileReader:
    """Default :class:`FileReader` — opens the path via ``builtins.open``.

    Production callers get this for free; tests substitute a
    fake reader with an in-memory dict of ``path → content``.
    """

    def read(self, file_path: str) -> str:
        with open(file_path) as f:
            return f.read()


# ── Style constants ──────────────────────────────────────────────

# Style strings used for delete / insert lines. Exposed at module
# scope so tests can import them without instantiating anything
# (pinning the palette).
DELETE_STYLE = "#ff6b6b on #3d0000"
INSERT_STYLE = "#69db7c on #003d00"


# ── Row computer ─────────────────────────────────────────────────


class EditDiffComputer:
    """Compose ``edit_file`` tool args into a list of :class:`DiffRow`.

    Owns:

    * The disk seam (via injected :class:`FileReader`).
    * The per-opcode row-emission dispatch — the four ``difflib``
      opcodes (``equal``, ``delete``, ``insert``, ``replace``) are
      a closed set, so we route them through a dispatch dict on
      this class rather than an if/elif chain (audit AP4).
    """

    def __init__(self, *, file_reader: FileReader | None = None) -> None:
        self._file_reader = file_reader or LocalFileReader()
        # Dispatch dict — keys are difflib opcode strings, values
        # are per-opcode row-emitters on this class. Killing the
        # tag if/elif chain (audit AP4). A future new opcode would
        # need a matching method + registry entry — a one-line add,
        # same shape as the old chain but the polymorphism is
        # explicit and testable per branch.
        self._opcode_handlers: dict[str, Callable[..., tuple[int, int]]] = {
            "equal": self._render_equal,
            "delete": self._render_delete,
            "insert": self._render_insert,
            "replace": self._render_replace,
        }

    # ── Public API ───────────────────────────────────────────

    def compute(self, tool_args: dict[str, Any] | None) -> list[DiffRow] | None:
        """Compute the DiffRow list for a tool's edit arguments.

        Returns ``None`` when there's nothing to render (missing
        or malformed args, or empty diff). Matches the pre-refactor
        contract from ``_format_edit_diff`` exactly (minus the Rich
        Table pair, which is now the TUI's responsibility).
        """
        if not tool_args or not isinstance(tool_args, dict):
            return None
        old = tool_args.get("old_string", "")
        new = tool_args.get("new_string", "")
        if not old and not new:
            return None

        old_lines = old.splitlines()
        new_lines = new.splitlines()
        start_line = self._find_start_line(
            file_path=tool_args.get("file_path", ""),
            old=old,
            new=new,
        )

        rows = self._compute_rows(old_lines, new_lines, start_line)
        if not rows:
            return None
        return rows

    # ── Row computation ──────────────────────────────────────

    def _compute_rows(
        self, old_lines: list[str], new_lines: list[str], start_line: int
    ) -> list[DiffRow]:
        """Run ``difflib.SequenceMatcher`` and dispatch each opcode
        to its handler. Handlers advance the ``old_num`` / ``new_num``
        counters and append rows in place."""
        sm = difflib.SequenceMatcher(None, old_lines, new_lines)
        rows: list[DiffRow] = []
        old_num = start_line
        new_num = start_line
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            handler = self._opcode_handlers.get(tag)
            if handler is None:
                # Defensive — ``difflib`` never emits anything
                # outside the four known opcodes, but a future
                # stdlib change shouldn't silently drop rows.
                continue
            old_num, new_num = handler(
                rows,
                old_lines,
                new_lines,
                i1,
                i2,
                j1,
                j2,
                old_num,
                new_num,
            )
        return rows

    def _render_equal(
        self,
        rows: list[DiffRow],
        old_lines: list[str],
        new_lines: list[str],
        i1: int,
        i2: int,
        j1: int,
        j2: int,
        old_num: int,
        new_num: int,
    ) -> tuple[int, int]:
        for k in range(j2 - j1):
            rows.append(DiffRow(text=f"  {new_num + k:>4}   {new_lines[j1 + k]}", style=""))
        return old_num + (i2 - i1), new_num + (j2 - j1)

    def _render_delete(
        self,
        rows: list[DiffRow],
        old_lines: list[str],
        new_lines: list[str],
        i1: int,
        i2: int,
        j1: int,
        j2: int,
        old_num: int,
        new_num: int,
    ) -> tuple[int, int]:
        for k in range(i2 - i1):
            rows.append(
                DiffRow(
                    text=f"- {old_num + k:>4}   {old_lines[i1 + k]}",
                    style=DELETE_STYLE,
                )
            )
        return old_num + (i2 - i1), new_num

    def _render_insert(
        self,
        rows: list[DiffRow],
        old_lines: list[str],
        new_lines: list[str],
        i1: int,
        i2: int,
        j1: int,
        j2: int,
        old_num: int,
        new_num: int,
    ) -> tuple[int, int]:
        for k in range(j2 - j1):
            rows.append(
                DiffRow(
                    text=f"+ {new_num + k:>4}   {new_lines[j1 + k]}",
                    style=INSERT_STYLE,
                )
            )
        return old_num, new_num + (j2 - j1)

    def _render_replace(
        self,
        rows: list[DiffRow],
        old_lines: list[str],
        new_lines: list[str],
        i1: int,
        i2: int,
        j1: int,
        j2: int,
        old_num: int,
        new_num: int,
    ) -> tuple[int, int]:
        # Diff convention: emit ALL deletes first, THEN inserts.
        for k in range(i2 - i1):
            rows.append(
                DiffRow(
                    text=f"- {old_num + k:>4}   {old_lines[i1 + k]}",
                    style=DELETE_STYLE,
                )
            )
        for k in range(j2 - j1):
            rows.append(
                DiffRow(
                    text=f"+ {new_num + k:>4}   {new_lines[j1 + k]}",
                    style=INSERT_STYLE,
                )
            )
        return old_num + (i2 - i1), new_num + (j2 - j1)

    # ── File-lookup helper ───────────────────────────────────

    def _find_start_line(self, *, file_path: str, old: str, new: str) -> int:
        """Locate the diff's real starting line inside the file.

        Try ``new`` first (post-edit / history re-render), then
        fall back to ``old`` (live in-flight edit — the file still
        has the pre-edit content). Falls back to 1 when neither
        can be found or the file can't be read. Preserves the
        pre-refactor semantics tested in ``test_format_edit_diff``.
        """
        if not file_path:
            return 1
        try:
            file_content = self._file_reader.read(file_path)
        except Exception:
            return 1

        idx = file_content.find(new) if new else -1
        if idx < 0 and old:
            idx = file_content.find(old)
        if idx < 0:
            return 1
        return file_content[:idx].count("\n") + 1


__all__ = [
    "DELETE_STYLE",
    "INSERT_STYLE",
    "DiffRow",
    "EditDiffComputer",
    "FileReader",
    "LocalFileReader",
]
