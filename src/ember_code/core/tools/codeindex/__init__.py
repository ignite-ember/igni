"""CodeIndexTools — agent-facing structured query over the local code index.

The package layout:

  - :mod:`tool` — :class:`CodeIndexTools`, the agno toolkit entry point
  - :mod:`query_service` — :class:`QueryService`, owns ``codeindex_query``
  - :mod:`tree_service` — :class:`TreeService`, owns ``codeindex_tree``
  - :mod:`disambiguation` — :class:`DisambiguationService`, the
    reference-graph re-ranking that surfaces alongside query results
  - :mod:`schemas` — Pydantic models for input filters and output envelopes
  - :mod:`filters` — section-block filtering, where-clause builder,
    shared constants
  - :mod:`empty_guard` — the no-narrowing-input check

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

from ember_code.core.tools.codeindex.filters import build_where as _build_where
from ember_code.core.tools.codeindex.schemas import (
    ErrorResponse,
    ItemsResponse,
    _CategoricalFilters,
)
from ember_code.core.tools.codeindex.tool import CodeIndexTools

__all__ = [
    "CodeIndexTools",
    "ErrorResponse",
    "ItemsResponse",
    # Test-suite re-exports — kept for backward compatibility.
    "_CategoricalFilters",
    "_build_where",
]
