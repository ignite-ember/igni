"""``/loop`` continuation pump.

Wraps the five ``/loop`` state-machine RPCs (pop / cancel / pause /
resume / status) as :class:`LoopController` methods on one
:class:`Session`. The old free-function surface that took a
``BackendServer`` first-arg (Rule 6 offender) is gone.

The sibling scheduler surface — ``execute_scheduled_task``,
``cancel_scheduled_task``, ``get_scheduled_tasks``,
``start_scheduler`` — lives in :class:`SchedulerController`
(``server_scheduler.py``). This controller composes it as
:attr:`scheduler` so ``BackendServer.loop.scheduler.*`` is the
single access path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ember_code.backend.schemas_loop import LoopStatusSnapshot
from ember_code.backend.server_scheduler import SchedulerController

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.session import Session
    from ember_code.core.session.loop_ops import LoopAdvance

logger = logging.getLogger(__name__)


# Backwards-compat re-export: any external tooling still spelling the
# pre-refactor ``LoopStatus`` name resolves to the same Pydantic
# model as :class:`LoopStatusSnapshot`. Deletable once the refactor
# has aged one release cycle.
LoopStatus = LoopStatusSnapshot


class LoopController:
    """``/loop`` continuation pump for one :class:`Session`.

    Composes a :class:`SchedulerController` on :attr:`scheduler` —
    the two lifecycles are independent (pump ticks on every user
    message; scheduler runs a background poll loop) but they share
    the same session + settings.
    """

    def __init__(
        self,
        session: Session,
        settings: Settings | None,
    ) -> None:
        self._session = session
        self._settings = settings
        # Composition: the scheduler is materialised eagerly but
        # its methods never touch ``settings`` until :meth:`start`
        # is called, so pump-only fixtures that pass ``settings=None``
        # keep working (see ``tests/test_loop.py::_FakeBackend``).
        self._scheduler = SchedulerController(session=session, settings=settings)

    @property
    def scheduler(self) -> SchedulerController:
        """Composed scheduler surface. Reached from
        :class:`BackendServer` as ``self.loop.scheduler.*``."""
        return self._scheduler

    # ── Loop pump ─────────────────────────────────────────────

    async def pop_pending_iteration(self) -> LoopAdvance | None:
        """Pop the next ``/loop`` iteration descriptor."""
        return await self._session.advance_loop()

    async def cancel_pending(self) -> bool:
        """Clear ``/loop`` state. Returns whether anything was cancelled.

        Paused loops (loaded from disk on startup, not yet resumed)
        are intentionally NOT cancelled here.
        """
        if self._session.loop_paused:
            return False
        return await self._session.cancel_loop()

    async def pause(self) -> bool:
        """Pause the active loop without advancing the counter."""
        return await self._session.pause_loop()

    async def resume(self) -> str:
        """Flip the loop from paused to pumping and return the prompt."""
        prompt = await self._session.resume_loop()
        return prompt or ""

    async def status(self) -> LoopStatusSnapshot:
        """Snapshot for the ``/loop`` panel header.

        Delegates assembly to :meth:`LoopStatusSnapshot.from_session`
        so the announced-total lookup lives on the schema type that
        names the shape (data + behavior together).
        """
        return await LoopStatusSnapshot.from_session(self._session)


__all__ = ["LoopController", "LoopStatusSnapshot", "LoopStatus"]
