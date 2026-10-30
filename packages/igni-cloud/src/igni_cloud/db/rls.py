"""Row-level security in the cloud: a producer's connection sees its rows only.

``docs/CLOUD.md`` section 9. The application already filters every query
by org, producer and project (:class:`~ember_code.core.db.scope.StorageScope`).
This is the second layer, held by PostgreSQL itself, so it holds when igni
inside a producer's VM is buggy or compromised:

* **Each incarnation of a producer connects as its own role**, minted by
  the platform (:class:`ProducerRoles`, ``igni-cloud db grant-producer``)
  and handed to the VM like its token. A role belongs to one producer's
  incarnation through ``producer_roles(role_name, org, producer,
  incarnation)``, a table no producer role can
  read or write; the policies reach it only through ``SECURITY DEFINER``
  functions keyed on ``session_user`` — the role that logged in, which
  ``SET ROLE`` does not change and only a superuser's ``SET SESSION
  AUTHORIZATION`` could. (Inside a ``SECURITY DEFINER`` function
  ``current_user`` is the function's owner, so it would not do.) A setting the VM could ``SET`` would not do:
  igni compromised in the VM would set it to someone else.

  A role per incarnation, not one per producer told its incarnation: a
  person removed and added again is a new incarnation, and their new
  VM's credentials must not reach the old one's rows, kept for the org's
  retention period — nor the old credentials, should they leak, the new
  rows. The role is what the VM cannot change, so it carries the
  incarnation; the old role is dropped when the person is removed
  (``revoke-producer``) and the old rows stay, out of everyone's reach,
  until ``erase-producer``.

  The mapping table rather than role names that spell org and producer:
  org and producer ids are free-form and may not fit a role name (63
  bytes, quoting), and an opaque name can be rotated without the data
  changing.
* **Every igni-owned table** (each with ``org`` and ``producer`` columns)
  has ``ENABLE`` and ``FORCE ROW LEVEL SECURITY`` and a policy comparing the
  row's org and producer with the connection's, for reading and writing.
  The row's producer is the incarnation's
  (:attr:`~igni_cloud.producer.ProducerIncarnation.stored`,
  ``person:<id>@<incarnation>``), and so is the connection's.
  The project stays an application filter: a person sees all their
  projects.
* **Agno's tables** (schema ``ai``) carry the scope in ``user_id``
  (``<org>/<producer>@<incarnation>/<project>``, :mod:`igni_cloud.storage.scoped_agno`);
  their policy compares its prefix. Agno's schema-version table is
  readable; its other tables, which igni does not use, are closed.
* **Large objects** are not rows of a table: they are protected by their
  owner. A producer's role owns what it uploads, ``lo_compat_privileges``
  stays off, and nothing grants another role access — and the
  ``attachments`` row that names the oid is under the table policy.

The runtime roles get table privileges through the group role
``igni_runtime``, granted ``WITH INHERIT TRUE, SET FALSE`` (PostgreSQL 16+)
so a producer cannot ``SET ROLE`` to it either. None owns a table or has
``BYPASSRLS``. Migrations run as the admin role that owns the tables, with
``igni migrate --database-url <admin url>``, which applies all of this
again each time — a table added by a migration is covered by the next run.
The admin role must bypass row-level security (a superuser, or
``BYPASSRLS``) for any migration that moves data.
"""

from __future__ import annotations

import hashlib
import secrets

from pydantic import BaseModel
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from ember_code.core.db.urls import DatabaseUrl, Dialect
from igni_cloud.producer import ProducerIncarnation

#: The group role every producer role takes its table privileges from.
RUNTIME_ROLE = "igni_runtime"
#: Agno's schema.
AGNO_SCHEMA = "ai"
#: Agno's table of its own schema versions: readable, nobody's data.
AGNO_VERSIONS_TABLE = "agno_schema_versions"


