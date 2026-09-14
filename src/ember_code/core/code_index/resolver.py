"""Resolve the local git remote URL to its ember-server ``repository_id``.

Given a project directory, the resolver:

1. Reads the local git remote URL via ``git remote get-url origin``
2. Calls ``GET {server_url}/v1/codeindex/repository?remote_url=...``
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
class ResolveFailure:
    """Why the resolver could not answer.

    It has four ways of returning ``None`` — no git remote, no cloud
    token, an unreachable server, a response it could not read — and
    they used to be indistinguishable to every caller. The controller
    turned all four into ``install_state="unknown"``, and the pill
    turned that into **"not indexed — HEAD needs a sync"**: a remedy
    that cannot work, offered for four situations, three of which a
    sync would not touch.

    ``reason`` says what happened; ``fix`` says what to do. They are
    separate because the second is usually actionable when the first
    is not — see :mod:`ember_code.backend.subsystem_status`, which
    this feeds.
    """

    reason: str
    fix: str = ""


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
        self._failure: ResolveFailure | None = None
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> ResolvedRepository | None:
        return self._cached

    @property
    def failure(self) -> ResolveFailure | None:
        """Why the last resolve came back empty, if it did.

        ``None`` when the resolver has not run yet or has succeeded —
        the two states that are genuinely "nothing to report".
        """
        return self._failure

    def invalidate(self) -> None:
        """Forget the cached resolution, so the next call asks again.

        Called on wake. The resolution is an answer about a remote
        server, obtained before the machine slept; after a sleep of
        any length it is a claim about the past. Dropping it costs one
        request and removes the possibility of showing a stale
        "connected" (or a stale reason) indefinitely.

        The failure goes with it: a reason from before the sleep
        explains a situation that may no longer exist, and "not logged
        in" surviving a wake into a fresh session would be its own
        small lie.
        """
        self._cached = None
        self._failure = None

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
                return self._fail(
                    "this folder has no git remote",
                    "CodeIndex indexes a repository — open a folder with an "
                    "`origin` remote, or add one.",
                )

            token = self.credentials.access_token
            if not token:
                return self._fail(
                    "not logged in to igni Cloud",
                    "Run /login. CodeIndex resolves the repository through the "
                    "cloud, so it cannot tell what state this repo is in until "
                    "you are signed in.",
                )

            endpoint = f"{self.server_url}/v1/codeindex/repository"
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(
                        endpoint,
                        params={"remote_url": url},
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError as exc:
                return self._fail(
                    f"could not reach igni Cloud ({exc.__class__.__name__})",
                    "Check your connection. Everything else in igni works "
                    "offline; only CodeIndex needs the server.",
                )

            if response.status_code == 401:
                return self._fail(
                    "igni Cloud rejected the stored credentials",
                    "Run /login again — the session has probably expired.",
                )
            if response.status_code != 200:
                return self._fail(
                    f"igni Cloud answered {response.status_code} for this repository",
                    "If it persists, the repository may not be reachable by the "
                    "GitHub App. Check it at the portal.",
                )

            try:
                payload = response.json()
                resolved = ResolvedRepository(
                    status=DiscoveryStatus(payload["status"]),
                    repository_id=payload.get("repository_id"),
                    install_url=payload.get("install_url"),
                )
            except (KeyError, ValueError) as exc:
                return self._fail(
                    f"igni Cloud sent a reply this client could not read ({exc})",
                    "This usually means the client and server are different "
                    "versions. Updating igni is the fix.",
                )

            self._cached = resolved
            self._failure = None
            return self._cached

    def _fail(self, reason: str, fix: str = "") -> ResolvedRepository | None:
        """Record why there is no answer, and log it once.

        Logged at INFO rather than DEBUG because, since the backend
        keeps a log by default, INFO is what someone reconstructing a
        "why is this off?" report will actually have.
        """
        self._failure = ResolveFailure(reason=reason, fix=fix)
        logger.info("codeindex resolver: %s", reason)
        # Typed as returning the same thing ``resolve`` does, so its
        # four early exits can stay one line each.
        return None
