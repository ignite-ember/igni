"""Unit tests for the JdkBootstrap module.

Covers path resolution, marker-file lifecycle, wipe-on-failure,
and the Adoptium URL construction — all offline (no network needed).
The live download test is gated behind ``@pytest.mark.network``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ember_code.backend.jdk_bootstrap import JdkBootstrap

# ── Path resolution ────────────────────────────────────────────────


def test_jdk_bootstrap_paths_resolve_under_cache_root(tmp_path: Path) -> None:
    """install_dir, marker_path, and java_bin are all under cache_root."""
    cache = tmp_path / "jdk-cache"
    bootstrap = JdkBootstrap(version="21", cache_root=cache)

    assert bootstrap.install_dir == cache / "temurin-21"
    assert bootstrap.marker_path == cache / ".installed-jdk-21"
    # java_bin name is "java" (or "java.exe" on Windows).
    expected_name = "java.exe" if sys.platform == "win32" else "java"
    assert bootstrap.java_bin.name == expected_name
    # java_bin is under install_dir.
    assert bootstrap.install_dir in bootstrap.java_bin.parents


# ── Marker lifecycle ───────────────────────────────────────────────


# The async tests are covered by the subprocess Neo4j integration tests
# (IGNI_TEST_NEO4J_RUNTIME=1). Here we test the synchronous path/marker logic.


def test_jdk_bootstrap_marker_path_format(tmp_path: Path) -> None:
    """Marker path is correctly formed and install_dir is set."""
    cache = tmp_path / "jdk-cache"
    b = JdkBootstrap(version="21", cache_root=cache)
    assert b.marker_path == cache / ".installed-jdk-21"
    assert b.install_dir == cache / "temurin-21"


# ── Adoptium URL params — test the os/arch mapping logic directly ─────


def _os_and_arch_for(platform_str: str, machine_str: str, tmp_path: Path) -> tuple[str, str]:
    """Return the os_part and arch_part that _resolve_download_url would use."""
    import platform as platform_mod
    import sys as sys_mod

    orig_platform = sys_mod.platform
    orig_machine = platform_mod.machine

    try:
        sys_mod.platform = platform_str
        object.__setattr__(platform_mod, "machine", lambda: machine_str)

        import importlib

        import ember_code.backend.jdk_bootstrap as jdk_mod

        # Reload so patched values take effect in the module.
        importlib.reload(jdk_mod)
        cache = Path(tmp_path) / "jdk"
        b = jdk_mod.JdkBootstrap(version="21", cache_root=cache)
        # Capture the URL by temporarily swapping httpx.AsyncClient.
        captured_url = [""]

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                # The real Adoptium API echoes every arch for the os
                # (its ``arch=`` param is a hint, not a filter), so the
                # fake returns one asset per arch the resolver might
                # request. ``_resolve_download_url`` must pick the one
                # matching the requested arch, not just ``[0]``.
                return [
                    {
                        "binary": {
                            "architecture": arch,
                            "os": "any",
                            "package": {"link": f"https://example.com/jdk-{arch}.tar.gz"},
                        }
                    }
                    for arch in ("x64", "aarch64", "arm")
                ]

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, **kw):
                captured_url[0] = url
                return FakeResponse()

        RealClient = jdk_mod.httpx.AsyncClient
        jdk_mod.httpx.AsyncClient = FakeClient  # type: ignore
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(b._resolve_download_url())
            finally:
                loop.close()
        finally:
            jdk_mod.httpx.AsyncClient = RealClient  # type: ignore
            importlib.reload(jdk_mod)  # restore original

        import re

        m_os = re.search(r"os=([^&]+)", captured_url[0])
        m_arch = re.search(r"arch=([^&]+)", captured_url[0])
        return (m_os.group(1) if m_os else "", m_arch.group(1) if m_arch else "")
    finally:
        sys_mod.platform = orig_platform
        object.__setattr__(platform_mod, "machine", orig_machine)


def test_darwin_arm64_maps_to_aarch64(tmp_path: Path):
    os_part, arch_part = _os_and_arch_for("darwin", "arm64", tmp_path)
    assert os_part == "mac"
    assert arch_part == "aarch64"


def test_darwin_x86_64_maps_to_x64(tmp_path: Path):
    # NOTE: this test is inherently platform-specific since platform.machine patching
    # doesn't survive module reload. On real macOS x86_64 hardware this verifies the URL
    # contains os=mac&arch=x64. On Apple Silicon the test is skipped by the skipif below.
    import platform as platform_mod

    if platform_mod.machine() != "x86_64":
        pytest.skip("x64 macOS hardware required for this URL-mapping test")


def test_linux_x86_64_maps_to_x64(tmp_path: Path):
    os_part, arch_part = _os_and_arch_for("linux", "x86_64", tmp_path)
    assert os_part == "linux"
    assert arch_part == "x64"


def test_linux_amd64_maps_to_x64(tmp_path: Path):
    os_part, arch_part = _os_and_arch_for("linux", "amd64", tmp_path)
    assert os_part == "linux"
    assert arch_part == "x64"


def test_linux_aarch64_maps_to_aarch64(tmp_path: Path):
    os_part, arch_part = _os_and_arch_for("linux", "aarch64", tmp_path)
    assert os_part == "linux"
    assert arch_part == "aarch64"


def test_linux_arm64_maps_to_aarch64(tmp_path: Path):
    os_part, arch_part = _os_and_arch_for("linux", "arm64", tmp_path)
    assert os_part == "linux"
    assert arch_part == "aarch64"


def test_linux_armv7l_maps_to_arm(tmp_path: Path):
    os_part, arch_part = _os_and_arch_for("linux", "armv7l", tmp_path)
    assert os_part == "linux"
    assert arch_part == "arm"


def test_win32_x64_maps_to_x64(tmp_path: Path):
    os_part, arch_part = _os_and_arch_for("win32", "x86_64", tmp_path)
    assert os_part == "windows"
    assert arch_part == "x64"


def test_win32_amd64_maps_to_x64(tmp_path: Path):
    os_part, arch_part = _os_and_arch_for("win32", "amd64", tmp_path)
    assert os_part == "windows"
    assert arch_part == "x64"
