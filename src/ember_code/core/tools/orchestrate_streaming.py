"""Thin public wrappers for the two OOP stream handlers.

Public API preserved verbatim so ``orchestrate.py`` and the test
suite import unchanged:

* :func:`run_agent_streaming` — spawn one specialist. Streams
  agent events, tracks HITL pauses, resumes on approve,
  finalises worktrees on completion.
* :func:`run_team_streaming` — same shape for a coordinated
  team. Threads the per-member ``agent_path`` through the FE
  event so the card shows ``team → member`` not just
  ``team``.

Both return ``(response, log)`` so the parent agent's tool
return contains the sub-agent's final text plus the
per-tool-call activity log line.

The heavy lifting moved to
:class:`SubAgentStreamHandler` (``orchestrate_agent_stream.py``)
and :class:`TeamStreamHandler` (``orchestrate_team_stream.py``),
both on top of :class:`BaseStreamHandler`. This module now
holds only the flat function API. Tests that monkeypatch
``ember_code.core.tools.orchestrate._run_agent_streaming`` /
``_run_team_streaming`` still resolve — those are aliases
in ``orchestrate.py`` for the two functions below.
"""

from __future__ import annotations

from typing import Any

from ember_code.core.tools.orchestrate_agent_stream import SubAgentStreamHandler
from ember_code.core.tools.orchestrate_events import (
    EventAppender,
    HitlCoordinatorProtocol,
    OnProgress,
    SubAgentRegistry,
)
from ember_code.core.tools.orchestrate_team_stream import TeamStreamHandler


async def run_agent_streaming(
    agent: Any,
    task: str,
    on_progress: OnProgress | None = None,
    hitl_coordinator: HitlCoordinatorProtocol | None = None,
    agent_path: list[str] | None = None,
    card_id: str = "",
    *,
    subagent_registry: SubAgentRegistry | None = None,
    event_appender: EventAppender | None = None,
) -> tuple[str, list[str]]:
    """Stream an agent run, collecting activity log. Returns
    ``(response, log)``.

    If ``hitl_coordinator`` is provided, ``RunPausedEvent``s from the
    sub-agent are surfaced through it so the user can confirm/deny in
    the TUI; the run is then resumed via ``acontinue_run`` and we keep
    iterating its events. Without a coordinator, pauses are ignored
    and the sub-agent's tools will return empty results.

    ``agent_path`` is the chain of agent names from the main
    orchestrator down to the agent being run here (e.g.
    ``["architect"]``). It rides along with each pause requirement so
    the FE dialog can name the specialist that's asking for
    permission, not just the tool.

    ``subagent_registry`` / ``event_appender`` are the injected
    collaborators that used to live as class attributes on
    :class:`OrchestrateTools`. Callers not driving a live session
    (unit tests, ad-hoc use) can leave them as defaults — a fresh
    empty registry and a no-op appender are created here.
    """
    handler = SubAgentStreamHandler(
        agent,
        task,
        on_progress=on_progress,
        hitl_coordinator=hitl_coordinator,
        agent_path=agent_path,
        card_id=card_id,
        subagent_registry=subagent_registry
        if subagent_registry is not None
        else SubAgentRegistry(),
        event_appender=event_appender,
    )
    return await handler.run()


async def run_team_streaming(
    team: Any,
    task: str,
    on_progress: OnProgress | None = None,
    hitl_coordinator: HitlCoordinatorProtocol | None = None,
    agent_path: list[str] | None = None,
    card_id: str = "",
    *,
    subagent_registry: SubAgentRegistry | None = None,
    event_appender: EventAppender | None = None,
) -> tuple[str, list[str]]:
    """Stream a team run, collecting activity log. Returns
    ``(response, log)``.

    Mirrors :func:`run_agent_streaming`'s pause-handling:
    ``RunPausedEvent`` from any team member is forwarded through the
    coordinator so the user can confirm/deny via the TUI, then we
    resume via ``acontinue_run``.

    ``agent_path`` is the chain of names down to this team. We pull
    each paused member's name from the requirement's
    ``member_agent_name`` (set by Agno when the requirement
    originates from a team member) and append it to ``path`` so the
    FE shows ``team → member`` not just ``team``.
    """
    handler = TeamStreamHandler(
        team,
        task,
        on_progress=on_progress,
        hitl_coordinator=hitl_coordinator,
        agent_path=agent_path,
        card_id=card_id,
        subagent_registry=subagent_registry
        if subagent_registry is not None
        else SubAgentRegistry(),
        event_appender=event_appender,
    )
    return await handler.run()
