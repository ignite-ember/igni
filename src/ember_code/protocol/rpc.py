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

    # ── Session / status ──────────────────────────────────────────
    GET_SESSION_ID = "get_session_id"
    GET_RUN_TIMEOUT = "get_run_timeout"
    GET_STATUS = "get_status"
    GET_PROCESSING = "get_processing"
    GET_CHAT_HISTORY = "get_chat_history"
    GET_PENDING_MESSAGES = "get_pending_messages"
    LIST_SESSIONS = "list_sessions"
    SWITCH_SESSION = "switch_session"
    CANCEL_RUN = "cancel_run"
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

    # ── Knowledge ─────────────────────────────────────────────────
    AUTO_SYNC_KNOWLEDGE = "auto_sync_knowledge"

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
    RELOAD_CLOUD_CREDENTIALS = "reload_cloud_credentials"
    CLEAR_CLOUD_CREDENTIALS = "clear_cloud_credentials"
    SWITCH_MODEL = "switch_model"
    GET_MODEL_REGISTRY = "get_model_registry"
    GET_DISPLAY_CONFIG = "get_display_config"
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
    # The directory the session is locked to (tools + shell cwd).
    GET_PROJECT_DIR = "get_project_dir"

    # ── Agents ────────────────────────────────────────────────────
    GET_AGENT_DETAILS = "get_agent_details"
    PROMOTE_EPHEMERAL_AGENT = "promote_ephemeral_agent"
    DISCARD_EPHEMERAL_AGENT = "discard_ephemeral_agent"

    # ── Skills ────────────────────────────────────────────────────
    GET_SKILL_DETAILS = "get_skill_details"

    # ── Knowledge ─────────────────────────────────────────────────
    GET_KNOWLEDGE_STATUS = "get_knowledge_status"
    KNOWLEDGE_SEARCH = "knowledge_search"
    KNOWLEDGE_ADD = "knowledge_add"

    # ── Conversation ──────────────────────────────────────────────
    COUNT_CONTEXT_TOKENS = "count_context_tokens"

    # ── CodeIndex ─────────────────────────────────────────────────
    CODEINDEX_STATUS = "codeindex_status"
    CODEINDEX_SYNC = "codeindex_sync"
    CODEINDEX_RESYNC = "codeindex_resync"
    CODEINDEX_CLEAN = "codeindex_clean"
    CODEINDEX_INSTALL = "codeindex_install"

    # ── Plugins ───────────────────────────────────────────────────
    GET_PLUGIN_DETAILS = "get_plugin_details"
    SET_PLUGIN_ENABLED = "set_plugin_enabled"
    INSTALL_PLUGIN = "install_plugin"
    UPDATE_PLUGIN = "update_plugin"
    REMOVE_PLUGIN = "remove_plugin"
    GET_MARKETPLACES = "get_marketplaces"
    ADD_MARKETPLACE = "add_marketplace"
    REMOVE_MARKETPLACE = "remove_marketplace"
    REFRESH_MARKETPLACES = "refresh_marketplaces"


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
