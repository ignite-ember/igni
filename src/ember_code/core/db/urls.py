"""Which database: a SQLite file on the desktop, PostgreSQL in the cloud.

:class:`DatabaseUrl` is the one value the engine registry, the schema
keeper, the migrator and ``igni migrate`` are keyed by. It is built from
either a file path (the desktop's per-project ``state.db``, unchanged)
or a SQLAlchemy URL (``IGNI_DATABASE_URL``), and it knows the two driver
spellings each dialect needs:

* SQLite — ``sqlite:///`` for sync access (alembic, the small key/value
  stores) and ``sqlite+aiosqlite:///`` for async access.
* PostgreSQL — ``postgresql+psycopg://`` for both. psycopg 3 is one
  driver with a sync and an async API, so alembic, the sync stores, the
  async stores and Agno's ``AsyncPostgresDb`` all speak to the server
  through the same library (``docs/CLOUD.md`` section 9).

Any other dialect is refused here, at the edge, rather than failing on
the first dialect-specific statement deep inside a store.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import URL, make_url


class Dialect(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class DatabaseUrl(BaseModel):
    """A database igni keeps its state in, in canonical form.

    ``url`` is the sync SQLAlchemy URL — for a file, the absolute,
    symlink-resolved path, so every spelling of one file is one key (two
    engines on one SQLite file are two pools fighting over its lock).
    """

    model_config = ConfigDict(frozen=True)

    url: str

    #: The sync and async drivers per dialect.
    _SYNC_DRIVER: ClassVar[dict[Dialect, str]] = {
        Dialect.SQLITE: "sqlite",
        Dialect.POSTGRESQL: "postgresql+psycopg",
    }
    _ASYNC_DRIVER: ClassVar[dict[Dialect, str]] = {
        Dialect.SQLITE: "sqlite+aiosqlite",
        Dialect.POSTGRESQL: "postgresql+psycopg",
    }

    @classmethod
    def for_file(cls, path: str | Path) -> DatabaseUrl:
        """The SQLite file at ``path`` (``~`` and relative paths resolved)."""
        return cls(url=f"sqlite:///{cls.normalize_path(path)}")

    @classmethod
    def parse(cls, url: str) -> DatabaseUrl:
        """A SQLAlchemy URL, any driver spelling of a supported dialect.

        ``postgres://``, ``postgresql://``, ``postgresql+asyncpg://`` and
        ``postgresql+psycopg://`` all name the same server; they become
        the one psycopg spelling. A SQLite URL's path is resolved the way
        :meth:`for_file` resolves one.
        """
        parsed = make_url(url)
        dialect = cls._dialect_of(parsed)
        if dialect is Dialect.SQLITE:
            if not parsed.database or parsed.database == ":memory:":
                raise ValueError("an in-memory SQLite database cannot hold igni's state")
            return cls.for_file(parsed.database)
        canonical = parsed.set(drivername=cls._SYNC_DRIVER[dialect])
        return cls(url=canonical.render_as_string(hide_password=False))

    @classmethod
    def of(cls, target: str | Path | DatabaseUrl) -> DatabaseUrl:
        """A path or a URL, whichever ``target`` is."""
        if isinstance(target, DatabaseUrl):
            return target
        text = str(target)
        if "://" in text:
            return cls.parse(text)
        return cls.for_file(text)

    @staticmethod
    def normalize_path(path: str | Path) -> str:
        """``~`` expanded, relative and ``..`` segments resolved."""
        return str(Path(str(path)).expanduser().resolve())

    @staticmethod
    def _dialect_of(parsed: URL) -> Dialect:
        backend = parsed.get_backend_name()
        if backend == "postgres":
            backend = Dialect.POSTGRESQL.value
        try:
            return Dialect(backend)
        except ValueError:
            raise ValueError(
                f"igni keeps its state in SQLite or PostgreSQL, not {backend!r}"
            ) from None

    @property
    def dialect(self) -> Dialect:
        return self._dialect_of(make_url(self.url))

    @property
    def is_sqlite(self) -> bool:
        return self.dialect is Dialect.SQLITE

    @property
    def file(self) -> Path | None:
        """The SQLite file, or ``None`` for a server."""
        if not self.is_sqlite:
            return None
        database = make_url(self.url).database
        return Path(database) if database else None

    @property
    def sync(self) -> str:
        return self.url

    @property
    def async_(self) -> str:
        parsed = make_url(self.url)
        return parsed.set(drivername=self._ASYNC_DRIVER[self.dialect]).render_as_string(
            hide_password=False
        )

    @property
    def redacted(self) -> str:
        """For messages: the URL with any password masked."""
        return make_url(self.url).render_as_string(hide_password=True)

    def prepare(self) -> None:
        """Create a SQLite file's directory; a server needs nothing."""
        path = self.file
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __str__(self) -> str:
        return self.redacted


__all__ = ["DatabaseUrl", "Dialect"]
