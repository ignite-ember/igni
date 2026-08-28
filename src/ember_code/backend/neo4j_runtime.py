"""BE-spawned Neo4j sidecar — process lifecycle, refcount, bootstrap.

Owns the Neo4j server subprocess for the lifetime of the BE process.
Multiple BEs (one per open project window) share the same sidecar
via a refcount published to ``~/.ember/neo4j.runtime.json``.

Module contents:

* :class:`Neo4jEndpoints` — value type carrying the running
  sidecar's bolt + HTTP URIs, PID, and auth credentials.
* :class:`Neo4jBootstrap` — ensures the Neo4j distribution is
  installed under ``~/.ember/neo4j/<version>/``. Downloads the
  tarball on first launch (httpx + tarfile extract) and writes a
  ``.installed-<version>`` marker so subsequent launches skip the
  download. Probes ``./bin/neo4j --version`` to verify the
  extraction is intact; on probe failure the partial install is
  wiped and one retry is attempted before raising.
* :class:`Neo4jRuntime` — refcounted singleton. :meth:`start`
  increments the refcount (or spawns a fresh server if no
  sidecar is reachable). :meth:`stop` decrements; the sidecar
  process is shut down on the 0→1 transition.
* :class:`Neo4jDiscovery` — read/write ``neo4j.runtime.json`` so
  a second BE can attach to the running sidecar via PID + bolt
  probe instead of spawning a duplicate.

The runtime never blocks the BE forever: it surfaces async
``wait_until_ready`` so callers can compose the start with other
background-service init steps (mirrors how the WebSocket transport
publishes a ``start()`` event).

Design notes:

* BE-agnostic. The runtime is constructed from ``Neo4jConfig`` and
  a ``data_dir``; it doesn't import anything from
  :mod:`ember_code.core.code_index`. The client (driver + queries)
  lives in :mod:`ember_code.core.code_index.neo4j_client`; the
  runtime only owns the process + bolt URI.
* Tauri precedent. The download-and-cache pattern mirrors
  ``clients/tauri/src-tauri/src/runtime.rs:ensure_backend_python`` —
  a marker file at the cache root decides whether to redownload.
  We use ``httpx`` (already a dependency) for the fetch.
* Refcount is in-memory on the singleton instance, NOT cross-
  process. Each BE increments the shared ``Neo4jRuntime`` counter;
  the last BE to exit (refcount→0) stops the process. The on-disk
  ``neo4j.runtime.json`` only carries the PID/bolt URI/auth — it
  isn't a refcount source. Cross-process refcount would need a
  lockfile with PID ownership; deferred.
* Port allocation mirrors :class:`WebSocketServerTransport`'s
  ``port=0`` auto-bind — bind a socket, read its assigned port,
  close, then pass that port to Neo4j's ``server.bolt.listen_address``.
* Auth: first spawn generates a random password and stashes it
  in ``<data_dir>/neo4j.auth`` (chmod 0o600). Subsequent BEs that
  discover the running sidecar read the password from there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ember_code.backend.jdk_bootstrap import JdkBootstrap, JdkBootstrapError

if TYPE_CHECKING:
    from neo4j import AsyncDriver

logger = logging.getLogger(__name__)


# Default Neo4j version — pinned to a known-good LTS. Override via
# :attr:`Neo4jConfig.version`. Bumping requires updating the tarball
# URL pattern below.
DEFAULT_NEO4J_VERSION = "5.26.0"

# Tarball URL template. Neo4j's distribution layout is stable across
# 5.x releases: ``https://dist.neo4j.org/neo4j-community-<version>-unix.tar.gz``.
# Override via ``Neo4jConfig.download_url`` for tests / air-gapped installs.
_DEFAULT_TARBALL_URL = "https://dist.neo4j.org/neo4j-community-{version}-unix.tar.gz"

# Memory sizing — defaults match the locked-in defaults from the plan
# (initial heap 512m, max heap 2g). Tunable via Neo4jConfig.
_DEFAULT_HEAP_INITIAL = "512m"
_DEFAULT_HEAP_MAX = "2g"

# Probe timing.
_DEFAULT_STARTUP_TIMEOUT_SEC = 60.0
_PROBE_INTERVAL_SEC = 0.25

# Shutdown grace window before SIGKILL.
#
# Neo4j checkpoints on a clean shutdown and only then. SIGKILL mid-checkpoint
# leaves the store unusable, so the next session rebuilds the commit from its
# changeset — which means re-embedding every chunk, and embedding is the whole
# cost of a load: 16 minutes for celery, 77 for sqlalchemy on an M-series
# machine. Ten seconds is not enough time for a large store to checkpoint, so the
# old default silently converted "close the session" into "throw the graph away".
# Measured: 22 per-commit state directories on this machine, every one of them
# 16K — config and a password file, no data.
#
# Two minutes is generous for the checkpoint and still bounded; a process that
# has not exited by then is stuck rather than busy.
_DEFAULT_SHUTDOWN_GRACE_SEC = 120.0

# Discovery file location — lives outside any project so all BEs see it.
_AUTH_FILE = "neo4j.auth"
_HEAP_INITIAL_KEY = "server.memory.heap.initial_size"
_HEAP_MAX_KEY = "server.memory.heap.max_size"


@dataclass
class Neo4jEndpoints:
    """Resolved bolt + HTTP endpoints for a running sidecar.

    Mirrors the wire shape of :class:`LockfilePayload` so the BE can
    publish the same fields into its existing discovery file format
    (or extend it without breaking readers).
    """

    bolt_uri: str
    http_uri: str
    pid: int
    user: str
    password_file: Path

    def to_discovery_dict(self) -> dict[str, Any]:
        """Serialise for ``neo4j.runtime.json`` on disk.

        Returns a dict that ``Neo4jDiscovery.from_runtime_json``
        can parse back without ambiguity. Includes ``version`` and
        ``data_dir`` so a future migration can detect version drift.
        """
        return {
            "bolt_uri": self.bolt_uri,
            "http_uri": self.http_uri,
            "pid": self.pid,
            "user": self.user,
            "password_file": str(self.password_file),
        }


class _ProjectCommitState:
    """In-memory state for one scope: a ``(project, commit)``
    code process OR a ``(project, knowledge)`` knowledge
    process.

    Holds the live subprocess, the cached driver, the bolt/HTTP
    endpoints, and the refcount. There's one of these per active
    scope in :attr:`Neo4jRuntime._processes`. The two process
    types share the same lifecycle code; only the data
    directory and lifecycle semantics differ.
    """

    def __init__(
        self,
        *,
        proc: asyncio.subprocess.Process,
        endpoints: Neo4jEndpoints,
        refcount: int = 1,
        state_dir: Path,
        auth_file: Path,
        config_file: Path,
        logs_dir: Path,
        data_dir: Path,
        runtime_file: Path,
    ) -> None:
        self.proc = proc
        self.endpoints = endpoints
        self.refcount = refcount
        # Cached on first driver_for() call; reset on _shutdown_one().
        self.driver: Any | None = None
        # Per-process paths.
        self.state_dir = state_dir
        self.auth_file = auth_file
        self.config_file = config_file
        self.logs_dir = logs_dir
        self.data_dir = data_dir
        self.runtime_file = runtime_file


class Neo4jBootstrap:
    """Ensures the Neo4j distribution tarball is extracted locally.

    Download flow (matches Tauri's ``ensure_backend_python``):

    1. Read ``<cache_root>/.installed-<version>`` marker.
    2. If marker present and ``<cache_root>/bin/neo4j`` probes
       ``--version`` cleanly → return the cached install path.
    3. Else download the tarball via httpx, extract, probe again.
    4. On probe failure after extraction → wipe the partial install,
       retry once. If the retry also fails → raise
       :class:`Neo4jBootstrapError`.

    The marker file is written only after a successful probe, so a
    crash mid-extract doesn't leave the cache in a "marked-good but
    actually broken" state.
    """

    def __init__(
        self,
        *,
        version: str,
        cache_root: Path,
        tarball_url: str | None = None,
    ):
        self._version = version
        self._cache_root = Path(cache_root).expanduser()
        self._tarball_url = tarball_url or _DEFAULT_TARBALL_URL.format(version=version)
        self._install_dir = self._cache_root / f"neo4j-community-{version}"

    @property
    def install_dir(self) -> Path:
        return self._install_dir

    @property
    def marker_path(self) -> Path:
        return self._cache_root / f".installed-{self._version}"

    @property
    def neo4j_bin(self) -> Path:
        return self._install_dir / "bin" / "neo4j"

    async def ensure(self) -> Path:
        """Return the path to the ``neo4j`` binary; download if missing.

        Idempotent — second and subsequent calls are a marker check +
        a probe. The probe guards against partial extracts that left
        the marker written by a prior run.
        """
        self._cache_root.mkdir(parents=True, exist_ok=True)
        if self.marker_path.exists() and await self._probe_version():
            logger.debug("neo4j %s already installed at %s", self._version, self._install_dir)
            return self.neo4j_bin

        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                await self._download_and_extract()
                await self._probe_version()  # raises if extraction is broken
                self._write_marker()
                return self.neo4j_bin
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "neo4j bootstrap attempt %d failed: %s; cleaning up",
                    attempt,
                    exc,
                )
                self._wipe_partial()
        raise Neo4jBootstrapError(
            f"failed to install Neo4j {self._version} after 2 attempts: {last_exc}"
        )

    async def _download_and_extract(self) -> None:
        """Fetch the tarball into a temp dir and extract to cache_root.

        Uses httpx streaming so a 250MB download doesn't fully
        buffer in memory. Extracts via :mod:`tarfile` (sync, since
        we run it via ``asyncio.to_thread``).
        """
        # Pre-clean: if a previous attempt left a half-extracted tree,
        # blow it away before we re-extract.
        if self._install_dir.exists():
            shutil.rmtree(self._install_dir, ignore_errors=True)

        with tempfile.NamedTemporaryFile(
            suffix=".tar.gz", delete=False, dir=self._cache_root
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            logger.info("downloading Neo4j %s from %s", self._version, self._tarball_url)
            await self._stream_to(tmp_path)
            await asyncio.to_thread(self._extract, tmp_path)
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink()

    async def _stream_to(self, dest: Path) -> None:
        """Stream the tarball to ``dest`` via httpx async client."""
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=300.0)
        async with (
            httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client,
            client.stream("GET", self._tarball_url) as resp,
        ):
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)

    def _extract(self, tarball: Path) -> None:
        """Extract the tarball into ``cache_root``.

        The distribution tarball contains a single top-level directory
        (``neo4j-community-<version>/``); :mod:`tarfile` with default
        filter handles the modern extract-safety rules.
        """
        with tarfile.open(tarball, "r:gz") as tar:
            # ``data`` filter (3.12+) strips absolute paths and
            # ``..``; ``fully_trusted`` is the old default which we
            # explicitly avoid since we don't control the upstream.
            tar.extractall(self._cache_root, filter="data")

    async def _probe_version(self) -> bool:
        """Run ``./bin/neo4j --version``; return True iff exit=0.

        Used both as a "did extraction succeed?" probe and as a
        cached-marker validator. The probe is fast (sub-second) so
        we always run it before trusting the cache.
        """
        if not self.neo4j_bin.exists():
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self.neo4j_bin),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, PermissionError):
            return False
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0

    def _write_marker(self) -> None:
        """Stamp the install as good-after-probe.

        Marker includes the install path so a moved cache doesn't
        silently inherit a stale marker. The probe on the next call
        also catches marker drift, so this is belt-and-suspenders.
        """
        self.marker_path.write_text(
            json.dumps(
                {
                    "version": self._version,
                    "install_dir": str(self._install_dir),
                    "probed_at": int(time.time()),
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _wipe_partial(self) -> None:
        """Best-effort cleanup after a failed install."""
        if self._install_dir.exists():
            shutil.rmtree(self._install_dir, ignore_errors=True)
        if self.marker_path.exists():
            with contextlib.suppress(OSError):
                self.marker_path.unlink()


class Neo4jBootstrapError(RuntimeError):
    """Raised when Neo4j bootstrap cannot produce a working binary."""


class Neo4jRuntime:
    """Per-(project, commit) Neo4j process manager.

    Each ``(project, commit)`` pair gets its own Neo4j process
    with its own data directory, auth file, and bolt port. The
    process boundary is a **safety guarantee** — the only data in
    any one process is that one commit's data, so a misbehaving
    query can only return that commit's items. (Belt-and-suspenders
    with the relationship-model ``commit_sha`` property on the
    edge; the property is the query-time filter, the process is
    the storage-time isolation.)

    Construction is cheap (no I/O). The first call to
    :meth:`start_for_commit` for a given ``(project, commit)``
    spawns that pair's process; subsequent BEs that want the
    same pair attach to it (refcount bump, no duplicate spawn).
    Switching commits within a project = stop the old
    ``(project, old_sha)`` and start the new ``(project, new_sha)``.

    Lifetime contract:

    * Per-``(project, commit)`` refcount. A BE that calls
      :meth:`start_for_commit` twice for the same pair only
      releases once on its single :meth:`stop_for_commit`.
    * :meth:`stop_for_commit` decrements and returns ``True``
      when that pair's process was actually shut down (refcount
      reached 0). ``False`` otherwise.
    * :meth:`stop_all` (called at BE shutdown) kills every
      active pair's process regardless of refcount.

    File layout under ``<data_dir>``::

        neo4j/dist/<version>/                    # shared distribution
        neo4j/state/<project>-<commit>/          # one per (project, commit)
            data/                                # Neo4j store
            logs/                                # Neo4j stdout/stderr
            auth.txt                             # per-process password
            runtime.json                         # per-process endpoints
            neo4j.conf                           # per-process config
    """

    def __init__(
        self,
        *,
        data_dir: str | Path,
        version: str = DEFAULT_NEO4J_VERSION,
        download_url: str | None = None,
        startup_timeout_sec: float = _DEFAULT_STARTUP_TIMEOUT_SEC,
        shutdown_grace_sec: float = _DEFAULT_SHUTDOWN_GRACE_SEC,
        heap_initial: str = _DEFAULT_HEAP_INITIAL,
        heap_max: str = _DEFAULT_HEAP_MAX,
        host: str = "127.0.0.1",
    ):
        self._data_dir = Path(str(data_dir)).expanduser()
        # Resolve via the canonical paths module so the runtime
        # and the cutover script agree on layout.
        from ember_code.core.code_index.paths import (
            neo4j_data_dir,
        )

        # ``self._state_root`` is the per-``(project, commit)``
        # parent dir. The shared distribution lives under
        # ``self._neo4j_root`` (same path for every project).
        self._state_root = neo4j_data_dir(self._data_dir) / "state"
        self._neo4j_root = neo4j_data_dir(self._data_dir)
        self._version = version
        self._download_url = download_url
        self._startup_timeout = startup_timeout_sec
        self._shutdown_grace = shutdown_grace_sec
        self._heap_initial = heap_initial
        self._heap_max = heap_max
        self._host = host

        # ``self._processes`` is the per-``(project, commit)`` state
        # table. Keyed by ``(project_hash, commit_sha)`` tuple.
        # Each entry holds the subprocess, the cached driver, the
        # bolt/HTTP endpoints, and the refcount.
        self._processes: dict[tuple[str, str], _ProjectCommitState] = {}
        self._lock = asyncio.Lock()

    # ── Public surface ─────────────────────────────────────────────

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def neo4j_root(self) -> Path:
        return self._neo4j_root

    def endpoints_for(self, project_hash: str, commit_sha: str) -> Neo4jEndpoints | None:
        """The resolved bolt + HTTP endpoints for a ``(project, commit)`` pair.

        ``None`` until :meth:`start_for_commit` has been called
        for the pair.
        """
        state = self._processes.get((project_hash, commit_sha))
        return state.endpoints if state is not None else None

    def is_running_for(self, project_hash: str, commit_sha: str) -> bool:
        state = self._processes.get((project_hash, commit_sha))
        return state is not None and state.proc.returncode is None

    def tracked_shas(self, project_hash: str) -> list[str]:
        """Return all tracked commit SHAs for a project.

        The runtime's ``_processes`` dict is the source of truth for
        which commits have been opened in this session. Used by
        ``CodeIndex.ahas_commit`` and ``CodeIndex.clean`` in place
        of the deprecated meta-DB queries.
        """
        return [
            commit_sha for (proj, commit_sha), _ in self._processes.items() if proj == project_hash
        ]

    def driver_for(self, project_hash: str, commit_sha: str) -> AsyncDriver:
        """Return the per-``(project, commit)`` driver.

        Constructs the driver on first call, then caches it. The
        driver is closed in :meth:`_shutdown_one` so subsequent
        callers in the same process get a fresh one (e.g. after
        an unclean shutdown + respawn).

        Raises ``RuntimeError`` if the pair hasn't been started.
        """
        state = self._processes.get((project_hash, commit_sha))
        if state is None:
            raise RuntimeError(
                f"Neo4jRuntime.start_for_commit({project_hash!r}, "
                f"{commit_sha!r}) must be called before driver_for(...)"
            )
        if state.driver is None:
            password = state.auth_file.read_text(encoding="utf-8").strip()
            auth = (state.endpoints.user, password)
            from neo4j import AsyncGraphDatabase  # lazy: heavy import

            state.driver = AsyncGraphDatabase.driver(
                state.endpoints.bolt_uri,
                auth=auth,
                max_connection_pool_size=50,
            )
        return state.driver

    async def start_for_commit(self, project_hash: str, commit_sha: str) -> Neo4jEndpoints:
        """Ensure a Neo4j process exists for ``(project, commit)``; bump refcount.

        Discovers an existing process for the pair first (the
        prior BE left a per-pair ``runtime.json`` with PID +
        ports). If the PID is alive AND the bolt port responds,
        attach via refcount. Otherwise, spawn a fresh process for
        the pair.
        """
        key = (project_hash, commit_sha)
        async with self._lock:
            state = self._processes.get(key)
            if state is not None and state.proc.returncode is None:
                state.refcount += 1
                logger.debug(
                    "neo4j (project=%s commit=%s) already running (pid=%d); refcount=%d",
                    project_hash,
                    commit_sha,
                    state.endpoints.pid,
                    state.refcount,
                )
                return state.endpoints

            existing = self._discover(project_hash, commit_sha)
            if existing is not None and self._is_alive(existing):
                # Attach to the existing process. We don't have a
                # subprocess.Popen handle — the original BE
                # started it — so we synthesize a minimal handle
                # wrapper that supports ``.pid`` and the
                # ``send_signal``/``wait`` shape ``_shutdown_one``
                # needs. The OS still owns the actual process; we
                # can signal it but not await its exit reliably.
                proc = _ExternalProcessHandle(existing.pid)
                state = _ProjectCommitState(
                    proc=proc,
                    endpoints=existing,
                    refcount=1,
                    state_dir=self._state_root / _pair_slug(project_hash, commit_sha),
                    auth_file=self._auth_path(project_hash, commit_sha),
                    config_file=self._config_path(project_hash, commit_sha),
                    logs_dir=self._logs_path(project_hash, commit_sha),
                    data_dir=self._data_path(project_hash, commit_sha),
                    runtime_file=self._runtime_path(project_hash, commit_sha),
                )
                self._processes[key] = state
                logger.info(
                    "attached to existing (project=%s commit=%s pid=%d)",
                    project_hash,
                    commit_sha,
                    existing.pid,
                )
                return existing

            # Cold path: bootstrap (download if needed) + spawn.
            bootstrap = Neo4jBootstrap(
                version=self._version,
                cache_root=self._neo4j_root,
                tarball_url=self._download_url,
            )
            neo4j_bin = await bootstrap.ensure()
            state = await self._spawn_one(project_hash, commit_sha, neo4j_bin)
            self._processes[key] = state
            self._save_runtime(state)
            return state.endpoints

    async def stop_for_commit(self, project_hash: str, commit_sha: str) -> bool:
        """Decrement refcount; kill the pair's process at zero.

        Returns True iff THIS call performed the shutdown (last
        to leave). ``False`` means other BEs are still attached.
        """
        key = (project_hash, commit_sha)
        async with self._lock:
            state = self._processes.get(key)
            if state is None:
                return False
            state.refcount -= 1
            if state.refcount > 0:
                logger.debug(
                    "neo4j (project=%s commit=%s) still attached by %d other BE(s)",
                    project_hash,
                    commit_sha,
                    state.refcount,
                )
                return False
            popped = self._processes.pop(key)
        return await self._shutdown_one(popped, project_hash, commit_sha)

    async def start_for_knowledge(self, project_hash: str) -> Neo4jEndpoints:
        """Ensure a Neo4j process exists for ``(project, knowledge)``.

        Knowledge is per-project (not per-commit) — the process
        is spawned once per project and stays alive across
        commit switches. Refcount bumps on every call; the
        process dies only when no BE is using it.
        """
        key = (project_hash, "knowledge")
        async with self._lock:
            state = self._processes.get(key)
            if state is not None and state.proc.returncode is None:
                state.refcount += 1
                logger.debug(
                    "neo4j (project=%s scope=knowledge) already running (pid=%d); refcount=%d",
                    project_hash,
                    state.endpoints.pid,
                    state.refcount,
                )
                return state.endpoints

            existing = self._discover_knowledge(project_hash)
            if existing is not None and self._is_alive(existing):
                proc = _ExternalProcessHandle(existing.pid)
                state = _ProjectCommitState(
                    proc=proc,
                    endpoints=existing,
                    refcount=1,
                    state_dir=self._state_dir_knowledge(project_hash),
                    auth_file=self._auth_path_knowledge(project_hash),
                    config_file=self._config_path_knowledge(project_hash),
                    logs_dir=self._logs_path_knowledge(project_hash),
                    data_dir=self._data_path_knowledge(project_hash),
                    runtime_file=self._runtime_path_knowledge(project_hash),
                )
                self._processes[key] = state
                logger.info(
                    "attached to existing knowledge (project=%s pid=%d)",
                    project_hash,
                    existing.pid,
                )
                return existing

            bootstrap = Neo4jBootstrap(
                version=self._version,
                cache_root=self._neo4j_root,
                tarball_url=self._download_url,
            )
            neo4j_bin = await bootstrap.ensure()
            state = await self._spawn_one_knowledge(project_hash, neo4j_bin)
            self._processes[key] = state
            self._save_runtime(state)
            return state.endpoints

    def driver_for_knowledge(self, project_hash: str) -> AsyncDriver:
        """Return the per-``(project, knowledge)`` driver."""
        state = self._processes.get((project_hash, "knowledge"))
        if state is None:
            raise RuntimeError(
                f"Neo4jRuntime.start_for_knowledge({project_hash!r}) must be "
                f"called before driver_for_knowledge(...)"
            )
        if state.driver is None:
            password = state.auth_file.read_text(encoding="utf-8").strip()
            auth = (state.endpoints.user, password)
            from neo4j import AsyncGraphDatabase

            state.driver = AsyncGraphDatabase.driver(
                state.endpoints.bolt_uri,
                auth=auth,
                max_connection_pool_size=50,
            )
        return state.driver

    async def stop_for_knowledge(self, project_hash: str) -> bool:
        """Decrement refcount; kill the knowledge process at zero.

        Knowledge processes are per-project, so the
        refcount-to-zero typically only happens at BE shutdown.
        """
        key = (project_hash, "knowledge")
        async with self._lock:
            state = self._processes.get(key)
            if state is None:
                return False
            state.refcount -= 1
            if state.refcount > 0:
                return False
            popped = self._processes.pop(key)
        return await self._shutdown_one(popped, project_hash, "knowledge")

    def endpoints_for_knowledge(self, project_hash: str) -> Neo4jEndpoints | None:
        state = self._processes.get((project_hash, "knowledge"))
        return state.endpoints if state is not None else None

    def _discover_knowledge(self, project_hash: str) -> Neo4jEndpoints | None:
        """Read the per-project knowledge runtime.json."""
        path = self._runtime_path_knowledge(project_hash)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("neo4j knowledge file at %s unreadable: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            return Neo4jEndpoints(
                bolt_uri=str(data["bolt_uri"]),
                http_uri=str(data["http_uri"]),
                pid=int(data["pid"]),
                user=str(data["user"]),
                password_file=Path(str(data["password_file"])),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("neo4j knowledge file %s schema check: %s", path, exc)
            return None

    async def _spawn_one_knowledge(self, project_hash: str, neo4j_bin: Path) -> _ProjectCommitState:
        """Spawn the per-project knowledge Neo4j process.

        Same shape as :meth:`_spawn_one` but writes to the
        per-project knowledge state dir (which is stable
        across commit switches).
        """
        state_dir = self._state_dir_knowledge(project_hash)
        state_dir.mkdir(parents=True, exist_ok=True)

        bolt_port = _pick_free_port(self._host)
        http_port = _pick_free_port(self._host)
        password = self._ensure_password(self._auth_path_knowledge(project_hash))
        user = "neo4j"

        # Before the config is written: the paths go *into* it.
        data_dir = self._data_path_knowledge(project_hash)
        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = self._logs_path_knowledge(project_hash)
        logs_dir.mkdir(parents=True, exist_ok=True)

        config_path = self._config_path_knowledge(project_hash)
        config_path.write_text(
            self._build_config(bolt_port, http_port, password, data_dir, logs_dir),
            encoding="utf-8",
        )

        env = {
            **os.environ,
            "JAVA_HOME": str(await self._detect_java_home(neo4j_bin)),
            "NEO4J_HOME": str(neo4j_bin.parent.parent),
            "NEO4J_CONF": str(config_path.parent),
            "NEO4J_DATA": str(data_dir),
            "NEO4J_LOGS": str(logs_dir),
        }

        stderr_file = logs_dir / "neo4j.stderr"
        logger.info(
            "spawning neo4j knowledge for (project=%s): bolt=%d http=%d data=%s",
            project_hash,
            bolt_port,
            http_port,
            data_dir,
        )
        stderr_handle = stderr_file.open("wb")
        proc = await asyncio.create_subprocess_exec(
            str(neo4j_bin),
            "console",
            cwd=str(neo4j_bin.parent.parent),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_handle,
            start_new_session=True,
        )
        await self._wait_for_bolt(
            bolt_port, user, password, proc, stderr_file, stderr_handle=stderr_handle
        )

        return _ProjectCommitState(
            proc=proc,
            endpoints=Neo4jEndpoints(
                bolt_uri=f"bolt://{self._host}:{bolt_port}",
                http_uri=f"http://{self._host}:{http_port}",
                pid=proc.pid,
                user=user,
                password_file=self._auth_path_knowledge(project_hash),
            ),
            refcount=1,
            state_dir=state_dir,
            auth_file=self._auth_path_knowledge(project_hash),
            config_file=config_path,
            logs_dir=logs_dir,
            data_dir=data_dir,
            runtime_file=self._runtime_path_knowledge(project_hash),
        )

    async def stop_all(self) -> None:
        """Kill every active pair's process.

        Called at BE shutdown. Refcounts are ignored — we want
        all processes dead before the BE exits.
        """
        async with self._lock:
            states = list(self._processes.items())
            self._processes.clear()
        for (project_hash, commit_sha), state in states:
            await self._shutdown_one(state, project_hash, commit_sha)

    # ── Internals ──────────────────────────────────────────────────

    # Per-``(project, commit)`` path helpers. Every file lives
    # under ``<state_root>/<project>-<commit>/`` so a single
    # project's commits are co-located and easy to enumerate /
    # bulk-delete.

    def _state_dir(self, project_hash: str, commit_sha: str) -> Path:
        return self._state_root / _pair_slug(project_hash, commit_sha)

    def _state_dir_knowledge(self, project_hash: str) -> Path:
        """Per-project state dir for the knowledge process.

        Knowledge is per-project (not per-commit) — knowledge
        data survives commit switches. The state dir uses
        ``<project>-knowledge`` as the slug, distinguishable
        from ``<project>-<commit>``.
        """
        return self._state_root / f"{project_hash}-knowledge"

    def _data_path_knowledge(self, project_hash: str) -> Path:
        return self._state_dir_knowledge(project_hash) / "data"

    def _logs_path_knowledge(self, project_hash: str) -> Path:
        return self._state_dir_knowledge(project_hash) / "logs"

    def _auth_path_knowledge(self, project_hash: str) -> Path:
        return self._state_dir_knowledge(project_hash) / "auth.txt"

    def _config_path_knowledge(self, project_hash: str) -> Path:
        return self._state_dir_knowledge(project_hash) / "neo4j.conf"

    def _runtime_path_knowledge(self, project_hash: str) -> Path:
        return self._state_dir_knowledge(project_hash) / "runtime.json"

    def _data_path(self, project_hash: str, commit_sha: str) -> Path:
        return self._state_dir(project_hash, commit_sha) / "data"

    def _logs_path(self, project_hash: str, commit_sha: str) -> Path:
        return self._state_dir(project_hash, commit_sha) / "logs"

    def _auth_path(self, project_hash: str, commit_sha: str) -> Path:
        return self._state_dir(project_hash, commit_sha) / "auth.txt"

    def _config_path(self, project_hash: str, commit_sha: str) -> Path:
        return self._state_dir(project_hash, commit_sha) / "neo4j.conf"

    def _runtime_path(self, project_hash: str, commit_sha: str) -> Path:
        return self._state_dir(project_hash, commit_sha) / "runtime.json"

    def _is_alive(self, endpoints: Neo4jEndpoints) -> bool:
        """Two-probe check: PID alive AND bolt port reachable."""
        try:
            os.kill(endpoints.pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return _is_port_open(self._host, _bolt_port(endpoints.bolt_uri))

    async def _spawn_one(
        self, project_hash: str, commit_sha: str, neo4j_bin: Path
    ) -> _ProjectCommitState:
        """Boot a fresh sidecar for one ``(project, commit)`` pair.

        Picks a free bolt + HTTP port, generates a per-pair
        password, writes a per-pair ``neo4j.conf``, and starts a
        subprocess with ``start_new_session=True`` (the child
        becomes its own process group so SIGTERM-the-group kills
        every JVM thread Neo4j spawns).
        """
        state_dir = self._state_dir(project_hash, commit_sha)
        state_dir.mkdir(parents=True, exist_ok=True)

        bolt_port = _pick_free_port(self._host)
        http_port = _pick_free_port(self._host)
        password = self._ensure_password(self._auth_path(project_hash, commit_sha))
        user = "neo4j"

        # Before the config is written: the paths go *into* it.
        data_dir = self._data_path(project_hash, commit_sha)
        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = self._logs_path(project_hash, commit_sha)
        logs_dir.mkdir(parents=True, exist_ok=True)

        config_path = self._config_path(project_hash, commit_sha)
        config_path.write_text(
            self._build_config(bolt_port, http_port, password, data_dir, logs_dir),
            encoding="utf-8",
        )

        env = {
            **os.environ,
            "JAVA_HOME": str(await self._detect_java_home(neo4j_bin)),
            "NEO4J_HOME": str(neo4j_bin.parent.parent),
            "NEO4J_CONF": str(config_path.parent),
            "NEO4J_DATA": str(data_dir),
            "NEO4J_LOGS": str(logs_dir),
        }

        stderr_file = logs_dir / "neo4j.stderr"
        logger.info(
            "spawning neo4j for (project=%s commit=%s): bolt=%d http=%d data=%s",
            project_hash,
            commit_sha,
            bolt_port,
            http_port,
            data_dir,
        )
        stderr_handle = stderr_file.open("wb")
        proc = await asyncio.create_subprocess_exec(
            str(neo4j_bin),
            "console",
            cwd=str(neo4j_bin.parent.parent),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_handle,
            # Detach into its own process group so shutdown can signal
            # the whole subtree (Java + helpers) without touching the
            # BE's own children. Same isolation as the knowledge path
            # — mismatched session leadership was a bug, not a
            # deliberate choice.
            start_new_session=True,
        )

        await self._wait_for_bolt(
            bolt_port, user, password, proc, stderr_file, stderr_handle=stderr_handle
        )

        return _ProjectCommitState(
            proc=proc,
            endpoints=Neo4jEndpoints(
                bolt_uri=f"bolt://{self._host}:{bolt_port}",
                http_uri=f"http://{self._host}:{http_port}",
                pid=proc.pid,
                user=user,
                password_file=self._auth_path(project_hash, commit_sha),
            ),
            refcount=1,
            state_dir=state_dir,
            auth_file=self._auth_path(project_hash, commit_sha),
            config_file=config_path,
            logs_dir=logs_dir,
            data_dir=data_dir,
            runtime_file=self._runtime_path(project_hash, commit_sha),
        )

    def _ensure_password(self, auth_file: Path) -> str:
        """Read the existing per-pair auth file or generate a new password.

        First BE generates + writes (chmod 0o600). Subsequent BEs
        just read. A missing file when one is expected is treated
        as "regenerate" (the prior sidecar was wiped).
        """
        if auth_file.exists():
            try:
                return auth_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        password = secrets.token_urlsafe(24)
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text(password + "\n", encoding="utf-8")
        try:
            os.chmod(auth_file, 0o600)
        except OSError:
            # Windows / non-POSIX FS — the password is in a
            # user-owned file already, so we don't fail.
            logger.debug("chmod 0o600 not supported on this filesystem")
        return password

    def _build_config(
        self,
        bolt_port: int,
        http_port: int,
        password: str,
        data_dir: Path,
        logs_dir: Path,
    ) -> str:
        """Render the per-pair ``neo4j.conf`` the sidecar boots with.

        ``data_dir`` and ``logs_dir`` must be absolute, and they must be written
        into the file: this is the only channel that reaches the server. Neo4j is
        started with ``--home-dir`` pointing at the *shared* install and only
        ``--config-dir`` per pair, so anything not stated here resolves against
        the install and is therefore shared by every project.

        Two things were wrong for as long as this method existed. The keys were
        the Neo4j 4.x spelling, which 5.x ignores in silence — 5.x renamed the
        whole family ``dbms.directories.*`` → ``server.directories.*``. And the
        values were relative, so even under the right key they would have
        resolved against the install rather than the pair's state dir. The
        comment they carried said the real values arrive through ``NEO4J_DATA``
        and ``NEO4J_LOGS``; the 5.x launcher reads neither.

        Measured consequence: every per-commit server stored into
        ``<install>/data/databases/neo4j``, one store shared by all of them,
        while each pair's own ``data/`` stayed empty. Each project's load
        therefore overwrote the previous project's graph, which is why nothing
        was ever reusable and every evaluation run re-embedded every repository
        from scratch — two thirds of that harness's wall clock, spent on work
        that had already been done.
        """
        return (
            f"# Auto-generated by ember-code — do not edit by hand.\n"
            f"server.bolt.enabled=true\n"
            f"server.bolt.listen_address=:{bolt_port}\n"
            f"server.http.enabled=true\n"
            f"server.http.listen_address=:{http_port}\n"
            f"dbms.security.auth_enabled=false\n"
            f"dbms.connector.bolt.enabled=true\n"
            f"dbms.default_database=neo4j\n"
            f"{_HEAP_INITIAL_KEY}={self._heap_initial}\n"
            f"{_HEAP_MAX_KEY}={self._heap_max}\n"
            f"# Absolute, and under the 5.x key: --home-dir is the shared\n"
            f"# install, so a relative path here would be shared too.\n"
            f"server.directories.data={data_dir}\n"
            f"server.directories.logs={logs_dir}\n"
        )

    def _jdk_cache_root(self) -> Path:
        """Root of the JDK cache (under the neo4j data dir)."""
        return self._neo4j_root / "jdk"

    async def _detect_java_home(self, neo4j_bin: Path) -> Path:
        """Locate a JDK for Neo4j to use.

        Three tiers (in priority order):
        1. Neo4j tarball's bundled ``jdk/`` (fast path, stat only).
        2. ``<neo4j_root>/jdk/temurin-21/`` after :class:`JdkBootstrap.ensure`.
        3. ``$JAVA_HOME`` env (CI / manually managed installs).

        Tier 2 is lazy: JdkBootstrap.ensure() is only called when tiers 1 and 3
        miss. Once cached, the marker file makes tier 2 free on subsequent calls.
        """
        # Tier 1: Neo4j tarball bundled JDK (if present).
        bundled = neo4j_bin.parent.parent / "jdk"
        if bundled.is_dir():
            return bundled

        # Tier 2: user-managed JAVA_HOME.
        fallback = os.environ.get("JAVA_HOME")
        if fallback:
            return Path(fallback)

        # Tier 3: bundled JDK downloaded by JdkBootstrap.
        try:
            jdk = JdkBootstrap(version="21", cache_root=self._jdk_cache_root())
            return (
                await jdk.ensure()
            ).parent.parent  # ensure() returns bin/java; parent.parent = JDK root
        except JdkBootstrapError:
            pass

        raise JdkBootstrapError(
            "could not locate a JDK: no bundled JDK, no JAVA_HOME, and JdkBootstrap failed"
        )

    async def _wait_for_bolt(
        self,
        port: int,
        user: str,
        password: str,
        proc: asyncio.subprocess.Process,
        stderr_file: Path | None = None,
        stderr_handle: Any | None = None,
    ) -> None:
        """Poll the bolt port until it accepts connections, the process exits, or we time out.

        A naive TCP probe is enough — once the port is open the
        auth + handshake is handled by the driver. We also check
        ``proc.returncode`` on every poll so a crash/exit is surfaced
        immediately rather than waiting the full timeout.

        ``stderr_handle`` (when provided) is the open file object
        piped to the subprocess's stderr. The child dup'd the fd at
        spawn time, so closing the parent's handle after we're done
        polling doesn't affect the subprocess's stderr redirection.

        Only on the error paths (crash-during-startup, timeout) do
        we terminate the process — the success path leaves it alive
        for the caller. This was a bug: an earlier ``finally``-based
        cleanup would kill EVERY spawned process (including successful
        ones) on the way out, so ``start_for_commit`` handed back
        endpoints pointing at a dead PID and downstream drivers saw
        "connection refused" immediately.
        """
        deadline = time.monotonic() + self._startup_timeout
        try:
            while time.monotonic() < deadline:
                # Check process health first — if it exited, read stderr and fail fast.
                if proc.returncode is not None:
                    stderr_text = ""
                    if stderr_file and stderr_file.exists():
                        try:
                            stderr_text = stderr_file.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            stderr_text = "<could not read stderr file>"
                    raise Neo4jBootstrapError(
                        f"neo4j process exited during startup (code={proc.returncode}): "
                        f"{stderr_text[:1000]}"
                    )
                if _is_port_open(self._host, port):
                    logger.info("neo4j bolt port %d is accepting connections", port)
                    # Success path — close the parent's stderr fd (the child
                    # keeps its own dup'd copy) but LEAVE THE PROCESS ALIVE.
                    if stderr_handle is not None:
                        with contextlib.suppress(Exception):
                            stderr_handle.close()
                    return
                await asyncio.sleep(_PROBE_INTERVAL_SEC)
            raise Neo4jBootstrapError(
                f"neo4j did not become reachable on {self._host}:{port} within {self._startup_timeout}s"
            )
        except BaseException:
            # Error path — reap the orphan Neo4j and close stderr.
            # `BaseException` catches both Neo4jBootstrapError raised above
            # and any surprise KeyboardInterrupt / SystemExit that might
            # otherwise leak a Java process.
            if stderr_handle is not None:
                with contextlib.suppress(Exception):
                    stderr_handle.close()
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(Exception):
                            proc.kill()
            raise

    def _save_runtime(self, state: _ProjectCommitState) -> None:
        """Write per-``(project, commit)`` runtime.json for discovery.

        Discovery is per-pair — another BE that wants the same
        pair reads the file, probes the PID + bolt port, and
        attaches to the running process (refcount bump).
        """
        state.runtime_file.parent.mkdir(parents=True, exist_ok=True)
        payload = state.endpoints.to_discovery_dict()
        # The slug in the state dir encodes the pair; recover it
        # for a sanity check in the discovery file.
        slug = state.endpoints.password_file.parent.name
        # The slug format is ``<project_hash>-<commit_sha>`` —
        # split from the right (commit sha is the rightmost
        # component, project hash is the rest).
        last_dash = slug.rfind("-")
        if last_dash > 0:
            project_hash, commit_sha = slug[:last_dash], slug[last_dash + 1 :]
        else:
            project_hash, commit_sha = state.endpoints.user, "?"
        payload["project_hash"] = project_hash
        payload["commit_sha"] = commit_sha
        state.runtime_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _discover(self, project_hash: str, commit_sha: str) -> Neo4jEndpoints | None:
        """Read this pair's runtime.json; return endpoints or None."""
        path = self._runtime_path(project_hash, commit_sha)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("neo4j runtime file at %s unreadable: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            return Neo4jEndpoints(
                bolt_uri=str(data["bolt_uri"]),
                http_uri=str(data["http_uri"]),
                pid=int(data["pid"]),
                user=str(data["user"]),
                password_file=Path(str(data["password_file"])),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("neo4j runtime file %s schema check: %s", path, exc)
            return None

    async def _shutdown_one(
        self,
        state: _ProjectCommitState | None,
        project_hash: str,
        commit_sha: str,
    ) -> bool:
        """SIGTERM a single pair's process group; escalate to SIGKILL on grace expiry."""
        if state is None:
            return True
        proc = state.proc
        pid = proc.pid
        # ``_ExternalProcessHandle`` (used when we attached to an
        # existing process via discovery) supports ``send_signal``
        # but not ``wait``. For internal processes we have the full
        # Popen handle.
        is_internal = hasattr(proc, "wait") and callable(proc.wait)
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("SIGTERM failed for neo4j pid %d: %s", pid, exc)

        if is_internal:
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._shutdown_grace)
            except asyncio.TimeoutError:
                # State the consequence, not just the signal. This is the line
                # that would have explained why every graph on disk was empty.
                logger.warning(
                    "neo4j (project=%s commit=%s pid=%d) did not exit within %ds; SIGKILL. "
                    "The checkpoint did not finish, so this commit's store is not reusable "
                    "and the next session will rebuild it from the changeset, re-embedding "
                    "every chunk. Raise shutdown_grace_sec if this repeats.",
                    project_hash,
                    commit_sha,
                    pid,
                    self._shutdown_grace,
                )
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError) as exc:
                    logger.debug("SIGKILL failed for neo4j pid %d: %s", pid, exc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.error(
                        "neo4j (project=%s commit=%s pid=%d) refuses to die",
                        project_hash,
                        commit_sha,
                        pid,
                    )
        else:
            # External process — we sent SIGTERM (and SIGKILL on
            # grace expiry, but the wait isn't possible). Just
            # sleep the grace period.
            await asyncio.sleep(self._shutdown_grace)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError) as exc:
                logger.debug("SIGKILL failed for neo4j pid %d: %s", pid, exc)

        if state.driver is not None:
            try:
                await state.driver.close()
            except Exception as exc:
                logger.debug(
                    "driver close failed for (%s, %s): %s",
                    project_hash,
                    commit_sha,
                    exc,
                )
            state.driver = None
        # Best-effort delete the runtime file (it's stale; another
        # BE will spawn fresh if it queries for this pair).
        with contextlib.suppress(OSError):
            state.runtime_file.unlink()
        logger.info(
            "neo4j (project=%s commit=%s pid=%d) shut down",
            project_hash,
            commit_sha,
            pid,
        )
        return True


