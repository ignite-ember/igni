"""Export and share an artifact.

Exporting and sharing are the same projection run twice — once to a
file on disk, once to the server. Keeping them in one class is what
guarantees a shared page can't contain more than a local export does,
which would otherwise be exactly the bug you don't find until it has
already left.

What the projection keeps and drops:

==========================================  ==========================
Kept                                        Dropped
==========================================  ==========================
Rendered content of the chosen version      Chunks and embeddings
Artifact name, kind, version number         ``session_id`` on versions
Confirmed derivations *within* the set      Candidate derivations
External citations                          Edges leaving the set
``created_at``, author display name         ``project_hash``, origin
==========================================  ==========================

The row that matters most is the last one on the right. An edge to an
artifact that was not shared is removed, not rendered as a dangling
reference — a dangling reference leaks the name and the existence of
private work to whoever opens the page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.render import (
    ArtifactRenderer,
    ExportScope,
    RenderRequest,
)

logger = logging.getLogger(__name__)

PUBLISH_TIMEOUT_SECONDS: float = 30.0


@dataclass
class ExportBundle:
    """One rendered artifact, ready to write or upload."""

    artifact_id: str
    title: str
    kind: str
    n: int
    html: str
    exported_at: str
    author: str = ""

    def to_payload(self, *, visibility: str, audience: list[str]) -> dict[str, object]:
        """The server-facing shape.

        Deliberately hand-built rather than dumped from a model: the
        set of fields that may cross this boundary is the privacy
        contract, and a serializer that picks up a new field
        automatically is a serializer that leaks one.
        """
        return {
            "artifact_id": self.artifact_id,
            "title": self.title,
            "kind": self.kind,
            "version": self.n,
            "html": self.html,
            "exported_at": self.exported_at,
            "author": self.author,
            "visibility": visibility,
            "audience": audience,
        }


class ArtifactExporter:
    """Renders artifacts into self-contained pages."""

    def __init__(
        self,
        artifacts: ArtifactIndex,
        *,
        renderer: ArtifactRenderer | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._renderer = renderer or ArtifactRenderer()

    async def bundle(
        self,
        ref: str,
        *,
        version: int = 0,
        scope: ExportScope | None = None,
    ) -> ExportBundle | None:
        """Render one artifact at one version."""
        summary = await self._artifacts.resolve(ref)
        if summary is None:
            return None
        target = await self._artifacts.version(summary.artifact_id, version)
        if target is None:
            return None
        lineage = await self._artifacts.lineage(summary.artifact_id, target.n)
        history = await self._artifacts.versions(summary.artifact_id)
        request = RenderRequest(
            summary=summary,
            version=target,
            lineage=lineage,
            history=history,
            scope=scope or ExportScope.local(),
        )
        return ExportBundle(
            artifact_id=summary.artifact_id,
            title=summary.name or summary.artifact_id,
            kind=summary.kind,
            n=target.n,
            html=self._renderer.render(request),
            exported_at=target.created_at,
            author=target.author,
        )

    async def share_bundle(
        self,
        ref: str,
        *,
        version: int = 0,
        together_with: list[str] | None = None,
    ) -> ExportBundle | None:
        """Render for an audience outside this machine.

        ``together_with`` names the other artifacts going out in the
        same share. Edges between them survive; everything else is
        projected away.
        """
        summary = await self._artifacts.resolve(ref)
        if summary is None:
            return None
        ids = {summary.artifact_id}
        for other in together_with or []:
            resolved = await self._artifacts.resolve(other)
            if resolved is not None:
                ids.add(resolved.artifact_id)
        return await self.bundle(ref, version=version, scope=ExportScope.shared(ids))

    async def write(
        self,
        ref: str,
        destination: Path,
        *,
        version: int = 0,
    ) -> Path | None:
        """Write a local export to ``destination``.

        A directory destination gets a filename derived from the
        artifact; a file destination is used as given.
        """
        bundle = await self.bundle(ref, version=version)
        if bundle is None:
            return None
        target = destination
        if destination.is_dir() or not destination.suffix:
            slug = _slug(bundle.title) or bundle.artifact_id
            target = destination / f"{slug}-v{bundle.n}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bundle.html, encoding="utf-8")
        return target


class ArtifactPublisher:
    """Uploads a projected bundle to the igni server.

    Publishing is a snapshot, not a live mirror: the server stores the
    rendered page, so later revisions do not silently change what a
    recipient sees. Re-publishing replaces the snapshot and records
    which version it came from.
    """

    def __init__(
        self,
        *,
        server_url: str,
        access_token: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._access_token = access_token
        self._client = client

    @property
    def is_configured(self) -> bool:
        """False when logged out — callers check rather than catching.

        A local-only user is a supported configuration, not an error
        path, and should not generate a stack trace per share attempt.
        """
        return bool(self._server_url and self._access_token)

    async def publish(
        self,
        bundle: ExportBundle,
        *,
        visibility: str = "private",
        audience: list[str] | None = None,
    ) -> str:
        """Upload and return the shareable URL.

        ``visibility`` is one of:

        * ``private`` — only you.
        * ``users`` — the identities in ``audience``, given as email
          addresses or user ids.
        * ``domain`` — anyone whose account email shares yours. The
          domain is taken from your account by the server and cannot
          be named here, because a client that could name its own
          domain could publish into somebody else's.
        * ``org`` — your organisation.
        * ``public`` — anyone with the link, signed in or not.

        The list is not validated here. The server owns it, and a
        second copy of the enum in the client is a second place to
        forget to update — the failure mode of getting it wrong is a
        422 naming the allowed values, which is a better error than
        anything this side could invent.
        """
        if not self.is_configured:
            raise RuntimeError("Sharing needs a signed-in igni account — run `igni login` first.")
        payload = bundle.to_payload(visibility=visibility, audience=audience or [])
        url = f"{self._server_url}/v1/artifacts/shared"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=PUBLISH_TIMEOUT_SECONDS)
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        finally:
            if owns_client:
                await client.aclose()
        share_url = str(data.get("url") or "")
        if not share_url:
            raise RuntimeError("Server accepted the artifact but returned no URL.")
        return share_url


def _slug(text: str) -> str:
    out = []
    for ch in str(text or "").strip().casefold():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/." and (not out or out[-1] != "-"):
            out.append("-")
    return "".join(out).strip("-")[:60]
