"""Codec for the CodeIndex ↔ Chroma wire shape.

Chroma metadata is a flat ``dict[str, str | int | bool]`` that rejects
``None``. Every ``CodeIndexItem`` gets flattened into that shape on
write and reconstituted on read; the two directions used to be two
mirror-image dict literals in ``index.py`` that had to be edited in
lockstep every time a field was added.

:class:`ChromaRowCodec` owns:

  - The single list of quality categorical field names.
  - The single list of multi-value list field names.
  - The ``\\x1f`` separator that brackets list-field values so
    ``$contains`` matches exactly (``"sql"`` doesn't collide with
    ``"sql-injection"``).
  - :meth:`flatten` — write path (item → row metadata).
  - :meth:`parse` — read path (row metadata → CodeIndexResult).
  - :meth:`flatten_chunk_row` — chunk write path.

Because :meth:`flatten` and :meth:`parse` both iterate the same
class-level tuples, the two dict literals can no longer drift.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ember_code.core.code_index.schema.chroma_row import ChromaChunkRow, ChromaRowMetadata
from ember_code.core.code_index.schema.items import CodeIndexItem, CodeIndexResult


class ChromaRowCodec:
    """Encode/decode :class:`CodeIndexItem` ↔ chroma row metadata.

    All configuration is class-level so tests / knowledge/index.py can
    subclass and override a single field list without touching every
    read/write callsite.
    """

    # ASCII unit separator — used to bracket multi-value list fields so
    # ``$contains: "\x1fsql-injection\x1f"`` exact-matches without false
    # prefix collisions (``"sql"`` would otherwise match ``"sql-injection"``).
    LIST_SEP: str = "\x1f"

    # Quality categorical fields — each is a single-value enum string on
    # the chroma row (or ``""`` when not assessed). Listed here so the
    # flattener / read paths agree on which fields exist.
    QUALITY_CATEGORICAL_FIELDS: tuple[str, ...] = (
        "quality",
        "complexity",
        "security",
        "testing",
        "testability",
        "documentation",
        "performance",
        "issues",
        "maintainability",
        "architecture",
        "technical_debt",
        "cohesion",
        "coupling",
        "stability",
        "priority",
    )

    # Multi-value list fields — stored as ``\x1f``-bracketed strings.
    LIST_FIELDS: tuple[str, ...] = (
        "vulnerabilities",
        "frameworks",
        "domain",
        "concerns",
        "layers",
        "patterns",
        "keywords",
        "file_issues",
    )

    # ── Write path ──────────────────────────────────────────────────

    def flatten(self, item: CodeIndexItem) -> ChromaRowMetadata:
        """Pack a :class:`CodeIndexItem`'s fields into chromadb-friendly metadata.

        Single-value fields land as exact-match strings; quality
        categoricals use ``""`` as the "not assessed" sentinel since
        chroma metadata can't hold ``None``. Multi-value lists use
        ``\\x1f`` brackets so ``$contains`` is exact-on-value.

        Line numbers use ``-1`` for "not applicable" (folder/file rows).
        """
        payload: dict[str, Any] = {
            "name": item.name or "",
            "type": item.type.value if hasattr(item.type, "value") else str(item.type),
            "kind": item.kind or "",
            "entity_type": item.entity_type or "",
            "parent_id": item.parent_id or "",
            "file_extension": item.file_extension or "",
            "repository_id": item.repository_id or "",
            "path": item.path or "",
            "archived": bool(getattr(item, "archived", False)),
            "timestamp": item.timestamp or "",
            "token_count": int(item.token_count or 0),
            "line_from": self._line_to_int(item.line_from),
            "line_to": self._line_to_int(item.line_to),
            "needs_refactoring": bool(item.needs_refactoring)
            if item.needs_refactoring is not None
            else False,
        }
        for field in self.QUALITY_CATEGORICAL_FIELDS:
            payload[field] = getattr(item, field, None) or ""
        for field in self.LIST_FIELDS:
            values = getattr(item, field, None) or []
            payload[field] = self._encode_list(values)
        return ChromaRowMetadata.model_validate(payload)

    def flatten_chunk_row(self, item: CodeIndexItem, chunk_index: int) -> ChromaChunkRow:
        """Build the per-chunk metadata row for one chunk of ``item``.

        Replaces the ad-hoc dict literal that used to live inside
        ``CodeIndex.add_item``.
        """
        return ChromaChunkRow(
            parent_doc_id=item.item_id,
            chunk_index=chunk_index,
            name=item.name or "",
            type=item.type.value if hasattr(item.type, "value") else str(item.type),
            kind=item.kind or "",
            path=item.path or "",
            file_extension=item.file_extension or "",
            repository_id=item.repository_id or "",
        )

    # ── Read path ───────────────────────────────────────────────────

    def parse(
        self,
        item_id: str,
        meta: ChromaRowMetadata | dict[str, Any] | None,
        sha: str,
        *,
        content: str = "",
        score: float | None = None,
        chunk_preview: str | None = None,
    ) -> CodeIndexResult:
        """Build a :class:`CodeIndexResult` from one chroma row.

        Centralized so ``search`` / ``filter_items`` / ``get_item`` all
        return the same pydantic shape.
        """
        if isinstance(meta, ChromaRowMetadata):
            source: dict[str, Any] = meta.model_dump()
        else:
            source = dict(meta or {})

        payload: dict[str, Any] = {
            "item_id": item_id,
            "name": source.get("name", ""),
            "type": source.get("type", ""),
            "kind": source.get("kind", ""),
            "entity_type": source.get("entity_type", ""),
            "path": source.get("path", ""),
            "file_extension": source.get("file_extension", ""),
            "repository_id": source.get("repository_id", ""),
            "parent_id": source.get("parent_id", ""),
            "archived": bool(source.get("archived", False)),
            "timestamp": source.get("timestamp", ""),
            "token_count": int(source.get("token_count", 0) or 0),
            "line_from": self._int_to_line(source.get("line_from")),
            "line_to": self._int_to_line(source.get("line_to")),
            "needs_refactoring": bool(source.get("needs_refactoring", False)),
            "commit": sha,
            "content": content,
            "score": score,
            "chunk_preview": chunk_preview,
        }
        for field in self.QUALITY_CATEGORICAL_FIELDS:
            payload[field] = source.get(field, "")
        for field in self.LIST_FIELDS:
            payload[field] = self._decode_list(source.get(field))
        return CodeIndexResult.model_validate(payload)

    # ── List encoding ───────────────────────────────────────────────

    def _encode_list(self, values: Iterable[str]) -> str:
        """``["a", "b"]`` → ``"\x1fa\x1fb\x1f"`` for $contains exact match.

        Empty list → ``""`` (so ``$contains`` against any value misses cleanly).
        """
        parts = [str(v) for v in values if v]
        if not parts:
            return ""
        return self.LIST_SEP + self.LIST_SEP.join(parts) + self.LIST_SEP

    def _decode_list(self, encoded: Any) -> list[str]:
        if not encoded:
            return []
        text = str(encoded)
        return [part for part in text.split(self.LIST_SEP) if part]

    # ── Line-number sentinels ───────────────────────────────────────

    @staticmethod
    def _line_to_int(value: int | None) -> int:
        """Encode ``None`` as the ``-1`` sentinel chroma actually stores."""
        return int(value) if value is not None else -1

    @staticmethod
    def _int_to_line(value: Any) -> int | None:
        """Decode the chroma sentinel for "no line range".

        ``-1`` (or any negative value) means "not applicable"; convert
        back to ``None`` so consumers see a clean nullable.
        """
        if value is None:
            return None
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None
