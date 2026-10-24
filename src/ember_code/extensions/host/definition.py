"""The host object model: every object, property, signal and method.

This is the description ``docs/HOST_OBJECT_MODEL.md`` promises — the
one place the model is written down. The Python SDK
(``packages/igni-extension``) and the TypeScript bridge
(``packages/extension-bridge``) are generated from it by
:mod:`ember_code.extensions.host.codegen`, and
``tests/test_the_host_model_covers_the_app.py`` holds it against every
RPC method, UI action and broadcast igni has.

**Changing this file is changing a promise** (EXTENSIBILITY §8). Bump
:data:`SCHEMA_VERSION`: minor for an added member, major for a changed
or removed one, and regenerate the SDKs (``make host-sdks``).

Types written ``json`` are the host's report of something whose shape
is not part of the promise yet — a plugin's details, an MCP server's
tool list. They are the next members to be typed, and typing one is a
minor version.
"""

from __future__ import annotations

from ember_code.extensions.events import HostTopic
from ember_code.extensions.host.spec import (
    HostSchema,
    ObjectSpec,
    PermissionSpec,
    field,
    method,
    param,
    prop,
    signal,
    typedef,
)

#: The model's version. Semver; see the module docstring.
SCHEMA_VERSION = "1.0.0"

# ── Permissions ───────────────────────────────────────────────────

PERMISSIONS = (
    PermissionSpec(name="app", doc="igni's display settings, version and connection."),
    PermissionSpec(
        name="ui",
        doc="The windows and what is on screen in them: page, sidebar, composer, and so on.",
    ),
    PermissionSpec(name="sessions", doc="The list of sessions, and which one is open."),
    PermissionSpec(name="chat.read", doc="The conversation: messages, runs, status, todos."),
    PermissionSpec(
        name="chat.post",
        doc="Post messages in the chat, labelled as this extension, for the agent to answer.",
    ),
    PermissionSpec(
        name="chat.append",
        doc="Show its own blocks in the transcript (never sent to the model), and see the "
        "chat's interactive views.",
    ),
    PermissionSpec(
        name="chat.control",
        doc="Steer a run: cancel, truncate, compact, approve a plan, drive /loop.",
    ),
    PermissionSpec(name="chat.approvals", doc="See and answer the agent's requests to act."),
    PermissionSpec(name="commands", doc="Slash commands and skills: list and run them."),
    PermissionSpec(name="agents", doc="Agent definitions; promote or discard ephemeral ones."),
    PermissionSpec(name="workflows", doc="List, run and cancel workflows."),
    PermissionSpec(name="scheduler", doc="Scheduled tasks: list, start, run now, cancel."),
    PermissionSpec(name="processes", doc="Background shells: list, tail, stop, run."),
    PermissionSpec(
        name="workspace",
        doc="The project, through the host. Granted by `permissions.workspace` (read or "
        "write), limited to `workspacePaths` — not by a `permissions.host` entry.",
    ),
    PermissionSpec(name="models", doc="The model registry, and which model is in use."),
    PermissionSpec(name="mcp", doc="MCP servers: status, connect, disconnect, tools."),
    PermissionSpec(name="plugins", doc="Plugins and marketplaces: read, install, remove."),
    PermissionSpec(
        name="extensions",
        doc="Every installed extension and its state. Read-only: an extension only ever "
        "changes its own settings.",
    ),
    PermissionSpec(name="hooks", doc="Hook details; reload hooks."),
    PermissionSpec(name="permissions", doc="Permission rules and mode; check and save rules."),
    PermissionSpec(name="policy", doc="The group policy in force."),
    PermissionSpec(
        name="account",
        doc="Whether someone is signed in, and starting or ending the person's own "
        "sign-in. Never the session or any credential.",
    ),
    PermissionSpec(
        name="notifications",
        doc="Every notification, from every source. Without it an extension sees and "
        "marks only its own.",
    ),
)

# ── Value types ───────────────────────────────────────────────────

