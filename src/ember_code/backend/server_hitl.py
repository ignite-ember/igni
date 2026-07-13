"""HITL (human-in-the-loop) resolution + permission-rule persistence.

Extracted from :mod:`ember_code.backend.server` — the batch/single
requirement-resolution methods, ``check_permission`` /
``save_permission_rule`` shims, and the sticky-choice → permission-
rule persister that keeps "Always allow" from re-prompting on the
next call.

Free-function shape (``backend`` as first arg) so
:class:`BackendServer` doesn't grow another surface — the class
holds one-line delegates.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

from ember_code.core.config.permission_eval import PermissionRule
from ember_code.core.config.tool_permissions import (
    FUNC_TO_TOOL,
    ToolPermissions,
    build_pattern_rule,
    build_rule,
)
from ember_code.core.hooks.events import HookEvent
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)

_HITL_LLM_LOGGER = logging.getLogger("ember_code.llm_calls")


async def resolve_hitl_batch(
    backend: "BackendServer",
    decisions: list[msg.HITLDecision],
) -> AsyncIterator[msg.Message]:
    """Resolve every requirement from a multi-req pause in one shot.

    Agno's ``acontinue_run`` denies anything not in the resolved-
    requirements list. The earlier per-req ``resolve_hitl`` loop
    therefore meant: only the first user-approved tool actually
    ran; the rest of an 8-tool batch came back as "User denied"
    and the LLM reported them as REJECTED. This batch method:

    1. Splits each decision between the sub-agent coordinator
       (its own resolve path) and the main team's pending list.
    2. Confirms/rejects every main-team requirement object so
       Agno sees the full resolution set.
    3. Calls ``acontinue_run`` exactly once with all resolved
       reqs, then streams the continuation.

    Sub-agent reqs don't need ``acontinue_run`` — their
    coordinator wakes the spawning stream directly.
    """
    if not decisions:
        return

    main_resolved_reqs: list[Any] = []
    run_id: str | None = None
    for d in decisions:
        # Belt-and-suspenders: even if the sub-agent coordinator
        # claims the requirement, drop any main-team entry under
        # the same id so it can't strand the dict in case of a
        # double-registration bug elsewhere.
        if backend._session.sub_agent_hitl.resolve(d.requirement_id, d.action):
            backend._pending_requirements.pop(d.requirement_id, None)
            continue
        entry = backend._pending_requirements.pop(d.requirement_id, None)
        if entry is None:
            yield msg.Error(text=f"Unknown requirement: {d.requirement_id}")
            continue
        req, this_run_id = entry
        # All reqs from a single RunPaused share a run_id. Reject
        # any cross-pause batch — passing mixed run_ids to
        # ``acontinue_run`` would silently resume the wrong run.
        if run_id is None:
            run_id = this_run_id
        elif this_run_id != run_id:
            yield msg.Error(
                text=(
                    f"Cross-run HITL batch rejected: "
                    f"requirement {d.requirement_id} belongs to run "
                    f"{this_run_id} but batch is for run {run_id}"
                )
            )
            # Put the requirement back so a later batch can resolve
            # it correctly.
            backend._pending_requirements[d.requirement_id] = entry
            continue
        # Isolate per-req failures: one Agno requirement raising
        # on confirm()/reject() must not strand the remaining reqs
        # in the pause. Without this, a single bad req leaves the
        # whole run waiting forever.
        try:
            if d.action == "confirm":
                req.confirm()
            else:
                req.reject(note="User denied")
        except Exception as exc:
            logger.warning(
                "resolve_hitl_batch: requirement %s %s() raised %s; skipping",
                d.requirement_id,
                d.action,
                exc,
            )
            yield msg.Error(text=f"Failed to {d.action} requirement {d.requirement_id}: {exc}")
            continue
        main_resolved_reqs.append(req)
        # Persist the user's decision when they picked a sticky
        # option ("always" / "similar" / "deny"). Web / VSCode /
        # JetBrains clients rely on this — only the TUI has its
        # own save-then-confirm path (which remains an idempotent
        # no-op alongside this). Before this branch shipped,
        # "Always allow" from the web dialog did the exact same
        # thing as "Allow once": confirmed this call, persisted
        # nothing, re-prompted on the next call. See the v0.8.1
        # postmortem.
        with contextlib.suppress(Exception):
            maybe_persist_choice(backend, d, req)

    # Merge in any auto-resolved (plan/acceptEdits/bypass/deny)
    # requirements that were decided in ``_handle_pause`` for the
    # same run. Agno's ``acontinue_run`` denies anything not in
    # the resolved set, so we MUST pass them all in one call.
    # ``getattr`` default keeps tests that build the server via
    # ``__new__`` (skipping ``__init__``) working unchanged.
    auto_bucket = getattr(backend, "_auto_resolved_requirements", None)
    if run_id is not None and auto_bucket is not None:
        stashed = auto_bucket.pop(run_id, [])
        if stashed:
            main_resolved_reqs.extend(stashed)

    if not main_resolved_reqs:
        return  # everything was sub-agent or failed

    team = backend._session.main_team

    _HITL_LLM_LOGGER.info(
        "resolve_hitl_batch: %d req(s) resolved, run_id=%s",
        len(main_resolved_reqs),
        run_id,
    )
    async for proto in backend._stream_with_subagent_hitl(
        team.acontinue_run(
            run_id=run_id,
            session_id=backend._session.session_id,
            requirements=main_resolved_reqs,
            stream=True,
            stream_events=True,
        )
    ):
        yield proto

    # Fire Stop hook after continuation completes.
    stop_result = await backend._session.hook_executor.execute(
        event=HookEvent.STOP.value,
        payload={"session_id": backend._session.session_id},
    )
    if stop_result.message and not stop_result.should_continue:
        yield msg.Info(text=stop_result.message)


async def resolve_hitl(
    backend: "BackendServer",
    requirement_id: str,
    action: str,
    choice: str = "once",
) -> AsyncIterator[msg.Message]:
    """Resolve a single HITL requirement.

    Implemented as a thin shim over ``resolve_hitl_batch`` so the
    dangerous ``acontinue_run(requirements=[req])`` callsite only
    exists in *one* place — the batch method — which always
    passes the *full* set of resolved requirements. This way a
    future caller that hits a multi-req pause via the legacy
    single-req path doesn't silently re-introduce the v0.5.11
    "User denied" cascade. For a 1-req pause this behaves
    identically to the old direct-call implementation.
    """
    decision = msg.HITLDecision(requirement_id=requirement_id, action=action, choice=choice)
    async for proto in resolve_hitl_batch(backend, [decision]):
        yield proto


def check_permission(
    backend: "BackendServer",
    tool_name: str,
    func_name: str,
    tool_args: dict,
) -> str:
    """Check permission level for a tool call. Returns 'allow'/'deny'/'ask'."""
    perms = ToolPermissions(project_dir=backend._session.project_dir)
    registry_name = FUNC_TO_TOOL.get(func_name, tool_name)
    return perms.check(registry_name, func_name, tool_args)


def save_permission_rule(backend: "BackendServer", rule: str, level: str) -> None:
    """Persist a permission rule."""
    perms = ToolPermissions(project_dir=backend._session.project_dir)
    perms.save_rule(rule, level)


def maybe_persist_choice(backend: "BackendServer", decision: Any, req: Any) -> None:
    """Persist the user's HITL choice as a permission rule when
    they picked a sticky option ("always" / "similar" / "deny").

    The FE dialog knows the choice but doesn't compute rule
    strings — that logic lives in the shared ``tool_permissions``
    module so every client stays in lockstep. "once" is a no-op
    (the confirm/reject already happened; no rule to persist).

    Best-effort: silently no-ops on missing tool metadata (older
    Agno versions might not populate ``req.tool_execution``) or
    unsupported choice values. The caller's
    ``contextlib.suppress`` also wraps this; we don't let a bad
    decision blob strand the whole batch.
    """
    choice = str(getattr(decision, "choice", "") or "").lower()
    if choice not in ("always", "similar", "deny"):
        return
    tool_execution = getattr(req, "tool_execution", None)
    tool_name = str(getattr(tool_execution, "tool_name", "") or "")
    tool_args = getattr(tool_execution, "tool_args", None) or {}
    if not tool_name:
        return
    # ``FUNC_TO_TOOL`` maps Agno function names
    # (``run_shell_command``) to our canonical tool names
    # (``Bash``). Persisted rules use the canonical name so they
    # read consistently with what a user would type by hand into
    # settings.json.
    canonical = FUNC_TO_TOOL.get(tool_name, tool_name)
    if choice == "similar":
        rule = build_pattern_rule(canonical, tool_args)
    else:
        rule = build_rule(canonical, tool_args)
    level = "deny" if choice == "deny" else "allow"
    perms = ToolPermissions(project_dir=backend._session.project_dir)
    perms.save_rule(rule, level)

    # Disk-persist alone doesn't stop the next re-prompt: the
    # session's ``PermissionEvaluator`` is built once from
    # ``settings.permissions`` at startup and never re-reads
    # ``settings.local.json``. Without this in-memory patch the
    # next call to the same tool re-enters ``_generate_hitl_
    # requirements`` → ``evaluator.evaluate()`` → still sees an
    # empty ``.allow`` list → DEFER → dialog fires again. Exactly
    # the "I clicked Always allow, still re-prompts" bug.
    evaluator = getattr(backend._session, "permission_evaluator", None)
    if evaluator is not None:
        parsed = PermissionRule.parse(rule)
        if parsed is not None:
            target = evaluator.deny if level == "deny" else evaluator.allow
            if parsed not in target:
                target.append(parsed)
    logger.info("Persisted HITL rule from choice=%s: %s %s", choice, level, rule)
