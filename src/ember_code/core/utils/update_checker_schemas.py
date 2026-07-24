"""Pydantic DTOs consumed by :class:`UpdateChecker`.

Kept in a sibling module (``update_checker_schemas.py``) so:

* The domain / wire / cache shapes live on the data, not on the
  orchestrator — the CLI, tests, and RPC-level ``check_for_update``
  handler can all import :class:`UpdateInfo` (and friends) without
  dragging in the network + filesystem side-effects that live on
  :class:`UpdateChecker`.
* Rule 1 (no raw dicts crossing module boundaries) is enforced
  structurally — the PyPI JSON envelope is parsed through
  :class:`PyPIInfoResponse`, the on-disk cache is round-tripped
  through :class:`UpdateCacheEntry`, and the installed-package
  probe returns :class:`PackageMetadata` — no ``dict[str, Any]``
  gets propagated past this layer.
* Importing this module has no side effects. The old
  ``update_checker.py`` ran ``importlib.metadata.metadata()`` at
  import time; :meth:`PackageMetadata.load` moves that to an
  explicit classmethod call, so ``from … import UpdateInfo``
  in a hot path (e.g. tests) doesn't touch the installed-package
  DB.

Every model here is re-exported from
:mod:`ember_code.core.utils.update_checker` so external callers can
keep a single import site.
"""

from __future__ import annotations

import time
from enum import Enum
from importlib.metadata import metadata

from pydantic import BaseModel, ConfigDict, Field


class UpdateError(str, Enum):
    """Typed reason an update check failed.

    Kept as a StrEnum so the wire value is still a plain string
    (``UpdateInfo.error`` serialises to ``"network"`` etc.) while
    the code path has an enum to switch on.
    """

    NETWORK = "network"
    HTTP_STATUS = "http_status"
    PARSE = "parse"
    CACHE_IO = "cache_io"
    UNKNOWN = "unknown"


class Version(BaseModel):
    """Comparable version value object.

    Replaces the free-function ``_parse_version`` / ``_is_newer``
    pair. Version comparison is behavior *on* the parsed tuple, so
    it lives on the model with ``__lt__`` / ``__gt__`` dunders —
    call sites read ``Version.parse(latest) > Version.parse(current)``
    instead of routing through a helper module.
    """

    model_config = ConfigDict(frozen=True)

    parts: tuple[int, ...] = (0,)

    @classmethod
    def parse(cls, raw: str) -> Version:
        """Parse a version string into a :class:`Version`.

        Leading ``v`` / whitespace is stripped; any component that
        can't be coerced to ``int`` collapses the whole version to
        ``(0,)`` so garbage never compares as newer than a real
        release.
        """
        try:
            parts = tuple(int(x) for x in raw.strip().lstrip("v").split("."))
        except (ValueError, AttributeError):
            parts = (0,)
        return cls(parts=parts)

    def __lt__(self, other: Version) -> bool:
        return self.parts < other.parts

    def __gt__(self, other: Version) -> bool:
        return self.parts > other.parts

    def __le__(self, other: Version) -> bool:
        return self.parts <= other.parts

    def __ge__(self, other: Version) -> bool:
        return self.parts >= other.parts


class UpdateInfo(BaseModel):
    """Result of an update check.

    Consumed by the CLI banner + the ``check_for_update`` RPC.
    Never carries a raw exception object — network / parse / cache
    failures land in :attr:`error` (a typed :class:`UpdateError`)
    plus a human-readable :attr:`error_detail` string.
    """

    available: bool = False
    latest_version: str = ""
    current_version: str = ""
    release_notes: str = ""
    download_url: str = ""
    error: UpdateError | None = None
    error_detail: str | None = None

    @property
    def message(self) -> str:
        """Human-readable update message."""
        if self.error:
            return ""
        if not self.available:
            return ""
        msg = f"Update available: v{self.current_version} → v{self.latest_version}"
        if self.release_notes:
            msg += f"  ({self.release_notes})"
        if self.download_url:
            msg += f"\n  Upgrade: {self.download_url}"
        return msg


class UpdateCacheEntry(BaseModel):
    """Typed on-disk cache entry for the update check.

    Replaces the raw ``dict[str, Any]`` that used to be written to
    ``~/.ember/.update-check``. ``extra='ignore'`` lets older cache
    files with legacy fields validate cleanly, and every field has
    a default so a forward-compat schema evolution can't silently
    void the user's cache.
    """

    model_config = ConfigDict(extra="ignore")

    latest_version: str = ""
    download_url: str = ""
    release_notes: str = ""
    checked_at: float = Field(default_factory=time.time)

    def is_fresh(self, ttl: int) -> bool:
        """Whether the entry is still within its TTL."""
        return (time.time() - self.checked_at) <= ttl


class _PyPIInfo(BaseModel):
    """Inner ``info`` block of the PyPI JSON envelope."""

    model_config = ConfigDict(extra="ignore")

    version: str = ""
    project_url: str = ""


class PyPIInfoResponse(BaseModel):
    """Typed view of the PyPI ``/pypi/{pkg}/json`` response.

    ``extra='ignore'`` drops the ~40 other fields PyPI returns at
    the boundary, satisfying Pattern 7 (parse-don't-shape at the
    wire seam). Replaces the ``data.get('info', {}).get('version',
    '')`` chain on :class:`UpdateChecker`.
    """

    model_config = ConfigDict(extra="ignore")

    info: _PyPIInfo = Field(default_factory=_PyPIInfo)


class PackageMetadata(BaseModel):
    """Installed-package identifiers.

    Replaces the module-level ``_PKG_META`` / ``_PKG_NAME`` /
    ``_PROJECT_URL`` globals that used to run
    :func:`importlib.metadata.metadata` at import time.
    :meth:`load` runs the probe only when explicitly invoked, so
    importing this file (and the sibling ``update_checker``
    module) has no side effects.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    project_url: str = ""

    @classmethod
    def load(cls, distribution_name: str = "ignite-ember") -> PackageMetadata:
        """Probe :mod:`importlib.metadata` for the installed
        distribution and pull the canonical name + best project URL
        off it.

        The Project-URL list is scanned once for a Homepage or
        Repository entry; anything else collapses to the empty
        string (the caller falls back to the PyPI-provided URL in
        that case).
        """
        pkg_meta = metadata(distribution_name)
        name = pkg_meta["Name"]
        project_url = ""
        for line in pkg_meta.get_all("Project-URL") or []:
            if "Homepage" in line or "Repository" in line:
                project_url = line.split(",", 1)[-1].strip()
                break
        return cls(name=name, project_url=project_url)
