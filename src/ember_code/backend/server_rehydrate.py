"""Boot-time state-recovery helpers for :class:`BackendServer`.

Extracted from :mod:`ember_code.backend.server` — five async
functions that populate in-memory session stores from persisted
state on server startup. Each takes the ``BackendServer`` (called
`backend` inside the module) as an explicit argument.

Recovery is best-effort throughout: every failure path logs at
DEBUG level and returns rather than crashing startup. Partial
recovery is preferable to a broken restart.

Contents:

* :func:`rehydrate_event_log` — reload the append-only event
  log so ``get_session_events`` can serve it after restart.
* :func:`rehydrate_orphan_processes` — re-adopt any background
  shell processes that survived the previous BE lifetime.
* :func:`rehydrate_plan_decisions` — restore the
  ``{run_id: decision}`` map onto the live ``PlanStore``.
* :func:`rehydrate_todos` — overlay the persisted todo snapshot
  onto ``session.todo_store`` (authoritative once execution has
  started).
* :func:`rehydrate_plan_store` — repopulate ``PlanStore`` from
  the most recent ``exit_plan_mode`` tool call in the persisted
  Agno history.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ember_code.core.session.event_log_schema import SessionEvent

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


async def rehydrate_event_log(backend: "BackendServer") -> None:
    """Load the persisted append-only event log onto the session so
    ``get_session_events`` can serve it. Also restores
    ``_event_seq`` from the max ``seq`` seen so subsequent
    :meth:`Session.append_event` calls stay monotonic across the
    restart boundary.
    """
    persistence = getattr(backend._session, "persistence", None)
    if persistence is None:
        return
    try:
        entries = await persistence.load_event_log()
    except Exception as exc:
        logger.debug("event log rehydrate failed: %s", exc)
        return
    if not isinstance(entries, list):
        return
    # Parse each persisted dict back to a :class:`SessionEvent`,
    # dropping any that fail validation (corrupt row, schema drift).
    parsed = [
        evt
        for e in entries
        if isinstance(e, dict) and (evt := SessionEvent.from_wire(e)) is not None
    ]
    backend._session.event_log = parsed
    backend._session._event_seq = max((e.seq for e in parsed), default=0)


async def rehydrate_orphan_processes(backend: "BackendServer") -> None:
    """Re-adopt every backgrounded shell process that survived the
    previous BE lifetime. Without this, ``run_shell_command(background=True)``
    spawns that outlive a BE restart become invisible orphans — the
    OS keeps them alive via ``start_new_session=True`` but the
    registry resets to empty.
    """
    from ember_code.core.tools import process_log
    from ember_code.core.tools.shell import rehydrate_orphan_processes as _rehydrate

    # ``project_dir`` is optional on the session stub used by unit
    # tests — fall back to ``None`` so the log path resolver uses
    # TMPDIR and the rehydrate is a no-op, rather than crashing
    # startup.
    project_dir = getattr(backend._session, "project_dir", None)
    process_log.set_default_project_dir(project_dir)
    if project_dir is None:
        return
    try:
        await _rehydrate(project_dir)
    except Exception as exc:
        logger.debug("orphan process rehydrate failed: %s", exc)


async def rehydrate_plan_decisions(backend: "BackendServer") -> None:
    """Load the ``{run_id: decision}`` map persisted on
    ``session_data`` back into the in-memory ``PlanStore``.

    Without this, after BE restart every plan's state would fall
    back to the default "pending" / "approved" logic in
    ``get_chat_history`` and the user's previous approval clicks
    would silently vanish from the FE.
    """
    store = getattr(backend._session, "plan_store", None)
    if store is None:
        return
    persistence = getattr(backend._session, "persistence", None)
    if persistence is None:
        return
    try:
        data = await persistence.load_plan_decisions()
    except Exception as exc:
        logger.debug("plan decision rehydrate failed: %s", exc)
        return
    store.load_decisions(data)


async def rehydrate_todos(backend: "BackendServer") -> None:
    """Load the persisted todo snapshot back into
    ``session.todo_store``.

    Order matters: this runs AFTER :func:`rehydrate_plan_store` so
    it overwrites the plan-args seeding only when a real snapshot
    exists (i.e., execution has happened since the plan submission).
    """
    todo = getattr(backend._session, "todo_store", None)
    persistence = getattr(backend._session, "persistence", None)
    if todo is None or persistence is None:
        return
    try:
        snapshot = await persistence.load_todos()
    except Exception as exc:
        logger.debug("todo rehydrate failed: %s", exc)
        return
    if not snapshot:
        return  # no live execution state yet; keep the plan-args seed
    try:
        from ember_code.core.tools.todo import _coerce_items

        items, _errs = _coerce_items(snapshot)
        if items:
            todo.set(items)
    except Exception as exc:
        logger.debug("todo rehydrate: coerce failed: %s", exc)


async def rehydrate_plan_store(backend: "BackendServer") -> None:
    """Repopulate ``session.plan_store`` from the persisted history.

    ``PlanStore`` is in-memory only — submitted via the agent's
    ``exit_plan_mode`` tool, never written to its own table. On BE
    restart the store is empty even when the previous run clearly
    produced a plan. We walk the Agno session for the most recent
    ``exit_plan_mode`` tool call and pull its ``plan`` / ``tasks``
    arguments back into the live stores, so the FE's restore path
    sees the same PlanCard it did before close.
    """
    store = getattr(backend._session, "plan_store", None)
    if store is None or store.latest:
        return  # nothing to do (already populated or absent)
    try:
        agent = backend._session.main_team
        agno_session = await agent.aget_session(
            session_id=backend._session.session_id,
            user_id=backend._session.user_id,
        )
    except Exception as exc:
        logger.debug("plan rehydrate: aget_session failed: %s", exc)
        return
    if agno_session is None:
        return
    runs = getattr(agno_session, "runs", None) or []
    for run in reversed(runs):
        messages = getattr(run, "messages", None) or []
        for m in reversed(messages):
            if getattr(m, "role", "") != "assistant":
                continue
            tool_calls = getattr(m, "tool_calls", None) or []
            for tc in tool_calls:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                if fn.get("name") != "exit_plan_mode":
                    continue
                args_raw = fn.get("arguments")
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except Exception:
                        continue
                elif isinstance(args_raw, dict):
                    args = args_raw
                else:
                    continue
                plan_text = str(args.get("plan", "")).strip()
                if not plan_text:
                    continue
                store.set_plan(plan_text)
                tasks_raw = args.get("tasks")
                todo = getattr(backend._session, "todo_store", None)
                if todo is not None and isinstance(tasks_raw, list):
                    try:
                        from ember_code.core.tools.todo import _coerce_items

                        items, _errs = _coerce_items(tasks_raw)
                        if items:
                            todo.set(items)
                    except Exception as exc:
                        logger.debug("plan rehydrate: todo coerce failed: %s", exc)
                logger.info(
                    "Rehydrated plan_store from history (run_id=%s, plan=%d chars)",
                    getattr(run, "run_id", ""),
                    len(plan_text),
                )
                return  # most recent plan wins; stop scanning
