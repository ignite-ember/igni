"""Captured (non-interactive) shell runner for the ``$``-prefix RPC.

Extracted from :mod:`ember_code.backend.rpc_router` where the
subprocess boilerplate + timeout + output-cap constants lived inline
in the ``_run_shell`` handler. Turning it into a class gives the
timeout and output-cap named-field seams for tests, and keeps
``rpc_router`` focused on dispatch.

Runs through the same :class:`ProcessRegistry` as the agent's
``run_shell_command``. It used to spawn a bare
``asyncio.create_subprocess_shell`` and hold it privately, which had
two consequences a user could see:

* A ``$`` command was invisible to the watcher. ``$ npm run dev``
  started a real child of the BE that nothing in the UI listed, showed
  output for, or could stop — the panel whose whole subject is
  "background processes & live logs" reported "no processes" while two
  of them were running.
* It was killed at the timeout. Anything slower than two minutes died,
  rather than carrying on in the background the way the same command
  does when the agent runs it.

Both are now the agent path's behaviour, because it is the same path:
silent ``add`` on spawn, and ``announce_start`` only if the command
outlives the foreground window — so a quick ``$ ls`` does not leave a
row behind, and a long one appears in the watcher instead of dying.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.backend.schemas_rpc import RunShellResult
from ember_code.core.tools.managed_process import ManagedProcess
from ember_code.core.tools.process_supervisor_locator import supervisors

if TYPE_CHECKING:
    from ember_code.core.tools.process_supervisor import ProcessSupervisor

logger = logging.getLogger(__name__)

#: How long to let the reader task drain trailing output after the
#: process exits. Without it a fast command returns before its own last
#: line has been read off the pipe.
_DRAIN_SECONDS = 0.15


class CapturedShellRunner:
    """Run a shell command with the BE's ``$``-prefix semantics.

    Parity with the TUI's inline shell for the common cases — no
    stdin, output captured (stderr merged into stdout), trailing bytes
    truncated so a runaway command can't drown the wire in text — and
    parity with the agent's shell tool for everything about lifecycle.
    """

    def __init__(
        self,
        project_dir: Path,
        *,
        timeout_seconds: int = 120,
        max_output_bytes: int = 100_000,
        supervisor: ProcessSupervisor | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        # Defaults to the process-wide supervisor, which is the one the
        # watcher reads. Injected in tests.
        self._supervisor = supervisor or supervisors.default()

    async def run(self, command: str) -> RunShellResult:
        command = command.strip()
        if not command:
            return RunShellResult(output="", exit_code=0)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self._project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Own process group, so stopping it from the watcher
                # kills the whole tree rather than orphaning children.
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the user, not raised
            return RunShellResult(output=f"could not start: {exc}", exit_code=-1)

        mp = ManagedProcess(proc, command, self._supervisor)
        mp.start_reader()
        pid = self._supervisor.registry.add(mp)  # silent; announce only if it survives

        try:
            await asyncio.wait_for(proc.wait(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            # Backgrounded, not killed. The row appears in the watcher
            # from here, and the completion notice fires when it ends.
            mp.was_backgrounded = True
            self._supervisor.registry.announce_start(mp)
            logger.info(
                "shell: %s outlived %ds; backgrounded as %d", command, self._timeout_seconds, pid
            )
            tail = mp.read(tail=200)
            note = (
                f"(still running after {self._timeout_seconds}s — backgrounded as PID {pid}; "
                f"see the Watcher to follow or stop it)"
            )
            return RunShellResult(
                output=f"{note}\n{tail}"[-self._max_output_bytes :],
                exit_code=0,
            )

        await asyncio.sleep(_DRAIN_SECONDS)
        output = mp.read(tail=2000)
        exit_code = mp.returncode() or 0
        # Kept, not deleted, and written down. A command that just
        # finished — especially one that just failed — is the thing
        # someone opens the watcher to read, and dropping the row at
        # that exact moment is what made history impossible. The
        # registry's eviction TTL bounds the in-memory copy; the
        # persisted one outlives the BE.
        self._supervisor.registry.record_finished(mp)
        return RunShellResult(
            output=output[-self._max_output_bytes :],
            exit_code=exit_code,
        )
