"""Commit-scoped metadata for indexed items, persisted in SQLite.

Used to track per-commit data that would change as the codebase evolves
without requiring a re-index — primarily ``line_from`` / ``line_to``
ranges that can shift across commits.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CommitMetadataEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_id: str
    commit_sha: str
    key: str
    value: dict = Field(default_factory=dict)


class CommitMetadataCreate(BaseModel):
    item_id: str
    commit_sha: str
    key: str
    value: dict = Field(default_factory=dict)


class CommitMetadataBulkItem(BaseModel):
    item_id: str
    value: dict = Field(default_factory=dict)


class CommitMetadataBulkCreate(BaseModel):
    commit_sha: str
    key: str
    items: list[CommitMetadataBulkItem]
