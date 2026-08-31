"""Resolve the local git remote URL to its ember-server ``repository_id``.

Given a project directory, the resolver:

1. Reads the local git remote URL via ``git remote get-url origin``
2. Calls ``GET {server_url}/v1/codeindex/repository?remote_url=...``
   with the user's cloud auth token. The server returns one of:

   - ``status='registered'`` + ``repository_id`` → caller has access; proceed.
   - ``status='install_required'`` + ``install_url`` → user needs to install
     the GitHub App; surface the URL.

3. Caches the response so subsequent calls are free.

Most degraded paths (no git, no remote, no auth, server down) return
``None`` rather than raising — the sync manager treats ``None`` as "skip
silently", which is right when there is nothing the user could do.

**Access denial is not one of them.** A 403 means the server answered,
and its answer is usually actionable: under SSO nobody has a provider
identity linked at sign-in, so "connect your GitHub account" is where
most people start. Swallowing that into ``None`` reported it as
"server unreachable" — untrue, and it hid the one instruction that
would have fixed it. Denials come back as ``ACCESS_DENIED`` carrying
the server's reason and message.
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
from ember_code.core.utils.http_retry import retry_with_backoff

logger = logging.getLogger(__name__)


class DiscoveryStatus(StrEnum):
    """The server's discovery outcomes, plus one this client derives.

    ``REGISTERED`` and ``INSTALL_REQUIRED`` mirror the server's enum and
    arrive in a 200 body. ``ACCESS_DENIED`` has no server-side twin: a
    refusal is a 403, not a 200, and this is where that lands so callers
    have a single thing to branch on rather than two.
    """

    REGISTERED = "registered"
    INSTALL_REQUIRED = "install_required"
    ACCESS_DENIED = "access_denied"


@dataclass(frozen=True)
class ResolvedRepository:
    """Result of resolving a git remote URL against ember-server."""

    status: DiscoveryStatus
    repository_id: str | None = None  # set when status == REGISTERED
    install_url: str | None = None  # set when status == INSTALL_REQUIRED
    # Both set when status == ACCESS_DENIED. ``reason`` is the server's
    # enum value, for branching; ``message`` is the sentence to show.
    denial_reason: str | None = None
    denial_message: str | None = None

    @property
    def needs_install(self) -> bool:
        return self.status == DiscoveryStatus.INSTALL_REQUIRED

    @property
    def access_denied(self) -> bool:
        return self.status == DiscoveryStatus.ACCESS_DENIED


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

            endpoint = f"{self.server_url}/v1/codeindex/repository"
            try:

                async def _fetch_repository() -> httpx.Response:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        resp = await client.get(
                            endpoint,
                            params={"remote_url": url},
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        resp.raise_for_status()
                        return resp

                response, metadata = await retry_with_backoff(_fetch_repository)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 403:
                    logger.info("codeindex resolver: server refused (%s)", exc)
                    return None
                # Deliberately not cached. The commonest denial is "you
                # have not linked a provider identity", which the user
                # fixes in a browser without restarting igni — a cached
                # verdict would keep reporting a problem they just
                # solved.
                return self._denial(exc.response)
            except httpx.HTTPError as exc:
                logger.info("codeindex resolver: server unreachable (%s)", exc)
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

    @staticmethod
    def _denial(response: httpx.Response) -> ResolvedRepository:
        """Read a 403 body into an ``ACCESS_DENIED`` result.

        Falls back to a generic sentence rather than to ``None``: an
        older server, or a proxy that rewrote the body, still leaves the
        user better off knowing they were refused than being told the
        server was unreachable.
        """
        reason: str | None = None
        message: str | None = None
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        if isinstance(detail, dict):
            reason = detail.get("reason")
            message = detail.get("message")
        elif isinstance(detail, str):
            message = detail

        logger.info("codeindex resolver: access denied (%s)", reason or "unspecified")
        return ResolvedRepository(
            status=DiscoveryStatus.ACCESS_DENIED,
            denial_reason=reason,
            denial_message=message or "You do not have access to this repository.",
        )
