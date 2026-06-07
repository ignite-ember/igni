"""Pydantic models for the knowledge system."""

from pydantic import BaseModel, Field


class KnowledgeAddResult(BaseModel):
    """Result of adding content to the knowledge base."""

    success: bool = True
    message: str = ""
    error: str | None = None

    @classmethod
    def ok(cls, message: str) -> "KnowledgeAddResult":
        return cls(success=True, message=message)

    @classmethod
    def fail(cls, error: str) -> "KnowledgeAddResult":
        return cls(success=False, error=error)


class KnowledgeSearchResult(BaseModel):
    """A single search result from the knowledge base."""

    content: str = ""
    name: str = ""
    score: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class KnowledgeSearchResponse(BaseModel):
    """Collection of search results."""

    query: str
    results: list[KnowledgeSearchResult] = Field(default_factory=list)
    total: int = 0


class KnowledgeStatus(BaseModel):
    """Current status of the knowledge base."""

    enabled: bool = False
    collection_name: str = ""
    document_count: int = 0
    embedder: str = ""


class KnowledgeSyncResult(BaseModel):
    """Result of a knowledge sync operation."""

    direction: str = ""  # "file_to_db" or "db_to_file"
    new_entries: int = 0
    existing_entries: int = 0
    total_entries: int = 0
    message: str = ""
    error: str | None = None

    @property
    def summary(self) -> str:
        if self.error:
            return f"Sync error: {self.error}"
        if self.new_entries == 0:
            return f"Already in sync ({self.total_entries} entries)"
        return (
            f"Synced {self.new_entries} new entries "
            f"({self.existing_entries} existing, {self.total_entries} total)"
        )
