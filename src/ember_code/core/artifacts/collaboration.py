"""Two-way sync between the local artifact graph and the server.

The server is the **ordering authority** for a co-authored document
and nothing else. It decides what order appends happened in. It does
not merge, it cannot resolve a derivation edge, and it has no view of
anybody's graph — ``sources`` travels through it as opaque JSON so
each collaborator rebuilds the same edges locally.

Everything else lives here, and that is the point: once a pull has
run, the whole history is in the local graph. Versions, who wrote
them, and the comments on each one all answer to Cypher alongside
staleness and lineage, with the network down.

**Pull, then push.** Always that order. The push carries the base it
was written from, so pushing first means colliding with work this
machine could simply have read a moment earlier. A collision is still
handled — the server refuses and hands back what is missing — but a
refusal the client could have avoided costs a round trip and makes
every concurrent edit look like a conflict.

**A refused push is not an error.** ``ChainDiverged`` carries the
versions the server has that this machine does not, which is exactly
what a rebase needs. There is no automatic merge: two people editing
the same paragraph is a decision, and a document nobody wrote is the
worst possible answer to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.models import (
    Comment,
    CommentStatus,
    SourceDeclaration,
)

logger = logging.getLogger(__name__)

#: Sync is a background errand, not something a person waits on, but
#: it must not hang a shutdown either.
SYNC_TIMEOUT_SECONDS: float = 30.0

#: Comments per pull. The cursor makes this a page size rather than a
#: cap — a client behind by thousands catches up over several pulls
#: instead of asking for all of them in one request that times out.
COMMENT_PAGE: int = 500


@dataclass(frozen=True)
class RemoteVersion:
    """One version as the server has it."""

    n: int
    base_n: int
    content: str
    sources: dict[str, Any] = field(default_factory=dict)
    author_email: str = ""
    created_at: str = ""


class ChainDiverged(Exception):
    """The push was refused: somebody else appended first.

    Carries the versions this machine is missing, so the caller can
    show them and rebase without a second request. Deliberately not
    an automatic merge — see the module docstring.
    """

    def __init__(self, head_n: int, missing: list[RemoteVersion]) -> None:
        super().__init__(f"remote head is {head_n}; {len(missing)} version(s) missing locally")
        self.head_n = head_n
        self.missing = missing


class NotAllowed(Exception):
    """This account may not append to that document, or it is gone.

    One exception for both because the server answers 404 for both,
    on purpose: distinguishing them would confirm a private document
    exists to whoever guessed its slug.
    """


@dataclass
class PullResult:
    versions_applied: int = 0
    comments_projected: int = 0
    head_n: int = 0
    #: Server cursor for the next comment pull. Carried verbatim —
    #: a client deriving it from its own clock skips rows whenever the
    #: two disagree.
    cursor: str | None = None


class CollaborationSync:
    """Pulls a co-authored chain and its comments; pushes local work."""

    def __init__(
        self,
        index: ArtifactIndex,
        *,
        server_url: str,
        access_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._index = index
        self._server_url = (server_url or "").rstrip("/")
        self._access_token = access_token
        self._client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._server_url and self._access_token)

    # ── Transport ────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.is_configured:
            raise RuntimeError("Collaboration needs a signed-in igni account — run `igni login`.")
        client = self._client
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=SYNC_TIMEOUT_SECONDS)
        try:
            return await client.request(
                method,
                f"{self._server_url}{path}",
                headers={"Authorization": f"Bearer {self._access_token}"},
                **kwargs,
            )
        finally:
            if owns:
                await client.aclose()

    # ── Pull ─────────────────────────────────────────────────────────

    async def pull(self, *, slug: str, ref: str, cursor: str | None = None) -> PullResult:
        """Bring the local chain up to the server's, then its comments.

        ``ref`` names the local artifact the chain belongs to. Versions
        are applied in order through the normal write path, so every
        one of them gets chunked, embedded and edge-attached exactly as
        a locally authored version would. A projected version that
        skipped that path would be invisible to search — present in the
        graph and absent from every answer, which is worse than not
        having it.
        """
        result = PullResult()
        local_head = await self._local_head(ref)

        response = await self._request(
            "GET", f"/v1/artifacts/shared/{slug}/versions", params={"after": local_head}
        )
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        body = response.json()
        result.head_n = int(body.get("head_n") or 0)

        for row in body.get("versions") or []:
            remote = RemoteVersion(
                n=int(row.get("n") or 0),
                base_n=int(row.get("base_n") or 0),
                content=str(row.get("content") or ""),
                sources=dict(row.get("sources") or {}),
                author_email=str(row.get("author_email") or ""),
                created_at=str(row.get("created_at") or ""),
            )
            await self._apply(ref=ref, remote=remote)
            result.versions_applied += 1

        result.comments_projected, result.cursor = await self.pull_comments(cursor=cursor)
        return result

    async def _apply(self, *, ref: str, remote: RemoteVersion) -> None:
        """Append one remote version locally.

        The author is carried through so the local chain records who
        actually wrote each version rather than attributing the lot to
        whoever ran the sync — the main reason mirroring the chain is
        worth doing at all.
        """
        await self._index.revise(
            ref=ref,
            content=remote.content,
            sources=SourceDeclaration.model_validate(remote.sources)
            if remote.sources
            else SourceDeclaration(),
            author=remote.author_email,
        )

    async def pull_comments(self, *, cursor: str | None = None) -> tuple[int, str | None]:
        """Project comment activity into the graph.

        Ordered by the server on ``updated_at``, so a comment
        *resolved* on another machine arrives here too — its creation
        time has not changed, and a ``created_at`` cursor would never
        show it again.
        """
        params: dict[str, Any] = {"limit": COMMENT_PAGE}
        if cursor:
            params["since"] = cursor
        response = await self._request("GET", "/v1/artifacts/comments/sync", params=params)
        response.raise_for_status()
        body = response.json()

        comments = [_to_comment(row) for row in (body.get("comments") or [])]
        projected = await self._index.client.project_comments(comments)
        if comments and projected < len(comments):
            # Versions this machine has not pulled yet. Not an error:
            # the cursor is the server's and does not advance past
            # them, so the next pull places them.
            logger.info(
                "projected %d of %d comments; the rest name versions not pulled yet",
                projected,
                len(comments),
            )
        return projected, body.get("cursor") or cursor

    # ── Push ─────────────────────────────────────────────────────────

    async def push(self, *, slug: str, ref: str, base_n: int, html: str | None = None) -> int:
        """Append the local head to the shared chain.

        Raises :class:`ChainDiverged` when ``base_n`` is no longer the
        server's head, carrying the versions to rebase onto.
        """
        version = await self._index.version(ref, 0)
        if version is None:
            raise NotAllowed(f"no local artifact matches {ref!r}")

        lineage = await self._index.lineage(ref)
        payload: dict[str, Any] = {
            "content": version.content,
            "base_n": base_n,
            # Confirmed edges only. A candidate is this machine's
            # record that something was read; sending it would plant
            # an unadjudicated edge in a colleague's graph that they
            # never saw the evidence for.
            "sources": {
                "pins": [
                    {"artifact_id": e.to_artifact_id, "n": e.pinned_n, "state": e.state.value}
                    for e in lineage.sources
                    if e.state.value == "confirmed"
                ]
            },
        }
        if html is not None:
            payload["html"] = html

        response = await self._request(
            "POST", f"/v1/artifacts/shared/{slug}/versions", json=payload
        )
        if response.status_code == 409:
            body = response.json()
            raise ChainDiverged(
                head_n=int(body.get("head_n") or 0),
                missing=[
                    RemoteVersion(
                        n=int(r.get("n") or 0),
                        base_n=int(r.get("base_n") or 0),
                        content=str(r.get("content") or ""),
                        sources=dict(r.get("sources") or {}),
                        author_email=str(r.get("author_email") or ""),
                        created_at=str(r.get("created_at") or ""),
                    )
                    for r in (body.get("missing") or [])
                ],
            )
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        return int(response.json().get("n") or 0)

    async def resolve(
        self, *, comment_id: str, status: CommentStatus, resolved_in_n: int | None = None
    ) -> Comment | None:
        """Push an adjudication back, then project the result.

        Written to the server first and projected from its response
        rather than optimistically: the server is the authority on a
        comment's status, and a local copy that moved on its own would
        disagree with what the commenter sees.
        """
        payload: dict[str, Any] = {"status": status.value}
        if resolved_in_n is not None:
            payload["resolved_in_n"] = resolved_in_n
        response = await self._request(
            "PATCH", f"/v1/artifacts/comments/{comment_id}", json=payload
        )
        if response.status_code == 404:
            raise NotAllowed(comment_id)
        response.raise_for_status()
        comment = _to_comment(response.json())
        await self._index.client.project_comments([comment])
        return comment

    # ── Helpers ──────────────────────────────────────────────────────

    async def _local_head(self, ref: str) -> int:
        summary = await self._index.resolve(ref)
        return int(getattr(summary, "head_n", 0) or 0) if summary else 0


def _to_comment(row: dict[str, Any]) -> Comment:
    raw = str(row.get("status") or CommentStatus.OPEN.value)
    try:
        status = CommentStatus(raw)
    except ValueError:
        # A status this build does not know must not disappear. Open
        # puts it in front of a person; anything else hides it.
        logger.warning("unknown comment status %r from server; treating as open", raw)
        status = CommentStatus.OPEN
    return Comment(
        comment_id=str(row.get("id") or ""),
        artifact_id=str(row.get("artifact_id") or ""),
        n=int(row.get("version") or 0),
        thread_id=str(row.get("thread_id") or ""),
        parent_id=row.get("parent_id"),
        quote=str(row.get("quote") or ""),
        quote_start=row.get("quote_start"),
        quote_end=row.get("quote_end"),
        body=str(row.get("body") or ""),
        suggested_text=row.get("suggested_text"),
        status=status,
        resolved_in_n=row.get("resolved_in_n"),
        author=str(row.get("author_email") or ""),
        at=str(row.get("updated_at") or row.get("created_at") or ""),
    )
