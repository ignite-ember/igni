"""Session artifact operations — wraps :class:`ArtifactIndex` for the session.

Returns the higher-level result models the tools and CLI consume, and
exposes the URL/path ingestion path.

It used to mirror every write into ``.igni/artifacts.yaml`` as well.
A document's home is the cloud: the version chain, the declaration
each collaborator rebuilds their edges from, and the filing all live
there, and a second copy in the repository was a second thing to keep
in step with no reader that needed it.

The one policy that lives here rather than in the index:
**a write must declare what it was built on.** The index will happily
store an artifact with no sources; the session refuses to let an agent
do it silently, because an uncited derivation is invisible forever and
the moment to ask is while the agent still knows the answer.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ember_code.backend.schemas_artifacts import ArtifactCommentResolved
from ember_code.core.artifacts.collaboration import CollaborationSync, NotAllowed
from ember_code.core.artifacts.draft_desk import DraftDesk, DraftRefused
from ember_code.core.artifacts.errors import SourcesRequired
from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.ingest import Ingester
from ember_code.core.artifacts.models import (
    ArtifactLineage,
    ArtifactSearchHit,
    ArtifactSearchResponse,
    ArtifactStatus,
    ArtifactSummary,
    ArtifactVersion,
    ArtifactWriteResult,
    Comment,
    CommentOrigin,
    CommentStatus,
    ReconcilePlan,
    SourceDeclaration,
    StaleDependent,
)
from ember_code.core.config.settings import Settings

# ``SourcesRequired`` is re-exported because callers reach for it from
# the manager that raises it, not from the package that defines it.
__all__ = ["SessionArtifactManager", "SourcesRequired"]

logger = logging.getLogger(__name__)


class SessionArtifactManager:
    """Per-session artifact manager backed by :class:`ArtifactIndex`."""

    def __init__(
        self,
        artifacts: ArtifactIndex | None,
        settings: Settings,
        project_dir: Path,
        *,
        session_id: str = "",
        author: str = "",
    ):
        self.artifacts = artifacts
        self.settings = settings
        self.project_dir = project_dir
        self.session_id = session_id
        self.author = author
        # Advanced by the sync, so a second pull in one session asks
        # only for what changed. Not persisted: starting from the
        # beginning is idempotent, and a stale cursor on disk would be
        # the one way to *miss* something.

    # ── Config surface ────────────────────────────────────────────────

    @property
    def disabled_message(self) -> str:
        return "Artifacts are not enabled. Set artifacts.enabled=true in config."

    # ── Writes ────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        name: str,
        content: str,
        sources: list[str] | None,
        kind: str = "note",
        no_sources_reason: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ArtifactWriteResult:
        """Create v1 of an artifact, declaring its sources."""
        if self.artifacts is None:
            return ArtifactWriteResult.fail(self.disabled_message)
        declaration = self._declare(sources, no_sources_reason=no_sources_reason)
        result = await self.artifacts.create(
            name=name,
            content=content,
            sources=declaration,
            kind=kind,
            author=self.author,
            session_id=self.session_id,
            metadata=metadata,
        )
        return result

    async def revise(
        self,
        *,
        ref: str,
        content: str,
        sources: list[str] | None,
        no_sources_reason: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ArtifactWriteResult:
        """Append a version, declaring what this revision was built on."""
        if self.artifacts is None:
            return ArtifactWriteResult.fail(self.disabled_message)
        declaration = self._declare(sources, no_sources_reason=no_sources_reason)
        result = await self.artifacts.revise(
            ref=ref,
            content=content,
            sources=declaration,
            author=self.author,
            session_id=self.session_id,
            metadata=metadata,
        )
        return result

    # ── Editing through the draft ────────────────────────────────────
    #
    # The same two acts the editor uses, so a change asked for in a
    # comment lands where a change typed into the document lands.
    # Otherwise the agent answering "reword this paragraph" publishes
    # a version while the person who asked is still deciding, and the
    # draft they had open is suddenly behind a version they did not
    # make.

    def draft_desk(self) -> DraftDesk | None:
        """Where an edit goes before it is a version."""
        if self.artifacts is None:
            return None
        return DraftDesk(self.artifacts, author=self.author)

    async def write_draft(self, *, ref: str, content: str) -> ArtifactWriteResult:
        """Put a change in the draft without making a version."""
        desk = self.draft_desk()
        if desk is None:
            return ArtifactWriteResult.fail(self.disabled_message)
        found = await self.artifacts.resolve(ref)
        if found is None:
            return ArtifactWriteResult.fail(f"No artifact matches {ref!r}.")
        try:
            await desk.save(found.artifact_id, content)
        except (DraftRefused, NotAllowed) as refused:
            return ArtifactWriteResult.fail(str(refused) or "Could not write that draft.")
        return ArtifactWriteResult.ok(
            "Written to the draft, not to a version. Whoever asked for this "
            "publishes it when they are happy with it.",
            artifact_id=found.artifact_id,
        )

    async def publish_draft(self, *, ref: str) -> ArtifactWriteResult:
        """Turn the draft into a version."""
        desk = self.draft_desk()
        if desk is None:
            return ArtifactWriteResult.fail(self.disabled_message)
        found = await self.artifacts.resolve(ref)
        if found is None:
            return ArtifactWriteResult.fail(f"No artifact matches {ref!r}.")
        try:
            n = await desk.publish(found.artifact_id)
        except (DraftRefused, NotAllowed) as refused:
            return ArtifactWriteResult.fail(str(refused) or "Could not publish that draft.")
        return ArtifactWriteResult.ok(f"Published as v{n}.", artifact_id=found.artifact_id, n=n)

    def _declare(
        self,
        sources: list[str] | None,
        *,
        no_sources_reason: str,
    ) -> SourceDeclaration:
        """Turn the raw ``sources`` argument into a typed declaration.

        An empty list is allowed **only** with a stated reason. Some
        artifacts genuinely have no antecedent — a first observation,
        a meeting note — and forcing a fake citation would poison the
        graph worse than an honest empty one. What is not allowed is
        omitting the question.
        """
        declared = [s for s in (sources or []) if (s or "").strip()]
        if not declared and not no_sources_reason.strip():
            raise SourcesRequired(
                "This write needs a source declaration.",
                candidates=self.pending_sources(),
            )
        return SourceDeclaration.from_raw(declared)

    def pending_sources(self) -> list[str]:
        """What this session has read, as ``artifact_id@n`` refs."""
        if self.artifacts is None:
            return []
        return [pin.ref for pin in self.artifacts.ledger.candidates()]

    # ── Ingestion ─────────────────────────────────────────────────────

    async def add_url(
        self,
        url: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactWriteResult:
        """Fetch a URL, chunk via the right Agno reader, store it.

        Ingestion is the one write that never asks for sources: the
        URL *is* the source, and it is recorded as a citation.
        """
        if self.artifacts is None:
            return ArtifactWriteResult.fail(self.disabled_message)
        result = await Ingester(self.artifacts).add_url(url, metadata=metadata)
        return ArtifactWriteResult.from_ingest(result, source_label=url)

    async def add_path(
        self,
        path: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactWriteResult:
        """Read a file or directory and store each document."""
        if self.artifacts is None:
            return ArtifactWriteResult.fail(self.disabled_message)
        result = await Ingester(self.artifacts).add_path(path, metadata=metadata)
        return ArtifactWriteResult.from_ingest(result, source_label=path)

    # ── Reads ─────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        limit: int = 5,
        *,
        record: bool = True,
    ) -> ArtifactSearchResponse:
        if self.artifacts is None:
            return ArtifactSearchResponse(query=query)
        try:
            hits = await self.artifacts.search(query=query, limit=limit, record=record)
        except Exception as exc:
            logger.debug("Artifact search failed: %s", exc)
            return ArtifactSearchResponse(query=query)
        results = [self._coerce_hit(h) for h in hits]
        return ArtifactSearchResponse(query=query, results=results, total=len(results))

    @staticmethod
    def _coerce_hit(hit: object) -> ArtifactSearchHit:
        """Normalize hits into :class:`ArtifactSearchHit`.

        The index returns typed hits already, but tests stub the
        facade with dict payloads — accept both.
        """
        if isinstance(hit, ArtifactSearchHit):
            return hit
        if isinstance(hit, dict):
            return ArtifactSearchHit(
                artifact_id=hit.get("artifact_id", ""),
                name=hit.get("name", ""),
                kind=hit.get("kind", ""),
                n=int(hit.get("n", 0) or 0),
                content=hit.get("content", ""),
                score=hit.get("score"),
                project=hit.get("project", ""),
                metadata={k: str(v) for k, v in (hit.get("metadata") or {}).items()},
            )
        return ArtifactSearchHit(artifact_id="")

    async def list_artifacts(
        self, *, limit: int = 1000, include_archived: bool = False
    ) -> list[ArtifactSummary]:
        if self.artifacts is None:
            return []
        return await self.artifacts.list_artifacts(limit=limit, include_archived=include_archived)

    async def repair_chunks(self, *, budget: int = 12) -> int:
        """Re-split versions whose chunks predate correct line spans.

        Returns how many versions are *still* waiting, so the caller
        can say the answer is incomplete rather than quietly showing a
        partial one. Bounded per call: a large store gets better over
        a few openings rather than blocking the first one.

        Recomputes rather than patches. Line spans, ordinals and
        vectors all come out of one split, and repairing them
        separately would leave a version whose ordinals belong to one
        split and whose spans belong to another.
        """
        if self.artifacts is None:
            return 0
        client = self.artifacts.client
        pending = await client.versions_needing_rechunk(limit=budget)
        if not pending:
            return 0
        embedder = getattr(self.artifacts, "_embedder", None)
        for row in pending:
            content = str(row.get("content") or "")
            if not content.strip():
                continue
            rows = self.artifacts._chunk(content)
            vectors: list[list[float]] = []
            if embedder is not None and rows:
                vectors = list(embedder.embed([text for text, _lf, _lt in rows]))
            await client.replace_chunks(
                version_id=str(row["version_id"]), rows=rows, vectors=vectors
            )
        return len(await client.versions_needing_rechunk(limit=1))

    async def backfill_chunk_embeddings(self, *, budget: int = 500) -> int:
        """Give vectors to chunks written before they were stored.

        Returns how many are *still* unplaced after this pass, so the
        caller can say the answer is incomplete rather than quietly
        showing a partial one. Bounded per call: a large store should
        make provenance progressively better over a few openings
        rather than blocking the first one for a minute.
        """
        if self.artifacts is None:
            return 0
        client = self.artifacts.client
        pending = await client.chunks_without_embeddings(limit=budget)
        if not pending:
            return 0
        embedder = getattr(self.artifacts, "_embedder", None)
        if embedder is None:
            return len(pending)
        vectors = embedder.embed([row["text"] for row in pending])
        await client.set_chunk_embeddings(
            [
                {"chunk_id": row["chunk_id"], "embedding": list(vec)}
                for row, vec in zip(pending, vectors, strict=False)
            ]
        )
        # Anything beyond this pass's budget is still unplaced.
        remaining = await client.chunks_without_embeddings(limit=1)
        return len(remaining)

    async def resolve_artifact(self, ref: str) -> ArtifactSummary | None:
        """A name, id or ``id@n`` ref, as the document it names."""
        if self.artifacts is None:
            return None
        return await self.artifacts.resolve(ref)

    async def versions(self, ref: str) -> list[ArtifactVersion]:
        if self.artifacts is None:
            return []
        return await self.artifacts.versions(ref)

    async def version(
        self, ref: str, n: int = 0, *, record: bool = False
    ) -> ArtifactVersion | None:
        if self.artifacts is None:
            return None
        return await self.artifacts.version(ref, n, record=record)

    async def lineage(self, ref: str, n: int = 0) -> ArtifactLineage | None:
        if self.artifacts is None:
            return None
        return await self.artifacts.lineage(ref, n)

    async def ask_graph(self, question: str) -> str:
        """Answer a question about the graph, as text for the agent.

        The agent already has ``artifact_search`` for "find me a
        document about X". This is for the questions search cannot
        answer because they are about *structure*: what was built on
        what, what would break, what nobody has reviewed.
        """
        if self.artifacts is None:
            return self.disabled_message
        planner = self._planner()
        if planner is None:
            return "The artifact graph is not reachable in this session."
        plan = await planner.ask(question)
        if not plan.ok:
            return f"Could not answer that: {plan.error}"
        result = plan.result
        assert result is not None  # plan.ok implies a result
        if not result.rows:
            return f"No rows.\n\nQuery used:\n{result.cypher}"
        lines = [f"{len(result.rows)} row(s):"]
        for row in result.rows[:40]:
            lines.append("  " + ", ".join(f"{k}={v}" for k, v in row.items()))
        if len(result.rows) > 40:
            lines.append(f"  … and {len(result.rows) - 40} more")
        lines.append(f"\nQuery used:\n{result.cypher}")
        return "\n".join(lines)

    async def run_cypher(self, cypher: str, *, semantic_query: str = ""):
        """Run a hand-written query, with no planning and no retry.

        The author can read the error and fix it, and rewriting their
        query underneath them would be worse than showing them what
        the database said.
        """
        from ember_code.core.artifacts.cypher import CypherResult

        planner = self._planner()
        if planner is None:
            return CypherResult(cypher=cypher, error=self.disabled_message)
        return await planner.run_raw(cypher, semantic_query=semantic_query)

    def _planner(self):
        """Build a planner over this session's store, or None.

        Lives here rather than on the index because it needs the
        session's *model*, which the index has no business knowing
        about — the index stores documents; choosing who writes a
        query is a session concern.
        """
        from ember_code.core.artifacts.cypher import ArtifactCypherService
        from ember_code.core.artifacts.query_planner import ArtifactQueryPlanner
        from ember_code.core.config.models import ModelRegistry

        client = getattr(self.artifacts, "client", None)
        driver = getattr(client, "_driver", None)
        if driver is None:
            return None
        service = ArtifactCypherService(
            driver,
            client.project_id,
            embedder=getattr(self.artifacts, "_embedder", None),
        )
        return ArtifactQueryPlanner(
            service,
            model_factory=lambda: ModelRegistry(self.settings).get_model(),
        )

    # ── Comments ──────────────────────────────────────────────────────

    async def comments(
        self, *, artifact_id: str | None = None, open_only: bool = True
    ) -> list[Comment]:
        """Read projected comments from the local graph.

        Local, not over the API: the queue has to answer with the
        network down, which is the whole reason comments are projected
        rather than fetched.
        """
        if self.artifacts is None:
            return []
        return await self.artifacts.client.comments(artifact_id=artifact_id, open_only=open_only)

    async def add_local_comment(
        self,
        *,
        artifact_id: str,
        n: int,
        body: str,
        quote: str = "",
        quote_start: int | None = None,
        quote_end: int | None = None,
        author: str = "",
        thread_id: str = "",
        parent_id: str | None = None,
        origin: CommentOrigin = CommentOrigin.LOCAL,
        at: str = "",
    ) -> Comment | None:
        """Record a comment written here, against the local copy.

        Local only: it is projected into the graph and never pushed.
        A comment written in igni is addressed to the agent, and
        sending it to the people on the shared copy would publish one
        side of a conversation they are not part of. When the agent
        answers, what *does* reach them is the revision — a version
        with an author and a reason, which is the thing they can read.
        """
        if self.artifacts is None:
            return None
        comment = Comment(
            comment_id=f"local-{uuid.uuid4().hex[:16]}",
            artifact_id=artifact_id,
            n=n,
            # A reply joins the thread it answers; a fresh comment
            # starts one named after itself.
            thread_id=thread_id or f"local-thread-{uuid.uuid4().hex[:12]}",
            parent_id=parent_id,
            quote=quote,
            # Where the passage sat in the text at the time. A hint
            # for picking between repeated lines, never the anchor:
            # everything below an edit shifts, so an offset is wrong
            # the moment anything above it changes.
            quote_start=quote_start,
            quote_end=quote_end,
            body=body,
            status=CommentStatus.OPEN,
            author=author,
            at=at or datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            origin=origin,
        )
        await self.artifacts.client.project_comments([comment])
        return comment

    async def thread_for(self, *, artifact_id: str, thread_id: str) -> list[Comment]:
        """Every comment on one thread, whatever its origin or status.

        Not ``open_only``: a settled comment is exactly the kind of
        thing a new instruction can contradict, and the agent should
        be able to notice it is being asked to reverse a decision
        rather than discover it by making the change twice.
        """
        if self.artifacts is None or not thread_id:
            return []
        rows = await self.artifacts.client.comments(artifact_id=artifact_id, open_only=False)
        return [c for c in rows if c.thread_id == thread_id]

    async def resolve_comment(
        self, *, comment_id: str, status: str, content: str = "", sync: CollaborationSync
    ) -> ArtifactCommentResolved:
        """Adjudicate one comment, appending a version when accepting.

        **Write first, then record.** Accepting with content revises
        the document and only then tells the server which version
        applied it. The other order leaves a closed thread pointing at
        a version that does not exist whenever the write fails — and
        the commenter, who sees only the server, would be told their
        suggestion was applied to nothing.
        """
        if self.artifacts is None:
            return ArtifactCommentResolved(ok=False, message=self.disabled_message)
        try:
            state = CommentStatus(status)
        except ValueError:
            return ArtifactCommentResolved(ok=False, message=f"Unknown status: {status!r}")

        applied_in = 0
        if content:
            existing = await self.artifacts.client.comments(limit=10_000)
            target = next((c for c in existing if c.comment_id == comment_id), None)
            if target is None:
                return ArtifactCommentResolved(ok=False, message="No such comment here.")
            written = await self.revise(
                ref=target.artifact_id,
                content=content,
                # No new sources are declared, and that is not the
                # same as having none: a revision inherits the
                # previous version's confirmed edges, so the document
                # keeps standing on exactly what it stood on before.
                # What changed is the wording somebody asked about,
                # and the reason records who asked.
                sources=None,
                no_sources_reason=(
                    f"Applied comment {comment_id} from "
                    f"{target.author or 'a collaborator'}; sources unchanged."
                ),
            )
            # ``success``, not ``ok``. ``ok`` is the classmethod that
            # builds one of these, so it is a bound method here and
            # always truthy — the refusal branch below could not be
            # reached, and a failed write was reported to the
            # commenter as their suggestion having been applied.
            if not written.success:
                return ArtifactCommentResolved(ok=False, message=written.error or written.message)
            applied_in = written.n

        try:
            await sync.resolve(
                comment_id=comment_id,
                status=state,
                resolved_in_n=applied_in or None,
            )
        except NotAllowed:
            # A 404, not a network failure. The shared copy has never
            # heard of this comment — which is the ordinary state of
            # one written here before comments went to the server —
            # and reporting it as unreachable sent people to check
            # their connection over a comment that was never there.
            #
            # ``NotAllowed`` carries the comment id and nothing else,
            # so its ``str()`` is a bare id: the old message read
            # "Could not reach the server: local-cdcb975178854562".
            logger.info("comment %s is not on the shared copy", comment_id)
            return ArtifactCommentResolved(
                ok=False,
                status=state.value,
                resolved_in_n=applied_in,
                message=(
                    f"Version {applied_in} was written, but this comment is not on the "
                    "shared copy, so the ruling was not recorded."
                    if applied_in
                    else "This comment only exists on this machine — the shared copy has "
                    "no record of it, so there is nothing to rule on yet."
                ),
            )
        except Exception as exc:
            # The version is written and the server does not know. Say
            # so plainly: the next sync will not repair it, and a
            # silent "ok" here would hide a real split.
            logger.warning("comment %s resolved locally but not on the server", comment_id)
            return ArtifactCommentResolved(
                ok=False,
                status=state.value,
                resolved_in_n=applied_in,
                message=(
                    f"Version {applied_in} was written, but the server was not told: {exc}"
                    if applied_in
                    else f"Could not reach the server: {exc}"
                ),
            )
        return ArtifactCommentResolved(ok=True, status=state.value, resolved_in_n=applied_in)

    async def stale(self, *, limit: int = 100) -> list[StaleDependent]:
        if self.artifacts is None:
            return []
        return await self.artifacts.stale(limit=limit)

    async def reconcile_plan(self, stale: StaleDependent) -> ReconcilePlan | None:
        if self.artifacts is None:
            return None
        return await self.artifacts.reconcile_plan(stale)

    async def accept_reconcile(
        self,
        *,
        plan: ReconcilePlan,
        content: str | None = None,
    ) -> ArtifactWriteResult:
        if self.artifacts is None:
            return ArtifactWriteResult.fail(self.disabled_message)
        result = await self.artifacts.accept_reconcile(
            plan=plan,
            content=content,
            author=self.author,
            session_id=self.session_id,
        )
        return result

    async def confirm_source(self, *, ref: str, source_ref: str) -> bool:
        if self.artifacts is None:
            return False
        return await self.artifacts.confirm_source(ref=ref, source_ref=source_ref)

    async def dismiss_source(self, *, ref: str, source_ref: str) -> bool:
        if self.artifacts is None:
            return False
        return await self.artifacts.dismiss_source(ref=ref, source_ref=source_ref)

    async def delete(self, ref: str) -> bool:
        if self.artifacts is None:
            return False
        deleted = await self.artifacts.delete(ref)
        return deleted

    async def status(self) -> ArtifactStatus:
        cfg = self.settings.artifacts
        if self.artifacts is None:
            return ArtifactStatus(enabled=False)
        try:
            count = await self.artifacts.count()
            versions = await self.artifacts.version_count()
            stale = len(await self.artifacts.stale(limit=500))
            candidates = await self.artifacts.candidate_count()
        except Exception as exc:
            logger.warning("Artifact status failed: %s", exc)
            count = versions = stale = candidates = 0
        return ArtifactStatus(
            enabled=True,
            collection_name=cfg.collection_name,
            document_count=count,
            version_count=versions,
            stale_count=stale,
            pending_candidates=candidates,
            embedder="sentence-transformers:all-MiniLM-L6-v2",
        )
