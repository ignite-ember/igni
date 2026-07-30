"""Shutdown pipeline — polymorphic cleanup steps for
:class:`LifecycleController`.

Extracted from :mod:`server_lifecycle` where six shutdown
closures used to line ``LifecycleController.shutdown`` alongside
a small ``ShutdownStepRunner`` accumulator. The step logic now
lives as a :class:`ShutdownStep` hierarchy driven by
:class:`ShutdownPipeline`; the lifecycle controller just builds
the pipeline once and calls ``await self._shutdown_pipeline.run()``.

Design rules:

* Each :class:`ShutdownStep` subclass implements
  :meth:`_run(session, result)`. Success recording (mark bits on
  :class:`ShutdownResult`) happens inside ``_run`` — the base
  class only owns the try/except that catches step failures and
  routes them to :meth:`ShutdownResult.record_error`.
* The pipeline is data on the controller, not a closure blob in
  a method body. Best-effort teardown is preserved (an exception
  in one step is recorded and the runner moves on).
* Steps stay stateless w.r.t. the session — the pipeline threads
  it in on :meth:`ShutdownPipeline.run`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, ClassVar

from ember_code.backend.schemas_lifecycle import (
    SessionEndPayload,
    ShutdownResult,
)
from ember_code.core.hooks.events import HookEvent
from ember_code.core.tools.shell import EmberShellTools

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class ShutdownStep(ABC):
    """Abstract base for one ordered cleanup step.

    Subclasses implement :meth:`_run` (async by default —
    synchronous steps can just ``return`` without awaiting).
    Exceptions raised inside ``_run`` are caught by
    :meth:`execute` and routed to
    :meth:`ShutdownResult.record_error` under the subclass's
    ``label``. Success is recorded by the subclass itself — the
    base class deliberately does not assume every step maps to a
    single ``mark_*`` bit (``ShellCleanupStep`` reports a count).
    """

    label: ClassVar[str]

    async def execute(self, session: Session, result: ShutdownResult) -> None:
        """Run the step, catching + recording exceptions.

        Wrapping the try/except here (rather than in every
        subclass) is the whole point of the polymorphism —
        subclasses stay focused on the happy path.
        """
        try:
            await self._run(session, result)
        except Exception as exc:
            result.record_error(self.label, exc)
            logger.debug("shutdown step %s failed: %s", self.label, exc)

    @abstractmethod
    async def _run(self, session: Session, result: ShutdownResult) -> None:
        """Actual step body — mark the corresponding bit on ``result``."""


class SessionEndHookStep(ShutdownStep):
    """Fire the ``SESSION_END`` hook so plugins can flush state."""

    label: ClassVar[str] = "session_end_hook"

    async def _run(self, session: Session, result: ShutdownResult) -> None:
        payload = SessionEndPayload(session_id=session.session_id)
        await session.hook_executor.execute(
            event=HookEvent.SESSION_END.value,
            payload=payload.model_dump(),
        )
        result.mark_hook_fired()


class PoolCleanupStep(ShutdownStep):
    """Clean up ephemeral agents if the auto-cleanup gate is on."""

    label: ClassVar[str] = "pool_cleanup"

    async def _run(self, session: Session, result: ShutdownResult) -> None:
        session.pool.cleanup_ephemeral_if_auto(session.settings)
        result.mark_pool_cleaned()


class McpDisconnectStep(ShutdownStep):
    """Disconnect every attached MCP client."""

    label: ClassVar[str] = "mcp_disconnect"

    async def _run(self, session: Session, result: ShutdownResult) -> None:
        await session.mcp_manager.disconnect_all()
        result.mark_mcp_disconnected()


class SchedulerStopStep(ShutdownStep):
    """Stop the scheduler background runner (when one is wired up)."""

    label: ClassVar[str] = "scheduler_stop"

    def __init__(self, scheduler_stop: Callable[[], Awaitable[None]]) -> None:
        self._scheduler_stop = scheduler_stop

    async def _run(self, session: Session, result: ShutdownResult) -> None:
        await self._scheduler_stop()
        result.mark_scheduler_stopped()


class ShellCleanupStep(ShutdownStep):
    """Kill any orphan background processes left by shell tools."""

    label: ClassVar[str] = "shell_cleanup"

    async def _run(self, session: Session, result: ShutdownResult) -> None:
        killed = EmberShellTools.cleanup()
        result.record_shell_kills(killed)
        if killed:
            logger.info("Shutdown: killed %d background process(es)", killed)


class ShutdownPipeline:
    """Ordered driver for a fixed set of :class:`ShutdownStep`.

    Built once in :meth:`LifecycleController.__init__` so the
    controller's :meth:`shutdown` is a two-line delegate.
    ``scheduler_stop`` may be ``None`` (production always wires
    it; some ``__new__``-bypass tests do not) — in that case the
    scheduler step is omitted from the pipeline entirely rather
    than being conditionally skipped at run time.
    """

    def __init__(
        self,
        session: Session,
        scheduler_stop: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._session = session
        steps: list[ShutdownStep] = [
            SessionEndHookStep(),
            PoolCleanupStep(),
            McpDisconnectStep(),
        ]
        if scheduler_stop is not None:
            steps.append(SchedulerStopStep(scheduler_stop))
        steps.append(ShellCleanupStep())
        self._steps: list[ShutdownStep] = steps

    async def run(self) -> ShutdownResult:
        """Iterate every step, accumulating outcomes on one result."""
        result = ShutdownResult()
        for step in self._steps:
            await step.execute(self._session, result)
        return result


__all__ = [
    "McpDisconnectStep",
    "PoolCleanupStep",
    "SchedulerStopStep",
    "SessionEndHookStep",
    "ShellCleanupStep",
    "ShutdownPipeline",
    "ShutdownStep",
]
