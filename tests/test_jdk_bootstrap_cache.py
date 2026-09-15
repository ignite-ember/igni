"""A cached JDK is found again by the process that did not install it.

``JdkBootstrap.__init__`` predicts ``<cache>/temurin-21/bin/java``.
Adoptium's tarball unpacks to something else entirely — on this machine
``<cache>/jdk-21.0.12.1+1/Contents/Home/bin/java`` — and
``_download_and_extract`` coped by rebinding ``_jdk_bin`` to whatever it
found. That rebinding lived only in the process that downloaded, so
every later backend start probed a path that had never existed,
declared the cache broken, and pulled 348 MB again.

Observed directly: a backend start logging ``downloading JDK
temurin/21`` with a complete, working JDK already sitting in
``~/.ember/neo4j/jdk``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Imported as a module, not as names. ``test_jdk_bootstrap.py`` calls
# ``importlib.reload`` on this module, which mints fresh class objects —
# so a name bound at import time stops being the class that later code
# raises, and ``pytest.raises`` silently misses it. Only visible in a
# full-suite run, which is exactly when it bit.
import ember_code.backend.jdk_bootstrap as jdk_mod

# The real shape, not the predicted one: the nesting is what broke it.
_REAL_LAYOUT = Path("jdk-21.0.12.1+1") / "Contents" / "Home"


def _fake_jdk(cache_root: Path, *, record_path: bool = True) -> Path:
    java = cache_root / _REAL_LAYOUT / "bin" / "java"
    java.parent.mkdir(parents=True, exist_ok=True)
    java.write_text("")
    java.chmod(0o755)
    marker = {"vendor": "temurin", "version": "21"}
    if record_path:
        marker["java_bin"] = str(java)
    (cache_root / ".installed-jdk-21").write_text(json.dumps(marker))
    return java


async def test_a_cached_jdk_is_not_downloaded_again(tmp_path, monkeypatch):
    """The regression, in one number."""
    java = _fake_jdk(tmp_path)
    boot = jdk_mod.JdkBootstrap(version="21", cache_root=tmp_path)

    downloads = 0

    async def _count():
        nonlocal downloads
        downloads += 1

    monkeypatch.setattr(boot, "_download_and_extract", _count)

    assert await boot.ensure() == java
    assert downloads == 0


async def test_a_marker_from_before_the_fix_still_finds_its_jdk(tmp_path, monkeypatch):
    """Markers already on disk have no ``java_bin`` field. Those users
    should not pay for the upgrade with another 348 MB."""
    java = _fake_jdk(tmp_path, record_path=False)
    boot = jdk_mod.JdkBootstrap(version="21", cache_root=tmp_path)

    async def _fail():
        raise AssertionError("should not redownload for an old marker")

    monkeypatch.setattr(boot, "_download_and_extract", _fail)

    assert await boot.ensure() == java


async def test_a_marker_with_no_jdk_behind_it_downloads(tmp_path, monkeypatch):
    """The guard has to still work: a marker over an empty cache is a
    broken install, not a cached one."""
    (tmp_path / ".installed-jdk-21").write_text(json.dumps({"version": "21"}))
    boot = jdk_mod.JdkBootstrap(version="21", cache_root=tmp_path)

    downloads = 0

    async def _install():
        nonlocal downloads
        downloads += 1
        java = _fake_jdk(tmp_path)
        object.__setattr__(boot, "_jdk_bin", java)

    monkeypatch.setattr(boot, "_download_and_extract", _install)

    await boot.ensure()

    assert downloads == 1


async def test_an_extract_that_yields_no_java_raises(tmp_path, monkeypatch):
    """``_probe_java``'s result was discarded after the download, under
    a comment claiming it raised — so an extract that produced nothing
    got stamped good."""
    boot = jdk_mod.JdkBootstrap(version="21", cache_root=tmp_path)

    async def _produces_nothing():
        pass

    monkeypatch.setattr(boot, "_download_and_extract", _produces_nothing)

    with pytest.raises(jdk_mod.JdkBootstrapError):
        await boot.ensure()

    assert not (tmp_path / ".installed-jdk-21").exists()


async def test_checking_the_cache_spawns_nothing(tmp_path, monkeypatch):
    """No ``java -version``: a 15-second deadline against a cold JVM on
    a just-unpacked JDK is a coin toss, and losing it cost the whole
    install."""
    _fake_jdk(tmp_path)
    boot = jdk_mod.JdkBootstrap(version="21", cache_root=tmp_path)

    def _no(*args, **kwargs):
        raise AssertionError("the cache check must not spawn a subprocess")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _no)

    await boot.ensure()
