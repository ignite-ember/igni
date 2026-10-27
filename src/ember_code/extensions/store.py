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

**Where a bundle came from is written down beside it.** A bundle igni
server distributed — its signature checked against the approved
service's key — is recorded in ``<id>.distribution.json``; a bundle
with no record was installed for development. The installer decides
trust from that, and the distribution sync decides what to update
and what to remove from it, so a development install is never
overwritten or removed by a sync.

A ``python``-packaged extension gets a private environment under
``.python/<id>/<version>/``. It belongs to the bundle: replacing or
removing the bundle removes it.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from ember_code.core.layer import Layers
from ember_code.core.paths import home_config_dir
from ember_code.extensions.assets import AssetCache, ReadOnlyTree
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.settings_values import SettingsValues

logger = logging.getLogger(__name__)

#: Under the data directory. Not configurable yet, and named here so
#: that when it becomes configurable there is one place to change.
EXTENSIONS_DIR = "extensions"


#: Inside the store, and dot-named so :meth:`ExtensionStore.installed`
#: never mistakes it for a bundle.
ENVIRONMENTS_DIR = ".python"

#: Inside the store, dot-named for the same reason: one directory per
#: extension that is its alone, kept across reinstalls.
DATA_DIR = ".data"

#: What an extension's process is told that directory is.
DATA_DIR_ENV = "IGNI_EXTENSION_DATA"

#: Inside the store, dot-named: assets downloaded for installed
#: extensions, one file per digest (:class:`~ember_code.extensions.assets.AssetCache`).
ASSETS_DIR = ".assets"

#: Beside a bundle: where it came from, when igni distributed it.
DISTRIBUTION_SUFFIX = ".distribution.json"

#: The super admin's priority order of extensions, as the last
#: distribution listing carried it.
PRIORITY_FILE = ".priority.json"

#: The signed-in person's group, as the last distribution listing
#: carried it: who this machine's extensions were installed for.
GROUP_FILE = ".group.json"


class StoreError(Exception):
    """The bundle will not be stored, and why."""


class DistributionRecord(BaseModel):
    """A bundle igni server distributed, as it was verified.

    Written only after the signature checked out against the
    publisher's key and the archive's digest matched it, so its
    presence is what "distributed by igni" means on this machine.
    """

    extension_id: str
    version: str
    sha256: str
    #: The service the extension belongs to — and the audience of the
    #: token the host fetches for it.
    service_url: str
    published_at: datetime | None = None


class StoredPriority(BaseModel):
    #: The super admin's order of extensions, highest first. It settles
    #: every collision, and is the order edits apply and extensions' hooks run in.
    priority: list[str] = Field(default_factory=list)


class StoredGroup(BaseModel):
    #: The one group the signed-in person belongs to in their
    #: organisation, read by igni server when it listed what they get
    #: (its §8.3); ``None`` when they are in none.
    group: str | None = None