class _ExternalProcessHandle:
    """Minimal ``subprocess.Popen``-shaped wrapper for a process
    we discovered via PID (rather than spawned ourselves).

    The original BE started the process; we have the PID + a
    SIGTERM/SIGKILL-able process group, but no Popen handle for
    awaiting. The :class:`Neo4jRuntime` shutdown path only needs
    ``.pid`` and ``.send_signal``/``.terminate``; the ``wait`` path
    is gated on a duck-typed check.

    ``returncode`` is exposed so callers doing the standard
    ``proc.returncode is None`` liveness check (e.g.
    ``start_for_commit`` when re-attaching to a cached state) work
    against both real ``asyncio.subprocess.Process`` and this
    handle. We check the process group via ``os.kill(pid, 0)`` —
    which raises when the process is gone, letting us return an
    exit-code marker instead of ``None``.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid

    @property
    def returncode(self) -> int | None:
        """``None`` while the process is alive, ``-1`` once it's gone.

        Poll-only: we don't have a wait channel, so a caller that
        needs to KNOW the true exit code has to use a different path.
        Everyone using this for liveness (``.returncode is None``)
        gets the right answer either way.
        """
        try:
            os.kill(self.pid, 0)
            return None
        except (ProcessLookupError, PermissionError, OSError):
            return -1

    def send_signal(self, sig: int) -> None:
        """Send a signal to the process group (Neo4j's children)."""
        os.killpg(self.pid, sig)

    def terminate(self) -> None:
        """Alias for :meth:`send_signal` with SIGTERM."""
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        """Alias for :meth:`send_signal` with SIGKILL."""
        self.send_signal(signal.SIGKILL)


# ── Helpers (module-level; pure) ────────────────────────────────────────


def _pair_slug(project_hash: str, commit_sha: str) -> str:
    """Filesystem-friendly slug for one ``(project, commit)`` pair.

    Uses ``-`` as the separator (the project_hash and commit_sha
    are both hex, so there's no ambiguity). Both the project_hash
    and commit_sha are 12+ hex chars; the slug fits in any
    filesystem's path-length limits.
    """
    return f"{project_hash}-{commit_sha}"


def _pick_free_port(host: str) -> int:
    """Bind a socket to port 0; read the assigned port; close.

    Mirrors the WebSocket transport's ``port=0`` auto-bind pattern.
    The brief socket-open window is a TOCTOU window but it's
    negligible — the next caller has to win the race between
    ``getsockname`` and ``close``, which is microseconds.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Cheap TCP connect probe."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _bolt_port(bolt_uri: str) -> int:
    """Parse ``bolt://host:port`` → port. Defensive against weird URIs."""
    try:
        return int(bolt_uri.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


# ── Discovery ──────────────────────────────────────────────────────────


_DISCOVERY_FILENAME = "neo4j.runtime.json"


class Neo4jDiscovery:
    """Read/write ``<data_dir>/neo4j.runtime.json``.

    Persists the running sidecar's endpoints so a second BE
    process (different project, same data_dir) can attach to the
    same sidecar via a PID + bolt-port probe instead of spawning
    a duplicate.

    Constructed cheaply per :class:`Neo4jRuntime` call; the file
    I/O is synchronous since the payload is small and writes are
    rare (two per sidecar lifetime: at start and at shutdown).

    Why a separate discovery file (vs extending the per-project
    ``backend.lock``):

    * ``backend.lock`` is per-project; this file is cross-project
      (one sidecar serves every open project window). Same shape
      as the global ``ember.db`` vs per-project ``state.db`` split.
    * Adding bolt/HTTP URIs to ``LockfilePayload`` would force
      every out-of-tree reader (VSCode extension, JB plugin) to
      learn new optional fields. The Neo4j discovery is internal
      to the BE family only.
    """

    def __init__(self, data_dir: str | Path):
        self._dir = Path(str(data_dir)).expanduser()
        self._path = self._dir / _DISCOVERY_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Neo4jEndpoints | None:
        """Return the parsed endpoints, or None if absent/corrupt.

        Corrupt files log a warning and return None — the caller
        treats that as "no sidecar to attach to" and spawns a
        fresh one. Same graceful-degradation pattern as
        :meth:`Lockfile.read`.
        """
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "neo4j discovery file at %s is unreadable (%s); ignoring",
                self._path,
                exc,
            )
            return None
        if not isinstance(data, dict):
            return None
        try:
            return Neo4jEndpoints(
                bolt_uri=str(data["bolt_uri"]),
                http_uri=str(data["http_uri"]),
                pid=int(data["pid"]),
                user=str(data["user"]),
                password_file=Path(str(data["password_file"])),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "neo4j discovery file at %s failed schema check (%s); ignoring",
                self._path,
                exc,
            )
            return None

    def save(self, endpoints: Neo4jEndpoints) -> None:
        """Atomically write ``endpoints`` to ``<data_dir>/neo4j.runtime.json``.

        Per-writer temp filename (PID + uuid suffix) so two BEs that
        race the first start don't clobber each other's tmp file
        and trip ``os.replace`` with ``FileNotFoundError`` — same
        pattern as :meth:`ManifestStore.save`.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = endpoints.to_discovery_dict()
        tmp = self._path.with_suffix(self._path.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)
        logger.debug("wrote neo4j discovery: %s", payload)

    def clear(self) -> None:
        """Remove the discovery file. Idempotent.

        Best-effort — a missing file is success. Used at sidecar
        shutdown so the next BE doesn't see a stale pointer.
        """
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.debug("neo4j discovery remove failed: %s", exc)
            return
        logger.debug("removed neo4j discovery: %s", self._path)
