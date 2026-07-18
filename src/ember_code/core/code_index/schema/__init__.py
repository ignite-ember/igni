"""Public re-export surface for ``ember_code.core.code_index.schema``.

Domain Pydantic models live in sibling files (branches, chroma_row,
commit_metadata, file_reference, items, manifest, queries, stats,
where_filter). Wire-format coercion lives in :mod:`.wire`. The clock
collaborator lives in :mod:`.manifest` (the only consumer is
manifest's own mutators); this ``__init__`` re-exports it alongside
``wire`` so callers can ``from ember_code.core.code_index.schema import Clock``.
"""

from __future__ import annotations

from ember_code.core.code_index.schema.manifest import Clock, SystemClock
from ember_code.core.code_index.schema.wire import JsonSafe, WeaviateWireCodec

__all__ = [
    "Clock",
    "JsonSafe",
    "SystemClock",
    "WeaviateWireCodec",
]
