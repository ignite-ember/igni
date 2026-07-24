"""File-to-file reference model — stored in SQLite, not the vector store.

Used to express custom code relationships (imports, calls, extends, etc.)
between two indexed items. ``relation`` is the canonical edge kind —
indexed in the database so filtering by relation is a real index lookup.

``meta`` is intentionally typed as ``dict`` (not a Pydantic model) so
callers can introduce new per-relation metadata without schema
migrations — same no-migration contract as
:class:`CommitMetadataEntry.value`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.code_index.enums import Relation


class FileReference(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_uuid: str
    to_uuid: str
    relation: Relation
    meta: dict = Field(default_factory=dict)
