"""Query input/output models for code_index search.

In-process callers pass native Weaviate ``Filter`` and ``Sort`` objects
directly — no pickle/base64 transport encoding (which the cloud variant
needed for HTTP).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from weaviate.collections.classes.filters import (
    _FilterAnd,
    _FilterOr,
    _Filters,
    _FilterValue,
    _Operator,
)
from weaviate.collections.classes.grpc import _Sorting


class QueryResponse(BaseModel):
    items: list[Any]
    limit: int | None = None
    offset: int | None = None


class Query(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    near_text: str | None = None
    near_id: str | None = None
    target_vector: str = "default"
    limit: int = 100
    offset: int | None = None
    distance: float = 0.75
    filters: _Filters | None = None
    sort: _Sorting | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)

    def extract_tag_filter(self) -> tuple[list[str] | None, bool]:
        """Walk ``filters`` and pull out a ``tags`` predicate, if any.

        Returns ``(tags, match_all)`` — ``match_all=True`` means
        ``CONTAINS_ALL``, otherwise ``CONTAINS_ANY``. Used by the
        reference-resolution path so SQLite-side reference queries can
        honor the same tag filter the user applied to Weaviate.
        """
        if self.filters is None:
            return None, False

        def walk(f: Any) -> tuple[list[str] | None, bool]:
            if isinstance(f, _FilterValue):
                target_str = str(f.target) if not isinstance(f.target, str) else f.target
                if "tags" in target_str.lower():
                    if f.operator == _Operator.CONTAINS_ANY:
                        value = list(f.value) if isinstance(f.value, (list, tuple)) else [f.value]
                        return value, False
                    if f.operator == _Operator.CONTAINS_ALL:
                        value = list(f.value) if isinstance(f.value, (list, tuple)) else [f.value]
                        return value, True
                return None, False
            if isinstance(f, (_FilterAnd, _FilterOr)):
                for sub in f.filters:
                    result, match_all = walk(sub)
                    if result is not None:
                        return result, match_all
            return None, False

        return walk(self.filters)
