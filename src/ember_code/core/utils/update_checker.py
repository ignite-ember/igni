"""Update checker — asks PyPI whether a newer release exists.

Calls ``GET https://pypi.org/pypi/{package}/json`` and inspects
the ``info.version`` field of the response. The check runs
asynchronously at session start and never blocks the user.
Results are cached in ``~/.ember/.update-check`` (a JSON blob
matching :class:`UpdateCacheEntry`) to avoid hitting the network
on every session; the TTL is configurable via
``update_check_ttl`` in settings.

Architecture — everything is on :class:`UpdateChecker`:

* :class:`UpdateCache` (composed value object) owns the file-cache
  read/write. Its constructor takes the cache path so tests can
  point it at a tmp file without patching module globals.
* :class:`PackageMetadata` (in the sibling schemas module) owns
  the :func:`importlib.metadata.metadata` probe. Resolved *lazily*
  on first :class:`UpdateChecker` construction — importing this
  module no longer runs the probe.
* :class:`UpdateChecker.check` is the orchestrator. The
  module-level :func:`check_for_update` is a thin back-compat
  wrapper that constructs a fresh checker on every call (so
  ``patch('...CACHE_FILE', ...)`` still flows through) and
  swallows any last-mile exception into an :class:`UpdateInfo`
  with a typed :class:`UpdateError`.

Every Pydantic model in this module comes from
:mod:`.update_checker_schemas` and is re-exported here so
external callers can keep a single import site.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
from pydantic import ValidationError

from ember_code import __version__
from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.utils.update_checker_schemas import (
    PackageMetadata,
    PyPIInfoResponse,
    UpdateCacheEntry,
    UpdateError,
    UpdateInfo,
    Version,
)

# Re-exports so callers keep the single-import-site convention.
__all__ = [
    "CACHE_FILE",
    "PackageMetadata",
    "PyPIInfoResponse",
    "UpdateCache",
    "UpdateCacheEntry",
    "UpdateChecker",
    "UpdateError",
    "UpdateInfo",
    "Version",
    "check_for_update",
]

logger = logging.getLogger(__name__)

CACHE_FILE = Path.home() / ".ember" / ".update-check"


# ── Cache value object ────────────────────────────────────────────


class UpdateCache:
    """File-backed TTL cache for the update check.

    Composed onto :class:`UpdateChecker` rather than exposed as a
    module-level pair of free functions. Constructor takes the
    path so tests point it at a tmp file directly — no more
    module-global patching for cache IO.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, ttl: int) -> UpdateCacheEntry | None:
        """Return the cached entry when it exists and is still
        fresh. Any read / parse failure is logged and treated as a
        cache miss (the caller falls back to the network) so a
        corrupt cache file can never break the update check."""
        try:
            if not self._path.exists():
                return None
            raw = self._path.read_text()
        except OSError as exc:
            logger.debug("Failed to read update cache: %s", exc)
            return None
        try:
            entry = UpdateCacheEntry.model_validate_json(raw)
        except (ValueError, ValidationError) as exc:
            logger.debug("Failed to parse update cache: %s", exc)
            return None
        if not entry.is_fresh(ttl):
            return None
        return entry

    def write(self, entry: UpdateCacheEntry) -> None:
        """Persist the entry to disk. Best-effort — a full disk /
        permission-denied is logged and swallowed so the update
        check never breaks the caller."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Rewrite ``checked_at`` at write time so the TTL is
            # measured from persistence, not construction.
            payload = entry.model_copy(update={"checked_at": time.time()})
            self._path.write_text(payload.model_dump_json())
        except OSError as exc:
            logger.debug("Failed to write update cache: %s", exc)


# ── Orchestrator ─────────────────────────────────────────────────


class UpdateChecker:
    """Asks PyPI whether a newer release exists.

    Composition:

    * :class:`UpdateCache` — file IO
    * :class:`PackageMetadata` — installed-package probe (lazy)

    The ``timeout`` + ``settings`` + ``cache_path`` are constructor
    args so a test / caller can override every collaborator
    without patching module state.
    """

    _pkg_cache: PackageMetadata | None = None

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        timeout: float = 5.0,
        cache_path: Path | None = None,
        package: PackageMetadata | None = None,
    ) -> None:
        # Read the module-level default at construction so
        # ``patch('...CACHE_FILE', ...)`` in existing tests still
        # flows into the instance.
        self._settings = settings
        self._timeout = timeout
        self._cache = UpdateCache(cache_path if cache_path is not None else CACHE_FILE)
        self._package = package if package is not None else self._resolve_package()

    @classmethod
    def _resolve_package(cls) -> PackageMetadata:
        """Lazy singleton for the installed-package probe.

        The probe hits :mod:`importlib.metadata` which walks the
        site-packages dist-info; not free. We resolve it on first
        use and cache it on the class so subsequent checks are
        instantaneous, without paying the cost at *import* time
        the way the old module-level ``metadata()`` call did.
        """
        if cls._pkg_cache is None:
            cls._pkg_cache = PackageMetadata.load()
        return cls._pkg_cache

    async def check(self) -> UpdateInfo:
        """Ask PyPI (or the cache) whether a newer version exists.

        Returns an :class:`UpdateInfo`. Narrow ``except`` clauses
        inside — :exc:`asyncio.CancelledError` must propagate so a
        parent-task cancellation isn't swallowed by the update
        check. The module-level :func:`check_for_update` wrapper
        catches anything truly unexpected.
        """
        current = __version__
        settings = self._settings if self._settings is not None else load_settings()
        ttl = settings.update_check_ttl

        # Cache lane
        cached = self._cache.read(ttl)
        if cached is not None:
            latest = cached.latest_version
            if latest and Version.parse(latest) > Version.parse(current):
                return UpdateInfo(
                    available=True,
                    latest_version=latest,
                    current_version=current,
                    release_notes=cached.release_notes,
                    download_url=cached.download_url,
                )
            return UpdateInfo(available=False, current_version=current)

        # Network lane
        pypi_url = f"https://pypi.org/pypi/{self._package.name}/json"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(pypi_url)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.debug("Update check failed (HTTP status): %s", exc)
            return UpdateInfo(
                available=False,
                current_version=current,
                error=UpdateError.HTTP_STATUS,
                error_detail=str(exc),
            )
        except httpx.HTTPError as exc:
            logger.debug("Update check failed (network): %s", exc)
            return UpdateInfo(
                available=False,
                current_version=current,
                error=UpdateError.NETWORK,
                error_detail=str(exc),
            )
        except ValueError as exc:  # resp.json() decode failure
            logger.debug("Update check failed (parse): %s", exc)
            return UpdateInfo(
                available=False,
                current_version=current,
                error=UpdateError.PARSE,
                error_detail=str(exc),
            )

        try:
            info = PyPIInfoResponse.model_validate(payload)
        except ValidationError as exc:
            logger.debug("Update check failed (schema): %s", exc)
            return UpdateInfo(
                available=False,
                current_version=current,
                error=UpdateError.PARSE,
                error_detail=str(exc),
            )

        latest = info.info.version
        release_url = info.info.project_url or self._package.project_url

        # Cache the response for the next session.
        self._cache.write(UpdateCacheEntry(latest_version=latest, download_url=release_url))

        if latest and Version.parse(latest) > Version.parse(current):
            return UpdateInfo(
                available=True,
                latest_version=latest,
                current_version=current,
                download_url=release_url,
            )
        return UpdateInfo(available=False, current_version=current)


# ── Back-compat module-level wrapper ─────────────────────────────


async def check_for_update(
    settings: Settings | None = None,
    timeout: float = 5.0,
) -> UpdateInfo:
    """Check PyPI for a newer CLI version.

    Thin wrapper around :meth:`UpdateChecker.check` kept for the
    existing call sites (``interactive_loop._check_update``,
    ``rpc_router._check_for_update``, the test suite). Constructs
    a *fresh* :class:`UpdateChecker` on every call so
    ``patch('ember_code.core.utils.update_checker.CACHE_FILE', ...)``
    in tests still flows into the instance's :class:`UpdateCache`.

    Never raises. :exc:`asyncio.CancelledError` propagates so a
    parent-task cancellation isn't silently swallowed; every other
    exception is logged and reported via
    ``UpdateInfo.error=UpdateError.UNKNOWN`` — some interactive-loop
    callers rely on "never raises" as an invariant.
    """
    checker = UpdateChecker(settings=settings, timeout=timeout)
    try:
        return await checker.check()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — top-boundary safety net
        logger.debug("Update check failed (unexpected): %s", exc)
        return UpdateInfo(
            available=False,
            current_version=__version__,
            error=UpdateError.UNKNOWN,
            error_detail=str(exc),
        )
