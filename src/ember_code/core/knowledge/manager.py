"""Factory for :class:`KnowledgeIndex` instances scoped per project."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ember_code.core.config.settings import Settings
from ember_code.core.knowledge.index import KnowledgeIndex

logger = logging.getLogger(__name__)


class KnowledgeManager:
    """Build a :class:`KnowledgeIndex` for the active session's project."""

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        neo4j_client: Any | None = None,
        embedder: Any | None = None,
    ):
        self.settings = settings
        self._project_dir = project_dir or Path.cwd()
        # Optional neo4j client (typically the project's
        # ``Neo4jKnowledgeClient`` constructed by the BE from
        # the runtime's knowledge driver). When supplied, the
        # knowledge index is built on neo4j instead of chroma.
        self._neo4j_client = neo4j_client
        self._embedder = embedder

    def create_knowledge(self) -> KnowledgeIndex | None:
        """Return a :class:`KnowledgeIndex`, or ``None`` if disabled in config."""
        if not self.settings.knowledge.enabled:
            return None
        return KnowledgeIndex(
            project=self._project_dir,
            data_dir=self.settings.storage.data_dir,
            neo4j_client=self._neo4j_client,
            embedder=self._embedder,
        )
