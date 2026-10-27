"""``igni-cloud db`` (or ``python -m igni_cloud.db.admin_cli``): producers' database roles.

``docs/CLOUD.md`` section 9. Each producer connects to its org's
database as its own PostgreSQL role, so row-level security holds even if
igni inside its VM is compromised (``core/db/rls.py``). The node agent
or igni server runs these with the admin role's URL:

* ``grant-producer --org O --producer P [--owner app:<id>]`` creates the role (or
  rotates its password) and prints, as JSON, the credentials and the
  ``IGNI_DATABASE_URL`` to hand the producer's VM;
* ``revoke-producer --org O --producer P`` drops it.

Its own small module, because the node agent runs it on the path of a new
producer's first start: it imports click, SQLAlchemy and the role code,
not the rest of igni. The admin URL comes from ``--database-url`` or
``IGNI_DATABASE_URL``.
"""

from __future__ import annotations

import json
import os

import click

from ember_code.core.db.urls import DatabaseUrl
from igni_cloud.db.rls import ProducerRoles
from igni_cloud.producer import ProducerRef, UnknownProducer

#: The variable that names the database (``StorageConfig.DATABASE_URL_ENV``).
DATABASE_URL_ENV = "IGNI_DATABASE_URL"

_ADMIN_URL_HELP = f"The database's admin role URL (default: {DATABASE_URL_ENV})"


class AdminUrl:
    @staticmethod
    def resolve(database_url: str | None) -> DatabaseUrl:
        raw = database_url or os.environ.get(DATABASE_URL_ENV, "").strip()
        if not raw:
            raise click.UsageError(f"name the database: --database-url or {DATABASE_URL_ENV}")
        return DatabaseUrl.parse(raw)


@click.group("db")
def db() -> None:
    """Producers' roles on the cloud's database."""


class ProducerArgument:
    @staticmethod
    def parse(producer: str, owner: str | None) -> ProducerRef:
        try:
            return ProducerRef.parse(producer, owner=owner)
        except UnknownProducer as exc:
            raise click.BadParameter(str(exc), param_hint="--producer / --owner") from exc


@db.command("grant-producer")
@click.option("--org", required=True)
@click.option("--producer", required=True, help="`person:<id>`, `app:<id>` or `space:<id>`")
@click.option("--owner", default=None, help="A space's app: `app:<id>`")
@click.option("--database-url", default=None, help=_ADMIN_URL_HELP)
def grant_producer(org: str, producer: str, owner: str | None, database_url: str | None) -> None:
    """Create a producer's role (or rotate its password); print its credentials."""
    ref = ProducerArgument.parse(producer, owner)
    credentials = ProducerRoles(AdminUrl.resolve(database_url)).grant(org, ref.ref)
    printed = credentials.model_dump(mode="json")
    if ref.owner:
        printed["owner"] = ref.owner
    click.echo(json.dumps(printed))


@db.command("revoke-producer")
@click.option("--org", required=True)
@click.option("--producer", required=True)
@click.option("--database-url", default=None, help=_ADMIN_URL_HELP)
def revoke_producer(org: str, producer: str, database_url: str | None) -> None:
    """Drop a producer's role."""
    dropped = ProducerRoles(AdminUrl.resolve(database_url)).revoke(org, producer)
    click.echo("dropped" if dropped else "no such role")


def main() -> None:
    db(prog_name="igni-cloud db")


if __name__ == "__main__":
    main()
