"""Tests for ``core/db/engine`` — the SQLAlchemy engine cache.

The module's invariants are subtle:

  * Engines + sessionmakers are cached by NORMALISED path. Two
    callers passing ``~/.igni/state.db`` and the equivalent
    absolute path must get the SAME engine (otherwise SQLite
    locking gets confused with two engines on the same file).
  * Paths are auto-created. First call to ``get_engine`` for a
    file under a non-existent parent must create the parent.
  * ``dispose_all`` actually clears the caches — otherwise the
    next call returns a disposed engine, which fails on first
    use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.db.engine import (
    dispose_all,
    get_async_engine,
    get_async_sessionmaker,
    get_engine,
    get_sessionmaker,
)
from ember_code.core.db.urls import DatabaseUrl, Dialect
from ember_code.core.paths import DEFAULT_DATA_DIR

_normalize_path = DatabaseUrl.normalize_path


def sync_url(path):
    return DatabaseUrl.for_file(path).sync


def async_url(path):
    return DatabaseUrl.for_file(path).async_


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    """Each test starts with empty caches. The module's caches are
    process-globals, so tests would otherwise leak engine identity
    across cases."""
    dispose_all()
    yield
    dispose_all()


class TestNormalizePath:
    def test_expands_tilde(self):
        # ``~`` must expand to the home dir. Without this, the
        # engine cache would treat ``~/.igni/x.db`` and the
        # expanded form as different paths → two engines on
        # the same file.
        normalized = _normalize_path(f"{DEFAULT_DATA_DIR}/state.db")
        # Result must not contain ``~``.
        assert "~" not in normalized
        # And must be an absolute path.
        assert Path(normalized).is_absolute()

    def test_resolves_relative_path(self, tmp_path, monkeypatch):
        # Relative paths resolve from cwd. Two callers from
        # different cwds passing the same relative path would
        # otherwise get different engines — bad.
        monkeypatch.chdir(tmp_path)
        normalized = _normalize_path("local.db")
        assert normalized == str((tmp_path / "local.db").resolve())

    def test_resolves_dot_segments(self, tmp_path):
        # ``../`` segments collapse. The engine cache key MUST
        # be canonical or we'd cache the same physical file
        # under two different keys.
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        normalized = _normalize_path(nested / ".." / "b" / "state.db")
        assert ".." not in normalized
        assert normalized == str((nested / "state.db").resolve())

    def test_absolute_path_passes_through(self, tmp_path):
        absolute = tmp_path / "state.db"
        assert _normalize_path(absolute) == str(absolute.resolve())

    def test_accepts_path_object(self, tmp_path):
        # The signature is ``str | Path`` — both shapes should
        # produce the same key (otherwise typed callers fork
        # the cache from string callers).
        absolute = tmp_path / "state.db"
        assert _normalize_path(str(absolute)) == _normalize_path(absolute)


class TestUrls:
    def test_sync_url_format(self, tmp_path):
        # ``sqlite:///`` prefix is the SQLAlchemy convention.
        # Drift here breaks alembic.
        url = sync_url(tmp_path / "x.db")
        assert url.startswith("sqlite:///")
        assert str(tmp_path) in url

    def test_async_url_uses_aiosqlite_driver(self, tmp_path):
        # The async flavour is what code_index uses. The
        # driver suffix (``+aiosqlite``) is load-bearing —
        # without it, SQLAlchemy picks the sync driver and
        # the async engine fails at first query.
        url = async_url(tmp_path / "x.db")
        assert url.startswith("sqlite+aiosqlite:///")

    def test_urls_normalize_input(self, tmp_path):
        # Same normalisation as get_engine — the URL builder
        # also resolves ``~`` / relative / ``..``. So calling
        # ``sync_url`` with a tilde path gives the same string
        # as calling it with the expanded form.
        absolute = tmp_path / "state.db"
        # Make sure both produce the same URL.
        assert sync_url(absolute) == sync_url(str(absolute))


class TestDatabaseUrls:
    """``IGNI_DATABASE_URL`` spellings: one canonical URL per database."""

    def test_every_postgres_spelling_is_psycopg(self):
        for raw in (
            "postgres://u:p@h:5432/db",
            "postgresql://u:p@h:5432/db",
            "postgresql+asyncpg://u:p@h:5432/db",
            "postgresql+psycopg://u:p@h:5432/db",
        ):
            url = DatabaseUrl.parse(raw)
            assert url.dialect is Dialect.POSTGRESQL
            assert url.sync == "postgresql+psycopg://u:p@h:5432/db"
            assert url.async_ == "postgresql+psycopg://u:p@h:5432/db"
            assert url.file is None

    def test_the_password_is_hidden_when_shown(self):
        url = DatabaseUrl.parse("postgresql://u:secret@h/db")
        assert "secret" not in str(url)
        assert "secret" not in url.redacted

    def test_a_sqlite_url_is_its_file(self, tmp_path):
        url = DatabaseUrl.parse(f"sqlite:///{tmp_path}/nested/../state.db")
        assert url == DatabaseUrl.for_file(tmp_path / "state.db")
        assert url.file == (tmp_path / "state.db").resolve()

    def test_a_path_and_a_url_for_one_file_are_one_key(self, tmp_path):
        assert DatabaseUrl.of(tmp_path / "x.db") == DatabaseUrl.of(f"sqlite:///{tmp_path}/x.db")

    def test_other_dialects_and_memory_are_refused(self):
        with pytest.raises(ValueError):
            DatabaseUrl.parse("mysql://u@h/db")
        with pytest.raises(ValueError):
            DatabaseUrl.parse("sqlite://")


class TestEngineCacheIdentity:
    """The cache key is the normalised path. Same logical path →
    same engine instance."""

    def test_same_path_returns_same_engine(self, tmp_path):
        # Lock down identity equality — same engine instance,
        # not just "an equivalent engine". SQLAlchemy's
        # connection pool is per-engine, so two engines on the
        # same file would mean two separate pools (and SQLite
        # lock contention).
        path = tmp_path / "state.db"
        first = get_engine(path)
        second = get_engine(path)
        assert first is second

    def test_different_paths_get_different_engines(self, tmp_path):
        a = get_engine(tmp_path / "a.db")
        b = get_engine(tmp_path / "b.db")
        assert a is not b

    def test_equivalent_path_forms_collapse_to_one_engine(self, tmp_path):
        # ``state.db`` and ``./state.db`` and ``../<parent>/state.db``
        # all reference the same file. The cache key normalises so
        # ALL of them get the same engine.
        canonical = tmp_path / "state.db"
        nested = tmp_path / "nested"
        nested.mkdir()
        # Same file, three different spellings of the path:
        a = get_engine(canonical)
        b = get_engine(str(canonical))
        c = get_engine(nested / ".." / "state.db")
        assert a is b is c

    def test_sessionmaker_is_cached_per_path(self, tmp_path):
        sm1 = get_sessionmaker(tmp_path / "state.db")
        sm2 = get_sessionmaker(tmp_path / "state.db")
        assert sm1 is sm2

    def test_async_engine_cache_is_independent_of_sync(self, tmp_path):
        # Sync and async engines for the same path are
        # different caches (they're different SQLAlchemy
        # objects — one uses pysqlite, the other aiosqlite).
        sync = get_engine(tmp_path / "x.db")
        async_eng = get_async_engine(tmp_path / "x.db")
        assert sync is not async_eng  # Different types entirely.

    def test_async_sessionmaker_cached(self, tmp_path):
        sm1 = get_async_sessionmaker(tmp_path / "state.db")
        sm2 = get_async_sessionmaker(tmp_path / "state.db")
        assert sm1 is sm2


class TestParentDirAutoCreate:
    """First call to ``get_engine`` for a file under a non-existent
    parent must create the parent. Without it the SQLite open call
    would fail and the caller has to mkdir defensively everywhere."""

    def test_parent_dir_created_on_first_sync_call(self, tmp_path):
        deep = tmp_path / "subdir" / "deeper" / "state.db"
        assert not deep.parent.exists()
        get_engine(deep)
        assert deep.parent.exists()

    def test_parent_dir_created_on_first_async_call(self, tmp_path):
        deep = tmp_path / "async_dir" / "deeper" / "state.db"
        assert not deep.parent.exists()
        get_async_engine(deep)
        assert deep.parent.exists()

    def test_existing_parent_is_noop(self, tmp_path):
        # The mkdir is ``exist_ok=True`` — second call with an
        # existing parent is a noop (no errors).
        deep = tmp_path / "exists" / "state.db"
        deep.parent.mkdir()
        get_engine(deep)  # must not raise
        assert deep.parent.exists()


class TestDisposeAll:
    def test_clears_sync_engine_cache(self, tmp_path):
        # After dispose, a subsequent ``get_engine`` for the
        # same path must return a FRESH engine (the disposed
        # one is unusable).
        path = tmp_path / "state.db"
        first = get_engine(path)
        dispose_all()
        second = get_engine(path)
        assert first is not second

    def test_clears_sessionmaker_cache(self, tmp_path):
        path = tmp_path / "state.db"
        first = get_sessionmaker(path)
        dispose_all()
        second = get_sessionmaker(path)
        assert first is not second

    def test_clears_async_caches(self, tmp_path):
        # Async engines need ``await engine.dispose()`` to
        # cleanly close their pools, but dispose_all does the
        # CACHE clear synchronously (intentional — see the
        # source comment). Just pin the cache-clear behaviour.
        path = tmp_path / "state.db"
        first = get_async_engine(path)
        dispose_all()
        second = get_async_engine(path)
        assert first is not second
