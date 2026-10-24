"""Wire-contract enums for the protocol layer.

Every enum here replaces a previously free-string field on a
:class:`~ember_code.protocol.messages.Message` subclass. All are
``StrEnum`` so Pydantic emits the underlying string on the wire
— existing FE clients keying off literals like ``"markdown"`` /
``"quit"`` are unaffected, but producers gain autocomplete +
typo-checking.

The *ingest-side* enums — the ones parsed from a wire string or a
replayed event-log row — carry a ``UNKNOWN`` safety-valve member
plus a ``_missing_`` override that maps unrecognised strings to it
rather than raising ``ValueError``. That is
:class:`OrchestrationTaskStatus`, :class:`HITLAction`,
:class:`HITLChoice`, :class:`SchedulerEventType`,
:class:`PermissionModeName` and :class:`PushChannel` — critical for
forward-compat when a newer BE emits a value this client doesn't
recognise, and for surviving the persisted event log when replayed
after an upgrade.

:class:`CommandResultKind` and :class:`CommandAction` deliberately
do NOT: a ``CommandResult`` is built in-process by this backend's
own command handlers and never parsed back off the wire here, so an
unlisted value is a producer bug that should fail loudly at
construction, not degrade to ``UNKNOWN`` and render as blank text.

Behaviour lives on the enum itself (``is_terminal`` / ``is_active``
/ ``is_confirm`` / ``is_persistent``) so dispatch sites never
compare against magic strings — the invariant is one method call.
"""

from __future__ import annotations

from enum import StrEnum


class CommandResultKind(StrEnum):
    """How the FE should render a ``CommandResult.content`` payload.

    Was a free-string field so producers and consumers had to agree
    on literals like ``"info"`` / ``"markdown"`` without any
    type-checker help. Wire format stays string-compatible via
    ``StrEnum``.

    * ``MARKDOWN`` — render as rich markdown (default for slash
      commands that emit long-form help text).
    * ``INFO`` — single-line dim chat line (status updates,
      confirmations).
    * ``ERROR`` — single-line red chat line.
    * ``ACTION`` — no content to render directly; the FE
      dispatches off ``action`` to open a panel / picker / etc.
    """

    MARKDOWN = "markdown"
    INFO = "info"
    ERROR = "error"
    ACTION = "action"


class CommandAction(StrEnum):
    """Closed set of actions a ``CommandResult`` can request.

    Was an unconstrained ``str`` field — comparisons in
    ``app.py``/``commands.py`` used string literals like
    ``"quit"``, ``"clear"``, which made typos silent (mismatched
    arm would never fire) and made it hard to know the full
    surface from one place. ``StrEnum`` keeps wire compatibility
    (Pydantic serialises enum values to their strings) while
    giving the dispatch sites a single authoritative list.

    There is deliberately no ``UNKNOWN`` member and no ``_missing_``
    override here (unlike the ingest-side enums — see the module
    docstring). Every ``CommandAction`` is stamped in-process by this
    backend's own command handlers; nothing parses one back off the
    wire on the Python side, so an unlisted value means a producer
    typo and should raise at construction rather than degrade into a
    silent no-op. ``NONE`` (``""``) is the real "no action" value.
    """

    NONE = ""  # default — no action, just render content
    QUIT = "quit"
    CLEAR = "clear"
    FORK = "fork"  # session was duplicated; ``content`` carries new id
    SESSIONS = "sessions"
    MODEL = "model"  # show picker
    MODEL_SWITCHED = "model_switched"  # direct switch happened — refresh bar
    LOGIN = "login"
    LOGOUT = "logout"
    HELP = "help"
    MCP = "mcp"
    PLUGINS = "plugins"
    EXTENSIONS = "extensions"
    AGENTS = "agents"
    SKILLS = "skills"
    ARTIFACTS = "artifacts"
    CODEINDEX = "codeindex"
    HOOKS = "hooks"
    LOOP = "loop"
    SCHEDULE = "schedule"
    WATCHER = "watcher"  # opens the background-process panel
    COMPACT = "compact"
    CTX = "ctx"  # opens the context-breakdown page
    RUN_PROMPT = "run_prompt"