class TenantFunctions:
    """The ``SECURITY DEFINER`` functions the policies call."""

    ORG = "igni_org"
    PRODUCER = "igni_producer"
    AGNO_PREFIX = "igni_agno_prefix"

    #: The producer column of the connection's rows: the incarnation's.
    STORED = f"producer || '{ProducerIncarnation.SEPARATOR}' || incarnation"

    @classmethod
    def statements(cls) -> list[str]:
        body = {
            cls.ORG: "SELECT org FROM public.producer_roles WHERE role_name = session_user",
            cls.PRODUCER: (
                f"SELECT {cls.STORED} FROM public.producer_roles WHERE role_name = session_user"
            ),
            cls.AGNO_PREFIX: (
                f"SELECT org || '/' || {cls.STORED} || '/' FROM public.producer_roles "
                "WHERE role_name = session_user"
            ),
        }
        out: list[str] = []
        for name, query in body.items():
            out += [
                f"CREATE OR REPLACE FUNCTION public.{name}() RETURNS text "
                f"LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp "
                f"AS $$ {query} $$",
                f"REVOKE ALL ON FUNCTION public.{name}() FROM PUBLIC",
                f"GRANT EXECUTE ON FUNCTION public.{name}() TO {RUNTIME_ROLE}",
            ]
        return out


class RowLevelSecurity:
    """Applies the roles, functions, grants and policies, as the admin role."""

    def __init__(self, admin: DatabaseUrl) -> None:
        if admin.dialect is not Dialect.POSTGRESQL:
            raise ValueError("row-level security is PostgreSQL's")
        self._admin = admin

    def engine(self) -> Engine:
        return create_engine(self._admin.sync, isolation_level="AUTOCOMMIT")

    def apply(self) -> None:
        engine = self.engine()
        try:
            with engine.connect() as connection:
                self._apply(connection)
        finally:
            engine.dispose()

    def _apply(self, c: Connection) -> None:
        c.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = "
                f"'{RUNTIME_ROLE}') THEN CREATE ROLE {RUNTIME_ROLE} NOLOGIN NOBYPASSRLS; "
                "END IF; END $$"
            )
        )
        c.execute(
            text(
                "CREATE TABLE IF NOT EXISTS public.producer_roles ("
                "role_name text PRIMARY KEY, org text NOT NULL, producer text NOT NULL, "
                "incarnation text NOT NULL, UNIQUE (org, producer, incarnation))"
            )
        )
        c.execute(text("REVOKE ALL ON public.producer_roles FROM PUBLIC"))
        for statement in TenantFunctions.statements():
            c.execute(text(statement))
        c.execute(text(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}"))
        c.execute(text(f"GRANT SELECT ON public.alembic_version TO {RUNTIME_ROLE}"))
        for table in self.tenant_tables(c):
            self._protect(
                c,
                f'public."{table}"',
                f"org = (SELECT public.{TenantFunctions.ORG}()) "
                f"AND producer = (SELECT public.{TenantFunctions.PRODUCER}())",
            )
        c.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}"))
        if self.schema_exists(c, AGNO_SCHEMA):
            self._protect_agno(c)

    def _protect(self, c: Connection, table: str, condition: str) -> None:
        c.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        c.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        c.execute(text(f"DROP POLICY IF EXISTS igni_tenant ON {table}"))
        c.execute(
            text(
                f"CREATE POLICY igni_tenant ON {table} TO {RUNTIME_ROLE} "
                f"USING ({condition}) WITH CHECK ({condition})"
            )
        )
        c.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RUNTIME_ROLE}"))

    def _protect_agno(self, c: Connection) -> None:
        c.execute(text(f"GRANT USAGE ON SCHEMA {AGNO_SCHEMA} TO {RUNTIME_ROLE}"))
        c.execute(
            text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {AGNO_SCHEMA} TO {RUNTIME_ROLE}")
        )
        rows = c.execute(
            text(
                "SELECT table_name, bool_or(column_name = 'user_id') FROM "
                "information_schema.columns WHERE table_schema = :schema GROUP BY table_name"
            ),
            {"schema": AGNO_SCHEMA},
        ).all()
        for table, has_user in rows:
            qualified = f'{AGNO_SCHEMA}."{table}"'
            if has_user:
                prefix = f"(SELECT public.{TenantFunctions.AGNO_PREFIX}())"
                self._protect(c, qualified, f"starts_with(user_id, {prefix})")
                continue
            c.execute(text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
            c.execute(text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))
            if table == AGNO_VERSIONS_TABLE:
                c.execute(text(f"DROP POLICY IF EXISTS igni_read ON {qualified}"))
                c.execute(
                    text(
                        f"CREATE POLICY igni_read ON {qualified} FOR SELECT "
                        f"TO {RUNTIME_ROLE} USING (true)"
                    )
                )
                c.execute(text(f"GRANT SELECT ON {qualified} TO {RUNTIME_ROLE}"))

    @staticmethod
    def tenant_tables(c: Connection) -> list[str]:
        return list(
            c.scalars(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name IN ('org', 'producer') "
                    "AND table_name <> 'producer_roles' "
                    "GROUP BY table_name HAVING count(*) = 2 ORDER BY table_name"
                )
            ).all()
        )

    @staticmethod
    def agno_scoped_tables(c: Connection) -> list[str]:
        """Agno's tables with a ``user_id``: the ones holding a scope's rows."""
        if not RowLevelSecurity.schema_exists(c, AGNO_SCHEMA):
            return []
        return list(
            c.scalars(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND column_name = 'user_id' ORDER BY table_name"
                ),
                {"schema": AGNO_SCHEMA},
            ).all()
        )

    @staticmethod
    def schema_exists(c: Connection, schema: str) -> bool:
        return bool(
            c.scalar(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
                {"s": schema},
            )
        )


