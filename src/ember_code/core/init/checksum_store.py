"""Checksum-based sync for bundled agents / skills.

Owns everything the old flat ``core/init_checksums.py`` module used
to expose as four free functions:

* SHA-256 hashing of a package file (leaf primitive).
* Load / save of the ``.ember/.checksums.json`` map.
* The three-way merge that decides whether a per-file update is a
  clean copy, a legacy record, a byte-stable no-op, an untouched
  overwrite, or a diverged ``.new`` sidecar.

Lands inside the OOP-first :mod:`ember_code.core.init` package
alongside :class:`ProjectInitializer`, :class:`HookProvisioner`,
and :class:`HomeConfigMigrator`. The class owns the checksum map
so the top-level orchestrator no longer plumbs a mutable
``dict[str, str]`` through three free-function calls.

The three-way merge lives in :meth:`ChecksumStore.sync_file`:

- dst missing → copy, record hash → :class:`SyncOutcome` kind
  ``copied``
- dst present, no stored hash → record hash, skip →
  ``recorded_legacy``
- pkg unchanged → skip → ``unchanged``
- pkg changed, dst matches stored hash → overwrite, update hash
  → ``overwritten``
- pkg changed, dst diverged → write ``.new`` sidecar, warn →
  ``diverged``
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.init.json_file import JsonFile
from ember_code.core.init.schemas import InitConfig, SyncOutcome


class ChecksumStore(BaseModel):
    """The ``.ember/.checksums.json`` map for one project.

    Owns the state (the ``entries`` dict), the persistence path
    (derived from ``project_dir`` + ``config.checksums_file``), and
    the three-way-merge algorithm on :meth:`sync_file`.

    Instantiate via :meth:`load` — the classmethod reads the JSON
    from disk (fail-soft) and returns a populated store. Mutate
    via :meth:`sync_file` (one call per built-in file). Persist via
    :meth:`save`. The orchestrator no longer threads a mutable
    ``dict[str, str]`` through three free-function calls.

    :meth:`file_hash` is a :func:`staticmethod` — the SHA-256 leaf
    primitive is stateless (Rule 6 leaf-function exception) but
    housed on the owning class so callers don't need to import a
    second symbol.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_dir: Path
    config: InitConfig
    entries: dict[str, str] = Field(default_factory=dict)

    # ── Construction ──────────────────────────────────────────────

    @classmethod
    def load(cls, project_dir: Path, config: InitConfig) -> ChecksumStore:
        """Read ``.ember/<config.checksums_file>`` and return a
        populated store.

        Missing / unparseable file → empty ``entries`` (the underlying
        :class:`JsonFile` returns ``{}`` on both cases). The full
        :class:`InitConfig` is retained on the instance so
        :meth:`save` can round-trip the same filename without an
        extra caller-side argument.
        """
        path = project_dir / ".ember" / config.checksums_file
        raw = JsonFile(path=path).load()
        return cls(project_dir=project_dir, config=config, entries=dict(raw))

    # ── Persistence ───────────────────────────────────────────────

    def _json_file(self) -> JsonFile:
        """Compose the checksums-file path into a :class:`JsonFile` once.

        Both :meth:`load` and :meth:`save` derive the same path from
        ``project_dir + .ember + config.checksums_file`` — routing
        through this helper keeps the path build in one place.
        """
        return JsonFile(
            path=self.project_dir / ".ember" / self.config.checksums_file,
        )

    def save(self) -> None:
        """Write ``entries`` back to
        ``.ember/<config.checksums_file>``.
        """
        self._json_file().save(self.entries)

    # ── Leaf primitive ────────────────────────────────────────────

    @staticmethod
    def file_hash(path: Path) -> str:
        """Compute the truncated SHA-256 hash of a file's content.

        Stateless leaf primitive — housed on the class so the
        merge algorithm and any future caller share one symbol.
        """
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    # ── Three-way merge ───────────────────────────────────────────

    def sync_file(self, src: Path, dst: Path, key: str) -> SyncOutcome:
        """Sync one built-in file. Mutates :attr:`entries` in place.

        Logic (see module docstring for the full table):

        * dst doesn't exist → copy, record checksum → ``copied``
        * no stored checksum (legacy) → record current package hash,
          skip update → ``recorded_legacy``
        * package unchanged → skip → ``unchanged``
        * package changed + user didn't modify → overwrite, update
          checksum → ``overwritten``
        * package changed + user modified → skip, write ``.new``
          sidecar → ``diverged`` (the only outcome that carries a
          warning message)
        """
        pkg_hash = self.file_hash(src)
        stored_hash = self.entries.get(key)

        if not dst.exists():
            # New file — copy and record.
            shutil.copy2(src, dst)
            self.entries[key] = pkg_hash
            return SyncOutcome(kind="copied", key=key)

        if stored_hash is None:
            # Legacy: file exists but no checksum recorded. Record
            # the current package hash so future updates work.
            self.entries[key] = pkg_hash
            return SyncOutcome(kind="recorded_legacy", key=key)

        if pkg_hash == stored_hash:
            # Package hasn't changed — nothing to do.
            return SyncOutcome(kind="unchanged", key=key)

        # Package has changed — check if the user modified their copy.
        local_hash = self.file_hash(dst)

        if local_hash == stored_hash:
            # User hasn't touched it — safe to overwrite.
            shutil.copy2(src, dst)
            self.entries[key] = pkg_hash
            return SyncOutcome(kind="overwritten", key=key)

        # User modified AND package updated — write the new version
        # alongside so the user can diff at their convenience.
        sidecar_path = dst.with_suffix(dst.suffix + ".new")
        shutil.copy2(src, sidecar_path)
        self.entries[key] = pkg_hash
        return SyncOutcome(
            kind="diverged",
            key=key,
            sidecar_path=sidecar_path,
        )