class OrchestrationTaskStatus(StrEnum):
    """Status of one orchestration task inside a team run.

    Named ``OrchestrationTaskStatus`` — *not* ``TaskStatus`` — to
    avoid collision with
    :class:`ember_code.core.scheduler.models.TaskStatus`, which
    tracks a different domain (scheduled cron-style tasks vs.
    within-turn agent orchestration).

    Values match the wire strings emitted by
    :mod:`ember_code.protocol.event_handlers` /
    :class:`ember_code.protocol.messages.TaskSnapshot.from_agno`,
    so this is a drop-in retype of the previous free-string
    ``status`` field on
    :class:`~ember_code.protocol.messages.TaskCreated`,
    :class:`~ember_code.protocol.messages.TaskUpdated`, and
    :class:`~ember_code.protocol.messages.TaskSnapshot`.

    ``UNKNOWN`` catches any value Agno emits that we don't
    recognise; :meth:`_missing_` routes unknown wire strings here
    rather than raising ``ValueError`` on ingest.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> OrchestrationTaskStatus:
        """Route unrecognised wire strings to ``UNKNOWN``.

        Prevents ``TaskSnapshot.from_agno`` (and any event-log
        replay) from raising when Agno emits a new status literal
        we haven't listed. The dispatcher's ``UNKNOWN`` branch is
        the safe forward-compat surface.
        """
        return cls.UNKNOWN

    def is_terminal(self) -> bool:
        """``True`` when this status means the task has stopped
        making progress (completed / failed / cancelled).

        Behaviour on the enum so dispatch sites don't repeat the
        ``status in {"completed", "failed", "cancelled"}``
        comparison.
        """
        return self in (self.COMPLETED, self.FAILED, self.CANCELLED)

    def is_active(self) -> bool:
        """``True`` when the task is still in flight (pending /
        running). Inverse of :meth:`is_terminal` modulo
        ``UNKNOWN`` (which is treated as neither — the safe
        default)."""
        return self in (self.PENDING, self.RUNNING)


class HITLAction(StrEnum):
    """Domain enum for the user's HITL decision — confirm or reject.

    Was a free-string field ``msg.HITLResponse.action`` /
    ``msg.HITLDecision.action`` with a ``"confirm" | "reject"``
    comment. ``StrEnum`` keeps wire compatibility while giving
    the dispatch sites a real type.

    See :class:`~ember_code.backend.schemas_hitl.HitlAction` for
    the domain-internal twin used by
    :class:`~ember_code.backend.hitl_controller.AgnoDecisionApplier`;
    they carry the same string values, so
    ``HitlAction(msg.HITLDecision.action)`` still works whether
    the wire arm is a ``str`` or the new ``HITLAction`` member.
    """

    CONFIRM = "confirm"
    REJECT = "reject"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> HITLAction:
        return cls.UNKNOWN

    def is_confirm(self) -> bool:
        """``True`` when the user approved. Behaviour on the enum
        so callers don't repeat ``action == "confirm"``."""
        return self is self.CONFIRM


class HITLChoice(StrEnum):
    """Domain enum for the persistence-scope of a HITL decision.

    Was a free-string field ``msg.HITLResponse.choice`` /
    ``msg.HITLDecision.choice`` with a ``"once" | "always" |
    "similar"`` comment.

    * ``ONCE`` — decision applies to this invocation only; no
      permission rule persisted.
    * ``ALWAYS`` — persist an exact rule (same tool + args).
    * ``SIMILAR`` — persist a pattern rule (matches similar args).
    * ``DENY`` — user actively denied and wants to persist a deny
      rule.
    """

    ONCE = "once"
    ALWAYS = "always"
    SIMILAR = "similar"
    DENY = "deny"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> HITLChoice:
        return cls.UNKNOWN

    def is_persistent(self) -> bool:
        """``True`` when this choice should be saved as a
        permission rule.

        ``ONCE`` returns ``False``; everything else (always /
        similar / deny) is persistent. Callers use this instead
        of comparing against ``"once"``."""
        return self in (self.ALWAYS, self.SIMILAR, self.DENY)


class SchedulerEventType(StrEnum):
    """Notification kind for a scheduled task.

    Was a free-string ``event_type`` field on
    :class:`~ember_code.protocol.messages.SchedulerEvent` with a
    ``"started" | "completed" | "failed"`` comment. Wire values
    match the previous string literals.

    ``ERROR`` is retained as an alias for the older ``"error"``
    literal that
    :meth:`ember_code.backend.server_scheduler.SchedulerController._TaskHookBridge.on_completed`
    used to emit on failure — kept so a mixed cluster (old FE +
    new BE) doesn't drop the event.
    """

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"  # legacy alias for FAILED, still emitted by some paths
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> SchedulerEventType:
        return cls.UNKNOWN

    def is_terminal(self) -> bool:
        """``True`` when the event marks the task finished
        (completed or failed / error)."""
        return self in (self.COMPLETED, self.FAILED, self.ERROR)


class PermissionModeName(StrEnum):
    """Mirror of :class:`~ember_code.core.config.permission_eval.PermissionMode`
    for the wire layer.

    Kept string-compatible with the domain enum so
    :attr:`~ember_code.protocol.messages.StatusUpdate.permission_mode`
    can be typed here without importing the domain layer into
    the protocol leaf module. The two enums MUST stay in sync;
    :mod:`tests.protocol.test_permission_mode_mirror` asserts
    that at import time.
    """

    DEFAULT = "default"
    DONT_ASK = "dontAsk"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    PLAN = "plan"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> PermissionModeName:
        return cls.UNKNOWN


