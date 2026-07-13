"""Backend lifecycle — startup, interrupted-run detection, shutdown.

Extracted from :mod:`ember_code.backend.server`. Three free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates:

* :func:`startup` — awaited post-``__init__`` hook. Loads
  persisted ``/loop`` state, probes for a crashed-mid-run
  session (delegated to :func:`detect_interrupted_run`), and
  rehydrates the five persisted state roots (plan store, plan
  decisions, todos, event log, orphan processes) in a
  documented order — plan_store seeds first, todos overlays.
* :func:`detect_interrupted_run` — build a system-context note
  when the previous launch crashed mid-run. Consulted in
  order: Agno's session with ``status=running``, then the
  pending-message store (pre-persistence layer). One-shot per
  launch.
* :func:`shutdown` — graceful teardown: SessionEnd hook,
  ephemeral pool cleanup, MCP disconnect, background-process
  kill.

Rule 2 clean — all imports at module top, with the two agno /
tools imports at file-top since they're always used.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from agno.run.base import RunStatus

from ember_code.core.hooks.events import HookEvent
from ember_code.core.tools.shell import EmberShellTools

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


async def startup(backend: "BackendServer") -> None:
    """Async post-construction hook.

    ``Session.__init__`` is sync (lots of synchronous wiring) but
    a few subsystems need an awaited initialization step. Right
    now this hydrates the persisted ``/loop`` state — if the CLI
    was killed mid-loop, the prompt + counters are restored from
    ``state.db`` so the panel reflects the interrupted run.

    Also probes the resumed session for an in-flight run that
    never reached ``status=completed`` — that's the signature of
    a crash mid-chain. When detected, a one-shot summary is
    stashed so the next ``run_message`` can hand it to the
    agent as system context.

    Rehydrate order is load-bearing: ``plan_store`` seeds from
    ``exit_plan_mode(tasks=...)`` args, ``todos`` overlays live
    execution state.
    """
    await backend._session.load_persisted_loop_state()
    await detect_interrupted_run(backend)
    await backend._rehydrate_plan_store()
    await backend._rehydrate_plan_decisions()
    await backend._rehydrate_todos()
    await backend._rehydrate_event_log()
    await backend._rehydrate_orphan_processes()


async def detect_interrupted_run(backend: "BackendServer") -> None:
    """Build a system-context note if the previous launch crashed mid-run.

    Two independent signals are consulted, in order of how much
    they tell us:

    1. **Agno's session** — if ``aget_session`` returns a session
       whose latest run has ``status=running``, we have rich
       partial state (tool calls, partial content). Use that.
    2. **Pending-message store** — ours. If Agno's session has
       no interrupted run but the pre-persistence layer has a
       ``pending`` row, the previous process died before Agno
       ever wrote anything (the common case for text-only
       responses). We still know the user's question and that
       it didn't finish, which is enough to nudge the agent
       into recapping.

    The summary is one-shot per launch — consumed and cleared
    on the next ``run_message``. Pending rows are then
    discarded so they don't surface again on a subsequent
    restart.
    """
    try:
        session = await backend._session.main_team.aget_session(
            session_id=backend._session.session_id,
        )
        interrupted_run = None
        if session is not None:
            runs = getattr(session, "runs", None) or []
            if runs and getattr(runs[-1], "status", None) == RunStatus.running:
                interrupted_run = runs[-1]

        # Pending pre-persisted user messages — the only signal
        # we have when Agno never wrote anything for the crashed
        # run.
        try:
            pending = await backend._pending_store.alist_pending(backend._session.session_id)
        except Exception:
            pending = []

        if interrupted_run is None and not pending:
            return  # nothing to recover from — clean shutdown

        parts = ["Previous run was interrupted before completion."]
        if pending:
            # The pre-persisted question(s) the user actually
            # typed last time. Quoting verbatim so the agent
            # can recap their words rather than paraphrasing.
            if len(pending) == 1:
                parts.append(f"The user had asked: {pending[0].text!r}.")
            else:
                qs = "; ".join(p.text for p in pending)
                parts.append(f"The user had pending question(s): {qs!r}.")

        if interrupted_run is not None:
            tool_names: list[str] = []
            for t in getattr(interrupted_run, "tools", None) or []:
                name = getattr(t, "tool_name", None) or "?"
                tool_names.append(str(name))
            content = (getattr(interrupted_run, "content", None) or "")[:400]
            if tool_names:
                parts.append(f"Tool calls completed: {', '.join(tool_names)}.")
            if content.strip():
                parts.append(f"Partial response so far: {content!r}.")

        parts.append(
            "The user has not yet sent a new message. Decide whether to "
            "continue, recap what you found, or ask for direction."
        )
        backend._interrupted_run_summary = " ".join(parts)
        # Pending IDs are stashed so the next ``run_message`` can
        # discard them after the agent acknowledges the resume.
        # We deliberately do NOT discard here — the FE needs to
        # read the pending text via ``get_pending_messages`` to
        # render the interrupted question in the conversation
        # pane on ``--continue``. If we discarded eagerly the FE
        # would only have a single ``Info`` line referencing a
        # question the user could no longer see.
        backend._pending_message_ids_to_drop = [p.message_id for p in pending]

        logger.info(
            "detected interrupted previous run "
            "(agno_run=%s, pending=%d); summary will be injected on next user message",
            getattr(interrupted_run, "run_id", None),
            len(pending),
        )
    except Exception as exc:
        logger.debug("interrupted-run detection failed: %s", exc)


async def shutdown(backend: "BackendServer") -> None:
    """Graceful shutdown — disconnect MCP, fire hooks, kill bg processes."""
    with contextlib.suppress(Exception):
        await backend._session.hook_executor.execute(
            event=HookEvent.SESSION_END.value,
            payload={"session_id": backend._session.session_id},
        )
    with contextlib.suppress(Exception):
        if backend._session.settings.orchestration.auto_cleanup:
            backend._session.pool.cleanup_ephemeral()
    with contextlib.suppress(Exception):
        await backend._session.mcp_manager.disconnect_all()
    with contextlib.suppress(Exception):
        killed = EmberShellTools.cleanup()
        if killed:
            logger.info("Shutdown: killed %d background process(es)", killed)
