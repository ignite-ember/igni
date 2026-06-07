"""File-to-file reference model — stored in SQLite, not the vector store.

Used to express custom code relationships (imports, calls, extends, etc.)
between two indexed items. ``relation`` is the canonical edge kind —
indexed in the database so filtering by relation is a real index lookup.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FileReference(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_uuid: str
    to_uuid: str
    relation: str
    meta: dict = Field(default_factory=dict)
