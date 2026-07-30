"""Runner classes for :mod:`orchestrate` spawns.

Extracted from ``orchestrate.py`` so the :class:`OrchestrateTools`
Agno-facing facade stays small and the spawn machinery becomes
testable in isolation.

Hierarchy:

* :class:`SpawnRunner` — abstract base. Holds every collaborator
  the two spawn shapes share (hook executor, progress callback,
  event appender, sub-agent registry, HITL coordinator, budget,
  and an injected :class:`SpawnDeps` bundle) and owns the real
  template :meth:`run` — reserve budget, run preflight, build
  the runnable, fire SubagentStart, emit ``agent_started``
  progress, delegate the stream inside :func:`asyncio.wait_for`,
  fire SubagentStop exactly once, and compose the parent-facing
  response via :meth:`format_result`.
* :class:`AgentSpawn` — single-agent flavour. Owns a
  :class:`SpawnSandbox` for optional worktree isolation and
  delegates streaming to the injected ``agent_streamer``.
* :class:`TeamSpawn` — sub-team flavour. Builds an Agno
  :class:`Team` from a name list + mode via the injected
  ``team_factory`` / ``model_registry_factory`` and delegates
  streaming to the injected ``team_streamer``. No sandbox — team
  isolation is a per-member concern.

Dependency policy: streaming callables + :class:`Team` /
:class:`ModelRegistry` factories are injected via
:class:`SpawnDeps` at construction time. The facade
(:class:`OrchestrateTools`) reads them off the :mod:`orchestrate`
module at *tool-invocation* time, so test monkey-patches on
``ember_code.core.tools.orchestrate._run_agent_streaming`` /
``.Team`` / ``.ModelRegistry`` continue to take effect (the
patched attribute is re-read on every ``spawn_agent`` /
``spawn_team`` call before the runner is constructed).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import time
import uuid
from typing import TYPE_CHECKING, Any

from ember_code.core.tools.orchestrate_events import (
    AgentBuildResult,
    AgentStartedEvent,
    EventAppender,
    HitlCoordinatorProtocol,
    IsolationMode,
    OnProgress,
    SpawnDeps,
    SpawnResult,
    SubAgentRegistry,
    SubagentStartPayload,
    SubagentStopPayload,
    SubTeamBuildResult,
    TeamMode,
)
from ember_code.core.tools.orchestrate_sandbox import SpawnSandbox

if TYPE_CHECKING:
    from pathlib import Path

    from ember_code.core.agents import AgentPool
    from ember_code.core.config.settings import Settings
    from ember_code.core.hooks.executor import HookExecutor
    from ember_code.core.tools.orchestrate_budget import SpawnBudget


class SpawnRunner:
    """Abstract base for a single sub-spawn.

    Owns the template :meth:`run` skeleton — depth guard, budget
    reservation, sub-runnable build via :meth:`_build`, hook lifecycle,
    ``agent_started`` emission, :func:`asyncio.wait_for` timeout, and
    the single ``SubagentStop`` fire site (guarded by
    ``_stop_fired`` so subclass overrides can't accidentally fire it
    twice — see the ``fires-exactly-once`` invariant).

    Concrete subclasses override:

    * :meth:`_preflight` — cheap validation *before* the budget is
      reserved (depth already checked upstream). Return an error
      string to short-circuit or ``None`` to proceed.
    * :meth:`_reserve_count` — how many budget slots to reserve.
    * :meth:`_build` — assemble the runnable (agent or team) and
      return a build-result envelope. Returning an envelope with
      ``.error`` short-circuits.
    * :meth:`_execute` — actually stream the runnable and return
      ``(result_text, activity_lines)``.
    * :meth:`_agent_label` — the ``agent_name`` field on the
      SubagentStart / SubagentStop payloads.
    * :meth:`format_result` — compose the parent-facing string.
    * :meth:`_on_finally` — optional cleanup (worktree finalize).

    All spawn-wide collaborators arrive on the constructor so the
    class is testable without a full :class:`OrchestrateTools`.
    """

    def __init__(
        self,
        *,
        pool: AgentPool,
        settings: Settings,
        session_id: str,
        hook_executor: HookExecutor | None,
        on_progress: OnProgress | None,
        event_appender: EventAppender | None,
        subagent_registry: SubAgentRegistry,
        hitl_coordinator: HitlCoordinatorProtocol | None,
        budget: SpawnBudget,
        current_depth: int,
        max_depth: int,
        project_dir: Path | None,
        deps: SpawnDeps,
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._session_id = session_id
        self._hook_executor = hook_executor
        self._on_progress = on_progress
        self._event_appender = event_appender
        self._subagent_registry = subagent_registry
        self._hitl_coordinator = hitl_coordinator
        self._budget = budget
        self._current_depth = current_depth
        self._max_depth = max_depth
        self._project_dir = project_dir
        self._deps = deps
        # Stable per-spawn id — stamped on every orchestrate event
        # emitted for this spawn so the FE routes them all into the
        # same team-progress card. See ``BaseStreamHandler._emit``.
        self._card_id = uuid.uuid4().hex[:8]
        self._spawn_timeout = settings.orchestration.sub_team_timeout
        # Guards the "SubagentStop fires exactly once" invariant. Any
        # subclass override calling ``_fire_stop`` from ``_execute``
        # would still trip the flag; the base's finally-branch reads
        # it and skips a second fire.
        self._stop_fired = False

    # ── Template method ──────────────────────────────────────────

    async def run(self) -> str:
        """Template method — orchestrates the whole spawn lifecycle.

        Returns the parent-facing response string (Agno tool return
        contract). Internally routes error branches through
        :class:`SpawnResult` before rendering via
        :meth:`_render_error` so the failure information is typed at
        the boundary even though the outer return is a string.
        """
        if self._current_depth >= self._max_depth:
            return f"Error: Maximum nesting depth ({self._max_depth}) reached."

        if err := self._preflight():
            return err

        if limit_err := self._budget.try_reserve(self._reserve_count()):
            return limit_err

        build_err = self._build()
        if build_err is not None:
            return build_err

        label = self._agent_label()
        await self._fire_start(
            SubagentStartPayload(
                session_id=self._session_id,
                agent_name=label,
                task=self._task_preview(),
                mode=self._start_mode(),
            )
        )
        self._on_started_progress()

        try:
            start = time.monotonic()
            result, activity = await asyncio.wait_for(
                self._execute(),
                timeout=self._spawn_timeout,
            )
            elapsed = time.monotonic() - start

            await self._fire_stop_once(
                SubagentStopPayload(
                    session_id=self._session_id,
                    agent_name=label,
                    result_preview=result[:500],
                )
            )
            rendered = self.format_result(elapsed=elapsed, result=result, activity=activity)
            self._on_finally()
            return rendered
        except asyncio.TimeoutError:
            envelope = SpawnResult(ok=False, message=self._timeout_message())
            await self._fire_stop_once(
                SubagentStopPayload(
                    session_id=self._session_id,
                    agent_name=label,
                    error=envelope.message,
                )
            )
            self._on_finally()
            return self._render_error(envelope)
        except Exception as exc:
            envelope = SpawnResult(ok=False, message=self._exception_message(exc))
            await self._fire_stop_once(
                SubagentStopPayload(
                    session_id=self._session_id,
                    agent_name=label,
                    error=envelope.message,
                )
            )
            self._on_finally()
            return self._render_error(envelope)

    # ── Extension points ─────────────────────────────────────────

    def _preflight(self) -> str | None:
        """Cheap validation before budget reservation.

        Return an error string to short-circuit, or ``None`` to
        proceed. Base impl: no-op.
        """
        return None

    def _reserve_count(self) -> int:
        """How many budget slots this spawn reserves."""
        return 1

    def _build(self) -> str | None:
        """Assemble the runnable (agent or team). Return an error
        string to short-circuit, or ``None`` on success (subclass
        stashes the runnable on ``self``).
        """
        return None

    async def _execute(self) -> tuple[str, list[str]]:
        """Stream the built runnable. Returns ``(result, activity)``."""
        raise NotImplementedError

    def _agent_label(self) -> str:
        """The ``agent_name`` field on the SubagentStart/Stop payloads."""
        raise NotImplementedError

    def _task_preview(self) -> str:
        """Task string clipped to 500 chars for hook payloads."""
        return getattr(self, "_task", "")[:500]

    def _start_mode(self) -> str | None:
        """Optional ``mode`` on the SubagentStart payload."""
        return None

    def _on_started_progress(self) -> None:
        """Emit any spawn-specific ``agent_started`` progress event."""

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        """Compose the parent-facing response string."""
        raise NotImplementedError

    def _on_finally(self) -> None:
        """Optional cleanup — always called on the way out."""

    def _timeout_message(self) -> str:
        """Error message for the timeout branch."""
        return f"Sub-spawn exceeded spawn timeout ({self._spawn_timeout}s) and was aborted."

    def _exception_message(self, exc: Exception) -> str:
        """Error message for the generic-exception branch."""
        return f"Error running sub-spawn: {exc}"

    def _render_error(self, envelope: SpawnResult) -> str:
        """Turn a :class:`SpawnResult` failure into the outer string
        return. Subclasses can override to add trailers.
        """
        return envelope.message

    # ── Hook / progress plumbing ─────────────────────────────────

    async def _fire_start(self, payload: SubagentStartPayload) -> None:
        await self._fire_hook("SubagentStart", payload.model_dump(exclude_none=True))

    async def _fire_stop_once(self, payload: SubagentStopPayload) -> None:
        """Fire ``SubagentStop`` at most once per spawn.

        Guards the invariant even if a subclass override calls
        ``_fire_stop_once`` from ``_execute`` or ``_on_finally`` —
        subsequent calls are no-ops.
        """
        if self._stop_fired:
            return
        self._stop_fired = True
        await self._fire_hook("SubagentStop", payload.model_dump(exclude_none=True))

    async def _fire_hook(self, event: str, payload: dict[str, Any]) -> None:
        if not self._hook_executor:
            return
        with contextlib.suppress(Exception):
            await self._hook_executor.execute(event=event, payload=payload)

    def _emit_agent_started(self, agent_name: str, task: str) -> None:
        """Emit the ``agent_started`` progress event for this spawn.

        Uses :class:`AgentStartedEvent` (audit Pattern 2 — typed
        model at the emit site) then converts to a dict for the
        ``on_progress`` callback so downstream wire code stays
        dict-oriented.
        """
        if self._on_progress is None:
            return
        event = AgentStartedEvent(
            agent_path=agent_name,
            agent=agent_name,
            parent=None,
            task=task,
            card_id=self._card_id,
        )
        with contextlib.suppress(Exception):
            self._on_progress(event.model_dump(exclude_none=True, by_alias=True))


class AgentSpawn(SpawnRunner):
    """Single-agent spawn.

    Owns a :class:`SpawnSandbox` for optional worktree isolation.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        task: str,
        isolation: str,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(**base_kwargs)
        self._agent_name = agent_name
        self._task = task
        self._isolation_raw = isolation
        # Populated by :meth:`_preflight` — established here as
        # ``NONE`` so callers that inspect the attribute before
        # ``run()`` (tests, error paths) don't AttributeError.
        self._isolation: IsolationMode = IsolationMode.NONE
        # Populated by :meth:`_build`; consumed in ``_execute`` /
        # ``format_result`` / ``_on_finally``.
        self._agent: Any = None
        self._agent_desc: str = ""
        self._agent_tools: str = "none"
        self._effective_task: str = task
        self._sandbox: SpawnSandbox | None = None

    # ── SpawnRunner extension points ─────────────────────────────

    def _preflight(self) -> str | None:
        parsed = IsolationMode.parse_or_error(self._isolation_raw)
        # IsolationMode is a str-mixin enum, so ``isinstance(parsed, str)``
        # matches BOTH the enum success case and the error message. Check
        # for the enum first — an ``IsolationMode`` narrows to a success;
        # anything else is the error message returned by ``parse_or_error``.
        if not isinstance(parsed, IsolationMode):
            return parsed
        self._isolation = parsed
        return None

    def _build(self) -> str | None:
        result = self._build_agent()
        if result.error is not None:
            return result.error
        self._agent = result.agent
        self._agent_desc = result.defn.description if result.defn else ""
        self._agent_tools = (
            ", ".join(result.defn.tools) if result.defn and result.defn.tools else "none"
        )
        self._effective_task = result.task
        self._sandbox = result.sandbox
        return None

    def _build_agent(self) -> AgentBuildResult:
        """Assemble the per-spawn agent copy + sandbox.

        Mirrors :meth:`TeamSpawn._build_sub_team` so both spawn
        shapes have a symmetric build method (audit offender #5).
        Returns a typed :class:`AgentBuildResult` envelope: unknown
        agent names / worktree creation failures land as
        ``AgentBuildResult(agent=None, error='...')``.
        """
        try:
            shared = self._pool.get(self._agent_name)
        except KeyError as e:
            return AgentBuildResult(agent=None, error=str(e))

        # Shallow-copy per spawn. Agno ``Agent`` instances hold per-
        # run state (``run_id``, ``session_id``, ``run_response``);
        # the pool caches one instance per agent name and hands it to
        # every caller. Concurrent spawns of the same specialist race
        # on that state without a per-spawn copy.
        agent = copy.copy(shared)

        defn = self._pool.get_definition(self._agent_name)

        # Plugin-shipped agents force their own isolation regardless
        # of what the caller asked for — CC parity row 37.
        isolation_value = self._isolation.value
        if defn is not None and defn.force_isolation == "worktree":
            isolation_value = IsolationMode.WORKTREE.value

        setup = SpawnSandbox.create(
            project_dir=self._project_dir,
            session_id=self._session_id,
            agent=agent,
            agent_name=self._agent_name,
            isolation=isolation_value,
            task=self._task,
        )
        if setup.error is not None:
            return AgentBuildResult(agent=None, defn=defn, error=setup.error)

        return AgentBuildResult(
            agent=agent,
            defn=defn,
            isolation=isolation_value,
            sandbox=setup.sandbox,
            task=setup.task,
        )

    async def _execute(self) -> tuple[str, list[str]]:
        return await self._deps.agent_streamer(
            self._agent,
            self._effective_task,
            on_progress=self._on_progress,
            hitl_coordinator=self._hitl_coordinator,
            agent_path=[self._agent_name],
            card_id=self._card_id,
            subagent_registry=self._subagent_registry,
            event_appender=self._event_appender,
        )

    def _agent_label(self) -> str:
        return self._agent_name

    def _on_started_progress(self) -> None:
        self._emit_agent_started(self._agent_name, self._task)

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        """Compose the four-line header + activity log + response +
        (optional) run-error warning + worktree footer.

        Same layout the legacy ``_format_spawn_result`` produced —
        moved onto the class because every argument already came
        from ``self._*``.
        """
        header = self._header(elapsed)
        activity_log = "\n".join(activity) if activity else "  (no tool calls)"
        run_errors = [line for line in activity if "RUN ERROR" in line]
        error_section = ""
        if run_errors:
            error_section = (
                "\n\nWARNING: This sub-agent terminated with a run error — "
                "the response below is partial. Consider retrying, or proceed "
                "with the partial result if it's sufficient.\n" + "\n".join(run_errors)
            )
        worktree_footer = self._sandbox.finalize() if self._sandbox else ""
        return (
            f"{header}\n\n"
            f"Activity:\n{activity_log}\n\n"
            f"Response:\n{result}"
            f"{error_section}"
            f"{worktree_footer}"
        )

    def _header(self, elapsed: float) -> str:
        return (
            f"[Agent: {self._agent_name}] {self._agent_desc}\n"
            f"[Tools: {self._agent_tools}]\n"
            f"[Task: {self._task}]\n"
            f"[Time: {elapsed:.1f}s]"
        )

    def _on_finally(self) -> None:
        # ``_render_error`` doesn't call this — but the template's
        # ``_on_finally`` fires from every exit path. The sandbox's
        # ``finalize()`` is idempotent, so a second call after a
        # success path's ``format_result`` already finalized is safe.
        # For the error paths, this is the only finalize.
        pass

    def _timeout_message(self) -> str:
        return (
            f"Sub-agent '{self._agent_name}' exceeded spawn timeout "
            f"({self._spawn_timeout}s) and was aborted. The model "
            "provider likely stalled mid-stream."
        )

    def _exception_message(self, exc: Exception) -> str:
        return f"Error running sub-agent '{self._agent_name}': {exc}"

    def _render_error(self, envelope: SpawnResult) -> str:
        # Sandbox finalize on error paths — matches the pre-refactor
        # behaviour where every ``except`` branch called finalize
        # before returning the error string.
        if self._sandbox is not None:
            self._sandbox.finalize()
        return envelope.message


