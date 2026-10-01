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
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ember_code.backend.file_opener import FileOpener
from ember_code.backend.schemas_artifacts import (
    AccessAsk,
    ArtifactAccessAsked,
    ArtifactAccessAsks,
    ArtifactCandidateEvidence,
    ArtifactCandidateVerdict,
    ArtifactColleagues,
    ArtifactCommentAnchor,
    ArtifactCommentPosted,
    ArtifactCommentRelevance,
    ArtifactCommentReplied,
    ArtifactCommentResolved,
    ArtifactCommentRow,
    ArtifactDependent,
    ArtifactDraft,
    ArtifactDraftPublished,
    ArtifactDraftSaved,
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
    ArtifactLocked,
    ArtifactMentions,
    ArtifactNotification,
    ArtifactNotifications,
    ArtifactOpened,
    ArtifactOpenResult,
    ArtifactPeople,
    ArtifactProvenance,
    ArtifactQueryResult,
    ArtifactRemoveResult,
    ArtifactRevised,
    ArtifactRoom,
    ArtifactShareResult,
    ArtifactStaleRow,
    ArtifactStatus,
    ArtifactTree,
    ArtifactTreeChanged,
    ArtifactTreeNode,
    ArtifactVersionRow,
    ArtifactViewGranted,
    ArtifactWaiting,
    CandidateOverlapRow,
    Colleague,
    DriftAffectedRow,
    MentionCandidate,
    ProvenanceMatchRow,
    ProvenancePassageRow,
    RosterPerson,
)
from ember_code.core.artifacts import side_by_side
from ember_code.core.artifacts.anchoring import RelevanceReview, surrounding, track_anchor
from ember_code.core.artifacts.collaboration import (
    CollaborationSync,
    NotAllowed,
    WaitingStream,
)
from ember_code.core.artifacts.conversation import CommentConversation
from ember_code.core.artifacts.cypher import ArtifactCypherService, CypherResult
from ember_code.core.artifacts.draft_desk import (
    DraftDesk,
    DraftRefused,
    PublishedUnderneath,
)
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
    Comment,
    CommentOrigin,
    EdgeState,
    SourceDeclaration,
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
from ember_code.core.broadcast_schema import BroadcastEvent
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)

#: How many changes to a local-only document are kept before the
#: window that typed them is asked for the whole thing instead.
#:
#: The same trade the server makes on its own log, for the same
#: reason: a list of changes grows without bound and a document does
#: not. Larger than the server's, because nothing here is paying for
#: Redis memory — only this process, for as long as it runs.
ALONE_SNAPSHOT_EVERY = 200

#: What a destination the server does not have is called.
#:
#: A parent in this tree is a folder **or a document** — a page holds
#: its sub-pages — so the one place something can be dropped has two
#: kinds, and "No such folder." named the wrong one every time
#: somebody dragged a page onto a page. The server says the same
#: sentence back; they are deliberately identical, so the reader is
#: not told two different things about one refusal.
_NO_SUCH_PLACE = "No such folder or document."


def unreachable(exc: Exception) -> str:
    """The sentence shown when a call to the server did not come back.

    Written through here rather than interpolated at each of the
    dozen-odd call sites, because several of the exceptions that land
    on those call sites stringify to **nothing**. ``httpx.ReadError``
    and its neighbours carry the cause in their type and not in their
    message, so ``unreachable(exc)`` renders as a
    colon and a blank — a reader is told something went wrong and
    given no word about what, which is worse than the raw class name
    they would at least be able to repeat to somebody.
    """
    said = str(exc).strip()
    return f"Could not reach the server: {said or type(exc).__name__}"


