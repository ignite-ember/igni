"""MCP transport layer — stdio and HTTP transport handling."""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class StdioTransport:
    """Handles stdio-based MCP communication."""

    def __init__(
        self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None
    ):
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._process = None

    async def start(self):
        """Start the subprocess."""

        full_env = {**os.environ, **self.env}

        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )

    async def stop(self):
        """Stop the subprocess."""
        if self._process:
            self._process.terminate()
            try:
                await self._process.wait()
            except Exception as exc:
                logger.debug("Failed to wait for process termination, killing: %s", exc)
                self._process.kill()
            self._process = None

    @property
    def stdin(self):
        return self._process.stdin if self._process else None

    @property
    def stdout(self):
        return self._process.stdout if self._process else None
