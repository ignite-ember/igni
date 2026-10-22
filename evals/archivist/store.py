"""An artifact store for the archivist eval, in memory.

The agent goes through the real pipeline — real model, real toolkit,
real source-declaration policy, real read-only guard. Only the
database is fake, which is the same boundary
``codeindex_architect_cypher`` stubs and for the same reason: a real
Neo4j is a heavy dependency for a suite that already costs a model
call per case.

The fake matters more than a mock usually would, because the agent is
graded on judgements it makes *in response to what the store says*.
If the store reported staleness wrongly, the eval would grade the
agent against wrong feedback and the result would mean nothing. So
this implements the semantics rather than canned answers:

* a write appends ``n+1`` and moves the head
* a pin records the version that was read and does not move
* staleness is ``pinned_n < head_n`` over **confirmed** edges only
* a dismissed edge stays dismissed

``tests/test_eval_fake_store_matches_neo4j.py`` runs one scenario
through this and through :class:`Neo4jArtifactClient` and asserts they
agree, so the fake cannot quietly drift from the thing it stands in
for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import (
    ArtifactLineage,
    ArtifactSearchHit,
    ArtifactSummary,
    ArtifactVersion,
    Citation,
    Comment,
    CommentStatus,
    EdgeState,
    LineageEdge,
    StaleDependent,
    VersionPin,
)
from ember_code.core.artifacts.neo4j_client import WriteOutcome


@dataclass
class _Edge:
    source_artifact_id: str
    pinned_n: int
    state: EdgeState
    at: str


@dataclass
class _Version:
    artifact_id: str
    n: int
    content: str
    head: bool = True
    created_at: str = ""
    author: str = ""
    session_id: str = ""
    metadata: dict = field(default_factory=dict)
    edges: list[_Edge] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    #: ``(text, line_from, line_to)`` per passage, as the chunker
    #: produced them. Kept — the vectors still are not — because
    #: "did this version go through the write path, or was it
    #: inserted behind it" is a question about chunks, and a version
    #: with none is invisible to drift and to every lineage question
    #: that reasons about paragraphs.
    chunks: list = field(default_factory=list)

    @property
    def version_id(self) -> str:
        return f"{self.artifact_id}::{self.n}"


@dataclass
class _Artifact:
    artifact_id: str
    name: str
    kind: str = "note"
    head_n: int = 0
    created_at: str = ""
    updated_at: str = ""
    author: str = ""
    origin: str = ""
    versions: list[_Version] = field(default_factory=list)


class FakeArtifactStore:
    """Implements the :class:`Neo4jArtifactClient` surface, in memory."""

    def __init__(self, project_id: str = "eval-project") -> None:
        self._project_id = project_id
        self._artifacts: dict[str, _Artifact] = {}
        self._comments: dict[str, Comment] = {}
        self._codec = ArtifactMetadataCodec()
        # The cypher service reaches for this; a sentinel is enough
        # because ``_execute`` is stubbed above it.
        self._driver = object()

    @property
    def project_id(self) -> str:
        return self._project_id

    async def apply_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # ── Writes ────────────────────────────────────────────────────────

    async def append_version(
        self,
        *,
        artifact_id: str,
        name: str,
        content: str,
        kind: str | None = None,
        embedding: list[float] | None = None,
        chunks: list | None = None,
        # Accepted and ignored: nothing here reads a vector. Present so
        # the signature keeps matching the real client's. ``chunks``
        # above *is* kept — see ``_Version.chunks``.
        chunk_embeddings: list | None = None,
        metadata: dict | None = None,
        author: str = "",
        session_id: str = "",
        origin: str = "",
        pins: list[VersionPin] | None = None,
        citations: list[Citation] | None = None,
    ) -> WriteOutcome:
        now = _now()
        art = self._artifacts.get(artifact_id)
        if art is None:
            art = _Artifact(
                artifact_id=artifact_id,
                name=name,
                kind=kind or "note",
                created_at=now,
                author=author,
                origin=origin,
            )
            self._artifacts[artifact_id] = art
        art.name = name
        if kind:
            art.kind = kind
        art.updated_at = now

        for existing in art.versions:
            existing.head = False
        art.head_n += 1
        version = _Version(
            artifact_id=artifact_id,
            n=art.head_n,
            content=content,
            head=True,
            created_at=now,
            author=author,
            session_id=session_id,
            metadata=dict(metadata or {}),
            chunks=list(chunks or []),
        )
        art.versions.append(version)

        written, unresolved = 0, []
        for pin in pins or []:
            source = self._artifacts.get(pin.artifact_id)
            if source is None:
                unresolved.append(pin.artifact_id)
                continue
            pinned = source.head_n if pin.n <= 0 else pin.n
            version.edges.append(_Edge(pin.artifact_id, pinned, pin.state, now))
            written += 1
        version.citations.extend(citations or [])

        return WriteOutcome(
            artifact_id=artifact_id,
            version_id=version.version_id,
            n=version.n,
            edges_written=written,
            unresolved_pins=unresolved,
        )

    async def passages(self, *, artifact_id: str, n: int) -> list[Any]:
        """Every chunk of one version, in the order the chunker made them.

        Returns lightweight stand-ins rather than the real ``Passage``
        — the fields anything here reads are ``text``, ``line_from``
        and ``line_to``.
        """
        version = self._version(artifact_id, n)
        if version is None:
            return []
        return [
            SimpleNamespace(
                chunk_id=f"{version.version_id}::{ordinal}",
                text=text,
                line_from=line_from,
                line_to=line_to,
                ordinal=ordinal,
            )
            for ordinal, (text, line_from, line_to) in enumerate(version.chunks)
        ]

    async def project_comments(self, comments: list[Comment]) -> int:
        """Mirror the real client, including the part that skips.

        A comment naming a version this store does not hold is not
        placed — the real projection binds with ``MATCH`` and simply
        fails to. A fake that accepted it anyway would let a test pass
        against behaviour the product does not have.
        """
        placed = 0
        for comment in comments:
            artifact = self._artifacts.get(comment.artifact_id)
            if artifact is None or not any(v.n == comment.n for v in artifact.versions):
                continue
            self._comments[comment.comment_id] = comment
            placed += 1
        return placed

    async def comments(
        self,
        *,
        artifact_id: str | None = None,
        n: int | None = None,
        open_only: bool = False,
        limit: int = 500,
    ) -> list[Comment]:
        rows = [
            c
            for c in self._comments.values()
            if (artifact_id is None or c.artifact_id == artifact_id)
            and (n is None or c.n == n)
            and (not open_only or c.status is CommentStatus.OPEN)
        ]
        return sorted(rows, key=lambda c: c.at)[:limit]

    async def open_comment_count(self) -> int:
        return sum(1 for c in self._comments.values() if c.status is CommentStatus.OPEN)

    async def record_candidates(self, *, version_id: str, pins: list[VersionPin]) -> int:
        version = self._version_by_id(version_id)
        if version is None:
            return 0
        added = 0
        for pin in pins:
            source = self._artifacts.get(pin.artifact_id)
            if source is None or pin.artifact_id == version.artifact_id:
                continue
            # Never downgrade an edge somebody already ruled on.
            if any(e.source_artifact_id == pin.artifact_id for e in version.edges):
                continue
            pinned = source.head_n if pin.n <= 0 else pin.n
            version.edges.append(_Edge(pin.artifact_id, pinned, EdgeState.CANDIDATE, _now()))
            added += 1
        return added

    async def set_edge_state(
        self, *, artifact_id: str, n: int, source_artifact_id: str, state: EdgeState
    ) -> bool:
        version = self._version(artifact_id, n)
        if version is None:
            return False
        hit = False
        for edge in version.edges:
            if edge.source_artifact_id == source_artifact_id:
                edge.state = state
                hit = True
        return hit

    async def repin(self, *, artifact_id: str, n: int, source_artifact_id: str) -> int:
        version = self._version(artifact_id, n)
        source = self._artifacts.get(source_artifact_id)
        if version is None or source is None:
            return 0
        moved = 0
        for edge in version.edges:
            if edge.source_artifact_id == source_artifact_id and edge.pinned_n != source.head_n:
                edge.pinned_n = source.head_n
                edge.state = EdgeState.CONFIRMED
                moved += 1
        return moved

    async def prune_candidates(self, *, before_iso: str) -> int:
        pruned = 0
        for art in self._artifacts.values():
            for version in art.versions:
                keep = [
                    e
                    for e in version.edges
                    if not (e.state is EdgeState.CANDIDATE and e.at < before_iso)
                ]
                pruned += len(version.edges) - len(keep)
                version.edges = keep
        return pruned

    async def delete_artifact(self, artifact_id: str) -> bool:
        self._artifacts.pop(artifact_id, None)
        for art in self._artifacts.values():
            for version in art.versions:
                version.edges = [e for e in version.edges if e.source_artifact_id != artifact_id]
        return True

    # ── Reads ─────────────────────────────────────────────────────────

    async def search(self, *, query_embedding, limit: int = 5) -> list[ArtifactSearchHit]:
        """Rank heads by word overlap with the seeded query text.

        The real store ranks by cosine distance over embeddings. A
        fake cannot reproduce that and should not pretend to — what it
        has to preserve is the property the agent depends on: **heads
        only**, so the agent never sees text a later version replaced.
        """
        query = getattr(query_embedding, "eval_text", "") or ""
        terms = {w for w in query.lower().split() if len(w) > 3}
        scored = []
        for art in self._artifacts.values():
            head = self._head(art)
            if head is None:
                continue
            words = set(head.content.lower().split()) | set(art.name.lower().split())
            overlap = len(terms & words)
            if overlap or not terms:
                scored.append((overlap, art, head))
        scored.sort(key=lambda row: row[0], reverse=True)
        return [
            ArtifactSearchHit(
                artifact_id=art.artifact_id,
                name=art.name,
                kind=art.kind,
                n=head.n,
                content=head.content,
                score=float(score),
                project=self._project_id,
            )
            for score, art, head in scored[:limit]
        ]

    async def count(self) -> int:
        return len(self._artifacts)

    async def version_count(self) -> int:
        return sum(len(a.versions) for a in self._artifacts.values())

    async def candidate_count(self) -> int:
        return sum(
            1
            for a in self._artifacts.values()
            for v in a.versions
            if v.head
            for e in v.edges
            if e.state is EdgeState.CANDIDATE
        )

    async def list_artifacts(
        self, *, limit: int = 1000, include_archived: bool = False
    ) -> list[ArtifactSummary]:
        # Nothing here is ever archived, so the flag changes nothing —
        # but the signature has to match ``Neo4jArtifactClient``, because
        # the whole point of this store is that the two answer the same
        # questions the same way. It did not, and every test in
        # ``test_eval_fake_store_matches_neo4j`` failed on the TypeError
        # before reaching anything it meant to compare.
        _ = include_archived
        return [self._summary(a) for a in self._artifacts.values()][:limit]

    async def get_artifact(self, artifact_id: str) -> ArtifactSummary | None:
        art = self._artifacts.get(artifact_id)
        return self._summary(art) if art else None

    async def find_by_name(self, name: str) -> ArtifactSummary | None:
        by_id = self._artifacts.get(self._codec.artifact_id(name))
        if by_id is not None:
            return self._summary(by_id)
        for art in self._artifacts.values():
            if art.name.casefold() == name.casefold():
                return self._summary(art)
        return None

    async def versions(self, artifact_id: str, *, limit: int = 200) -> list[ArtifactVersion]:
        art = self._artifacts.get(artifact_id)
        if art is None:
            return []
        return [self._as_version(v) for v in sorted(art.versions, key=lambda v: -v.n)][:limit]

    async def version(self, artifact_id: str, n: int) -> ArtifactVersion | None:
        found = self._version(artifact_id, n)
        return self._as_version(found) if found else None

    async def has_artifact(self, artifact_id: str) -> bool:
        return artifact_id in self._artifacts

    async def lineage(self, artifact_id: str, n: int = 0) -> ArtifactLineage:
        version = self._version(artifact_id, n)
        if version is None:
            return ArtifactLineage(artifact_id=artifact_id, n=n)

        sources = []
        for edge in version.edges:
            source = self._artifacts.get(edge.source_artifact_id)
            if source is None:
                continue
            sources.append(
                LineageEdge(
                    from_artifact_id=artifact_id,
                    from_n=version.n,
                    from_name=self._artifacts[artifact_id].name,
                    to_artifact_id=source.artifact_id,
                    to_n=edge.pinned_n,
                    to_name=source.name,
                    state=edge.state,
                    pinned_n=edge.pinned_n,
                    source_head_n=source.head_n,
                    at=edge.at,
                )
            )

        dependents = []
        for other in self._artifacts.values():
            for other_version in other.versions:
                for edge in other_version.edges:
                    if edge.source_artifact_id != artifact_id:
                        continue
                    if edge.pinned_n != version.n:
                        continue
                    dependents.append(
                        LineageEdge(
                            from_artifact_id=other.artifact_id,
                            from_n=other_version.n,
                            from_name=other.name,
                            to_artifact_id=artifact_id,
                            to_n=version.n,
                            to_name=self._artifacts[artifact_id].name,
                            state=edge.state,
                            pinned_n=edge.pinned_n,
                            source_head_n=self._artifacts[artifact_id].head_n,
                            at=edge.at,
                        )
                    )
        return ArtifactLineage(
            artifact_id=artifact_id,
            n=version.n,
            sources=sources,
            dependents=dependents,
            citations=list(version.citations),
        )

    async def stale_dependents(self, *, limit: int = 100) -> list[StaleDependent]:
        rows = []
        for art in self._artifacts.values():
            head = self._head(art)
            if head is None:
                continue
            for edge in head.edges:
                if edge.state is not EdgeState.CONFIRMED:
                    continue
                source = self._artifacts.get(edge.source_artifact_id)
                if source is None or edge.pinned_n >= source.head_n:
                    continue
                rows.append(
                    StaleDependent(
                        dependent_artifact_id=art.artifact_id,
                        dependent_name=art.name,
                        dependent_n=head.n,
                        source_artifact_id=source.artifact_id,
                        source_name=source.name,
                        pinned_n=edge.pinned_n,
                        source_head_n=source.head_n,
                    )
                )
        rows.sort(key=lambda r: r.versions_behind, reverse=True)
        return rows[:limit]

    # ── Internals ─────────────────────────────────────────────────────

    def _head(self, art: _Artifact) -> _Version | None:
        return next((v for v in art.versions if v.head), None)

    def _version(self, artifact_id: str, n: int) -> _Version | None:
        art = self._artifacts.get(artifact_id)
        if art is None:
            return None
        if n <= 0:
            return self._head(art)
        return next((v for v in art.versions if v.n == n), None)

    def _version_by_id(self, version_id: str) -> _Version | None:
        artifact_id, _, raw_n = version_id.rpartition("::")
        try:
            return self._version(artifact_id, int(raw_n))
        except ValueError:
            return None

    def _summary(self, art: _Artifact) -> ArtifactSummary:
        return ArtifactSummary(
            artifact_id=art.artifact_id,
            name=art.name,
            kind=art.kind,
            head_n=art.head_n,
            created_at=art.created_at,
            updated_at=art.updated_at,
            author=art.author,
            origin=art.origin,
            version_count=len(art.versions),
        )

    def _as_version(self, v: _Version) -> ArtifactVersion:
        return ArtifactVersion(
            artifact_id=v.artifact_id,
            version_id=v.version_id,
            n=v.n,
            content=v.content,
            content_hash=self._codec.content_hash(v.content),
            head=v.head,
            created_at=v.created_at,
            author=v.author,
            session_id=v.session_id,
            metadata=dict(v.metadata),
        )


class EvalEmbedder:
    """Carries the query text through to the fake's ranking.

    The real embedder produces 384 floats the fake cannot rank by, so
    the vector doubles as a carrier: it is a real-shaped list with the
    source text attached. Nothing in the agent's path reads it.
    """

    class _Vector(list):
        eval_text: str = ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = self._Vector([0.0] * 384)
            vec.eval_text = text
            out.append(vec)
        return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeSharedServer:
    """The cloud side, in memory, for the same reason as the store above.

    Artifacts are a cloud service: a session with no account writes
    nothing, so anything exercising a write needs a server for it to
    reach. Records what arrived and answers the way the real client
    does — a slug per artifact, kept once minted, and a version
    number one past the base it was given.
    """

    is_configured = True

    def __init__(self, *, slug: str = "s1", fail: Exception | None = None) -> None:
        self._slug = slug
        self._fail = fail
        self.published: list[dict[str, Any]] = []
        self.pushed: list[dict[str, Any]] = []
        #: Set once published, the way the real client's cache is.
        self.known: dict[str, str] = {}
        #: Title per artifact id, as published.
        self.titles: dict[str, str] = {}
        #: Head version per slug, from what has been pushed.
        self.heads: dict[str, int] = {}
        #: Edges per ``(slug, n)``. Per *version*, like the real
        #: server — see :meth:`push_content`.
        self.edges: dict[tuple[str, int], list[dict[str, Any]]] = {}

    async def ensure_shared(self, **kwargs: Any) -> str:
        artifact_id = kwargs["artifact_id"]
        if artifact_id in self.known:
            return self.known[artifact_id]
        self.published.append(kwargs)
        # A slug *per document*. This handed out one for all of them,
        # which was harmless while nothing here was keyed by slug and
        # is not once lineage is: every document's edges landed on the
        # same key and overwrote each other. The first keeps the
        # configured value so single-document tests still read `s1`.
        self.known[artifact_id] = (
            self._slug if not self.known else f"{self._slug}-{len(self.known)}"
        )
        # The real server answers lineage with *titles*, because that
        # is what gets rendered. Returning an id here would have made
        # every assertion about a document's name pass or fail on the
        # fake's spelling rather than the product's.
        self.titles[artifact_id] = str(kwargs.get("name") or artifact_id)
        return self.known[artifact_id]

    async def push_content(self, **kwargs: Any) -> int:
        if self._fail is not None:
            raise self._fail
        self.pushed.append(kwargs)
        n = int(kwargs["base_n"]) + 1
        slug = str(kwargs.get("slug") or self._slug)
        self.heads[slug] = max(self.heads.get(slug, 0), n)
        # Edges hang off a *version*, so they are recorded per `n`.
        # The real server does the same, and it is what makes a
        # revision start with none unless its declaration re-states
        # them — which is the behaviour `_inherit_confirmed` exists to
        # supply and which these fakes have to reproduce or the
        # inheritance tests measure nothing.
        self.edges[(slug, n)] = [
            {
                "artifact_id": p.artifact_id,
                "n": p.n,
                "state": getattr(p.state, "value", str(p.state)),
                "passages": [
                    {"chunk_id": c.chunk_id, "quote": c.quote} for c in getattr(p, "passages", [])
                ],
            }
            for p in (kwargs.get("pins") or [])
        ]
        return n

    #: What :meth:`search` answers with. ``None`` means this
    #: deployment has no document search configured, which the real
    #: server distinguishes from "nothing matched" and so does this.
    hits: list[dict[str, Any]] | None = None

    async def search(self, *, query: str, limit: int = 5) -> list[dict[str, Any]] | None:
        self.searched = {"query": query, "limit": limit}
        if self.hits is None:
            return None
        return self.hits[:limit]

    #: Make slug lookup fail, the way an unreachable server does.
    #: Set by tests of the paths that exist *for* that case — local
    #: drafts, most of all. Explicit, because those tests used to get
    #: there by accident: this class had no ``slug_for`` at all, the
    #: caller caught the AttributeError, and the fallback ran for a
    #: reason nobody had chosen.
    slug_lookup_fails: bool = False

    async def slug_for(self, artifact_id: str) -> str:
        """The slug this artifact was published under, if any."""
        if self.slug_lookup_fails:
            raise ConnectionError("the server is not reachable")
        return self.known.get(artifact_id, "")

    async def revoke(self, *, artifact_id: str) -> bool:
        """Take the server's copy away. Answer whether there was one.

        ``ArtifactIndex.delete`` revokes here before touching the
        graph, and raises ``DeleteRefused`` on anything that is not a
        clean ``True`` — including the ``AttributeError`` from this
        method not existing, which is how a store that cannot delete
        looked exactly like a server that refused.
        """
        slug = self.known.pop(artifact_id, "")
        if not slug:
            return False
        self.titles.pop(artifact_id, None)
        head = self.heads.pop(slug, 0)
        for n in range(1, head + 1):
            self.edges.pop((slug, n), None)
        return True

    def _head_of(self, artifact_id: str, fallback: int) -> int:
        """Where a source has got to, from what has been pushed.

        Derived rather than held in a second dict: a fixture that sets
        heads by hand can disagree with what it pushed, and then a
        staleness test passes against a state the product could not
        produce.
        """
        return self.heads.get(self.known.get(artifact_id, ""), fallback)

    def _artifact_of(self, slug: str) -> str:
        for artifact_id, known in self.known.items():
            if known == slug:
                return artifact_id
        return ""

    async def lineage(self, *, slug: str, n: int = 0) -> dict[str, Any] | None:
        """Sources of one version, answered from what was pushed.

        Answers in the server's shape rather than the store's, because
        that is what the client parses — including the `artifact_id`
        on both ends. Returning a slug there is the mistake that
        breaks inheritance silently: the revision re-declares its
        sources by artifact id, a slug resolves to nothing, and the
        document quietly stops saying what it stands on.
        """
        self.asked_lineage = {"slug": slug, "n": n}
        if slug not in self.heads and slug != self._slug:
            return None
        asked = n or self.heads.get(slug, 0)
        dependent = self._artifact_of(slug)
        sources = [
            {
                "id": f"{slug}:{asked}:{pin['artifact_id']}",
                "dependent_slug": slug,
                "dependent_artifact_id": dependent,
                "dependent_title": self.titles.get(dependent, dependent),
                "dependent_n": asked,
                "source_slug": self.known.get(pin["artifact_id"], pin["artifact_id"]),
                "source_artifact_id": pin["artifact_id"],
                "source_title": self.titles.get(pin["artifact_id"], pin["artifact_id"]),
                "pinned_n": pin["n"],
                "source_head_n": self._head_of(pin["artifact_id"], pin["n"]),
                "state": pin["state"],
                "is_stale": pin["state"] == "confirmed"
                and pin["n"] < self._head_of(pin["artifact_id"], pin["n"]),
            }
            for pin in self.edges.get((slug, asked), [])
        ]
        return {"n": asked, "sources": sources, "dependents": [], "citations": [], "hidden": 0}

    async def decide_edge(self, *, edge_id: str, state: str) -> dict[str, Any] | None:
        """Confirm or dismiss, by the id :meth:`lineage` handed out.

        The id is ``slug:n:source``, so the edge it names can be found
        without a second index — the real server has a row id and this
        does not, and inventing one here would mean the fake and the
        product disagreeing about what an edge id *is*.
        """
        slug, _, rest = edge_id.partition(":")
        version, _, source = rest.partition(":")
        try:
            edges = self.edges.get((slug, int(version)), [])
        except ValueError:
            return None
        for edge in edges:
            if edge["artifact_id"] == source:
                edge["state"] = state
                return {"id": edge_id, "state": state}
        return None

    async def repin_edge(self, *, edge_id: str, to_n: int) -> dict[str, Any] | None:
        slug, _, rest = edge_id.partition(":")
        version, _, source = rest.partition(":")
        try:
            edges = self.edges.get((slug, int(version)), [])
        except ValueError:
            return None
        for edge in edges:
            if edge["artifact_id"] == source:
                edge["n"] = to_n
                return {"id": edge_id, "pinned_n": to_n}
        return None

    async def stale(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Every confirmed edge of the head whose source has moved."""
        rows: list[dict[str, Any]] = []
        for slug, head in self.heads.items():
            body = await self.lineage(slug=slug, n=head)
            rows += [
                {
                    "edge_id": row["id"],
                    "dependent_slug": row["dependent_slug"],
                    "dependent_artifact_id": row["dependent_artifact_id"],
                    "dependent_title": row["dependent_title"],
                    "dependent_n": row["dependent_n"],
                    "source_slug": row["source_slug"],
                    "source_artifact_id": row["source_artifact_id"],
                    "source_title": row["source_title"],
                    "pinned_n": row["pinned_n"],
                    "source_head_n": row["source_head_n"],
                    "behind": row["source_head_n"] - row["pinned_n"],
                }
                for row in (body or {}).get("sources", [])
                if row["is_stale"]
            ]
        return rows[:limit]
