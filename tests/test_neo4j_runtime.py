"""Unit tests for the Neo4j runtime module.

Covers pure-logic seams that don't require a running Neo4j:

* Free-port allocation
* Password generation + chmod
* Bootstrap marker file lifecycle
* ``neo4j.conf`` rendering
* ``Neo4jDiscovery`` JSON round-trip
* Settings → :class:`Neo4jRuntime` plumbing

The subprocess start/stop path (which actually spawns Neo4j) is
covered by ``test_neo4j_integration.py`` (behind
``@pytest.mark.integration``).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ember_code.backend.neo4j_runtime import (
    DEFAULT_NEO4J_VERSION,
    Neo4jBootstrap,
    Neo4jDiscovery,
    Neo4jEndpoints,
    Neo4jRuntime,
    _bolt_port,
    _pick_free_port,
)

# ── Port allocation ────────────────────────────────────────────────────


def test_pick_free_port_returns_an_unused_port() -> None:
    """The helper closes its socket before returning, so a probe
    immediately after will typically fail. We just check the
    returned number is in the unprivileged-port range.
    """
    port = _pick_free_port("127.0.0.1")
    assert 1024 < port < 65536


def test_bolt_port_parser_handles_canonical_uri() -> None:
    assert _bolt_port("bolt://127.0.0.1:7687") == 7687


def test_bolt_port_parser_returns_zero_on_garbage() -> None:
    """Defensive: a malformed URI returns 0 instead of raising."""
    assert _bolt_port("not-a-uri") == 0
    assert _bolt_port("") == 0


# ── Password generation ───────────────────────────────────────────────


def test_password_generated_with_0o600(tmp_path: Path) -> None:
    rt = Neo4jRuntime(data_dir=tmp_path)
    auth_file = rt._auth_path("test", "aaa")
    pw = rt._ensure_password(auth_file)
    assert len(pw) >= 24  # token_urlsafe(24) → ≥32 chars, but be loose
    assert auth_file.exists()
    # Verify the password was actually written.
    assert auth_file.read_text().strip() == pw
    # Mode 0o600 (skip on Windows where chmod is a no-op).
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(auth_file).st_mode)
        assert mode == 0o600


def test_password_reused_across_runtime_instances(tmp_path: Path) -> None:
    rt1 = Neo4jRuntime(data_dir=tmp_path)
    rt2 = Neo4jRuntime(data_dir=tmp_path)
    auth_file = rt1._auth_path("test", "aaa")
    assert rt1._ensure_password(auth_file) == rt2._ensure_password(auth_file)


# ── neo4j.conf rendering ───────────────────────────────────────────────


def test_config_includes_required_keys(tmp_path: Path) -> None:
    rt = Neo4jRuntime(data_dir=tmp_path)
    config = rt._build_config(
        bolt_port=7687,
        http_port=7474,
        password="hunter2",
        data_dir=tmp_path / "state/p-c/data",
        logs_dir=tmp_path / "state/p-c/logs",
    )
    # Bolt + HTTP listen directives.
    assert "server.bolt.listen_address=:7687" in config
    assert "server.http.listen_address=:7474" in config
    # Auth disabled for spawned ephemeral localhost instances.
    assert "dbms.security.auth_enabled=false" in config
    # HNSW-style heap sizing.
    assert "server.memory.heap.initial_size=512m" in config
    assert "server.memory.heap.max_size=2g" in config


def test_config_pins_the_store_to_this_pairs_own_directory(tmp_path: Path) -> None:
    """The data directory must be absolute and under the 5.x key.

    Neo4j is started with ``--home-dir`` pointing at the shared install and only
    ``--config-dir`` per pair, so this file is the single channel that decides
    where the store lands. It used to say ``dbms.directories.data=./data``:
    the 4.x key, which 5.x ignores without complaint, and a relative value that
    would have resolved against the shared install even under the right key. The
    result was one store at ``<install>/data/databases/neo4j`` shared by every
    project, each load overwriting the last, and no graph ever reusable.
    """
    rt = Neo4jRuntime(data_dir=tmp_path)
    data_dir = tmp_path / "state/proj-abc123/data"
    logs_dir = tmp_path / "state/proj-abc123/logs"
    config = rt._build_config(
        bolt_port=7687,
        http_port=7474,
        password="hunter2",
        data_dir=data_dir,
        logs_dir=logs_dir,
    )

    assert f"server.directories.data={data_dir}" in config
    assert f"server.directories.logs={logs_dir}" in config
    # The 4.x spelling is silently ignored by 5.x, so it must not be what we
    # rely on — and a relative path is shared whatever the key.
    assert "dbms.directories.data" not in config
    assert "dbms.directories.logs" not in config
    assert "=./data" not in config
    assert "=./logs" not in config


def test_two_commits_of_one_project_get_separate_stores(tmp_path: Path) -> None:
    """Different commits must not be handed the same data directory.

    This is the property the shared-store bug actually broke: the paths were
    already distinct, they simply never reached the server.
    """
    rt = Neo4jRuntime(data_dir=tmp_path)
    first = rt._build_config(
        bolt_port=7687,
        http_port=7474,
        password="x",
        data_dir=rt._data_path("proj", "aaaaaaa"),
        logs_dir=rt._logs_path("proj", "aaaaaaa"),
    )
    second = rt._build_config(
        bolt_port=7688,
        http_port=7475,
        password="x",
        data_dir=rt._data_path("proj", "bbbbbbb"),
        logs_dir=rt._logs_path("proj", "bbbbbbb"),
    )

    def data_line(config: str) -> str:
        return next(
            line for line in config.splitlines() if line.startswith("server.directories.data=")
        )

    assert data_line(first) != data_line(second)


# ── Neo4jDiscovery ────────────────────────────────────────────────────


def test_discovery_round_trip(tmp_path: Path) -> None:
    auth = tmp_path / "neo4j.auth"
    auth.write_text("hunter2\n")
    endpoints = Neo4jEndpoints(
        bolt_uri="bolt://127.0.0.1:7687",
        http_uri="http://127.0.0.1:7474",
        pid=12345,
        user="neo4j",
        password_file=auth,
    )
    discovery = Neo4jDiscovery(tmp_path)
    discovery.save(endpoints)
    loaded = discovery.load()
    assert loaded == endpoints


def test_discovery_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert Neo4jDiscovery(tmp_path).load() is None


def test_discovery_clear_is_idempotent(tmp_path: Path) -> None:
    discovery = Neo4jDiscovery(tmp_path)
    discovery.clear()  # no file → no error
    assert discovery.load() is None


def test_discovery_load_tolerates_corrupt_file(tmp_path: Path) -> None:
    discovery = Neo4jDiscovery(tmp_path)
    discovery.path.write_text("not json{", encoding="utf-8")
    assert discovery.load() is None  # warning logged, no raise


def test_discovery_load_rejects_missing_keys(tmp_path: Path) -> None:
    discovery = Neo4jDiscovery(tmp_path)
    discovery.path.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert discovery.load() is None  # schema-check fails → treated as corrupt


def test_discovery_save_writes_atomically(tmp_path: Path) -> None:
    """``os.replace``-based atomic write — no half-written files."""
    endpoints = Neo4jEndpoints(
        bolt_uri="bolt://h:1",
        http_uri="http://h:2",
        pid=99,
        user="neo4j",
        password_file=tmp_path / "p",
    )
    Neo4jDiscovery(tmp_path).save(endpoints)
    raw = Neo4jDiscovery(tmp_path).path.read_text()
    payload = json.loads(raw)
    assert payload["pid"] == 99
    assert payload["bolt_uri"] == "bolt://h:1"


# ── Bootstrap (offline) ────────────────────────────────────────────────


def test_bootstrap_paths_resolve_under_cache_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "neo4j"
    b = Neo4jBootstrap(version="5.26.0", cache_root=cache_root)
    assert b.install_dir == cache_root / "neo4j-community-5.26.0"
    assert b.neo4j_bin == cache_root / "neo4j-community-5.26.0" / "bin" / "neo4j"
    assert b.marker_path == cache_root / ".installed-5.26.0"


def test_bootstrap_default_url_uses_public_dist(tmp_path: Path) -> None:
    """No override → public Neo4j distribution URL."""
    b = Neo4jBootstrap(version="5.26.0", cache_root=tmp_path)
    assert "dist.neo4j.org" in b._tarball_url
    assert "5.26.0" in b._tarball_url


def test_bootstrap_custom_url_overrides_default(tmp_path: Path) -> None:
    b = Neo4jBootstrap(
        version="5.26.0",
        cache_root=tmp_path,
        tarball_url="https://mirror.local/neo4j.tar.gz",
    )
    assert b._tarball_url == "https://mirror.local/neo4j.tar.gz"


@pytest.mark.parametrize(
    "tarball_url",
    ["https://dist.neo4j.org/neo4j-community-{version}-unix.tar.gz"],
)
def test_default_tarball_url_format(tmp_path: Path, tarball_url: str) -> None:
    b = Neo4jBootstrap(version="5.26.0", cache_root=tmp_path)
    assert b._tarball_url == tarball_url.format(version="5.26.0")


# ── Runtime config wiring ─────────────────────────────────────────────


def test_runtime_resolves_paths_via_canonical_module(tmp_path: Path) -> None:
    """Paths must agree with :mod:`core.code_index.paths`."""
    from ember_code.core.code_index.paths import (
        neo4j_data_dir,
    )

    rt = Neo4jRuntime(data_dir=tmp_path)
    assert rt._neo4j_root == neo4j_data_dir(tmp_path)
    assert rt._runtime_path("test", "aaa") == (
        neo4j_data_dir(tmp_path) / "state" / "test-aaa" / "runtime.json"
    )
    assert rt._auth_path("test", "aaa") == rt._data_path("test", "aaa").parent / "auth.txt"


def test_default_version_is_pinned() -> None:
    assert DEFAULT_NEO4J_VERSION == "5.26.0"


def test_is_alive_returns_false_for_dead_pid() -> None:
    """A PID nobody will ever allocate (the magic PID 0 is reserved)."""
    endpoints = Neo4jEndpoints(
        bolt_uri="bolt://127.0.0.1:0",
        http_uri="http://127.0.0.1:0",
        pid=0,
        user="neo4j",
        password_file=Path("/tmp/whatever"),
    )
    rt = Neo4jRuntime(data_dir="/tmp")
    assert not rt._is_alive(endpoints)
