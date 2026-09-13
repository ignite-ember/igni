"""A cached Neo4j install is not re-downloaded because Java is absent.

``ensure()`` validated its 159 MB cache by running ``bin/neo4j
--version`` — a shell script that starts a JVM. On a machine with no
system Java (most Macs) that exits non-zero whether the extract is
perfect or shredded, and a failed probe means "wipe and download
again". Every backend start. The bug was invisible while
``EMBER_NEO4J_RUNTIME`` kept the code from running at all; enabling
knowledge by default is what put it in everyone's path.

These tests never touch the network: the download is monkeypatched to
a counter, so "did it re-download?" is a number rather than a wait.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ember_code.backend.neo4j_runtime import Neo4jBootstrap, Neo4jBootstrapError


def _fake_install(cache_root: Path, version: str = "5.26.0", *, whole: bool = True) -> Path:
    """Lay out a cache that looks like a finished extract."""
    install = cache_root / f"neo4j-community-{version}"
    (install / "bin").mkdir(parents=True, exist_ok=True)
    (install / "bin" / "neo4j").write_text("#!/bin/sh\nexit 0\n")
    (install / "bin" / "neo4j").chmod(0o755)
    lib = install / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    if whole:
        (lib / "neo4j-kernel.jar").write_bytes(b"")
    (cache_root / f".installed-{version}").write_text(
        json.dumps({"version": version, "install_dir": str(install)})
    )
    return install


@pytest.fixture
def no_java(monkeypatch):
    """A machine with no JDK anywhere the bootstrap can see."""
    monkeypatch.delenv("JAVA_HOME", raising=False)


async def test_a_cached_install_is_reused_when_there_is_no_java(tmp_path, no_java, monkeypatch):
    """The regression. Without this, the answer was "download it all
    again", once per backend start."""
    _fake_install(tmp_path)
    boot = Neo4jBootstrap(version="5.26.0", cache_root=tmp_path)

    downloads = 0

    async def _count():
        nonlocal downloads
        downloads += 1

    monkeypatch.setattr(boot, "_download_and_extract", _count)

    await boot.ensure()

    assert downloads == 0


async def test_a_partial_extract_is_still_caught_without_java(tmp_path, no_java, monkeypatch):
    """The guard the probe was there for has to survive the fix: a
    marker over an empty ``lib/`` is exactly the crash-mid-extract
    case, and it must still redownload."""
    _fake_install(tmp_path, whole=False)
    boot = Neo4jBootstrap(version="5.26.0", cache_root=tmp_path)

    downloads = 0

    async def _repair():
        nonlocal downloads
        downloads += 1
        _fake_install(tmp_path, whole=True)

    monkeypatch.setattr(boot, "_download_and_extract", _repair)

    await boot.ensure()

    assert downloads == 1


async def test_a_download_that_produces_nothing_usable_raises(tmp_path, no_java, monkeypatch):
    """``_probe_version``'s result used to be discarded after the
    download — the comment claimed it raised, so a shredded extract was
    stamped good and the retry loop could never fire."""
    boot = Neo4jBootstrap(version="5.26.0", cache_root=tmp_path)

    async def _produces_junk():
        _fake_install(tmp_path, whole=False)

    monkeypatch.setattr(boot, "_download_and_extract", _produces_junk)

    with pytest.raises(Neo4jBootstrapError):
        await boot.ensure()

    # Never stamped good: the next start retries instead of trusting a
    # marker written over a broken extract.
    assert not (tmp_path / ".installed-5.26.0").exists()


async def test_validating_the_cache_spawns_nothing(tmp_path, no_java, monkeypatch):
    """The check is stat-only now.

    This is the property that makes the others hold: a JVM that cannot
    start, or that takes longer than a timeout to answer, is no longer
    able to condemn an intact install. The observed failure was
    ``rc -9`` — the probe killed at its own ten-second deadline while
    macOS verified a freshly-downloaded JDK.
    """
    _fake_install(tmp_path)
    boot = Neo4jBootstrap(version="5.26.0", cache_root=tmp_path)

    def _no(*args, **kwargs):
        raise AssertionError("the cache check must not spawn a subprocess")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _no)

    assert await boot.ensure() == boot.neo4j_bin
