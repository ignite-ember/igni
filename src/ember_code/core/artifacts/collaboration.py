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

import asyncio
import contextlib
import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from ember_code.core.artifacts.models import (
    Comment,
    CommentStatus,
    SourceDeclaration,
)

if TYPE_CHECKING:  # pragma: no cover — the index imports this module back
    # Annotation only. Imported for real, it is a cycle: the index
    # writes through this client, and this client is constructed with
    # the index.
    from ember_code.core.artifacts.index import ArtifactIndex

logger = logging.getLogger(__name__)

#: Sync is a background errand, not something a person waits on, but
#: it must not hang a shutdown either.
SYNC_TIMEOUT_SECONDS: float = 30.0

#: Comments per pull. The cursor makes this a page size rather than a
#: cap — a client behind by thousands catches up over several pulls
#: instead of asking for all of them in one request that times out.
COMMENT_PAGE: int = 500

#: Backoff bounds for the live channel, in seconds.
#:
#: The stream ends itself roughly every fifteen minutes, so a normal
#: reconnect is not a failure and must not be punished with a long
#: wait — hence the small floor. The ceiling is what stops a client
#: hammering a server that is down: a laptop that wakes to a dead
#: deployment retries every couple of minutes, not every second.
RECONNECT_MIN_SECONDS: float = 1.0
RECONNECT_MAX_SECONDS: float = 120.0

