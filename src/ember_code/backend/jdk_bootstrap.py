"""Download and cache a JDK for Neo4j sidecar use.

Mirrors :class:`Neo4jBootstrap` — same ensure/marker/retry shape.
JdkBootstrap downloads Temurin JDK 21 from api.adoptium.net into
``<cache_root>/`` and writes a ``.installed-jdk-21`` marker recording
where the ``bin/java`` actually landed. The recorded path is the
important part: Adoptium does not unpack to the directory name this
class predicts, so the location has to be discovered after extraction
and handed to the next process.

No OS-level installation, no installer — just a download-and-extract into a
user-owned cache directory. The BE subprocess sets ``JAVA_HOME`` to the
extracted path so Neo4j finds Java without any system-level setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class JdkBootstrapError(RuntimeError):
    """Raised when the JDK bootstrap cannot produce a working java binary."""


class JdkBootstrap:
    """Ensures a JDK is extracted locally for Neo4j use.

    Download flow (matches :class:`Neo4jBootstrap`):

    1. Check ``<cache_root>/.installed-jdk-<version>`` marker.
    2. If marker present and the recorded ``bin/java`` is there →
       return it.
    3. Else download the archive via httpx streaming and extract.
    4. If extraction produced no java binary → wipe the partial
       install, retry once. If the retry also fails → raise
       :class:`JdkBootstrapError`.

    The marker is written only after a binary is in hand, so a crash
    mid-extract does not leave the cache in a "marked-good but
    actually broken" state.
    """

    def __init__(
        self,
        *,
        version: str = "21",
        vendor: str = "temurin",
        cache_root: Path,
    ):
        self._version = version
        self._vendor = vendor
        self._cache_root = Path(cache_root).expanduser()
        self._install_dir = self._cache_root / f"{vendor}-{version}"
        self._jdk_bin = (
            self._install_dir / "bin" / ("java.exe" if sys.platform == "win32" else "java")
        )

    @property
    def install_dir(self) -> Path:
        return self._install_dir

    @property
    def marker_path(self) -> Path:
        return self._cache_root / f".installed-jdk-{self._version}"

    @property
    def java_bin(self) -> Path:
        return self._jdk_bin

    async def _resolve_download_url(self) -> str:
        """Query the Adoptium assets API and return the CDN URL for this platform.

        The ``/v3/assets/latest/`` endpoint always returns the correct download
        link (via ``package.link``) regardless of platform, unlike
        ``/v3/binary/latest/`` which 404s for macOS aarch64.
        """
        machine = platform.machine().lower()
        system = sys.platform

        if system == "darwin":
            os_part = "mac"
            # Apple Silicon reports "arm64"; Adoptium names it "aarch64".
            # Intel Macs report "x86_64" → "x64". Don't hardcode either.
            arch_map = {
                "arm64": "aarch64",
                "aarch64": "aarch64",
                "x86_64": "x64",
                "amd64": "x64",
            }
            arch_part = arch_map.get(machine)  # type: ignore[assignment]
        elif system == "linux":
            os_part = "linux"
            arch_map = {
                "aarch64": "aarch64",
                "arm64": "aarch64",
                "armv7l": "arm",
                "x86_64": "x64",
                "amd64": "x64",
            }
            arch_part = arch_map.get(machine)  # type: ignore[assignment]
        elif system == "win32":
            os_part = "windows"
            arch_map = {"x86_64": "x64", "amd64": "x64"}
            arch_part = arch_map.get(machine)  # type: ignore[assignment]
        else:
            raise JdkBootstrapError(f"unsupported platform: {system}/{machine}")

        if arch_part is None:
            raise JdkBootstrapError(f"unsupported machine: {machine} on {system}")

        assets_url = (
            f"https://api.adoptium.net/v3/assets/latest/{self._version}/hotspot"
            f"?os={os_part}&arch={arch_part}&image_type=jdk&vendor=eclipse"
        )
        timeout = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(assets_url)
            resp.raise_for_status()
            assets = resp.json()

        if not assets:
            raise JdkBootstrapError(f"no Adoptium assets for {self._version}/{os_part}/{arch_part}")
        # The Adoptium ``arch=`` query param is a hint, not a hard
        # filter — the API returns every arch for the os (e.g. both
        # x64 and aarch64 for mac). Match the requested arch explicitly;
        # picking ``assets[0]`` blindly hands an Apple-Silicon host the
        # x64 JDK (runs under Rosetta at best, fails at worst).
        for asset in assets:
            if asset.get("binary", {}).get("architecture") == arch_part:
                return asset["binary"]["package"]["link"]
        raise JdkBootstrapError(
            f"no {arch_part} Adoptium asset for {self._version}/{os_part}; "
            f"got: {[a.get('binary', {}).get('architecture') for a in assets]}"
        )

    async def ensure(self) -> Path:
        """Return the path to ``bin/java``; download if missing.

        Idempotent — second and subsequent calls are a marker check
        plus a look at the recorded path, with no subprocess. Running
        ``java -version`` to validate the cache was the old approach
        and it had two failure modes that both ended in a 348 MB
        redownload: it probed a path the extract never creates (see
        :meth:`_adopt_cached_java_bin`), and it killed the probe after
        15 s, which a cold JVM on a freshly unpacked JDK can exceed
        while macOS verifies the new binaries.
        """
        self._cache_root.mkdir(parents=True, exist_ok=True)
        if self.marker_path.exists() and self._adopt_cached_java_bin():
            logger.debug(
                "JDK %s/%s already installed at %s",
                self._vendor,
                self._version,
                self._jdk_bin,
            )
            return self._jdk_bin

        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                await self._download_and_extract()
                # The comment here said "raises if broken". It returns
                # a bool, and the value was dropped — so an extract
                # that produced nothing runnable was stamped good and
                # the retry below could never fire.
                if not self._jdk_bin.exists():
                    raise JdkBootstrapError(
                        f"JDK {self._vendor}-{self._version} produced no java binary"
                    )
                self._write_marker()
                return self._jdk_bin
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "JDK bootstrap attempt %d failed: %s; cleaning up",
                    attempt,
                    exc,
                )
                self._wipe_partial()
        raise JdkBootstrapError(
            f"failed to install JDK {self._vendor}-{self._version} after 2 attempts: {last_exc}"
        )

    async def _download_and_extract(self) -> None:
        """Fetch the JDK archive into a temp file and extract to cache_root.

        Uses httpx streaming so a ~190 MB download doesn't fully
        buffer in memory. Extracts via :mod:`tarfile` (mac/linux) or
        :mod:`zipfile` (windows) via :mod:`asyncio.to_thread`.
        """
        if self._install_dir.exists():
            shutil.rmtree(self._install_dir, ignore_errors=True)

        url = await self._resolve_download_url()
        suffix = ".zip" if sys.platform == "win32" else ".tar.gz"
        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
            dir=self._cache_root,
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            logger.info(
                "downloading JDK %s/%s from %s",
                self._vendor,
                self._version,
                url,
            )
            await self._stream_to(url, tmp_path)
            await asyncio.to_thread(self._extract, tmp_path)
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink()

        # Find the actual java binary — Adoptium tarballs place it under
        # ``<extracted>/jdk-<version>/Contents/Home/bin/java`` on macOS
        # and ``<extracted>/jdk-<version>/bin/java`` on Linux/Windows.
        candidates = list(self._cache_root.glob("**/bin/java"))
        java_bins = [p for p in candidates if p.is_file() and not p.is_symlink()]
        if not java_bins:
            raise JdkBootstrapError(
                f"no bin/java found after extraction; candidates were: {candidates}"
            )
        if len(java_bins) > 1:
            logger.warning("multiple java binaries found: %s; using first", java_bins)
        actual_java_bin = java_bins[0]
        # Update _jdk_bin to the actual path (binds tighter than property setter).
        object.__setattr__(self, "_jdk_bin", actual_java_bin)

    async def _stream_to(self, url: str, dest: Path) -> None:
        """Stream the archive to ``dest`` via httpx async client."""
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=300.0)
        async with (
            httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client,
            client.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    fh.write(chunk)

    def _extract(self, archive: Path) -> None:
        """Extract the archive into ``cache_root``."""
        if sys.platform == "win32":
            self._extract_zip(archive)
        else:
            self._extract_tar(archive)

    def _extract_tar(self, archive: Path) -> None:
        """Extract tar.gz / tar.bz2 into cache_root."""
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(self._cache_root, filter="data")

    def _extract_zip(self, archive: Path) -> None:
        """Extract ZIP into cache_root (Windows)."""
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(self._cache_root)

    def _write_marker(self) -> None:
        """Stamp the install as good-after-probe.

        Records ``java_bin``, which is the whole point: the extracted
        layout is not the one this class predicts, so the path found
        after extraction is knowledge the next process cannot
        reconstruct and must be told.
        """
        self.marker_path.write_text(
            json.dumps(
                {
                    "vendor": self._vendor,
                    "version": self._version,
                    "install_dir": str(self._install_dir),
                    "java_bin": str(self._jdk_bin),
                    "probed_at": int(time.time()),
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _adopt_cached_java_bin(self) -> bool:
        """Point ``_jdk_bin`` at the JDK a previous run installed.

        ``__init__`` guesses ``<cache>/<vendor>-<version>/bin/java``.
        Adoptium tarballs do not unpack to that name — on this machine
        the real path is ``<cache>/jdk-21.0.12.1+1/Contents/Home/bin/java``
        — and :meth:`_download_and_extract` papers over the difference
        by rebinding ``_jdk_bin`` to whatever it finds. That rebinding
        lives in the process that downloaded and dies with it, so every
        later process probed a path that had never existed, concluded
        the cache was broken, and pulled 348 MB again. Every backend
        start.

        Reads the path from the marker, falling back to a search for
        markers written before the field existed.
        """
        if self._jdk_bin.exists():
            return True
        try:
            recorded = json.loads(self.marker_path.read_text(encoding="utf-8")).get("java_bin")
        except (OSError, ValueError):
            recorded = None
        if recorded and Path(recorded).exists():
            object.__setattr__(self, "_jdk_bin", Path(recorded))
            return True
        found = [p for p in self._cache_root.glob("**/bin/java") if p.is_file()]
        if found:
            object.__setattr__(self, "_jdk_bin", found[0])
            return True
        return False

    def _wipe_partial(self) -> None:
        """Best-effort cleanup after a failed install."""
        if self._install_dir.exists():
            shutil.rmtree(self._install_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            self.marker_path.unlink()
