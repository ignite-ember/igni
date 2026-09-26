"""Session artifact operations — wraps :class:`ArtifactIndex` for the session.

Folds in YAML mirroring on write, returns the higher-level result
models the tools and CLI consume, and exposes the URL/path ingestion
path.

The one policy that lives here rather than in the index:
**a write must declare what it was built on.** The index will happily
store an artifact with no sources; the session refuses to let an agent
do it silently, because an uncited derivation is invisible forever and
the moment to ask is while the agent still knows the answer.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.core.artifacts.errors import SourcesRequired
from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.ingest import Ingester
from ember_code.backend.schemas_artifacts import ArtifactCommentResolved
from ember_code.core.artifacts.collaboration import CollaborationSync
from ember_code.core.artifacts.models import (
    ArtifactLineage,
    ArtifactSearchHit,
    ArtifactSearchResponse,
    ArtifactStatus,
    ArtifactSummary,
    ArtifactSyncResult,
    ArtifactVersion,
    ArtifactWriteResult,
    Comment,
    CommentStatus,
    ReconcilePlan,
    SourceDeclaration,
    StaleDependent,
)
from ember_code.core.artifacts.sync import ArtifactSyncer
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
        self._comment_cursor: str | None = None

    # ── Config surface ────────────────────────────────────────────────

    def share_enabled(self) -> bool:
        return (
            self.settings.artifacts.enabled
            and self.settings.artifacts.share
            and self.artifacts is not None
        )

    def file_path(self) -> Path:
        return self.project_dir / self.settings.artifacts.share_file

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
        if result.success and self.share_enabled():
            await self._mirror_to_yaml()
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
        if result.success and self.share_enabled():
            await self._mirror_to_yaml()
        return result

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

    async def list_artifacts(self, *, limit: int = 1000) -> list[ArtifactSummary]:
        if self.artifacts is None:
            return []
        return await self.artifacts.list_artifacts(limit=limit)

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

    def _sync(self) -> CollaborationSync | None:
        """The sync client, or ``None`` when this machine is signed out.

        Built per call rather than held: the account token can change
        under a long session, and a cached client would keep using the
        one it was constructed with.
        """
        if self.artifacts is None:
            return None
        server = str(getattr(self.settings, "server_url", "") or "")
        token = str(getattr(self.settings, "access_token", "") or "")
        if not (server and token):
            return None
        return CollaborationSync(self.artifacts, server_url=server, access_token=token)

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

    async def sync_comments(self) -> int:
        sync = self._sync()
        if sync is None:
            return 0
        projected, _ = await sync.pull_comments(cursor=self._comment_cursor)
        return projected

    async def resolve_comment(
        self, *, comment_id: str, status: str, content: str = ""
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
        sync = self._sync()
        if sync is None:
            return ArtifactCommentResolved(
                ok=False,
                message="Resolving a comment needs a signed-in igni account — run `igni login`.",
            )
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
                sources=SourceDeclaration(),
            )
            if not written.ok:
                return ArtifactCommentResolved(ok=False, message=written.message)
            applied_in = written.n

        try:
            await sync.resolve(
                comment_id=comment_id,
                status=state,
                resolved_in_n=applied_in or None,
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
        if result.success and content and self.share_enabled():
            await self._mirror_to_yaml()
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
        if deleted and self.share_enabled():
            await self._mirror_to_yaml()
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

    # ── YAML sync ─────────────────────────────────────────────────────

    async def sync_from_file(self) -> ArtifactSyncResult:
        if not self.share_enabled():
            return ArtifactSyncResult(
                direction="file_to_db", message="Artifact sharing is not enabled."
            )
        try:
            return await ArtifactSyncer(
                file_path=self.file_path(), artifacts=self.artifacts
            ).sync_file_to_db()
        except Exception as exc:
            return ArtifactSyncResult(direction="file_to_db", error=str(exc))

    async def sync_to_file(self) -> ArtifactSyncResult:
        if not self.share_enabled():
            return ArtifactSyncResult(
                direction="db_to_file", message="Artifact sharing is not enabled."
            )
        try:
            return await ArtifactSyncer(
                file_path=self.file_path(), artifacts=self.artifacts
            ).sync_db_to_file()
        except Exception as exc:
            return ArtifactSyncResult(direction="db_to_file", error=str(exc))

    async def sync_bidirectional(self) -> list[ArtifactSyncResult]:
        return [await self.sync_from_file(), await self.sync_to_file()]

    async def _mirror_to_yaml(self) -> None:
        """Rewrite the shared file from the store's heads.

        A full rewrite rather than an append: the file carries head
        content only, so after a revision the row that is already
        there is the *stale* one, and appending would leave both.
        """
        try:
            await self.sync_to_file()
        except Exception as exc:
            logger.warning("Artifact YAML mirror failed: %s", exc)
