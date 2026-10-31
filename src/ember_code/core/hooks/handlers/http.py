"""``http`` hook handler — POSTs the payload to a URL.

Non-200 responses and network errors degrade to non-blocking:
a flaky webhook shouldn't block the agent's tool call. For
firm gating, use a ``command`` hook with exit 2.

Under ``air_gapped: true`` a URL outside the customer's network
(:class:`~ember_code.core.network_policy.NetworkPolicy`) is not
sent at all: the hook degrades exactly as an unreachable one does.
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

import httpx

from ember_code.core.activity.http import busy_call
from ember_code.core.hooks.envelope import HookEnvelope
from ember_code.core.hooks.handlers.base import HookHandler
from ember_code.core.hooks.schemas import (
    HookDefinition,
    HookPayload,
    HookResult,
    HookType,
)
from ember_code.core.network_policy import NetworkPolicy

logger = logging.getLogger(__name__)


class HttpHookHandler(HookHandler):
    """POST-the-payload webhook handler."""

    handles: ClassVar[HookType] = "http"

    def __init__(self, network_policy: NetworkPolicy | None = None) -> None:
        #: Which URLs a hook may POST to. Without one, any — an install
        #: that is not air-gapped.
        self._network_policy = network_policy or NetworkPolicy()

    async def run(
        self,
        hook: HookDefinition,
        event: str,
        payload: HookPayload,
    ) -> HookResult:
        if not self._network_policy.allows(hook.url):
            logger.warning(
                "HTTP hook not sent: %s",
                self._network_policy.refusal(hook.url, what="the hook's URL"),
            )
            return self._non_blocking()
        try:
            timeout_secs = hook.timeout / 1000
            # Expand env vars in headers so users can put
            # ``$WEBHOOK_TOKEN`` in settings.json without pre-
            # rendering.
            headers = {k: os.path.expandvars(v) for k, v in hook.headers.items()}
            async with busy_call("hook"), httpx.AsyncClient(timeout=timeout_secs) as client:
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
