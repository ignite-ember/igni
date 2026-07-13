"""``/loop`` continuation + scheduler RPCs.

Extracted from :mod:`ember_code.backend.server`. Nine free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates:

* Loop pump — :func:`pop_pending_loop_iteration`,
  :func:`cancel_pending_loop`, :func:`loop_pause`,
  :func:`loop_resume`, :func:`loop_status`. Thin wrappers over
  :class:`Session` loop state; the actual counter math and
  persistence live on the session.
* Scheduler — :func:`execute_scheduled_task`,
  :func:`cancel_scheduled_task`, :func:`get_scheduled_tasks`,
  :func:`start_scheduler`. Owns the background poller that
  observes the persistent task store and dispatches into the
  agent.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ember_code.core.hooks.events import HookEvent
from ember_code.core.scheduler.models import TaskStatus
from ember_code.core.scheduler.runner import SchedulerRunner
from ember_code.core.scheduler.store import TaskStore
from ember_code.core.session.loop_ops import LoopAdvance
from ember_code.core.tools.loop import LoopTools
from ember_code.core.utils.response import extract_response_text
from ember_code.protocol import messages as msg
from pydantic import BaseModel

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


class LoopStatus(BaseModel):
    """Wire shape for :func:`loop_status` — cheap-to-poll snapshot
    of the ``/loop`` panel state.

    ``cap_explicit=False`` means the panel hides the "total"; True
    means it renders ``N / M``. ``announced_total`` (from
    ``loop_set_total``) takes precedence over ``cap_explicit`` when
    set — it reflects the *actual* item count derived from the
    work, not just a bound."""

    active: bool
    paused: bool
    prompt: str
    iteration_index: int
    iterations_remaining: int
    cap_explicit: bool
    announced_total: int | None


# ── Loop pump ─────────────────────────────────────────────────


async def pop_pending_loop_iteration(
    backend: "BackendServer",
) -> LoopAdvance | None:
    """Pop the next ``/loop`` iteration descriptor (or completion).

    Thin wrapper over :py:meth:`Session.advance_loop` — that
    method owns the counter math and the persistence write so
    a CLI restart sees the correct in-flight iteration.
    Returns shapes match what the FE's run controller expects.
    """
    return await backend._session.advance_loop()


async def cancel_pending_loop(backend: "BackendServer") -> bool:
    """Clear ``/loop`` state. Returns whether anything was actually
    cancelled.

    Called by the FE's ``process_message`` cancel guard when the
    user types a non-``/loop`` message — user input takes
    precedence over an *actively pumping* loop.

    Paused loops (loaded from disk on startup, not yet resumed)
    are intentionally NOT cancelled here: the user might be
    about to type ``/loop resume`` or simply asking something
    unrelated. If they want to discard the paused state they
    say ``/loop stop`` explicitly. Without this guard, the
    first character typed after a restart would destroy the
    very state the user might want to continue.
    """
    if backend._session.loop_paused:
        return False
    return await backend._session.cancel_loop()


async def loop_pause(backend: "BackendServer") -> bool:
    """Pause the active loop without advancing the counter.

    Called by the FE's ``_check_loop_continuation`` when an
    iteration's ``_run`` raised (e.g. a 429 from the model
    API). Keeping the counter at the failing iteration N
    means a subsequent ``/loop resume`` retries N, not skips
    to N+1.
    """
    return await backend._session.pause_loop()


async def loop_resume(backend: "BackendServer") -> str:
    """Flip the loop from paused to pumping and return the prompt.

    Returns the prompt verbatim so the panel-side app handler
    can fire ``_run(prompt)`` directly — same trick the slash
    ``/loop resume`` uses to bypass ``process_message``'s
    cancel guard. Returns an empty string when there's nothing
    to resume (no loop or not paused); the caller surfaces an
    appropriate message.
    """
    prompt = await backend._session.resume_loop()
    return prompt or ""


async def loop_status(backend: "BackendServer") -> LoopStatus:
    """Snapshot for the ``/loop`` panel header.

    Cheap read of the session loop fields — safe to poll at 1Hz
    from the panel while a loop is running. ``active`` mirrors
    ``pending_loop_prompt is not None`` so the panel can pick
    empty-state vs. live-state without inspecting the prompt.

    ``iteration_index`` is the count of iterations already
    *fired* (0-based when no iteration has run yet), and
    ``iterations_remaining`` is how many more *will* fire if
    the cap isn't shortened. Their sum on a running loop is the
    configured cap.
    """
    sess = backend._session
    # Read the agent's announced iteration total (if any) from
    # the loop_progress reserved key. Cheap (one indexed
    # lookup); safe to do on every poll. ``None`` when the
    # agent hasn't called ``loop_set_total`` yet.
    announced_total: int | None = None
    if sess.loop_run_id:
        raw = await sess.loop_progress_store.get(
            sess.loop_run_id, LoopTools._ANNOUNCED_TOTAL_KEY
        )
        if raw:
            try:
                announced_total = int(raw)
            except ValueError:
                announced_total = None
    return LoopStatus(
        active=sess.pending_loop_prompt is not None,
        paused=sess.loop_paused,
        prompt=sess.pending_loop_prompt or "",
        iteration_index=sess.loop_iteration_index,
        iterations_remaining=sess.loop_iterations_remaining,
        cap_explicit=sess.loop_cap_explicit,
        announced_total=announced_total,
    )


# ── Scheduler ─────────────────────────────────────────────────


async def execute_scheduled_task(backend: "BackendServer", description: str) -> str:
    """Execute a scheduled task via the agent. Returns result text."""
    team = backend._session.main_team
    response = await team.arun(description, stream=False)
    return extract_response_text(response)


async def cancel_scheduled_task(backend: "BackendServer", task_id: str) -> msg.Info:
    """Cancel a scheduled task."""
    store = TaskStore(project_dir=backend._session.project_dir)
    await store.update_status(task_id, TaskStatus.cancelled)
    return msg.Info(text=f"Cancelled task {task_id}")


async def get_scheduled_tasks(backend: "BackendServer", include_done: bool = True) -> list:
    """Get all scheduled tasks."""
    store = TaskStore(project_dir=backend._session.project_dir)
    return await store.get_all(include_done=include_done)


def start_scheduler(
    backend: "BackendServer",
    on_task_started=None,
    on_task_completed=None,
) -> Any:
    """Start the background scheduler. Idempotent: caches the
    runner on the pool so reconnects (and the Web/TUI both calling
    this) don't spawn duplicate pollers competing for the same
    task store. Returns the runner for stop()."""
    existing = getattr(backend._session.pool, "_scheduler_runner", None)
    if existing is not None and getattr(existing, "is_running", False):
        return existing

    sched_cfg = backend._settings.scheduler
    store = TaskStore(project_dir=backend._session.project_dir)

    # Compose the caller's task callbacks with hook-event firings
    # so plugins observe scheduler lifecycle without each call
    # site re-implementing it. TaskCreated fires when the
    # scheduler spawns a task (the moment the runtime first
    # touches it); TaskCompleted fires regardless of outcome
    # with the success/failure flag in ``status``.
    hook_executor = backend._session.hook_executor
    session_id = backend._session.session_id

    def _on_started(task_id: str, description: str) -> None:
        if on_task_started:
            on_task_started(task_id, description)
        asyncio.create_task(
            hook_executor.execute(
                event=HookEvent.TASK_CREATED.value,
                payload={
                    "session_id": session_id,
                    "task_id": task_id,
                    "description": description,
                },
            )
        )

    def _on_completed(task_id: str, description: str, success: bool) -> None:
        if on_task_completed:
            on_task_completed(task_id, description, success)
        asyncio.create_task(
            hook_executor.execute(
                event=HookEvent.TASK_COMPLETED.value,
                payload={
                    "session_id": session_id,
                    "task_id": task_id,
                    "description": description,
                    "status": "completed" if success else "error",
                },
            )
        )

    runner = SchedulerRunner(
        store=store,
        execute_fn=backend.execute_scheduled_task,
        on_task_started=_on_started,
        on_task_completed=_on_completed,
        poll_interval=sched_cfg.poll_interval,
        task_timeout=sched_cfg.task_timeout,
        max_concurrent=sched_cfg.max_concurrent,
    )
    runner.start()
    backend._session.pool._scheduler_runner = runner
    return runner
