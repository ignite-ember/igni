"""Captured (non-interactive) shell runner for the ``$``-prefix RPC.

Extracted from :mod:`ember_code.backend.rpc_router` where the
subprocess boilerplate + timeout + output-cap constants lived inline
in the ``_run_shell`` handler. Turning it into a class gives the
timeout and output-cap named-field seams for tests, and keeps
``rpc_router`` focused on dispatch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ember_code.backend.schemas_rpc import RunShellResult


class CapturedShellRunner:
    """Run a shell command with the BE's ``$``-prefix semantics.

    Parity with the TUI's inline shell for the common cases — no
    stdin, output captured (stderr merged into stdout), hard timeout,
    trailing bytes truncated so a runaway command can't drown the
    wire in text.
    """

    def __init__(
        self,
        project_dir: Path,
        *,
        timeout_seconds: int = 120,
        max_output_bytes: int = 100_000,
    ) -> None:
        self._project_dir = project_dir
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    async def run(self, command: str) -> RunShellResult:
        command = command.strip()
        if not command:
            return RunShellResult(output="", exit_code=0)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self._project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            return RunShellResult(
                output=f"(timed out after {self._timeout_seconds}s)", exit_code=-1
            )
        return RunShellResult(
            output=out.decode(errors="replace")[-self._max_output_bytes :],
            exit_code=proc.returncode or 0,
        )
