"""Typed ``where`` filter for CodeIndex reads.

The chroma metadata filter is a nested dict — ``{"$and": [{"field": v},
{"field2": {"$in": [...]}}]}``. Callers used to build that dict by hand
and pass it as ``dict[str, Any]`` into :meth:`CodeIndex.search` /
:meth:`CodeIndex.filter_items`. That leaks the chroma shape into every
caller and gives no type-checker any leverage.

:class:`ChromaWhereFilter` is a Pydantic surface over the same shape.
Callers construct one via the class-methods (:meth:`equal`,
:meth:`in_ids`, :meth:`and_`) or by populating the fields directly,
and :meth:`to_chroma_where` renders the dict that chroma actually
accepts. The rendering is byte-identical to what
:func:`build_where` used to hand-build.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChromaWhereFilter(BaseModel):
    """A composable, typed chroma metadata ``where`` filter.

    Three field families, combined via top-level ``$and`` at the chroma
    boundary:

    - :attr:`equals` — exact-match single-value predicates.
    - :attr:`in_` — ``$in`` predicates for enum-multi values.
    - :attr:`contains` — ``$contains`` predicates for the
      ``\\x1f``-bracketed list columns.
    """

    model_config = ConfigDict(extra="forbid")

    equals: dict[str, str | int | bool] = Field(default_factory=dict)
    in_: dict[str, list[str]] = Field(default_factory=dict)
    contains: dict[str, str] = Field(default_factory=dict)

    # ── Construction helpers ────────────────────────────────────────

    @classmethod
    def equal(cls, field: str, value: str | int | bool) -> ChromaWhereFilter:
        """Single-clause equality filter."""
        return cls(equals={field: value})

    @classmethod
    def in_ids(cls, field: str, values: list[str]) -> ChromaWhereFilter:
        """Single-clause ``$in`` filter."""
        return cls(in_={field: list(values)})

    @classmethod
    def and_(cls, *filters: ChromaWhereFilter | None) -> ChromaWhereFilter | None:
        """Merge multiple filters at top level. ``None`` inputs are skipped."""
        merged = cls()
        any_populated = False
        for f in filters:
            if f is None:
                continue
            any_populated = True
            for k, v in f.equals.items():
                merged.equals[k] = v
            for k, vs in f.in_.items():
                merged.in_[k] = list(vs)
            for k, v in f.contains.items():
                merged.contains[k] = v
        if not any_populated:
            return None
        return merged

    # ── Rendering ───────────────────────────────────────────────────

    def is_empty(self) -> bool:
        return not (self.equals or self.in_ or self.contains)

    def to_chroma_where(self) -> dict[str, Any] | None:
        """Render to the dict shape ``chromadb`` accepts.

        Returns ``None`` when the filter is empty — chroma rejects
        ``where={}`` and callers use ``None`` to skip the argument.
        """
        clauses: list[dict[str, Any]] = []
        for field, value in self.equals.items():
            clauses.append({field: value})
        for field, values in self.in_.items():
            if not values:
                continue
            if len(values) == 1:
                clauses.append({field: values[0]})
            else:
                clauses.append({field: {"$in": list(values)}})
        for field, value in self.contains.items():
            clauses.append({field: {"$contains": value}})

        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}
