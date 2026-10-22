"""``command`` hook handler — runs a shell command with the
payload on stdin and interprets exit code + stdout JSON.

Exit-code contract (CC-compatible):

* ``0`` — success. Stdout may be a JSON envelope
  (``continue`` / ``systemMessage`` / ``permissionDecision``).
* ``2`` — block. Stderr (or the ``systemMessage`` from stdout
  JSON) becomes the block reason. For ``async_rewake`` hooks,
  the message is queued for the next turn instead of blocking.
* Anything else — non-blocking soft error, logged at WARNING.
  Hooks fail OPEN: a hook that cannot run (missing script, bad
  interpreter, timeout) does not block the tool call, so the log
  is the only evidence it did not run. Do not downgrade it.
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
            return self._handle_soft_error(hook, proc.returncode, stderr)
        except asyncio.TimeoutError:
            # Also a fail-open, also invisible before this line: the
            # message below is dropped by every caller that isn't
            # blocking on the result.
            logger.warning(
                "Hook %r timed out after %.1fs — continuing (hooks fail open)",
                hook.command,
                hook.timeout / 1000,
            )
            return self._non_blocking("Hook timed out")
        except Exception as exc:
            logger.warning(
                "Hook %r could not be run (%s) — continuing (hooks fail open)",
                hook.command,
                exc,
            )
            return self._non_blocking()

    def _handle_soft_error(
        self,
        hook: HookDefinition,
        returncode: int | None,
        stderr: bytes,
    ) -> HookResult:
        """Any exit code that is neither 0 nor 2 — the hook did not
        run to the contract, so we continue (fail open).

        The log line is the whole point. Failing open silently is how
        a PreToolUse hook whose script is simply not there reads as
        "hook passed": ``bash -c`` exits 127, this branch returns
        ``should_continue=True``, and the permission pipeline only
        ever surfaces a hook's message when it *blocks*. Measured
        before this existed: a missing ``validate-bash.sh`` produced
        ``should_continue=True`` and not one log record at any level,
        from any logger.

        The stderr stays in the log rather than being handed back as
        the hook's message: it is the hook author's diagnostic, and
        routing subprocess stderr into the model's context would
        publish whatever a broken script happened to print.
        """
        detail = stderr.decode(errors="replace").strip()
        logger.warning(
            "Hook %r exited %s — continuing (hooks fail open). stderr: %s",
            hook.command,
            returncode,
            detail[:500] if detail else "<empty>",
        )
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
