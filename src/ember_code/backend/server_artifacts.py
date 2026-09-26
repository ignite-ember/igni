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

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ember_code.backend.schemas_artifacts import (
    ArtifactExportResult,
    ArtifactGetResult,
    ArtifactGraphEdge,
    ArtifactGraphNode,
    ArtifactGraphView,
    ArtifactHit,
    ArtifactLineageRow,
    ArtifactLineageView,
    ArtifactListEntry,
    ArtifactQueryResult,
    ArtifactRemoveResult,
    ArtifactShareResult,
    ArtifactStaleRow,
    ArtifactStatus,
    ArtifactVersionRow,
)
from ember_code.core.artifacts.cypher import ArtifactCypherService, CypherResult
from ember_code.core.artifacts.export import ArtifactExporter, ArtifactPublisher
from ember_code.core.artifacts.models import ArtifactSummary, EdgeState
from ember_code.core.artifacts.query_planner import ArtifactQueryPlanner
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


class ArtifactController:
    """Artifact RPCs for one :class:`Session`."""

    def __init__(self, session: Session) -> None:
        self._session = session

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
                n=r.n,
                content=r.content,
                score=r.score,
                metadata={k: str(v) for k, v in r.metadata.items()},
            )
            for r in response.results
        ]

    async def add(self, source: str) -> msg.Info:
        """Add content from the panel.

        A URL or path is ingestion — the source is self-evident and no
        declaration is asked for. Pasted text is a new artifact with
        no antecedent, which the panel states rather than inventing
        one.
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
        return msg.Info(text=result.message)

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
    ) -> ArtifactExportResult:
        """Write a self-contained HTML page to disk."""
        artifacts = self._session.artifacts
        if artifacts is None:
            return ArtifactExportResult(error="Artifacts are disabled.")
        exporter = ArtifactExporter(artifacts)
        target = Path(destination).expanduser() if destination else self._session.project_dir
        try:
            written = await exporter.write(entry_id, target, version=version)
        except OSError as exc:
            return ArtifactExportResult(error=f"Could not write export: {exc}")
        if written is None:
            return ArtifactExportResult(error=f"Artifact {entry_id} not found.")
        return ArtifactExportResult(path=str(written))

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

        ``visibility`` is ``private``, ``users``, ``domain``, ``org``
        or ``public``. ``domain`` means everyone whose account email
        shares the author's — the deployment serves one customer whose
        people may sit in several orgs, so "my colleagues" is the
        domain rather than the org, and the server derives it from the
        signed-in account rather than taking it from here.
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