class TeamSpawn(SpawnRunner):
    """Sub-team spawn.

    Builds an Agno :class:`Team` from a comma-separated name list
    plus a mode and delegates streaming to the injected
    ``team_streamer``. Teams don't isolate today — the audit's
    Pattern 8 split intentionally keeps team spawns sandbox-free.
    """

    def __init__(
        self,
        *,
        names: list[str],
        task: str,
        mode: str,
        **base_kwargs: Any,
    ) -> None:
        super().__init__(**base_kwargs)
        self._names = names
        self._task = task
        self._mode_raw = mode
        # Populated by :meth:`_build`.
        self._team: Any = None
        self._mode: TeamMode = TeamMode.COORDINATE
        self._member_lines: list[str] = []
        self._team_label: str = ""

    # ── SpawnRunner extension points ─────────────────────────────

    def _preflight(self) -> str | None:
        if not self._names:
            return "Error: No agent names provided."
        return None

    def _reserve_count(self) -> int:
        return len(self._names)

    def _build(self) -> str | None:
        build = self._build_sub_team()
        if build.error is not None:
            return build.error
        self._team = build.team
        # ``SubTeamBuildResult.mode`` is the coerced string; wrap
        # back into the enum for downstream typed use.
        self._mode = TeamMode.coerce_or_default(build.mode)

        member_lines = []
        for n in self._names:
            defn = self._pool.get_definition(n)
            desc = defn.description[:60] if defn else ""
            member_lines.append(f"  - {n}: {desc}")
        self._member_lines = member_lines
        self._team_label = f"team({self._mode.value}:{','.join(self._names)})"
        return None

    def _build_sub_team(self) -> SubTeamBuildResult:
        """Assemble a sub-:class:`Team` for this spawn.

        Members get per-spawn shallow copies — see
        :meth:`AgentSpawn._build_agent` for the rationale. Sub-team
        members race on shared per-run state without the copy.

        Shares the session's DB with the pool so Agno's
        ``team.acontinue_run(run_id, session_id)`` can resolve the
        run — without it, broadcast_* / single_specialist_* cases
        hit "No runs found for run ID …".
        """
        members = []
        for name in self._names:
            try:
                members.append(copy.copy(self._pool.get(name)))
            except KeyError as e:
                return SubTeamBuildResult(team=None, mode=None, error=str(e))

        mode = TeamMode.coerce_or_default(self._mode_raw)
        team_model = self._deps.model_registry_factory(self._settings).get_model()
        team_kwargs: dict[str, Any] = {
            "name": f"sub-team-depth-{self._current_depth + 1}",
            "mode": mode.value,
            "model": team_model,
            "members": members,
            "markdown": True,
        }
        # Public accessor — replaces the ex ``getattr(pool, "_db")``.
        pool_db = self._pool.db
        if pool_db is not None:
            team_kwargs["db"] = pool_db
        if mode is TeamMode.TASKS:
            team_kwargs["max_iterations"] = self._settings.orchestration.max_task_iterations

        return SubTeamBuildResult(
            team=self._deps.team_factory(**team_kwargs), mode=mode.value, error=None
        )

    async def _execute(self) -> tuple[str, list[str]]:
        return await self._deps.team_streamer(
            self._team,
            self._task,
            on_progress=self._on_progress,
            hitl_coordinator=self._hitl_coordinator,
            agent_path=[self._team_label],
            card_id=self._card_id,
            subagent_registry=self._subagent_registry,
            event_appender=self._event_appender,
        )

    def _agent_label(self) -> str:
        return f"team({','.join(self._names)})"

    def _start_mode(self) -> str | None:
        return self._mode.value

    def format_result(self, *, elapsed: float, result: str, activity: list[str]) -> str:
        activity_log = "\n".join(activity) if activity else "  (no activity)"
        header = self._header(elapsed)
        return f"{header}\n\nActivity:\n{activity_log}\n\nResponse:\n{result}"

    def _header(self, elapsed: float) -> str:
        return (
            f"[Team: {', '.join(self._names)}] (mode: {self._mode.value})\n"
            f"[Members:\n" + "\n".join(self._member_lines) + "]\n"
            f"[Task: {self._task}]\n"
            f"[Time: {elapsed:.1f}s]"
        )

    def _timeout_message(self) -> str:
        return (
            f"Sub-team {self._team_label!r} exceeded spawn timeout "
            f"({self._spawn_timeout}s) and was aborted."
        )

    def _exception_message(self, exc: Exception) -> str:
        return f"Error running sub-team: {exc}"


__all__ = ["SpawnRunner", "AgentSpawn", "TeamSpawn"]
