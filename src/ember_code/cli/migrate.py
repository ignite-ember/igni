"""``igni migrate`` — bring igni's databases to the current schema.

A backend checks its databases at start and migrates them only when they
are new or behind (:mod:`ember_code.core.db.schema`), so on the desktop
nothing ever needs this. It exists for the cloud, where the database is
prepared before any backend first starts: running it there means every
backend starts with a current schema and never imports alembic
(``docs/CLOUD.md``).

With ``storage.database_url`` set (``IGNI_DATABASE_URL``, or
``--database-url``) that is the one database migrated. Without it, the
project's ``state.db`` and the global ``ember.db`` are.
"""

from __future__ import annotations

from pathlib import Path

import click

from ember_code.core.config.settings import Settings
from ember_code.core.data_paths import canonical_project_dir
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.schema import HEAD_REVISION, SchemaRevision, keeper
from ember_code.core.db.urls import DatabaseUrl


class SchemaMigration:
    """Migrates the databases a :class:`StorageLocator` names, and says how it went."""

    def __init__(self, locator: StorageLocator, project: Path) -> None:
        self._locator = locator
        self._project = project

    def targets(self) -> list[DatabaseUrl]:
        project = self._locator.project_url(self._project)
        shared = self._locator.global_url()
        return [project] if shared == project else [project, shared]

    @staticmethod
    def migrate(url: DatabaseUrl) -> str:
        before = SchemaRevision.of(url)
        keeper.ensure_current(url)
        name = url.file if url.file is not None else url.redacted
        if before == HEAD_REVISION:
            return f"{name}: already at {HEAD_REVISION}"
        return f"{name}: {before or 'new'} -> {HEAD_REVISION}"


@click.command("migrate")
@click.option(
    "--project-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="The project whose database to migrate (default: the current directory)",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="igni's data directory (default: from settings)",
)
@click.option(
    "--database-url",
    default=None,
    help="The database to migrate (default: storage.database_url / IGNI_DATABASE_URL)",
)
def migrate(project_dir: Path | None, data_dir: Path | None, database_url: str | None) -> None:
    """Migrate igni's databases to the current schema."""
    settings = Settings()
    locator = StorageLocator(
        database_url=database_url or settings.storage.database_url,
        data_dir=data_dir if data_dir is not None else settings.storage.data_dir,
    )
    project = canonical_project_dir(project_dir if project_dir is not None else Path.cwd())
    migration = SchemaMigration(locator, project)
    for url in migration.targets():
        click.echo(migration.migrate(url))


__all__ = ["SchemaMigration", "migrate"]
