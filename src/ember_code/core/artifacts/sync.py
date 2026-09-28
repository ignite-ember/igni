"""Bidirectional sync between ``.igni/artifacts.yaml`` and the store.

The YAML file is the git-shareable form. It carries **head content
only** — the version chain and the derivation graph stay local. That
is a deliberate trade: writing history into the repo makes every
artifact edit produce a diff nobody can review, and the file stops
being worth having. The consequence is equally deliberate and worth
stating out loud: a fresh clone has artifacts, but no lineage and no
history.

Direction matters here in a way it did not for the old knowledge file:

* **file → db** creates what is missing and *revises* what differs.
  The old syncer skipped any id it already had, which was correct
  when ids were content hashes (a changed entry was a new id) and
  wrong now that an artifact keeps its id across revisions — a pulled
  change would have been silently ignored.
* **db → file** rewrites the file from the store's heads rather than
  appending. Appending would leave the superseded row sitting next to
  the new one.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import (
    ArtifactEntry,
    ArtifactProvenance,
    ArtifactSyncResult,
    ArtifactYamlFile,
    EdgeState,
    SourceDeclaration,
)
from ember_code.core.paths import CONFIG_DIR

__all__ = [
    "ArtifactSyncer",
    "ArtifactEntry",
    "DEFAULT_ARTIFACTS_FILE",
    "LEGACY_KNOWLEDGE_FILE",
]

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACTS_FILE = f"{CONFIG_DIR}/artifacts.yaml"

# Projects that predate the rename still have this file committed.
# Reading it on load (and writing only the new name) migrates a repo
# the first time anyone syncs, without a separate migration step and
# without losing entries for anyone who has not pulled yet.
LEGACY_KNOWLEDGE_FILE = f"{CONFIG_DIR}/knowledge.yaml"


class ArtifactSyncer:
    """Orchestrates YAML ↔ :class:`ArtifactIndex` sync.

    Delegates file I/O to :class:`ArtifactYamlFile` and id/timestamp
    policy to :class:`ArtifactMetadataCodec`.
    """

    def __init__(
        self,
        file_path: Path,
        artifacts: ArtifactIndex | None = None,
        *,
        codec: ArtifactMetadataCodec | None = None,
    ) -> None:
        self.file_path = file_path
        self.artifacts = artifacts
        self.codec = codec or ArtifactMetadataCodec()

    def make_entry(self, content: str, source: str = "", name: str = "") -> ArtifactEntry:
        return ArtifactEntry.from_content(content, source=source, name=name, codec=self.codec)

    def load_file(self) -> list[ArtifactEntry]:
        """Read the shared file, falling back to the pre-rename name."""
        entries = ArtifactYamlFile.load_from(self.file_path)
        if entries:
            return entries
        legacy = self.file_path.parent / Path(LEGACY_KNOWLEDGE_FILE).name
        if legacy != self.file_path and legacy.exists():
            logger.info("Reading legacy knowledge file %s", legacy)
            return ArtifactYamlFile.load_from(legacy)
        return []

    def save_file(self, entries: list[ArtifactEntry]) -> None:
        ArtifactYamlFile.write_to(self.file_path, entries, codec=self.codec)

    async def sync_file_to_db(self) -> ArtifactSyncResult:
        """File → store. Creates what is missing, revises what differs."""
        if self.artifacts is None:
            return ArtifactSyncResult(direction="file_to_db", message="Artifacts disabled.")

        file_entries = self.load_file()
        if not file_entries:
            return ArtifactSyncResult(
                direction="file_to_db", new_entries=0, existing_entries=0, total_entries=0
            )

        inserted = 0
        existing = 0
        errors: list[str] = []
        for entry in file_entries:
            name = entry.name or entry.id
            try:
                current = await self.artifacts.resolve(entry.id) or (
                    await self.artifacts.resolve(name)
                )
                declaration = self._declaration_for(entry)
                if current is None:
                    result = await self.artifacts.create(
                        name=name,
                        content=entry.content,
                        sources=declaration,
                        kind=entry.kind or "note",
                        metadata=ArtifactProvenance(added_at=entry.added_at).to_metadata(),
                    )
                    if not result.success:
                        errors.append(f"{name}: {result.error}")
                    else:
                        inserted += 1
                    continue
                head = await self.artifacts.version(current.artifact_id, 0)
                if head is not None and head.content == entry.content:
                    existing += 1
                    continue
                result = await self.artifacts.revise(
                    ref=current.artifact_id,
                    content=entry.content,
                    sources=declaration,
                )
                if not result.success:
                    errors.append(f"{name}: {result.error}")
                else:
                    inserted += 1
            except Exception as exc:
                message = f"Failed to sync entry {name}: {exc}"
                logger.warning(message)
                errors.append(message)

        return ArtifactSyncResult(
            direction="file_to_db",
            new_entries=inserted,
            existing_entries=existing,
            total_entries=existing + inserted,
            errors=errors,
        )

    @staticmethod
    def _declaration_for(entry: ArtifactEntry) -> SourceDeclaration:
        """Rebuild the source declaration from a YAML row.

        A pulled file may name sources that do not exist in this
        clone's store yet — the store skips unknown refs and reports
        them rather than refusing the write, so an artifact never
        fails to land because its source has not synced yet.
        """
        declaration = SourceDeclaration.from_raw(entry.sources)
        # A file round-trip is not a fresh assertion of provenance,
        # so anything the file carries stays confirmed rather than
        # being demoted to a candidate nobody will ever review.
        declaration.pins = [
            pin.model_copy(update={"state": EdgeState.CONFIRMED}) for pin in declaration.pins
        ]
        return declaration

    async def sync_db_to_file(self) -> ArtifactSyncResult:
        """Store → file. Full rewrite from current heads."""
        if self.artifacts is None:
            return ArtifactSyncResult(direction="db_to_file", message="Artifacts disabled.")

        previous = self.load_file()
        # Archived entries stay in the file. Dropping them would
        # delete them from it, and the next file-to-store sync would
        # bring them back as new documents at the top level.
        summaries = await self.artifacts.list_artifacts(include_archived=True)
        rows: list[ArtifactEntry] = []
        for summary in summaries:
            head = await self.artifacts.version(summary.artifact_id, 0)
            if head is None:
                continue
            lineage = await self.artifacts.lineage(summary.artifact_id, head.n)
            sources = (
                [
                    f"{edge.to_artifact_id}@{edge.pinned_n}"
                    for edge in lineage.sources
                    if edge.state is EdgeState.CONFIRMED
                ]
                + [c.url for c in lineage.citations]
                if lineage is not None
                else []
            )
            provenance = ArtifactProvenance.from_metadata(head.metadata)
            rows.append(
                ArtifactEntry(
                    id=summary.artifact_id,
                    name=summary.name,
                    kind=summary.kind,
                    content=head.content,
                    source=head.metadata.get("source", ""),
                    added_at=provenance.added_at or summary.created_at or self.codec.now_iso(),
                    n=head.n,
                    sources=sources,
                )
            )

        if not rows and not previous:
            return ArtifactSyncResult(
                direction="db_to_file", new_entries=0, existing_entries=0, total_entries=0
            )

        known = {e.id for e in previous}
        added = [r for r in rows if r.id not in known]
        self.save_file(rows)
        return ArtifactSyncResult(
            direction="db_to_file",
            new_entries=len(added),
            existing_entries=len(rows) - len(added),
            total_entries=len(rows),
        )
