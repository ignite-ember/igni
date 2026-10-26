"""``igni db`` — producers' database roles, for the platform to call.

``docs/CLOUD.md`` section 9. Each producer connects to its org's
database as its own PostgreSQL role, so row-level security holds even if
igni inside its VM is compromised (``core/db/rls.py``). The node agent
or igni server runs these with the admin role's URL:

* ``igni db grant-producer --org O --producer P`` creates the role (or
  rotates its password) and prints, as JSON, the credentials and the
  ``IGNI_DATABASE_URL`` to hand the producer's VM;
* ``igni db revoke-producer --org O --producer P`` drops it.
"""

from __future__ import annotations

import click

from ember_code.core.config.settings import Settings
from ember_code.core.db.rls import ProducerRoles
from ember_code.core.db.urls import DatabaseUrl

_ADMIN_URL_HELP = (
    "The database's admin role URL (default: storage.database_url / IGNI_DATABASE_URL)"
)


class AdminUrl:
    @staticmethod
    def resolve(database_url: str | None) -> DatabaseUrl:
        raw = database_url or Settings().storage.database_url
        if not raw:
            raise click.UsageError("name the database: --database-url or IGNI_DATABASE_URL")
        return DatabaseUrl.parse(raw)


@click.group("db")
def db() -> None:
    """Producers' roles on the cloud's database."""


@db.command("grant-producer")
@click.option("--org", required=True)
@click.option("--producer", required=True, help="`person:<id>` or `app:<id>`")
@click.option("--database-url", default=None, help=_ADMIN_URL_HELP)
def grant_producer(org: str, producer: str, database_url: str | None) -> None:
    """Create a producer's role (or rotate its password); print its credentials."""
    credentials = ProducerRoles(AdminUrl.resolve(database_url)).grant(org, producer)
    click.echo(credentials.model_dump_json())


@db.command("revoke-producer")
@click.option("--org", required=True)
@click.option("--producer", required=True)
@click.option("--database-url", default=None, help=_ADMIN_URL_HELP)
def revoke_producer(org: str, producer: str, database_url: str | None) -> None:
    """Drop a producer's role."""
    dropped = ProducerRoles(AdminUrl.resolve(database_url)).revoke(org, producer)
    click.echo("dropped" if dropped else "no such role")


__all__ = ["db"]
