"""Where a delivered service event goes in this backend (``extensions/service_events.py``).

The inbox is the core's; this module is what it needs from a running
backend: the producer's main session (the pool's default runtime), and
the extension's process, asked over its host socket.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from pydantic_core import to_jsonable_python

from ember_code.backend.activity import ActivityState
from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.wire import Outcome
from ember_code.extensions.service_events import ServiceEvent

logger = logging.getLogger(__name__)


class MainSession:
    """The producer's main session: the pool's default runtime."""

    def __init__(self, orchestrator: Any) -> None:
        self._orchestrator = orchestrator

    def _runtime(self) -> Any:
        return self._orchestrator.pool.default

    @property
    def session_id(self) -> str:
        return str(self._runtime().current_session_id())

    def busy(self) -> bool:
        """Working, or waiting on an approval: anything but idle."""
        probe = getattr(self._runtime().backend, "activity", None)
        if probe is None:
            return False
        return probe().state is not ActivityState.IDLE

    async def post(self, *, extension_id: str, text: str, queued: bool) -> None:
        await self._orchestrator.post_from_extension(
            session_id=self.session_id,
            extension_id=extension_id,
            text=text,
            media=None,
            queued=queued,
        )


class ExtensionEventHandler:
    """Asks the extension an event is for what the agent should see."""

    #: Longer than a hook's — the extension may look something up first —
    #: and well under the gateway's 30 s for the whole delivery: an event
    #: it does not answer in time goes to the run as it came.
    TIMEOUT_S: ClassVar[float] = 10.0
    #: How long a process that is not connected yet is waited for. One
    #: not installed or not running here is not waited for at all.
    CONNECT_TIMEOUT_S: ClassVar[float] = 2.0
    #: The frame's event name, the SDK's ``SERVICE_EVENT``.
    EVENT: ClassVar[str] = "ServiceEvent"

    def __init__(self, hooks: ExtensionHooks | None = None) -> None:
        self._hooks = hooks

    async def __call__(self, event: ServiceEvent) -> str | None:
        hooks = self._hooks if self._hooks is not None else ExtensionHooks.shared()
        answer = await hooks.ask_one(
            event.extension_id,
            self.EVENT,
            to_jsonable_python(event.model_dump(mode="json")),
            timeout=self.TIMEOUT_S,
            connect_timeout=self.CONNECT_TIMEOUT_S,
        )
        if answer.outcome is Outcome.MODIFY and isinstance(answer.value, dict):
            prompt = answer.value.get("prompt")
            return str(prompt) if prompt else None
        if answer.outcome is Outcome.FAILED:
            logger.info(
                "%s's handler of %s failed: %s", event.extension_id, event.type, answer.reason
            )
        return None


__all__ = ["ExtensionEventHandler", "MainSession"]
