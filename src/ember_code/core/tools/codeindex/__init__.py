"""CodeIndexTools — agent-facing structured query over the local code index.

The package layout:

  - :mod:`tool` — :class:`CodeIndexTools`, the agno toolkit entry point
  - :mod:`services` — :class:`CodeIndexServices`, lifecycle owner for
    the shared :class:`CodeIndex` + its two service wrappers
  - :mod:`telemetry` — :class:`TelemetryLog`, best-effort JSON-lines
    sink for eval telemetry (``EMBER_EVAL_TELEMETRY_PATH``)
  - :mod:`invocation` — :class:`ToolInvocationRecorder`, timing +
    serialization + telemetry + error-wrap for tool method calls
  - :mod:`query_service` — :class:`QueryService`, owns ``codeindex_query``
  - :mod:`tree_service` — :class:`TreeService`, owns ``codeindex_tree``
  - :mod:`tree_builder` — :class:`TreeBuilder`, ancestor walk + forest
    assembly for query responses (extracted from :class:`QueryService`
    so the service reads as three named phases)
  - :mod:`disambiguation` — :class:`DisambiguationService`, the
    reference-graph re-ranking that surfaces alongside query results
  - :mod:`schemas` — Pydantic models for input (``QueryInput``,
    ``TreeInput``, ``RenderedRow``, ``TelemetryEntry``) and output
    (``ItemsResponse``, ``ErrorResponse``). :class:`_CategoricalFilters`
    owns :meth:`to_where` (chroma metadata filter translation).
  - :mod:`serializer` — :class:`JsonSerializer`, the JSON boundary
    the toolkit uses to render typed responses to strings
  - :mod:`section_markup` — :class:`SectionMarkup`, value-object
    over one item's ``content`` string with :meth:`shorten` and
    :meth:`keep` (section filter) operations
  - :mod:`test_paths` — :class:`TestPathClassifier`, path-based
    test-file detection used by ``codeindex_query`` default exclusion
  - :mod:`empty_guard` — thin back-compat shim over
    :meth:`QueryInput.is_empty_call`

The toolkit takes typed enum args and translates them into chroma
where-clauses internally; agents never write raw chroma queries.
Every quality dimension is a typed enum, every multi-value category
is a list of strings; combining them in one call is exact-match-ANDed
across categories and OR-within a single multi-value category.

Why structured-only (no ``where=`` escape hatch):

  - The agent doesn't have to know chroma operators (``$and`` / ``$or`` /
    ``$contains`` / ``$in``) or the ``\\x1f``-bracketed encoding of list
    fields — both stay internal.
  - Schema or storage changes don't break prompts; only the tool internals
    move.
  - Wrong field values fail fast at the SDK level (enum constraint), not
    silently with empty results.

Public re-exports below preserve the historical import path
``from ember_code.core.tools.codeindex import CodeIndexTools`` (and a
few helpers used by tests).
"""

from ember_code.core.code_index.schema.where_filter import ChromaWhereFilter
from ember_code.core.tools.codeindex.schemas import (
    ErrorResponse,
    ItemsResponse,
    QueryInput,
    RenderedRow,
    _CategoricalFilters,
)
from ember_code.core.tools.codeindex.tool import CodeIndexTools


def _build_where(filters: _CategoricalFilters) -> ChromaWhereFilter | None:
    """Back-compat adapter for the historical free-function API.

    :meth:`_CategoricalFilters.to_where` now owns the translation of
    the filter envelope into a chroma where-clause — behavior on the
    model itself, not a free function. This wrapper preserves the
    ``from ember_code.core.tools.codeindex import _build_where``
    import path used by ``tests/test_codeindex_tools.py`` so the
    transition doesn't need synchronised test edits.
    """
    return filters.to_where()


__all__ = [
    "CodeIndexTools",
    "ErrorResponse",
    "ItemsResponse",
    "QueryInput",
    "RenderedRow",
    # Test-suite re-exports — kept for backward compatibility.
    "_CategoricalFilters",
    "_build_where",
]
