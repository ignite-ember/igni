"""Backend lifecycle — startup, interrupted-run detection, shutdown.

Class-first module: :class:`LifecycleController` is the only entry
point. Startup / detect_interrupted_run / shutdown are typed
methods on it.

* :meth:`LifecycleController.startup` — awaited post-``__init__``
  hook. Delegates the rehydrate summary log to
  :class:`RehydrateOutcomeSet.log` so the outcomes own their
  logging ceremony.
* :meth:`LifecycleController.detect_interrupted_run` — delegates
  to :class:`InterruptedRunSummaryBuilder` for the actual
  assembly and hands a typed :class:`InterruptedRunSummary` to
  :meth:`RunController.set_interrupted_summary`.
* :meth:`LifecycleController.shutdown` — delegates to a
  :class:`ShutdownPipeline` built once in ``__init__``. Cleanup
  failures land on the returned :class:`ShutdownResult` (typed
  ``errors`` list) rather than getting swallowed by
  ``contextlib.suppress`` blocks.

The controller no longer reaches into ``backend._runs`` or
``backend._pending_store`` — both are constructor-injected.
Backward-compat free-function shims are gone; callers use
``backend.lifecycle.*`` directly (via the ``BackendServer``
delegates).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ember_code.backend.schemas_lifecycle import (
    InterruptedRunSummary,
    RehydrateOutcomeSet,
    ShutdownResult,
)
from ember_code.backend.server_interrupted_run import InterruptedRunSummaryBuilder
from ember_code.backend.server_shutdown import ShutdownPipeline

if TYPE_CHECKING:
    from ember_code.backend.run_controller import RunController
    from ember_code.backend.server import BackendServer
    from ember_code.backend.server_rehydrate import RehydrateController
    from ember_code.core.session import Session
    from ember_code.core.session.pending_messages import PendingMessageStore

logger = logging.getLogger(__name__)


class LifecycleController:
    """Startup / shutdown / interrupted-run detection for one BackendServer."""

    def __init__(
        self,
        session: Session,
        pending_store: PendingMessageStore | None,
        runs: RunController | None,
        rehydrate: RehydrateController,
        scheduler_stop: Callable[[], Awaitable[None]] | None = None,
        backend: BackendServer | None = None,
    ) -> None:
        self._session = session
        self._pending_store = pending_store
        self._runs = runs
        self._rehydrate = rehydrate
        # Held so :meth:`startup` can route rehydrate calls through
        # ``BackendServer._rehydrate_*`` / ``._detect_interrupted_run``
        # method-attributes, keeping the test-patch seams live for
        # ``__new__``-bypass fixtures that patch those names.
        # Documented AP6 test-shape concession: 12+ tests (e.g.
        # test_plan_rehydrate.py, test_plan_rpc_wiring.py,
        # test_session_restart_round_trip.py) bind ``AsyncMock`` on
        # those attribute names on partial-init instances — keeping
        # the underscore seam preserves those fixtures. Cleanup is
        # tracked as a follow-up ticket.
        self._backend = backend
        self._summary_builder = InterruptedRunSummaryBuilder(
            session=session,
            pending_store=pending_store,
        )
        # Shutdown pipeline is data on the controller, not a closure
        # blob in a method body. Scheduler-stop is threaded in here
        # so the pipeline decides once (at build time) whether to
        # include :class:`SchedulerStopStep`.
        self._shutdown_pipeline = ShutdownPipeline(
            session=session,
            scheduler_stop=scheduler_stop,
        )

    async def startup(self) -> None:
        """Async post-construction hook.

        Hydrates the persisted ``/loop`` state, probes for an
        interrupted-run signal, and rehydrates the five persisted
        state roots (plan store, plan decisions, todos, event log,
        orphan processes) in a documented order — plan_store seeds
        first, todos overlays. Each rehydrate step returns a typed
        :class:`RehydrateOutcome`; the batch is handed to
        :class:`RehydrateOutcomeSet` which owns the summary log.
        """
        await self._session.load_persisted_loop_state()
        # Route detect + rehydrate through BackendServer's thin
        # wrappers (`_detect_interrupted_run`, `_rehydrate_*`) so
        # tests that patch those method names on partial-init
        # instances (via ``BackendServer.__new__``) still intercept.
        # The wrappers themselves delegate to this controller — the
        # extra hop costs one function call per step.
        await self._backend._detect_interrupted_run()
        outcomes = RehydrateOutcomeSet.of(
            await self._backend._rehydrate_plan_store(),
            await self._backend._rehydrate_plan_decisions(),
            await self._backend._rehydrate_todos(),
            await self._backend._rehydrate_event_log(),
            await self._backend._rehydrate_orphan_processes(),
        )
        outcomes.log(logger)

    async def detect_interrupted_run(self) -> InterruptedRunSummary | None:
        """Build a system-context note if the previous launch crashed
        mid-run.

        Delegates assembly to :class:`InterruptedRunSummaryBuilder`
        and hands the typed :class:`InterruptedRunSummary` to
        :meth:`RunController.set_interrupted_summary`. Returns the
        summary so tests can assert on the shape without reaching
        through the run controller.
        """
        summary = await self._summary_builder.build()
        if summary is not None and self._runs is not None:
            self._runs.set_interrupted_summary(summary)
        return summary

    async def shutdown(self) -> ShutdownResult:
        """Graceful shutdown — disconnect MCP, fire hooks, kill bg processes.

        Returns a typed :class:`ShutdownResult` so cleanup failures
        are observable outcomes rather than silent swallows. The
        pipeline was built once in :meth:`__init__`; each step is
        best-effort (an exception in one step is recorded and the
        runner moves on).
        """
        return await self._shutdown_pipeline.run()
