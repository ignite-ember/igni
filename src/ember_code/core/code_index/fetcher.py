"""Download per-commit JSONL changesets via short-lived signed URLs.

The CLI never hits ``storage.googleapis.com`` directly. The flow is:

1. ember-code asks ember-server for a signed URL: ``POST /v1/codeindex/changeset-url``
   with the cloud auth token and ``{repository_id, commit_sha}``.
2. ember-server verifies live read access (calls GitHub/GitLab as the
   user) and mints a 10-minute signed GCS URL.
3. ember-code GETs the signed URL and streams the JSONL to disk.
4. :func:`apply_delta` replays the file into the local index.

Every degraded path (no auth, no remote, repo not registered, no access,
signed URL expired) raises :class:`ChangesetFetchError`. The sync
manager turns those into ``SyncResult(error=...)`` rather than crashing.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import httpx

from ember_code.core.code_index.delta import DeltaStats, apply_delta

logger = logging.getLogger(__name__)


class ChangesetFetchError(RuntimeError):
    """Raised when a changeset can't be downloaded."""


class PreflightStatus(StrEnum):
    """Mirrors ember-server's PreflightStatus — outcomes ember-code branches on."""

    OK = "ok"
    IN_PROGRESS = "in_progress"
    FAILED = "failed"
    LINK_REQUIRED = "link_required"
    NO_MATCHING_ACCOUNT = "no_matching_account"
    REPO_NOT_FOUND = "repo_not_found"
    CHANGESET_NOT_FOUND = "changeset_not_found"


@dataclass
class PreflightResult:
    """Parsed shape of POST /codeindex/preflight."""

    status: PreflightStatus
    parent_sha: str | None = None
    progress_percentage: int | None = None
    current_step: str | None = None
    started_at: datetime | None = None
    error_message: str | None = None
    link_start_url: str | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> PreflightResult:
        raw_started = payload.get("started_at")
        started_at: datetime | None = None
        if raw_started:
            try:
                started_at = datetime.fromisoformat(str(raw_started).replace("Z", "+00:00"))
            except ValueError:
                started_at = None
        return cls(
            status=PreflightStatus(payload["status"]),
            parent_sha=payload.get("parent_sha"),
            progress_percentage=payload.get("progress_percentage"),
            current_step=payload.get("current_step"),
            started_at=started_at,
            error_message=payload.get("error_message"),
            link_start_url=payload.get("link_start_url"),
        )


