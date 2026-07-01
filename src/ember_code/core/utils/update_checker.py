"""Update checker — checks the Ember server for new versions.

Calls ``GET {version_endpoint}`` on the Ember server. The server returns:

    {
        "latest_version": "0.2.0",
        "min_version": "0.1.0",
        "release_notes": "Bug fixes and knowledge sync improvements.",
        "download_url": "https://github.com/ignite-ember/igni/releases"
    }

The check runs asynchronously at session start and never blocks the user.
Results are cached in ``~/.ember/.update-check`` to avoid hitting the server
on every session. The TTL is configurable via ``update_check_ttl`` in settings.
"""

import json
import logging
import time
from importlib.metadata import metadata
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from ember_code import __version__
from ember_code.core.config.settings import Settings

# Derive package name and project URL from installed package metadata
_PKG_META = metadata("ignite-ember")
_PKG_NAME = _PKG_META["Name"]
_PROJECT_URL = ""
for line in _PKG_META.get_all("Project-URL") or []:
    if "Homepage" in line or "Repository" in line:
        _PROJECT_URL = line.split(",", 1)[-1].strip()
        break

logger = logging.getLogger(__name__)

CACHE_FILE = Path.home() / ".ember" / ".update-check"


class UpdateInfo(BaseModel):
    """Result of an update check."""

    available: bool = False
    latest_version: str = ""
    current_version: str = ""
    release_notes: str = ""
    download_url: str = ""
    error: str | None = None

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


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse a version string into a comparable tuple."""
    try:
        return tuple(int(x) for x in version.strip().lstrip("v").split("."))
    except (ValueError, AttributeError):
        return (0,)


def _is_newer(latest: str, current: str) -> bool:
    """Check if latest version is newer than current."""
    return _parse_version(latest) > _parse_version(current)


# ── Cache ────────────────────────────────────────────────────────────


def _read_cache(ttl: int) -> dict[str, Any] | None:
    """Read cached update check result if not expired."""
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text())
        if time.time() - data.get("checked_at", 0) > ttl:
            return None
        return data
    except Exception as exc:
        logger.debug("Failed to read update cache: %s", exc)
        return None


def _write_cache(data: dict[str, Any]) -> None:
    """Cache update check result."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data["checked_at"] = time.time()
        CACHE_FILE.write_text(json.dumps(data))
    except Exception as exc:
        logger.debug("Failed to write update cache: %s", exc)
        pass  # cache is best-effort


# ── Check ────────────────────────────────────────────────────────────


async def check_for_update(
    settings: Settings | None = None,
    timeout: float = 5.0,
) -> UpdateInfo:
    """Check the Ember server for a newer CLI version.

    Returns an ``UpdateInfo`` with ``available=True`` if an update exists.
    Never raises — returns an ``UpdateInfo`` with ``error`` set on failure.
    Uses a file cache (TTL from ``settings.update_check_ttl``) to avoid
    repeated network calls.
    """
    current = __version__

    if settings is None:
        from ember_code.core.config.settings import load_settings

        settings = load_settings()

    ttl = settings.update_check_ttl

    # Check cache first
    cached = _read_cache(ttl)
    if cached:
        latest = cached.get("latest_version", "")
        if latest and _is_newer(latest, current):
            return UpdateInfo(
                available=True,
                latest_version=latest,
                current_version=current,
                release_notes=cached.get("release_notes", ""),
                download_url=cached.get("download_url", ""),
            )
        return UpdateInfo(available=False, current_version=current)

    # Fetch from PyPI
    try:
        pypi_url = f"https://pypi.org/pypi/{_PKG_NAME}/json"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(pypi_url)
            resp.raise_for_status()
            data = resp.json()

        latest = data.get("info", {}).get("version", "")
        release_url = data.get("info", {}).get("project_url", "") or _PROJECT_URL

        # Cache the response
        _write_cache({"latest_version": latest, "download_url": release_url})

        if latest and _is_newer(latest, current):
            return UpdateInfo(
                available=True,
                latest_version=latest,
                current_version=current,
                download_url=release_url,
            )
        return UpdateInfo(available=False, current_version=current)

    except Exception as e:
        logger.debug("Update check failed: %s", e)
        return UpdateInfo(available=False, current_version=current, error=str(e))
