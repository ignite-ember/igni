"""Factory for :class:`ArtifactIndex` instances scoped per project."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ember_code.core.artifacts.capture import RetrievalLedger
from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class ArtifactManager:
    """Build an :class:`ArtifactIndex` for the active session's project."""

    def __init__(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        neo4j_client: Any | None = None,
        embedder: Any | None = None,
        ledger: RetrievalLedger | None = None,
    ):
        self.settings = settings
        self._project_dir = project_dir or Path.cwd()
        self._neo4j_client = neo4j_client
        self._embedder = embedder
        # One ledger per manager, so every index it builds shares the
        # session's reads. A ledger per index would mean a rebuilt
        # index (config reload, runtime swap) forgets what the agent
        # read a minute ago, and the next write captures nothing.
        self._ledger = ledger or RetrievalLedger()

    def create_artifacts(self) -> ArtifactIndex | None:
        """Return an :class:`ArtifactIndex`, or ``None`` if disabled."""
        if not self.settings.artifacts.enabled:
            return None
        return ArtifactIndex(
            project=self._project_dir,
            data_dir=self.settings.storage.data_dir,
            neo4j_client=self._neo4j_client,
            embedder=self._embedder,
            ledger=self._ledger,
        )
