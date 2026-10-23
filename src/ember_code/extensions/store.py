"""Where installed extensions live between restarts.

``ExtensionInstaller`` takes a bundle directory and makes it run. It
does not remember: restart the backend and it has installed nothing,
because nothing wrote the decision down. That is fine for a test and
useless for a deployment, where "installed" has to mean "still
installed tomorrow".

So an install copies the bundle under ``<data>/extensions/<id>/`` and
the backend reads that directory at boot. Copied rather than linked
to where the author happened to leave it: a bundle that lives in
somebody's ``~/Downloads`` is one ``rm`` away from a deployment that
cannot start its extensions, and a signature over a directory
somebody else can edit is not a signature over anything.

**Validation happens here, before the copy.** A directory that is not
an extension should not become one half-written in the install path,
and an administrator should be told what is wrong with the thing they
pointed at rather than with the thing we made from it.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ember_code.core.paths import home_config_dir
from ember_code.extensions.manifest import ExtensionManifest

logger = logging.getLogger(__name__)

#: Under the data directory. Not configurable yet, and named here so
#: that when it becomes configurable there is one place to change.
EXTENSIONS_DIR = "extensions"


class StoreError(Exception):
    """The bundle will not be stored, and why."""


class ExtensionStore:
    """The installed bundles on disk."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else home_config_dir() / EXTENSIONS_DIR

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, extension_id: str) -> Path:
        """Where this extension's bundle lives, installed or not."""
        if "/" in extension_id or "\\" in extension_id or extension_id in {".", ".."}:
            # The id is reverse-DNS and validated by the manifest, so
            # this cannot happen through the normal path — but it is
            # a directory name built from a string, and the cost of
            # being sure is one line.
            raise StoreError(f"{extension_id!r} is not a usable directory name")
        return self._root / extension_id

    def installed(self) -> list[Path]:
        """Every stored bundle, in a stable order.

        A directory with no readable manifest is skipped with a
        warning rather than raising: one corrupt extension must not
        stop a backend from starting the others.
        """
        if not self._root.is_dir():
            return []
        bundles: list[Path] = []
        for path in sorted(self._root.iterdir()):
            if not path.is_dir():
                continue
            if not (path / "manifest.json").is_file():
                logger.warning("ignoring %s: no manifest.json", path)
                continue
            bundles.append(path)
        return bundles

    def read(self, bundle: Path) -> ExtensionManifest:
        """Validate a bundle's manifest, naming the file on failure."""
        path = bundle / "manifest.json"
        if not path.is_file():
            raise StoreError(f"{bundle} has no manifest.json, so it is not an extension.")
        try:
            return ExtensionManifest.load(path)
        except ValueError as exc:
            raise StoreError(f"{path} is not a valid manifest: {exc}") from exc

    def add(self, bundle: Path, *, replace: bool = False) -> Path:
        """Copy a bundle into the store. Returns where it landed.

        ``replace`` is how an upgrade lands, and it is not the
        default: an install that silently overwrote one would make
        "which version is this" unanswerable, and the grants an
        administrator accepted belong to the version they accepted.
        """
        manifest = self.read(bundle)
        target = self.path_for(manifest.id)

        if target.exists():
            if not replace:
                raise StoreError(
                    f"{manifest.id} is already installed at {target}. "
                    "Pass --replace to upgrade it, which re-asks for its "
                    "permissions."
                )
            shutil.rmtree(target)

        self._root.mkdir(parents=True, exist_ok=True)
        # Copy to a sibling and rename, so an interrupted copy does
        # not leave a half-written bundle that ``installed`` would
        # then try to start at the next boot.
        staging = target.with_name(f".{manifest.id}.incoming")
        if staging.exists():
            shutil.rmtree(staging)
        try:
            shutil.copytree(bundle, staging)
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        logger.info("stored extension %s at %s", manifest.id, target)
        return target

    def remove(self, extension_id: str) -> bool:
        """Delete a stored bundle. ``True`` if there was one."""
        target = self.path_for(extension_id)
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        logger.info("removed extension %s", extension_id)
        return True
