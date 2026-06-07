"""Resolve the local git remote URL to its ember-server ``repository_id``.

Given a project directory, the resolver:

1. Reads the local git remote URL via ``git remote get-url origin``
2. Calls ``GET {server_url}/v1/cli/codeindex/repository?remote_url=...``
   with the user's cloud auth token. The server returns one of:

   - ``status='registered'`` + ``repository_id`` → caller has access; proceed.
   - ``status='install_required'`` + ``install_url`` → user needs to install
     the GitHub App; surface the URL.

3. Caches the response so subsequent calls are free.

Every degraded path (no git, no remote, no auth, server down, access
denied) returns ``None`` rather than raising — the sync manager treats
``None`` as "skip silently".
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from ember_code.core.auth.credentials import CloudCredentials

logger = logging.getLogger(__name__)


class DiscoveryStatus(StrEnum):
    """Mirror of the server's DiscoveryStatus."""

    REGISTERED = "registered"
    INSTALL_REQUIRED = "install_required"


@dataclass(frozen=True)
class ResolvedRepository:
    """Result of resolving a git remote URL against ember-server."""

    status: DiscoveryStatus
    repository_id: str | None = None  # set when status == REGISTERED
    install_url: str | None = None  # set when status == INSTALL_REQUIRED

    @property
    def needs_install(self) -> bool:
        return self.status == DiscoveryStatus.INSTALL_REQUIRED


class RepositoryResolver:
    """Discover ``repository_id`` (or App install URL) from the local git remote."""

    def __init__(
        self,
        *,
        project_dir: Path,
        server_url: str,
        credentials: CloudCredentials,
        timeout: float = 10.0,
    ) -> None:
        self.project_dir = project_dir
        self.server_url = server_url.rstrip("/")
        self.credentials = credentials
        self.timeout = timeout
        self._cached: ResolvedRepository | None = None
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> ResolvedRepository | None:
        return self._cached

    def remote_url(self) -> str | None:
        """Return ``git remote get-url origin``, or ``None`` if unavailable."""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        return url or None

    async def resolve(self, *, force: bool = False) -> ResolvedRepository | None:
        """Return the cached resolution, or fetch it from the server."""
        if self._cached is not None and not force:
            return self._cached

        async with self._lock:
            if self._cached is not None and not force:
                return self._cached

            url = self.remote_url()
            if not url:
                logger.debug("skipping codeindex resolve: no git remote")
                return None

            token = self.credentials.access_token
            if not token:
                logger.debug("skipping codeindex resolve: no cloud auth")
                return None

            endpoint = f"{self.server_url}/v1/cli/codeindex/repository"
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(
                        endpoint,
                        params={"remote_url": url},
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError as exc:
                logger.info("codeindex resolver: server unreachable (%s)", exc)
                return None

            if response.status_code != 200:
                logger.info(
                    "codeindex resolver: unexpected status %d for %s",
                    response.status_code,
                    url,
                )
                return None

            try:
                payload = response.json()
                resolved = ResolvedRepository(
                    status=DiscoveryStatus(payload["status"]),
                    repository_id=payload.get("repository_id"),
                    install_url=payload.get("install_url"),
                )
            except (KeyError, ValueError) as exc:
                logger.info("codeindex resolver: malformed payload (%s)", exc)
                return None

            self._cached = resolved
            return self._cached
