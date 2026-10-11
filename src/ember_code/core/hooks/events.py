"""Hook event definitions."""

from enum import Enum


class HookEvent(str, Enum):
    """Events that can trigger hooks."""

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    NOTIFICATION = "Notification"
    # Compaction lifecycle — fired around ``Session.force_compact``
    # (manual ``/compact``) and ``Session.compact_if_needed`` (auto
    # trigger when input tokens approach the model's context
    # window). Payload contract:
    #   PreCompact  → {scope: "manual"|"auto", tokens_before: int}
    #   PostCompact → {scope, tokens_before, tokens_after, summary_chars: int}
    # Use this to drive plugin-managed context-budget strategies
    # (export-before-compact, conditional summarisation, etc.).
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    # Observability for hierarchical rules loading. One fire site:
    # ``RulesSuffixer._emit``, after ``RulesIndex.consume_path``
    # surfaces subdir / path-scoped rules for a path a tool just
    # touched. Payload: ``{source: "rules_index", files: [str],
    # bytes: int}``.
    #
    # ``source`` is a discriminator with one value today. A second,
    # ``"session_init"``, was documented here and in docs/HOOKS.md
    # but never implemented — ``SessionCore._init_project_context``
    # builds the ``RulesIndex`` and returns without firing — so a
    # plugin branching on it only ever saw one arm. Firing it there
    # is a live option; until someone does, this comment and the
    # page must not promise it.
    INSTRUCTIONS_LOADED = "InstructionsLoaded"
    # Scheduler task lifecycle. Fired by
    # ``backend/server_scheduler.py`` (``SchedulerController.
    # _TaskHookBridge``) as the scheduler starts and finishes a due
    # task — cron + one-shot, the surface behind ``/schedule``.
    # Payload (see ``backend/schemas_scheduler.py``):
    #   TaskCreated   → {session_id, task_id, description}
    #   TaskCompleted → {session_id, task_id, description,
    #                    status: "completed"|"error"}
    # ``TaskCreated`` fires at task START, not at scheduling time,
    # so there is no ``scheduled_at`` / ``recurrence`` on it, and
    # ``TaskCompleted`` carries no result / error / duration.
    # Distinct from ``SubagentStart``/``SubagentStop`` which cover
    # within-turn agent dispatch; ``TaskCreated``/``Completed``
    # cover the longer-lived scheduled-execution layer.
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"
    # Paired with ``Stop`` — fires when a run terminates with an
    # unhandled exception or non-recoverable error rather than
    # normal completion. Payload: ``{session_id, error: str,
    # error_type: str}``. Lets crash-reporting plugins react in-band
    # instead of grepping audit logs after the fact.
    STOP_FAILURE = "StopFailure"
    # Permission system events (paired). ``PermissionRequest`` fires
    # when the evaluator returns ``ASK`` — caller surfaces the
    # request to the user / canUseTool callback / UI bridge.
    # ``PermissionDenied`` fires when ``DENY`` wins, regardless of
    # source (deny rule, plan-mode block, headless-mode unmatched).
    # Payload (both):
    #   {session_id, tool_name, tool_args, rule?: str, reason?: str}
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"
