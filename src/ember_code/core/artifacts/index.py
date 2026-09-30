"""Per-project artifact index, backed by Neo4j.

:class:`ArtifactIndex` is a thin coordinator — each distinct
responsibility is delegated:

  - :class:`ArtifactMetadataCodec` — id, hash and timestamp policy.
  - :class:`NewlinePreservingChunker` — default chunker; callers can
    inject any :class:`ChunkingStrategy`.
  - :class:`RetrievalLedger` — what was read this session, so writes
    can cite it without asking the model.
  - :class:`Reconciler` — the pinned→head diff behind a proposed fix.
  - :class:`Neo4jArtifactClient` — all Cypher.

The index's own job is the part that needs a decision rather than a
delegation: resolving a human-typed reference to an artifact id,
deciding which reads become candidate edges, and refusing a write that
would silently lose a declared source.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agno.knowledge.chunking.strategy import ChunkingStrategy
from agno.knowledge.document.base import Document

from ember_code.core.artifacts.capture import RetrievalLedger
from ember_code.core.artifacts.chunking import NewlinePreservingChunker
from ember_code.core.artifacts.collaboration import ChainDiverged
from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import (
    ArtifactDeleteResult,
    ArtifactLineage,
    ArtifactSearchHit,
    ArtifactSummary,
    ArtifactVersion,
    ArtifactWriteResult,
    EdgeState,
    ReconcilePlan,
    SourceDeclaration,
    StaleDependent,
    VersionPin,
)
from ember_code.core.artifacts.neo4j_client import Neo4jArtifactClient
from ember_code.core.artifacts.reconcile import Reconciler
from ember_code.core.artifacts.render import ArtifactRenderer, RenderRequest
from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.paths import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)


class ArtifactIndex:
    """Per-project artifact index backed by Neo4j.

    Args:
        project: project directory (used to derive the project id).
        data_dir: igni root, defaults to ``~/.igni``.
        chunker: how to split content. Default
            ``NewlinePreservingChunker(chunk_size=550, overlap=75)`` —
            sized so chunks stay under the 256-token window of the
            ``all-MiniLM-L6-v2`` embedder. Markdown/code is
            token-dense (~0.36 tokens/char), so 550 chars ≈ 200 tokens
            with headroom.
        neo4j_client: required; a :class:`Neo4jArtifactClient` bound to
            the per-project driver.
        embedder: 384-dim vector producer; inject ``LiveEmbedder()``
            for production or ``HashEmbedder()`` for tests.
        ledger: retrieval ledger; one per session.
    """

    def __init__(
        self,
        *,
        project: str | Path,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        chunker: ChunkingStrategy | None = None,
        neo4j_client: Neo4jArtifactClient,
        embedder: Any | None = None,
        ledger: RetrievalLedger | None = None,
        codec: ArtifactMetadataCodec | None = None,
        reconciler: Reconciler | None = None,
    ):
        if neo4j_client is None:
            raise RuntimeError(
                "ArtifactIndex requires neo4j_client= — construct a "
                "Neo4jArtifactClient from Neo4jRuntime."
                "driver_for_artifacts(project_id) and pass it in."
            )
        self.project = project
        self.project_id = resolve_project_id(project)
        self.data_dir = data_dir
        self.chunker = chunker or NewlinePreservingChunker(chunk_size=550, overlap=75)
        self._client = neo4j_client
        self._embedder = embedder
        self._codec = codec or ArtifactMetadataCodec()
        self._ledger = ledger or RetrievalLedger()
        self._reconciler = reconciler or Reconciler()
        #: Where a version goes first. Attached after construction
        #: because the thing that satisfies it is built *from* this
        #: index — see :meth:`attach_shared`.
        self._shared: Any | None = None

    def attach_shared(self, shared: Any) -> None:
        """Name the server this index writes through.

        A late setter rather than a constructor argument, and not for
        convenience: the collaboration client is constructed with this
        index, so one of the two has to exist first. Nothing else
        breaks the cycle without a third object whose only job is to
        hold a reference.

        Unattached — no account, artifacts running standalone — the
        index writes to the graph alone, which is the old behaviour
        and the only thing that works with no server to reach.
        """
        self._shared = shared

    @property
    def shared(self) -> Any | None:
        """The server this index writes through, if there is one."""
        return self._shared

    @property
    def client(self) -> Neo4jArtifactClient:
        return self._client

    @property
    def ledger(self) -> RetrievalLedger:
        return self._ledger

    async def start(self) -> None:
        """No-op — the client is owned by the runtime and already open."""
        return

    async def close(self) -> None:
        """No-op — the driver outlives any one index instance."""

    # ── Writes ────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        name: str,
        content: str,
        sources: SourceDeclaration,
        kind: str = "note",
        author: str = "",
        session_id: str = "",
        metadata: dict[str, str] | None = None,
        projecting: bool = False,
    ) -> ArtifactWriteResult:
        """Create a new artifact at v1.

        Refuses when the name already exists rather than silently
        appending: "create" and "revise" mean different things to the
        caller, and quietly turning one into the other is how an agent
        overwrites a document it did not know was there.
        """
        if not name.strip():
            return ArtifactWriteResult.fail("An artifact needs a name.")
        artifact_id = self._codec.artifact_id(name)
        existing = await self._client.find_by_name(name)
        if existing is not None:
            return ArtifactWriteResult.fail(
                f"'{name}' already exists at v{existing.head_n} "
                f"(id {existing.artifact_id}). Use artifact_revise to add a "
                f"version, or pick a different name."
            )
        return await self._write(
            artifact_id=artifact_id,
            name=name,
            content=content,
            sources=sources,
            kind=kind,
            author=author,
            session_id=session_id,
            metadata=metadata,
            projecting=projecting,
        )

    async def revise(
        self,
        *,
        ref: str,
        content: str,
        sources: SourceDeclaration,
        author: str = "",
        session_id: str = "",
        metadata: dict[str, str] | None = None,
        projecting: bool = False,
    ) -> ArtifactWriteResult:
        """Append a new version to an existing artifact.

        Refused when the artifact is archived. Archiving is what is
        offered instead of deleting something the lineage depends on,
        and it is only worth offering if the archived thing stops
        moving: a document whose dependents were told "this is what it
        was built on" must not quietly gain a v7 afterwards.

        Refused *here* rather than in each caller. Accepting a comment,
        applying a drift fix and the agent's own revise tool all end up
        in this method, and a gate per entrance is a gate somebody adds
        an entrance around.
        """
        summary = await self.resolve(ref)
        if summary is None:
            return ArtifactWriteResult.fail(
                f"No artifact matches '{ref}'. Use artifact_list to see what exists, "
                f"or artifact_create to start a new one."
            )
        if summary.archived:
            return ArtifactWriteResult.fail(
                f"'{summary.name or ref}' is archived, and archived documents are "
                f"frozen. Restore it first if it needs to change."
            )
        return await self._write(
            artifact_id=summary.artifact_id,
            name=summary.name or ref,
            content=content,
            sources=await self._inherit_confirmed(summary.artifact_id, sources),
            kind=None,
            author=author,
            session_id=session_id,
            metadata=metadata,
            projecting=projecting,
        )

    async def _inherit_confirmed(
        self, artifact_id: str, sources: SourceDeclaration
    ) -> SourceDeclaration:
        """Carry the previous version's confirmed sources into this one.

        Edges belong to a *version*, so without this a revision starts
        with none, and a declaration that simply forgets to re-state a
        source drops it. The effect is the one thing this subsystem
        exists to prevent: revising a stale document makes the
        staleness go away, not because anything was reconciled but
        because the dependency was erased. Measured on the eval
        fixture — Login flow v1 had two confirmed sources and a
        candidate, v2 had none, and the stale row vanished with them.

        Inherited pins keep the version they were pinned at, so this
        resolves nothing on its own: an inherited stale edge stays
        stale and still needs reconcile or repin. A source the
        declaration *does* name wins, which is how a revision moves a
        pin forward.

        Only ``confirmed`` edges are inherited. A candidate is evidence
        that a version was read, not a claim about this one, and it is
        re-captured anyway if the agent read it again. Dropping a
        confirmed edge stays possible, but it now takes
        ``artifact_dismiss_source`` — a decision somebody made, rather
        than an omission.
        """
        try:
            lineage = await self.lineage(artifact_id)
        except Exception:  # lineage is an optimisation here, never the point
            logger.warning("could not read lineage for %s; not inheriting", artifact_id)
            return sources
        declared = {p.artifact_id for p in sources.pins}
        inherited = [
            VersionPin(artifact_id=e.to_artifact_id, n=e.pinned_n, state=EdgeState.CONFIRMED)
            for e in lineage.sources
            # Never the document itself: an edge recorded before this
            # was guarded would otherwise be copied onto every later
            # revision, outliving the bug that made it.
            if e.state is EdgeState.CONFIRMED
            and e.to_artifact_id not in declared
            and e.to_artifact_id != artifact_id
        ]
        if not inherited:
            return sources
        return sources.model_copy(update={"pins": [*sources.pins, *inherited]})

    async def _write(
        self,
        *,
        artifact_id: str,
        name: str,
        content: str,
        sources: SourceDeclaration,
        kind: str | None,
        author: str,
        session_id: str,
        metadata: dict[str, str] | None,
        projecting: bool = False,
    ) -> ArtifactWriteResult:
        if not content.strip():
            return ArtifactWriteResult.fail("Refusing to store an empty artifact.")
        if sources.unresolved:
            return ArtifactWriteResult.fail(
                "These sources are neither an artifact reference nor a URL: "
                + ", ".join(sources.unresolved)
                + ". Use 'artifact_id' or 'artifact_id@n' for artifacts, a full "
                "URL for external material."
            )
        # A document never derives from itself.
        #
        # The version chain already records that v6 follows v5 —
        # ``HAS_VERSION`` and ``n`` say so. A ``DERIVED_FROM`` edge
        # alongside it says something different and false: that this
        # document is *built on* an earlier draft of itself, the way
        # it is built on another document. Staleness is a query over
        # those edges, so every revision then made the document out of
        # date with respect to itself, for ever, and the banner said
        # "Sources have changed since this was written" about the one
        # document that had not changed underneath it.
        #
        # Filtered here rather than at each caller because all three
        # paths can produce one: a declared source naming the document
        # itself, an inherited edge carrying a previous mistake
        # forward, and the read ledger.
        self_pins = [p for p in sources.pins if p.artifact_id == artifact_id]
        if self_pins:
            logger.debug("dropping %d self-derivation pin(s) for %s", len(self_pins), artifact_id)
            sources = sources.model_copy(
                update={"pins": [p for p in sources.pins if p.artifact_id != artifact_id]}
            )

        embedder = self._require_embedder()
        chunks = self._chunk(content)
        # One call for the document and every passage in it. Batching
        # matters: a local sentence-transformer pays its cost per
        # call, not per string, and a document of forty paragraphs
        # would otherwise be forty round trips.
        texts = [content, *(text for text, _lf, _lt in chunks)]
        vectors = embedder.embed(texts)
        embedding = vectors[0]
        chunk_embeddings = list(vectors[1:])

        # The server first, when there is one.
        #
        # A document is a shared thing, so the copy everybody reads is
        # the copy that decides. Writing the graph first and pushing
        # afterwards produces the state this ordering exists to
        # prevent: a version that exists on one machine and nowhere
        # else, indistinguishable from a version that was saved.
        #
        # A failure here fails the write. Falling back to a local-only
        # save would be the same split store arriving quietly instead
        # of loudly.
        # Unless this version *came* from the server, in which case
        # sending it back is at best a no-op and at worst a divergence
        # — the push carries the base it was written from, and a
        # version being mirrored was written from a base this machine
        # has not reached yet. That is what ``projecting`` means: the
        # server already has this, put it in the graph and nothing
        # else.
        shared_n = (
            None
            if projecting
            else await self._write_shared(
                artifact_id=artifact_id,
                name=name,
                content=content,
                kind=kind or "note",
                author=author,
                pins=sources.pins,
            )
        )
        if isinstance(shared_n, ArtifactWriteResult):
            return shared_n

        outcome = await self._client.append_version(
            artifact_id=artifact_id,
            name=name,
            content=content,
            kind=kind,
            embedding=embedding,
            chunks=chunks,
            chunk_embeddings=chunk_embeddings,
            metadata=metadata,
            author=author,
            session_id=session_id,
            pins=sources.pins,
            citations=sources.citations,
        )

        # Candidates are attached after the version exists, and never
        # include the declared sources or the artifact itself — a
        # revision reads its own previous version on almost every turn.
        declared = [p.artifact_id for p in sources.pins]
        candidates = self._ledger.candidates(exclude=[*declared, artifact_id])
        captured = 0
        if candidates:
            captured = await self._client.record_candidates(
                version_id=outcome.version_id,
                pins=candidates,
            )

        message = f"Stored '{name}' as v{outcome.n} ({outcome.artifact_id})"
        if outcome.edges_written:
            message += f", citing {outcome.edges_written} source(s)"
        if captured:
            message += f", {captured} candidate source(s) captured for review"
        if outcome.unresolved_pins:
            message += f". Unknown source(s) skipped: {', '.join(outcome.unresolved_pins)}"
        # The ledger now holds this artifact too — a document just
        # written is a document the agent has read.
        self._ledger.record([VersionPin(artifact_id=outcome.artifact_id, n=outcome.n)])
        return ArtifactWriteResult.ok(
            message,
            artifact_id=outcome.artifact_id,
            n=outcome.n,
            version_id=outcome.version_id,
            recorded_sources=outcome.edges_written,
            captured_candidates=captured,
        )

    def _chunk(self, content: str) -> list[tuple[str, int, int]]:
        """Split content, tracking each chunk's line span.

        Line spans are resolved by walking the original text rather
        than trusting chunk order, so an overlapping chunker still
        produces spans that point at the right lines.

        The walk is **whitespace-tolerant**, and that is the whole
        difficulty. The chunker normalises as it splits — runs of
        blank lines collapse, runs of spaces collapse — so a chunk's
        text is very often not a literal substring of the document it
        came from. A plain ``find`` misses, and the version of this
        that did returned ``(0, 0)`` for those chunks: they sorted to
        the top of the document, and every line number they carried
        pointed at a line that does not exist.
        """
        documents = self.chunker.chunk(Document(content=content))
        texts = [d.content for d in documents if d.content] or [content]
        # Normalised copy plus a map back to the original offsets, so
        # a match found in normalised space still reports true lines.
        flat, offsets = _normalise_with_offsets(content)
        rows: list[tuple[str, int, int]] = []
        cursor = 0
        # Where the previous chunk ended, so an unfindable one lands
        # after it rather than at the top.
        previous_to = 1
        for text in texts:
            needle, _ = _normalise_with_offsets(text)
            found = flat.find(needle, cursor) if needle else -1
            if found < 0 and needle:
                found = flat.find(needle)
            if found < 0:
                # Genuinely unplaceable. Keep it in document order by
                # pinning it to where the last one ended: a wrong-but-
                # ordered span beats a right-looking zero.
                logger.debug("chunk could not be located in its own document; ordering only")
                rows.append((text, previous_to, previous_to))
                continue
            start = offsets[found]
            last = offsets[min(found + len(needle) - 1, len(offsets) - 1)]
            line_from = content.count("\n", 0, start) + 1
            line_to = content.count("\n", 0, last) + 1
            rows.append((text, line_from, line_to))
            previous_to = line_to
            cursor = found + max(len(needle) // 2, 1)
        return rows

    async def _write_shared(
        self,
        *,
        artifact_id: str,
        name: str,
        content: str,
        kind: str,
        author: str,
        pins: list[Any],
    ) -> int | ArtifactWriteResult:
        """Put the version on the server. Answer its number, or why not.

        An :class:`ArtifactWriteResult` coming back is a refusal the
        caller must return unchanged — the write did not happen and
        must not happen locally either.

        No server attached is not a refusal: artifacts still run for
        somebody with no account, and answering 0 lets the graph
        assign the number as it always did.
        """
        shared = self._shared
        if shared is None or not getattr(shared, "is_configured", False):
            return 0

        summary = await self._client.get_artifact(artifact_id)
        base_n = int(getattr(summary, "head_n", 0) or 0)
        page = self._render_for_share(
            artifact_id=artifact_id, name=name, content=content, kind=kind, n=base_n + 1
        )
        try:
            slug = await shared.ensure_shared(
                artifact_id=artifact_id,
                name=name,
                kind=kind,
                n=base_n + 1,
                html=page,
                author=author,
            )
            if not slug:
                return ArtifactWriteResult.fail(
                    f"The server did not give '{name}' a home to write to."
                )
            try:
                return await shared.push_content(
                    slug=slug, content=content, base_n=base_n, pins=pins, html=page
                )
            except ChainDiverged as diverged:
                # An *empty* chain is not a divergence. A document
                # written here before it had a shared copy arrives
                # with a local history the server has never seen, so
                # the first push is from v6 onto nothing — which is
                # refused by the same check that protects against a
                # colleague appending underneath.
                #
                # Distinguished by the server's own head. Zero means
                # there is nothing to collide with; anything else
                # means somebody wrote something, and that still has
                # to be refused rather than overwritten.
                if diverged.head_n:
                    raise
                return await shared.push_content(
                    slug=slug, content=content, base_n=0, pins=pins, html=page
                )
        except ChainDiverged as diverged:
            # Somebody else appended while this was being written. Not
            # merged here: two people editing one document is a
            # decision, and a version nobody wrote is the worst
            # possible answer to it.
            return ArtifactWriteResult.fail(
                f"'{name}' has moved on since this was written — the server is at "
                f"v{diverged.head_n} and this was written from v{base_n}. "
                f"Read it again before revising."
            )
        except Exception as exc:  # noqa: BLE001 — network, auth, permissions
            logger.info("shared write for %s failed: %s", artifact_id, exc)
            return ArtifactWriteResult.fail(f"Could not reach the server to store '{name}': {exc}")

    def _render_for_share(
        self, *, artifact_id: str, name: str, content: str, kind: str, n: int
    ) -> str:
        """The page a recipient opens, built from the text in hand.

        Rendered from values rather than fetched, because at this
        point the version exists nowhere yet — which is the point of
        writing the server first. ``RenderRequest`` takes everything
        it needs as data, so nothing here consults the graph.

        Without lineage: edges are settled after the version exists,
        and a page claiming sources this version has not been given
        would be wrong in the one direction that matters. The next
        write refreshes the snapshot.
        """
        now = datetime.now(UTC).isoformat(timespec="seconds")
        return ArtifactRenderer().render(
            RenderRequest(
                summary=ArtifactSummary(artifact_id=artifact_id, name=name, kind=kind, head_n=n),
                version=ArtifactVersion(
                    artifact_id=artifact_id, n=n, content=content, created_at=now
                ),
            )
        )

    def _require_embedder(self) -> Any:
        if self._embedder is None:
            raise RuntimeError(
                "ArtifactIndex requires an embedder to write or search. Pass "
                "embedder=HashEmbedder() for tests or LiveEmbedder() in "
                "production (the Session.attach_artifacts_neo4j() seam injects it)."
            )
        return self._embedder

    # ── Reads ─────────────────────────────────────────────────────────

    async def search(
        self,
        *,
        query: str,
        limit: int = 5,
        record: bool = True,
    ) -> list[ArtifactSearchHit]:
        """Semantic search over head versions.

        Recording is on by default and is the whole capture mechanism:
        a search the agent ran is a read, and a read is what a later
        write may have been built on. ``record=False`` exists for the
        panel and for delete-by-query, where a UI listing artifacts is
        not the agent learning from them.
        """
        embedder = self._require_embedder()
        query_vec = embedder.embed([query])[0]
        hits = await self._client.search(query_embedding=query_vec, limit=limit)
        if record and hits:
            self._ledger.record([VersionPin(artifact_id=h.artifact_id, n=h.n) for h in hits])
        return hits

    async def resolve(self, ref: str) -> ArtifactSummary | None:
        """Turn an id, an ``id@n`` or a name into an artifact.

        Every agent-facing entry point goes through here, so the agent
        can refer to an artifact however it last saw it named.
        """
        text = (ref or "").strip()
        if not text:
            return None
        artifact_id = text.partition("@")[0].strip()
        found = await self._client.get_artifact(artifact_id)
        if found is not None:
            return found
        return await self._client.find_by_name(text)

    async def list_artifacts(
        self, *, limit: int = 1000, include_archived: bool = False
    ) -> list[ArtifactSummary]:
        return await self._client.list_artifacts(limit=limit, include_archived=include_archived)

    async def versions(self, ref: str, *, limit: int = 200) -> list[ArtifactVersion]:
        summary = await self.resolve(ref)
        if summary is None:
            return []
        return await self._client.versions(summary.artifact_id, limit=limit)

    async def version(
        self, ref: str, n: int = 0, *, record: bool = False
    ) -> ArtifactVersion | None:
        """Fetch one revision. ``n <= 0`` means head.

        An explicit ``ref`` of the form ``id@n`` wins over the ``n``
        argument only when ``n`` was left at its default, so a caller
        that passes both does not get a silent surprise.

        ``record`` marks the read in the capture ledger. Off by
        default because most callers here are machinery — the sync,
        the exporter, the reconcile loop — and a document the
        exporter touched is not a document anybody read. The agent's
        read tool passes it, and reading something in full is a
        stronger signal of having built on it than seeing it in a
        search snippet, which was the only read being captured.
        """
        summary = await self.resolve(ref)
        if summary is None:
            return None
        wanted = n
        _, _, suffix = (ref or "").partition("@")
        if wanted <= 0 and suffix.isdigit():
            wanted = int(suffix)
        found = await self._client.version(summary.artifact_id, wanted)
        if record and found is not None:
            self._ledger.record([VersionPin(artifact_id=found.artifact_id, n=found.n)])
        return found

    async def lineage(self, ref: str, n: int = 0) -> ArtifactLineage | None:
        summary = await self.resolve(ref)
        if summary is None:
            return None
        return await self._client.lineage(summary.artifact_id, n)

    async def stale(self, *, limit: int = 100) -> list[StaleDependent]:
        return await self._client.stale_dependents(limit=limit)

    async def count(self) -> int:
        return await self._client.count()

    async def version_count(self) -> int:
        return await self._client.version_count()

    async def candidate_count(self) -> int:
        return await self._client.candidate_count()

    async def has_artifact(self, artifact_id: str) -> bool:
        return await self._client.has_artifact(artifact_id)

    # ── Lineage maintenance ───────────────────────────────────────────

    async def confirm_source(self, *, ref: str, source_ref: str) -> bool:
        """Promote a candidate edge to confirmed."""
        return await self._set_state(ref, source_ref, EdgeState.CONFIRMED)

    async def dismiss_source(self, *, ref: str, source_ref: str) -> bool:
        """Mark a captured read as not a real derivation.

        Dismissed rather than deleted: re-reading the same source
        would otherwise re-capture it and the reviewer would decide
        the same thing again every session.
        """
        return await self._set_state(ref, source_ref, EdgeState.DISMISSED)

    async def _set_state(self, ref: str, source_ref: str, state: EdgeState) -> bool:
        summary = await self.resolve(ref)
        source = await self.resolve(source_ref)
        if summary is None or source is None:
            return False
        return await self._client.set_edge_state(
            artifact_id=summary.artifact_id,
            n=summary.head_n,
            source_artifact_id=source.artifact_id,
            state=state,
        )

    async def reconcile_plan(self, stale: StaleDependent) -> ReconcilePlan | None:
        """Build the pinned→head diff behind one stale dependency."""
        pinned = await self._client.version(stale.source_artifact_id, stale.pinned_n)
        head = await self._client.version(stale.source_artifact_id, 0)
        dependent = await self._client.version(stale.dependent_artifact_id, stale.dependent_n)
        if pinned is None or head is None or dependent is None:
            return None
        return self._reconciler.plan(
            stale=stale,
            dependent_content=dependent.content,
            pinned_content=pinned.content,
            head_content=head.content,
        )

    async def accept_reconcile(
        self,
        *,
        plan: ReconcilePlan,
        content: str | None = None,
        author: str = "",
        session_id: str = "",
    ) -> ArtifactWriteResult:
        """Resolve a stale dependency.

        With ``content``, appends a revised version pinned to the
        source's new head. Without it, the reviewer read the diff and
        decided nothing needs changing — the edge is re-pinned in
        place so the same decision isn't asked for again. Both are
        resolutions; only one is an edit.
        """
        if content is None or not content.strip():
            moved = await self._client.repin(
                artifact_id=plan.dependent_artifact_id,
                n=plan.dependent_n,
                source_artifact_id=plan.source_artifact_id,
            )
            if not moved:
                return ArtifactWriteResult.fail(
                    "Nothing to re-pin — the edge may already point at head."
                )
            return ArtifactWriteResult.ok(
                f"Re-pinned {plan.dependent_name or plan.dependent_artifact_id} to "
                f"{plan.source_name or plan.source_artifact_id}@{plan.source_head_n} "
                f"without changing its content.",
                artifact_id=plan.dependent_artifact_id,
                n=plan.dependent_n,
            )
        sources = SourceDeclaration(
            pins=[
                VersionPin(
                    artifact_id=plan.source_artifact_id,
                    n=plan.source_head_n,
                    state=EdgeState.CONFIRMED,
                    at=self._codec.now_iso(),
                )
            ]
        )
        return await self.revise(
            ref=plan.dependent_artifact_id,
            content=content,
            sources=sources,
            author=author,
            session_id=session_id,
        )

    async def prune_candidates(self, *, before_iso: str) -> int:
        return await self._client.prune_candidates(before_iso=before_iso)

    # ── Deletes ───────────────────────────────────────────────────────

    async def delete(self, ref: str) -> bool:
        summary = await self.resolve(ref)
        if summary is None:
            return False
        self._ledger.forget(summary.artifact_id)
        return await self._client.delete_artifact(summary.artifact_id)

    async def delete_by_query(self, query: str, *, limit: int = 10) -> ArtifactDeleteResult:
        """Find artifacts matching ``query`` and delete them.

        Searches with ``record=False``: deleting something is not
        learning from it, and letting a delete seed the capture ledger
        would attach candidate edges to whatever is written next.
        """
        hits = await self.search(query=query, limit=limit, record=False)
        if not hits:
            return ArtifactDeleteResult(deleted=0, reason="no matches")
        deleted = 0
        errors: list[str] = []
        for hit in hits:
            try:
                if await self._client.delete_artifact(hit.artifact_id):
                    deleted += 1
                    self._ledger.forget(hit.artifact_id)
            except Exception as exc:  # surface rather than swallow
                errors.append(f"{hit.artifact_id}: {exc}")
        return ArtifactDeleteResult(deleted=deleted, errors=errors)


def _normalise_with_offsets(text: str) -> tuple[str, list[int]]:
    """Apply the chunker's whitespace rules, keeping a map home.

    Returns the normalised text and, for each character in it, the
    index it came from in ``text``. Mirrors
    :meth:`NewlinePreservingChunker.clean_text` — line endings
    unified, three-or-more newlines capped at two, runs of spaces and
    tabs collapsed to one — because the point is to match what the
    chunker produced against what the author wrote.
    """
    out: list[str] = []
    offsets: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            # ``\r\n`` and a bare ``\r`` both become one ``\n``.
            out.append("\n")
            offsets.append(i)
            i += 2 if i + 1 < n and text[i + 1] == "\n" else 1
            continue
        if ch == "\n":
            run = 0
            j = i
            while j < n and text[j] in "\n\r":
                if text[j] == "\n":
                    run += 1
                j += 1
            kept = min(run, 2)
            for _ in range(kept):
                out.append("\n")
                offsets.append(i)
            i = j
            continue
        if ch in " \t\f\v":
            j = i
            while j < n and text[j] in " \t\f\v":
                j += 1
            out.append(" ")
            offsets.append(i)
            i = j
            continue
        out.append(ch)
        offsets.append(i)
        i += 1
    if not offsets:
        offsets = [0]
    return "".join(out), offsets