class ChangesetFetcher:
    """Fetch per-commit JSONL changesets from ember-server (signed URLs)."""

    def __init__(
        self,
        *,
        server_url: str,
        bearer_token: str,
        timeout: float = 60.0,
    ) -> None:
        if not server_url:
            raise ValueError("server_url is required")
        if not bearer_token:
            raise ValueError("bearer_token is required")
        self.server_url = server_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout = timeout

    async def download(
        self,
        *,
        repository_id: str,
        commit_sha: str,
        dest_dir: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> Path:
        """Stream the per-commit delta changeset to disk and return the file path."""
        return await self._download_via(
            endpoint="changeset-url",
            repository_id=repository_id,
            commit_sha=commit_sha,
            dest_dir=dest_dir,
            client=client,
        )

    async def download_snapshot(
        self,
        *,
        repository_id: str,
        commit_sha: str,
        dest_dir: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> Path:
        """Stream a full-state snapshot JSONL to disk and return the file path.

        Used when the local index has no ancestor to copy-on-write
        from (fresh install, pruned history, branch switch). Server
        builds + caches the snapshot in GCS the first time, returns a
        signed URL the same way the delta endpoint does.
        """
        return await self._download_via(
            endpoint="changeset-snapshot",
            repository_id=repository_id,
            commit_sha=commit_sha,
            dest_dir=dest_dir,
            client=client,
        )

    async def _download_via(
        self,
        *,
        endpoint: str,
        repository_id: str,
        commit_sha: str,
        dest_dir: str | Path | None,
        client: httpx.AsyncClient | None,
    ) -> Path:
        target_dir = (
            Path(dest_dir) if dest_dir else Path(tempfile.mkdtemp(prefix="ember-changeset-"))
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{commit_sha}.jsonl"

        owns_client = client is None
        http = client or httpx.AsyncClient(timeout=self.timeout)
        try:
            signed_url = await self._request_signed_url(
                client=http,
                endpoint=endpoint,
                repository_id=repository_id,
                commit_sha=commit_sha,
            )
            await self._stream_to_disk(client=http, signed_url=signed_url, target=target)
        finally:
            if owns_client:
                await http.aclose()

        logger.info("Downloaded %s %s/%s → %s", endpoint, repository_id, commit_sha[:8], target)
        return target

    async def preflight(
        self,
        *,
        repository_id: str,
        commit_sha: str,
        client: httpx.AsyncClient | None = None,
    ) -> PreflightResult:
        """Ask ember-server whether the changeset for (repo, commit) is usable.

        Always returns 200 with a structured PreflightResult — the caller
        branches on ``result.status`` to decide whether to download, poll,
        prompt the user to link, etc. Network/parsing failures raise
        ChangesetFetchError so the sync manager can degrade gracefully.
        """
        owns_client = client is None
        http = client or httpx.AsyncClient(timeout=self.timeout)
        try:
            endpoint = f"{self.server_url}/v1/codeindex/preflight"
            try:
                response = await http.post(
                    endpoint,
                    json={"repository_id": repository_id, "commit_sha": commit_sha},
                    headers={"Authorization": f"Bearer {self.bearer_token}"},
                )
            except httpx.HTTPError as exc:
                raise ChangesetFetchError(f"preflight unreachable: {exc}") from exc

            if response.status_code == 401:
                raise ChangesetFetchError("preflight unauthorized — bearer token rejected")
            if response.status_code >= 400:
                raise ChangesetFetchError(
                    f"preflight failed ({response.status_code}): {response.text[:200]}"
                )
            try:
                return PreflightResult.from_payload(response.json())
            except (KeyError, ValueError) as exc:
                raise ChangesetFetchError(f"preflight returned malformed payload: {exc}") from exc
        finally:
            if owns_client:
                await http.aclose()

    async def pull_and_apply(
        self,
        *,
        index,
        file_refs,
        repository_id: str,
        commit_sha: str,
    ) -> DeltaStats:
        """Download the per-commit delta and apply it to the local index in one shot."""
        return await self._pull_and_apply_via(
            self.download,
            index=index,
            file_refs=file_refs,
            repository_id=repository_id,
            commit_sha=commit_sha,
        )

    async def pull_and_apply_snapshot(
        self,
        *,
        index,
        file_refs,
        repository_id: str,
        commit_sha: str,
    ) -> DeltaStats:
        """Download a snapshot and apply it to the local index in one shot.

        Mirrors ``pull_and_apply`` but uses the snapshot endpoint, so
        the caller doesn't need a local ancestor — the JSONL upserts
        the full state at ``commit_sha`` from scratch.
        """
        return await self._pull_and_apply_via(
            self.download_snapshot,
            index=index,
            file_refs=file_refs,
            repository_id=repository_id,
            commit_sha=commit_sha,
        )

    async def _pull_and_apply_via(
        self,
        downloader,
        *,
        index,
        file_refs,
        repository_id: str,
        commit_sha: str,
    ) -> DeltaStats:
        jsonl_path = await downloader(repository_id=repository_id, commit_sha=commit_sha)
        try:
            return await apply_delta(index=index, file_refs=file_refs, jsonl_path=jsonl_path)
        finally:
            try:
                jsonl_path.unlink()
                jsonl_path.parent.rmdir()
            except OSError:
                pass

    # ── Internals ────────────────────────────────────────────────────

    async def _request_signed_url(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        repository_id: str,
        commit_sha: str,
    ) -> str:
        endpoint = f"{self.server_url}/v1/codeindex/{endpoint}"
        try:
            response = await client.post(
                endpoint,
                json={"repository_id": repository_id, "commit_sha": commit_sha},
                headers={"Authorization": f"Bearer {self.bearer_token}"},
            )
        except httpx.HTTPError as exc:
            raise ChangesetFetchError(f"server unreachable: {exc}") from exc

        if response.status_code == 403:
            raise ChangesetFetchError(
                "access denied (403): you lack read access to this repository"
            )
        if response.status_code == 404:
            raise ChangesetFetchError(
                f"changeset not found (404): repository_id={repository_id!r} commit={commit_sha[:8]}"
            )
        if response.status_code >= 400:
            raise ChangesetFetchError(
                f"signed-URL request failed ({response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        signed_url = payload.get("signed_url")
        if not signed_url:
            raise ChangesetFetchError("signed-URL response missing signed_url field")
        return signed_url

    @staticmethod
    async def _stream_to_disk(
        *,
        client: httpx.AsyncClient,
        signed_url: str,
        target: Path,
    ) -> None:
        async with client.stream("GET", signed_url) as response:
            if response.status_code == 404:
                raise ChangesetFetchError(
                    "signed URL returned 404 — changeset has not been uploaded yet"
                )
            if response.status_code >= 400:
                raise ChangesetFetchError(
                    f"changeset download failed ({response.status_code}) from signed URL"
                )
            with target.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    fh.write(chunk)
