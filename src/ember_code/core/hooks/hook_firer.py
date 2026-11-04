"""Uniform wire path for firing a typed hook payload.

Replaces the private ``ToolEventHook._fire`` shim that used to
live inside ``tool_hook.py``. Every stage / invoker / suffixer
in the refactored pipeline takes a :class:`HookFirer` and calls
:meth:`HookFirer.fire` — no more scattered ``try/except`` blocks
that inject ``session_id`` and swallow exceptions.

There is deliberately NO "does anything subscribe?" pre-check
here. That gate lives inside :meth:`HookExecutor.execute`, which
resolves the match set and returns early — see
:meth:`HookFirer.fire` for why a caller-side one would be wrong.
The cost of firing into the void is therefore one typed payload
construction per event, which is the price of the pipeline
reading the live hooks dict rather than a flag cached at
construction time (a plugin hot-reloading ``HookExecutor.hooks``
is picked up immediately).
"""

from __future__ import annotations

import logging

from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.schemas import HookResult
from ember_code.core.hooks.tool_events import ToolHookEventPayload

logger = logging.getLogger(__name__)


class HookFirer:
    """Shared wire path — ``fire(event, target, payload)``.

    * Injects ``session_id`` on the payload so callers construct
      typed payloads WITHOUT worrying about the field.
    * Swallows any exception raised by a hook (with a debug log)
      and returns a non-blocking :class:`HookResult` — matches the
      pre-refactor tolerance so a single flaky hook can't wedge
      the tool pipeline.
    """

    def __init__(self, executor: HookExecutor, session_id: str) -> None:
        self._executor = executor
        self._session_id = session_id

    async def fire(
        self,
        event: HookEvent,
        target: str,
        payload: ToolHookEventPayload,
    ) -> HookResult:
        """Fire ``event`` for ``target`` with ``payload``.

        Returns the merged :class:`HookResult` from every matching
        hook — callers inspect ``permission_decision`` / ``message``
        / ``should_continue`` to drive the pipeline.

        The "does anything subscribe?" gate lives inside
        :meth:`HookExecutor.execute` (checks the resolved match set),
        not here — a caller-side pre-check on ``self.hooks`` would
        skip test recorders that observe every event through an
        overridden ``execute()``.
        """
        # Inject session_id at the seam so payload constructors
        # don't have to plumb it through.
        payload.session_id = self._session_id
        try:
            return await self._executor.execute(
                event=event.value,
                payload=payload.to_wire_dict(),
                target=target,
            )
        except Exception as exc:
            logger.debug("Hook %s/%s failed: %s", event.value, target, exc)
            return HookResult(should_continue=True)
