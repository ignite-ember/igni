"""Run agent-authored Cypher against the artifact store, safely.

Three jobs, in order:

1. **Refuse anything that writes.** Delegated to
   :func:`assert_read_only_cypher`, the same guard the code index
   uses — the rules are about Cypher, not about which graph it runs
   against, and a second copy would drift.
2. **Cap what comes back.** A panel renders the result; an unbounded
   query would stream a whole corpus into a browser. The cap is
   applied here rather than trusted to the query's own ``LIMIT``,
   because the query was written by a model.
3. **Project a graph.** The rows answer the question; the graph shows
   the shape of the answer. Both come from one execution — running the
   query twice to get a second view would double the cost and could
   return two different answers if the store changed in between.

The graph projection is deliberately built from whatever the rows
happen to contain rather than from a second structural query. A result
with ``dependent`` and ``source`` columns draws an edge between them; a
result with one name column draws unconnected nodes. That means the
picture always matches the table, including when the query is odd.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from neo4j import AsyncDriver

from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.graph import DEFAULT_DATABASE
from ember_code.core.graph.cypher_guard import (
    CypherGuardError,
    assert_read_only_cypher,
)

logger = logging.getLogger(__name__)

# Rows past this point are a scroll nobody reads and a payload the
# WebSocket has to carry. The query's own LIMIT usually bites first;
# this is the backstop for when the model forgets one.
MAX_ROWS: int = 200

# Long text in a cell makes every other column unreadable. The full
# content is one click away — the panel links each row to its
# document — so the table shows enough to recognise it by.
MAX_CELL_CHARS: int = 600


@dataclass
class GraphNode:
    """One node in the picture drawn beside the results."""

    id: str
    label: str
    kind: str = "artifact"
    version: int | None = None

    def key(self) -> str:
        return self.id


@dataclass
class GraphEdge:
    """A relationship between two rendered nodes."""

    source: str
    target: str
    label: str = ""
    stale: bool = False


@dataclass
class GraphView:
    """The nodes and edges a result implies.

    Empty when the rows carry nothing identifiable — a query returning
    only counts has no graph, and drawing an empty canvas is more
    honest than inventing one node per row.
    """

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.nodes


@dataclass
class CypherResult:
    """What a run produced, including why it failed."""

    cypher: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    graph: GraphView = field(default_factory=GraphView)
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# Column names that identify a document. Ordered: the first one
# present wins, so a query returning both ``name`` and ``dependent``
# labels its nodes by the more specific column.
_SUBJECT_COLUMNS: tuple[str, ...] = (
    "dependent",
    "affected",
    "document",
    "artifact",
    "name",
    "title",
)

# Column names that identify the *other* end of a relationship.
_OBJECT_COLUMNS: tuple[str, ...] = (
    "source",
    "possible_source",
    "built_on",
    "derived_from",
    "target",
)

_ID_COLUMNS: tuple[str, ...] = ("artifact_id", "id")


class ArtifactCypherService:
    """Executes validated read-only Cypher against one project's store."""

    def __init__(
        self,
        driver: AsyncDriver,
        project_id: str,
        *,
        embedder: Any | None = None,
        max_rows: int = MAX_ROWS,
        codec: ArtifactMetadataCodec | None = None,
    ) -> None:
        self._driver = driver
        self._project_id = project_id
        self._embedder = embedder
        self._max_rows = max(1, max_rows)
        self._codec = codec or ArtifactMetadataCodec()

    async def run(
        self,
        cypher: str,
        *,
        params: dict[str, Any] | None = None,
        semantic_query: str = "",
    ) -> CypherResult:
        """Validate, execute and project.

        Failures come back as a populated ``error`` rather than an
        exception: every caller here is a UI or an agent that needs to
        show the reason and let the author try again, and neither
        benefits from a traceback.
        """
        try:
            validated = assert_read_only_cypher(cypher)
        except CypherGuardError as exc:
            return CypherResult(cypher=cypher, error=str(exc))

        bound: dict[str, Any] = dict(params or {})
        bound.setdefault("proj", self._project_id)
        if semantic_query:
            vector = self._embed(semantic_query)
            if vector is None:
                return CypherResult(
                    cypher=validated,
                    error=(
                        "This query needs $query_vector, but no embedder is "
                        "available in this session."
                    ),
                )
            bound["query_vector"] = vector
            bound["semantic_query"] = semantic_query

        try:
            records, columns = await self._execute(validated, bound)
        except Exception as exc:  # the database's complaint is the useful part
            logger.debug("artifact cypher failed: %s", exc)
            return CypherResult(cypher=validated, error=_readable(exc))

        truncated = len(records) > self._max_rows
        rows = [_clean_row(r) for r in records[: self._max_rows]]
        return CypherResult(
            cypher=validated,
            columns=columns,
            rows=rows,
            graph=self.project(rows),
            truncated=truncated,
        )

    async def _execute(
        self, cypher: str, params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        async with self._driver.session(database=DEFAULT_DATABASE) as session:
            result = await session.run(cypher, **params)
            eager = await result.to_eager_result()
        records = [dict(r) for r in eager.records]
        return records, list(eager.keys)

    def _embed(self, text: str) -> list[float] | None:
        if self._embedder is None:
            return None
        try:
            return list(self._embedder.embed([text])[0])
        except Exception as exc:  # pragma: no cover — embedder contract
            logger.debug("embedding the query failed: %s", exc)
            return None

    # ── Graph projection ──────────────────────────────────────────────

    def project(self, rows: list[dict[str, Any]]) -> GraphView:
        """Turn result rows into something drawable.

        A row with a subject and an object becomes two nodes and an
        edge; a row with only a subject becomes one node. Rows with
        neither contribute nothing, which is why a ``RETURN count(*)``
        draws no picture instead of one mystery dot.
        """
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []

        for row in rows:
            subject = _first_present(row, _SUBJECT_COLUMNS)
            obj = _first_present(row, _OBJECT_COLUMNS)
            if subject is None and obj is None:
                continue

            subject_node = self._node_for(row, subject, primary=True)
            if subject_node is not None:
                nodes.setdefault(subject_node.key(), subject_node)
            object_node = self._node_for(row, obj, primary=False)
            if object_node is not None:
                nodes.setdefault(object_node.key(), object_node)

            if subject_node is not None and object_node is not None:
                edges.append(
                    GraphEdge(
                        source=subject_node.key(),
                        target=object_node.key(),
                        label=_edge_label(row),
                        stale=_looks_stale(row),
                    )
                )
        return GraphView(nodes=list(nodes.values()), edges=edges)

    def _node_for(
        self, row: dict[str, Any], label: str | None, *, primary: bool
    ) -> GraphNode | None:
        if not label:
            return None
        # The id, when the query returned one, so two rows naming the
        # same document collapse to one node even if one of them spelled
        # the name differently. Falling back to the label keeps a query
        # that forgot to return an id drawable.
        identifier = _first_present(row, _ID_COLUMNS) if primary else None
        version = _first_int(row, ("version", "n", "now_at") if primary else ("pinned_to",))
        return GraphNode(
            id=str(identifier or label),
            label=str(label),
            kind="artifact" if primary else "source",
            version=version,
        )


# ── Row helpers ───────────────────────────────────────────────────────


def _first_present(row: dict[str, Any], names: tuple[str, ...]) -> Any | None:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _first_int(row: dict[str, Any], names: tuple[str, ...]) -> int | None:
    value = _first_present(row, names)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _edge_label(row: dict[str, Any]) -> str:
    pinned = row.get("pinned_to")
    now = row.get("now_at")
    if pinned is not None and now is not None:
        return f"v{pinned} → v{now}"
    if pinned is not None:
        return f"v{pinned}"
    state = row.get("state")
    return str(state) if state else ""


def _looks_stale(row: dict[str, Any]) -> bool:
    """Whether this row describes a pin the source has moved past.

    Read from the row rather than re-queried: the staleness example in
    the schema returns exactly these two columns, so a query following
    it gets the highlighting for free, and one that does not simply
    draws a plain edge.
    """
    try:
        return int(row["pinned_to"]) < int(row["now_at"])
    except (KeyError, TypeError, ValueError):
        return False


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    """Make a record JSON-safe and readable in a table cell."""
    return {str(k): _clean_value(v) for k, v in row.items()}


def _clean_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_clean_value(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k): _clean_value(v) for k, v in value.items()}
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + f"… (+{len(text) - MAX_CELL_CHARS} chars)"
    return text


def _readable(exc: Exception) -> str:
    """The database's complaint, without the stack of wrappers.

    Neo4j's messages name the offending clause and are the most useful
    thing an author can be given; the driver's class name is not.
    """
    message = str(exc).strip()
    return message.splitlines()[0] if message else exc.__class__.__name__
