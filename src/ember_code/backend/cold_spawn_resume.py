"""Continue what a previous backend left waiting inside a ``spawn_agent`` call.

A sub-agent's approval outlives the backend that asked it
(:class:`~ember_code.core.approvals.journal.ApprovalJournal`),
and so does what hangs from it (``waiting_spawns``): the sub-agent's
paused run, and the chain of calls waiting above it — normally the main
agent's ``spawn_agent`` call. When the person answers after a restart,
nothing in this process is waiting: :class:`ColdSpawnResume` does what
the dead backend would have done.

1. Continue the sub-agent's run from Agno's stored copy with the
   decisions, through the same stream handler a live spawn uses — so a
   further pause asks the person again, and progress reaches the
   window.
2. Hand its answer to the waiting call as that tool's result: the
   caller's run is rebuilt as Agno pauses a run on a tool it executes
   outside (its messages up to the call, the call as an external
   execution, the answer as its result) and continued by run id.
3. Repeat up the chain; the last caller is the main agent, whose
   continued run streams to the session's windows like any resumed run.

The same happens on the desktop after igni is quit and started again.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput, RunStatus
from agno.run.requirement import RunRequirement

from ember_code.backend.schemas_hitl import StreamFactory
from ember_code.core.sub_agent_hitl import RestoredRun
from ember_code.core.tools.orchestrate import OrchestrateTools
from ember_code.core.tools.orchestrate_agent_stream import ResumedSubAgentStreamHandler
from ember_code.core.tools.orchestrate_events import SubAgentRegistry
from ember_code.core.tools.spawn_context import ParentCall, SpawnCalls
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class ColdSpawnResume:
    """Continues a restored sub-agent run and every call waiting above it."""

    def __init__(self, session: Session, stream_factory: StreamFactory) -> None:
        self._session = session
        self._stream_factory = stream_factory

    async def resume(self, ready: RestoredRun) -> AsyncIterator[msg.Message]:
        link = ready.link
        if link is None or not link.parents:
            yield msg.Info(
                text="The approval was recorded; the run that asked for it ended "
                "with the previous backend and cannot be continued."
            )
            return
        agent = self._pool_agent(link.agent_name)
        if agent is None:
            yield msg.Error(text=f"The agent {link.agent_name!r} is no longer available.")
            return
        result = await self._continue(
            ResumedSubAgentStreamHandler,
            agent,
            chain=link.parents,
            path=SpawnCalls.path_to(link.agent_name, link.parents),
            run_id=ready.run_id,
            session_id=link.agent_session_id,
            requirements=ready.requirements,
        )
        self._session.sub_agent_hitl.forget_spawn(ready.run_id)
        answer = self.tool_result(link.agent_name, link.parents[0], result)
        coordinator = self._session.sub_agent_hitl
        for depth, parent in enumerate(link.parents):
            run_output = self.paused_run(parent, answer, coordinator.turn_results(parent.run_id))
            coordinator.forget_turn(parent.run_id)
            if self._is_main(parent):
                main = self._session.main_team
                async for proto in self._stream_factory(
                    main.acontinue_run(
                        run_response=run_output,
                        requirements=list(run_output.requirements or []),
                        session_id=parent.session_id,
                        stream=True,
                        stream_events=True,
                    )
                ):
                    yield proto
                return
            caller = self._pool_agent(parent.agent_name)
            if caller is None:
                yield msg.Error(text=f"The agent {parent.agent_name!r} is no longer available.")
                return
            # Continued like a live sub-agent: its progress reaches the
            # window, and it can ask the person again — that pause is
            # stored with the calls still waiting above it.
            rest = link.parents[depth + 1 :]
            result = await self._continue(
                ResumedSubAgentStreamHandler,
                caller,
                chain=rest,
                path=SpawnCalls.path_to(parent.agent_name, rest),
                run_id=parent.run_id,
                session_id=parent.session_id,
                requirements=list(run_output.requirements or []),
                run_response=run_output,
            )
            # Its answer is the result of the call waiting on *it*.
            waiting = rest[0] if rest else parent
            answer = self.tool_result(parent.agent_name, waiting, result)

    async def _continue(
        self,
        handler_cls: Any,
        runnable: Any,
        *,
        chain: list[ParentCall],
        path: list[str],
        **resume: Any,
    ) -> str:
        """Run one continued agent through its stream handler, under the
        calls still waiting above it."""
        tools = self._orchestrate_tools()
        with SpawnCalls.restored(chain):
            handler = handler_cls(
                runnable,
                on_progress=tools.on_progress if tools is not None else None,
                hitl_coordinator=self._session.sub_agent_hitl,
                agent_path=path,
                card_id="",
                subagent_registry=(
                    tools.subagent_registry if tools is not None else SubAgentRegistry()
                ),
                event_appender=tools.event_appender if tools is not None else None,
                **resume,
            )
            result, _activity = await handler.run()
        return str(result)

    @staticmethod
    def tool_result(agent_name: str, parent: ParentCall, result: str) -> str:
        """What the waiting call returns: the spawned agent's answer, headed
        the way a live spawn heads it."""
        task = str(parent.tool_args.get("task", ""))
        return f"[Agent: {agent_name}]\n[Task: {task}]\n\nResponse:\n{result}"

    #: The result a call gets when the restart cut it off.
    INTERRUPTED = (
        "Not run to completion: igni restarted while this call was running, and its "
        "result was lost. It may or may not have taken effect — check before relying "
        "on it, or run it again."
    )

    @classmethod
    def paused_run(
        cls, parent: ParentCall, answer: str, finished: dict[str, str] | None = None
    ) -> RunOutput:
        """``parent``'s run as Agno pauses one on tools executed outside it.

        The waiting call's result is ``answer``. Every other call of the
        same assistant turn gets its stored result when it finished, and
        :attr:`INTERRUPTED` when it was still running at the restart — it
        is not run again: a call cut off may already have done what it
        does (a deploy, a write), and doing it twice is worse than the
        model knowing it was interrupted. So the continued run sees a
        complete turn.
        """
        done = finished or {}
        executions: list[ToolExecution] = []
        requirements: list[RunRequirement] = []
        calls = parent.turn() or [
            {
                "id": parent.tool_call_id,
                "function": {"name": parent.tool_name, "arguments": "{}"},
            }
        ]
        for call in calls:
            call_id = str(call.get("id") or "")
            function = call.get("function") or {}
            if call_id == parent.tool_call_id:
                name, args, result = parent.tool_name, dict(parent.tool_args), answer
            else:
                name = str(function.get("name") or "")
                args = cls._arguments(function.get("arguments"))
                result = done.get(call_id, cls.INTERRUPTED)
            execution = ToolExecution(
                tool_call_id=call_id,
                tool_name=name,
                tool_args=args,
                external_execution_required=True,
            )
            requirement = RunRequirement(tool_execution=execution)
            requirement.set_external_execution_result(result)
            executions.append(execution)
            requirements.append(requirement)
        return RunOutput(
            run_id=parent.run_id,
            session_id=parent.session_id,
            agent_id=parent.agent_id or parent.agent_name,
            agent_name=parent.agent_name,
            messages=[Message.from_dict(m) for m in parent.messages],
            tools=executions,
            requirements=requirements,
            status=RunStatus.paused,
        )

    @staticmethod
    def _arguments(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _is_main(self, parent: ParentCall) -> bool:
        main = getattr(self._session, "main_team", None)
        return main is not None and parent.agent_name == str(getattr(main, "name", "") or "")

    def _pool_agent(self, name: str) -> Any:
        try:
            return copy.copy(self._session.pool.get(name))
        except KeyError:
            logger.warning("cold resume: no agent %r in the pool", name)
            return None

    def _orchestrate_tools(self) -> OrchestrateTools | None:
        for tool in getattr(self._session.main_team, "tools", None) or []:
            if isinstance(tool, OrchestrateTools):
                return tool
        return None


__all__ = ["ColdSpawnResume"]
