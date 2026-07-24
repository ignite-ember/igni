"""``command`` hook handler — runs a shell command with the
payload on stdin and interprets exit code + stdout JSON.

Exit-code contract (CC-compatible):

* ``0`` — success. Stdout may be a JSON envelope
  (``continue`` / ``systemMessage`` / ``permissionDecision``).
* ``2`` — block. Stderr (or the ``systemMessage`` from stdout
  JSON) becomes the block reason. For ``async_rewake`` hooks,
  the message is queued for the next turn instead of blocking.
* Anything else — non-blocking soft error.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import ClassVar

from ember_code.core.hooks.envelope import HookEnvelope
from ember_code.core.hooks.handlers.base import HookHandler
from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookPayload,
    HookResult,
    HookType,
)

logger = logging.getLogger(__name__)


class CommandHookHandler(HookHandler):
    """Subprocess-based hook handler.

    ``rewake_callback`` is a closure handed in by the executor
    (originally by :class:`Session`) that knows how to buffer a
    system reminder for the next ``handle_message`` turn — used
    only by ``async_rewake`` hooks that exit with code 2.
    Constructor-injected rather than reached in from the
    executor's private state (was the old design's smell).
    """

    handles: ClassVar[HookType] = "command"

    def __init__(self, rewake_callback: Callable[[str], None] | None = None):
        self._rewake_callback = rewake_callback

    async def run(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayload,
    ) -> HookResult:
        try:
            timeout_secs = hook.timeout / 1000
            payload_json = json.dumps(payload.to_wire_dict())
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                hook.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=payload_json.encode()),
                timeout=timeout_secs,
            )
            if proc.returncode == 2:
                return self._handle_block(hook, stdout, stderr)
            if proc.returncode == 0:
                return self._handle_success(stdout)
            return self._non_blocking()
        except asyncio.TimeoutError:
            return self._non_blocking("Hook timed out")
        except Exception as exc:
            logger.debug("Command hook failed: %s", exc)
            return self._non_blocking()

    def _handle_block(self, hook: HookDefinition, stdout: bytes, stderr: bytes) -> HookResult:
        """Assemble the block message for an exit-2 hook, then
        either buffer it as an async rewake reminder or return a
        blocking result.

        Stderr-fallback (``"Blocked by hook"``) is command-hook-
        specific — it does NOT live in :class:`HookEnvelope`, which
        only knows the stdout JSON shape.
        """
        try:
            data = json.loads(stdout.decode())
            envelope = HookEnvelope.from_raw(data)
            msg = envelope.system_message if envelope else ""
            if not msg:
                msg = "Blocked by hook"
        except (json.JSONDecodeError, UnicodeDecodeError):
            msg = stderr.decode().strip() or "Blocked by hook"
        if hook.async_rewake and self._rewake_callback is not None:
            try:
                self._rewake_callback(msg)
            except Exception as exc:
                logger.debug("rewake_callback raised: %s", exc)
            return self._non_blocking()
        return HookResult(should_continue=False, message=msg)

    def _handle_success(self, stdout: bytes) -> HookResult:
        """Parse a successful hook's stdout JSON into a
        :class:`HookResult` via :class:`HookEnvelope`. Malformed
        stdout is non-blocking.
        """
        try:
            data = json.loads(stdout.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._non_blocking()
        envelope = HookEnvelope.from_raw(data)
        if envelope is None:
            return self._non_blocking()
        return envelope.to_result()
