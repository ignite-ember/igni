"""Where an edit goes before it is a version.

One place, because there are three ways to edit a document and they
must not disagree about where the words land: somebody typing in the
editor, the agent answering a comment that asked for a change, and
whatever writes through the tools next year.

Two homes, one rule. A shared document keeps its draft on the server,
so everybody who can read the document sees what is being written
into it. A document nobody has shared keeps it in a file beside the
store. Which one is not a setting — it is whether the document has a
shared copy, which is already a fact about it.

**Publishing is the only thing that makes a version**, and it is the
only thing that clears the draft. Every refusal on the way leaves
both where they were, because a draft that is thrown away on a failed
append is the one copy of what somebody wrote.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ember_code.core.artifacts.drafts import LocalDrafts
from ember_code.core.artifacts.models import SourceDeclaration
from ember_code.core.paths import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)


class DraftRefused(Exception):
    """The draft could not be written or published, and why."""


class PublishedUnderneath(DraftRefused):
    """Somebody made a version while this draft was being written.

    Its own class because it is the one refusal that is not a
    mistake: the draft is fine, it simply describes a document that
    has moved, and the answer is to read the new version rather than
    to try again.
    """


@dataclass(frozen=True)
class DraftView:
    """A draft, however it is stored."""

    exists: bool = False
    content: str = ""
    #: The version it was written from.
    base_n: int = 0
    #: The version the document is on now.
    head_n: int = 0
    #: Who touched it last, where that is known. Only shared drafts
    #: know: a local one has nobody else to have written it.
    updated_by: str = ""


class DraftDesk:
    """Reading, writing and publishing the draft of one store.

    Args:
        index: the artifact index these documents live in.
        sync: the collaboration client to ask about shared copies.
            Defaults to the one attached to the index. The backend
            passes its own, which is rebuilt when the signed-in
            account changes — the index's is built once, and a client
            holding the previous account's token would answer with
            their documents.
        author: recorded on versions this makes.
    """

    def __init__(self, index: Any, *, sync: Any = None, author: str = "") -> None:
        self._index = index
        self._sync = sync if sync is not None else getattr(index, "shared", None)
        self._author = author
        self._local = LocalDrafts(
            data_dir=getattr(index, "data_dir", DEFAULT_DATA_DIR),
            project_id=str(getattr(index, "project_id", "") or "default"),
        )

    # ── Which copy ───────────────────────────────────────────────────

    async def _slug(self, artifact_id: str) -> str:
        """The shared copy's name, or nothing.

        Nothing means *local only*, which is the ordinary state — most
        documents are written before anybody decides somebody else
        should read them. It is also what an unreachable server looks
        like, and the two are answered the same way: the draft goes to
        the file, and the words are kept. Refusing to save because a
        network call failed would lose the only thing worth keeping.
        """
        if self._sync is None or not getattr(self._sync, "is_configured", False):
            return ""
        try:
            return str(await self._sync.slug_for(artifact_id) or "")
        except Exception as exc:  # noqa: BLE001 — network, auth
            logger.info("could not ask whether %s is shared: %s", artifact_id, exc)
            return ""

    async def _head_n(self, artifact_id: str) -> int:
        """Which version this document is on."""
        try:
            found = await self._index.resolve(artifact_id)
        except Exception as exc:  # noqa: BLE001 — store
            logger.info("could not read the head of %s: %s", artifact_id, exc)
            return 0
        return int(getattr(found, "head_n", 0) or 0) if found is not None else 0

    # ── The three acts ───────────────────────────────────────────────

    async def read(self, artifact_id: str) -> DraftView:
        """What is being written on this document, if anything."""
        slug = await self._slug(artifact_id)
        if not slug:
            kept = self._local.read(artifact_id=artifact_id)
            head = await self._head_n(artifact_id)
            if kept is None:
                return DraftView(head_n=head)
            return DraftView(exists=True, content=kept.content, base_n=kept.base_n, head_n=head)
        answer = await self._sync.draft(slug=slug)
        return DraftView(
            exists=bool(answer.get("exists")),
            content=str(answer.get("content") or ""),
            base_n=int(answer.get("base_n") or 0),
            head_n=int(answer.get("head_n") or 0),
            updated_by=str(answer.get("updated_by") or ""),
        )

    async def save(self, artifact_id: str, content: str) -> None:
        """Keep an edit. Never makes a version.

        The base is read here rather than passed in. A draft is
        written from whatever the head is *now*, and a caller that
        remembered a base from when it opened the editor would write
        one that was already stale — which is a conflict at publish
        time over a version nobody actually disagreed with.
        """
        slug = await self._slug(artifact_id)
        if not slug:
            self._local.write(
                artifact_id=artifact_id,
                content=content,
                base_n=await self._head_n(artifact_id),
            )
            return
        head = int((await self._sync.draft(slug=slug)).get("head_n") or 0)
        await self._sync.save_draft(slug=slug, content=content, base_n=head)

    async def publish(self, artifact_id: str) -> int:
        """Turn the draft into a version, and answer which one.

        For a shared document the server appends first and this
        machine mirrors what it accepted — the same order every other
        write takes here, because a version that exists on one machine
        and nowhere else is indistinguishable from one that was
        saved.
        """
        slug = await self._slug(artifact_id)
        if not slug:
            return await self._publish_local(artifact_id)

        draft = await self._sync.draft(slug=slug)
        if not draft.get("exists"):
            raise DraftRefused("There is nothing to publish.")
        from ember_code.core.artifacts.collaboration import (  # noqa: PLC0415 — cycle
            ChainDiverged,
        )

        try:
            n = int(await self._sync.publish_draft(slug=slug))
        except ChainDiverged as diverged:
            raise PublishedUnderneath(
                "Somebody published while this was being written. Your draft is still "
                "here — read the new version and publish again."
            ) from diverged

        # Mirrored, not pushed: the server has it already, and sending
        # it back would carry a base this machine has not reached.
        try:
            await self._index.revise(
                ref=artifact_id,
                content=str(draft.get("content") or ""),
                sources=SourceDeclaration(),
                author=self._author,
                projecting=True,
            )
        except Exception as exc:  # noqa: BLE001 — store
            # The version exists where it counts. A local mirror that
            # failed is a stale copy, which the next sync corrects;
            # reporting the publish as failed would invite a second
            # one and make two versions of the same words.
            logger.info("published v%s but could not mirror it locally: %s", n, exc)
        return n

    async def _publish_local(self, artifact_id: str) -> int:
        """Publish the draft of a document nobody has shared."""
        kept = self._local.read(artifact_id=artifact_id)
        if kept is None:
            raise DraftRefused("There is nothing to publish.")
        result = await self._index.revise(
            ref=artifact_id,
            content=kept.content,
            sources=SourceDeclaration(),
            author=self._author,
        )
        if not getattr(result, "success", False):
            # Kept. The refusal it most often meets is the document
            # being archived, which somebody can undo and then want
            # their draft back.
            raise DraftRefused(getattr(result, "error", "") or "Could not publish that.")
        self._local.clear(artifact_id=artifact_id)
        return int(getattr(result, "n", 0) or 0)
