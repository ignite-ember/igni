"""``http`` hook handler — POSTs the payload to a URL.

Non-200 responses and network errors degrade to non-blocking:
a flaky webhook shouldn't block the agent's tool call. For
firm gating, use a ``command`` hook with exit 2.
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

import httpx

from ember_code.core.hooks.envelope import HookEnvelope
from ember_code.core.hooks.handlers.base import HookHandler
from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookPayload,
    HookResult,
    HookType,
)

logger = logging.getLogger(__name__)


class HttpHookHandler(HookHandler):
    """POST-the-payload webhook handler."""

    handles: ClassVar[HookType] = "http"

    async def run(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayload,
    ) -> HookResult:
        try:
            timeout_secs = hook.timeout / 1000
            # Expand env vars in headers so users can put
            # ``$WEBHOOK_TOKEN`` in settings.json without pre-
            # rendering.
            headers = {k: os.path.expandvars(v) for k, v in hook.headers.items()}
            async with httpx.AsyncClient(timeout=timeout_secs) as client:
                response = await client.post(
                    hook.url,
                    json=payload.to_wire_dict(),
                    headers=headers,
                )
            if response.status_code != 200:
                return self._non_blocking()
            try:
                data = response.json()
            except Exception as exc:
                logger.debug("Failed to parse HTTP hook response: %s", exc)
                return self._non_blocking()
            envelope = HookEnvelope.from_raw(data)
            if envelope is None:
                return self._non_blocking()
            return envelope.to_result()
        except Exception as exc:
            logger.debug("HTTP hook failed: %s", exc)
            return self._non_blocking()
