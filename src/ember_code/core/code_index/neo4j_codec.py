"""Codec for the CodeIndex ↔ Neo4j node-property wire shape.

Field list kept as class-level tuples so write/read stay in
lockstep. The wire shape is much closer to the Pydantic model:

* **Lists are lists.** Chroma's metadata API rejected ``None`` and
  lists, forcing us into ``\\x1f``-bracketed strings. Neo4j stores
  list properties natively, so the multi-value categories
  (``vulnerabilities``, ``frameworks``, ...) land as actual lists
  on the node.
* **Nulls are nulls.** The ``""`` / ``-1`` / ``False`` sentinels
  used to dodge Chroma's None-rejection are gone. A missing
  ``quality`` is a null property; a missing line range is ``null``.
* **One source of truth.** Both :meth:`to_node_params` (write) and
  :meth:`from_node` (read) iterate the same class-level field
  tuples, so a new field added to :class:`CodeIndexItem` propagates
  to both ends without a parallel edit.

:class:`Neo4jRowCodec` is intentionally not a singleton — callers
compose one into the :class:`Neo4jClient` (mirroring how
:class:`ChromaRowCodec` was composed into :class:`CodeIndex`).
"""

from __future__ import annotations

from typing import Any

from ember_code.core.code_index.schema.items import CodeIndexItem, CodeIndexResult


class Neo4jRowCodec:
    """Encode/decode :class:`CodeIndexItem` ↔ Neo4j node properties.

    The codec is stateless; the only state is the field-name tuples
    shared by the read and write paths.
    """

    # Quality categorical fields — stored as enum strings, or null
    # when not assessed.
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

    # Multi-value list fields — stored as native lists on the node.
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

    # Identity / scope fields — exact-match strings, null when unknown.
    IDENTITY_FIELDS: tuple[str, ...] = (
        "name",
        "type",
        "kind",
        "entity_type",
        "parent_id",
        "file_extension",
        "repository_id",
        "path",
    )

    # ── Write path ──────────────────────────────────────────────────

    def to_node_params(self, item: CodeIndexItem, *, content: str | None = None) -> dict[str, Any]:
        """Pack a :class:`CodeIndexItem` into a dict for Cypher params.

        Returns a flat dict of node-property values, ready to pass
        as ``$props`` in a ``MERGE (i:Item {item_id: $item_id}) SET
        i += $props`` style statement. ``None`` is preserved as
        ``None`` (Neo4j stores null properties cleanly); empty
        strings are preserved as empty strings (kept distinct
        from null so callers can detect "explicitly empty" vs
        "unset").

        ``content`` is split out because it lives in the
        ``document`` column of the chroma equivalent — Neo4j
        models it as a regular ``content`` property on the
        :class:`Item` node (no separate document collection).
        """
        params: dict[str, Any] = {
            "item_id": item.item_id,
            "name": item.name,
            "type": item.type.value if hasattr(item.type, "value") else item.type,
            "kind": item.kind,
            "entity_type": item.entity_type,
            "parent_id": item.parent_id,
            "file_extension": item.file_extension,
            "repository_id": item.repository_id,
            "path": item.path,
            "archived": bool(getattr(item, "archived", False)),
            "timestamp": item.timestamp,
            "token_count": int(item.token_count) if item.token_count is not None else None,
            "line_from": int(item.line_from) if item.line_from is not None else None,
            "line_to": int(item.line_to) if item.line_to is not None else None,
            "needs_refactoring": bool(item.needs_refactoring)
            if item.needs_refactoring is not None
            else False,
            "content": content if content is not None else item.content,
        }
        for field in self.QUALITY_CATEGORICAL_FIELDS:
            params[field] = getattr(item, field, None)
        for field in self.LIST_FIELDS:
            values = getattr(item, field, None) or []
            params[field] = list(values)
        return params

    def chunk_params(
        self,
        *,
        chunk_id: str,
        parent_id: str,
        chunk_index: int,
        text: str,
        embedding: list[float],
        item: CodeIndexItem,
    ) -> dict[str, Any]:
        """Build a chunk node's property dict.

        Chunks carry a narrow subset of the parent item's fields
        (enough to display a chunk in search results without
        joining back). The embedding lives directly on the chunk
        node; the vector index is on ``Chunk(embedding)``.
        """
        return {
            "chunk_id": chunk_id,
            "parent_id": parent_id,
            "chunk_index": chunk_index,
            "text": text,
            "embedding": embedding,
            "name": item.name,
            "type": item.type.value if hasattr(item.type, "value") else item.type,
            "kind": item.kind,
            "path": item.path,
            "file_extension": item.file_extension,
            "repository_id": item.repository_id,
        }

    # ── Read path ───────────────────────────────────────────────────

    def from_node(
        self,
        item_id: str,
        props: dict[str, Any] | None,
        *,
        content: str = "",
        score: float | None = None,
        chunk_preview: str | None = None,
        commit: str = "",
    ) -> CodeIndexResult:
        """Build a :class:`CodeIndexResult` from one node's properties.

        Per-process isolation: each Neo4j process holds exactly one
        commit's data. ``commit`` is session-derived and echoed
        by callers rather than stored on nodes.
        """
        source: dict[str, Any] = dict(props or {})
        payload: dict[str, Any] = {
            "item_id": item_id,
            "name": source.get("name") or "",
            "type": str(source.get("type") or ""),
            "kind": source.get("kind") or "",
            "entity_type": source.get("entity_type") or "",
            "path": source.get("path") or "",
            "file_extension": source.get("file_extension") or "",
            "repository_id": source.get("repository_id") or "",
            "parent_id": source.get("parent_id") or "",
            "archived": bool(source.get("archived", False)),
            "timestamp": source.get("timestamp") or "",
            "token_count": int(source.get("token_count") or 0),
            "line_from": source.get("line_from"),
            "line_to": source.get("line_to"),
            "needs_refactoring": bool(source.get("needs_refactoring", False)),
            "content": content or source.get("content") or "",
            "score": score,
            "chunk_preview": chunk_preview,
            "commit": commit,
        }
        for field in self.QUALITY_CATEGORICAL_FIELDS:
            payload[field] = source.get(field) or ""
        for field in self.LIST_FIELDS:
            value = source.get(field)
            payload[field] = list(value) if isinstance(value, list) else []
        return CodeIndexResult.model_validate(payload)