@dataclass
class OpenRoom:
    """One document this machine has open, and who is watching it."""

    artifact_id: str
    #: Every window with this document on screen. The room comes down
    #: when the last one goes, and not before.
    watchers: set[str] = field(default_factory=set)
    #: Everything typed into it, so a window that opens the editor
    #: later can catch up in one answer.
    log: list[str] = field(default_factory=list)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    #: Where a keystroke goes on its way to the server. ``None`` for a
    #: document nobody has shared: the room is this machine, and there
    #: is nowhere further for it to go.
    outbox: asyncio.Queue[dict[str, Any]] | None = None


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
        # The sync client, kept for its connection pool and its
        # slug cache. See :meth:`_collaboration` for why it is held
        # rather than built per call, and for what invalidates it.
        self._sync: CollaborationSync | None = None
        self._sync_for: tuple[int, str, str] | None = None
        # The document whose room this session is in, and the handles
        # to get out of it. One at a time — see :meth:`watch_room`.
        #: The documents this machine has open, by artifact id. One
        #: room each, shared by every window watching that document.
        self._rooms: dict[str, OpenRoom] = {}
        #: Changes to documents nobody has shared, per document, kept
        #: past the last window that had them open — there is no
        #: server holding a log for these.
        self._alone: dict[str, list[str]] = {}
        #: The server's comment channel, followed for as long as this
        #: process runs. One per account — see :meth:`watch_comments`.
        self._waiting_stream: WaitingStream | None = None
        self._waiting_stream_task: asyncio.Task[None] | None = None
        self._waiting_stream_for: tuple[str, str] | None = None
        # One adoption at a time per slug. Following a link fires the
        # lookup from an effect, and React runs effects twice in
        # development — two calls raced, both found nothing locally,
        # and both created the document. Same name, so the same
        # derived id, so one artifact written twice and listed twice.
        self._adopting: dict[str, asyncio.Lock] = {}

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

    async def add(self, source: str, parent_id: str = "", kind: str = "") -> msg.Info | msg.Error:
        """Add content from the panel.

        A URL or path is ingestion — the source is self-evident and no
        declaration is asked for. Pasted text is a new artifact with
        no antecedent, which the panel states rather than inventing
        one.

        **``kind`` is what the person chose**, and it is believed. The
        panel has three buttons — URL, path, paste — and used to send
        only the string, leaving this to guess from the characters in
        it. The guess was "contains a slash, so it is a path", which
        is true of most prose: pasting a paragraph with a date, a
        fraction or a fenced ``json-render`` block in it tried to open
        a file and failed with "file name too long", quoting the
        paragraph back.

        Empty still guesses, for callers that have nothing better to
        say.

        ``parent_id`` is where it goes in the tree, and it is passed at
        creation rather than moved afterwards: a document that appears
        at the top level and then jumps is two events for one
        intention, and if the move fails it has quietly landed
        somewhere nobody asked for.
        """
        mgr = self._session.artifact_mgr
        asked = (kind or "").strip().lower()
        looks_like_url = source.startswith(("http://", "https://"))
        looks_like_path = "/" in source or source.startswith(".")
        if asked == "url" or (not asked and looks_like_url):
            result = await mgr.add_url(source)
        elif asked == "path" or (not asked and looks_like_path):
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
            # ``Error``, not ``Info``. The two are indistinguishable
            # to a caller that only reads ``text``, and the panel read
            # neither — so a refused write returned to the list as
            # though it had worked, with the reason on the floor. A
            # document that was not saved must not look like one that
            # was.
            return msg.Error(text=result.error or "Add failed.")

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
                    parent_id=c.parent_id or "",
                    origin=c.origin.value,
                    quote=c.quote,
                    quote_start=-1 if c.quote_start is None else c.quote_start,
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

    async def revise(self, *, entry_id: str, content: str) -> ArtifactRevised:
        """Write a document somebody edited by hand as a new version.

        Sources are left empty deliberately. ``ArtifactIndex.revise``
        inherits the confirmed edges from the version before, so
        editing a paragraph keeps the record of what the document was
        built on — which a hand edit has no opinion about and should
        not be able to erase by omission.

        The archived refusal is not repeated here. It lives on
        ``revise`` itself, where every other way of writing a version
        also passes; a second copy would be a second thing to forget
        to update.
        """
        if self._session.artifacts is None:
            return ArtifactRevised(error="Artifacts are disabled.")
        if not content.strip():
            return ArtifactRevised(error="Refusing to save an empty document.")
        result = await self._session.artifacts.revise(
            ref=entry_id,
            content=content,
            sources=SourceDeclaration(),
            author=self._local_author(),
        )
        if not result.success:
            return ArtifactRevised(error=result.error or "Could not save that.")
        return ArtifactRevised(ok=True, n=result.n, message=result.message)

    async def create_folder(self, *, name: str, parent_id: str = "") -> ArtifactTreeChanged:
        """A place to put things, at the top or inside something else."""
        if self._session.artifacts is None:
            return ArtifactTreeChanged(error="Artifacts are disabled.")
        title = name.strip()
        if not title:
            return ArtifactTreeChanged(error="A folder needs a name.")
        shared_id: Any = None
        # Where it is going has to be somewhere the server can file
        # into, the same as any other move — a folder made offline, or
        # a page that has never been published, is a destination this
        # machine can see and that one cannot.
        ready = await self._shared_destination(parent_id) if parent_id else True
        if isinstance(ready, ArtifactTreeChanged):
            return ready
        if ready:
            # The server names it, and the graph adopts that name. Two
            # copies that each mint an id agree the folder exists and
            # about nothing else, and every later operation addresses
            # whichever one the caller happened to be looking at.
            shared_id = await self._shared_folder(
                lambda sync: sync.create_folder(name=title, parent_id=parent_id or ""),
                missing=_NO_SUCH_PLACE,
            )
            if isinstance(shared_id, ArtifactTreeChanged):
                return shared_id
        folder_id = await self._session.artifacts.client.create_folder(
            name=title, parent_id=parent_id or "", folder_id=shared_id or ""
        )
        return ArtifactTreeChanged(ok=True, node_id=folder_id, message=f"Created {title}.")

    async def rename_folder(self, *, folder_id: str, name: str) -> ArtifactTreeChanged:
        if self._session.artifacts is None:
            return ArtifactTreeChanged(error="Artifacts are disabled.")
        title = name.strip()
        if not title:
            return ArtifactTreeChanged(error="A folder needs a name.")
        refused = await self._shared_folder(
            lambda sync: sync.update_folder(folder_id=folder_id, name=title)
        )
        if isinstance(refused, ArtifactTreeChanged):
            return refused
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
        refused = await self._shared_folder(lambda sync: sync.delete_folder(folder_id=folder_id))
        if isinstance(refused, ArtifactTreeChanged):
            return refused
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

    async def _shared_folder(self, call: Any, *, missing: str = "No such folder.") -> Any:
        """Do a filing operation on the server before doing it here.

        Answers an :class:`ArtifactTreeChanged` when the server
        refused, which the caller must return unchanged — the graph
        must not be moved to a shape the shared copy rejected.
        Anything else is the server's own answer, or ``None`` when
        there is no server to ask.

        ``missing`` is what a 404 is called, because the same refusal
        means different things to the person reading it. Renaming
        something that is gone is about a folder; filing something is
        about a destination that may equally have been a document, and
        saying "no such folder" there named the wrong thing — about an
        id nobody had mentioned, for a page they were looking at.
        """
        sync = self._collaboration()
        if sync is None:
            return None
        try:
            return await call(sync)
        except NotAllowed:
            return ArtifactTreeChanged(error=missing)
        except Exception as exc:  # noqa: BLE001 — network, auth, permissions
            logger.info("filing did not reach the server: %s", exc)
            return ArtifactTreeChanged(error=unreachable(exc))

    async def _shared_place(
        self,
        *,
        node_id: str,
        parent_id: str | None = None,
        to_top: bool = False,
        archived: bool | None = None,
    ) -> Any:
        """The same, for something that may be a folder or a document.

        The tree holds both and the caller has one id, so which
        endpoint applies is decided by asking whether the id names a
        document. A folder is what is left — checked in that direction
        because a document has a shared identity to look up and a
        folder's id is the same string on both sides.

        **Both ends may be either kind.** A document holds its
        sub-pages here the way a folder holds documents, so the
        destination is a node and not necessarily a folder — see
        :meth:`_shared_destination`, which is what makes sure it is
        somewhere the server can file into before anything is moved.
        """
        sync = self._collaboration()
        if sync is None:
            return None
        if parent_id and not to_top:
            ready = await self._shared_destination(parent_id)
            if isinstance(ready, ArtifactTreeChanged):
                return ready
            if not ready:
                # The destination is on this machine and nowhere else.
                # The local move still happens; the shared copy keeps
                # the arrangement it had, and gains this one when the
                # destination is published. Refusing instead would
                # mean a page cannot be dragged under a page that has
                # not been shared with anybody, which is most of them.
                return None
        summary = await self._session.artifacts.client.get_artifact(node_id)
        if summary is None:
            return await self._shared_folder(
                lambda s: s.update_folder(
                    folder_id=node_id,
                    parent_id=parent_id,
                    to_top=to_top,
                    archived=archived,
                ),
                missing=_NO_SUCH_PLACE,
            )
        try:
            slug = await sync.slug_for(node_id)
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("could not resolve %s on the server: %s", node_id, exc)
            return ArtifactTreeChanged(error=unreachable(exc))
        if not slug:
            # Never published. Nothing to file on the shared copy, and
            # not an error: the next write gives it a home, and the
            # local arrangement is what it has until then.
            return None
        return await self._shared_folder(
            lambda s: s.place_document(
                slug=slug, parent_id=parent_id, to_top=to_top, archived=archived
            ),
            missing=_NO_SUCH_PLACE,
        )

    async def _shared_destination(self, parent_id: str) -> Any:
        """Make sure the place something is being filed into is there.

        Answers ``True`` to go ahead, ``False`` when the destination
        cannot exist on the server yet and the move is this machine's
        alone, or an :class:`ArtifactTreeChanged` the caller returns
        unchanged.

        The two kinds are missing for different reasons and neither is
        the person's mistake. A **folder** made while this machine was
        signed out exists here and nowhere else, so it is created
        there first, outermost link down — otherwise filing into a
        folder somebody is looking straight at answers "no such
        folder". A **document** cannot be back-filled the same way:
        its shared identity is minted when it is published, and until
        then there is nothing on the server for a sub-page to point
        at. So the local arrangement stands on its own, which is the
        same answer this gives for a document being moved that has
        never been published.
        """
        if self._session.artifacts is None:
            return True
        summary = await self._session.artifacts.client.get_artifact(parent_id)
        if summary is None:
            refused = await self._shared_ancestry(parent_id)
            return True if refused is None else refused
        sync = self._collaboration()
        if sync is None:
            return False
        try:
            return bool(await sync.slug_for(parent_id))
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("could not resolve the destination %s: %s", parent_id, exc)
            return ArtifactTreeChanged(error=unreachable(exc))

    async def _shared_ancestry(self, folder_id: str) -> ArtifactTreeChanged | None:
        """Make sure a folder and everything above it exist on the server.

        Answers an :class:`ArtifactTreeChanged` when the server
        refused, which the caller returns unchanged; ``None`` when
        there is nothing to do or it is done.

        **Outermost first.** A folder cannot be created inside a
        parent the server has not got, so the chain is walked up from
        the destination and then created downwards. Anything already
        there is skipped — the server answers an existing folder of
        this owner's rather than failing, so a half-finished
        back-fill from a dropped connection simply finishes.

        Silent on the way past a folder that is not local either: the
        id may name something this machine has never seen, and a
        filing operation that has to report on the shape of the whole
        ancestry is a filing operation nobody can read the result of.
        The server has the last word, and refuses the place itself.
        """
        sync = self._collaboration()
        if sync is None or self._session.artifacts is None:
            return None
        local = {
            str(row.get("node_id") or ""): row
            for row in await self._session.artifacts.client.folders()
        }

        chain: list[dict[str, Any]] = []
        at = folder_id
        # ``seen`` guards a cycle that is already in the data. Moves
        # are checked for loops before they are written, so this
        # should not happen — and walking forever is a request that
        # never returns rather than an error anybody can read.
        seen: set[str] = set()
        while at and at in local and at not in seen:
            seen.add(at)
            chain.append(local[at])
            at = str(local[at].get("parent_id") or "")
        if not chain:
            return None

        for row in reversed(chain):
            node_id = str(row.get("node_id") or "")
            parent = str(row.get("parent_id") or "")
            made = await self._shared_folder(
                lambda s, row=row, parent=parent, node_id=node_id: s.create_folder(
                    name=str(row.get("name") or ""),
                    # Only a parent this walk has just made sure of.
                    # The outermost link may name a folder this
                    # machine does not have, and asking the server to
                    # create something inside an id neither side knows
                    # answers "no such folder" — about the wrong
                    # folder, which is the confusing failure this whole
                    # method exists to remove.
                    parent_id=parent if parent in seen else "",
                    folder_id=node_id,
                )
            )
            if isinstance(made, ArtifactTreeChanged):
                return made
            if made and str(made) != node_id:
                # The id was taken by another account, so the server
                # minted its own and the two copies now disagree about
                # what this folder is called. Filing into it would
                # fail again, one call later and less legibly. Said
                # here instead, where the folder in question is the
                # one the message names.
                logger.info("folder %s exists on the server as %s", node_id, made)
                return ArtifactTreeChanged(
                    error=f"“{row.get('name') or 'That folder'}” could not be filed on the server."
                )
        return None

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
        refused = await self._shared_place(node_id=node_id, archived=archived)
        if isinstance(refused, ArtifactTreeChanged):
            return refused
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
        refused = await self._shared_place(
            node_id=node_id,
            parent_id=parent_id or None,
            to_top=not parent_id,
        )
        if isinstance(refused, ArtifactTreeChanged):
            return refused
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
        """Comment on a document, through the server that holds it.

        Artifacts are shared, so a comment that never leaves this
        machine is invisible to the people it is addressed to. The
        write goes to the server and the server's answer is projected
        into the local graph, which makes the graph a cache of what
        everybody can see rather than a second opinion about it.

        A document nobody has published yet has no shared copy to
        write to, so one is published — privately, which is a row on
        the server and not an audience. Sharing it with somebody is
        still a separate, deliberate act.

        The local write remains, for the one case the server cannot
        answer: signed out, or unreachable. That is a cache entry
        waiting for a push, not a private conversation — and it is
        reported as such rather than looking like a successful post.
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

        # The shared copy first. Falls through to the local write when
        # there is no server to reach — the tail below is the same
        # either way, because what a comment *is* does not depend on
        # which of the two recorded it.
        comment = await self._comment_on_server(
            artifact_id=summary.artifact_id,
            version=version,
            body=text,
            quote=quote,
            quote_start=quote_start,
            quote_end=quote_end,
            parent_id=parent_id,
            speaker=speaker,
        )
        if comment is not None:
            return await self._answer_for(comment, mgr=mgr, speaker=speaker)

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
        return await self._answer_for(comment, mgr=mgr, speaker=speaker)

    async def _comment_on_server(
        self,
        *,
        artifact_id: str,
        version: int,
        body: str,
        quote: str,
        quote_start: int,
        quote_end: int,
        parent_id: str,
        speaker: CommentOrigin,
    ) -> Comment | None:
        """Write the comment to the shared copy, or answer ``None``.

        ``None`` is "no server to write to" — signed out, unreachable,
        or a refusal — and the caller falls back to the local graph.
        It is never "the comment was rejected for what it said": that
        raises, because silently keeping a refused comment on one
        machine is how a person comes to believe they have been heard.

        A reply goes through the thread it answers rather than being
        re-addressed to the document. The server takes the version,
        the quote and its offsets from the comment being replied to,
        which is the only way a reply cannot end up pointing somewhere
        its own thread does not.
        """
        sync = self._collaboration()
        if sync is None:
            return None
        try:
            slug = await sync.slug_for(artifact_id)
            if not slug:
                # A document with no row on the server. Not published
                # from here: a comment must not decide a document's
                # visibility, and publishing only the documents
                # somebody happens to comment on would migrate the
                # store halfway and leave it there. The seam is
                # creation — a document should have a server identity
                # because it was written, not because it was
                # discussed.
                return None
            written = (
                await sync.reply(comment_id=parent_id, body=body)
                if parent_id
                else await sync.comment(
                    slug=slug,
                    version=version,
                    body=body,
                    quote=quote,
                    quote_start=quote_start if quote_start >= 0 else None,
                    quote_end=quote_end if quote_end >= 0 else None,
                )
            )
            return await self._keep_origin(written, speaker)
        except NotAllowed:
            raise
        except Exception as exc:  # noqa: BLE001 — network, auth, DNS, timeouts
            logger.info("comment on %s did not reach the server: %s", artifact_id, exc)
            return None

    async def _keep_origin(self, comment: Comment | None, speaker: CommentOrigin) -> Comment | None:
        """Put back the origin the round trip lost.

        Whether a comment is an *instruction* or a *remark* is the
        load-bearing distinction in this model — it decides whether
        the agent may act on it — and the shared copy cannot carry
        it: ``artifact_comments`` has no such column, so everything
        written there comes back as a remark. A comment written in
        igni then stopped being something the agent could be asked
        about, and posting one failed outright on the refusal in
        ``prompt_for``.

        Restored from what this machine *sent*, which is the only
        place the answer exists. It survives locally and is lost
        again on any pull — so this is a patch over a gap in the
        server's schema, not a fix for it.
        """
        if comment is None or comment.origin is speaker:
            return comment
        corrected = comment.model_copy(update={"origin": speaker})
        # Re-projected, or the graph keeps the server's answer and
        # the distinction is lost on the next read instead of now.
        await self._session.artifacts.client.project_comments([corrected])
        return corrected

    async def _answer_for(
        self, comment: Comment, *, mgr: Any, speaker: CommentOrigin
    ) -> ArtifactCommentPosted:
        """What the caller gets back, wherever the comment was written."""
        summary = await mgr.resolve_artifact(comment.artifact_id)
        # The thread is read *after* the write so the new comment is
        # in it; ``history_for`` takes it back out. One query either
        # way, and no chance of assembling a thread that is missing
        # the thing being asked about.
        thread = await mgr.thread_for(artifact_id=comment.artifact_id, thread_id=comment.thread_id)
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
                document_name=(summary.name if summary else "") or comment.artifact_id,
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

    async def mentions(self, *, artifact_id: str, query: str = "") -> ArtifactMentions:
        """Who could be named in a comment on this document.

        Every answer comes from the server, including the access one.
        Whether a colleague can read a shared document is not a fact
        this machine holds — it is a property of how the document is
        shared, which lives where the document is shared.

        Failures are reported in ``error`` and never raised. This runs
        on a keystroke behind a popup; a document that was never
        published, an account that is signed out, and a server that is
        down all mean the same thing to the person typing, which is
        that no completions are coming.
        """
        sync = self._collaboration()
        if sync is None:
            return ArtifactMentions(error="Not signed in to a server.")
        try:
            slug = await sync.slug_for(artifact_id)
            if not slug:
                return ArtifactMentions(
                    error="This document has not been shared, so there is nobody to mention yet."
                )
            answer = await sync.mentionable(slug=slug, query=query)
        except NotAllowed:
            return ArtifactMentions(error="This document is not yours to read.")
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("mention lookup did not reach the server: %s", exc)
            return ArtifactMentions(error=unreachable(exc))
        return ArtifactMentions(
            people=[MentionCandidate(**person) for person in answer.get("people") or []],
            can_grant=bool(answer.get("can_grant")),
            owner_name=str(answer.get("owner_name") or ""),
            owner_email=str(answer.get("owner_email") or ""),
        )

    async def grant_view(self, *, artifact_id: str, identity: str) -> ArtifactViewGranted:
        """Let one more person read a shared document.

        Named by address rather than by id, because the address is
        what was written into the comment and what the access rule
        matches on — granting to an id the reader never saw would look
        right here and fail to match the mention.
        """
        who = identity.strip()
        if not who:
            return ArtifactViewGranted(error="Nobody was named.")
        sync = self._collaboration()
        if sync is None:
            return ArtifactViewGranted(error="Not signed in to a server.")
        try:
            slug = await sync.slug_for(artifact_id)
            if not slug:
                return ArtifactViewGranted(error="This document has not been shared.")
            await sync.grant_view(slug=slug, identity=who)
        except NotAllowed:
            # Both "no such document" and "not yours" arrive here, and
            # the server will not say which. The honest message covers
            # both without guessing at the one it did not say.
            return ArtifactViewGranted(error="This document is not yours to share.")
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("grant did not reach the server: %s", exc)
            return ArtifactViewGranted(error=unreachable(exc))
        return ArtifactViewGranted(ok=True, identity=who, message=f"{who} can now read this.")

    async def people(self, *, artifact_id: str) -> ArtifactPeople:
        """Who this document is shared with.

        Empty rather than an error for a document that was never
        shared. A private draft has nobody on it for a good reason,
        and the panel draws nothing at all in that case — reporting
        it would put a failure under every unpublished document.
        """
        sync = self._collaboration()
        if sync is None:
            return ArtifactPeople()
        try:
            slug = await sync.slug_for(artifact_id)
            if not slug:
                return ArtifactPeople()
            answer = await sync.roster(slug=slug)
        except NotAllowed:
            return ArtifactPeople()
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("roster lookup did not reach the server: %s", exc)
            return ArtifactPeople(error=unreachable(exc))
        return ArtifactPeople(
            people=[RosterPerson(**person) for person in answer.get("people") or []],
            reach=str(answer.get("reach") or "private"),
            domain=str(answer.get("domain") or ""),
            can_manage=bool(answer.get("can_manage")),
            url=str(answer.get("url") or ""),
        )

    async def colleagues(self, *, query: str = "") -> ArtifactColleagues:
        """Who this account could share a document with.

        Empty rather than an error when there is no server to ask.
        A picker with nothing in it says "nobody to pick" on its own,
        and a failure drawn over a sharing dialog reads as the sharing
        having failed.
        """
        sync = self._collaboration()
        if sync is None:
            return ArtifactColleagues()
        try:
            found = await sync.colleagues(query=query)
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("colleague lookup did not reach the server: %s", exc)
            return ArtifactColleagues(error=unreachable(exc))
        return ArtifactColleagues(people=[Colleague(**person) for person in found])

    # ── The draft ────────────────────────────────────────────────────

    def _draft_desk(self) -> DraftDesk | None:
        """Where an edit goes before it is a version.

        Given this controller's own sync client rather than the one
        the index holds: that one is built when the index is, and a
        sign-in part-way through a session would leave it writing as
        whoever was signed in before.
        """
        index = self._session.artifacts
        if index is None:
            return None
        return DraftDesk(index, sync=self._collaboration(), author=self._local_author())

    async def draft(self, *, artifact_id: str) -> ArtifactDraft:
        """What is being written on this document right now."""
        desk = self._draft_desk()
        if desk is None:
            return ArtifactDraft()
        try:
            found = await desk.read(artifact_id)
        except NotAllowed:
            # Not ours to read. Answered as "no draft" rather than as
            # an error: the document itself is on screen, and a lock
            # drawn over the version list says nothing the reader can
            # act on.
            return ArtifactDraft()
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("draft lookup did not reach the server: %s", exc)
            return ArtifactDraft(error=unreachable(exc))
        return ArtifactDraft(
            exists=found.exists,
            content=found.content,
            base_n=found.base_n,
            head_n=found.head_n,
            updated_by=found.updated_by,
        )

    async def save_draft(self, *, artifact_id: str, content: str) -> ArtifactDraftSaved:
        """Store an edit without making a version."""
        desk = self._draft_desk()
        if desk is None:
            return ArtifactDraftSaved(error="Artifacts are disabled.")
        try:
            await desk.save(artifact_id, content)
        except NotAllowed:
            return ArtifactDraftSaved(error="This document is not yours to edit.")
        except DraftRefused as refused:
            return ArtifactDraftSaved(error=str(refused))
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("draft save did not reach the server: %s", exc)
            return ArtifactDraftSaved(error=unreachable(exc))
        return ArtifactDraftSaved(ok=True)

    async def publish_draft(self, *, artifact_id: str) -> ArtifactDraftPublished:
        """Turn the draft into a version."""
        desk = self._draft_desk()
        if desk is None:
            return ArtifactDraftPublished(error="Artifacts are disabled.")
        try:
            n = await desk.publish(artifact_id)
            self.forget_local_draft(artifact_id)
        except PublishedUnderneath as diverged:
            # Not an error the reader caused, and the draft is intact:
            # said as ``stale`` so the editor stays open on it rather
            # than closing over the only copy.
            return ArtifactDraftPublished(stale=True, message=str(diverged))
        except NotAllowed:
            return ArtifactDraftPublished(error="This document is not yours to publish.")
        except DraftRefused as refused:
            return ArtifactDraftPublished(error=str(refused))
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("publishing a draft did not reach the server: %s", exc)
            return ArtifactDraftPublished(error=unreachable(exc))
        return ArtifactDraftPublished(ok=True, n=n, message=f"Published as v{n}.")

    # ── Arriving from a link ─────────────────────────────────────────

    async def open_shared(self, *, slug: str) -> ArtifactOpened:
        """Bring a shared document into this machine, and say where it is.

        What happens when somebody follows a link. The document lives
        on the server and belongs to whoever published it; this pulls
        its chain into the local graph so it can be opened where the
        reader's own documents are — with the tree beside it, the
        conversation next to it, and the whole history answerable to
        the same queries as anything they wrote themselves.

        **Adopted, not copied.** The local chain is a mirror of the
        server's, applied through the ordinary write path, so every
        version is chunked, embedded and edge-attached exactly as a
        locally authored one would be. A projection that skipped that
        would be present in the graph and absent from every answer.

        A reader who may not open it gets the locked state back rather
        than an error, because "you need to ask for this" is an answer
        and a 404 is not.
        """
        sync = self._collaboration()
        if sync is None:
            return ArtifactOpened(error="Not signed in to a server.")
        try:
            door = await sync.locked(slug=slug)
        except NotAllowed:
            return ArtifactOpened(error="No such document.")
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("could not open %s: %s", slug, exc)
            return ArtifactOpened(error=unreachable(exc))

        title = str(door.get("title") or "")
        if not door.get("open"):
            # Shut out. The caller draws the door, which needs the
            # title and the owner — both already in hand.
            return ArtifactOpened(
                locked=ArtifactLocked(
                    open=False,
                    title=title,
                    owner_name=str(door.get("owner_name") or ""),
                    owner_email=str(door.get("owner_email") or ""),
                    asked=bool(door.get("asked")),
                    declined=bool(door.get("declined")),
                )
            )

        if self._session.artifacts is None:
            return ArtifactOpened(error="Artifacts are disabled.")
        index = self._session.artifacts
        try:
            artifact_id = await self._adopt(sync=sync, slug=slug, title=title)
        except NotAllowed:
            return ArtifactOpened(error="No such document.")
        except Exception as exc:  # noqa: BLE001 — network, graph
            logger.info("could not adopt %s: %s", slug, exc)
            return ArtifactOpened(error=f"Could not open that document: {exc}")
        del index
        return ArtifactOpened(ok=True, artifact_id=artifact_id, title=title)

    async def _adopt(self, *, sync: CollaborationSync, slug: str, title: str) -> str:
        """Find or make the local artifact this slug belongs to, then sync it.

        Matched by name rather than by the id on the shared row. That
        id is the *author's* local identity for the document and means
        nothing in anybody else's graph — adopting by it would create
        rows whose ids collide with whatever the reader happens to
        have at that address. A name is what both sides actually agree
        on, and it is what the reader will search for later.

        One at a time per slug. The check for an existing document and
        the write that follows it are not atomic, and two calls
        arriving together both find nothing and both create — the name
        is the same, so the derived id is the same, and the result is
        one artifact written twice and listed twice. Two calls
        arriving together is not exotic: following a link fires this
        from an effect, and React runs effects twice in development.
        """
        lock = self._adopting.setdefault(slug, asyncio.Lock())
        async with lock:
            return await self._adopt_once(sync=sync, slug=slug, title=title)

    async def _adopt_once(self, *, sync: CollaborationSync, slug: str, title: str) -> str:
        client = self._session.artifacts.client
        existing = await client.find_by_name(title)
        if existing is None:
            # Created from the first remote version rather than empty:
            # an artifact at v1 with no content is a row that exists
            # and says nothing, and if the pull then fails the reader
            # is left holding it.
            first = await sync.first_version(slug=slug)
            written = await self._session.artifacts.create(
                name=title,
                content=first.content,
                sources=SourceDeclaration(),
                author=first.author_email,
                # The server has this already — it is where it came
                # from. Publishing it back would claim the reader
                # wrote somebody else's first draft.
                projecting=True,
            )
            # ``success``, not ``ok``. ``ArtifactWriteResult.ok`` is the
            # classmethod that *builds* a successful one, so reading it
            # as a flag gets a bound method — always truthy, so a
            # refused create read as a good one and the failure only
            # surfaced two lines later as a missing id.
            if not written.success or not written.artifact_id:
                raise RuntimeError(written.error or written.message or "could not create it here")
            ref = written.artifact_id
        else:
            ref = str(existing.artifact_id)

        # The rest of the chain, and the conversation with it.
        await sync.pull(slug=slug, ref=ref)
        return ref

    # ── Being let in ─────────────────────────────────────────────────

    async def locked(self, *, artifact_id: str) -> ArtifactLocked:
        """What this document is, for somebody who cannot read it.

        ``open`` for anything this account can already read, and for
        anything never shared — a local draft is not locked, it is
        simply local, and drawing a request form over one would ask
        somebody to petition themselves.
        """
        sync = self._collaboration()
        if sync is None:
            return ArtifactLocked(open=True)
        try:
            slug = await sync.slug_for(artifact_id)
            if not slug:
                return ArtifactLocked(open=True)
            answer = await sync.locked(slug=slug)
        except NotAllowed:
            return ArtifactLocked(error="No such document.")
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("access check did not reach the server: %s", exc)
            # Open, on a failure to ask. The document is on this
            # machine and readable from it; a server we cannot reach
            # is not grounds for putting a lock over work somebody
            # already has.
            return ArtifactLocked(open=True, error=unreachable(exc))
        return ArtifactLocked(
            open=bool(answer.get("open")),
            title=str(answer.get("title") or ""),
            owner_name=str(answer.get("owner_name") or ""),
            owner_email=str(answer.get("owner_email") or ""),
            asked=bool(answer.get("asked")),
            declined=bool(answer.get("declined")),
        )

    async def ask_access(
        self, *, artifact_id: str = "", slug: str = "", identity: str = "", note: str = ""
    ) -> ArtifactAccessAsked:
        """Ask to be let in — for yourself, or for somebody you named.

        Named by either the local artifact or the slug. The slug is
        what somebody arriving from a link has: the document is not in
        their graph, which is the whole reason they are asking, so
        there is no local id to name it by.
        """
        sync = self._collaboration()
        if sync is None:
            return ArtifactAccessAsked(error="Not signed in to a server.")
        try:
            if not slug:
                slug = await sync.slug_for(artifact_id)
            if not slug:
                return ArtifactAccessAsked(error="This document has not been shared.")
            await sync.ask_access(slug=slug, identity=identity, note=note)
        except NotAllowed:
            return ArtifactAccessAsked(error="That is not yours to ask about.")
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("access request did not reach the server: %s", exc)
            return ArtifactAccessAsked(error=unreachable(exc))
        who = identity.strip()
        return ArtifactAccessAsked(
            ok=True,
            identity=who,
            message=f"Asked for {who}." if who else "Asked.",
        )

    async def access_asks(self, *, artifact_id: str) -> ArtifactAccessAsks:
        """Who is waiting on this document. Empty unless it is yours."""
        sync = self._collaboration()
        if sync is None:
            return ArtifactAccessAsks()
        try:
            slug = await sync.slug_for(artifact_id)
            if not slug:
                return ArtifactAccessAsks()
            rows = await sync.waiting_on(slug=slug)
        except NotAllowed:
            # Not the owner, or not shared. Neither is an error worth
            # drawing: somebody who cannot settle requests has no use
            # for the queue.
            return ArtifactAccessAsks()
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("access queue did not reach the server: %s", exc)
            return ArtifactAccessAsks(error=unreachable(exc))
        return ArtifactAccessAsks(
            asks=[
                AccessAsk(
                    id=str(row.get("id") or ""),
                    identity=str(row.get("identity") or ""),
                    name=str(row.get("name") or ""),
                    asked_by_name=str(row.get("asked_by_name") or ""),
                    note=str(row.get("note") or ""),
                    created_at=str(row.get("created_at") or ""),
                )
                for row in rows
            ]
        )

    async def settle_access_ask(
        self, *, artifact_id: str, request_id: str, granted: bool
    ) -> ArtifactViewGranted:
        """Let somebody in, or decline. Owner only."""
        sync = self._collaboration()
        if sync is None:
            return ArtifactViewGranted(error="Not signed in to a server.")
        try:
            slug = await sync.slug_for(artifact_id)
            if not slug:
                return ArtifactViewGranted(error="This document has not been shared.")
            await sync.settle_ask(slug=slug, request_id=request_id, granted=granted)
        except NotAllowed:
            return ArtifactViewGranted(error="That is not yours to decide.")
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("settling a request did not reach the server: %s", exc)
            return ArtifactViewGranted(error=unreachable(exc))
        return ArtifactViewGranted(ok=True, message="Let them in." if granted else "Declined.")

    # ── Presence ─────────────────────────────────────────────────────

    async def watch_room(self, *, artifact_id: str, watcher: str = "") -> ArtifactRoom:
        """Join the room for a document, and answer with what is in it.

        **One room per document, not one room per machine.** It used
        to be the latter — opening a second document left the first —
        and with more than one window open that is not a tidy
        simplification, it is one window silently evicting another
        from a document it still has on screen.

        ``watcher`` is that window. Rooms are held for as long as
        somebody is watching and torn down when the last one leaves,
        so two windows on one document share a socket and neither can
        end the other's session.

        The draft comes back **here**, in the answer, as well as on
        the push channel. The server sends its log once, when the
        socket connects; a window that opens the editor a minute
        later would be waiting for a frame that went past before it
        was listening — which is exactly how long it seemed to take
        to load a draft, and why a second window seeded its own copy
        instead of joining the one being written.
        """
        held = self._rooms.get(artifact_id)
        if held is not None:
            held.watchers.add(watcher)
            return ArtifactRoom(ok=True, shared=held.outbox is not None, updates=list(held.log))

        sync = self._collaboration()
        slug = ""
        if sync is not None:
            try:
                slug = str(await sync.slug_for(artifact_id) or "")
            except Exception as exc:  # noqa: BLE001 — network, auth
                logger.info("could not resolve %s for presence: %s", artifact_id, exc)

        room = OpenRoom(artifact_id=artifact_id, watchers={watcher})
        self._rooms[artifact_id] = room

        if not slug or sync is None:
            # Never shared, so there is no room on any server and
            # nobody at another desk can be in it. Not an error: a
            # private draft has an empty roster for a good reason,
            # and saying so would put a warning under every document
            # somebody has not published yet.
            #
            # There is still a room, though — this machine. Two
            # windows open on one document here are two editors of
            # one draft, and they diverged silently: each wrote the
            # whole text when its typing paused, and the last one to
            # pause won. So the changes are kept and relayed here,
            # exactly as the server would, and the only thing the
            # document misses out on is other people.
            room.log = list(self._alone.get(artifact_id, ()))
            return ArtifactRoom(ok=True, shared=False, updates=list(room.log))

        room.outbox = asyncio.Queue()
        room.task = asyncio.create_task(
            sync.room(
                slug=slug,
                on_roster=lambda here: self._room_changed(artifact_id, here),
                on_frame=lambda frame: self._room_frame(artifact_id, frame),
                outbox=room.outbox,
                stop=room.stop,
            )
        )
        return ArtifactRoom(ok=True, shared=True, updates=[])

    async def room_state(self, *, artifact_id: str) -> ArtifactRoom:
        """Everything typed into this document so far, right now.

        Asked for rather than waited for. What an editor needs on
        opening is the whole log, and the log is here — held as it
        arrives so that a window joining late gets it in one answer
        instead of sitting through a timeout hoping for a broadcast
        that already happened.
        """
        held = self._rooms.get(artifact_id)
        if held is None:
            return ArtifactRoom(
                ok=False, shared=False, updates=list(self._alone.get(artifact_id, ()))
            )
        return ArtifactRoom(ok=True, shared=held.outbox is not None, updates=list(held.log))

    async def send_to_room(self, *, artifact_id: str, frame: dict[str, Any]) -> ArtifactRoom:
        """Put one frame on the open room socket.

        Everything about what is *in* the frame belongs to the editor
        that made it. This is a post box: it knows which rooms are
        open and refuses frames for any other, because a change typed
        into one document must never be relayed into another.

        **And straight back out to the windows here.** One machine
        has one of these processes and one socket per room, however
        many windows are open on it; the server relays a change to
        every socket *except* the one that sent it, which is correct
        and which makes the other windows on this machine invisible
        to each other. So a change is put on the local bus as well as
        on the wire. Applying it twice costs nothing — these changes
        commute and repeat harmlessly, which is the property the
        whole design rests on — and the window that typed it ignores
        its own echo.
        """
        held = self._rooms.get(artifact_id)
        if held is None:
            # Not an error worth showing. The room opens a moment
            # after the document does, and a keystroke in that moment
            # is one the editor still holds — the next one carries the
            # same state, because these are changes to a document, not
            # a stream that can lose its place.
            return ArtifactRoom(ok=False, shared=False)

        kind = str(frame.get("type") or "")
        update = str(frame.get("update") or "")
        self._remember(held, kind, update)
        # Changes *and* carets. The wire carries both and the server
        # sends neither back to the socket that produced it — which
        # is right, and which makes every window on this machine
        # invisible to every other, because they share that socket.
        # Only changes were echoed here, so two windows edited one
        # document while showing each other no cursor at all.
        if kind in ("edit", "cursor"):
            self._tell_windows(artifact_id, frame)
        if held.outbox is None:
            return ArtifactRoom(ok=True, shared=False)
        held.outbox.put_nowait(frame)
        return ArtifactRoom(ok=True, shared=True)

    def _remember(self, room: OpenRoom, kind: str, update: str) -> None:
        """Keep the log this machine can answer from.

        A mirror of what the server holds, kept for the same reason
        the server holds one: a window that opens the editor after
        the typing started has to be able to catch up, and neither
        end can build a document out of these — only an editor can.

        Compacted by asking whoever just typed for the whole thing,
        which is what the server does with its own log and what the
        ``snapshot`` frame is for. Without it the list grows for as
        long as the process runs.
        """
        if kind == "snapshot" and update:
            room.log = [update]
        elif kind == "edit" and update:
            room.log.append(update)
            if len(room.log) >= ALONE_SNAPSHOT_EVERY:
                self._tell_windows(room.artifact_id, {"type": "snapshot"})
                room.log = room.log[-ALONE_SNAPSHOT_EVERY:]
        if room.outbox is None:
            # A document only this machine has: its log has to outlive
            # the room, because there is no server keeping one.
            self._alone[room.artifact_id] = list(room.log)

    def forget_local_draft(self, artifact_id: str) -> None:
        """Drop the changes — the draft they described is gone.

        Called when a draft becomes a version. Leaving them would put
        the next window that opens the editor back into an edit of a
        document that has already been published.
        """
        self._alone.pop(artifact_id, None)
        held = self._rooms.get(artifact_id)
        if held is not None:
            held.log = []

    def _room_frame(self, artifact_id: str, frame: dict[str, Any]) -> None:
        """A frame off the wire: write it down, then pass it on.

        Unread on the way past — except to be counted. The draft is a
        pile of opaque changes and the only thing that can make a
        document out of them is the editor holding it, which is in
        the browser. This process holds the credentials, so it holds
        the socket, and that is the whole of its involvement.

        Only for frames that *arrive*. What this machine sends is
        written down by :meth:`send_to_room` before it goes; recording
        it here as well put every local keystroke in the log twice,
        which doubled what a joining window had to apply and tripped
        the snapshot threshold at half the intended length.
        """
        held = self._rooms.get(artifact_id)
        if held is None:
            return
        kind = str(frame.get("type") or "")
        if kind == "draft":
            # The server's whole log, on connecting. It replaces what
            # we had: it is the same log, from the side that stores
            # it.
            held.log = [str(u) for u in (frame.get("updates") or [])]
        elif kind == "edit":
            self._remember(held, "edit", str(frame.get("update") or ""))
        # A cursor is passed straight through and written down
        # nowhere — see ``_remember``, which knows about changes and
        # snapshots and deliberately not about this.
        self._tell_windows(artifact_id, frame)

    def _tell_windows(self, artifact_id: str, frame: dict[str, Any]) -> None:
        """Put a frame in front of every window drawing this document."""
        if artifact_id not in self._rooms:
            return
        self._session.broadcast_bus.emit(
            BroadcastEvent(
                channel="artifact_draft", payload={"artifact_id": artifact_id, "frame": frame}
            )
        )

    async def leave_room(self, *, artifact_id: str = "", watcher: str = "") -> None:
        """Stop holding a place, for one window.

        The room stays up while anybody else is watching. It used to
        come down on the first call from anywhere, which with two
        windows open meant either of them could end the other's
        editing session by navigating away.

        Awaits the task rather than cancelling and walking away: the
        socket's close is what removes this reader from everybody
        else's roster immediately, and not waiting for it means
        leaving a ghost in the document until the server's expiry
        catches up.
        """
        wanted = [artifact_id] if artifact_id else list(self._rooms)
        for one in wanted:
            room = self._rooms.get(one)
            if room is None:
                continue
            room.watchers.discard(watcher)
            if room.watchers:
                continue
            self._rooms.pop(one, None)
            room.stop.set()
            if room.task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(room.task, timeout=3.0)

    def _room_changed(self, artifact_id: str, here: list[dict[str, Any]]) -> None:
        """Push a new roster to whoever is drawing it.

        Dropped if nobody is in this document any more. A roster for
        a document nobody is looking at would arrive after the panel
        had already drawn a different one, and replace it.
        """
        if artifact_id not in self._rooms:
            return
        self._session.broadcast_bus.emit(
            BroadcastEvent(
                channel="artifact_room", payload={"artifact_id": artifact_id, "here": here}
            )
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

        **Kept, not rebuilt.** It was rebuilt on every call, which
        threw away two things the client holds: its connection pool,
        and ``slug_for``'s cache of which slug belongs to which
        artifact. Resolving a slug means listing every shared document
        this account has, so a fresh client per call turned that into
        a full listing per operation — unnoticeable for filing, and
        about a second per keystroke once the mention menu started
        asking on every letter typed.

        Rebuilt when the credentials change, which is the one thing
        that makes the old client wrong: a cached client signed in as
        the previous account would answer with their documents.
        """
        artifacts = self._session.artifacts
        if artifacts is None:
            return None
        auth = getattr(self._session, "cloud_auth", None)
        server_url = getattr(auth, "server_url", "") or ""
        access_token = getattr(auth, "access_token", None) or ""
        credentials = (id(artifacts), server_url, access_token)
        if self._sync is None or self._sync_for != credentials:
            sync = CollaborationSync(
                artifacts,
                server_url=server_url,
                access_token=access_token,
            )
            self._sync = sync if sync.is_configured else None
            self._sync_for = credentials
        return self._sync

    async def watch_comments(self) -> ArtifactWaiting:
        """Follow the server's comment stream, and say what is waiting.

        **The stream is what makes a notification a notification.** A
        comment is written by somebody at another desk; without a
        channel carrying that over, the only way to learn of it is to
        ask, and a screen that only refreshes when you press a button
        tells you nothing you did not already know by pressing it.

        Asked for once, by whatever is drawing the count, and then
        held for the life of the process. The stream itself is a
        latency optimisation over :meth:`sync_comments` — it carries
        a cursor and never a comment — so losing it costs freshness
        and never correctness, which is why nothing here treats a
        dropped connection as an error worth reporting.
        """
        sync = self._collaboration()
        if sync is None:
            return ArtifactWaiting()
        credentials = (sync.server_url, sync.access_token)
        if self._waiting_stream is not None and self._waiting_stream_for != credentials:
            # Signed in as somebody else. The old stream is following
            # the previous account's comments.
            self._waiting_stream.stop()
            self._waiting_stream = None
        if self._waiting_stream is None:
            stream = WaitingStream(sync)
            self._waiting_stream = stream
            self._waiting_stream_for = credentials
            self._waiting_stream_task = asyncio.create_task(
                stream.run(on_change=self._something_arrived)
            )
        return ArtifactWaiting(ok=True, waiting=await self._waiting_count())

    async def _something_arrived(self, projected: int) -> None:
        """Something happened elsewhere. Tell whoever is drawing a count.

        Not only comments, despite ``projected`` being a count of
        them: an access request, a document being shared, a version
        published all arrive here with ``projected`` at zero, and the
        badge and the notification panel both need to hear about them.

        The channel is ``artifact_waiting`` rather than
        ``artifact_comments`` — the old name is also the name of an
        RPC method, so one string was doing two unrelated jobs, and
        the one it described is no longer what this carries.
        """
        self._session.broadcast_bus.emit(
            BroadcastEvent(
                channel="artifact_waiting",
                payload={"projected": projected, "waiting": await self._waiting_count()},
            )
        )

    async def _waiting_count(self) -> int:
        """How many things are waiting, from the server.

        Read rather than projected, unlike comments. A comment is
        part of a document's history and belongs in the store beside
        it; a notification is a message to one person about a moment,
        and a local mirror of it would go on saying somebody is
        waiting after they have been dealt with somewhere else.
        """
        sync = self._collaboration()
        if sync is None:
            return 0
        try:
            return int((await sync.waiting()).get("waiting") or 0)
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("could not count what is waiting: %s", exc)
            return 0

    async def waiting(self, *, unread_only: bool = True) -> ArtifactNotifications:
        """Everything waiting on this account, newest first."""
        sync = self._collaboration()
        if sync is None:
            return ArtifactNotifications()
        try:
            answer = await sync.waiting(unread_only=unread_only)
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("could not read what is waiting: %s", exc)
            return ArtifactNotifications(error=unreachable(exc))
        return ArtifactNotifications(
            ok=True,
            waiting=int(answer.get("waiting") or 0),
            notifications=[
                ArtifactNotification(
                    **{k: row.get(k) for k in ArtifactNotification.model_fields if k in row}
                )
                for row in (answer.get("notifications") or [])
            ],
        )

    async def mark_seen(self, *, notification_id: str = "") -> ArtifactNotifications:
        """Say one has been read, or all of them."""
        sync = self._collaboration()
        if sync is None:
            return ArtifactNotifications()
        try:
            answer = await sync.mark_seen(notification_id=notification_id)
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("could not mark a notification seen: %s", exc)
            return ArtifactNotifications(error=unreachable(exc))
        return ArtifactNotifications(ok=True, waiting=int(answer.get("waiting") or 0))

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
            published = await publisher.publish(
                bundle, visibility=visibility, audience=audience or []
            )
        except Exception as exc:
            logger.warning("Artifact share failed: %s", exc)
            return ArtifactShareResult(error=f"Share failed: {exc}")
        # This document has a shared copy now, and the sync client
        # remembers that it did not. Publishing goes through a
        # different client, so nothing else would tell it — and a
        # document stuck on the stale answer edits its draft on this
        # machine for ever, invisible to the people it was just
        # shared with.
        sync = self._collaboration()
        if sync is not None:
            sync.forget_slugs()
        return ArtifactShareResult(url=published.url, version=bundle.n)

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