class ProducerCredentials(BaseModel):
    """What the platform hands a producer's VM to connect with."""

    role: str
    password: str
    #: ``IGNI_DATABASE_URL`` for the VM: the database, as this role.
    database_url: str


class ProducerRoles:
    """Mints and drops producers' PostgreSQL roles, as the admin role.

    What the node agent calls (``igni-cloud db grant-producer`` /
    ``revoke-producer``) when a producer's incarnation is first placed or
    removed. Granting again rotates the password.
    """

    PREFIX = "igni_p_"

    def __init__(self, admin: DatabaseUrl) -> None:
        self._rls = RowLevelSecurity(admin)
        self._admin = admin

    @classmethod
    def role_name(cls, org: str, producer: ProducerIncarnation) -> str:
        key = f"{org}\x00{producer.producer.ref}\x00{producer.incarnation}"
        digest = hashlib.sha256(key.encode()).hexdigest()[:40]
        return f"{cls.PREFIX}{digest}"

    def grant(self, org: str, producer: ProducerIncarnation) -> ProducerCredentials:
        role = self.role_name(org, producer)
        # Only [A-Za-z0-9_-]: safe inside a quoted literal, which role
        # DDL needs (it takes no bound parameters).
        password = secrets.token_urlsafe(32)
        engine = self._rls.engine()
        try:
            with engine.connect() as c:
                exists = c.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
                verb = "ALTER" if exists else "CREATE"
                c.execute(
                    text(
                        f"{verb} ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"
                    )
                )
                c.execute(text(f"GRANT {RUNTIME_ROLE} TO {role} WITH INHERIT TRUE, SET FALSE"))
                c.execute(
                    text(
                        "INSERT INTO public.producer_roles (role_name, org, producer, incarnation) "
                        "VALUES (:r, :o, :p, :i) ON CONFLICT (role_name) DO UPDATE "
                        "SET org = excluded.org, producer = excluded.producer, "
                        "incarnation = excluded.incarnation"
                    ),
                    {"r": role, "o": org, "p": producer.producer.ref, "i": producer.incarnation},
                )
        finally:
            engine.dispose()
        url = make_url(self._admin.sync).set(username=role, password=password)
        return ProducerCredentials(
            role=role, password=password, database_url=url.render_as_string(hide_password=False)
        )

    def revoke(self, org: str, producer: ProducerIncarnation) -> bool:
        """Drop the role. What it owned (its uploads' large objects) passes
        to the admin role; its rows stay — for the org's retention period,
        until ``erase-producer`` — unreachable until granted again."""
        engine = self._rls.engine()
        try:
            with engine.connect() as c:
                return self.drop(c, self.role_name(org, producer), keep_owned=True)
        finally:
            engine.dispose()

    @staticmethod
    def drop(c: Connection, role: str, *, keep_owned: bool) -> bool:
        """Drop ``role`` and its mapping; what it owns is kept (passed to
        the admin role) or dropped with it. ``False`` if there was none."""
        if c.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}):
            if keep_owned:
                c.execute(text(f"REASSIGN OWNED BY {role} TO CURRENT_USER"))
            c.execute(text(f"DROP OWNED BY {role}"))
            c.execute(text(f"DROP ROLE {role}"))
            existed = True
        else:
            existed = False
        mapped = c.execute(
            text("DELETE FROM public.producer_roles WHERE role_name = :r"), {"r": role}
        )
        return existed or bool(mapped.rowcount)


__all__ = [
    "AGNO_SCHEMA",
    "RUNTIME_ROLE",
    "ProducerCredentials",
    "ProducerRoles",
    "RowLevelSecurity",
    "TenantFunctions",
]
