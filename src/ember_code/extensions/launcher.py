"""Starting what the store holds, and restarting one that changed.

Two callers start extensions. The backend's boot starts everything
already in the store; the distribution sync starts one it has just
stored, and stops one it is about to replace or remove. Both go
through here and so through the same installer — the ordinary boot
path, with its trust, profile and resolution checks — because a
second path for extensions that arrived while igni was running would
be a second place for one of those checks to go missing.

Failures are logged and recorded, never raised: one extension that
will not start must not stop the backend or a sync, and the registry
records why so the UI can say it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.extensions.contributions import ExtensionContributions
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.store import ExtensionStore, StoreError

logger = logging.getLogger(__name__)


class ExtensionLauncher:
    """Starts and stops stored extensions through the installer."""

    def __init__(
        self,
        *,
        store: ExtensionStore,
        installer: ExtensionInstaller,
        registry: ExtensionRegistry,
        contributions: ExtensionContributions | None = None,
    ) -> None:
        self._store = store
        self._installer = installer
        self._registry = registry
        self._contributions = contributions

    async def start_installed(self) -> None:
        """Start every bundle in the store."""
        for bundle in self._store.installed():
            try:
                extension_id = self._store.read(bundle).id
            except StoreError as exc:
                logger.error("extension at %s was not installed: %s", bundle, exc)
                continue
            await self.activate(extension_id, bundle)

    async def activate(self, extension_id: str, bundle: Path) -> None:
        """Start one, replacing a running copy."""
        if self._registry.get(extension_id) is not None:
            await self._installer.uninstall(extension_id)
        if self._contributions is not None:
            # Where it came from, for a `source: file` fragment.
            # Recorded before the install so a fragment resolved
            # during it can find the bundle.
            self._contributions.note_bundle(extension_id, bundle)
        try:
            entry = await self._installer.install(
                bundle, distribution=self._store.distribution_of(extension_id)
            )
        except Exception as exc:  # noqa: BLE001 — one extension must not stop the rest
            # Refused before registration — a bad manifest, a
            # development install without the flag. There is no
            # registry entry to carry the reason, so the log is the
            # only record.
            logger.error("extension at %s was not installed: %s", bundle, exc)
            return
        if entry.state is not ExtensionState.READY:
            logger.warning("extension %s is %s: %s", entry.id, entry.state.value, entry.reason)

    async def deactivate(self, extension_id: str) -> None:
        """Stop one, if it is running."""
        try:
            await self._installer.uninstall(extension_id)
        except Exception:  # noqa: BLE001 — stopping is best effort
            logger.exception("stopping extension %s failed", extension_id)
