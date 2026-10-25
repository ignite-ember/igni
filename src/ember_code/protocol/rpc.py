"""RPC method registry — the wire contract between the TUI client
and the backend server.

Why this exists
---------------

The frontend's :meth:`BackendClient._rpc` sends a string method name
to the backend, which dispatches via a lookup table at startup. The
string is the wire format. Using bare string literals worked while
the surface was small, but:

- Typos surface only at runtime — a misspelled call goes through the
  socket and the backend replies with ``Unknown RPC method``, which
  the client either swallows or surfaces as a one-line error far from
  the typo.
- Renaming a method on the backend silently breaks the client. A
  refactor that misses one call site is invisible until that code path
  fires (often in production, on a user's machine).
- The dispatch table and the client implementations are two
  independent string lists; nothing enforces they stay in sync.

Enums fix all three. Both sides import the same :class:`RpcMethod`
enum; the wire value is the enum member's string value, so the
protocol stays identical (no version bump needed). The dispatch
table's keys and the client's call sites become symbol references —
typos fail at import time, renames bubble through to every reference.

Adding a new RPC
----------------

1. Add an enum member here. The value is the canonical wire name and
   must match the backend method.
2. Implement the method on :class:`BackendServer` (or wherever the
   dispatch lambda routes).
3. Register a dispatch entry in :mod:`ember_code.backend.__main__`'s
   ``_build_rpc_table``, keyed by the new enum member.
4. Add the client-side wrapper in :class:`BackendClient` that calls
   ``self._rpc(RpcMethod.X, ...)``.

The ``validate_rpc_table`` helper at the bottom checks that every
enum value is covered by the dispatch table at startup, so an
incomplete registration fails fast rather than at first call.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class RpcMethod(StrEnum):
    """Wire-protocol method names for the FE↔BE RPC.

    StrEnum so the value IS the string used on the wire — no
    ``.value`` access needed at call sites (both ``RpcMethod.LOGIN``
    and ``"login"`` compare equal as dict keys / equality checks).
    """

    # ── MCP ───────────────────────────────────────────────────────
    ENSURE_MCP = "ensure_mcp"
    MCP_CONNECT = "mcp_connect"
    MCP_DISCONNECT = "mcp_disconnect"
    GET_MCP_STATUS = "get_mcp_status"
    GET_MCP_SERVERS = "get_mcp_servers"
    GET_MCP_SERVER_DETAILS = "get_mcp_server_details"
    SET_MCP_TOOL_ENABLED = "set_mcp_tool_enabled"

    # ── Session / status ──────────────────────────────────────────
    GET_SESSION_ID = "get_session_id"
    GET_RUN_TIMEOUT = "get_run_timeout"
    GET_STATUS = "get_status"
    GET_PROCESSING = "get_processing"
    GET_CHAT_HISTORY = "get_chat_history"
    SEARCH_CHAT = "search_chat"
    GET_PENDING_MESSAGES = "get_pending_messages"
    # Explicit interrupted-run record (durable; survives restart).
    # The FE renders these as banners with Retry/Discard/Edit-prompt
    # actions on the assistant bubble that was in-flight when the
    # run stopped.
    GET_INTERRUPTED_RUNS = "get_interrupted_runs"
    # Hard-delete a single interrupted run — used by the FE's
    # "Discard" action.
    DISCARD_INTERRUPTED_RUN = "discard_interrupted_run"
    LIST_SESSIONS = "list_sessions"
    SWITCH_SESSION = "switch_session"
    CANCEL_RUN = "cancel_run"
    CANCEL_AGENT_RUN = "cancel_agent_run"
    SHUTDOWN = "shutdown"

    # ── Compaction / learning ────────────────────────────────────
    COMPACT_IF_NEEDED = "compact_if_needed"
    EXTRACT_LEARNINGS = "extract_learnings"

    # ── /loop continuation ───────────────────────────────────────
    POP_PENDING_LOOP_ITERATION = "pop_pending_loop_iteration"
    CANCEL_PENDING_LOOP = "cancel_pending_loop"
    LOOP_STATUS = "loop_status"
    LOOP_RESUME = "loop_resume"
    LOOP_PAUSE = "loop_pause"

    # ── Skills ────────────────────────────────────────────────────
    GET_SKILL_NAMES = "get_skill_names"
    GET_SKILL_DEFINITIONS = "get_skill_definitions"

    # ── Slash commands (SDK enumeration) ─────────────────────────
    # Mirrors Claude Code's SDK ``slash_commands`` field. Returns
    # built-ins, markdown-authored commands, and user-invocable
    # skills in one snapshot so IDE plugins / completion UIs can
    # paint the full surface without inferring it.
    GET_SLASH_COMMANDS = "get_slash_commands"

    # ── Todo list (CC ``TodoWrite`` parity) ──────────────────────
    # Snapshot of the per-session todo list maintained by the
    # agent's ``todo_write`` tool. The UI polls or refreshes
    # after every turn — there's no push channel yet.
    GET_TODOS = "get_todos"

    # ── Visualization actions (json-render round-trip) ───────────
    # User interacted with a component inside a ``<JsonRenderView>``
    # card (e.g. clicked a Button, changed a Select). The FE
    # forwards the action name + params here so agents can react.
    # BE stashes the last N events into ``session._visualization_actions``
    # and pushes ``visualization_action_dispatched`` so any
    # listener (or a future agent tool that polls for actions) can
    # observe them. Returns ``{ok: bool, action, params}``.
    DISPATCH_VISUALIZATION_ACTION = "dispatch_visualization_action"

    # ── Background-process watcher panel ─────────────────────────
    # The FE's right-side watcher panel surfaces every
    # backgrounded ``run_shell_command`` for view-only tail +
    # explicit stop. Push channels ``process_started`` /
    # ``process_line`` / ``process_exited`` deliver live updates
    # so the panel doesn't poll.
    LIST_BACKGROUND_PROCESSES = "list_background_processes"
    READ_PROCESS_TAIL = "read_process_tail"
    STOP_BACKGROUND_PROCESS = "stop_background_process"

    # ── Plan mode (CC row 50) ────────────────────────────────────
    # Latest plan the agent submitted via ``exit_plan_mode``.
    # Returns ``{latest: str, history: list[str]}``. Empty when
    # the agent hasn't called the tool yet.
    GET_LATEST_PLAN = "get_latest_plan"
    # User clicked Approve on a PlanCard. Persists the decision
    # keyed by the originating run_id, flips permission mode →
    # default, broadcasts ``plan_decided``. Returns
    # ``{run_id, decision, mode_status}``.
    APPROVE_PLAN = "approve_plan"
    # User clicked Refine. Persists the dismissal but keeps the
    # session in plan mode so the user can iterate. Same return
    # shape as APPROVE_PLAN with empty ``mode_status``.
    DISMISS_PLAN = "dismiss_plan"

    # ── Output styles (CC row 52) ────────────────────────────────
    # Discovered output styles + the active one. Returns
    # ``{active: str, styles: [{name, description}, ...]}``.
    # FE uses this to render a picker / status chip.
    GET_OUTPUT_STYLES = "get_output_styles"

    # ── Attachments (drag/drop, paste, OS picker) ────────────────
    UPLOAD_ATTACHMENT = "upload_attachment"

    # ── Hooks ─────────────────────────────────────────────────────
    FIRE_SESSION_START_HOOK = "fire_session_start_hook"
    GET_HOOKS_DETAILS = "get_hooks_details"
    RELOAD_HOOKS = "reload_hooks"

    # ── Scheduler ────────────────────────────────────────────────
    START_SCHEDULER = "start_scheduler"
    EXECUTE_SCHEDULED_TASK = "execute_scheduled_task"
    CANCEL_SCHEDULED_TASK = "cancel_scheduled_task"
    GET_SCHEDULED_TASKS = "get_scheduled_tasks"

    # ── Auth / models / config ───────────────────────────────────
    LOGIN = "login"
    # Where this address's organisation runs igni, read from a record
    # the organisation publishes on its own domain.
    DISCOVER_SERVER = "discover_server"
    # Write that server down, so the next launch knows it.
    SET_SERVER = "set_server"
    RELOAD_CLOUD_CREDENTIALS = "reload_cloud_credentials"
    CLEAR_CLOUD_CREDENTIALS = "clear_cloud_credentials"
    SWITCH_MODEL = "switch_model"
    GET_MODEL_REGISTRY = "get_model_registry"
    GET_DISPLAY_CONFIG = "get_display_config"
    GET_GROUP_POLICY = "get_group_policy"
    TOGGLE_VERBOSE = "toggle_verbose"

    # ── Permissions ──────────────────────────────────────────────
    CHECK_PERMISSION = "check_permission"
    SAVE_PERMISSION_RULE = "save_permission_rule"

    # ── Misc ──────────────────────────────────────────────────────
    CHECK_FOR_UPDATE = "check_for_update"

    # ── GUI-client parity (TUI does these FE-side) ────────────────
    # @-mention file completions — webviews can't touch the FS, so
    # the FileIndex runs in the BE for them.
    COMPLETE_FILES = "complete_files"
    # $-prefix shell mode — the TUI spawns the shell in its own
    # process; GUI shells route it through the BE (same machine,
    # same user, the SESSION's project dir as cwd).
    RUN_SHELL = "run_shell"
    # Bind/create a session, optionally in a specific project
    # directory. Handled at the session-pool level (backend/__main__
    # dispatch), NOT by per-runtime tables — the per-runtime entry is
    # a guard stub.
    ATTACH_SESSION = "attach_session"
    # Directory listing for the GUI folder browser (picking a
    # project dir for a new session). Webviews can't touch the FS.
    LIST_DIRS = "list_dirs"
    # Open the OS-native folder dialog ON THE BE's machine (same
    # machine — loopback transport) and return the chosen path.
    # Lets even plain browser tabs use the real OS picker; the
    # in-app browser is the last-resort fallback (headless Linux).
    PICK_DIR_NATIVE = "pick_dir_native"
    # The directory the session is locked to (tools + shell cwd).
    GET_PROJECT_DIR = "get_project_dir"

    # ── Agents ────────────────────────────────────────────────────
    GET_AGENT_DETAILS = "get_agent_details"
    PROMOTE_EPHEMERAL_AGENT = "promote_ephemeral_agent"
    DISCARD_EPHEMERAL_AGENT = "discard_ephemeral_agent"

    # ── Skills ────────────────────────────────────────────────────
    GET_SKILL_DETAILS = "get_skill_details"

    # ── File access (sandboxed to project/.igni) ────────────────
    READ_FILE = "read_file"
    TRUNCATE_HISTORY = "truncate_history"
    SEARCH_CODE = "search_code"

    # ── Conversation ──────────────────────────────────────────────
    COUNT_CONTEXT_TOKENS = "count_context_tokens"
    #: The ``/ctx`` page's numbers. ``count_context_tokens`` answers
    #: "how big", this answers "made of what" — the split ``/compact``
    #: can shrink versus the floor it cannot.
    GET_CONTEXT_BREAKDOWN = "get_context_breakdown"

    # ── Workflows (CC ``/workflows`` parity) ───────────────────────
    # The BE acts as a runner for ``.claude/workflows/*.mjs``
    # scripts — the same ones ClaudeCode drives. Discovery returns
    # the meta block of every workflow; ``run_workflow`` spawns
    # one, returns the ``workflow_run_id`` immediately, and
    # streams progress on the ``workflow_event`` push channel.
    # ``cancel_workflow`` stops a running subprocess cleanly
    # (writes a cancel message, escalates to SIGTERM after a
    # grace period).
    LIST_WORKFLOWS = "list_workflows"
    RUN_WORKFLOW = "run_workflow"
    CANCEL_WORKFLOW = "cancel_workflow"

    # ── Plugins ───────────────────────────────────────────────────
    GET_PLUGIN_DETAILS = "get_plugin_details"
    GET_PLUGIN_CONTENTS = "get_plugin_contents"
    PREVIEW_PLUGIN = "preview_plugin"
    SET_PLUGIN_ENABLED = "set_plugin_enabled"
    INSTALL_PLUGIN = "install_plugin"
    UPDATE_PLUGIN = "update_plugin"
    REMOVE_PLUGIN = "remove_plugin"
    GET_MARKETPLACES = "get_marketplaces"
    ADD_MARKETPLACE = "add_marketplace"
    REMOVE_MARKETPLACE = "remove_marketplace"
    REFRESH_MARKETPLACES = "refresh_marketplaces"

    # ── Extensions ────────────────────────────────────────────────
    #
    # Only the *base* side of the extension surface lives in this
    # enum. An extension's own methods are ``ext.<id>.<method>`` and
    # are deliberately not members: they arrive with a manifest
    # rather than at import time, and making them members would give
    # up the exhaustiveness guarantee this enum exists for. They are
    # routed by prefix instead — see
    # :mod:`ember_code.extensions.rpc`.
    LIST_EXTENSIONS = "list_extensions"
    SET_EXTENSION_SETTINGS = "set_extension_settings"

    # ── Notifications ─────────────────────────────────────────────
    #
    # The floor's, not any extension's (igni server's
    # ``docs/service-identity.md`` §10): what igni server delivered to
    # whoever is signed in, merged with what extensions on this
    # machine told the host. See :mod:`ember_code.notifications`.
    NOTIFICATIONS_LIST = "notifications_list"
    NOTIFICATIONS_COUNTS = "notifications_counts"
    NOTIFICATIONS_MARK_READ = "notifications_mark_read"

    # ── The host object model ─────────────────────────────────────
    #
    # How an extension's *page* reaches ``docs/HOST_OBJECT_MODEL.md``:
    # the bridge forwards ``bridge.host.…`` here, with the frame's
    # extension as ``caller``, and the host answers from the same
    # :class:`~ember_code.extensions.host.model.HostModel` the
    # extension's process reaches over its loopback socket. Pool-level,
    # like ``attach_session``: the model spans every session.
    HOST_MODEL = "host_model"
    # How a *window* reaches its own part of the host model: it hydrates
    # its model, and writes every change its views make, here — the
    # same :class:`~ember_code.extensions.host.windows.Windows` an
    # extension's ``host.ui`` writes. Pool-level: a window spans
    # sessions. See ``clients/web/src/clientState.ts``.
    WINDOW_MODEL = "window_model"
    # An extension's *page* asking for a token for its own service
    # (``bridge.service``), with the frame's extension as ``caller``.
    # The audience is the service recorded on that extension's UI
    # origin, never an argument — see
    # :class:`~ember_code.extensions.service_token.PageServiceTokens`.
    # Pool-level: a token is the person's, not a session's.
    EXTENSION_SERVICE_TOKEN = "extension_service_token"


def validate_rpc_table(registered_keys: Iterable[str]) -> None:
    """Raise if any :class:`RpcMethod` member is missing from the
    backend's dispatch table.

    Called once at backend startup (after ``_build_rpc_table`` returns).
    Catches the "added enum member, forgot to register handler" mistake
    immediately instead of at first call from the client. Equivalent
    in spirit to mypy's ``assert_never`` for exhaustive matching.
    """
    registered = set(registered_keys)
    missing = {m.value for m in RpcMethod} - registered
    if missing:
        raise RuntimeError(
            "RPC dispatch table is missing handlers for "
            f"{sorted(missing)!r}. Every RpcMethod enum value must "
            "have a corresponding entry in `_build_rpc_table` "
            "(backend/__main__.py)."
        )
