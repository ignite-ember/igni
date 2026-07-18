"""Wire formats for ``code_index_commit_metadata`` reads and writes.

The table is a generic key-addressed store: ``key`` is a free string
column and ``value`` is a JSON blob whose shape is owned by the caller
for that key. The ``CommitMetadataEntry`` / ``CommitMetadataCreate``
models here pin the row schema; the per-key value shape is deliberately
left as a plain ``dict`` so adding a new key never requires a schema
migration.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# -- Entry & wire formats ------------------------------------------------------


class CommitMetadataEntry(BaseModel):
    """Persisted row shape — what callers see when they read."""

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    commit_sha: str
    key: str
    value: dict = Field(default_factory=dict)


class CommitMetadataCreate(BaseModel):
    """Single-row upsert payload."""

    item_id: str
    commit_sha: str
    key: str
    value: dict = Field(default_factory=dict)


class CommitMetadataBulkItem(BaseModel):
    """One row inside a bulk upsert."""

    item_id: str
    value: dict = Field(default_factory=dict)


class CommitMetadataBulkCreate(BaseModel):
    """Bulk upsert payload — commit_sha + key live outside the per-item loop."""

    commit_sha: str
    key: str
    items: list[CommitMetadataBulkItem]