class ExtensionStore:
    """The installed bundles on disk.

    Where the layer keeps no files of it (the cloud's) what igni server sends with the
    listing — the priority order and the person's group — is kept in this
    process only: it is igni server's, read-only in the VM, and a VM
    starts by asking again. Settings values live in the database
    (:mod:`ember_code.extensions.settings_values`).
    """

    #: What the listing carried, in the cloud: by store root.
    _received_priority: ClassVar[dict[str, StoredPriority]] = {}
    _received_group: ClassVar[dict[str, StoredGroup]] = {}

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else home_config_dir() / EXTENSIONS_DIR
        self._values: SettingsValues | None = None

    @property
    def settings_values(self) -> SettingsValues:
        if self._values is None:
            self._values = SettingsValues.for_store(self._root)
        return self._values

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

    @property
    def asset_cache(self) -> AssetCache:
        """Assets on this machine, shared by every extension, by digest."""
        return AssetCache(self._root / ASSETS_DIR)

    def prune_assets(self) -> None:
        """Forget the assets no installed extension declares any more."""
        keep: set[str] = set()
        for bundle in self.installed():
            try:
                keep |= {asset.sha256 for asset in self.read(bundle).assets}
            except StoreError:
                continue
        self.asset_cache.prune(keep)

    @property
    def environments_root(self) -> Path:
        """Where ``python``-packaged extensions get their environments."""
        return self._root / ENVIRONMENTS_DIR

    def installed(self) -> list[Path]:
        """Every stored bundle, in a stable order.

        A directory with no readable manifest is skipped with a
        warning rather than raising: one corrupt extension must not
        stop a backend from starting the others. Dot-named ones are
        the store's own — staging, environments — and skipped quietly.
        """
        if not self._root.is_dir():
            return []
        bundles: list[Path] = []
        for path in sorted(self._root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
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

    def add(
        self,
        bundle: Path,
        *,
        replace: bool = False,
        distribution: DistributionRecord | None = None,
    ) -> Path:
        """Copy a bundle into the store. Returns where it landed.

        ``replace`` is how an upgrade lands, and it is not the
        default: an install that silently overwrote one would make
        "which version is this" unanswerable, and the grants an
        administrator accepted belong to the version they accepted.

        ``distribution`` is the verified record when igni server
        distributed this bundle. Without one the bundle is a
        development install, and any record a previous distributed
        copy left is removed with it.
        """
        manifest = self.read(bundle)
        target = self.path_for(manifest.id)

        if target.exists() and not replace:
            raise StoreError(
                f"{manifest.id} is already installed at {target}. "
                "Pass --replace to upgrade it, which re-asks for its "
                "permissions."
            )

        self._root.mkdir(parents=True, exist_ok=True)
        # Copy to a sibling and rename, so an interrupted copy does
        # not leave a half-written bundle that ``installed`` would
        # then try to start at the next boot — and, on an upgrade,
        # does not cost the version that works: the old bundle is set
        # aside only once the new one is complete, and put back if the
        # swap fails.
        staging = target.with_name(f".{manifest.id}.incoming")
        retired = target.with_name(f".{manifest.id}.outgoing")
        for leftover in (staging, retired):
            if leftover.exists():
                ReadOnlyTree.remove(leftover)
        try:
            shutil.copytree(bundle, staging)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if target.exists():
            target.rename(retired)
        try:
            staging.rename(target)
        except Exception:
            if retired.exists():
                retired.rename(target)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # Its assets were laid out read-only.
        ReadOnlyTree.remove(retired)

        # The environment was built from the bundle just replaced.
        self.drop_environments(manifest.id)
        if distribution is not None:
            self.record_distribution(distribution)
        else:
            self.distribution_path(manifest.id).unlink(missing_ok=True)

        logger.info("stored extension %s at %s", manifest.id, target)
        return target

    # ── Provenance ─────────────────────────────────────────────────

    def distribution_path(self, extension_id: str) -> Path:
        self.path_for(extension_id)  # the same name check
        return self._root / f"{extension_id}{DISTRIBUTION_SUFFIX}"

    def record_distribution(self, record: DistributionRecord) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self.distribution_path(record.extension_id).write_text(
            record.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )

    def distribution_of(self, extension_id: str) -> DistributionRecord | None:
        """How igni distributed this extension, or ``None`` when it
        did not — a development install, or nothing installed.

        An unreadable record reads as none, which makes the bundle a
        development install: the safe direction, because that needs
        the dev flag to start.
        """
        path = self.distribution_path(extension_id)
        if not path.is_file():
            return None
        try:
            return DistributionRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            logger.warning("ignoring %s: %s", path, exc)
            return None

    def distributed(self) -> list[DistributionRecord]:
        """Every extension igni distributed to this machine."""
        if not self._root.is_dir():
            return []
        records: list[DistributionRecord] = []
        for path in sorted(self._root.glob(f"*{DISTRIBUTION_SUFFIX}")):
            record = self.distribution_of(path.name.removesuffix(DISTRIBUTION_SUFFIX))
            if record is not None:
                records.append(record)
        return records

    # ── Priority ───────────────────────────────────────────────────

    @property
    def priority_path(self) -> Path:
        return self._root / PRIORITY_FILE

    def record_priority(self, priority: list[str]) -> bool:
        """Keep the priority order the listing carried. ``True`` if it changed.

        Kept so the next start applies it before igni server has been
        asked anything: a machine that cannot reach igni still settles
        collisions the way the super admin ordered.
        """
        stored = StoredPriority(priority=priority)
        if self._stored() == stored:
            return False
        if not Layers.current().keeps_listing_in_files():
            self._received_priority[str(self._root)] = stored
            return True
        self._root.mkdir(parents=True, exist_ok=True)
        self.priority_path.write_text(stored.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return True

    def priority(self) -> list[str]:
        """The priority order as last received, highest first. Empty, if nothing was."""
        return self._stored().priority

    def _stored(self) -> StoredPriority:
        if not Layers.current().keeps_listing_in_files():
            return self._received_priority.get(str(self._root), StoredPriority())
        path = self.priority_path
        if not path.is_file():
            return StoredPriority()
        try:
            return StoredPriority.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            logger.warning("ignoring %s: %s", path, exc)
            return StoredPriority()

    @property
    def group_path(self) -> Path:
        return self._root / GROUP_FILE

    def record_group(self, group: str | None) -> None:
        """Keep the person's group the listing carried, for the next start before igni answers."""
        if not Layers.current().keeps_listing_in_files():
            self._received_group[str(self._root)] = StoredGroup(group=group)
            return
        self._root.mkdir(parents=True, exist_ok=True)
        stored = StoredGroup(group=group)
        self.group_path.write_text(stored.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def group(self) -> StoredGroup | None:
        """The person's group as last received; ``None`` if no listing ever arrived."""
        if not Layers.current().keeps_listing_in_files():
            return self._received_group.get(str(self._root))
        path = self.group_path
        if not path.is_file():
            return None
        try:
            return StoredGroup.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            logger.warning("ignoring %s: %s", path, exc)
            return None

    def drop_environments(self, extension_id: str) -> None:
        """Remove every private environment built for this extension."""
        target = self.environments_root / extension_id
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    # ── Settings ───────────────────────────────────────────────────
    #
    # `contributes.settings` describes configuration the host renders
    # and the deployment stores. Beside the bundle rather than inside
    # it: the bundle is what was signed, and a value an administrator
    # typed is not part of what a vendor attested to.

    def settings_path(self, extension_id: str) -> Path:
        return self._root / f"{extension_id}.settings.json"

    def settings(self, manifest: ExtensionManifest) -> dict[str, object]:
        """This extension's settings, defaults filled in.

        A key the manifest no longer declares is dropped rather than
        passed through: an extension that removed a setting should
        stop being handed it, and a stored value for a key nobody
        declares is how a stale option survives three upgrades.
        """
        declared = {setting.key: setting for setting in manifest.contributes.settings}
        if not declared:
            return {}

        stored = self.settings_values.read(manifest.id)

        return {
            key: stored.get(key, setting.default)
            for key, setting in declared.items()
            if stored.get(key, setting.default) is not None
        }

    def save_settings(
        self, manifest: ExtensionManifest, values: dict[str, object]
    ) -> dict[str, object]:
        """Store values for the keys this extension declares.

        Undeclared keys are refused rather than ignored. A settings
        write is an administrator acting on what the host showed
        them, so a key that was not on that form did not come from
        there.
        """
        declared = {setting.key: setting for setting in manifest.contributes.settings}
        unknown = sorted(set(values) - set(declared))
        if unknown:
            raise StoreError(
                f"{manifest.id} does not declare {unknown}. Its settings are "
                f"{sorted(declared) or 'none'}."
            )
        for key, setting in declared.items():
            if key in values and setting.type == "enum" and values[key] not in setting.options:
                raise StoreError(f"{manifest.id}: {key!r} must be one of {setting.options}")

        merged = {**self.settings(manifest), **values}
        self.settings_values.write(manifest.id, merged)
        return merged

    def remove(self, extension_id: str) -> bool:
        """Delete a stored bundle. ``True`` if there was one.

        What belongs to it goes even when the bundle itself is already
        gone — a record or an environment left over from a bundle
        somebody deleted by hand would otherwise outlive it forever.
        """
        target = self.path_for(extension_id)
        self.distribution_path(extension_id).unlink(missing_ok=True)
        self.drop_environments(extension_id)
        if not target.is_dir():
            return False
        ReadOnlyTree.remove(target)
        # Its settings go with it. Leaving them would mean
        # reinstalling an extension silently inherited whatever the
        # last administrator had configured.
        self.settings_values.delete(extension_id)
        logger.info("removed extension %s", extension_id)
        return True