class PushChannel(StrEnum):
    """The known channels a
    :class:`~ember_code.protocol.messages.PushNotification` can
    carry — a *catalogue*, not a closed set.

    Was the free-string ``channel`` field with a
    ``"scheduler_event", "orchestrate_progress", "login_status"``
    comment that didn't match every producer callsite (the actual
    channels emitted include ``scheduler_started`` /
    ``scheduler_completed`` / ``login_result`` /
    ``session_named`` / ``permission_mode_changed`` /
    ``file_edited`` / ``process_started`` / etc.).

    The producer set is genuinely open, and this docstring used to
    deny it: besides the :func:`~ember_code.protocol.schemas.push`
    factories, ``push_bridge`` forwards raw channel strings and
    bridges the whole session broadcast bus into pushes, and
    :class:`~ember_code.core.broadcast_schema.BroadcastEvent`
    documents its ``channel`` as producer-open so a new tool can
    introduce one without a schema change. Claiming closure here
    bought nothing and went stale — six live channels
    (``workflow_event``, ``todos_updated``, ``plan_decided``,
    ``http_retry_attempt``, ``http_retry_succeeded``,
    ``visualization_action_dispatched``) were missing from the
    "authoritative" list while the FE routed on all of them.

    So: ``PushNotification.channel`` stays a raw ``str`` on the wire,
    ``_missing_`` maps anything unlisted to ``UNKNOWN``, and this
    enum's job is to give the producer sites we *do* control a
    symbol to reference. Add a member when you add a channel; the
    wire keeps working either way.
    """

    SCHEDULER_STARTED = "scheduler_started"
    SCHEDULER_COMPLETED = "scheduler_completed"
    SCHEDULER_EVENT = "scheduler_event"
    ORCHESTRATE_PROGRESS = "orchestrate_progress"
    ORCHESTRATE_EVENT = "orchestrate_event"
    LOGIN_STATUS = "login_status"
    LOGIN_RESULT = "login_result"
    SESSION_NAMED = "session_named"
    PERMISSION_MODE_CHANGED = "permission_mode_changed"
    OUTPUT_STYLE_CHANGED = "output_style_changed"
    PLAN_SUBMITTED = "plan_submitted"
    FILE_EDITED = "file_edited"
    #: One extension's event, on its way to that extension's frame.
    #: One channel for all of them rather than one each: the
    #: audience was already decided by
    #: :class:`~ember_code.extensions.events.EventRouter`, and the
    #: payload names which extension it is for.
    EXTENSION_EVENT = "extension_event"
    #: The floor's unread counts moved — igni server's notifications,
    #: an extension's local notice, or something marked read. Every
    #: badge reads this; the list is fetched again only by whoever is
    #: showing it.
    NOTIFICATIONS_CHANGED = "notifications_changed"
    PROCESS_STARTED = "process_started"
    PROCESS_LINE = "process_line"
    PROCESS_EXITED = "process_exited"
    BACKGROUND_PROCESS_DONE = "background_process_done"
    # Bridged from the session broadcast bus / workflow runner rather
    # than a ``push_*`` factory — which is exactly why they were
    # missing while the FE routed on every one of them.
    WORKFLOW_EVENT = "workflow_event"  # backend/workflow_runner.py
    TODOS_UPDATED = "todos_updated"  # core/tools/todo.py
    PLAN_DECIDED = "plan_decided"  # core/session/plan_ops.py
    HTTP_RETRY_ATTEMPT = "http_retry_attempt"  # core/tools/http_fetcher.py
    HTTP_RETRY_SUCCEEDED = "http_retry_succeeded"  # core/tools/http_fetcher.py
    VISUALIZATION_ACTION_DISPATCHED = (
        "visualization_action_dispatched"  # backend/visualization_action_bus.py
    )
    #: A host-model signal for an extension's page: the window posts
    #: it into that extension's frames. See
    #: :mod:`ember_code.extensions.host`.
    HOST_SIGNAL = "host_signal"
    #: A window's client state was written by somebody other than the
    #: window — an extension through ``host.ui`` — so the window
    #: applies it. ``{client_id, key, value}``; an empty value deletes.
    CLIENT_STATE_CHANGED = "client_state_changed"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> PushChannel:
        return cls.UNKNOWN


__all__ = [
    "CommandResultKind",
    "CommandAction",
    "OrchestrationTaskStatus",
    "HITLAction",
    "HITLChoice",
    "SchedulerEventType",
    "PermissionModeName",
    "PushChannel",
]
