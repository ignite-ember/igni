"""BackendProcess — spawns and manages the BE subprocess."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import signal
import sys
import uuid
from pathlib import Path

from ember_code.frontend.tui.backend_client import BackendClient

logger = logging.getLogger(__name__)


# Track every live BackendProcess so the atexit hook can guarantee
# cleanup even when the TUI exits abnormally (uncaught exception,
# Ctrl+C, terminal hangup). Without this, the BE subprocess is orphaned
# and can keep running indefinitely — we caught one such zombie that had
# burned 117 hours of CPU over 11 days.
_LIVE_PROCESSES: set[BackendProcess] = set()


def _kill_all_live() -> None:
    """atexit hook: send SIGKILL to any BE subprocess whose parent FE is
    on its way out. SIGTERM was tried during normal shutdown; this is
    the last-resort sweep."""
    for bp in list(_LIVE_PROCESSES):
        with contextlib.suppress(Exception):
            bp._kill_now()


atexit.register(_kill_all_live)


def _install_signal_cleanup() -> None:
    """Install SIGINT/SIGTERM/SIGHUP handlers that run the atexit sweep
    before re-raising the default behaviour. Registered once at import.
    """
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            old_handler = signal.getsignal(sig)

            def _handler(signum, frame, _old=old_handler, _sig=sig):
                _kill_all_live()
                if callable(_old) and _old not in (signal.SIG_IGN, signal.SIG_DFL):
                    _old(signum, frame)
                else:
                    # Restore default and re-raise so the process actually exits.
                    signal.signal(_sig, signal.SIG_DFL)
                    os.kill(os.getpid(), _sig)

            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # ValueError: not in main thread. OSError: signal not supported.
            pass


_install_signal_cleanup()


class BackendProcess:
    """Spawns BackendServer as a subprocess and connects via Unix socket."""

    def __init__(
        self,
        project_dir: Path | None = None,
        resume_session_id: str | None = None,
        additional_dirs: list[Path] | None = None,
        settings: object | None = None,
        debug: bool = False,
    ):
        self._project_dir = project_dir or Path.cwd()
        self._resume_session_id = resume_session_id
        self._additional_dirs = additional_dirs
        self._settings = settings
        self._debug = debug
        self._socket_path = f"/tmp/ember-code/{uuid.uuid4().hex[:12]}.sock"
        # WS attach point for mirrored GUI views; set from the BE
        # ready line in ``start()``.
        self.ws_url = ""
        self._process: asyncio.subprocess.Process | None = None
        self._client: BackendClient | None = None

    async def start(self) -> BackendClient:
        """Spawn BE subprocess, wait for READY, return connected client."""
        # Ensure socket directory exists
        Path(self._socket_path).parent.mkdir(parents=True, exist_ok=True)

        # Build command
        cmd = [
            sys.executable,
            "-m",
            "ember_code.backend",
            "--socket",
            self._socket_path,
            # Mirroring: also listen on a loopback WS port so GUI
            # views (browser tabs, IDE webviews) can attach to this
            # TUI session and render the same events live. Port 0 =
            # auto-assign; the bound port comes back in the ready
            # line and is exposed via ``self.ws_url``.
            "--ws-port",
            "0",
            "--project-dir",
            str(self._project_dir),
        ]
        if self._resume_session_id:
            cmd.extend(["--resume-session", self._resume_session_id])
        if self._additional_dirs:
            for d in self._additional_dirs:
                cmd.extend(["--additional-dirs", str(d)])
        if self._debug:
            cmd.append("--debug")

        logger.info("Spawning BE: %s", " ".join(cmd))

        # ``start_new_session=True`` puts the BE in its own process group.
        # That lets ``stop()`` kill BE + every child it spawned (shells,
        # subprocesses, anything Agno forks) atomically via ``killpg``.
        # Without this, killing the BE leaves its grandchildren orphaned —
        # which is how we ended up with an 11-day-old runaway process.
        # ``EMBER_PARENT_PID`` lets the BE detect parent death and self-
        # terminate even if signals don't reach it (e.g. crash on macOS).
        env = {**os.environ, "EMBER_PARENT_PID": str(os.getpid())}
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
        _LIVE_PROCESSES.add(self)

        # Wait for JSON ready signal on stdout (skip non-JSON lines like warnings)
        import json

        try:
            deadline = asyncio.get_event_loop().time() + 60.0
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                line = await asyncio.wait_for(self._process.stdout.readline(), timeout=remaining)
                text = line.decode().strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                    if data.get("status") == "ready":
                        logger.info("BE ready: %s", text)
                        self.ws_url = data.get("ws_url", "")
                        break
                except json.JSONDecodeError:
                    # Skip non-JSON lines (library warnings, model load reports)
                    logger.debug("BE stdout (non-JSON): %s", text[:200])
                    continue
        except asyncio.TimeoutError:
            self._process.kill()
            stderr = await self._process.stderr.read()
            raise RuntimeError(
                f"BE failed to start within 60s. stderr: {stderr.decode()[:500]}"
            ) from None

        # Connect client
        self._client = BackendClient(self._socket_path)
        await self._client.connect()

        # Cache initial state
        if self._settings:
            self._client._cached_settings = self._settings
        await self._client.refresh_cache()

        return self._client

    async def stop(self) -> None:
        """Send shutdown, wait for the process group to exit, then sweep.

        Uses ``killpg`` so any subprocess the BE spawned (shells the
        agents ran, etc.) dies with the BE. Without this, those children
        are orphaned and reparented to launchd/init, where they can run
        indefinitely.
        """
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.shutdown()

        if self._process:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("BE did not exit gracefully, killing process group")
                self._kill_now()

        # Cleanup socket file
        socket_path = Path(self._socket_path)
        if socket_path.exists():
            with contextlib.suppress(Exception):
                socket_path.unlink()

        _LIVE_PROCESSES.discard(self)

    def _kill_now(self) -> None:
        """Synchronously SIGKILL the entire BE process group.

        Called from atexit / signal handlers (where async is unsafe) and
        as the timeout escape hatch in ``stop()``.
        """
        proc = self._process
        if proc is None or proc.returncode is not None:
            return
        pid = proc.pid
        try:
            # ``start_new_session=True`` made the BE its own pgid leader.
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(Exception):
                proc.kill()

    def is_alive(self) -> bool:
        """Check if the BE process is running."""
        return self._process is not None and self._process.returncode is None

    @property
    def client(self) -> BackendClient | None:
        return self._client
