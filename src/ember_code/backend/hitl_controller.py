"""HITL (human-in-the-loop) resolution + permission-rule persistence.

Renamed from :mod:`ember_code.backend.server_hitl` — the "server_"
prefix implied a free-function module that took ``BackendServer``
first-arg (Rule 6 offender). The rename + shim-removal make the
class-per-concern layout obvious: :class:`HitlController` owns the
whole HITL surface for one :class:`Session`, and
:class:`AgnoDecisionApplier` owns the ``req.confirm()`` /
``req.reject()`` dispatch behind an enum (Pattern 7 — wire keeps
the string literal; domain uses the enum).

* :meth:`HitlController.resolve_batch` — resolve every requirement
  from a multi-req pause in one shot (fixes the v0.5.11 "User
  denied" cascade).
* :meth:`HitlController.resolve_single` — thin shim over
  :meth:`resolve_batch` for the legacy single-req entry point.
* :meth:`HitlController.check_permission` — check permission level
  for a tool call.
* :meth:`HitlController.save_permission_rule` — persist a
  permission rule.
* :meth:`HitlController.maybe_persist_choice` — persist the
  user's HITL choice as a permission rule when they picked a
  sticky option.

Composition: the controller holds one
:class:`AgnoDecisionApplier` instance as ``self._decision_applier``.
The applier is a small class rather than a free function so the
``req.confirm()`` / ``req.reject()`` dispatch is a real method
lookup (polymorphic on :class:`HitlAction`), not a stringly-typed
compare on ``decision.action``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ember_code.backend.hitl_stream_mux import HITLStreamMultiplexer
from ember_code.backend.hitl_tracer import HITLTracer
from ember_code.backend.pause_handler import PauseHandler
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.backend.schemas_hitl import (
    ApplyDecisionResult,
    HitlAction,
    PersistChoiceResult,
    RunRequirement,
    StreamFactory,
    ToolCallArgs,
)
from ember_code.backend.schemas_pause import PauseHandleResult, PendingRequirement
from ember_code.backend.schemas_run import StopHookPayload
from ember_code.core.config.permission_eval import PermissionRule
from ember_code.core.config.tool_permissions import (
    PermissionLevel,
    ToolInvocation,
    ToolNameResolver,
    ToolPermissions,
)
from ember_code.core.hooks.events import HookEvent
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)

_HITL_LLM_LOGGER = logging.getLogger("ember_code.llm_calls")


class AgnoDecisionApplier:
    """Applies an :class:`msg.HITLDecision` to an Agno
    :class:`RunRequirement` via ``confirm()`` / ``reject()``.

    Extracted from ``HitlController._apply_agno_decision`` (a free
    method) so the dispatch is polymorphic on :class:`HitlAction`
    (a real enum) rather than a stringly-typed ``if decision.action
    == "confirm"`` compare. Kept as a small collaborator so
    :class:`HitlController` composes rather than inherits — one
    instance lives on the controller and is reused across every
    ``resolve_batch`` call.

    Wire/domain split (Pattern 7): the caller
    (:meth:`HitlController.resolve_batch`) builds the wire
    ``msg.Error`` from the returned :class:`ApplyDecisionResult`.
    The applier itself never touches ``msg.*`` — it deals in the
    domain enum + Result.
    """

    def __init__(self) -> None:
        """Stateless — one instance per :class:`HitlController`."""

    def apply(self, decision: msg.HITLDecision, req: RunRequirement) -> ApplyDecisionResult:
        """Call ``req.confirm()`` or ``req.reject(...)``.

        Returns :class:`ApplyDecisionResult` — ``ok=False`` when
        the Agno call raised. Caller yields an ``msg.Error`` built
        from ``result.reason`` on failure.
        """
        try:
            action = HitlAction(decision.action)
        except ValueError:
            reason = (
                f"Unknown HITL action '{decision.action}' for requirement {decision.requirement_id}"
            )
            logger.warning("resolve_hitl_batch: %s", reason)
            return ApplyDecisionResult(ok=False, reason=reason)

        try:
            if action is HitlAction.CONFIRM:
                req.confirm()
            else:
                req.reject(note="User denied")
        except Exception as exc:
            logger.warning(
                "resolve_hitl_batch: requirement %s %s() raised %s; skipping",
                decision.requirement_id,
                action.value,
                exc,
            )
            return ApplyDecisionResult(
                ok=False,
                reason=(f"Failed to {action.value} requirement {decision.requirement_id}: {exc}"),
            )
        return ApplyDecisionResult(ok=True)


class HitlController:
    """HITL resolution + permission-rule persistence for a single
    session.

    Owns every HITL responsibility scattered previously on
    ``BackendServer``:

    * :meth:`resolve_batch` / :meth:`resolve_single` — the FE
      resolution entry points.
    * :meth:`check_permission` / :meth:`save_permission_rule` —
      the wire-side permission RPCs.
    * :meth:`maybe_persist_choice` — persist a sticky HITL choice
      as a permission rule.
    * :meth:`handle_pause` — evaluate a paused team event
      (previously ``server._handle_pause``); returns a typed
      :class:`PauseHandleResult`.
    * :meth:`build_subagent_run_paused` — wrap a sub-agent
      coordinator batch into an ``msg.RunPaused``.
    * :meth:`stream_with_subagent` — instantiate a fresh
      :class:`HITLStreamMultiplexer` for one team-stream lifecycle.
    * :meth:`sweep_run` — drop pending + auto-resolved requirements
      for a finished run.

    Construction accepts the optional ``pause_handler`` +
    ``tracer`` collaborators so tests can inject fakes without
    monkey-patching ``BackendServer``. Production wiring builds
    both from the session so the "reach into ``_hitl_store`` /
    ``_hitl_tracer``" that server.py used to do is gone.
    """

    def __init__(
        self,
        session: Session,
        store: PendingRequirementsStore,
        stream_factory: StreamFactory | None = None,
        *,
        pause_handler: PauseHandler | None = None,
        tracer: HITLTracer | None = None,
    ) -> None:
        self._session = session
        self._store = store
        # ``stream_factory`` is retained for backwards-compat with
        # legacy call sites that inject a stub. When omitted, the
        # controller drives its own :class:`HITLStreamMultiplexer`
        # via :meth:`stream_with_subagent` — the mux is instantiated
        # per stream because its queue + drain-task state is
        # per-call.
        self._stream_factory: StreamFactory = stream_factory or self.stream_with_subagent
        self._decision_applier = AgnoDecisionApplier()
        self._pause_handler = pause_handler or PauseHandler(
            evaluator=getattr(session, "permission_evaluator", None),
            store=store,
        )
        self._tracer = tracer or HITLTracer(enabled=False)

    async def resolve_batch(self, decisions: list[msg.HITLDecision]) -> AsyncIterator[msg.Message]:
        """Resolve every requirement from a multi-req pause in one shot.

        Agno's ``acontinue_run`` denies anything not in the
        resolved-requirements list.
        """
        if not decisions:
            return

        main_resolved_reqs: list[RunRequirement] = []
        run_id: str | None = None
        for d in decisions:
            if self._session.sub_agent_hitl.resolve(d.requirement_id, d.action):
                # Sub-agent coordinator owns this req — drop any
                # main-team entry under the same id.
                self._store.pop(d.requirement_id)
                continue
            entry = self._store.pop(d.requirement_id)
            if entry is None:
                yield msg.Error(text=f"Unknown requirement: {d.requirement_id}")
                continue
            req, this_run_id = entry.req, entry.run_id
            cross_run_error = self._guard_cross_run(d.requirement_id, entry, this_run_id, run_id)
            if cross_run_error is not None:
                yield cross_run_error
                continue
            if run_id is None:
                run_id = this_run_id
            applied = self._decision_applier.apply(d, req)
            if not applied.ok:
                yield msg.Error(text=applied.reason)
                continue
            main_resolved_reqs.append(req)
            persist_result = self.maybe_persist_choice(d, req)
            if not persist_result.ok and persist_result.reason:
                logger.debug(
                    "maybe_persist_choice: skipped for %s — %s",
                    d.requirement_id,
                    persist_result.reason,
                )

        self._merge_auto_resolved(main_resolved_reqs, run_id)

        if not main_resolved_reqs:
            return  # everything was sub-agent or failed

        team = self._session.main_team

        _HITL_LLM_LOGGER.info(
            "resolve_hitl_batch: %d req(s) resolved, run_id=%s",
            len(main_resolved_reqs),
            run_id,
        )
        async for proto in self._stream_factory(
            team.acontinue_run(
                run_id=run_id,
                session_id=self._session.session_id,
                requirements=main_resolved_reqs,
                stream=True,
                stream_events=True,
            )
        ):
            yield proto

        # Fire Stop hook after continuation completes.
        stop_result = await self._session.hook_executor.execute(
            event=HookEvent.STOP.value,
            payload=StopHookPayload(session_id=self._session.session_id).model_dump(),
        )
        if stop_result.message and not stop_result.should_continue:
            yield msg.Info(text=stop_result.message)

    async def resolve_single(
        self, requirement_id: str, action: str, choice: str = msg.HITLChoice.ONCE.value
    ) -> AsyncIterator[msg.Message]:
        """Resolve a single HITL requirement.

        Implemented as a thin shim over :meth:`resolve_batch` so
        the dangerous ``acontinue_run(requirements=[req])`` call
        site only exists in one place.

        ``action`` / ``choice`` typed as ``str`` for wire-compat;
        producers can pass :class:`msg.HITLAction` /
        :class:`msg.HITLChoice` members (StrEnum coerces to str).
        """
        decision = msg.HITLDecision(requirement_id=requirement_id, action=action, choice=choice)
        async for proto in self.resolve_batch([decision]):
            yield proto

    def check_permission(
        self, tool_name: str, func_name: str, tool_args: ToolCallArgs
    ) -> PermissionLevel:
        """Check permission level for a tool call.

        Accepts :class:`ToolCallArgs` (validated at the RPC seam)
        rather than a raw ``dict[str, Any]`` — the widest
        wire→domain boundary lives at ``rpc_router._check_permission``.
        """
        perms = ToolPermissions(project_dir=self._session.project_dir)
        # Prefer the Agno-function-name table over the caller-supplied
        # ``tool_name`` — matches the historical
        # ``FUNC_TO_TOOL.get(func_name, tool_name)`` fallback shape.
        # The catalog side (``run_shell_command`` → ``Bash``) is the
        # authoritative name for permission dispatch.
        resolver = ToolNameResolver()
        registry_name = resolver.catalog_for(func_name) or tool_name
        return perms.check(registry_name, func_name, tool_args.as_dict())

    def save_permission_rule(self, rule: str, level: PermissionLevel) -> None:
        """Persist a permission rule."""
        perms = ToolPermissions(project_dir=self._session.project_dir)
        perms.save_rule(rule, level)

    def maybe_persist_choice(
        self, decision: msg.HITLDecision, req: RunRequirement
    ) -> PersistChoiceResult:
        """Persist the user's HITL choice as a permission rule when
        they picked a sticky option ("always" / "similar" / "deny").

        Returns :class:`PersistChoiceResult` — ``ok=False`` with a
        ``reason`` when the requirement was malformed or the
        choice was a per-invocation ``once``. Callers log
        ``reason`` at debug and move on; a genuine bug from a
        malformed decision blob still bubbles.

        Dispatch is on :class:`msg.HITLChoice` members via
        ``StrEnum``, replacing the previous stringly-typed
        ``choice in ("always", "similar", "deny")`` compare — the
        enum's :meth:`is_persistent` method owns the "which
        choices persist" invariant.
        """
        choice = msg.HITLChoice((decision.choice or "").lower())
        if not choice.is_persistent():
            return PersistChoiceResult(
                ok=False, reason=f"choice '{decision.choice}' is not persistable"
            )
        tool_execution = getattr(req, "tool_execution", None)
        tool_name = str(getattr(tool_execution, "tool_name", "") or "")
        tool_args = getattr(tool_execution, "tool_args", None) or {}
        if not tool_name:
            logger.warning(
                "maybe_persist_choice: missing tool metadata on requirement; "
                "choice=%s degraded to no-op",
                choice.value,
            )
            return PersistChoiceResult(ok=False, reason="missing tool metadata on requirement")
        # Prefer the catalog-name for the persisted rule so it's
        # readable in ``.ember/settings.local.json`` regardless of
        # whether the requirement arrived tagged with the Agno
        # function name or the friendly catalog name already.
        resolver = ToolNameResolver()
        canonical = resolver.catalog_for(tool_name) or tool_name
        invocation = ToolInvocation.from_raw(canonical, tool_args)
        if choice is msg.HITLChoice.SIMILAR:
            rule = invocation.pattern_rule()
        else:
            rule = invocation.exact_rule()
        level: PermissionLevel = "deny" if choice is msg.HITLChoice.DENY else "allow"
        perms = ToolPermissions(project_dir=self._session.project_dir)
        perms.save_rule(rule, level)
        self._patch_live_evaluator(rule, level)
        logger.info("Persisted HITL rule from choice=%s: %s %s", choice.value, level, rule)
        return PersistChoiceResult(ok=True, rule=rule, level=level)

    # ── Pause / stream lifecycle (moved from BackendServer) ─────────

    def handle_pause(self, event: Any) -> PauseHandleResult:
        """Convert a paused team event into a typed
        :class:`PauseHandleResult`.

        Migrated from ``BackendServer._handle_pause``. Uses the
        controller's owned :class:`PauseHandler` (bound at
        construction) rather than instantiating a fresh handler per
        call — fixes the two-site duplication where server.py built
        one here and the mux built another.
        """
        return self._pause_handler.handle(event)

    def build_subagent_run_paused(self, entries: list[tuple[str, Any]]) -> msg.Message:
        """Wrap a batch of sub-agent coordinator entries in an
        ``msg.RunPaused``.

        Migrated from ``BackendServer._build_subagent_run_paused``
        — delegates to the now-public
        :meth:`HITLStreamMultiplexer.build_subagent_paused` static
        so the wire shape is identical.
        """
        return HITLStreamMultiplexer.build_subagent_paused(entries)

    def sweep_run(self, run_id: str) -> None:
        """Drop pending + auto-resolved entries for a finished run.

        Migrated from ``BackendServer._drop_pending_for_run`` —
        thin wrapper on :meth:`PendingRequirementsStore.sweep_run`.
        """
        self._store.sweep_run(run_id)

    async def stream_with_subagent(
        self, team_stream: AsyncIterator[Any]
    ) -> AsyncIterator[msg.Message]:
        """Drive one team-stream lifecycle through a fresh
        :class:`HITLStreamMultiplexer`.

        Migrated from ``BackendServer._stream_with_subagent_hitl``.
        The mux is instantiated per stream because its
        ``asyncio.Queue`` + drain-task state is per-call — sharing
        would leak state across concurrent streams.
        """
        mux = HITLStreamMultiplexer(
            session=self._session,
            store=self._store,
            pause_handler=self._pause_handler,
            tracer=self._tracer,
        )
        async for proto in mux.stream(team_stream):
            yield proto

    def _guard_cross_run(
        self,
        requirement_id: str,
        entry: PendingRequirement,
        this_run_id: str | None,
        batch_run_id: str | None,
    ) -> msg.Error | None:
        """Reject any requirement whose ``run_id`` doesn't match the
        batch."""
        if batch_run_id is None or this_run_id == batch_run_id:
            return None
        self._store.register(requirement_id, entry)
        return msg.Error(
            text=(
                f"Cross-run HITL batch rejected: "
                f"requirement {requirement_id} belongs to run "
                f"{this_run_id} but batch is for run {batch_run_id}"
            )
        )

    def _merge_auto_resolved(
        self,
        main_resolved_reqs: list[RunRequirement],
        run_id: str | None,
    ) -> None:
        """Drain the auto-resolved bucket for ``run_id`` and merge
        into ``main_resolved_reqs``."""
        if run_id is None:
            return
        main_resolved_reqs.extend(self._store.drain_auto_resolved(run_id))

    def _patch_live_evaluator(self, rule: str, level: PermissionLevel) -> None:
        """Append ``rule`` to the session's live ``PermissionEvaluator``."""
        evaluator = getattr(self._session, "permission_evaluator", None)
        if evaluator is None:
            return
        parsed = PermissionRule.parse(rule)
        if parsed is None:
            return
        target = evaluator.deny if level == "deny" else evaluator.allow
        if parsed not in target:
            target.append(parsed)