TYPES = (
    typedef(
        "Change",
        "One change anywhere in the model, as `host.changed` reports it.",
        field("path", "str", "The member that changed, `object.member`."),
        field("value", "json", "Its new value."),
        field("window", "str | None", "The window, for a per-window member.", optional=True),
    ),
    typedef(
        "Display",
        "How igni presents the conversation.",
        field("verbose", "bool", "Whether routing detail is shown."),
        field("output_style", "str", "The active output style; empty for none."),
        field("output_styles", "list[str]", "The styles that can be chosen."),
        field("color_theme", "str", "`auto`, `light` or `dark`."),
        field("show_reasoning", "bool"),
        field("show_tool_calls", "bool"),
    ),
    typedef(
        "Connection",
        "Whether igni can reach a model and its server.",
        field("cloud_connected", "bool"),
        field("model_configured", "bool", "False when runs would reach no model."),
    ),
    typedef(
        "UpdateInfo",
        "A newer igni than the one running.",
        field("current_version", "str"),
        field("latest_version", "str"),
        field("download_url", "str"),
    ),
    typedef(
        "StateChange",
        "One key of an extension's own state.",
        field("key", "str"),
        field("value", "str | None", "`None` when the key was deleted.", optional=True),
    ),
    typedef(
        "WindowInfo",
        "One igni window: a desktop window, an IDE panel or a browser tab.",
        field("id", "str", "Stable across reloads of the same window."),
        field("kind", "str", "`tauri`, `vscode`, `jetbrains` or `web`."),
        field("connected", "bool"),
        field("focused", "bool", "Stage 2: false until the window reports focus."),
        field("session_id", "str | None", "The session it shows.", optional=True),
        field("project_dir", "str | None", optional=True),
    ),
    typedef(
        "Page",
        "One level of a window's page stack.",
        field("kind", "str", "`plugins`, `artifacts`, an extension's page kind, …"),
        field("label", "str"),
        field("params", "dict[str, str]", optional=True),
    ),
    typedef(
        "Route",
        "Where a window is: its page stack, empty for the chat.",
        field("window", "str"),
        field("pages", "list[Page]"),
    ),
    typedef(
        "Panel",
        "The side panel open in a window.",
        field("kind", "str"),
        field("params", "dict[str, json]", optional=True),
    ),
    typedef(
        "PanelChange",
        "A window's side panel opened or closed.",
        field("window", "str"),
        field("panel", "Panel | None", optional=True),
    ),
    typedef(
        "Sidebar",
        "A window's sidebar.",
        field("open", "bool"),
        field("width", "int | None", "Stage 2: not reported yet.", optional=True),
        field("section", "str | None", "Stage 2: not reported yet.", optional=True),
    ),
    typedef(
        "SidebarChange",
        "A window's sidebar changed.",
        field("window", "str"),
        field("sidebar", "Sidebar"),
    ),
    typedef(
        "Preview",
        "The file or document previewed in a window.",
        field("path", "str"),
    ),
    typedef(
        "PreviewChange",
        "A window's preview changed.",
        field("window", "str"),
        field("preview", "Preview | None", optional=True),
    ),
    typedef(
        "SelectionState",
        "What is highlighted or selected in a window.",
        field("message_id", "str | None", optional=True),
        field("text", "str | None", optional=True),
        field("file", "str | None", optional=True),
    ),
    typedef(
        "SelectionChange",
        "A window's selection changed.",
        field("window", "str"),
        field("selection", "SelectionState"),
    ),
    typedef(
        "Composer",
        "A window's composer.",
        field("text", "str", "The draft."),
        field("attachments", "list[str]", "Stage 2: not reported yet.", optional=True),
        field("mode", "str | None", "Stage 2: not reported yet.", optional=True),
    ),
    typedef(
        "ComposerChange",
        "A window's draft changed.",
        field("window", "str"),
        field("composer", "Composer"),
    ),
    typedef(
        "Toast",
        "A transient message on screen.",
        field("id", "str"),
        field("text", "str"),
        field("kind", "str", "`info`, `success`, `warning` or `error`."),
    ),
    typedef(
        "ToastChange",
        "A toast appeared or went away.",
        field("window", "str"),
        field("toast", "Toast"),
    ),
    typedef(
        "Dialog",
        "A dialog on screen.",
        field("id", "str"),
        field("spec", "json"),
    ),
    typedef(
        "DialogChange",
        "A dialog opened or closed.",
        field("window", "str"),
        field("dialog", "Dialog"),
        field("answer", "json", "What the person answered, when it closed.", optional=True),
    ),
    typedef(
        "Scroll",
        "Where a window's transcript is scrolled to.",
        field("message_id", "str | None", optional=True),
        field("at_bottom", "bool"),
    ),
    typedef(
        "SessionInfo",
        "One session, as the session list shows it.",
        field("session_id", "str"),
        field("name", "str"),
        field("created_at", "int"),
        field("updated_at", "int"),
        field("run_count", "int"),
        field("summary", "str"),
        field("agent_name", "str"),
    ),
    typedef(
        "SessionChange",
        "A window moved to another session.",
        field("session_id", "str"),
        field("window", "str | None", optional=True),
    ),
    typedef(
        "SessionList",
        "The session list, as it was just read.",
        field("sessions", "list[SessionInfo]"),
    ),
    typedef(
        "SessionCleared",
        "`/clear`: a session carries on under a new id.",
        field("session_id", "str"),
        field("new_session_id", "str"),
        field("summary", "str"),
    ),
    typedef(
        "SessionNamed",
        "A session was given its name.",
        field("session_id", "str"),
        field("name", "str"),
    ),
    typedef(
        "SessionAttached",
        "A session bound, and the folder it works in.",
        field("session_id", "str"),
        field("project_dir", "str"),
    ),
    typedef(
        "ChatMessage",
        "A message posted in a chat.",
        field("session_id", "str"),
        field("text", "str"),
        field("queued", "bool", "For the next turn rather than now."),
        field(
            "from_extension",
            "str | None",
            "The extension that posted it; `None` for the person.",
            optional=True,
        ),
    ),
    typedef(
        "HistoryChange",
        "Turns were removed from a session's history.",
        field("session_id", "str"),
        field("what", "str", "`truncated` or `discarded`."),
        field("run_id", "str", "The run removed from, or the message discarded."),
    ),
    typedef(
        "ContentDelta",
        "A piece of the agent's answer as it streams.",
        field("session_id", "str"),
        field("text", "str"),
        field("thinking", "bool"),
    ),
    typedef(
        "RunRef",
        "Which run.",
        field("session_id", "str"),
        field("run_id", "str"),
        field("parent_run_id", "str", "Set for a sub-agent's run."),
    ),
    typedef(
        "RunStart",
        "A run started.",
        field("session_id", "str"),
        field("run_id", "str"),
        field("parent_run_id", "str"),
        field("agent_name", "str"),
        field("model", "str"),
    ),
    typedef(
        "Run",
        "A finished run.",
        field("session_id", "str"),
        field("run_id", "str"),
        field("parent_run_id", "str"),
        field("input_tokens", "int"),
        field("output_tokens", "int"),
        field("reasoning_tokens", "int"),
        field("duration", "float", "Seconds."),
        field(
            "changed_files",
            "list[str]",
            "Files the run edited, relative to the project — only those within the "
            "extension's workspace grant, and none without one.",
        ),
    ),
    typedef(
        "RunFailure",
        "A run stopped with an error.",
        field("session_id", "str"),
        field("error", "str"),
    ),
    typedef(
        "Approval",
        "The agent asking to act.",
        field("requirement_id", "str"),
        field("session_id", "str"),
        field("tool_name", "str"),
        field("friendly_name", "str"),
        field("tool_args", "json"),
        field("details", "str"),
        field("agent_path", "list[str]", "Which sub-agent is asking, outermost first."),
    ),
    typedef(
        "RunPause",
        "A run waiting on approvals.",
        field("session_id", "str"),
        field("run_id", "str"),
        field("requirements", "list[Approval]"),
    ),
    typedef(
        "ApprovalResolved",
        "An approval was answered, by anyone.",
        field("session_id", "str"),
        field("requirement_id", "str"),
    ),
    typedef(
        "ApprovalDetails",
        "The reason for an approval, arriving after it.",
        field("session_id", "str"),
        field("requirement_id", "str"),
        field("details", "str"),
    ),
    typedef(
        "ApprovalAnswer",
        "One answer to one approval.",
        field("requirement_id", "str"),
        field("action", "str", "`confirm` or `reject`."),
        field(
            "choice", "str", "`once`, `always` or `similar`; empty with `reject`.", optional=True
        ),
    ),
    typedef(
        "ToolCall",
        "The agent started calling a tool.",
        field("session_id", "str"),
        field("run_id", "str"),
        field("parent_run_id", "str"),
        field("tool_name", "str"),
        field("friendly_name", "str"),
        field("args_summary", "str"),
    ),
    typedef(
        "ToolResult",
        "A tool call finished.",
        field("session_id", "str"),
        field("run_id", "str"),
        field("parent_run_id", "str"),
        field("summary", "str"),
        field("is_error", "bool"),
        field("is_rejected", "bool"),
    ),
    typedef(
        "ToolFailure",
        "A tool call failed.",
        field("session_id", "str"),
        field("run_id", "str"),
        field("parent_run_id", "str"),
        field("error", "str"),
    ),
    typedef(
        "ModelUsage",
        "One model call's tokens.",
        field("session_id", "str"),
        field("run_id", "str"),
        field("parent_run_id", "str"),
        field("input_tokens", "int"),
        field("output_tokens", "int"),
    ),
    typedef(
        "Typing",
        "Someone's draft in a session, live.",
        field("session_id", "str"),
        field("text", "str"),
        field("window", "str | None", optional=True),
    ),
    typedef(
        "Status",
        "A session's status line.",
        field("session_id", "str"),
        field("model", "str"),
        field("model_configured", "bool"),
        field("permission_mode", "str"),
        field("input_tokens", "int"),
        field("output_tokens", "int"),
        field("context_tokens", "int"),
        field("max_context", "int"),
    ),
    typedef(
        "Notice",
        "An info or error line in a session.",
        field("session_id", "str"),
        field("text", "str"),
    ),
    typedef(
        "ContextUsage",
        "How full the context is, and with what.",
        field("tokens", "int"),
        field("max", "int"),
        field("breakdown", "json"),
    ),
    typedef(
        "FileEdited",
        "The agent wrote a file.",
        field("session_id", "str"),
        field("path", "str", "Relative to the project; only within the workspace grant."),
    ),
    typedef(
        "AgentEvent",
        "Progress of a sub-agent or a team, as the transcript shows it.",
        field("session_id", "str"),
        field("kind", "str"),
        field("payload", "json"),
    ),
    typedef(
        "Retry",
        "A network call being retried, and whether it recovered.",
        field("session_id", "str"),
        field("kind", "str", "`attempt` or `succeeded`."),
        field("payload", "json"),
    ),
    typedef(
        "View",
        "A json-render block in the transcript.",
        field("view_id", "str"),
        field("session_id", "str"),
        field("title", "str"),
        field("spec", "json", "A json-render spec: `{root, elements}`."),
        field("source", "str", "`visualizer`, or the extension that appended it."),
    ),
    typedef(
        "ViewAction",
        "The person used a control inside a view.",
        field("session_id", "str"),
        field("action", "str"),
        field("params", "json"),
    ),
    typedef(
        "Plan",
        "The plan the agent proposed in plan mode.",
        field("latest", "str"),
        field("history", "list[str]"),
        field("tasks", "list[json]"),
        field("state", "str", "`pending`, or empty when there is none."),
    ),
    typedef(
        "PlanChange",
        "A plan was submitted or decided.",
        field("session_id", "str"),
        field("kind", "str", "`submitted` or `decided`."),
        field("payload", "json"),
    ),
    typedef(
        "TaskEvent",
        "A sub-agent task created or updated.",
        field("session_id", "str"),
        field("task_id", "str"),
        field("title", "str"),
        field("assignee", "str"),
        field("status", "str"),
    ),
    typedef(
        "TaskIteration",
        "A task loop's iteration count.",
        field("session_id", "str"),
        field("iteration", "int"),
        field("max_iterations", "int"),
    ),
    typedef(
        "TaskSnapshot",
        "Every task, as it now stands.",
        field("session_id", "str"),
        field("tasks", "list[json]"),
    ),
    typedef(
        "Todos",
        "The agent's todo list.",
        field("session_id", "str"),
        field("todos", "list[json]"),
    ),
    typedef(
        "LoopStatus",
        "`/loop`, as it stands.",
        field("active", "bool"),
        field("paused", "bool"),
        field("prompt", "str"),
        field("iteration_index", "int"),
        field("iterations_remaining", "int"),
        field("cap_explicit", "bool"),
    ),
    typedef(
        "CommandOutcome",
        "What a slash command answered.",
        field("kind", "str"),
        field("content", "str"),
        field("display_content", "str"),
    ),
    typedef(
        "CollectionChange",
        "Something in a collection changed; read it again.",
        field("what", "str", "What happened: `connected`, `installed`, `event`, …"),
        field("name", "str | None", "Which item, when there is one.", optional=True),
        field("payload", "json", "The host's report of it.", optional=True),
    ),
    typedef(
        "WorkflowRun",
        "A workflow started.",
        field("workflow_run_id", "str"),
        field("name", "str"),
    ),
    typedef(
        "SchedulerEvent",
        "A scheduled task started, finished or changed.",
        field("kind", "str"),
        field("task_id", "str"),
        field("description", "str"),
        field("result", "str"),
    ),
    typedef(
        "ProcessEvent",
        "A background shell started, printed, or exited.",
        field("kind", "str", "`started`, `line`, `exited` or `done`."),
        field("payload", "json"),
    ),
    typedef(
        "Head",
        "The project's git HEAD.",
        field("commit_sha", "str"),
        field("branch", "str"),
    ),
    typedef(
        "FilesChanged",
        "Files git sees as changed, within the extension's workspace grant.",
        field("paths", "list[str]"),
        field("truncated", "bool", "The list was capped: rescan rather than trust it."),
    ),
    typedef(
        "ModelChange",
        "The model in use changed.",
        field("current", "str"),
    ),
    typedef(
        "ExtensionInfo",
        "An installed extension.",
        field("id", "str"),
        field("name", "str"),
        field("version", "str"),
        field("state", "str", "`PREPARING`, `READY`, `FAILED` or `DISABLED`."),
        field("reason", "str", "Why it is not READY, and what to do."),
        field(
            "web_url",
            "str | None",
            "Its home on the web — its service's URL, which is the service's "
            "`client_uri` — when one is known.",
            optional=True,
        ),
    ),
    typedef(
        "ExtensionStateChange",
        "An extension's health moved.",
        field("extension_id", "str"),
        field("state", "str"),
        field("reason", "str"),
    ),
    typedef(
        "PermissionModeChange",
        "The session's permission mode changed.",
        field("mode", "str"),
        field("previous", "str"),
    ),
    typedef(
        "Account",
        "Who is signed in to igni, without any credential.",
        field("signed_in", "bool"),
        field("email", "str"),
        field("name", "str"),
        field("org", "str"),
    ),
    typedef(
        "NotificationTarget",
        "Where opening a notification goes: one of an extension's pages.",
        field("page", "str"),
        field("params", "dict[str, str]", optional=True),
    ),
    typedef(
        "NotificationEntry",
        "One notification, from igni server or from an extension on this machine.",
        field("id", "str"),
        field("origin", "str", "`server` or `local`."),
        field("source", "str", "The extension it belongs to."),
        field("kind", "str"),
        field("title", "str"),
        field("body", "str"),
        field("actor", "str"),
        field("target", "NotificationTarget | None", optional=True),
        field("created_at", "str | None", "ISO 8601.", optional=True),
        field("read", "bool"),
    ),
    typedef(
        "NotificationCounts",
        "Unread notifications, in all and per extension.",
        field("unread", "int"),
        field("by_source", "dict[str, int]"),
    ),
    typedef(
        "LocalNotification",
        "Something to tell the person at this machine. It never leaves it.",
        field("kind", "str", "The extension's word for what happened."),
        field("title", "str"),
        field("body", "str", "One line.", optional=True),
        field("target", "NotificationTarget", "One of this extension's pages."),
        field("event_key", "str", "The same event is shown once, however often it is sent."),
    ),
    typedef(
        "SettingsChange",
        "An extension's settings, as now configured.",
        field("values", "dict[str, json]"),
    ),
)

