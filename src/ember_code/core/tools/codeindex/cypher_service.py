"""Read-only Cypher escape hatch for agents who can author Cypher.

Wraps :meth:`Neo4jClient.execute_query` so that the toolkit's
``codeindex_cypher`` method has a single seam: this service owns
the routing through the per-commit client, the cap on result
rows, and the json-safe serialisation of :class:`neo4j._Node`
and :class:`neo4j._Record` values that come back from the
driver.

The hard read-only validator lives at
:mod:`ember_code.core.tools.codeindex.cypher_guard`. This
module assumes the input already passed the guard — the
toolkit runs :func:`assert_read_only_cypher` BEFORE invoking
``run``.
"""

from __future__ import annotations

import logging
from typing import Any

from ember_code.core.code_index.index import CodeIndex
from ember_code.core.tools.codeindex.schemas import CypherResponse

logger = logging.getLogger(__name__)


class CypherService:
    """Owns the Cypher execution route used by the agent-facing tool.

    Built once per toolkit. Lazy until the first ``run`` call:
    the underlying :class:`CodeIndex` doesn't open any disk or
    driver until :meth:`CodeIndex.client_for` is awaited, so
    constructing this object is free.
    """

    def __init__(self, index: CodeIndex) -> None:
        self._index = index

    async def run(
        self,
        *,
        cypher: str,
        params: dict[str, Any],
        limit: int,
        commit: str | None,
    ) -> str:
        """Execute a pre-validated Cypher query and return a JSON envelope.

        Args:
            cypher: read-only Cypher that has already passed
                :func:`cypher_guard.assert_read_only_cypher`.
            params: typed parameter dict (only names on the
                ``ALLOWED_PARAM_NAMES`` allowlist were permitted).
            limit: cap on rows returned to the agent.
            commit: optional commit SHA; defaults to head.

        Returns: a :class:`CypherResponse` envelope carrying the
        rows + row_count + truncated flag + the limit that was
        applied. The toolkit's :class:`ToolInvocationRecorder`
        serialises via :class:`JsonSerializer` — never call this
        directly to wire up; route through ``self._recorder.invoke``
        so telemetry + error-wrap shape stays consistent with
        the other codeindex tools.
        """
        # The toolkit pre-routes the cypher through the guardrail,
        # so by the time we get here the only remaining error
        # surface is the driver.
        try:
            client = await self._index.client_for(commit)
        except Exception as exc:
            logger.warning("client_for failed: %s", exc)
            return _err(str(exc), kind="client_for_failed")
        if client is None:
            return _err(
                "No Neo4j backend available — ensure CodeIndex is "
                "configured with a runtime= or neo4j_client=.",
                kind="no_backend",
            )
        scoped_params: dict[str, Any] = {"proj": self._index.project_id, **params}
        try:
            rows = await client.execute_query(cypher, **scoped_params)
        except Exception as exc:
            logger.warning("execute_query failed: %s", exc)
            return _err(str(exc), kind="cypher_failed")
        # Cap.
        truncated = len(rows) > limit
        rows = rows[:limit]
        return CypherResponse(
            rows=[_serialise_row(r) for r in rows],
            row_count=len(rows),
            truncated=truncated,
            limit=limit,
            commit=commit,
        )


def _serialise_row(record: Any) -> dict[str, Any]:
    """Convert one :class:`neo4j._Record` to a json-safe dict.

    :class:`neo4j._Node` and :class:`neo4j._Relationship` need
    ``dict(...)`` to surface the property bag; everything else we
    leave to ``str()`` so an unexpected node / scalar still comes
    back to the agent rather than silently dropping the row.
    """
    if hasattr(record, "items"):
        out: dict[str, Any] = {}
        for key, value in record.items():
            out[key] = _serialise_value(value)
        return out
    return {"value": _serialise_value(record)}


def _serialise_value(value: Any) -> Any:
    """Recursively serialise one node / list / scalar value."""
    # Nodes and relationships expose the property bag via ``dict``.
    try:
        # Avoid a hard import on the neo4j module — duck-type.
        # Plain mapping: dict / Mapping (no node ``labels`` attr).
        if (
            hasattr(value, "keys")
            and hasattr(value, "__iter__")
            and hasattr(value, "__getitem__")
            and not hasattr(value, "labels")
        ):
            return {k: _serialise_value(v) for k, v in value.items()}
    except Exception:
        pass
    # Fallback: stringify anything not natively json-serialisable.
    try:
        import json

        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _err(message: str, kind: str) -> Any:
    from ember_code.core.tools.codeindex.schemas import ErrorResponse

    return ErrorResponse(error=kind, message=message)
