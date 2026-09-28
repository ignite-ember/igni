"""Artifact RPCs — :class:`ArtifactController` owns the panel surface
for one session: status, search, add, list, get, versions, lineage,
stale, reconcile, export and remove.

Wire types are declared in :mod:`schemas_artifacts` and re-exported
here so external callers keep the previous import surface.

The controller stays a translation layer. Every decision — what
counts as stale, which edges leave on an export, whether a write may
skip its sources — lives in the store or the session manager, because
the agent path and the panel path have to make the same call and only
one of them goes through here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ember_code.backend.file_opener import FileOpener
from ember_code.backend.schemas_artifacts import (
    ArtifactCandidateEvidence,
    ArtifactCandidateVerdict,
    ArtifactCommentAnchor,
    ArtifactCommentPosted,
    ArtifactCommentRelevance,
    ArtifactCommentReplied,
    ArtifactCommentResolved,
    ArtifactCommentRow,
    ArtifactDependent,
    ArtifactDrift,
    ArtifactDriftApplied,
    ArtifactExportResult,
    ArtifactGetResult,
    ArtifactGraphEdge,
    ArtifactGraphNode,
    ArtifactGraphView,
    ArtifactHit,
    ArtifactLineageRow,
    ArtifactLineageView,
    ArtifactListEntry,
    ArtifactOpenResult,
    ArtifactProvenance,
    ArtifactQueryResult,
    ArtifactRemoveResult,
    ArtifactShareResult,
    ArtifactStaleRow,
    ArtifactStatus,
    ArtifactTree,
    ArtifactTreeChanged,
    ArtifactTreeNode,
    ArtifactVersionRow,
    CandidateOverlapRow,
    DriftAffectedRow,
    ProvenanceMatchRow,
    ProvenancePassageRow,
)
from ember_code.core.artifacts import side_by_side
from ember_code.core.artifacts.anchoring import RelevanceReview, surrounding, track_anchor
from ember_code.core.artifacts.collaboration import CollaborationSync
from ember_code.core.artifacts.conversation import CommentConversation
from ember_code.core.artifacts.cypher import ArtifactCypherService, CypherResult
from ember_code.core.artifacts.drift import (
    AffectedPassage,
    Divergence,
    DriftReview,
    DriftUpdate,
)
from ember_code.core.artifacts.export import (
    ArtifactExporter,
    ArtifactPublisher,
    ExportFormat,
)
from ember_code.core.artifacts.mentions import resolve
from ember_code.core.artifacts.models import (
    ArtifactSummary,
    CommentOrigin,
    EdgeState,
    StaleDependent,
)
from ember_code.core.artifacts.pdf import PdfUnavailable
from ember_code.core.artifacts.provenance import ProvenanceMapper, quote_lines, strength_of
from ember_code.core.artifacts.query_planner import ArtifactQueryPlanner
from ember_code.core.artifacts.side_by_side import RowKind, VersionDiff
from ember_code.core.artifacts.side_by_side import build as build_side_by_side
from ember_code.core.artifacts.sourcing import CandidateReview, Overlap
from ember_code.core.artifacts.threads import ThreadInstruction
from ember_code.core.artifacts.tree import build as build_tree
from ember_code.core.artifacts.tree import flatten as flatten_tree
from ember_code.core.artifacts.tree import would_cycle
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)

__all__ = [
    "ArtifactController",
    "ArtifactHit",
    "ArtifactListEntry",
    "ArtifactGetResult",
    "ArtifactRemoveResult",
    "ArtifactStatus",
]


def _run_failed(response: Any) -> bool:
    """Whether an agno run errored rather than answered.

    A failed run is not an exception here: ``arun`` returns normally
    with ``status`` set to ERROR and the provider's message sitting in
    ``content``. Anything that treats ``content`` as the answer will
    happily store "Token Plan usage limit reached" as a document.
    """
    status = getattr(response, "status", None)
    return str(getattr(status, "value", status) or "").upper() == "ERROR"


def _strip_fence(text: str) -> str:
    """Drop a code fence a model wrapped the whole document in.

    Asked for "the document and nothing else", a model will still
    sometimes hand back ```markdown …```. Written through, those three
    backticks become the document's first line for ever.
    """
    body = text.strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    if len(lines) < 2:
        return body
    closing = len(lines) - 1
    while closing > 0 and not lines[closing].strip().startswith("```"):
        closing -= 1
    if closing <= 0:
        return body
    return "\n".join(lines[1:closing]).strip()


class ArtifactController:
    """Artifact RPCs for one :class:`Session`."""

    def __init__(self, session: Session) -> None:
        self._session = session
        # Advanced by each pull so a second one asks only for what
        # changed. Not persisted: starting from the beginning is
        # idempotent, and a stale cursor on disk is the one way to
        # *miss* something.
        self._comment_cursor: str | None = None

    # ── Overview ──────────────────────────────────────────────────────

    async def status(self) -> ArtifactStatus:
        """Status snapshot for the panel header."""
        status = await self._session.artifact_mgr.status()
        return ArtifactStatus(
            enabled=status.enabled,
            collection_name=status.collection_name,
            document_count=status.document_count,
            version_count=status.version_count,
            stale_count=status.stale_count,
            pending_candidates=status.pending_candidates,
            embedder=status.embedder,
        )

    async def search(self, query: str) -> list[ArtifactHit]:
        """Search the store — one :class:`ArtifactHit` per result.

        Panel searches do **not** seed the capture ledger: a person
        browsing is not the agent learning, and letting the UI feed
        capture would attach candidate sources to whatever the agent
        writes next.
        """
        response = await self._session.artifact_mgr.search(query, record=False)
        return [
            ArtifactHit(
                artifact_id=r.artifact_id,
                name=r.name,
                kind=r.kind,
                n=r.n,
                content=r.content,
                score=r.score,
                metadata={k: str(v) for k, v in r.metadata.items()},
            )
            for r in response.results
        ]

    async def add(self, source: str, parent_id: str = "") -> msg.Info:
        """Add content from the panel.

        A URL or path is ingestion — the source is self-evident and no
        declaration is asked for. Pasted text is a new artifact with
        no antecedent, which the panel states rather than inventing
        one.

        ``parent_id`` is where it goes in the tree, and it is passed at
        creation rather than moved afterwards: a document that appears
        at the top level and then jumps is two events for one
        intention, and if the move fails it has quietly landed
        somewhere nobody asked for.
        """
        mgr = self._session.artifact_mgr
        if source.startswith(("http://", "https://")):
            result = await mgr.add_url(source)
        elif "/" in source or source.startswith("."):
            result = await mgr.add_path(source)
        else:
            first_line = next(
                (ln.strip().lstrip("# ").strip() for ln in source.splitlines() if ln.strip()),
                "",
            )
            result = await mgr.create(
                name=first_line[:80] or "untitled note",
                content=source,
                sources=[],
                no_sources_reason="added by hand from the artifacts panel",
            )
        if not result.success:
            return msg.Info(text=result.error or "Add failed.")

        placed = ""
        if parent_id and result.artifact_id and self._session.artifacts is not None:
            try:
                if await self._session.artifacts.client.set_parent(
                    node_id=result.artifact_id, parent_id=parent_id
                ):
                    placed = " Filed where you asked."
            except Exception as exc:
                # The document exists either way, so this is a note
                # about where it is rather than a failure of the add.
                logger.debug("could not file %s under %s: %s", result.artifact_id, parent_id, exc)
                placed = " It is at the top level — filing it failed."
        return msg.Info(text=f"{result.message}{placed}")

    async def list(self) -> list[ArtifactListEntry]:
        """Every artifact, head version — the panel's Browse tab."""
        mgr = self._session.artifact_mgr
        try:
            summaries = await mgr.list_artifacts()
        except Exception as exc:
            logger.debug("artifact_list failed: %s", exc)
            return []

        out: list[ArtifactListEntry] = []
        for summary in summaries:
            head = await mgr.version(summary.artifact_id, 0)
            content = head.content if head else ""
            out.append(
                ArtifactListEntry(
                    id=summary.artifact_id,
                    name=summary.name or self._name_for(summary),
                    source=summary.origin,
                    kind=summary.kind,
                    n=summary.head_n,
                    version_count=summary.version_count,
                    size=len(content),
                    preview=content[:240],
                    added_at=summary.updated_at or summary.created_at,
                    metadata={},
                )
            )
        out.sort(key=lambda d: d.added_at, reverse=True)
        return out

    async def get(self, entry_id: str, version: int = 0) -> ArtifactGetResult:
        """Full content for one artifact at one version."""
        mgr = self._session.artifact_mgr
        summary = await self._resolve(entry_id)
        if summary is None:
            return ArtifactGetResult(error=f"Artifact {entry_id} not found.")
        found = await mgr.version(summary.artifact_id, version)
        if found is None:
            return ArtifactGetResult(error=f"Artifact {entry_id} has no version {version}.")
        return ArtifactGetResult(
            id=summary.artifact_id,
            name=summary.name or entry_id,
            source=summary.origin,
            kind=summary.kind,
            n=found.n,
            head_n=summary.head_n,
            is_head=found.head,
            archived=summary.archived,
            content=found.content,
            created_at=found.created_at,
            author=found.author,
            metadata={k: str(v) for k, v in found.metadata.items() if v is not None},
        )

    # ── Lineage ───────────────────────────────────────────────────────

    async def versions(self, entry_id: str) -> list[ArtifactVersionRow]:
        """The revision chain, newest first."""
        versions = await self._session.artifact_mgr.versions(entry_id)
        return [
            ArtifactVersionRow(
                n=v.n,
                created_at=v.created_at,
                author=v.author,
                size=len(v.content),
                is_head=v.head,
            )
            for v in versions
        ]

    async def lineage(self, entry_id: str, version: int = 0) -> ArtifactLineageView:
        """Both directions around one version, plus citations."""
        lineage = await self._session.artifact_mgr.lineage(entry_id, version)
        if lineage is None:
            return ArtifactLineageView(artifact_id=entry_id, n=version)
        return ArtifactLineageView(
            artifact_id=lineage.artifact_id,
            n=lineage.n,
            sources=[
                ArtifactLineageRow(
                    artifact_id=e.to_artifact_id,
                    name=e.to_name,
                    n=e.to_n,
                    pinned_n=e.pinned_n,
                    head_n=e.source_head_n,
                    state=e.state.value,
                    is_stale=e.is_stale,
                )
                for e in lineage.sources
                if e.state is not EdgeState.DISMISSED
            ],
            dependents=[
                ArtifactLineageRow(
                    artifact_id=e.from_artifact_id,
                    name=e.from_name,
                    n=e.from_n,
                    pinned_n=e.pinned_n,
                    head_n=e.source_head_n,
                    state=e.state.value,
                    is_stale=e.is_stale,
                )
                for e in lineage.dependents
                if e.state is not EdgeState.DISMISSED
            ],
            citations=[c.url for c in lineage.citations],
        )

    async def stale(self) -> list[ArtifactStaleRow]:
        """Confirmed sources that have moved under their dependents."""
        rows = await self._session.artifact_mgr.stale()
        return [
            ArtifactStaleRow(
                artifact_id=s.dependent_artifact_id,
                name=s.dependent_name,
                n=s.dependent_n,
                source_artifact_id=s.source_artifact_id,
                source_name=s.source_name,
                pinned_n=s.pinned_n,
                source_head_n=s.source_head_n,
                versions_behind=s.versions_behind,
            )
            for s in rows
        ]

    async def compare(self, entry_id: str, from_n: int, to_n: int) -> VersionDiff:
        """Two versions of one document, aligned into columns.

        Either number may be ``0``, meaning "the head" — the panel
        offers "compare with current" far more often than it offers a
        specific pair, and resolving it here saves the client having
        to know the head number to ask the question.
        """
        mgr = self._session.artifact_mgr
        left = await mgr.version(entry_id, from_n)
        right = await mgr.version(entry_id, to_n)
        if left is None or right is None:
            # An empty diff rather than an error: asking for a version
            # that is not there is a stale panel, not a fault, and one
            # that throws on a deleted version is worse than one that
            # reports no changes.
            return VersionDiff(artifact_id=entry_id, from_n=from_n, to_n=to_n)
        # Archived included: this only turns ids into names, and a
        # name that goes blank because somebody archived the document
        # is the thing archiving promised would keep resolving.
        known = {s.artifact_id: s.name for s in await mgr.list_artifacts(include_archived=True)}
        return side_by_side.build(
            left.content,
            right.content,
            artifact_id=entry_id,
            name=known.get(entry_id, "") or "",
            from_n=left.n,
            to_n=right.n,
        )

    async def comments(
        self, *, artifact_id: str = "", open_only: bool = True
    ) -> list[ArtifactCommentRow]:
        """Comments projected from the server, for the review screen.

        Read from the local graph rather than the API. That is the
        point of projecting them: the queue answers with the network
        down, and it answers with the same query language as staleness
        and lineage.
        """
        mgr = self._session.artifact_mgr
        rows = await mgr.comments(artifact_id=artifact_id or None, open_only=open_only)
        # One listing rather than a lookup per comment: the queue is
        # cross-document but a person's documents number in the tens,
        # and a resolve per row would be a query per comment.
        known = {
            s.artifact_id: ((s.name or ""), int(s.head_n or 0))
            # Archived too: a comment on a document that was put away
            # is still a comment somebody has to answer, and a row
            # with no name is one nobody can act on.
            for s in await mgr.list_artifacts(include_archived=True)
        }
        out: list[ArtifactCommentRow] = []
        for c in rows:
            # A comment can outlive the document it was left on —
            # the row stays on the server after a local delete. Fall
            # back to the id rather than dropping it.
            name, head_n = known.get(c.artifact_id, (c.artifact_id, 0))
            out.append(
                ArtifactCommentRow(
                    comment_id=c.comment_id,
                    artifact_id=c.artifact_id,
                    document_name=name,
                    n=c.n,
                    thread_id=c.thread_id,
                    quote=c.quote,
                    body=c.body,
                    suggested_text=c.suggested_text or "",
                    status=c.status.value,
                    resolved_in_n=c.resolved_in_n or 0,
                    author=c.author,
                    at=c.at,
                    # The passage may have been rewritten since. Say so
                    # rather than letting a reviewer act on a quote that
                    # no longer appears in the document.
                    behind_head=bool(head_n and c.n < head_n),
                    head_n=head_n,
                )
            )
        return out

    async def provenance(self, *, artifact_id: str, n: int = 0) -> ArtifactProvenance:
        """Which passage of this document came from which passage of its sources.

        Inferred, never recorded — see
        :mod:`ember_code.core.artifacts.provenance`. The result is
        labelled as such all the way out to the screen, because a line
        drawn between two paragraphs with the confidence of a recorded
        fact would be a lie about what is known.

        Only **confirmed** sources are mapped. A candidate edge is a
        guess about whether the document derives from the source at
        all; refining a guess with a guess produces two of them
        stacked and one line on screen.
        """
        mgr = self._session.artifact_mgr
        if self._session.artifacts is None:
            return ArtifactProvenance(error="Artifacts are disabled.")

        summary = await mgr.resolve_artifact(artifact_id)
        if summary is None:
            return ArtifactProvenance(error=f"No document {artifact_id!r}.")
        version = n or int(summary.head_n or 0)

        # Repaired on demand rather than in a migration: a store
        # written before chunks carried vectors — or before their line
        # spans could survive the chunker's own whitespace
        # normalisation — needs re-splitting, and a document nobody
        # opens never needs it.
        pending_versions = await mgr.repair_chunks()
        unplaced = await mgr.backfill_chunk_embeddings()
        if pending_versions:
            unplaced += pending_versions

        client = self._session.artifacts.client
        passages = await client.passages(artifact_id=summary.artifact_id, n=version)
        if not passages:
            return ArtifactProvenance(artifact_id=summary.artifact_id, n=version)

        lineage = await mgr.lineage(summary.artifact_id, version)
        # Direct attribute access, not ``getattr`` with a default. The
        # first cut of this guessed the field names on ``LineageEdge``
        # — they are ``to_artifact_id`` / ``to_name`` / ``source_head_n``,
        # because the edge is rendered from either direction — and the
        # defaults turned every wrong guess into an empty string, so
        # the whole feature returned "nothing resembles a source"
        # instead of failing.
        confirmed = [
            edge
            for edge in (lineage.sources if lineage is not None else [])
            # A revision derives from its own previous version, and
            # that edge is real — but "this paragraph came from the
            # previous draft of itself" is true of nearly every
            # paragraph and answers nothing. It would also score near
            # 1.0 and crowd out the source that actually matters.
            if edge.state is EdgeState.CONFIRMED and edge.to_artifact_id != summary.artifact_id
        ]

        merged: dict[str, ProvenancePassageRow] = {
            p.chunk_id: ProvenancePassageRow(
                chunk_id=p.chunk_id,
                line_from=p.line_from,
                line_to=p.line_to,
                text=p.text,
            )
            for p in passages
        }
        source_names: list[str] = []

        for edge in confirmed:
            # ``to_*`` is the source end: ``sources`` edges point from
            # this document to what it was built on.
            source_id = edge.to_artifact_id
            pinned_n = int(edge.pinned_n or 0)
            head_n = int(edge.source_head_n or 0)
            source_name = edge.to_name or source_id

            # Read the source at the version it is *now*, not at the
            # pin. The question is which of my passages the source's
            # current text bears on — and when it has moved, which of
            # them the move touched.
            current_n = head_n or pinned_n
            source_passages = await client.passages(artifact_id=source_id, n=current_n)
            if not source_passages:
                continue
            source_names.append(source_name)

            # The source's own text, so a match can be quoted as the
            # lines it points at rather than as the chunk that happened
            # to contain them. One read per source, not per match.
            source_version = await mgr.version(source_id, current_n)
            source_content = (source_version.content or "") if source_version else ""

            changed: list[tuple[int, int]] = []
            if head_n and pinned_n and head_n > pinned_n:
                left = await mgr.version(source_id, pinned_n)
                right = await mgr.version(source_id, head_n)
                if left is not None and right is not None:
                    diff = build_side_by_side(
                        left.content or "",
                        right.content or "",
                        from_n=pinned_n,
                        to_n=head_n,
                    )
                    changed = ProvenanceMapper.changed_ranges(list(diff.rows))

            for mapped in ProvenanceMapper.map(passages, source_passages, changed=changed):
                row = merged[mapped.chunk_id]
                row.matches.extend(
                    ProvenanceMatchRow(
                        source_artifact_id=source_id,
                        source_name=source_name,
                        source_n=current_n,
                        source_line_from=m.source_line_from,
                        source_line_to=m.source_line_to,
                        source_text=quote_lines(
                            source_content, m.source_line_from, m.source_line_to
                        )
                        or m.source_text,
                        similarity=m.similarity,
                        strength=strength_of(m.similarity),
                        changed=m.changed,
                    )
                    for m in mapped.matches
                )

        for row in merged.values():
            # Strongest first across every source, so the UI can take
            # the head of the list and be showing the best answer.
            row.matches.sort(key=lambda m: -m.similarity)
            row.affected = any(m.changed for m in row.matches)

        rows = list(merged.values())
        return ArtifactProvenance(
            artifact_id=summary.artifact_id,
            n=version,
            passages=rows,
            sources=source_names,
            affected_count=sum(1 for r in rows if r.affected),
            unplaced=unplaced,
        )

    #: Overlaps quoted into a candidate review. Enough to decide on;
    #: a reviewer who needs the twentieth is reading both documents.
    MAX_OVERLAPS = 6

    async def candidate_evidence(
        self, *, artifact_id: str, source_id: str
    ) -> ArtifactCandidateEvidence:
        """Why a captured read might — or might not — be a source.

        The review row used to offer Confirm and Not a source under a
        name and a version number, which is the decision without any
        of what it rests on. This is what it rests on: the passages of
        this document that resemble passages of that one, strongest
        first.

        Deliberately pointed at an unconfirmed edge. :meth:`provenance`
        maps confirmed sources only, and is right to — refining a
        guess with a guess stacks two of them behind one line. Here the
        guess *is* the question, and an empty answer is the most
        useful one a reviewer can get: nothing in this document
        resembles that one, so whatever the search turned it up for, it
        did not end up here.
        """
        mgr = self._session.artifact_mgr
        if self._session.artifacts is None:
            return ArtifactCandidateEvidence(error="Artifacts are disabled.")

        dependent = await mgr.resolve_artifact(artifact_id)
        source = await mgr.resolve_artifact(source_id)
        if dependent is None or source is None:
            return ArtifactCandidateEvidence(error="Could not find both documents.")

        # Same repair the trace view needs: a store written before
        # chunks carried vectors cannot be matched on at all.
        unplaced = await mgr.repair_chunks() + await mgr.backfill_chunk_embeddings()

        lineage = await mgr.lineage(dependent.artifact_id, 0)
        edge = next(
            (
                e
                for e in (lineage.sources if lineage is not None else [])
                if e.to_artifact_id == source.artifact_id
            ),
            None,
        )
        # The version that was *read*, not the source's head. The
        # decision is about what the agent had in front of it.
        read_n = int(edge.pinned_n if edge is not None else 0) or int(source.head_n or 0)

        client = self._session.artifacts.client
        # An explicit version, both sides. ``passages(n=0)`` is not
        # "head" — it matches no version and returns nothing, and a
        # comparison against an empty left side answers "nothing here
        # resembles it" for every source there is. A false negative
        # that reads like a finding is worse than an error.
        mine_n = int(dependent.head_n or 0)
        mine = await client.passages(artifact_id=dependent.artifact_id, n=mine_n)
        theirs = await client.passages(artifact_id=source.artifact_id, n=read_n)

        base = ArtifactCandidateEvidence(
            artifact_id=dependent.artifact_id,
            source_artifact_id=source.artifact_id,
            source_name=source.name or source.artifact_id,
            source_n=read_n,
            captured_at=edge.at if edge is not None else "",
            total_passages=len(mine),
            unplaced=unplaced,
        )

        # Worked out once. The comparison is every passage against
        # every passage, and neither side moves on its own — a stored
        # answer for this exact pair of versions is still the answer.
        stored = await self._stored_review(dependent, source.artifact_id, mine_n, read_n)
        if stored is not None and stored.get("review_overlaps"):
            try:
                base.overlaps = [
                    CandidateOverlapRow(**row) for row in json.loads(stored["review_overlaps"])
                ]
                base.passages_touched = int(stored.get("review_touched", 0))
                base.total_passages = int(stored.get("review_total", 0)) or base.total_passages
                return base
            except (ValueError, TypeError) as exc:
                # A stored answer that will not load is not worth
                # failing over — recompute and overwrite it.
                logger.info("stored overlap for %s unreadable: %s", artifact_id, exc)
        if not mine or not theirs:
            # Nothing to compare is not the same as nothing in common,
            # and the caller has to be able to tell them apart.
            base.error = (
                "Neither document has indexed passages yet."
                if not mine and not theirs
                else f"{base.source_name} has no indexed passages at v{read_n}."
                if not theirs
                else "This document has no indexed passages yet."
            )
            return base

        source_content = ""
        read = await mgr.version(source.artifact_id, read_n)
        if read is not None:
            source_content = read.content or ""

        overlaps: list[CandidateOverlapRow] = []
        touched = 0
        for mapped in ProvenanceMapper.map(mine, theirs):
            if not mapped.matches:
                continue
            touched += 1
            best = mapped.matches[0]
            overlaps.append(
                CandidateOverlapRow(
                    here_text=mapped.text,
                    here_line_from=mapped.line_from,
                    here_line_to=mapped.line_to,
                    there_text=quote_lines(
                        source_content, best.source_line_from, best.source_line_to
                    )
                    or best.source_text,
                    there_line_from=best.source_line_from,
                    there_line_to=best.source_line_to,
                    similarity=best.similarity,
                    strength=strength_of(best.similarity),
                )
            )

        overlaps.sort(key=lambda o: -o.similarity)
        base.overlaps = overlaps[: self.MAX_OVERLAPS]
        base.passages_touched = touched

        await self._store_review(
            dependent,
            source.artifact_id,
            mine_n,
            read_n,
            {
                "review_overlaps": json.dumps([o.model_dump() for o in base.overlaps]),
                "review_touched": touched,
                "review_total": len(mine),
            },
        )
        return base

    async def _stored_review(
        self, dependent: Any, source_id: str, mine_n: int, read_n: int
    ) -> dict[str, Any] | None:
        """A stored review, but only if it is about these two versions.

        The version pair is the cache key, and it has to be: a review
        of v2 against v5 says nothing about v3 against v6, and serving
        it would be worse than recomputing — a stale verdict reads
        exactly like a fresh one.
        """
        client = self._session.artifacts.client
        try:
            props = await client.edge_review(
                artifact_id=dependent.artifact_id,
                n=mine_n,
                source_artifact_id=source_id,
            )
        except Exception as exc:  # noqa: BLE001 — a cache miss is not a failure
            logger.info("could not read stored review: %s", exc)
            return None
        if not props:
            return None
        if int(props.get("review_dependent_n", -1)) != mine_n:
            return None
        if int(props.get("review_source_n", -1)) != read_n:
            return None
        return props

    async def _store_review(
        self, dependent: Any, source_id: str, mine_n: int, read_n: int, review: dict[str, Any]
    ) -> None:
        """Keep an answer, stamped with the versions it is about."""
        client = self._session.artifacts.client
        try:
            await client.set_edge_review(
                artifact_id=dependent.artifact_id,
                n=mine_n,
                source_artifact_id=source_id,
                review={
                    **review,
                    "review_dependent_n": mine_n,
                    "review_source_n": read_n,
                },
            )
        except Exception as exc:  # noqa: BLE001 — losing the cache costs time, not truth
            logger.info("could not store review: %s", exc)

    async def candidate_verdict(
        self, *, artifact_id: str, source_id: str
    ) -> ArtifactCandidateVerdict:
        """Whether the agent thinks this captured read was a source.

        The evidence answered "what do these two documents share".
        This answers the question actually being asked — *did this one
        draw on that one* — which similarity cannot, because it says
        two paragraphs are about the same thing and not which way the
        influence ran.

        Advisory only. The verdict writes nothing; Confirm and Not a
        source still do, and a reviewer who disagrees presses the other
        button. A recommendation with its reasons can be argued with,
        which is the point of producing one rather than handing over a
        score.
        """
        evidence = await self.candidate_evidence(artifact_id=artifact_id, source_id=source_id)
        if evidence.error:
            return ArtifactCandidateVerdict(error=evidence.error)

        mgr = self._session.artifact_mgr
        dependent = await mgr.resolve_artifact(artifact_id)
        if dependent is None:
            return ArtifactCandidateVerdict(error="Could not find the document.")

        # Asked once. Beyond the cost, a model call is not a pure
        # function: reopening the row to think about a recommendation
        # and finding a different one is worse than waiting for the
        # first.
        mine_n = int(dependent.head_n or 0)
        stored = await self._stored_review(
            dependent, evidence.source_artifact_id, mine_n, evidence.source_n
        )
        if stored is not None and stored.get("review_verdict"):
            return ArtifactCandidateVerdict(
                recommendation=str(stored["review_verdict"]),
                reason=str(stored.get("review_reason", "")),
            )
        mine = await mgr.version(dependent.artifact_id, int(dependent.head_n or 0))
        theirs = await mgr.version(evidence.source_artifact_id, evidence.source_n)
        if mine is None or theirs is None:
            return ArtifactCandidateVerdict(error="Could not read both documents.")

        review = CandidateReview(
            dependent_name=dependent.name or dependent.artifact_id,
            dependent_content=mine.content or "",
            source_name=evidence.source_name,
            source_n=evidence.source_n,
            source_content=theirs.content or "",
            overlaps=[
                Overlap(
                    here_text=o.here_text,
                    here_line_from=o.here_line_from,
                    here_line_to=o.here_line_to,
                    there_text=o.there_text,
                    there_line_from=o.there_line_from,
                    there_line_to=o.there_line_to,
                    similarity=o.similarity,
                )
                for o in evidence.overlaps
            ],
            passages_touched=evidence.passages_touched,
            total_passages=evidence.total_passages,
        )

        try:
            from agno.agent import Agent  # noqa: PLC0415 — heavy, and rarely reached

            from ember_code.core.config.models import ModelRegistry  # noqa: PLC0415

            agent = Agent(
                name="_artifact_source_reviewer",
                model=ModelRegistry(self._session.settings).get_model(),
                markdown=False,
            )
            response = await agent.arun(review.prompt_for())
        except Exception as exc:  # noqa: BLE001 — a model call, many failure modes
            logger.info("candidate verdict failed for %s: %s", artifact_id, exc)
            return ArtifactCandidateVerdict(error=f"Could not ask: {exc}")

        # Agno does not raise on a provider failure — it sets ``status``
        # to ERROR and puts the message in ``content``. Without this
        # check "Token Plan usage limit reached" is rendered as the
        # agent's opinion about somebody's provenance.
        if _run_failed(response):
            detail = str(getattr(response, "content", "") or "").strip()
            return ArtifactCandidateVerdict(
                error=f"The model did not answer: {detail}"
                if detail
                else "The model did not answer."
            )

        verdict = CandidateReview.parse(_strip_fence(str(getattr(response, "content", "") or "")))
        await self._store_review(
            dependent,
            evidence.source_artifact_id,
            mine_n,
            evidence.source_n,
            {
                "review_verdict": verdict.recommendation.value,
                "review_reason": verdict.reason,
            },
        )
        return ArtifactCandidateVerdict(
            recommendation=verdict.recommendation.value, reason=verdict.reason
        )

    async def _comment_and_versions(self, comment_id: str):
        """One comment, the version it was left on, and head."""
        mgr = self._session.artifact_mgr
        if self._session.artifacts is None:
            return None, None, None, "Artifacts are disabled."
        rows = await self._session.artifacts.client.comments(limit=10_000)
        found = next((c for c in rows if c.comment_id == comment_id), None)
        if found is None:
            return None, None, None, "No such comment here."
        summary = await mgr.resolve_artifact(found.artifact_id)
        if summary is None:
            return found, None, None, "The document this was left on is gone."
        head = await mgr.version(found.artifact_id, int(summary.head_n or 0))
        if head is None:
            return found, None, None, "Could not read the document."
        return found, head, int(summary.head_n or 0), ""

    async def comment_anchor(self, *, comment_id: str) -> ArtifactCommentAnchor:
        """What became of the passage this comment was left on.

        No model, no opinion. The queue says "the document has moved
        on since" against every row, which is true of all of them and
        separates none — a passage still there word for word and a
        passage deleted outright read identically, and those are
        opposite decisions.

        Exact match first, because the common case is common and a
        string comparison is not a judgement. Only when that fails is
        anything inferred, and what is inferred is reported with the
        number it rests on.
        """
        found, head, head_n, error = await self._comment_and_versions(comment_id)
        if error:
            return ArtifactCommentAnchor(error=error)
        assert found is not None and head is not None  # noqa: S101 — narrowed above

        content = head.content or ""
        fate = track_anchor(found.quote or "", content)
        return ArtifactCommentAnchor(
            state=fate.state.value,
            then=fate.then,
            now=fate.now,
            similarity=round(fate.similarity, 3),
            commented_n=int(found.n or 0),
            head_n=head_n or 0,
            context=surrounding(content, fate.now or fate.then),
        )

    async def comment_relevance(self, *, comment_id: str) -> ArtifactCommentRelevance:
        """Whether the comment survived what happened to its passage.

        Asked after :meth:`comment_anchor` and never instead of it. A
        changed anchor is not an answered comment — "name the cookie
        and its lifetime" is answered by an edit that names the cookie
        and overtaken by one that removes cookies entirely, and both
        are "the anchor changed". Only something that reads the
        comment against the change can tell those apart.
        """
        found, head, head_n, error = await self._comment_and_versions(comment_id)
        if error:
            return ArtifactCommentRelevance(error=error)
        assert found is not None and head is not None  # noqa: S101 — narrowed above

        mgr = self._session.artifact_mgr
        summary = await mgr.resolve_artifact(found.artifact_id)
        content = head.content or ""
        fate = track_anchor(found.quote or "", content)
        review = RelevanceReview(
            document_name=(summary.name if summary else "") or found.artifact_id,
            comment_body=found.body or "",
            author=found.author or "",
            commented_n=int(found.n or 0),
            head_n=head_n or 0,
            fate=fate,
            context=surrounding(content, fate.now or fate.then),
            suggested_text=found.suggested_text or "",
        )

        try:
            from agno.agent import Agent  # noqa: PLC0415 — heavy, and rarely reached

            from ember_code.core.config.models import ModelRegistry  # noqa: PLC0415

            agent = Agent(
                name="_artifact_comment_reviewer",
                model=ModelRegistry(self._session.settings).get_model(),
                markdown=False,
            )
            response = await agent.arun(review.prompt_for())
        except Exception as exc:  # noqa: BLE001 — a model call, many failure modes
            logger.info("comment relevance failed for %s: %s", comment_id, exc)
            return ArtifactCommentRelevance(error=f"Could not ask: {exc}")

        # Agno reports provider failure as ``status=ERROR`` with the
        # message in ``content`` rather than by raising. Without this
        # "Token Plan usage limit reached" is rendered as the agent's
        # read on somebody's comment.
        if _run_failed(response):
            detail = str(getattr(response, "content", "") or "").strip()
            return ArtifactCommentRelevance(
                error=f"The model did not answer: {detail}"
                if detail
                else "The model did not answer."
            )

        verdict = RelevanceReview.parse(_strip_fence(str(getattr(response, "content", "") or "")))
        return ArtifactCommentRelevance(relevance=verdict.relevance.value, reason=verdict.reason)

    async def reply_comment(self, *, comment_id: str, body: str) -> ArtifactCommentReplied:
        """Answer a comment, and act on it only if asked to.

        The queue used to offer three terminal actions and no way to
        say anything back, so declining told the commenter "rejected"
        with no reason attached and a question could only be answered
        by closing it.

        **Whether the agent runs is decided by the text.** ``@igni``
        in the message is an instruction; its absence means the reply
        is between people, however much it sounds like a request. An
        agent that decided for itself when a message was meant for it
        would turn every aside in a shared thread into a possible
        edit.

        **And when it does run, it proposes rather than writes.** The
        local comment rail answers by appending a version, which is
        right there — one person, their own document. A web thread has
        other people watching it, so the agent's answer arrives as
        ``suggested_text`` and the existing Accept button stays the
        thing that writes. Nobody's document changes because somebody
        typed a name.
        """
        if not body.strip():
            return ArtifactCommentReplied(error="A reply needs something in it.")
        sync = self._collaboration()
        if sync is None:
            return ArtifactCommentReplied(
                error="Replying needs a signed-in igni account — run `igni login`."
            )

        mgr = self._session.artifact_mgr
        rows = (
            await self._session.artifacts.client.comments(limit=10_000)
            if self._session.artifacts is not None
            else []
        )
        parent = next((c for c in rows if c.comment_id == comment_id), None)
        if parent is None:
            return ArtifactCommentReplied(error="No such comment here.")

        thread = [
            c
            for c in rows
            if (c.thread_id or c.comment_id) == (parent.thread_id or parent.comment_id)
        ]
        addressed = resolve(body, [c.author for c in thread if c.author])

        try:
            posted = await sync.reply(comment_id=comment_id, body=body)
        except Exception as exc:  # noqa: BLE001 — network, auth, permissions
            logger.info("reply to %s failed: %s", comment_id, exc)
            return ArtifactCommentReplied(error=f"Could not post the reply: {exc}")

        result = ArtifactCommentReplied(
            ok=True,
            comment_id=getattr(posted, "comment_id", "") or "",
            mentioned=[p.label for p in addressed.people],
            unknown=addressed.unknown,
            message="Replied.",
        )
        if not addressed.agent:
            return result

        summary = await mgr.resolve_artifact(parent.artifact_id)
        head = (
            await mgr.version(parent.artifact_id, int(summary.head_n or 0))
            if summary is not None
            else None
        )
        review = ThreadInstruction(
            document_name=(summary.name if summary else "") or parent.artifact_id,
            document=head.content if head is not None else "",
            quote=parent.quote or "",
            thread=[(c.author or "someone", c.body or "") for c in thread],
            instruction=body,
        )
        try:
            from agno.agent import Agent  # noqa: PLC0415 — heavy, and rarely reached

            from ember_code.core.config.models import ModelRegistry  # noqa: PLC0415

            agent = Agent(
                name="_artifact_thread_agent",
                model=ModelRegistry(self._session.settings).get_model(),
                markdown=False,
            )
            response = await agent.arun(review.prompt_for())
        except Exception as exc:  # noqa: BLE001 — a model call, many failure modes
            logger.info("thread agent failed for %s: %s", comment_id, exc)
            result.message = f"Replied. The agent could not be reached: {exc}"
            return result

        if _run_failed(response):
            detail = str(getattr(response, "content", "") or "").strip()
            result.message = (
                f"Replied. The agent did not answer: {detail}"
                if detail
                else ("Replied. The agent did not answer.")
            )
            return result

        answer = ThreadInstruction.parse(_strip_fence(str(getattr(response, "content", "") or "")))
        if not answer.body.strip():
            result.message = "Replied. The agent had nothing to add."
            return result

        try:
            await sync.reply(
                comment_id=comment_id,
                body=answer.body,
                suggested_text=answer.suggestion or None,
            )
        except Exception as exc:  # noqa: BLE001 — the human reply is already posted
            logger.info("agent reply to %s failed: %s", comment_id, exc)
            result.message = f"Replied, but the agent's answer did not post: {exc}"
            return result

        result.agent_replied = True
        result.agent_body = answer.body
        result.agent_suggestion = answer.suggestion
        result.message = "Replied, and igni answered."
        return result

    async def _tree_rows(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """Documents and folders as one set of rows.

        The tree is the one place the two node kinds meet, so it is
        the one place that assembles both.
        """
        mgr = self._session.artifact_mgr
        docs = await mgr.list_artifacts(include_archived=include_archived)
        rows: list[dict[str, Any]] = [
            {
                "node_id": d.artifact_id,
                "name": d.name or d.artifact_id,
                "kind": d.kind or "note",
                "head_n": int(d.head_n or 0),
                "updated_at": d.updated_at or "",
                "parent_id": getattr(d, "parent_id", "") or "",
                "archived": bool(getattr(d, "archived", False)),
                "is_folder": False,
            }
            for d in docs
        ]
        if self._session.artifacts is not None:
            try:
                rows.extend(await self._session.artifacts.client.folders())
            except Exception as exc:  # noqa: BLE001 — a store without folders yet
                logger.info("could not read folders: %s", exc)
        return rows

    async def tree(self, *, include_archived: bool = False) -> ArtifactTree:
        """The navigation tree, flattened and ordered.

        Archived documents are left out unless asked for. They are
        asked for from one place — the reader turning them on — and
        they come back in the same tree rather than a separate screen,
        so restoring one is done where it will end up.
        """
        if self._session.artifacts is None:
            return ArtifactTree(error="Artifacts are disabled.")
        rows = await self._tree_rows(include_archived=include_archived)
        nodes = build_tree(rows)
        return ArtifactTree(
            nodes=[
                ArtifactTreeNode(
                    node_id=n.node_id,
                    name=n.name,
                    kind=n.kind,
                    is_folder=n.is_folder,
                    head_n=n.head_n,
                    depth=n.depth,
                    descendants=n.descendants,
                    placeholder=n.placeholder,
                    archived=n.archived,
                )
                for n in flatten_tree(nodes)
            ]
        )

    async def create_folder(self, *, name: str, parent_id: str = "") -> ArtifactTreeChanged:
        """A place to put things."""
        if self._session.artifacts is None:
            return ArtifactTreeChanged(error="Artifacts are disabled.")
        title = name.strip()
        if not title:
            return ArtifactTreeChanged(error="A folder needs a name.")
        folder_id = await self._session.artifacts.client.create_folder(
            name=title, parent_id=parent_id or ""
        )
        return ArtifactTreeChanged(ok=True, node_id=folder_id, message=f"Created {title}.")

    async def rename_folder(self, *, folder_id: str, name: str) -> ArtifactTreeChanged:
        if self._session.artifacts is None:
            return ArtifactTreeChanged(error="Artifacts are disabled.")
        title = name.strip()
        if not title:
            return ArtifactTreeChanged(error="A folder needs a name.")
        ok = await self._session.artifacts.client.rename_folder(folder_id=folder_id, name=title)
        if not ok:
            return ArtifactTreeChanged(error="No such folder.")
        return ArtifactTreeChanged(ok=True, node_id=folder_id, message=f"Renamed to {title}.")

    async def delete_folder(self, *, folder_id: str) -> ArtifactTreeChanged:
        """Remove a folder and keep everything that was in it.

        A folder is an arrangement. Deleting an arrangement must not
        delete the things arranged, so its contents move up to its own
        parent — said out loud in the message, because "deleted" next
        to a folder full of documents is a sentence people read twice.
        """
        if self._session.artifacts is None:
            return ArtifactTreeChanged(error="Artifacts are disabled.")
        moved = await self._session.artifacts.client.delete_folder(folder_id=folder_id)
        return ArtifactTreeChanged(
            ok=True,
            promoted=moved,
            message=(
                f"Folder removed. {moved} item{'' if moved == 1 else 's'} moved up."
                if moved
                else "Folder removed."
            ),
        )

    async def archive_node(self, *, node_id: str, archived: bool = True) -> ArtifactTreeChanged:
        """Put a document away, or bring it back.

        The answer to "I am finished with this" that does not break
        anything built on it. Nothing is removed: the versions stay,
        the edges into it stay, and a dependent's lineage still
        resolves. It leaves the tree and the list, which is the whole
        of what was asked for.
        """
        if self._session.artifacts is None:
            return ArtifactTreeChanged(error="Artifacts are disabled.")
        moved = await self._session.artifacts.client.set_archived(
            artifact_id=node_id, archived=archived
        )
        if moved is None:
            return ArtifactTreeChanged(error="No such document.")
        if not archived:
            return ArtifactTreeChanged(ok=True, node_id=node_id, message="Restored.")
        # Said out loud. Things moved out of a branch without being
        # asked to is exactly the kind of change people discover a
        # week later and cannot account for.
        moved_note = f" {moved} item{'' if moved == 1 else 's'} moved up a level." if moved else ""
        return ArtifactTreeChanged(
            ok=True,
            node_id=node_id,
            promoted=moved,
            message=(
                "Archived. It is out of the tree; anything built on it still resolves." + moved_note
            ),
        )

    async def delete_node(self, *, node_id: str) -> ArtifactTreeChanged:
        """Delete a document, unless it is joined to another one.

        **A document in the lineage graph is archived, never deleted.**
        Deleting takes the whole version chain with it, and every
        ``DERIVED_FROM`` edge touching any version goes with it. The
        damage does not show up on this screen — it shows up later, on
        a different document, as a source that has quietly stopped
        saying what it was.

        Both directions count. A document built on this one obviously
        breaks. So does this document's own source: the edge is the
        record of what this was written from, and the source's list of
        dependents is how anybody finds out it existed at all. Cutting
        either end destroys the same fact.

        Dismissed edges do not count. "This was not a source" is a
        ruling that there is no relationship, and holding a document
        hostage to an edge somebody already rejected would make the
        dismissal meaningless.
        """
        if self._session.artifacts is None:
            return ArtifactTreeChanged(error="Artifacts are disabled.")

        store = self._session.artifacts.client
        try:
            ties = await store.lineage_ties(node_id)
        except Exception as exc:  # noqa: BLE001
            # Not "delete anyway". A delete that cannot be checked is
            # the one case where doing nothing is clearly right.
            logger.warning("could not check the lineage of %s: %s", node_id, exc)
            return ArtifactTreeChanged(
                error="Could not check what this is joined to, so nothing was deleted."
            )

        joined = [t for t in ties if (t.get("state") or "") != "dismissed"]
        if joined:
            return ArtifactTreeChanged(
                blocked_by=[
                    ArtifactDependent(
                        artifact_id=t["artifact_id"],
                        name=t["name"],
                        state=t["state"],
                        direction=t.get("direction") or "",
                    )
                    for t in joined
                ],
                suggest_archive=True,
                error=self._why_not_deletable(joined),
            )

        result = await self.remove(node_id)
        if result.error or not result.removed:
            return ArtifactTreeChanged(error=result.error or "Nothing was deleted.")
        return ArtifactTreeChanged(ok=True, node_id=node_id, message="Deleted.")

    @staticmethod
    def _why_not_deletable(ties: list[dict[str, Any]]) -> str:
        """The refusal, in the reader's terms rather than the graph's.

        Said as a relationship rather than as a count. "2 documents"
        tells somebody they are stuck; which way the edge runs tells
        them what they would have to change to get unstuck.
        """
        below = [t for t in ties if t.get("direction") == "dependent"]
        above = [t for t in ties if t.get("direction") == "source"]

        def names(rows: list[dict[str, Any]]) -> str:
            return ", ".join(r["name"] or r["artifact_id"] for r in rows)

        parts: list[str] = []
        if below:
            parts.append(f"{names(below)} {'is' if len(below) == 1 else 'are'} built on it")
        if above:
            parts.append(f"it is built on {names(above)}")
        return (
            "This document is part of the lineage: "
            + "; ".join(parts)
            + ". Archive it instead — archiving keeps every version and "
            "every one of those links resolving."
        )

    async def move_node(self, *, node_id: str, parent_id: str = "") -> ArtifactTreeChanged:
        """Put something somewhere else in the tree.

        Refused when it would close a loop. A cycle does not error and
        does not look broken: the branch detaches from every root, so
        everything on it disappears from navigation while still being
        in the store. Checked against the same rows the tree is built
        from, so the refusal happens before the write rather than being
        reported after it.
        """
        if self._session.artifacts is None:
            return ArtifactTreeChanged(error="Artifacts are disabled.")
        rows = await self._tree_rows()
        if would_cycle(rows, node_id=node_id, parent_id=parent_id or ""):
            return ArtifactTreeChanged(error="That would put a document inside itself.")
        ok = await self._session.artifacts.client.set_parent(
            node_id=node_id, parent_id=parent_id or None
        )
        if not ok:
            return ArtifactTreeChanged(error="Could not find that.")
        return ArtifactTreeChanged(ok=True, node_id=node_id, message="Moved.")

    async def drift(self, *, artifact_id: str, source_id: str) -> ArtifactDrift:
        """What has landed upstream, and which of our sentences lean on it.

        Everything the judgement needs is assembled here rather than
        left for the agent to fetch: a round trip per question is the
        cheap failure, and an answer made up from the two names is the
        expensive one.
        """
        mgr = self._session.artifact_mgr
        if self._session.artifacts is None:
            return ArtifactDrift(error="Artifacts are disabled.")

        dependent = await mgr.resolve_artifact(artifact_id)
        source = await mgr.resolve_artifact(source_id)
        if dependent is None or source is None:
            return ArtifactDrift(error="One of those documents is not in the store.")
        if dependent.artifact_id == source.artifact_id:
            # The rule the whole model turns on. A later version of
            # this document is a commit on the branch we are on.
            return ArtifactDrift(
                error="A document does not derive from itself; its own versions are not drift."
            )

        lineage = await mgr.lineage(dependent.artifact_id, 0)
        edge = next(
            (
                e
                for e in (lineage.sources if lineage is not None else [])
                if e.to_artifact_id == source.artifact_id and e.state is EdgeState.CONFIRMED
            ),
            None,
        )
        if edge is None:
            return ArtifactDrift(
                error=f"{dependent.name} is not built on {source.name}, or the link is unconfirmed."
            )

        pinned_n = int(edge.pinned_n or 0)
        head_n = int(source.head_n or 0)

        divergence = Divergence(
            dependent_id=dependent.artifact_id,
            dependent_name=dependent.name or dependent.artifact_id,
            dependent_n=int(dependent.head_n or 0),
            source_id=source.artifact_id,
            source_name=source.name or source.artifact_id,
            pinned_n=pinned_n,
            source_head_n=head_n,
        )

        if divergence.has_diverged:
            left = await mgr.version(source.artifact_id, pinned_n)
            right = await mgr.version(source.artifact_id, head_n)
            mine = await mgr.version(dependent.artifact_id, 0)
            divergence.dependent_content = (mine.content if mine else "") or ""
            divergence.source_pinned_content = (left.content if left else "") or ""
            divergence.source_head_content = (right.content if right else "") or ""
            if left is not None and right is not None:
                diff = build_side_by_side(
                    left.content or "", right.content or "", from_n=pinned_n, to_n=head_n
                )
                divergence.changes = [
                    (row.kind.value, row.left, row.right)
                    for row in diff.rows
                    if row.kind is not RowKind.EQUAL
                ]

            # Which of our passages lean on what moved. The provenance
            # pass already answers this; reusing it means the review
            # and the "where this came from" view cannot disagree.
            traced = await self.provenance(artifact_id=dependent.artifact_id)
            divergence.affected = [
                AffectedPassage(
                    text=row.text,
                    line_from=row.line_from,
                    line_to=row.line_to,
                    source_text=(row.matches[0].source_text if row.matches else ""),
                    strength=strength_of(row.matches[0].similarity) if row.matches else "",
                )
                for row in traced.passages
                if row.affected
            ]

        return ArtifactDrift(
            dependent_artifact_id=divergence.dependent_id,
            dependent_name=divergence.dependent_name,
            dependent_n=divergence.dependent_n,
            source_artifact_id=divergence.source_id,
            source_name=divergence.source_name,
            pinned_n=pinned_n,
            source_head_n=head_n,
            versions_behind=divergence.versions_behind,
            summary=DriftReview.summary(divergence),
            affected=[
                DriftAffectedRow(
                    text=a.text,
                    line_from=a.line_from,
                    line_to=a.line_to,
                    source_text=a.source_text,
                    strength=a.strength,
                )
                for a in divergence.affected
            ],
            has_diverged=divergence.has_diverged,
        )

    async def apply_drift(self, *, artifact_id: str, source_id: str) -> ArtifactDriftApplied:
        """Bring a document up to date with its source, and move the pin.

        Two halves, and the second is the one that is easy to forget:
        the text is revised *and* the ``DERIVED_FROM`` edge is re-pinned
        to the source's current version. Without the re-pin the
        document would be corrected and still reported as drifted,
        which is the same warning for the rest of time.

        Append-only, so this is safe to run: the previous version is
        still there to read and to compare against. Nothing is
        overwritten and nothing is lost if the answer is wrong.
        """
        drift = await self.drift(artifact_id=artifact_id, source_id=source_id)
        if drift.error:
            return ArtifactDriftApplied(error=drift.error)
        if not drift.has_diverged:
            return ArtifactDriftApplied(error="Nothing has diverged.")

        mgr = self._session.artifact_mgr
        dependent = await mgr.resolve_artifact(artifact_id)
        source = await mgr.resolve_artifact(source_id)
        if dependent is None or source is None:
            return ArtifactDriftApplied(error="One of those documents is not in the store.")

        current = await mgr.version(dependent.artifact_id, 0)
        before = (current.content if current else "") or ""

        divergence = await self._divergence_for(dependent, source, drift)
        if divergence is None:
            return ArtifactDriftApplied(error="Could not read both versions.")

        try:
            from agno.agent import Agent  # noqa: PLC0415 — heavy, and rarely reached

            from ember_code.core.config.models import ModelRegistry  # noqa: PLC0415

            agent = Agent(
                name="_artifact_drift_updater",
                model=ModelRegistry(self._session.settings).get_model(),
                markdown=False,
            )
            response = await agent.arun(DriftUpdate.prompt_for(divergence))
        except Exception as exc:  # noqa: BLE001 — a model call, many failure modes
            logger.info("drift update failed for %s: %s", artifact_id, exc)
            return ArtifactDriftApplied(error=f"Could not write the update: {exc}")

        # Agno does not raise on a provider failure. It logs, sets
        # ``status`` to ERROR, and puts the *error message* into
        # ``content`` — so the ``except`` above never fires and the
        # text reads as an answer. Checking only for emptiness let
        # "Token Plan usage limit reached: Upgrade your Token Plan…"
        # through as a document, and this writes a version.
        if _run_failed(response):
            detail = str(getattr(response, "content", "") or "").strip()
            return ArtifactDriftApplied(
                error=f"The model did not answer: {detail}"
                if detail
                else "The model did not answer."
            )
        updated = _strip_fence(str(getattr(response, "content", "") or "").strip())
        if not updated:
            return ArtifactDriftApplied(error="The update came back empty.")

        stale = StaleDependent(
            dependent_artifact_id=dependent.artifact_id,
            dependent_name=dependent.name or dependent.artifact_id,
            dependent_n=int(dependent.head_n or 0),
            source_artifact_id=source.artifact_id,
            source_name=source.name or source.artifact_id,
            pinned_n=drift.pinned_n,
            source_head_n=drift.source_head_n,
        )
        plan = await mgr.reconcile_plan(stale)
        if plan is None:
            return ArtifactDriftApplied(error="Could not build the update.")

        # Unchanged text is a real answer, not a failure — the document
        # was already correct and only its pin was behind. Re-pinning
        # alone is exactly what ``accept_reconcile`` does with no
        # content, so say so rather than writing an identical version.
        same = updated.strip() == before.strip()
        result = await mgr.accept_reconcile(plan=plan, content=None if same else updated)
        if not result.success:
            return ArtifactDriftApplied(error=result.message or "The update was not written.")

        return ArtifactDriftApplied(
            ok=True,
            n=int(getattr(result, "n", 0) or 0),
            pinned_to=drift.source_head_n,
            text_unchanged=same,
            message=result.message or "",
        )

    async def _divergence_for(
        self, dependent: Any, source: Any, drift: ArtifactDrift
    ) -> Divergence | None:
        """Rebuild the divergence the update prompt needs."""
        mgr = self._session.artifact_mgr
        left = await mgr.version(source.artifact_id, drift.pinned_n)
        right = await mgr.version(source.artifact_id, drift.source_head_n)
        mine = await mgr.version(dependent.artifact_id, 0)
        if left is None or right is None or mine is None:
            return None
        diff = build_side_by_side(
            left.content or "",
            right.content or "",
            from_n=drift.pinned_n,
            to_n=drift.source_head_n,
        )
        return Divergence(
            dependent_id=dependent.artifact_id,
            dependent_name=dependent.name or dependent.artifact_id,
            dependent_n=int(dependent.head_n or 0),
            source_id=source.artifact_id,
            source_name=source.name or source.artifact_id,
            pinned_n=drift.pinned_n,
            source_head_n=drift.source_head_n,
            changes=[
                (row.kind.value, row.left, row.right)
                for row in diff.rows
                if row.kind is not RowKind.EQUAL
            ],
            dependent_content=mine.content or "",
            source_head_content=right.content or "",
        )

    async def comment(
        self,
        *,
        artifact_id: str,
        n: int = 0,
        body: str,
        quote: str = "",
        quote_start: int = -1,
        quote_end: int = -1,
        thread_id: str = "",
        parent_id: str = "",
        origin: str = "local",
    ) -> ArtifactCommentPosted:
        """Leave a comment on the local copy, addressed to the agent.

        This is the local half of the comment model. Written here, in
        igni, with no one else in the room — so it is an instruction,
        and the answer to it is a revision rather than a reply.

        Nothing is sent to the server. A comment written locally is
        one side of a conversation the people on the shared copy are
        not part of; what reaches them is the version the agent writes
        in response, which is a thing they can read.
        """
        mgr = self._session.artifact_mgr
        if self._session.artifacts is None:
            return ArtifactCommentPosted(message="Artifacts are disabled.")
        text = (body or "").strip()
        if not text:
            return ArtifactCommentPosted(message="A comment needs something in it.")

        summary = await mgr.resolve_artifact(artifact_id)
        if summary is None:
            return ArtifactCommentPosted(message=f"No document {artifact_id!r}.")
        # Version 0 means "whatever is current", the same convention
        # the rest of the artifact RPCs use. Pinning it here rather
        # than storing a zero keeps the comment anchored to the text
        # that was actually on screen.
        version = n or int(summary.head_n or 0)

        try:
            speaker = CommentOrigin(origin)
        except ValueError:
            return ArtifactCommentPosted(message=f"Unknown comment origin: {origin!r}")
        if speaker is CommentOrigin.WEB:
            # Writing a web comment from here would put words in the
            # mouths of people who are not in this conversation.
            return ArtifactCommentPosted(message="Only local turns can be recorded here.")

        comment = await mgr.add_local_comment(
            artifact_id=summary.artifact_id,
            n=version,
            body=text,
            quote=quote,
            quote_start=quote_start if quote_start >= 0 else None,
            quote_end=quote_end if quote_end >= 0 else None,
            author=self._local_author() if speaker is CommentOrigin.LOCAL else "igni",
            thread_id=thread_id,
            parent_id=parent_id or None,
            origin=speaker,
        )
        if comment is None:
            return ArtifactCommentPosted(message="Could not record that comment.")

        # The thread is read *after* the write so the new comment is
        # in it; ``history_for`` takes it back out. One query either
        # way, and no chance of assembling a thread that is missing
        # the thing being asked about.
        thread = await mgr.thread_for(artifact_id=summary.artifact_id, thread_id=comment.thread_id)
        history = CommentConversation.history_for(thread, exclude_id=comment.comment_id)

        # The agent's own turns are recorded so the thread reads as a
        # transcript, but nothing is asked of the agent by replaying
        # what it already said — so no prompt comes back for one.
        if speaker is not CommentOrigin.LOCAL:
            return ArtifactCommentPosted(
                ok=True, comment_id=comment.comment_id, thread_id=comment.thread_id
            )

        return ArtifactCommentPosted(
            ok=True,
            comment_id=comment.comment_id,
            thread_id=comment.thread_id,
            prompt=CommentConversation.prompt_for(
                comment,
                document_name=summary.name or summary.artifact_id,
                thread=thread,
            ),
            history_count=len(history),
        )

    def _local_author(self) -> str:
        """Who is writing, as far as this machine knows.

        The signed-in account when there is one — it is the same name
        the shared copy would show — and otherwise empty, which reads
        as "me" rather than inventing a identity that does not match
        anything on the server.
        """
        auth = getattr(self._session, "cloud_auth", None)
        return str(getattr(auth, "email", "") or "")

    async def resolve_comment(
        self, *, comment_id: str, status: str, content: str = ""
    ) -> ArtifactCommentResolved:
        """Accept, reject or resolve one comment.

        Accepting with ``content`` appends a version **first**, then
        records the acceptance against the version that carried it.
        That order is what keeps "somebody asked for this" joined to
        "here is where it happened" — recording acceptance first and
        writing afterwards leaves a closed thread pointing at nothing
        whenever the write fails.
        """
        sync = self._collaboration()
        if sync is None:
            return ArtifactCommentResolved(
                ok=False,
                message="Resolving a comment needs a signed-in igni account — run `igni login`.",
            )
        return await self._session.artifact_mgr.resolve_comment(
            comment_id=comment_id, status=status, content=content, sync=sync
        )

    def _collaboration(self) -> CollaborationSync | None:
        """The sync client, built from the session's cloud credentials.

        Built here rather than in ``SessionArtifactManager`` because
        this is where the credentials are: the manager holds settings
        and a project directory, and ``Settings`` carries neither a
        server URL nor a token — those live on
        ``session.cloud_auth``, which is also where ``share`` reads
        them. The first cut asked the settings object and silently got
        nothing, so every sync reported zero and every resolve claimed
        the account was signed out.
        """
        artifacts = self._session.artifacts
        if artifacts is None:
            return None
        auth = getattr(self._session, "cloud_auth", None)
        sync = CollaborationSync(
            artifacts,
            server_url=getattr(auth, "server_url", "") or "",
            access_token=getattr(auth, "access_token", None) or "",
        )
        return sync if sync.is_configured else None

    async def sync_comments(self) -> int:
        """Pull comment activity and project it. Returns how many landed."""
        sync = self._collaboration()
        if sync is None:
            return 0
        projected, cursor = await sync.pull_comments(cursor=self._comment_cursor)
        self._comment_cursor = cursor
        return projected

    async def confirm_source(self, entry_id: str, source_id: str) -> bool:
        return await self._session.artifact_mgr.confirm_source(ref=entry_id, source_ref=source_id)

    async def dismiss_source(self, entry_id: str, source_id: str) -> bool:
        return await self._session.artifact_mgr.dismiss_source(ref=entry_id, source_ref=source_id)

    async def reconcile_diff(self, entry_id: str, source_id: str) -> str:
        """The pinned→head diff behind one stale dependency."""
        rows = await self._session.artifact_mgr.stale()
        target = next(
            (
                r
                for r in rows
                if r.dependent_artifact_id == entry_id and r.source_artifact_id == source_id
            ),
            None,
        )
        if target is None:
            return ""
        plan = await self._session.artifact_mgr.reconcile_plan(target)
        return plan.source_diff if plan else ""

    # ── Ask ───────────────────────────────────────────────────────────

    async def ask(self, question: str) -> ArtifactQueryResult:
        """Answer a plain-English question about the artifact graph.

        Runs entirely on this machine: the question goes to the
        session's own configured model, the Cypher runs against the
        local Neo4j sidecar, and nothing leaves the host. There is no
        server-side counterpart and there is not meant to be — the
        graph this reads does not exist anywhere but here.
        """
        planner = self._planner()
        if planner is None:
            return ArtifactQueryResult(question=question, error="Artifacts are disabled.")
        plan = await planner.ask(question)
        return _query_result(
            question=plan.question,
            cypher=plan.cypher,
            result=plan.result,
            attempts=plan.attempts,
            unanswerable=plan.unanswerable,
            error=plan.error,
        )

    async def run_cypher(self, cypher: str) -> ArtifactQueryResult:
        """Run a query the user edited by hand.

        No planning and no retry. The author can see the error and fix
        it, and rewriting their query underneath them would be worse
        than showing them what the database said.
        """
        planner = self._planner()
        if planner is None:
            return ArtifactQueryResult(cypher=cypher, error="Artifacts are disabled.")
        result = await planner.run_raw(cypher)
        return _query_result(question="", cypher=result.cypher or cypher, result=result)

    def _planner(self) -> ArtifactQueryPlanner | None:
        """Build the planner against this session's store and model.

        Constructed per call rather than cached: the model can change
        under a session (``/model``), and a planner holding the old
        one would keep writing queries with a model the user switched
        away from.
        """
        artifacts = self._session.artifacts
        client = getattr(artifacts, "client", None)
        driver = getattr(client, "_driver", None)
        if artifacts is None or driver is None:
            return None
        service = ArtifactCypherService(
            driver,
            client.project_id,
            embedder=getattr(artifacts, "_embedder", None),
        )
        return ArtifactQueryPlanner(service, model_factory=self._model_for_planning)

    def _model_for_planning(self) -> Any:
        from ember_code.core.config.models import ModelRegistry

        return ModelRegistry(self._session.settings).get_model()

    # ── Export and share ──────────────────────────────────────────────

    async def export(
        self,
        entry_id: str,
        version: int = 0,
        destination: str = "",
        fmt: str = "html",
    ) -> ArtifactExportResult:
        """Write the artifact to disk as HTML, Markdown or PDF."""
        artifacts = self._session.artifacts
        if artifacts is None:
            return ArtifactExportResult(error="Artifacts are disabled.")
        try:
            chosen = ExportFormat.parse(fmt)
        except ValueError as exc:
            return ArtifactExportResult(error=str(exc))
        exporter = ArtifactExporter(artifacts)
        target = Path(destination).expanduser() if destination else self._session.project_dir
        try:
            written = await exporter.write(entry_id, target, version=version, fmt=chosen)
        except PdfUnavailable as exc:
            # Its own branch because it is the one failure the reader
            # can fix, and the fix is a command rather than a retry.
            return ArtifactExportResult(format=chosen.value, error=str(exc))
        except OSError as exc:
            return ArtifactExportResult(format=chosen.value, error=f"Could not write export: {exc}")
        if written is None:
            return ArtifactExportResult(
                format=chosen.value, error=f"Artifact {entry_id} not found."
            )
        return ArtifactExportResult(path=str(written), format=chosen.value)

    async def open_export(self, path: str) -> ArtifactOpenResult:
        """Hand an exported file to whatever opens it on this machine.

        Narrow on purpose. This takes a path from the UI and gives it
        to the operating system, so the only paths it will accept are
        ones this method could itself have produced: a real file, with
        an extension we export, inside the project. Without those
        checks the RPC is "open any file on this machine, by name" —
        which is not what the button says it does.
        """
        target = Path(path).expanduser()
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            return ArtifactOpenResult(error="That file is no longer there.")
        if not resolved.is_file():
            return ArtifactOpenResult(error="That is not a file.")
        if resolved.suffix.casefold().lstrip(".") not in {f.value for f in ExportFormat}:
            return ArtifactOpenResult(error="Only exported documents can be opened from here.")
        project = Path(self._session.project_dir).expanduser().resolve()
        if not resolved.is_relative_to(project):
            return ArtifactOpenResult(error="That file is outside the project.")
        opened = await asyncio.to_thread(FileOpener.open, resolved)
        if not opened:
            return ArtifactOpenResult(error="Could not open that file.")
        return ArtifactOpenResult(opened=True)

    async def preview(self, entry_id: str, version: int = 0) -> str:
        """The rendered page as a string, for the in-app viewer."""
        artifacts = self._session.artifacts
        if artifacts is None:
            return ""
        bundle = await ArtifactExporter(artifacts).bundle(entry_id, version=version)
        return bundle.html if bundle else ""

    async def share(
        self,
        entry_id: str,
        version: int = 0,
        visibility: str = "private",
        audience: list[str] | None = None,
        together_with: list[str] | None = None,
    ) -> ArtifactShareResult:
        """Publish a projected snapshot and return its URL.

        ``visibility`` is ``private``, ``users``, ``domain`` or
        ``public``. ``domain`` means everyone whose account email
        shares the author's, and the server derives it from the
        signed-in account rather than taking it from here.

        There is no ``org``: every account gets an organisation
        created for it at sign-up, so that scope usually contained
        only its author.
        """
        artifacts = self._session.artifacts
        if artifacts is None:
            return ArtifactShareResult(error="Artifacts are disabled.")
        auth = getattr(self._session, "cloud_auth", None)
        publisher = ArtifactPublisher(
            server_url=getattr(auth, "server_url", "") or "",
            access_token=getattr(auth, "access_token", None),
        )
        if not publisher.is_configured:
            return ArtifactShareResult(
                error="Sharing needs a signed-in igni account — run `igni login` first."
            )
        bundle = await ArtifactExporter(artifacts).share_bundle(
            entry_id, version=version, together_with=together_with
        )
        if bundle is None:
            return ArtifactShareResult(error=f"Artifact {entry_id} not found.")
        try:
            url = await publisher.publish(bundle, visibility=visibility, audience=audience or [])
        except Exception as exc:
            logger.warning("Artifact share failed: %s", exc)
            return ArtifactShareResult(error=f"Share failed: {exc}")
        return ArtifactShareResult(url=url, version=bundle.n)

    # ── Admin ─────────────────────────────────────────────────────────

    async def remove(self, entry_id: str) -> ArtifactRemoveResult:
        """Delete one artifact and its version history."""
        if self._session.artifacts is None:
            return ArtifactRemoveResult(removed=False, error="Artifacts are disabled.")
        try:
            removed = await self._session.artifact_mgr.delete(entry_id)
            return ArtifactRemoveResult(removed=removed)
        except Exception as exc:
            return ArtifactRemoveResult(removed=False, error=str(exc))

    async def auto_sync(self) -> str | None:
        """Sync the shared file on startup. Returns a status message."""
        if self._session.artifacts is None:
            return None
        try:
            result = await self._session.artifact_mgr.sync_from_file()
            if result:
                return f"Artifacts synced: {result.summary}"
        except Exception as exc:
            logger.debug("artifact sync_from_file failed (%s)", exc)
        return None

    async def _resolve(self, ref: str) -> ArtifactSummary | None:
        artifacts = self._session.artifacts
        if artifacts is None:
            return None
        return await artifacts.resolve(ref)

    @staticmethod
    def _name_for(summary: ArtifactSummary) -> str:
        """Best display label when an artifact has no name.

        Ingested documents are named after their path or URL, so the
        basename is the readable form; anything else falls back to the
        id rather than an empty cell.
        """
        origin = (summary.origin or "").strip()
        if origin:
            if origin.startswith(("http://", "https://")):
                return origin
            return PurePosixPath(origin).name or origin
        return summary.artifact_id


def _query_result(
    *,
    question: str,
    cypher: str,
    result: CypherResult | None,
    attempts: int = 0,
    unanswerable: bool = False,
    error: str = "",
) -> ArtifactQueryResult:
    """One mapping from the service's result to the wire shape.

    Shared by the planned and the hand-edited path so the panel cannot
    tell them apart — it renders a table and a graph either way, and
    the only difference is whether a model wrote the query.
    """
    graph = ArtifactGraphView()
    if result is not None:
        graph = ArtifactGraphView(
            nodes=[
                ArtifactGraphNode(id=n.id, label=n.label, kind=n.kind, version=n.version)
                for n in result.graph.nodes
            ],
            edges=[
                ArtifactGraphEdge(source=e.source, target=e.target, label=e.label, stale=e.stale)
                for e in result.graph.edges
            ],
        )
    return ArtifactQueryResult(
        question=question,
        cypher=cypher,
        columns=list(result.columns) if result else [],
        rows=list(result.rows) if result else [],
        graph=graph,
        truncated=bool(result and result.truncated),
        attempts=attempts,
        unanswerable=unanswerable,
        error=error or (result.error if result else ""),
    )
