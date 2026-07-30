"""Programmatic alembic upgrade — owned by a :class:`Migrator` instance.

Why programmatic vs. shell-out: callers (tests, the BE process) need a
synchronous "ensure schema is current" hook. Shelling out to ``alembic
upgrade head`` would couple us to a working CWD and the alembic CLI on
PATH; the Python API works from anywhere our package is importable.

State + behaviour live together on :class:`Migrator`:

* ``self._lock`` guards the per-path idempotency cache so concurrent
  ``Database(...)`` constructions on the same file don't race two
  ``alembic upgrade head`` runs.
* ``self._upgraded_paths`` short-circuits repeat upgrades of the same
  resolved path within a single process.
* ``self._package_root`` (defaulting to ``Path(__file__).resolve().parents[2]``
  → the ``ember_code`` package directory) is where ``alembic.ini`` and
  ``migrations/`` are looked up. Tests can point a fresh ``Migrator`` at
  a fixture directory to assert the botched-install error path without
  monkeypatching module globals.

Production callers use the module-level ``migrator`` singleton exposed
below (and the thin :func:`upgrade_to_head` shim that delegates to it,
so ``database.py`` needs no edit). Per-instance ``Migrator()``
construction is for TEST ISOLATION ONLY — mixing a fresh ``Migrator``
with the singleton on the same DB path would run ``alembic upgrade
head`` twice (benign, since alembic no-ops on an already-upgraded DB,
but wasted work).

Tests that construct ``Migrator(package_root=tmp_path)`` must physically
place an ``alembic.ini`` + ``migrations/`` tree under that root —
alembic's :class:`Config` reads the ini from disk, so pointing at a
directory that lacks these files trips the same :class:`FileNotFoundError`
that a botched install would raise in production.
"""

from __future__ import annotations

import threading
from pathlib import Path

from alembic import command
from alembic.config import Config

from ember_code.core.db.engine import sync_url


class Migrator:
    """Coordinator for ``alembic upgrade head`` runs against SQLite files.

    Owns the per-path idempotency cache, the lock guarding it, and the
    on-disk locations of the packaged ``alembic.ini`` + ``migrations/``
    tree. See the module docstring for the "singleton in production,
    fresh instance in tests" caveat.
    """

    def __init__(self, package_root: Path | None = None) -> None:
        # ``alembic.ini`` and ``migrations/`` live inside the
        # ``ember_code`` package (``src/ember_code/alembic.ini``,
        # ``src/ember_code/migrations/``) so non-source-tree installs
        # (Homebrew, pipx, system pip) ship them as package data.
        # ``parents[2]`` walks ``db/migrations.py → db → core → ember_code``.
        self._package_root: Path = (
            package_root if package_root is not None else Path(__file__).resolve().parents[2]
        )
        self._lock = threading.Lock()
        self._upgraded_paths: set[str] = set()

    def _locate_alembic_ini(self) -> Path:
        """Locate ``alembic.ini`` inside the package; validate ``migrations/``.

        Both files ship as package data; the in-package layout is the
        only supported one. Raises if either is missing so a botched
        install fails loudly instead of silently re-running migrations
        against a half-set-up DB.

        Lazy (called from :meth:`upgrade_to_head`) so a botched install
        only blows up when a caller actually needs to run a migration.
        """
        alembic_ini = self._package_root / "alembic.ini"
        migrations_dir = self._package_root / "migrations"
        if alembic_ini.exists() and migrations_dir.is_dir():
            return alembic_ini
        raise FileNotFoundError(
            f"alembic.ini and migrations/ missing from package at {self._package_root}. "
            "This usually means the wheel was built without package-data — "
            "reinstall via `pip install --force-reinstall ignite-ember` or "
            "`brew reinstall ignite-ember`."
        )

    def upgrade_to_head(self, db_path: str | Path) -> None:
        """Run alembic ``upgrade head`` against the SQLite file at ``db_path``.

        Idempotent and cached per resolved path so multiple constructions in
        the same process don't re-run migrations.
        """
        resolved_path = Path(str(db_path)).expanduser().resolve()
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved = str(resolved_path)
        with self._lock:
            if resolved in self._upgraded_paths:
                return

            ini_path = self._locate_alembic_ini()
            cfg = Config(str(ini_path))
            cfg.set_main_option("sqlalchemy.url", sync_url(resolved))
            command.upgrade(cfg, "head")
            self._upgraded_paths.add(resolved)


# Module-level singleton — production-wide shared cache. Tests and
# callers that need isolation should construct their own ``Migrator()``
# instead of touching this one.
migrator = Migrator()


def upgrade_to_head(db_path: str | Path) -> None:
    """Module-level shim delegating to the singleton :data:`migrator`.

    Kept as a ``def`` (not a bound-method assignment) so tests can
    replace ``migrator`` and the shim still routes through the current
    singleton. This mirrors the free-function surface preserved by
    :mod:`ember_code.core.db.engine` around :class:`EngineRegistry`.
    """
    migrator.upgrade_to_head(db_path)
