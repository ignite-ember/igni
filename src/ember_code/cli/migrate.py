"""``igni migrate`` — bring a project's database to the current schema.

A backend checks its database at start and migrates it only when it is
new or behind (:mod:`ember_code.core.db.schema`), so on the desktop
nothing ever needs this. It exists for the cloud, where a producer's
volume is prepared before its backend first starts: running it there
means every backend starts with a current database and never imports
alembic (``docs/CLOUD.md``).
"""

from __future__ import annotations

from pathlib import Path

import click

from ember_code.core.config.settings import Settings
from ember_code.core.data_paths import state_db_path
from ember_code.core.db.schema import HEAD_REVISION, SchemaRevision, keeper


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
def migrate(project_dir: Path | None, data_dir: Path | None) -> None:
    """Migrate a project's database to the current schema."""
    project = project_dir if project_dir is not None else Path.cwd()
    data = data_dir if data_dir is not None else Path(Settings().storage.data_dir)
    path = state_db_path(project, data_dir=data)
    before = SchemaRevision.of(path)
    keeper.ensure_current(path)
    if before == HEAD_REVISION:
        click.echo(f"{path}: already at {HEAD_REVISION}")
    else:
        click.echo(f"{path}: {before or 'new'} -> {HEAD_REVISION}")


__all__ = ["migrate"]