# ── Objects ───────────────────────────────────────────────────────

_SESSION_MOVED = "sessions.current_changed"

OBJECTS = (
    ObjectSpec(
        path="",
        doc="The root. `changed` reports every change anywhere in the model, for an "
        "extension that wants to see all of it; each delivery needs the grant of the "
        "member that changed.",
        permission=None,
        signals=(
            signal(
                "changed",
                "Change",
                "Anything changed. Carries the member's path and its new value.",
            ),
        ),
    ),
    ObjectSpec(
        path="app",
        doc="igni itself: how it presents things, which version, whether it is connected.",
        permission="app",
        properties=(
            prop(
                "display",
                "Display",
                "Theme, verbosity, output style.",
                changed_by=["display_changed"],
            ),
            prop("version", "str", "The igni version running.", constant=True),
            prop(
                "update_available",
                "UpdateInfo | None",
                "A newer igni, as last checked; `None` when there is none or it was not "
                "checked yet.",
                changed_by=["update_changed"],
            ),
            prop(
                "connection",
                "Connection",
                "Model and server reachability.",
                changed_by=["connection_changed"],
            ),
        ),
        signals=(
            signal("display_changed", "Display", "Verbosity or output style changed."),
            signal("connection_changed", "Connection", "The connection changed."),
            signal("update_changed", "UpdateInfo | None", "An update check answered."),
        ),
        methods=(
            method(
                "set_verbose", "Show or hide routing detail.", param("on", "bool"), result="Display"
            ),
            method(
                "set_output_style",
                "Switch the output style, as `/output-style <name>` does.",
                param("name", "str"),
                result="Display",
            ),
            method(
                "check_for_update", "Ask whether a newer igni exists.", result="UpdateInfo | None"
            ),
        ),
    ),
    ObjectSpec(
        path="app.state",
        doc="Key–value state of the extension's own, kept by the host across restarts. "
        "An extension sees only its own keys.",
        permission=None,
        properties=(
            prop("values", "dict[str, str]", "Every key and value.", changed_by=["changed"]),
        ),
        signals=(signal("changed", "StateChange", "A key was set or deleted."),),
        methods=(
            method("get", "One value, or `None`.", param("key", "str"), result="str | None"),
            method("set", "Store one value.", param("key", "str"), param("value", "str")),
            method("delete", "Remove one key.", param("key", "str")),
        ),
    ),
    ObjectSpec(
        path="windows",
        doc="Every open igni window. Address one window's UI with `host.windows.ui(id)`.",
        permission="ui",
        properties=(
            prop(
                "list",
                "list[WindowInfo]",
                "Every window connected now.",
                changed_by=["opened", "closed", "focus_changed"],
            ),
            prop(
                "focused",
                "str | None",
                "The window the person is using.",
                changed_by=["focus_changed"],
                stage=2,
            ),
        ),
        signals=(
            signal("opened", "WindowInfo", "A window connected."),
            signal("closed", "WindowInfo", "A window went away."),
            signal("focus_changed", "WindowInfo", "Another window took focus.", stage=2),
        ),
    ),
    ObjectSpec(
        path="ui",
        doc="What a window shows. `host.ui` is the active window — the one the person "
        "last used; `host.windows.ui(id)` any window.",
        permission="ui",
        per_window=True,
        properties=(
            prop(
                "route",
                "list[Page]",
                "The page stack; empty when the chat is showing. Writable: setting it navigates.",
                changed_by=["navigated"],
                writable=True,
            ),
            prop(
                "panel",
                "Panel | None",
                "The side panel open.",
                changed_by=["panel_changed"],
                stage=2,
            ),
            prop(
                "sidebar",
                "Sidebar",
                "The sidebar. Writable: `open` is applied.",
                changed_by=["sidebar_changed"],
                writable=True,
            ),
            prop(
                "preview",
                "Preview | None",
                "The file or document previewed.",
                changed_by=["preview_changed"],
                stage=2,
            ),
            prop(
                "selection",
                "SelectionState",
                "What is selected.",
                changed_by=["selection_changed"],
                stage=2,
            ),
            prop(
                "composer",
                "Composer",
                "The draft in the composer. Writable: the text is applied.",
                changed_by=["composer_changed"],
                writable=True,
            ),
            prop(
                "toasts",
                "list[Toast]",
                "Toasts on screen.",
                changed_by=["toast_shown", "toast_dismissed"],
                stage=2,
            ),
            prop(
                "dialog",
                "Dialog | None",
                "The open dialog.",
                changed_by=["dialog_opened", "dialog_closed"],
                stage=2,
            ),
            prop("scroll", "Scroll", "Where the transcript is.", changed_by=["scrolled"], stage=2),
        ),
        signals=(
            signal("navigated", "Route", "The page stack changed."),
            signal("panel_changed", "PanelChange", "A side panel opened or closed.", stage=2),
            signal("sidebar_changed", "SidebarChange", "The sidebar changed."),
            signal("preview_changed", "PreviewChange", "The preview changed.", stage=2),
            signal("selection_changed", "SelectionChange", "The selection changed.", stage=2),
            signal("composer_changed", "ComposerChange", "The draft changed."),
            signal("toast_shown", "ToastChange", "A toast appeared.", stage=2),
            signal("toast_dismissed", "ToastChange", "A toast went away.", stage=2),
            signal("dialog_opened", "DialogChange", "A dialog opened.", stage=2),
            signal("dialog_closed", "DialogChange", "A dialog closed, with the answer.", stage=2),
            signal("scrolled", "Scroll", "The transcript scrolled.", stage=2),
        ),
        methods=(
            method(
                "navigate",
                "Open a page at the top of the stack, as a header link does.",
                param("page", "str", "A page kind: `plugins`, `artifacts`, an extension's page."),
                param("params", "dict[str, str]", "The page's parameters.", optional=True),
                result="list[Page]",
            ),
            method(
                "back", "Go back one level; at the root, back to the chat.", result="list[Page]"
            ),
            method(
                "open_panel",
                "Open a side panel.",
                param("kind", "str"),
                param("params", "dict[str, json]", optional=True),
                stage=2,
            ),
            method("close_panel", "Close the side panel.", stage=2),
            method(
                "set_sidebar",
                "Open or close the sidebar.",
                param("open", "bool"),
                param("width", "int", "Stage 2.", optional=True),
                param("section", "str", "Stage 2.", optional=True),
                result="Sidebar",
            ),
            method(
                "open_preview",
                "Preview a file. (`preview` is the property.)",
                param("path", "str"),
                stage=2,
            ),
            method(
                "select",
                "Select a message, text or file.",
                param("selection", "SelectionState"),
                stage=2,
            ),
            method(
                "set_composer",
                "Replace the draft.",
                param("text", "str"),
                param("attachments", "list[str]", "Stage 2.", optional=True),
                result="Composer",
            ),
            method(
                "toast",
                "Show a transient message.",
                param("text", "str"),
                param(
                    "kind", "str", "`info` (default), `success`, `warning`, `error`.", optional=True
                ),
                stage=2,
            ),
            method(
                "show_dialog",
                "Show a dialog and wait for the answer. (`dialog` is the property.)",
                param("spec", "json"),
                result="json",
                stage=2,
            ),
            method(
                "scroll_to",
                "Scroll the transcript to a message.",
                param("message_id", "str"),
                stage=2,
            ),
        ),
    ),
    ObjectSpec(
        path="sessions",
        doc="The sessions of this project, and which one the active window shows.",
        permission="sessions",
        properties=(
            prop(
                "list",
                "list[SessionInfo]",
                "Every session, newest first.",
                changed_by=["listed", "cleared", "named"],
            ),
            prop(
                "current",
                "str",
                "The session the active window shows. Writable: setting it switches, "
                "and is refused where the person's switch would be.",
                changed_by=["current_changed"],
                writable=True,
            ),
        ),
        signals=(
            signal(
                "current_changed",
                "SessionChange",
                "A window moved to another session.",
                wire=HostTopic.SESSION_SWITCHED,
            ),
            signal("listed", "SessionList", "The session list was read."),
            signal("cleared", "SessionCleared", "`/clear` renewed a session."),
            signal("named", "SessionNamed", "A session got its name."),
        ),
        methods=(
            method(
                "open",
                "Show a session in the active window.",
                param("session_id", "str"),
                result="SessionChange",
            ),
            method(
                "create",
                "Start a new session, in a folder or in this project.",
                param("project_dir", "str", optional=True),
                result="SessionAttached",
            ),
            method(
                "attach",
                "Make sure a session is live, without showing it.",
                param("session_id", "str"),
                param("project_dir", "str", optional=True),
                result="SessionAttached",
            ),
        ),
    ),
    ObjectSpec(
        path="chat",
        doc="The conversation in the current session. Signals carry the session they "
        "happened in, and arrive for every live session.",
        permission="chat.read",
        properties=(
            prop("session_id", "str", "The current session.", changed_by=[_SESSION_MOVED]),
            prop(
                "messages",
                "list[json]",
                "The transcript, turn by turn.",
                changed_by=["message_added", "run_completed", "history_changed"],
            ),
            prop(
                "pending",
                "list[json]",
                "Messages waiting for a turn.",
                changed_by=["message_added", "run_started"],
            ),
            prop(
                "processing",
                "bool",
                "Whether a run is going.",
                changed_by=["run_started", "streaming_done", "run_error"],
            ),
            prop("status", "Status", "The status line.", changed_by=["status_changed"]),
            prop("timeout", "int", "Seconds a run may take.", changed_by=[_SESSION_MOVED]),
            prop(
                "interrupted_runs",
                "list[json]",
                "Runs that stopped part-way.",
                changed_by=["run_error", "run_started", "history_changed"],
            ),
            prop(
                "context",
                "ContextUsage",
                "Token counts and what fills them.",
                changed_by=["status_changed"],
            ),
        ),
        signals=(
            signal(
                "message_added",
                "ChatMessage",
                "A message was posted — by the person or an extension.",
            ),
            signal("content_delta", "ContentDelta", "The agent's answer, streaming."),
            signal("reasoning_started", "RunRef", "The agent started thinking."),
            signal("run_started", "RunStart", "A run started."),
            signal("run_completed", "Run", "A run finished, with the files it changed."),
            signal("run_error", "RunFailure", "A run stopped with an error."),
            signal("run_paused", "RunPause", "A run is waiting for approvals."),
            signal("streaming_done", "RunRef", "The answer finished streaming."),
            signal("typing", "Typing", "Somebody's draft, live."),
            signal("status_changed", "Status", "The status line changed."),
            signal("info", "Notice", "An info line."),
            signal("error", "Notice", "An error line."),
            signal("tool_started", "ToolCall", "The agent called a tool."),
            signal("tool_completed", "ToolResult", "A tool call finished."),
            signal("tool_error", "ToolFailure", "A tool call failed."),
            signal("model_completed", "ModelUsage", "A model call finished."),
            signal(
                "file_edited",
                "FileEdited",
                "The agent wrote a file.",
                permission="workspace",
                always=False,
            ),
            signal("agent_event", "AgentEvent", "A sub-agent or team moved on."),
            signal("retry", "Retry", "A network call is being retried."),
            signal("history_changed", "HistoryChange", "Turns were truncated or discarded."),
        ),
        methods=(
            method(
                "post",
                "Post a message in the chat, labelled as this extension. The agent "
                "sees it and answers in the chat as usual.",
                param("text", "str"),
                param("attachments", "dict[str, str]", "Path → content.", optional=True),
                permission="chat.post",
            ),
            method(
                "queue",
                "Post a message for the next turn, labelled as this extension.",
                param("text", "str"),
                permission="chat.post",
            ),
            method(
                "append",
                "Show a json-render block in the transcript. Shown to the person, never "
                "sent to the model. Returns its id.",
                param("view", "json", "A json-render spec: `{root, elements}`."),
                param("title", "str", optional=True),
                result="str",
                permission="chat.append",
            ),
            method(
                "replace",
                "Replace a block this extension appended.",
                param("view_id", "str"),
                param("view", "json"),
                param("title", "str", optional=True),
                permission="chat.append",
            ),
            method(
                "search",
                "Search the transcript.",
                param("query", "str"),
                param("limit", "int", "At most this many; 50 by default.", optional=True),
                result="list[json]",
            ),
            method("cancel", "Stop the run.", permission="chat.control"),
            method(
                "cancel_agent",
                "Stop one sub-agent's run.",
                param("run_id", "str"),
                permission="chat.control",
            ),
            method(
                "truncate",
                "Remove a run and everything after it.",
                param("run_id", "str"),
                result="json",
                permission="chat.control",
            ),
            method(
                "compact",
                "Compact the conversation if it needs it, as igni does after a run.",
                result="SessionCleared | None",
                permission="chat.control",
            ),
            method(
                "extract_learnings",
                "Have the agent note what a turn taught it.",
                param("user_msg", "str"),
                param("assistant_msg", "str"),
                permission="chat.control",
            ),
            method(
                "discard_interrupted",
                "Forget an interrupted run.",
                param("message_id", "str"),
                result="json",
                permission="chat.control",
            ),
            method(
                "upload",
                "Upload an attachment for a later `post`.",
                param("filename", "str"),
                param("content_base64", "str"),
                result="json",
                permission="chat.post",
            ),
        ),
    ),
    ObjectSpec(
        path="chat.approvals",
        doc="The agent asking to act, and the answers.",
        permission="chat.approvals",
        properties=(
            prop(
                "waiting",
                "list[Approval]",
                "Requests not yet answered.",
                changed_by=["requested", "resolved"],
            ),
        ),
        signals=(
            signal("requested", "Approval", "The agent asked."),
            signal("resolved", "ApprovalResolved", "Somebody answered."),
            signal("details", "ApprovalDetails", "The reason arrived."),
        ),
        methods=(
            method(
                "answer",
                "Answer one request, as the person's dialog does.",
                param("requirement_id", "str"),
                param("action", "str", "`confirm` or `reject`."),
                param("choice", "str", "`once`, `always` or `similar`.", optional=True),
            ),
            method(
                "answer_all",
                "Answer every request of a pause at once.",
                param("answers", "list[ApprovalAnswer]"),
            ),
        ),
    ),
    ObjectSpec(
        path="chat.views",
        doc="json-render blocks in the transcript — the visualizer's and extensions'.",
        permission="chat.append",
        properties=(
            prop("list", "list[View]", "The views in the current session.", changed_by=["shown"]),
        ),
        signals=(
            signal("shown", "View", "A view appeared or was replaced."),
            signal("action", "ViewAction", "The person used a control in a view."),
        ),
        methods=(
            method(
                "dispatch",
                "Act on a view as if the person had.",
                param("view_id", "str"),
                param("action", "str"),
                param("params", "dict[str, json]", optional=True),
                result="json",
            ),
        ),
    ),
    ObjectSpec(
        path="plan",
        doc="Plan mode: the plan the agent proposed.",
        permission="chat.control",
        properties=(prop("latest", "Plan", "The latest plan.", changed_by=["changed"]),),
        signals=(signal("changed", "PlanChange", "A plan was submitted or decided."),),
        methods=(
            method(
                "approve", "Approve the plan.", param("run_id", "str", optional=True), result="json"
            ),
            method(
                "dismiss",
                "Send the plan back.",
                param("run_id", "str", optional=True),
                result="json",
            ),
        ),
    ),
    ObjectSpec(
        path="tasks",
        doc="The agent's todo list and its sub-agent tasks.",
        permission="chat.read",
        properties=(prop("todos", "list[json]", "The todo list.", changed_by=["todos_changed"]),),
        signals=(
            signal("created", "TaskEvent", "A task was created."),
            signal("updated", "TaskEvent", "A task changed."),
            signal("iteration", "TaskIteration", "A task loop moved on."),
            signal("snapshot", "TaskSnapshot", "Every task, as it now stands."),
            signal("todos_changed", "Todos", "The todo list changed."),
        ),
    ),
    ObjectSpec(
        path="loop",
        doc="`/loop`: a prompt repeated until done.",
        permission="chat.control",
        properties=(prop("status", "LoopStatus", "Where it is.", changed_by=["changed"]),),
        signals=(signal("changed", "LoopStatus", "It paused, resumed, advanced or stopped."),),
        methods=(
            method("pause", "Pause after this iteration.", result="bool"),
            method("resume", "Carry on.", result="str"),
            method("cancel_pending", "Stop the loop.", result="bool"),
            method("pop_pending", "Take the next iteration.", result="json"),
        ),
    ),
    ObjectSpec(
        path="commands",
        doc="Slash commands and skills.",
        permission="commands",
        properties=(
            prop("list", "list[json]", "Every slash command.", changed_by=["plugins.changed"]),
            prop("skills", "list[str]", "Skill names.", changed_by=["plugins.changed"]),
            prop(
                "skill_definitions",
                "list[json]",
                "Each skill's description and prompt.",
                changed_by=["plugins.changed"],
            ),
            prop(
                "skill_details",
                "list[json]",
                "Each skill's details.",
                changed_by=["plugins.changed"],
            ),
        ),
        methods=(
            method(
                "run",
                "Run a slash command, as the extension. The answer comes back here.",
                param("command", "str", "With its slash: `/ctx`."),
                result="CommandOutcome",
            ),
        ),
    ),
    ObjectSpec(
        path="agents",
        doc="Agent definitions.",
        permission="agents",
        properties=(prop("details", "list[json]", "Every agent.", changed_by=["changed"]),),
        signals=(signal("changed", "CollectionChange", "An agent was promoted or discarded."),),
        methods=(
            method(
                "promote_ephemeral",
                "Keep an agent made for one task.",
                param("name", "str"),
                result="json",
            ),
            method(
                "discard_ephemeral",
                "Drop an agent made for one task.",
                param("name", "str"),
                result="json",
            ),
        ),
    ),
    ObjectSpec(
        path="workflows",
        doc="Workflows: scripted runs.",
        permission="workflows",
        properties=(prop("list", "list[json]", "Every workflow.", changed_by=["changed"]),),
        signals=(signal("changed", "CollectionChange", "A workflow run moved on."),),
        methods=(
            method(
                "run",
                "Start a workflow.",
                param("name", "str"),
                param("input", "dict[str, json]", optional=True),
                result="WorkflowRun",
            ),
            method(
                "cancel", "Stop a workflow run.", param("workflow_run_id", "str"), result="bool"
            ),
        ),
    ),
    ObjectSpec(
        path="scheduler",
        doc="Scheduled tasks.",
        permission="scheduler",
        properties=(prop("tasks", "list[json]", "Every scheduled task.", changed_by=["event"]),),
        signals=(signal("event", "SchedulerEvent", "A task started, finished or changed."),),
        methods=(
            method("start", "Start the scheduler."),
            method("run_now", "Run a scheduled task now.", param("task_id", "str"), result="str"),
            method("cancel", "Cancel a scheduled task.", param("task_id", "str")),
        ),
    ),
    ObjectSpec(
        path="processes",
        doc="Background shells.",
        permission="processes",
        properties=(prop("list", "list[json]", "Every background shell.", changed_by=["changed"]),),
        signals=(signal("changed", "ProcessEvent", "A shell started, printed or exited."),),
        methods=(
            method(
                "tail",
                "A shell's last lines.",
                param("pid", "int"),
                param("lines", "int", "200 by default.", optional=True),
                result="json",
            ),
            method("stop", "Stop a shell.", param("pid", "int"), result="json"),
            method(
                "run",
                "Run a command in the project and wait for it.",
                param("command", "str"),
                result="json",
            ),
        ),
    ),
    ObjectSpec(
        path="workspace",
        doc="The project, as far as the extension's workspace grant reaches.",
        permission="workspace",
        properties=(
            prop("root", "str", "The project directory.", changed_by=[_SESSION_MOVED]),
            prop(
                "head",
                "Head | None",
                "Git HEAD; `None` outside a repository.",
                changed_by=["head_changed"],
            ),
        ),
        signals=(
            signal("head_changed", "Head", "HEAD moved.", wire=HostTopic.WORKSPACE_HEAD_CHANGED),
            signal(
                "files_changed",
                "FilesChanged",
                "Files changed, as git sees them, within the grant.",
                wire=HostTopic.WORKSPACE_FILES_CHANGED,
            ),
        ),
        methods=(
            method("read", "Read a file in the project.", param("path", "str"), result="json"),
            method(
                "list",
                "List a directory in the project.",
                param("dir", "str", optional=True),
                result="json",
            ),
            method(
                "complete", "Complete a path, as `@` does.", param("prefix", "str"), result="json"
            ),
            method("search", "Search the code.", param("query", "str"), result="json"),
            method("pick_dir", "Ask the person to pick a folder.", result="json"),
        ),
    ),
    ObjectSpec(
        path="models",
        doc="Models.",
        permission="models",
        properties=(
            prop("registry", "json", "Every model igni knows.", changed_by=["changed"]),
            prop(
                "current",
                "str",
                "The model in use. Writable: setting it switches.",
                changed_by=["changed"],
                writable=True,
            ),
        ),
        signals=(signal("changed", "ModelChange", "The model in use changed."),),
    ),
    ObjectSpec(
        path="mcp",
        doc="MCP servers.",
        permission="mcp",
        properties=(
            prop("servers", "list[json]", "Every server.", changed_by=["changed"]),
            prop("status", "list[json]", "Each server's status.", changed_by=["changed"]),
            prop(
                "details", "list[json]", "Each server's tools and details.", changed_by=["changed"]
            ),
        ),
        signals=(
            signal(
                "changed", "CollectionChange", "A server connected, disconnected or changed tools."
            ),
        ),
        methods=(
            method("connect", "Connect a server.", param("name", "str")),
            method("disconnect", "Disconnect a server.", param("name", "str")),
            method("ensure", "Start the servers that should be running.", result="json"),
            method(
                "set_tool_enabled",
                "Offer or withhold one tool.",
                param("server", "str"),
                param("tool", "str"),
                param("on", "bool"),
                result="json",
            ),
        ),
    ),
    ObjectSpec(
        path="plugins",
        doc="Plugins and marketplaces.",
        permission="plugins",
        properties=(
            prop("installed", "list[json]", "Every installed plugin.", changed_by=["changed"]),
            prop("marketplaces", "list[json]", "Every marketplace.", changed_by=["changed"]),
        ),
        signals=(signal("changed", "CollectionChange", "A plugin or marketplace changed."),),
        methods=(
            method("contents", "What a plugin carries.", param("name", "str"), result="json"),
            method(
                "preview",
                "What a plugin would install.",
                param("source", "str"),
                param("branch", "str", optional=True),
                param("subdir", "str", optional=True),
                result="json",
            ),
            method(
                "install",
                "Install a plugin.",
                param("ref", "str"),
                param("install_ref", "str", optional=True),
            ),
            method(
                "update",
                "Update a plugin.",
                param("name", "str"),
                param("install_ref", "str", optional=True),
            ),
            method("remove", "Remove a plugin.", param("name", "str")),
            method(
                "set_enabled",
                "Enable or disable a plugin.",
                param("name", "str"),
                param("on", "bool"),
            ),
            method("add_marketplace", "Add a marketplace.", param("url", "str")),
            method("remove_marketplace", "Remove a marketplace.", param("name", "str")),
            method(
                "refresh_marketplaces", "Refresh marketplaces.", param("name", "str", optional=True)
            ),
        ),
    ),
    ObjectSpec(
        path="extensions",
        doc="Installed extensions. Read-only, apart from this extension's own settings.",
        permission="extensions",
        properties=(
            prop(
                "installed",
                "list[ExtensionInfo]",
                "Every installed extension.",
                changed_by=["state_changed"],
            ),
        ),
        signals=(
            signal(
                "state_changed",
                "ExtensionStateChange",
                "An extension's health moved.",
                wire=HostTopic.EXTENSION_STATE_CHANGED,
            ),
        ),
        methods=(
            method(
                "set_settings",
                "Change this extension's own settings.",
                param("values", "dict[str, json]"),
                result="SettingsChange",
                always=True,
            ),
        ),
    ),
    ObjectSpec(
        path="hooks",
        doc="Hooks.",
        permission="hooks",
        properties=(prop("details", "list[json]", "Every hook.", changed_by=["changed"]),),
        signals=(signal("changed", "CollectionChange", "Hooks were reloaded."),),
        methods=(
            method("reload", "Reload hooks."),
            method("fire_session_start", "Run the session-start hooks."),
        ),
    ),
    ObjectSpec(
        path="permissions",
        doc="What the agent may do without asking.",
        permission="permissions",
        properties=(
            prop(
                "mode",
                "str",
                "`default`, `plan`, `acceptEdits` or `bypassPermissions`.",
                changed_by=["mode_changed"],
            ),
        ),
        signals=(signal("mode_changed", "PermissionModeChange", "The mode changed."),),
        methods=(
            method(
                "check",
                "Would this tool call be allowed, asked or denied?",
                param("tool_name", "str"),
                param("func_name", "str"),
                param("tool_args", "dict[str, json]", optional=True),
                result="json",
            ),
            method(
                "save_rule",
                "Save a permission rule.",
                param("rule", "str"),
                param("level", "str", "`allow`, `ask` or `deny`."),
            ),
        ),
    ),
    ObjectSpec(
        path="policy",
        doc="The group policy in force.",
        permission="policy",
        properties=(
            prop("pack", "json", "The group's policy pack.", changed_by=["account.changed"]),
        ),
    ),
    ObjectSpec(
        path="account",
        doc="Whether the person is signed in to igni. Never their session or a credential.",
        permission="account",
        properties=(prop("status", "Account", "Who is signed in.", changed_by=["changed"]),),
        signals=(signal("changed", "Account", "Someone signed in or out."),),
        methods=(
            method("sign_in", "Start the person's own sign-in. Its result is theirs."),
            method("sign_out", "Sign the person out.", result="Account"),
        ),
    ),
    ObjectSpec(
        path="notifications",
        doc="The floor's notifications. Its own always; everyone's with `notifications`.",
        permission="notifications",
        properties=(
            prop(
                "list",
                "list[NotificationEntry]",
                "Newest first.",
                changed_by=["changed"],
                always=True,
            ),
            prop(
                "unread",
                "NotificationCounts",
                "Unread counts.",
                changed_by=["changed"],
                always=True,
            ),
        ),
        signals=(signal("changed", "NotificationCounts", "The counts moved.", always=True),),
        methods=(
            method(
                "notify",
                "Tell the person at this machine something. It never leaves the machine.",
                param("notification", "LocalNotification"),
                always=True,
            ),
            method(
                "mark_read",
                "Mark notifications read: these ids, one source, or all of them.",
                param("ids", "list[str]", optional=True),
                param("source", "str", optional=True),
                result="NotificationCounts",
                always=True,
            ),
        ),
    ),
    ObjectSpec(
        path="self",
        doc="This extension, as the host sees it.",
        permission=None,
        properties=(
            prop("id", "str", "Its manifest id.", constant=True),
            prop(
                "settings",
                "dict[str, json]",
                "Its settings. Writable.",
                changed_by=["settings_changed"],
                writable=True,
            ),
            prop("state", "ExtensionInfo", "Its own entry.", changed_by=["state_changed"]),
            prop(
                "web_url",
                "str | None",
                "Its home on the web: its service's URL, when it has one.",
                changed_by=["settings_changed"],
            ),
        ),
        signals=(
            signal("settings_changed", "SettingsChange", "Its settings changed."),
            signal("state_changed", "ExtensionStateChange", "Its health moved."),
        ),
    ),
)


HOST_SCHEMA = HostSchema(
    version=SCHEMA_VERSION,
    permissions=PERMISSIONS,
    types=TYPES,
    objects=OBJECTS,
)

__all__ = ["HOST_SCHEMA", "SCHEMA_VERSION"]