#: Failures that mean "the connection you were handed was already
#: dead", as opposed to "the server said no" or "the server is down".
#:
#: All three arrive when a pooled keep-alive outlives the server on
#: the other end of it, and which one you get depends on how far the
#: write got before the closed socket was noticed. None of them means
#: the request was processed, which is what makes retrying it safe.
STALE_CONNECTION = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)


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
        #: ``artifact_id`` → the server's slug, for the life of this
        #: object. A document's slug does not change once published,
        #: and the alternative is a listing request per comment.
        self._slugs: dict[str, str] = {}
        #: How often the room wants to hear from us. Taken from the
        #: server's greeting rather than agreed in advance, so the
        #: interval and the expiry it guards against cannot drift
        #: apart across a deployment.
        self._beat_every: float = 15.0
        #: Held while the shared-document list is being fetched, so
        #: the questions asked in parallel on opening a document
        #: share one round trip instead of taking one each.
        self._listing = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(self._server_url and self._access_token)

    # Read-only views for :class:`WaitingStream`, which opens its own
    # long-lived connection rather than borrowing ``_request`` — a
    # stream and a request want opposite timeouts.
    @property
    def server_url(self) -> str:
        return self._server_url

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def http(self) -> httpx.AsyncClient | None:
        return self._client

    # ── Transport ────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One call to the server, with one retry for a dead keep-alive.

        The retry is not a general one. It covers exactly the case
        where the connection was already open and the server has since
        gone — a deployment, a container restart, an idle socket the
        other end reaped. The pool hands out a connection that looks
        usable, the write succeeds into a closed socket, and the read
        fails. Nothing was processed, so sending it again is safe for
        any method; and without this, every screen in the app stays
        broken after a server restart until the backend is restarted
        too, which is not something a reader can be asked to do.

        A connection *refused* is not retried: the server is down
        rather than stale, and a second attempt a microsecond later
        answers the same thing more slowly.
        """
        if not self.is_configured:
            raise RuntimeError("Collaboration needs a signed-in igni account — run `igni login`.")
        client = self._client
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=SYNC_TIMEOUT_SECONDS)
        try:
            for attempt in (1, 2):
                try:
                    return await client.request(
                        method,
                        f"{self._server_url}{path}",
                        headers={"Authorization": f"Bearer {self._access_token}"},
                        **kwargs,
                    )
                except STALE_CONNECTION as exc:
                    if attempt == 2:
                        raise
                    logger.debug("retrying %s %s after a dead connection: %r", method, path, exc)
            raise AssertionError("unreachable")  # pragma: no cover
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
            # Mirroring, not authoring. Without this the write path
            # pushes each version straight back at the server it came
            # from, carrying a base this machine has not reached — so
            # a pull of a two-version chain refused itself with
            # "written from v0" and adopted nothing.
            projecting=True,
        )

    async def first_version(self, *, slug: str) -> RemoteVersion:
        """The opening version of a chain, for a reader who has none.

        Fetched on its own so a document can be created locally before
        :meth:`pull` runs, which needs somewhere to append to. Asking
        for ``after=0`` and taking the first row rather than the head:
        the chain is applied in order from the beginning, and starting
        at the head would leave a local v1 holding the latest text
        with every earlier version appended on top of it.
        """
        response = await self._request(
            "GET", f"/v1/artifacts/shared/{slug}/versions", params={"after": 0}
        )
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        rows = response.json().get("versions") or []
        if not rows:
            raise RuntimeError("that document has no versions yet")
        row = rows[0]
        return RemoteVersion(
            n=int(row.get("n") or 0),
            base_n=int(row.get("base_n") or 0),
            content=str(row.get("content") or ""),
            sources=dict(row.get("sources") or {}),
            author_email=str(row.get("author_email") or ""),
            created_at=str(row.get("created_at") or ""),
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

    # ── Who is in the room ───────────────────────────────────────────
    #
    # The one long-lived connection in this class. Everything else here
    # is a request and an answer; this stays open for as long as a
    # document is on screen, because what it carries — who else has it
    # open — is only ever true right now.
    #
    # **Held here rather than in the browser.** The page could open
    # this socket itself, and then the cloud token would have to be in
    # the browser to do it. It is not: every cloud call in this product
    # goes through this process, which is where the credentials are, so
    # the roster comes back the same way and the token stays put.

    async def room(
        self,
        *,
        slug: str,
        on_roster: Callable[[list[dict[str, Any]]], Any],
        stop: asyncio.Event,
        on_frame: Callable[[dict[str, Any]], Any] | None = None,
        outbox: asyncio.Queue[dict[str, Any]] | None = None,
    ) -> None:
        """Hold a place in a document until asked to leave.

        ``on_frame`` receives everything that is not a roster — the
        draft as it stands on joining, each change somebody else
        types, and the server asking for a snapshot. Nothing here
        reads them: the changes are opaque, which is the whole design.
        The server relays and stores blobs and never merges them, so
        the only thing that can turn a pile of changes into a document
        is the editor holding it, and that is in the browser.

        ``outbox`` is how changes go the other way. A queue rather
        than a method, because the socket is owned by this coroutine
        and reconnects underneath: anything that wrote to the socket
        directly would be writing to whichever one existed when it
        looked, which during a reconnect is none.

        Reconnects on its own, with a backoff, because a presence
        connection that gives up after one dropped network is a feature
        that works until the first time somebody closes their laptop
        lid. Each attempt re-announces arrival, which is also how a
        reader reappears to everybody else.

        Returns only when ``stop`` is set. Every failure is a reason to
        wait and try again, not a reason to return — there is nothing a
        caller could do with "the presence socket died" except
        reconnect, so it is done here.
        """
        url = self._room_url(slug)
        if not url:
            return
        backoff = 1.0
        while not stop.is_set():
            try:
                await self._attend(
                    url, on_roster=on_roster, stop=stop, on_frame=on_frame, outbox=outbox
                )
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — network, auth, server restart
                logger.debug("presence for %s dropped: %s", slug, exc)
            if stop.is_set():
                return
            # Capped, and the cap is low. This is a reconnect to a
            # server the user is actively using, not a background sync
            # — a minute of backoff would mean a minute of looking
            # absent to colleagues after a two-second blip.
            await self._wait(stop, backoff)
            backoff = min(backoff * 2, 15.0)

    async def _attend(
        self,
        url: str,
        *,
        on_roster: Callable[[list[dict[str, Any]]], Any],
        stop: asyncio.Event,
        on_frame: Callable[[dict[str, Any]], Any] | None = None,
        outbox: asyncio.Queue[dict[str, Any]] | None = None,
    ) -> None:
        """One connection, from greeting to close."""
        import websockets

        async with websockets.connect(url, max_size=2**20) as socket:
            await socket.send(json.dumps({"type": "auth", "token": self._access_token}))
            beat = asyncio.create_task(self._heartbeat(socket, stop))
            sending = asyncio.create_task(self._send_from(socket, outbox, stop))
            closing = asyncio.create_task(stop.wait())
            try:
                while True:
                    reading = asyncio.create_task(socket.recv())
                    done, _ = await asyncio.wait(
                        {reading, closing}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if closing in done:
                        reading.cancel()
                        return
                    message = json.loads(reading.result())
                    kind = message.get("type")
                    if kind == "roster":
                        on_roster(list(message.get("here") or []))
                    elif kind == "hello":
                        self._beat_every = float(message.get("heartbeat") or 15)
                        if on_frame is not None:
                            on_frame(message)
                    elif on_frame is not None:
                        on_frame(message)
            finally:
                for task in (beat, sending, closing):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    @staticmethod
    async def _send_from(
        socket: Any, outbox: asyncio.Queue[dict[str, Any]] | None, stop: asyncio.Event
    ) -> None:
        """Put whatever is queued onto this connection.

        A frame that cannot be sent is dropped rather than retried.
        The socket is gone, which means a reconnect is coming, and a
        reconnect re-reads the whole draft from the server — so a
        keystroke held here to be re-sent afterwards would arrive
        *after* the state that already contains it, out of order and
        for no gain.
        """
        if outbox is None:
            return
        while not stop.is_set():
            frame = await outbox.get()
            try:
                await socket.send(json.dumps(frame))
            except Exception as exc:  # noqa: BLE001 — the socket died
                logger.debug("a room frame did not get out: %s", exc)
                return

    async def _heartbeat(self, socket: Any, stop: asyncio.Event) -> None:
        """Say we are still here, until we are not.

        The server expires a presence entry that stops being refreshed,
        which is what clears out readers whose connection died without
        anybody being told. That backstop only works if a live
        connection keeps saying so.
        """
        while not stop.is_set():
            await self._wait(stop, self._beat_every)
            if stop.is_set():
                return
            with contextlib.suppress(Exception):
                await socket.send(json.dumps({"type": "beat"}))

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        """Sleep, unless asked to stop first."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    def _room_url(self, slug: str) -> str:
        """The socket address for a document's room.

        Derived from the configured server rather than configured
        separately: two settings that have to agree about which server
        this is would eventually disagree, and the failure would be a
        presence feature quietly pointing at the wrong deployment.
        """
        base = (self._server_url or "").rstrip("/")
        if not base:
            return ""
        scheme = "wss" if base.startswith("https://") else "ws"
        host = base.split("://", 1)[-1]
        return f"{scheme}://{host}/v1/artifacts/shared/{slug}/room"

    # ── Naming somebody ──────────────────────────────────────────────
    #
    # Both halves of "@" come from the server and neither is cached.
    # Who your colleagues are is not something this machine knows, and
    # whether one of them can read a document is a question only the
    # thing holding the document can answer — a local copy of either
    # would be wrong exactly when it mattered, which is the moment
    # somebody was just given access.

    async def mentionable(self, *, slug: str, query: str = "") -> dict[str, Any]:
        """Who could be named here, and whether they could read it.

        Returned as the server sent it. This is a completion list on
        its way to a popup, and every field in it is drawn — parsing it
        into a local shape here would mean a second definition of the
        same thing on the way to the same place.
        """
        response = await self._request(
            "GET", f"/v1/artifacts/shared/{slug}/mentions", params={"q": query}
        )
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        return dict(response.json())

    async def roster(self, *, slug: str) -> dict[str, Any]:
        """Who a document is shared with.

        Returned as the server sent it, like :meth:`mentionable` and
        for the same reason: every field is on its way to being drawn,
        and re-shaping it here would be a second definition of the
        same thing between the server and the screen.
        """
        response = await self._request("GET", f"/v1/artifacts/shared/{slug}/people")
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        return dict(response.json())

    async def ask_access(self, *, slug: str, identity: str = "", note: str = "") -> dict[str, Any]:
        """Ask to be let into a document — for yourself, or a colleague.

        ``identity`` empty means the account this client is signed in
        as. Naming somebody else is what a mention of a colleague
        without access turns into, and the server refuses it from
        anybody who cannot read the document themselves.
        """
        payload: dict[str, Any] = {}
        if identity:
            payload["identity"] = identity
        if note:
            payload["note"] = note
        response = await self._request("POST", f"/v1/artifacts/shared/{slug}/access", json=payload)
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        return dict(response.json())

    async def locked(self, *, slug: str) -> dict[str, Any]:
        """What a document is, for somebody who cannot read it."""
        response = await self._request("GET", f"/v1/artifacts/shared/{slug}/access")
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        return dict(response.json())

    async def waiting_on(self, *, slug: str) -> list[dict[str, Any]]:
        """Who is asking to be let in. Owner only."""
        response = await self._request("GET", f"/v1/artifacts/shared/{slug}/access-requests")
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        return list(response.json().get("asks") or [])

    async def settle_ask(self, *, slug: str, request_id: str, granted: bool) -> None:
        """Let them in, or do not. Owner only."""
        response = await self._request(
            "POST",
            f"/v1/artifacts/shared/{slug}/access-requests/{request_id}",
            json={"granted": granted},
        )
        if response.status_code == 404:
            raise NotAllowed(request_id)
        response.raise_for_status()

    # ── The draft ────────────────────────────────────────────────────
    #
    # Where an edit lands. A version is what publishing a draft makes,
    # and nothing else makes one — so these three are the whole of
    # editing a shared document.

    async def draft(self, *, slug: str) -> dict[str, Any]:
        """What is being written on this document right now."""
        response = await self._request("GET", f"/v1/artifacts/shared/{slug}/draft")
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        return dict(response.json())

    async def save_draft(self, *, slug: str, content: str, base_n: int) -> None:
        """Store the edit in progress. Never makes a version."""
        response = await self._request(
            "PUT",
            f"/v1/artifacts/shared/{slug}/draft",
            json={"content": content, "base_n": base_n},
        )
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()

    async def publish_draft(self, *, slug: str) -> int:
        """Turn the draft into a version, and answer which one.

        409 is a conflict rather than a failure: somebody published
        while this was being written, the draft is still there, and
        the caller needs to say so rather than retry.
        """
        response = await self._request("POST", f"/v1/artifacts/shared/{slug}/draft/publish")
        if response.status_code == 404:
            raise NotAllowed(slug)
        if response.status_code == 409:
            raise ChainDiverged(0, [])
        response.raise_for_status()
        return int(response.json().get("n") or 0)

    # ── What is waiting on you ───────────────────────────────────────
    #
    # Read from the server rather than projected into the graph, and
    # deliberately unlike comments. A comment is part of a document's
    # history and belongs in the store beside it; a notification is a
    # message to one person about a moment, true when it was written
    # and never again — there is nothing for a local copy to be
    # consistent with, and a queue that answered from a stale mirror
    # would say somebody is waiting after they have been dealt with.

    async def waiting(self, *, unread_only: bool = True) -> dict[str, Any]:
        """Everything waiting on this account, newest first."""
        response = await self._request(
            "GET", "/v1/notifications", params={"unread_only": unread_only}
        )
        response.raise_for_status()
        return dict(response.json())

    async def mark_seen(self, *, notification_id: str = "") -> dict[str, Any]:
        """Say one has been read, or all of them."""
        params = {"notification_id": notification_id} if notification_id else None
        response = await self._request("POST", "/v1/notifications/read", params=params)
        response.raise_for_status()
        return dict(response.json())

    async def colleagues(self, *, query: str = "") -> list[dict[str, Any]]:
        """Who this account could share something with.

        No document in the question, unlike :meth:`mentionable`. That
        one needs a slug, and a document being shared for the first
        time does not have one — which is exactly when a list of
        colleagues is wanted.
        """
        response = await self._request("GET", "/v1/colleagues", params={"q": query})
        response.raise_for_status()
        return list(response.json().get("people") or [])

    async def grant_view(self, *, slug: str, identity: str) -> None:
        """Let one more person read a document. Owner only.

        ``NotAllowed`` covers both "no such document" and "not yours",
        which the server deliberately does not tell apart — and the
        caller should not either, because the honest message for both
        is the same: this is not yours to widen.
        """
        response = await self._request(
            "POST", f"/v1/artifacts/shared/{slug}/audience", json={"identity": identity}
        )
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()

    # ── Filing ───────────────────────────────────────────────────────
    #
    # The tree is authored state: nobody can work out from a document
    # that it was dragged into "Platform". So it is written to the
    # server like a version is, and the graph mirrors it — with the
    # server's own ids, or the two copies disagree about what a folder
    # is called and every later operation addresses the wrong one.

    async def create_folder(self, *, name: str, parent_id: str = "", folder_id: str = "") -> str:
        """Make a folder on the server. Answer the id it chose.

        ``folder_id`` proposes one, for a folder this machine already
        has — made while it was signed out, so its id came first. The
        server keeps it when it can and mints its own when it cannot,
        which is why the answer is the id to use rather than the id
        that was asked for.
        """
        payload: dict[str, Any] = {"name": name}
        if parent_id:
            payload["parent_id"] = parent_id
        if folder_id:
            payload["folder_id"] = folder_id
        response = await self._request("POST", "/v1/artifacts/shared/folders", json=payload)
        if response.status_code == 404:
            raise NotAllowed(parent_id)
        response.raise_for_status()
        return str(response.json().get("id") or "")

    async def update_folder(
        self,
        *,
        folder_id: str,
        name: str | None = None,
        parent_id: str | None = None,
        to_top: bool = False,
        archived: bool | None = None,
    ) -> None:
        """Rename, move or archive one. ``to_top`` says which null.

        ``parent_id=None`` means "leave the parent alone", because
        every field here is optional. Moving something to top level is
        also setting its parent to null, and the two collide on the
        one field where it matters — so the move is said explicitly.
        """
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if to_top:
            payload["to_top"] = True
        elif parent_id is not None:
            payload["parent_id"] = parent_id
        if archived is not None:
            payload["archived"] = archived
        if not payload:
            return
        response = await self._request(
            "PATCH", f"/v1/artifacts/shared/folders/{folder_id}", json=payload
        )
        if response.status_code == 404:
            raise NotAllowed(folder_id)
        response.raise_for_status()

    async def delete_folder(self, *, folder_id: str) -> None:
        """Remove it. What was inside comes up a level, server-side."""
        response = await self._request("DELETE", f"/v1/artifacts/shared/folders/{folder_id}")
        if response.status_code == 404:
            raise NotAllowed(folder_id)
        response.raise_for_status()

    async def place_document(
        self,
        *,
        slug: str,
        parent_id: str | None = None,
        to_top: bool = False,
        archived: bool | None = None,
    ) -> None:
        """File a document, or put it away."""
        payload: dict[str, Any] = {}
        if to_top:
            payload["to_top"] = True
        elif parent_id is not None:
            payload["parent_id"] = parent_id
        if archived is not None:
            payload["archived"] = archived
        if not payload:
            return
        response = await self._request("PATCH", f"/v1/artifacts/shared/{slug}/place", json=payload)
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()

    async def search(self, *, query: str, limit: int = 5) -> list[dict[str, Any]] | None:
        """Ask the server for passages matching ``query``.

        Returns the raw hits, or ``None`` when this deployment has no
        search configured — which is a different answer from an empty
        list, and the caller says so differently. A deployment can run
        without a graph or without an embedder on purpose.

        **The server ranks, and the server decides what may be
        ranked.** This used to happen here: the client embedded the
        query with its own copy of the model and queried a local
        Neo4j, which meant the result depended on what each developer
        had indexed locally, and that a document shared with somebody
        was searchable only by whoever had happened to pull it. The
        access rule lives next to the documents now, where it already
        was for every other read.
        """
        response = await self._request(
            "GET",
            "/v1/artifacts/shared/search",
            params={"q": query, "limit": max(1, limit)},
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("available", True):
            return None
        return list(body.get("hits") or [])

    async def lineage(self, *, slug: str, n: int = 0) -> dict[str, Any] | None:
        """What this version was built on, and what was built on it.

        The server answers because the server owns the edges. It used
        to be the local graph, which made lineage a property of
        whichever laptop asked: a source one person confirmed was
        invisible to everybody else.

        The reply has a ``hidden`` count. Edges whose other end this
        reader may not open are removed there — rendering a title
        somebody cannot open discloses it — and the count is what
        keeps "built on 3 documents, 1 you cannot see" honest rather
        than silently short.
        """
        response = await self._request(
            "GET", f"/v1/artifacts/shared/{slug}/lineage", params={"n": max(0, n)}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    async def stale(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Documents of mine standing on something that has moved.

        One person's queue. The local version could only see what this
        machine had pulled, so a document going stale elsewhere was
        invisible until somebody happened to sync it.
        """
        response = await self._request("GET", "/v1/artifacts/stale", params={"limit": max(1, limit)})
        response.raise_for_status()
        return list(response.json().get("rows") or [])

    async def decide_edge(self, *, edge_id: str, state: str) -> dict[str, Any] | None:
        """Confirm or dismiss one proposed source."""
        response = await self._request(
            "POST", f"/v1/artifacts/shared/edges/{edge_id}/decide", json={"state": state}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    async def repin_edge(self, *, edge_id: str, to_n: int) -> dict[str, Any] | None:
        """Move a confirmed source forward, having read the change."""
        response = await self._request(
            "POST", f"/v1/artifacts/shared/edges/{edge_id}/repin", json={"to_n": max(0, to_n)}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    async def edge_evidence(self, *, edge_id: str) -> dict[str, Any] | None:
        """The passages a write says it drew on, if any were cited."""
        response = await self._request("GET", f"/v1/artifacts/shared/edges/{edge_id}/evidence")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    async def ensure_shared(
        self, *, artifact_id: str, name: str, kind: str, n: int, html: str, author: str = ""
    ) -> str:
        """The document's row on the server, making one if there is none.

        Published **once**, not on every write. The publish endpoint
        upserts on ``(owner, artifact_id)`` and overwrites
        ``visibility`` as it goes, so calling it per version would
        quietly return a document its author had shared with
        colleagues to private the next time they edited it. Keeping
        the snapshot current is :meth:`push_content`'s job, which
        carries ``html`` and leaves the sharing alone.

        Private at birth. A document exists on the server because it
        was written, which is not the same as being shared with
        anybody — that stays a deliberate, separate act.
        """
        slug = await self.slug_for(artifact_id)
        if slug:
            return slug
        response = await self._request(
            "POST",
            "/v1/artifacts/shared",
            json={
                "artifact_id": artifact_id,
                "title": name,
                "kind": kind or "note",
                "version": max(1, n),
                "html": html,
                "author": author,
                "visibility": "private",
                "audience": [],
            },
        )
        response.raise_for_status()
        slug = str(response.json().get("slug") or "")
        if slug:
            self._slugs[artifact_id] = slug
        return slug

    async def push_content(
        self,
        *,
        slug: str,
        content: str,
        base_n: int,
        pins: list[Any] | None = None,
        html: str | None = None,
    ) -> int:
        """Append a version from the text in hand.

        :meth:`push` reads the local head and mirrors it upward, which
        assumes the version already exists here. This one takes the
        content directly, so the server can be written *first* and the
        local graph can be what mirrors it.

        Raises :class:`ChainDiverged` when ``base_n`` is no longer the
        server's head, carrying the versions to rebase onto.
        """
        payload: dict[str, Any] = {
            "content": content,
            "base_n": base_n,
            # Candidates travel too, which they did not when edges
            # lived in each collaborator's local graph: sending one
            # then would have planted an unadjudicated edge in
            # somebody else's store that they never saw the evidence
            # for. The server is the review surface now, so a
            # candidate kept here is one nobody can adjudicate — the
            # queue would be empty on every machine but the one that
            # happened to do the reading.
            #
            # ``passages`` is the part worth sending: the exact
            # paragraphs this write drew on, so the claim can be
            # checked by rendering them rather than taken on trust.
            # A pin without them still records the document, which is
            # the weaker, unverifiable form of the same statement.
            "sources": {
                "pins": [
                    {
                        "artifact_id": p.artifact_id,
                        "n": p.n,
                        "state": getattr(p.state, "value", str(p.state)),
                        "passages": [
                            {"chunk_id": c.chunk_id, "quote": c.quote}
                            for c in getattr(p, "passages", [])
                        ],
                    }
                    for p in (pins or [])
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

    async def slug_for(self, artifact_id: str) -> str:
        """The server's name for a document this account has published.

        Resolved rather than remembered. The server already holds the
        client-side ``artifact_id`` on every shared row, under a
        unique constraint per owner, so it can answer this — and a
        slug cached in the local graph is a second copy of somebody
        else's primary key, wrong from the moment a document is
        re-published from another machine.

        Empty when this account has not published the document. That
        is a state, not a failure: it is how the caller knows there is
        no shared document to write to yet.

        **One listing at a time.** Opening a document asks three
        separate questions at once — is it locked, who is it shared
        with, what is in its draft — and every one of them starts
        here. On a cold cache each was fetching the whole list of
        shared documents, so the answer to "can I edit this" waited
        on three identical round trips queued behind each other.
        They share one now, which is most of the second it used to
        take to open an editor.
        """
        if artifact_id in self._slugs:
            return self._slugs[artifact_id]
        async with self._listing:
            # Checked again inside the lock: whoever held it was
            # almost certainly fetching the very list this needed.
            if artifact_id in self._slugs:
                return self._slugs[artifact_id]
            response = await self._request("GET", "/v1/artifacts/shared")
            response.raise_for_status()
            rows = response.json().get("items") or []
            for row in rows:
                known = str(row.get("artifact_id") or "")
                if known:
                    self._slugs[known] = str(row.get("slug") or "")
            # Remembered as "not shared" so a document nobody has
            # published does not re-list on every question asked
            # about it. ``forget_slugs`` is what makes sharing one
            # visible.
            self._slugs.setdefault(artifact_id, "")
        return self._slugs.get(artifact_id, "")

    def forget_slugs(self) -> None:
        """Ask the server again next time.

        Called after publishing a document for the first time: until
        then this cache holds the true answer that it had no shared
        copy, and that answer has just stopped being true.
        """
        self._slugs.clear()

    async def comment(
        self,
        *,
        slug: str,
        version: int,
        body: str,
        quote: str = "",
        quote_start: int | None = None,
        quote_end: int | None = None,
        parent_id: str | None = None,
    ) -> Comment | None:
        """Open a thread on a shared document, then project the result.

        The counterpart to :meth:`reply`, which can key on a comment
        id because the thread it joins already records the document.
        A first comment has no thread to name, so this one is
        addressed by slug.

        Projected from the server's response for the same reason the
        others are: the server decides what a comment's id, thread and
        timestamps are, and a local copy that invented its own would
        disagree with what everybody else is reading.
        """
        payload: dict[str, Any] = {"version": version, "body": body, "quote": quote}
        if quote_start is not None and quote_start >= 0:
            payload["quote_start"] = quote_start
        if quote_end is not None and quote_end >= 0:
            payload["quote_end"] = quote_end
        if parent_id:
            payload["parent_id"] = parent_id
        response = await self._request(
            "POST", f"/v1/artifacts/shared/{slug}/comments", json=payload
        )
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        comment = _to_comment(response.json())
        await self._index.client.project_comments([comment])
        return comment

    async def comments_on(self, *, slug: str) -> list[Comment]:
        """Every comment the server has on a shared document.

        Read from the server rather than from the graph: the graph
        holds what this machine has pulled, and somebody else's
        comment written a second ago is not in it yet.
        """
        response = await self._request("GET", f"/v1/artifacts/shared/{slug}/comments")
        if response.status_code == 404:
            raise NotAllowed(slug)
        response.raise_for_status()
        rows = response.json().get("comments") or []
        comments = [_to_comment(row) for row in rows]
        if comments:
            await self._index.client.project_comments(comments)
        return comments

    async def reply(
        self, *, comment_id: str, body: str, suggested_text: str | None = None
    ) -> Comment | None:
        """Answer a comment in its thread, then project the result.

        Keyed on the comment rather than on a slug, because this
        client holds no slug: :meth:`push` and :meth:`pull` are the
        only calls that take one and nothing calls them. The thread
        already records the document, the version and the passage.

        Projected from the server's response for the same reason
        :meth:`resolve` is — the server is the authority on what a
        thread contains, and a local copy that moved on its own would
        disagree with what the other participants see.
        """
        payload: dict[str, Any] = {"body": body}
        if suggested_text is not None:
            payload["suggested_text"] = suggested_text
        response = await self._request(
            "POST", f"/v1/artifacts/comments/{comment_id}/replies", json=payload
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


class WaitingStream:
    """Follow the server's live channel, reading when it says to.

    Renamed from ``CommentStream`` with the thing it follows: the
    channel now carries every kind of event that concerns somebody —
    a comment, a reply, being named, being asked for access, being
    given a document — and a class called after one of them would
    have read as though the others were an afterthought.

    **The stream is a latency optimisation, not a delivery
    mechanism.** It carries a cursor and never a comment, and the only
    thing it does is bring forward a pull that would have happened
    anyway. Correctness lives in :meth:`CollaborationSync.pull_comments`
    — ordered on ``updated_at``, projected with a MERGE on the comment
    id, skipping rows whose version is not here yet. A client that
    misses every notification for a week pulls once and is exactly as
    correct as one that received them all.

    That is what licenses everything below to be casual about
    failure. A dropped connection reconnects. A reconnect that fails
    backs off. A cursor that goes missing means a pull from the
    beginning, which is idempotent. None of it can lose a comment,
    so none of it needs to be careful.

    The cursor is sent as ``Last-Event-ID`` on reconnect, which is the
    same mechanism a browser's ``EventSource`` uses, arriving at the
    same server parameter as ``since``.
    """

    def __init__(self, sync: CollaborationSync) -> None:
        self._sync = sync
        self._cursor: str | None = None
        self._stop = asyncio.Event()

    @property
    def cursor(self) -> str | None:
        return self._cursor

    def stop(self) -> None:
        self._stop.set()

    async def run(self, *, on_change: Callable[[int], Any] | None = None) -> None:
        """Follow the stream until :meth:`stop`, pulling as told.

        ``on_change`` is called with the number of comments projected,
        whenever that is non-zero — enough for a badge or a refresh
        without this class knowing what a screen is.
        """
        delay = RECONNECT_MIN_SECONDS
        while not self._stop.is_set():
            try:
                await self.follow_once(on_change)
                # A clean end is the server asking for a reconnect,
                # which it does on a timer. Reset the backoff: this is
                # the expected path, not a failure.
                delay = RECONNECT_MIN_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("comment stream dropped (%s); retrying in %.0fs", exc, delay)
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # stop() was called during the wait
            except asyncio.TimeoutError:
                continue

    async def follow_once(self, on_change: Callable[[int], Any] | None = None) -> None:
        """One connection: follow it until the server ends it.

        Public because it is the useful unit on its own — "connect,
        catch up, come back" is what a foreground *sync now* wants,
        and it is the piece worth testing directly. :meth:`run` is
        this in a loop with backoff.
        """
        if not self._sync.is_configured:
            raise RuntimeError("Collaboration needs a signed-in igni account — run `igni login`.")
        headers = {"Authorization": f"Bearer {self._sync.access_token}"}
        if self._cursor:
            headers["Last-Event-ID"] = self._cursor

        client = self._sync.http
        owns = client is None
        if client is None:
            # No read timeout: an idle stream is the normal state, and
            # the server's keepalives are what prove it is alive.
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
            )
        try:
            async with client.stream(
                "GET", f"{self._sync.server_url}/v1/notifications/stream", headers=headers
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if self._stop.is_set():
                        return
                    # Keepalives are SSE comments and carry nothing.
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("id:"):
                        self._cursor = line[3:].strip() or self._cursor
                        continue
                    if line.startswith("event:") and line[6:].strip() == "reconnect":
                        return
                    if not line.startswith("data:"):
                        continue
                    projected, cursor = await self._sync.pull_comments(cursor=self._cursor)
                    if cursor:
                        self._cursor = cursor
                    # Every nudge, not only the ones that brought
                    # comments with them. The guard used to be ``if
                    # projected``, from when this followed the comment
                    # channel and a nudge that projected nothing had
                    # genuinely nothing to say. The channel now carries
                    # somebody asking for access, somebody being given
                    # a document, somebody publishing a version — none
                    # of which are comments, all of which projected
                    # zero and were therefore silently swallowed one
                    # line before reaching the screen.
                    if on_change is not None:
                        result = on_change(projected)
                        if inspect.isawaitable(result):
                            await result
        finally:
            if owns:
                await client.aclose()
