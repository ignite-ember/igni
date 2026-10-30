"""``igni-cloud db`` (or ``python -m igni_cloud.db.admin_cli``): producers' database roles.

``docs/CLOUD.md`` section 9. Each producer connects to its org's
database as its own PostgreSQL role, so row-level security holds even if
igni inside its VM is compromised (``core/db/rls.py``). The node agent
or igni server runs these with the admin role's URL:

* ``grant-producer --org O --producer P --incarnation I [--owner app:<id>]``
  creates the incarnation's role (or rotates its password) and prints, as
  JSON, the credentials and the ``IGNI_DATABASE_URL`` to hand its VM;
* ``revoke-producer --org O --producer P --incarnation I`` drops the role;
  the rows stay, for the org's retention period;
* ``export-producer --org O --producer P --incarnation I --out FILE`` writes
  the incarnation's data as a portable zip, for an org admin during
  retention, and prints its manifest;
* ``erase-producer --org O --producer P --incarnation I`` deletes every row
  and upload of the incarnation and drops its role, and prints what it
  removed, for the audit. Idempotent.

The incarnation is which life of the producer (igni server's
``service-identity.md`` §11.3): a person's membership of the org, an
app's or a space's own id (:class:`~igni_cloud.producer.ProducerIncarnation`).

Its own small module, because the node agent runs it on the path of a new
producer's first start: it imports click, SQLAlchemy and the role code,
not the rest of igni. The admin URL comes from ``--database-url`` or
``IGNI_DATABASE_URL``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from click.decorators import FC

from ember_code.core.db.urls import DatabaseUrl
from igni_cloud.db.producer_data import AdminCannotSeeRows, ProducerData
from igni_cloud.db.rls import ProducerRoles
from igni_cloud.producer import ProducerIncarnation, ProducerRef, UnknownProducer

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
    def parse(producer: str, incarnation: str, owner: str | None = None) -> ProducerIncarnation:
        try:
            return ProducerIncarnation.of(ProducerRef.parse(producer, owner=owner), incarnation)
        except UnknownProducer as exc:
            raise click.BadParameter(
                str(exc), param_hint="--producer / --incarnation / --owner"
            ) from exc


class Options:
    """The options the commands share."""

    @staticmethod
    def org(fn: FC) -> FC:
        return click.option("--org", required=True)(fn)

    @staticmethod
    def producer(fn: FC) -> FC:
        """``--producer`` and ``--incarnation``: one life of one producer."""
        fn = click.option(
            "--incarnation",
            required=True,
            help="Which life of the producer: a person's membership id, an app's or space's id",
        )(fn)
        return click.option(
            "--producer", required=True, help="`person:<id>`, `app:<id>` or `space:<id>`"
        )(fn)

    @staticmethod
    def database_url(fn: FC) -> FC:
        return click.option("--database-url", default=None, help=_ADMIN_URL_HELP)(fn)


@db.command("grant-producer")
@Options.org
@Options.producer
@click.option("--owner", default=None, help="A space's app: `app:<id>`")
@Options.database_url
def grant_producer(
    org: str, producer: str, incarnation: str, owner: str | None, database_url: str | None
) -> None:
    """Create an incarnation's role (or rotate its password); print its credentials."""
    named = ProducerArgument.parse(producer, incarnation, owner)
    credentials = ProducerRoles(AdminUrl.resolve(database_url)).grant(org, named)
    printed = credentials.model_dump(mode="json")
    printed.update(producer=named.producer.ref, incarnation=named.incarnation)
    if named.producer.owner:
        printed["owner"] = named.producer.owner
    click.echo(json.dumps(printed))


@db.command("revoke-producer")
@Options.org
@Options.producer
@Options.database_url
def revoke_producer(org: str, producer: str, incarnation: str, database_url: str | None) -> None:
    """Drop an incarnation's role; its rows stay until erased."""
    named = ProducerArgument.parse(producer, incarnation)
    dropped = ProducerRoles(AdminUrl.resolve(database_url)).revoke(org, named)
    click.echo("dropped" if dropped else "no such role")


@db.command("export-producer")
@Options.org
@Options.producer
@click.option(
    "--out",
    required=True,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="The zip to write: manifest.json, rows.jsonl, attachments/",
)
@Options.database_url
def export_producer(
    org: str, producer: str, incarnation: str, out: Path, database_url: str | None
) -> None:
    """Write an incarnation's data to a portable zip; print its manifest."""
    named = ProducerArgument.parse(producer, incarnation)
    try:
        manifest = ProducerData(AdminUrl.resolve(database_url)).export(org, named, out)
    except AdminCannotSeeRows as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(manifest.model_dump_json())


@db.command("erase-producer")
@Options.org
@Options.producer
@Options.database_url
def erase_producer(org: str, producer: str, incarnation: str, database_url: str | None) -> None:
    """Delete an incarnation's rows and uploads, drop its role; print what went."""
    named = ProducerArgument.parse(producer, incarnation)
    try:
        summary = ProducerData(AdminUrl.resolve(database_url)).erase(org, named)
    except AdminCannotSeeRows as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(summary.model_dump_json())


def main() -> None:
    db(prog_name="igni-cloud db")


if __name__ == "__main__":
    main()
