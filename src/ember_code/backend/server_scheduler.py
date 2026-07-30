"""Scheduler surface — task execution + background runner lifecycle.

Extracted out of :mod:`ember_code.backend.server_loop`, which had
grown to own both the ``/loop`` continuation pump AND the scheduled-
task RPCs. Splitting the two:

* :class:`LoopController` (in ``server_loop.py``) — ``/loop`` pump.
* :class:`SchedulerController` (this file) — scheduler.

They compose via :attr:`LoopController.scheduler`; ``BackendServer``
delegates every scheduled-task RPC through ``self.loop.scheduler``.

Ownership of the :class:`SchedulerRunner` also moves here: the old
implementation smuggled the runner onto ``session.pool._scheduler_runner``
so a re-created ``LoopController`` could find the same instance. That
private-attr reach-in is gone — the runner now lives on
:attr:`SchedulerController._runner` and its explicit :meth:`stop`
gives shutdown paths a composable teardown seam.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ember_code.backend.schemas_scheduler import (
    TaskCompletedPayload,
    TaskCreatedPayload,
)
from ember_code.core.hooks.events import HookEvent
from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.core.scheduler.runner import SchedulerRunner
from ember_code.core.scheduler.store import TaskStore
from ember_code.core.utils.response import extract_response_text
from ember_code.protocol import messages as msg
from ember_code.protocol.schemas.enums import SchedulerEventType

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.hooks.executor import HookExecutor
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


TaskStartedCallback = Callable[[str, str], None] | None
TaskCompletedCallback = Callable[[str, str, bool], None] | None


class SchedulerController:
    """Scheduled-task RPCs + background runner lifecycle for one :class:`Session`.

    Constructed once per :class:`LoopController` (which composes this
    controller on ``self.scheduler``) so ``BackendServer.loop.scheduler.*``
    is the single access path — no free-function facade.

    :meth:`start` is idempotent (returns the cached runner when
    already running); :meth:`stop` gives the composition root an
    explicit shutdown hook that composes with
    :meth:`BackendServer.shutdown`.
    """

    def __init__(
        self,
        session: Session,
        settings: Settings | None,
        *,
        execute_fn: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._session = session
        # ``settings`` may be ``None`` on ``BackendServer.__new__``-bypass
        # test fixtures that only care about the loop pump. Every
        # method that actually consults settings raises the natural
        # ``AttributeError`` in that state; the pump-only tests never
        # reach those methods.
        self._settings = settings
        # ``execute_fn`` is the runner's per-task callback. Default
        # binds to :meth:`execute` (the same async method exposed
        # via the RPC), so the scheduler dispatches through the
        # controller's own body and tests can inject a stub.
        self._execute_fn: Callable[[str], Awaitable[str]] = execute_fn or self.execute
        # Runner ownership lives here (previously smuggled onto
        # ``session.pool._scheduler_runner`` — that private-attr
        # reach-in is cured).
        self._runner: SchedulerRunner | None = None

    # ── Per-task RPCs ─────────────────────────────────────────────

    async def execute(self, description: str) -> str:
        """Run one scheduled task through the agent. Returns result text."""
        team = self._session.main_team
        response = await team.arun(description, stream=False)
        return extract_response_text(response)

    async def cancel(self, task_id: str) -> msg.Info:
        """Mark a scheduled task as cancelled in the store."""
        store = TaskStore(project_dir=self._session.project_dir)
        await store.update_status(task_id, TaskStatus.cancelled)
        return msg.Info(text=f"Cancelled task {task_id}")

    async def list_all(self, include_done: bool = True) -> list[ScheduledTask]:
        """Return every scheduled task from the store."""
        store = TaskStore(project_dir=self._session.project_dir)
        return await store.get_all(include_done=include_done)

    # ── Runner lifecycle ─────────────────────────────────────────

    def start(
        self,
        on_task_started: TaskStartedCallback = None,
        on_task_completed: TaskCompletedCallback = None,
    ) -> SchedulerRunner:
        """Start the background scheduler poller. Idempotent —
        returns the cached runner when one is already running.

        The positional ``(on_task_started, on_task_completed)``
        signature is preserved for :meth:`PushNotificationBridge.start_scheduler`,
        the only production caller of these callbacks.
        """
        if self._runner is not None and self._runner.is_running:
            return self._runner

        assert self._settings is not None, (
            "SchedulerController.start requires Settings; the pump-only "
            "test fixture that omits Settings should never reach this method"
        )
        sched_cfg = self._settings.scheduler
        store = TaskStore(project_dir=self._session.project_dir)

        bridge = self._TaskHookBridge(
            session_id=self._session.session_id,
            hook_executor=self._session.hook_executor,
            user_on_started=on_task_started,
            user_on_completed=on_task_completed,
        )

        runner = SchedulerRunner(
            store=store,
            execute_fn=self._execute_fn,
            on_task_started=bridge.on_started,
            on_task_completed=bridge.on_completed,
            poll_interval=sched_cfg.poll_interval,
            task_timeout=sched_cfg.task_timeout,
            max_concurrent=sched_cfg.max_concurrent,
        )
        runner.start()
        self._runner = runner
        return runner

    async def stop(self) -> None:
        """Stop the background runner. No-op when never started.

        Gives :class:`BackendServer` shutdown paths a composable
        teardown seam — the previous implementation left the runner
        pinned to the session pool with no explicit stop call site.
        """
        runner = self._runner
        if runner is None:
            return
        try:
            runner.stop()
        finally:
            self._runner = None

    @property
    def runner(self) -> SchedulerRunner | None:
        """Currently-running :class:`SchedulerRunner` or ``None``.

        Read-only accessor for tests that want to assert on runner
        state without going through :meth:`start`.
        """
        return self._runner

    # ── Nested hook bridge ───────────────────────────────────────

    class _TaskHookBridge:
        """Adapts the runner's ``(task_id, description[, success])``
        callback shape onto :class:`HookExecutor` events.

        Was two 20-line closures inside ``start_scheduler``; making
        the bridge a first-class object with two ``on_*`` methods
        replaces the ad-hoc pub/sub adapter and gives every fire
        site a typed :class:`TaskCreatedPayload` /
        :class:`TaskCompletedPayload` instead of a hand-built dict.
        """

        def __init__(
            self,
            *,
            session_id: str,
            hook_executor: HookExecutor,
            user_on_started: TaskStartedCallback,
            user_on_completed: TaskCompletedCallback,
        ) -> None:
            self._session_id = session_id
            self._hook_executor = hook_executor
            self._user_on_started = user_on_started
            self._user_on_completed = user_on_completed

        def on_started(self, task_id: str, description: str) -> None:
            """Runner callback for task start. Fires the user callback
            (if any) then dispatches the typed ``TaskCreated`` hook."""
            if self._user_on_started is not None:
                self._user_on_started(task_id, description)
            payload = TaskCreatedPayload(
                session_id=self._session_id,
                task_id=task_id,
                description=description,
            )
            asyncio.create_task(
                self._hook_executor.execute(
                    event=HookEvent.TASK_CREATED.value,
                    payload=payload.model_dump(),
                )
            )

        def on_completed(self, task_id: str, description: str, success: bool) -> None:
            """Runner callback for task completion. Fires the user
            callback (if any) then dispatches the typed
            ``TaskCompleted`` hook with a two-value status enum."""
            if self._user_on_completed is not None:
                self._user_on_completed(task_id, description, success)
            # ``status`` wire values match :class:`SchedulerEventType`
            # members — ``COMPLETED`` on success, ``ERROR`` (the
            # legacy alias for ``FAILED``) on failure. Kept as the
            # ``ERROR`` alias rather than ``FAILED`` to preserve
            # backward-compat with the existing hook-payload literal
            # (``TaskCompletedPayload.status: Literal["completed",
            # "error"]``) that FE consumers key on.
            payload = TaskCompletedPayload(
                session_id=self._session_id,
                task_id=task_id,
                description=description,
                status=(
                    SchedulerEventType.COMPLETED.value
                    if success
                    else SchedulerEventType.ERROR.value
                ),
            )
            asyncio.create_task(
                self._hook_executor.execute(
                    event=HookEvent.TASK_COMPLETED.value,
                    payload=payload.model_dump(),
                )
            )


__all__ = [
    "SchedulerController",
    "TaskStartedCallback",
    "TaskCompletedCallback",
]
