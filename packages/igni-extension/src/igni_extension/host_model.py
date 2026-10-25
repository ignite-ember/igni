"""GENERATED from the igni host schema 1.5.0 by ember_code.extensions.host.codegen — do not edit. Run `make host-sdks` after changing the schema.

`ext.host` as an extension's process sees it. Read
`docs/HOST_OBJECT_MODEL.md` in igni for what each object is.
"""

# ruff: noqa: E501

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from igni_extension.intercepts import Intercept
from igni_extension.members import (
    NO_RESULT,
    HostLink,
    HostObject,
    JsonValue,
    Property,
    Signal,
)
from igni_extension.notifications import LocalNotification, Target

SCHEMA_VERSION = "1.5.0"

NotificationTarget = Target

class Change(BaseModel):
    """One change anywhere in the model, as `host.changed` reports it."""

    model_config = ConfigDict(extra="allow")

    path: str
    """The member that changed, `object.member`."""
    value: JsonValue
    """Its new value."""
    window: str | None = None
    """The window, for a per-window member."""


class Display(BaseModel):
    """How igni presents the conversation."""

    model_config = ConfigDict(extra="allow")

    verbose: bool
    """Whether routing detail is shown."""
    output_style: str
    """The active output style; empty for none."""
    output_styles: list[str]
    """The styles that can be chosen."""
    color_theme: str
    """`auto`, `light` or `dark`."""
    show_reasoning: bool
    show_tool_calls: bool


class Connection(BaseModel):
    """Whether igni can reach a model and its server."""

    model_config = ConfigDict(extra="allow")

    cloud_connected: bool
    model_configured: bool
    """False when runs would reach no model."""


class UpdateInfo(BaseModel):
    """A newer igni than the one running."""

    model_config = ConfigDict(extra="allow")

    current_version: str
    latest_version: str
    download_url: str


class StateChange(BaseModel):
    """One key of an extension's own state."""

    model_config = ConfigDict(extra="allow")

    key: str
    value: str | None = None
    """`None` when the key was deleted."""


class WindowInfo(BaseModel):
    """One igni window: a desktop window, an IDE panel or a browser tab."""

    model_config = ConfigDict(extra="allow")

    id: str
    """Stable across reloads of the same window."""
    kind: str
    """`tauri`, `vscode`, `jetbrains` or `web`."""
    connected: bool
    focused: bool
    """Whether the person is using this window: it has focus."""
    session_id: str | None = None
    """The session it shows."""
    project_dir: str | None = None


class Page(BaseModel):
    """One level of a window's page stack."""

    model_config = ConfigDict(extra="allow")

    kind: str
    """`plugins`, `extensions`, an extension's page kind, …"""
    label: str
    params: dict[str, str] = Field(default_factory=dict)


class Route(BaseModel):
    """Where a window is: its page stack, empty for the chat."""

    model_config = ConfigDict(extra="allow")

    window: str
    pages: list[Page]


class Panel(BaseModel):
    """The panel open over a window's chat: `notifications`, `search` (find in the conversation), `login` or `dir-picker`. One at a time."""

    model_config = ConfigDict(extra="allow")

    kind: str
    params: dict[str, JsonValue] = Field(default_factory=dict)


class PanelChange(BaseModel):
    """A window's panel opened or closed."""

    model_config = ConfigDict(extra="allow")

    window: str
    panel: Panel | None = None


class Sidebar(BaseModel):
    """A window's sidebar."""

    model_config = ConfigDict(extra="allow")

    open: bool
    width: int | None = None
    """Its width in pixels; `None` for igni's own."""
    section: str | None = None
    """The part of it in view: `links` (the destinations) or `sessions`."""


class SidebarChange(BaseModel):
    """A window's sidebar changed."""

    model_config = ConfigDict(extra="allow")

    window: str
    sidebar: Sidebar


class Preview(BaseModel):
    """The file previewed in a window."""

    model_config = ConfigDict(extra="allow")

    path: str


class PreviewChange(BaseModel):
    """A window's preview changed."""

    model_config = ConfigDict(extra="allow")

    window: str
    preview: Preview | None = None


class SelectionState(BaseModel):
    """What is selected in a window. Each part is independent; `None` for nothing."""

    model_config = ConfigDict(extra="allow")

    message_id: str | None = None
    """The highlighted message, by the run it belongs to (`run_id` in `chat.messages` and the run signals)."""
    text: str | None = None
    """Text selected in the transcript."""
    file: str | None = None
    """The file last picked: a file pill, a reference."""


class SelectionChange(BaseModel):
    """A window's selection changed."""

    model_config = ConfigDict(extra="allow")

    window: str
    selection: SelectionState


class Composer(BaseModel):
    """A window's composer."""

    model_config = ConfigDict(extra="allow")

    text: str
    """The draft, without the mode's `/` or `$`."""
    attachments: list[str] = Field(default_factory=list)
    """The files the draft refers to (`@path`), in order."""
    mode: str | None = None
    """`chat`, `command` (a slash command) or `shell` (a `$` line)."""


class ComposerChange(BaseModel):
    """A window's draft changed."""

    model_config = ConfigDict(extra="allow")

    window: str
    composer: Composer


class Toast(BaseModel):
    """A transient message on screen."""

    model_config = ConfigDict(extra="allow")

    id: str
    text: str
    """Its first line."""
    kind: str
    """`info`, `success`, `warning` or `error`."""
    body: str | None = None
    """A second line."""
    target: Page | None = None
    """Where clicking it goes."""
    source: str | None = None
    """The extension that showed it; `None` for igni."""


class ToastChange(BaseModel):
    """A toast appeared or went away."""

    model_config = ConfigDict(extra="allow")

    window: str
    toast: Toast


class Dialog(BaseModel):
    """A dialog on screen: igni's own (`spec.kind` is `update`) or an extension's (`show_dialog`). One at a time; a new one replaces the last, which closes unanswered."""

    model_config = ConfigDict(extra="allow")

    id: str
    spec: JsonValue
    source: str | None = None
    """The extension that asked; `None` for igni."""


class DialogChange(BaseModel):
    """A dialog opened or closed."""

    model_config = ConfigDict(extra="allow")

    window: str
    dialog: Dialog
    answer: JsonValue = None
    """What the person answered, when it closed: `{button, value}`; `None` when it was dismissed or replaced."""


class Scroll(BaseModel):
    """Where a window's transcript is scrolled to."""

    model_config = ConfigDict(extra="allow")

    message_id: str | None = None
    """The message at the top of the view, by its run; `None` for none."""
    at_bottom: bool
    """Following the newest message."""
    window: str | None = None
    """The window, in a signal."""


class SessionInfo(BaseModel):
    """One session, as the session list shows it."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    name: str
    created_at: int
    updated_at: int
    run_count: int
    summary: str
    agent_name: str


class SessionChange(BaseModel):
    """A window moved to another session."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    window: str | None = None


class SessionList(BaseModel):
    """The session list, as it was just read."""

    model_config = ConfigDict(extra="allow")

    sessions: list[SessionInfo]


class SessionCleared(BaseModel):
    """`/clear`: a session carries on under a new id."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    new_session_id: str
    summary: str


class SessionNamed(BaseModel):
    """A session was given its name."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    name: str


class SessionAttached(BaseModel):
    """A session bound, and the folder it works in."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    project_dir: str


class ChatMessage(BaseModel):
    """A message posted in a chat."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    text: str
    queued: bool
    """For the next turn rather than now."""
    from_extension: str | None = None
    """The extension that posted it; `None` for the person."""


class HistoryChange(BaseModel):
    """Turns were removed from a session's history."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    what: str
    """`truncated` or `discarded`."""
    run_id: str
    """The run removed from, or the message discarded."""


class ContentDelta(BaseModel):
    """A piece of the agent's answer as it streams."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    text: str
    thinking: bool


class RunRef(BaseModel):
    """Which run."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    run_id: str
    parent_run_id: str
    """Set for a sub-agent's run."""


class RunStart(BaseModel):
    """A run started."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    run_id: str
    parent_run_id: str
    agent_name: str
    model: str


class Run(BaseModel):
    """A finished run."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    run_id: str
    parent_run_id: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    duration: float
    """Seconds."""
    changed_files: list[str]
    """Files the run edited, relative to the project — only those within the extension's workspace grant, and none without one."""


class RunFailure(BaseModel):
    """A run stopped with an error."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    error: str


class Approval(BaseModel):
    """The agent asking to act."""

    model_config = ConfigDict(extra="allow")

    requirement_id: str
    session_id: str
    tool_name: str
    friendly_name: str
    tool_args: JsonValue
    details: str
    agent_path: list[str]
    """Which sub-agent is asking, outermost first."""


class RunPause(BaseModel):
    """A run waiting on approvals."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    run_id: str
    requirements: list[Approval]


class ApprovalResolved(BaseModel):
    """An approval was answered, by anyone."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    requirement_id: str


class ApprovalDetails(BaseModel):
    """The reason for an approval, arriving after it."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    requirement_id: str
    details: str


class ApprovalAnswer(BaseModel):
    """One answer to one approval."""

    model_config = ConfigDict(extra="allow")

    requirement_id: str
    action: str
    """`confirm` or `reject`."""
    choice: str = ""
    """`once`, `always` or `similar`; empty with `reject`."""


class ToolCall(BaseModel):
    """The agent started calling a tool."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    run_id: str
    parent_run_id: str
    tool_name: str
    friendly_name: str
    args_summary: str


class ToolResult(BaseModel):
    """A tool call finished."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    run_id: str
    parent_run_id: str
    summary: str
    is_error: bool
    is_rejected: bool


class ToolFailure(BaseModel):
    """A tool call failed."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    run_id: str
    parent_run_id: str
    error: str


class ModelUsage(BaseModel):
    """One model call's tokens."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    run_id: str
    parent_run_id: str
    input_tokens: int
    output_tokens: int


class Typing(BaseModel):
    """Someone's draft in a session, live."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    text: str
    window: str | None = None


class Status(BaseModel):
    """A session's status line."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    model: str
    model_configured: bool
    permission_mode: str
    input_tokens: int
    output_tokens: int
    context_tokens: int
    max_context: int


class Notice(BaseModel):
    """An info or error line in a session."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    text: str


class ContextUsage(BaseModel):
    """How full the context is, and with what."""

    model_config = ConfigDict(extra="allow")

    tokens: int
    max: int
    breakdown: JsonValue


class FileEdited(BaseModel):
    """The agent wrote a file."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    path: str
    """Relative to the project; only within the workspace grant."""


class AgentEvent(BaseModel):
    """Progress of a sub-agent or a team, as the transcript shows it."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    kind: str
    payload: JsonValue


class Retry(BaseModel):
    """A network call being retried, and whether it recovered."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    kind: str
    """`attempt` or `succeeded`."""
    payload: JsonValue


class View(BaseModel):
    """A json-render block in the transcript."""

    model_config = ConfigDict(extra="allow")

    view_id: str
    session_id: str
    title: str
    spec: JsonValue
    """A json-render spec: `{root, elements}`."""
    source: str
    """`visualizer`, or the extension that appended it."""


class ViewAction(BaseModel):
    """The person used a control inside a view."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    action: str
    params: JsonValue


class Plan(BaseModel):
    """The plan the agent proposed in plan mode."""

    model_config = ConfigDict(extra="allow")

    latest: str
    history: list[str]
    tasks: list[JsonValue]
    state: str
    """`pending`, or empty when there is none."""


class PlanChange(BaseModel):
    """A plan was submitted or decided."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    kind: str
    """`submitted` or `decided`."""
    payload: JsonValue


class TaskEvent(BaseModel):
    """A sub-agent task created or updated."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    task_id: str
    title: str
    assignee: str
    status: str


class TaskIteration(BaseModel):
    """A task loop's iteration count."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    iteration: int
    max_iterations: int


class TaskSnapshot(BaseModel):
    """Every task, as it now stands."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    tasks: list[JsonValue]


class Todos(BaseModel):
    """The agent's todo list."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    todos: list[JsonValue]


class LoopStatus(BaseModel):
    """`/loop`, as it stands."""

    model_config = ConfigDict(extra="allow")

    active: bool
    paused: bool
    prompt: str
    iteration_index: int
    iterations_remaining: int
    cap_explicit: bool


class CommandOutcome(BaseModel):
    """What a slash command answered."""

    model_config = ConfigDict(extra="allow")

    kind: str
    content: str
    display_content: str


class CollectionChange(BaseModel):
    """Something in a collection changed; read it again."""

    model_config = ConfigDict(extra="allow")

    what: str
    """What happened: `connected`, `installed`, `event`, …"""
    name: str | None = None
    """Which item, when there is one."""
    payload: JsonValue = None
    """The host's report of it."""


class WorkflowRun(BaseModel):
    """A workflow started."""

    model_config = ConfigDict(extra="allow")

    workflow_run_id: str
    name: str


class SchedulerEvent(BaseModel):
    """A scheduled task started, finished or changed."""

    model_config = ConfigDict(extra="allow")

    kind: str
    task_id: str
    description: str
    result: str


class ProcessEvent(BaseModel):
    """A background shell started, printed, or exited."""

    model_config = ConfigDict(extra="allow")

    kind: str
    """`started`, `line`, `exited` or `done`."""
    payload: JsonValue


class Head(BaseModel):
    """The project's git HEAD."""

    model_config = ConfigDict(extra="allow")

    commit_sha: str
    branch: str


class FilesChanged(BaseModel):
    """Files git sees as changed, within the extension's workspace grant."""

    model_config = ConfigDict(extra="allow")

    paths: list[str]
    truncated: bool
    """The list was capped: rescan rather than trust it."""


class ModelChange(BaseModel):
    """The model in use changed."""

    model_config = ConfigDict(extra="allow")

    current: str


class ExtensionInfo(BaseModel):
    """An installed extension."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    version: str
    state: str
    """`PREPARING`, `READY`, `FAILED` or `DISABLED`."""
    reason: str
    """Why it is not READY, and what to do."""
    web_url: str | None = None
    """Its home on the web — its service's URL, which is the service's `client_uri` — when one is known."""
    switched_off: list[Withheld]
    """What it does not get: a collision with another extension decided against it or not decided yet, or an override whose text lacks the sections its manifest declares."""
    dropped: list[Withheld]
    """Its prompt edits that land nowhere: another extension overrides that prompt, and its override has no section the edit names."""


class Withheld(BaseModel):
    """One thing an extension declared and does not get, and why."""

    model_config = ConfigDict(extra="allow")

    claim: str
    """`prompt:main`, `prompt:main#workflow`, `prompt:agent:explorer+notes`, `command:/review`, `anchor:NAME`."""
    reason: str


class ExtensionStateChange(BaseModel):
    """An extension's health moved."""

    model_config = ConfigDict(extra="allow")

    extension_id: str
    state: str
    reason: str


class PermissionModeChange(BaseModel):
    """The session's permission mode changed."""

    model_config = ConfigDict(extra="allow")

    mode: str
    previous: str


class Account(BaseModel):
    """Who is signed in to igni, without any credential."""

    model_config = ConfigDict(extra="allow")

    signed_in: bool
    email: str
    name: str
    org: str


class NotificationEntry(BaseModel):
    """One notification, from igni server or from an extension on this machine."""

    model_config = ConfigDict(extra="allow")

    id: str
    origin: str
    """`server` or `local`."""
    source: str
    """The extension it belongs to."""
    kind: str
    title: str
    body: str
    actor: str
    target: NotificationTarget | None = None
    created_at: str | None = None
    """ISO 8601."""
    read: bool


class NotificationCounts(BaseModel):
    """Unread notifications, in all and per extension."""

    model_config = ConfigDict(extra="allow")

    unread: int
    by_source: dict[str, int]


class SettingsChange(BaseModel):
    """An extension's settings, as now configured."""

    model_config = ConfigDict(extra="allow")

    values: dict[str, JsonValue]


class DecisionQuestion(BaseModel):
    """One typed question about a context. It quotes the context's fields by name in backticks, as in: Is `diff` only a refactor?"""

    model_config = ConfigDict(extra="allow")

    id: str
    """Its name in the answers: a lowercase name."""
    kind: str
    """`choice` (one of `options`), `score` (one of `levels`, lowest first) or `noul` (yes or no — Laya's name for it)."""
    question: str
    """The question, up to 500 characters."""
    options: dict[str, str] | None = None
    """`choice` only: 2–20 labels, each to what it means (empty when the label says it)."""
    levels: list[str] | None = None
    """`score` only: 2–10 level labels, lowest first."""


class DecisionAnswer(BaseModel):
    """One question's answer, with the probabilities it came from."""

    model_config = ConfigDict(extra="allow")

    kind: str
    answer: str
    """The option chosen, the level reached, or `yes` / `no`."""
    confidence: float
    """The probability of that answer."""
    probabilities: dict[str, float]
    """Every option's, level's, or `yes` and `no`'s probability."""
    score: float | None = None
    """`score` only: the expected level, 0 the lowest."""


class Decision(BaseModel):
    """Every question's answer about one context."""

    model_config = ConfigDict(extra="allow")

    answers: dict[str, DecisionAnswer]
    """By question id."""
    model: str
    """The Laya checkpoint that answered: `english` or `multilingual`."""


class ChatSubmitting(BaseModel):
    """A message about to start a run: the person's, or one an extension posted."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    text: str
    """What the agent will be sent. An interceptor may change it."""
    from_extension: str | None = None
    """The extension that posted it; empty for the person's own."""
    has_media: bool
    """Whether files or images go with it."""


class ToolCalling(BaseModel):
    """A tool the agent is about to run, before igni's own permission checks."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    tool: str
    """Its name, as the agent called it."""
    args: dict[str, JsonValue]
    """Its arguments. An interceptor may change them; igni's checks then run on what it changed them to."""


class PlanSubmitting(BaseModel):
    """A plan the agent is about to put to the person (`exit_plan_mode`)."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    plan: str
    """The plan, in markdown. An interceptor may change it."""
    tasks: list[JsonValue]
    """The tasks the agent listed with it."""
    attempt: int
    """How many submissions were rejected since the agent entered plan mode: 0 for the first."""


class ModelWrite(BaseModel):
    """A write to the model about to be made."""

    model_config = ConfigDict(extra="allow")

    path: str
    """The member written: `ui.navigate`, `sessions.current`."""
    value: JsonValue = None
    """A property's new value."""
    params: dict[str, JsonValue] = Field(default_factory=dict)
    """A method's parameters."""
    window: str | None = None
    """The window, for a per-window member."""
    session_id: str | None = None
    """The session it is made in."""
    writer: str
    """`person`, or the id of the extension making it."""


class HostAppState(HostObject):
    """Key–value state of the extension's own, kept by the host across restarts. An extension sees only its own keys."""

    PATH = "app.state"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.values: Property[dict[str, str]] = Property(self, "values", dict[str, str], writable=False)
        """Every key and value."""
        self.changed: Signal[StateChange] = Signal("app.state.changed", StateChange)
        """A key was set or deleted."""

    async def get(self, key: str) -> str | None:
        """One value, or `None`."""
        return cast(str | None, await self._call("get", {"key": key}, str | None))

    async def set(self, key: str, value: str) -> None:
        """Store one value."""
        await self._call("set", {"key": key, "value": value}, NO_RESULT)

    async def delete(self, key: str) -> None:
        """Remove one key."""
        await self._call("delete", {"key": key}, NO_RESULT)

class HostChatApprovals(HostObject):
    """The agent asking to act, and the answers."""

    PATH = "chat.approvals"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.waiting: Property[list[Approval]] = Property(self, "waiting", list[Approval], writable=False)
        """Requests not yet answered."""
        self.requested: Signal[Approval] = Signal("chat.approvals.requested", Approval)
        """The agent asked."""
        self.resolved: Signal[ApprovalResolved] = Signal("chat.approvals.resolved", ApprovalResolved)
        """Somebody answered."""
        self.details: Signal[ApprovalDetails] = Signal("chat.approvals.details", ApprovalDetails)
        """The reason arrived."""

    async def answer(self, requirement_id: str, action: str, choice: str | None = None) -> None:
        """Answer one request, as the person's dialog does."""
        await self._call("answer", {"requirement_id": requirement_id, "action": action, "choice": choice}, NO_RESULT)

    async def answer_all(self, answers: list[ApprovalAnswer]) -> None:
        """Answer every request of a pause at once."""
        await self._call("answer_all", {"answers": answers}, NO_RESULT)

class HostChatViews(HostObject):
    """json-render blocks in the transcript — the visualizer's and extensions'."""

    PATH = "chat.views"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.list: Property[list[View]] = Property(self, "list", list[View], writable=False)
        """The views in the current session."""
        self.shown: Signal[View] = Signal("chat.views.shown", View)
        """A view appeared or was replaced."""
        self.action: Signal[ViewAction] = Signal("chat.views.action", ViewAction)
        """The person used a control in a view."""

    async def dispatch(self, view_id: str, action: str, params: dict[str, JsonValue] | None = None) -> JsonValue:
        """Act on a view as if the person had."""
        return cast(JsonValue, await self._call("dispatch", {"view_id": view_id, "action": action, "params": params}, JsonValue))

class HostApp(HostObject):
    """igni itself: how it presents things, which version, whether it is connected."""

    PATH = "app"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.state = HostAppState(link, window)
        self.display: Property[Display] = Property(self, "display", Display, writable=False)
        """Theme, verbosity, output style."""
        self.version: Property[str] = Property(self, "version", str, writable=False)
        """The igni version running."""
        self.update_available: Property[UpdateInfo | None] = Property(self, "update_available", UpdateInfo | None, writable=False)
        """A newer igni, as last checked; `None` when there is none or it was not checked yet."""
        self.connection: Property[Connection] = Property(self, "connection", Connection, writable=False)
        """Model and server reachability."""
        self.display_changed: Signal[Display] = Signal("app.display_changed", Display)
        """Verbosity or output style changed."""
        self.connection_changed: Signal[Connection] = Signal("app.connection_changed", Connection)
        """The connection changed."""
        self.update_changed: Signal[UpdateInfo | None] = Signal("app.update_changed", UpdateInfo | None)
        """An update check answered."""

    async def set_verbose(self, on: bool) -> Display:
        """Show or hide routing detail."""
        return cast(Display, await self._call("set_verbose", {"on": on}, Display))

    async def set_output_style(self, name: str) -> Display:
        """Switch the output style, as `/output-style <name>` does."""
        return cast(Display, await self._call("set_output_style", {"name": name}, Display))

    async def check_for_update(self) -> UpdateInfo | None:
        """Ask whether a newer igni exists."""
        return cast(UpdateInfo | None, await self._call("check_for_update", {}, UpdateInfo | None))

    async def open_external(self, url: str) -> None:
        """Open an https link in the person's browser, the way igni opens every link outside it. The window asked about opens it: a page's own, else the active one. Anything but an absolute `https://` URL with a host and no credentials in it is refused (`invalid`)."""
        await self._call("open_external", {"url": url}, NO_RESULT)

class HostWindows(HostObject):
    """Every open igni window. Address one window's UI with `host.windows.ui(id)`."""

    PATH = "windows"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.list: Property[list[WindowInfo]] = Property(self, "list", list[WindowInfo], writable=False)
        """Every window connected now."""
        self.focused: Property[str | None] = Property(self, "focused", str | None, writable=False)
        """The window the person is using — the one with focus; `None` when igni is in the background."""
        self.opened: Signal[WindowInfo] = Signal("windows.opened", WindowInfo)
        """A window connected."""
        self.closed: Signal[WindowInfo] = Signal("windows.closed", WindowInfo)
        """A window went away."""
        self.focus_changed: Signal[WindowInfo] = Signal("windows.focus_changed", WindowInfo)
        """A window took focus or lost it; `focused` says which."""

    def ui(self, window_id: str) -> HostUi:
        """`host.ui` of one window, by its id."""
        return HostUi(self._link, window_id)

class HostUi(HostObject):
    """What a window shows — the model its views bind to. `host.ui` is the active window, the one the person last used; `host.windows.ui(id)` any window. igni's own views write here too, by the same path, so every change below is a signal whoever made it."""

    PATH = "ui"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.route: Property[list[Page]] = Property(self, "route", list[Page], writable=True)
        """The page stack; empty when the chat is showing. Writable: setting it navigates."""
        self.panel: Property[Panel | None] = Property(self, "panel", Panel | None, writable=True)
        """The panel open over the chat. Writable: setting it opens one, `None` closes it."""
        self.sidebar: Property[Sidebar] = Property(self, "sidebar", Sidebar, writable=True)
        """The sidebar. Writable."""
        self.preview: Property[Preview | None] = Property(self, "preview", Preview | None, writable=True)
        """The file previewed. Writable: `None` closes it."""
        self.selection: Property[SelectionState] = Property(self, "selection", SelectionState, writable=True)
        """What is selected. Writable."""
        self.composer: Property[Composer] = Property(self, "composer", Composer, writable=True)
        """The composer: draft, the files it refers to, its mode. Writable."""
        self.toasts: Property[list[Toast]] = Property(self, "toasts", list[Toast], writable=False)
        """Toasts on screen, oldest first."""
        self.dialog: Property[Dialog | None] = Property(self, "dialog", Dialog | None, writable=False)
        """The open dialog."""
        self.scroll: Property[Scroll] = Property(self, "scroll", Scroll, writable=False)
        """Where the transcript is."""
        self.navigated: Signal[Route] = Signal("ui.navigated", Route)
        """The page stack changed."""
        self.panel_changed: Signal[PanelChange] = Signal("ui.panel_changed", PanelChange)
        """A panel opened or closed."""
        self.sidebar_changed: Signal[SidebarChange] = Signal("ui.sidebar_changed", SidebarChange)
        """The sidebar changed."""
        self.preview_changed: Signal[PreviewChange] = Signal("ui.preview_changed", PreviewChange)
        """The preview opened, changed or closed."""
        self.selection_changed: Signal[SelectionChange] = Signal("ui.selection_changed", SelectionChange)
        """The selection changed."""
        self.composer_changed: Signal[ComposerChange] = Signal("ui.composer_changed", ComposerChange)
        """The draft changed."""
        self.toast_shown: Signal[ToastChange] = Signal("ui.toast_shown", ToastChange)
        """A toast appeared."""
        self.toast_dismissed: Signal[ToastChange] = Signal("ui.toast_dismissed", ToastChange)
        """A toast went away."""
        self.dialog_opened: Signal[DialogChange] = Signal("ui.dialog_opened", DialogChange)
        """A dialog opened."""
        self.dialog_closed: Signal[DialogChange] = Signal("ui.dialog_closed", DialogChange)
        """A dialog closed, with the answer."""
        self.scrolled: Signal[Scroll] = Signal("ui.scrolled", Scroll)
        """The transcript came to rest somewhere else, or started or stopped following the newest message."""

    async def navigate(self, page: str, params: dict[str, str] | None = None) -> list[Page]:
        """Open a page at the top of the stack, as a header link does."""
        return cast(list[Page], await self._call("navigate", {"page": page, "params": params}, list[Page]))

    async def back(self) -> list[Page]:
        """Go back one level; at the root, back to the chat."""
        return cast(list[Page], await self._call("back", {}, list[Page]))

    async def open_panel(self, kind: str, params: dict[str, JsonValue] | None = None) -> Panel:
        """Open a panel over the chat, closing the one that was open."""
        return cast(Panel, await self._call("open_panel", {"kind": kind, "params": params}, Panel))

    async def close_panel(self) -> None:
        """Close the panel."""
        await self._call("close_panel", {}, NO_RESULT)

    async def set_sidebar(self, open: bool, width: int | None = None, section: str | None = None) -> Sidebar:
        """Open or close the sidebar, and size it or bring a section into view."""
        return cast(Sidebar, await self._call("set_sidebar", {"open": open, "width": width, "section": section}, Sidebar))

    async def open_preview(self, path: str) -> Preview:
        """Preview a file of the project. (`preview` is the property.)"""
        return cast(Preview, await self._call("open_preview", {"path": path}, Preview))

    async def close_preview(self) -> None:
        """Close the preview."""
        await self._call("close_preview", {}, NO_RESULT)

    async def select(self, selection: SelectionState) -> SelectionState:
        """Select a message, text or file. The parts left out are cleared."""
        return cast(SelectionState, await self._call("select", {"selection": selection}, SelectionState))

    async def set_composer(self, text: str, attachments: list[str] | None = None, mode: str | None = None) -> Composer:
        """Replace the draft."""
        return cast(Composer, await self._call("set_composer", {"text": text, "attachments": attachments, "mode": mode}, Composer))

    async def toast(self, text: str, kind: str | None = None, body: str | None = None, target: Page | None = None) -> Toast:
        """Show a transient message, labelled as this extension's."""
        return cast(Toast, await self._call("toast", {"text": text, "kind": kind, "body": body, "target": target}, Toast))

    async def dismiss_toast(self, toast_id: str) -> None:
        """Take one of this extension's toasts away."""
        await self._call("dismiss_toast", {"toast_id": toast_id}, NO_RESULT)

    async def show_dialog(self, spec: JsonValue) -> JsonValue:
        """Show a dialog and wait for the person's answer. `spec` is `{title, body, buttons: [{id, label, primary}], input: {label, placeholder, value}}` — `buttons` defaults to one OK, `input` is optional. Answers `{button, value}`, or `None` when the dialog was dismissed, or replaced by another. (`dialog` is the property.)"""
        return cast(JsonValue, await self._call("show_dialog", {"spec": spec}, JsonValue))

    async def scroll_to(self, message_id: str) -> Scroll:
        """Scroll the transcript to a message, by its run; `bottom` for the newest."""
        return cast(Scroll, await self._call("scroll_to", {"message_id": message_id}, Scroll))

class HostSessions(HostObject):
    """The sessions of this project, and which one the active window shows."""

    PATH = "sessions"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.list: Property[list[SessionInfo]] = Property(self, "list", list[SessionInfo], writable=False)
        """Every session, newest first."""
        self.current: Property[str] = Property(self, "current", str, writable=True)
        """The session the active window shows. Writable: setting it switches, and is refused where the person's switch would be."""
        self.current_changed: Signal[SessionChange] = Signal("session.switched", SessionChange)
        """A window moved to another session."""
        self.listed: Signal[SessionList] = Signal("sessions.listed", SessionList)
        """The session list was read."""
        self.cleared: Signal[SessionCleared] = Signal("sessions.cleared", SessionCleared)
        """`/clear` renewed a session."""
        self.named: Signal[SessionNamed] = Signal("sessions.named", SessionNamed)
        """A session got its name."""

    async def open(self, session_id: str) -> SessionChange:
        """Show a session in the active window."""
        return cast(SessionChange, await self._call("open", {"session_id": session_id}, SessionChange))

    async def create(self, project_dir: str | None = None) -> SessionAttached:
        """Start a new session, in a folder or in this project."""
        return cast(SessionAttached, await self._call("create", {"project_dir": project_dir}, SessionAttached))

    async def attach(self, session_id: str, project_dir: str | None = None) -> SessionAttached:
        """Make sure a session is live, without showing it."""
        return cast(SessionAttached, await self._call("attach", {"session_id": session_id, "project_dir": project_dir}, SessionAttached))

class HostChat(HostObject):
    """The conversation in the current session. Signals carry the session they happened in, and arrive for every live session."""

    PATH = "chat"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.approvals = HostChatApprovals(link, window)
        self.views = HostChatViews(link, window)
        self.session_id: Property[str] = Property(self, "session_id", str, writable=False)
        """The current session."""
        self.messages: Property[list[JsonValue]] = Property(self, "messages", list[JsonValue], writable=False)
        """The transcript, turn by turn."""
        self.pending: Property[list[JsonValue]] = Property(self, "pending", list[JsonValue], writable=False)
        """Messages waiting for a turn."""
        self.processing: Property[bool] = Property(self, "processing", bool, writable=False)
        """Whether a run is going."""
        self.status: Property[Status] = Property(self, "status", Status, writable=False)
        """The status line."""
        self.timeout: Property[int] = Property(self, "timeout", int, writable=False)
        """Seconds a run may take."""
        self.interrupted_runs: Property[list[JsonValue]] = Property(self, "interrupted_runs", list[JsonValue], writable=False)
        """Runs that stopped part-way."""
        self.context: Property[ContextUsage] = Property(self, "context", ContextUsage, writable=False)
        """Token counts and what fills them."""
        self.message_added: Signal[ChatMessage] = Signal("chat.message_added", ChatMessage)
        """A message was posted — by the person or an extension."""
        self.content_delta: Signal[ContentDelta] = Signal("chat.content_delta", ContentDelta)
        """The agent's answer, streaming."""
        self.reasoning_started: Signal[RunRef] = Signal("chat.reasoning_started", RunRef)
        """The agent started thinking."""
        self.run_started: Signal[RunStart] = Signal("chat.run_started", RunStart)
        """A run started."""
        self.run_completed: Signal[Run] = Signal("chat.run_completed", Run)
        """A run finished, with the files it changed."""
        self.run_error: Signal[RunFailure] = Signal("chat.run_error", RunFailure)
        """A run stopped with an error."""
        self.run_paused: Signal[RunPause] = Signal("chat.run_paused", RunPause)
        """A run is waiting for approvals."""
        self.streaming_done: Signal[RunRef] = Signal("chat.streaming_done", RunRef)
        """The answer finished streaming."""
        self.typing: Signal[Typing] = Signal("chat.typing", Typing)
        """Somebody's draft, live."""
        self.status_changed: Signal[Status] = Signal("chat.status_changed", Status)
        """The status line changed."""
        self.info: Signal[Notice] = Signal("chat.info", Notice)
        """An info line."""
        self.error: Signal[Notice] = Signal("chat.error", Notice)
        """An error line."""
        self.tool_started: Signal[ToolCall] = Signal("chat.tool_started", ToolCall)
        """The agent called a tool."""
        self.tool_completed: Signal[ToolResult] = Signal("chat.tool_completed", ToolResult)
        """A tool call finished."""
        self.tool_error: Signal[ToolFailure] = Signal("chat.tool_error", ToolFailure)
        """A tool call failed."""
        self.model_completed: Signal[ModelUsage] = Signal("chat.model_completed", ModelUsage)
        """A model call finished."""
        self.file_edited: Signal[FileEdited] = Signal("chat.file_edited", FileEdited)
        """The agent wrote a file."""
        self.agent_event: Signal[AgentEvent] = Signal("chat.agent_event", AgentEvent)
        """A sub-agent or team moved on."""
        self.retry: Signal[Retry] = Signal("chat.retry", Retry)
        """A network call is being retried."""
        self.history_changed: Signal[HistoryChange] = Signal("chat.history_changed", HistoryChange)
        """Turns were truncated or discarded."""

    async def post(self, text: str, attachments: dict[str, str] | None = None) -> None:
        """Post a message in the chat, labelled as this extension. The agent sees it and answers in the chat as usual."""
        await self._call("post", {"text": text, "attachments": attachments}, NO_RESULT)

    async def queue(self, text: str) -> None:
        """Post a message for the next turn, labelled as this extension."""
        await self._call("queue", {"text": text}, NO_RESULT)

    async def append(self, view: JsonValue, title: str | None = None) -> str:
        """Show a json-render block in the transcript. Shown to the person, never sent to the model. Returns its id."""
        return cast(str, await self._call("append", {"view": view, "title": title}, str))

    async def replace(self, view_id: str, view: JsonValue, title: str | None = None) -> None:
        """Replace a block this extension appended."""
        await self._call("replace", {"view_id": view_id, "view": view, "title": title}, NO_RESULT)

    async def search(self, query: str, limit: int | None = None) -> list[JsonValue]:
        """Search the transcript."""
        return cast(list[JsonValue], await self._call("search", {"query": query, "limit": limit}, list[JsonValue]))

    async def cancel(self) -> None:
        """Stop the run."""
        await self._call("cancel", {}, NO_RESULT)

    async def cancel_agent(self, run_id: str) -> None:
        """Stop one sub-agent's run."""
        await self._call("cancel_agent", {"run_id": run_id}, NO_RESULT)

    async def truncate(self, run_id: str) -> JsonValue:
        """Remove a run and everything after it."""
        return cast(JsonValue, await self._call("truncate", {"run_id": run_id}, JsonValue))

    async def compact(self) -> SessionCleared | None:
        """Compact the conversation if it needs it, as igni does after a run."""
        return cast(SessionCleared | None, await self._call("compact", {}, SessionCleared | None))

    async def extract_learnings(self, user_msg: str, assistant_msg: str) -> None:
        """Have the agent note what a turn taught it."""
        await self._call("extract_learnings", {"user_msg": user_msg, "assistant_msg": assistant_msg}, NO_RESULT)

    async def discard_interrupted(self, message_id: str) -> JsonValue:
        """Forget an interrupted run."""
        return cast(JsonValue, await self._call("discard_interrupted", {"message_id": message_id}, JsonValue))

    async def upload(self, filename: str, content_base64: str) -> JsonValue:
        """Upload an attachment for a later `post`."""
        return cast(JsonValue, await self._call("upload", {"filename": filename, "content_base64": content_base64}, JsonValue))

class HostPlan(HostObject):
    """Plan mode: the plan the agent proposed."""

    PATH = "plan"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.latest: Property[Plan] = Property(self, "latest", Plan, writable=False)
        """The latest plan."""
        self.changed: Signal[PlanChange] = Signal("plan.changed", PlanChange)
        """A plan was submitted or decided."""

    async def approve(self, run_id: str | None = None) -> JsonValue:
        """Approve the plan."""
        return cast(JsonValue, await self._call("approve", {"run_id": run_id}, JsonValue))

    async def dismiss(self, run_id: str | None = None) -> JsonValue:
        """Send the plan back."""
        return cast(JsonValue, await self._call("dismiss", {"run_id": run_id}, JsonValue))

class HostTasks(HostObject):
    """The agent's todo list and its sub-agent tasks."""

    PATH = "tasks"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.todos: Property[list[JsonValue]] = Property(self, "todos", list[JsonValue], writable=False)
        """The todo list."""
        self.created: Signal[TaskEvent] = Signal("tasks.created", TaskEvent)
        """A task was created."""
        self.updated: Signal[TaskEvent] = Signal("tasks.updated", TaskEvent)
        """A task changed."""
        self.iteration: Signal[TaskIteration] = Signal("tasks.iteration", TaskIteration)
        """A task loop moved on."""
        self.snapshot: Signal[TaskSnapshot] = Signal("tasks.snapshot", TaskSnapshot)
        """Every task, as it now stands."""
        self.todos_changed: Signal[Todos] = Signal("tasks.todos_changed", Todos)
        """The todo list changed."""

class HostLoop(HostObject):
    """`/loop`: a prompt repeated until done."""

    PATH = "loop"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.status: Property[LoopStatus] = Property(self, "status", LoopStatus, writable=False)
        """Where it is."""
        self.changed: Signal[LoopStatus] = Signal("loop.changed", LoopStatus)
        """It paused, resumed, advanced or stopped."""

    async def pause(self) -> bool:
        """Pause after this iteration."""
        return cast(bool, await self._call("pause", {}, bool))

    async def resume(self) -> str:
        """Carry on."""
        return cast(str, await self._call("resume", {}, str))

    async def cancel_pending(self) -> bool:
        """Stop the loop."""
        return cast(bool, await self._call("cancel_pending", {}, bool))

    async def pop_pending(self) -> JsonValue:
        """Take the next iteration."""
        return cast(JsonValue, await self._call("pop_pending", {}, JsonValue))

class HostCommands(HostObject):
    """Slash commands and skills."""

    PATH = "commands"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.list: Property[list[JsonValue]] = Property(self, "list", list[JsonValue], writable=False)
        """Every slash command."""
        self.skills: Property[list[str]] = Property(self, "skills", list[str], writable=False)
        """Skill names."""
        self.skill_definitions: Property[list[JsonValue]] = Property(self, "skill_definitions", list[JsonValue], writable=False)
        """Each skill's description and prompt."""
        self.skill_details: Property[list[JsonValue]] = Property(self, "skill_details", list[JsonValue], writable=False)
        """Each skill's details."""

    async def run(self, command: str) -> CommandOutcome:
        """Run a slash command, as the extension. The answer comes back here."""
        return cast(CommandOutcome, await self._call("run", {"command": command}, CommandOutcome))

class HostAgents(HostObject):
    """Agent definitions."""

    PATH = "agents"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.details: Property[list[JsonValue]] = Property(self, "details", list[JsonValue], writable=False)
        """Every agent."""
        self.changed: Signal[CollectionChange] = Signal("agents.changed", CollectionChange)
        """An agent was promoted or discarded."""

    async def promote_ephemeral(self, name: str) -> JsonValue:
        """Keep an agent made for one task."""
        return cast(JsonValue, await self._call("promote_ephemeral", {"name": name}, JsonValue))

    async def discard_ephemeral(self, name: str) -> JsonValue:
        """Drop an agent made for one task."""
        return cast(JsonValue, await self._call("discard_ephemeral", {"name": name}, JsonValue))

class HostWorkflows(HostObject):
    """Workflows: scripted runs."""

    PATH = "workflows"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.list: Property[list[JsonValue]] = Property(self, "list", list[JsonValue], writable=False)
        """Every workflow."""
        self.changed: Signal[CollectionChange] = Signal("workflows.changed", CollectionChange)
        """A workflow run moved on."""

    async def run(self, name: str, input: dict[str, JsonValue] | None = None) -> WorkflowRun:
        """Start a workflow."""
        return cast(WorkflowRun, await self._call("run", {"name": name, "input": input}, WorkflowRun))

    async def cancel(self, workflow_run_id: str) -> bool:
        """Stop a workflow run."""
        return cast(bool, await self._call("cancel", {"workflow_run_id": workflow_run_id}, bool))

class HostScheduler(HostObject):
    """Scheduled tasks."""

    PATH = "scheduler"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.tasks: Property[list[JsonValue]] = Property(self, "tasks", list[JsonValue], writable=False)
        """Every scheduled task."""
        self.event: Signal[SchedulerEvent] = Signal("scheduler.event", SchedulerEvent)
        """A task started, finished or changed."""

    async def start(self) -> None:
        """Start the scheduler."""
        await self._call("start", {}, NO_RESULT)

    async def run_now(self, task_id: str) -> str:
        """Run a scheduled task now."""
        return cast(str, await self._call("run_now", {"task_id": task_id}, str))

    async def cancel(self, task_id: str) -> None:
        """Cancel a scheduled task."""
        await self._call("cancel", {"task_id": task_id}, NO_RESULT)

class HostProcesses(HostObject):
    """Background shells."""

    PATH = "processes"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.list: Property[list[JsonValue]] = Property(self, "list", list[JsonValue], writable=False)
        """Every background shell."""
        self.changed: Signal[ProcessEvent] = Signal("processes.changed", ProcessEvent)
        """A shell started, printed or exited."""

    async def tail(self, pid: int, lines: int | None = None) -> JsonValue:
        """A shell's last lines."""
        return cast(JsonValue, await self._call("tail", {"pid": pid, "lines": lines}, JsonValue))

    async def stop(self, pid: int) -> JsonValue:
        """Stop a shell."""
        return cast(JsonValue, await self._call("stop", {"pid": pid}, JsonValue))

    async def run(self, command: str) -> JsonValue:
        """Run a command in the project and wait for it."""
        return cast(JsonValue, await self._call("run", {"command": command}, JsonValue))

class HostWorkspace(HostObject):
    """The project, as far as the extension's workspace grant reaches."""

    PATH = "workspace"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.root: Property[str] = Property(self, "root", str, writable=False)
        """The project directory."""
        self.head: Property[Head | None] = Property(self, "head", Head | None, writable=False)
        """Git HEAD; `None` outside a repository."""
        self.head_changed: Signal[Head] = Signal("workspace.head-changed", Head)
        """HEAD moved."""
        self.files_changed: Signal[FilesChanged] = Signal("workspace.files-changed", FilesChanged)
        """Files changed, as git sees them, within the grant."""

    async def read(self, path: str) -> JsonValue:
        """Read a file in the project."""
        return cast(JsonValue, await self._call("read", {"path": path}, JsonValue))

    async def list(self, dir: str | None = None) -> JsonValue:
        """List a directory in the project."""
        return cast(JsonValue, await self._call("list", {"dir": dir}, JsonValue))

    async def complete(self, prefix: str) -> JsonValue:
        """Complete a path, as `@` does."""
        return cast(JsonValue, await self._call("complete", {"prefix": prefix}, JsonValue))

    async def search(self, query: str) -> JsonValue:
        """Search the code."""
        return cast(JsonValue, await self._call("search", {"query": query}, JsonValue))

    async def pick_dir(self) -> JsonValue:
        """Ask the person to pick a folder."""
        return cast(JsonValue, await self._call("pick_dir", {}, JsonValue))

class HostModels(HostObject):
    """Models."""

    PATH = "models"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.registry: Property[JsonValue] = Property(self, "registry", JsonValue, writable=False)
        """Every model igni knows."""
        self.current: Property[str] = Property(self, "current", str, writable=True)
        """The model in use. Writable: setting it switches."""
        self.changed: Signal[ModelChange] = Signal("models.changed", ModelChange)
        """The model in use changed."""

class HostMcp(HostObject):
    """MCP servers."""

    PATH = "mcp"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.servers: Property[list[JsonValue]] = Property(self, "servers", list[JsonValue], writable=False)
        """Every server."""
        self.status: Property[list[JsonValue]] = Property(self, "status", list[JsonValue], writable=False)
        """Each server's status."""
        self.details: Property[list[JsonValue]] = Property(self, "details", list[JsonValue], writable=False)
        """Each server's tools and details."""
        self.changed: Signal[CollectionChange] = Signal("mcp.changed", CollectionChange)
        """A server connected, disconnected or changed tools."""

    async def connect(self, name: str) -> None:
        """Connect a server."""
        await self._call("connect", {"name": name}, NO_RESULT)

    async def disconnect(self, name: str) -> None:
        """Disconnect a server."""
        await self._call("disconnect", {"name": name}, NO_RESULT)

    async def ensure(self) -> JsonValue:
        """Start the servers that should be running."""
        return cast(JsonValue, await self._call("ensure", {}, JsonValue))

    async def set_tool_enabled(self, server: str, tool: str, on: bool) -> JsonValue:
        """Offer or withhold one tool."""
        return cast(JsonValue, await self._call("set_tool_enabled", {"server": server, "tool": tool, "on": on}, JsonValue))

class HostPlugins(HostObject):
    """Plugins and marketplaces."""

    PATH = "plugins"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.installed: Property[list[JsonValue]] = Property(self, "installed", list[JsonValue], writable=False)
        """Every installed plugin."""
        self.marketplaces: Property[list[JsonValue]] = Property(self, "marketplaces", list[JsonValue], writable=False)
        """Every marketplace."""
        self.changed: Signal[CollectionChange] = Signal("plugins.changed", CollectionChange)
        """A plugin or marketplace changed."""

    async def contents(self, name: str) -> JsonValue:
        """What a plugin carries."""
        return cast(JsonValue, await self._call("contents", {"name": name}, JsonValue))

    async def preview(self, source: str, branch: str | None = None, subdir: str | None = None) -> JsonValue:
        """What a plugin would install."""
        return cast(JsonValue, await self._call("preview", {"source": source, "branch": branch, "subdir": subdir}, JsonValue))

    async def install(self, ref: str, install_ref: str | None = None) -> None:
        """Install a plugin."""
        await self._call("install", {"ref": ref, "install_ref": install_ref}, NO_RESULT)

    async def update(self, name: str, install_ref: str | None = None) -> None:
        """Update a plugin."""
        await self._call("update", {"name": name, "install_ref": install_ref}, NO_RESULT)

    async def remove(self, name: str) -> None:
        """Remove a plugin."""
        await self._call("remove", {"name": name}, NO_RESULT)

    async def set_enabled(self, name: str, on: bool) -> None:
        """Enable or disable a plugin."""
        await self._call("set_enabled", {"name": name, "on": on}, NO_RESULT)

    async def add_marketplace(self, url: str) -> None:
        """Add a marketplace."""
        await self._call("add_marketplace", {"url": url}, NO_RESULT)

    async def remove_marketplace(self, name: str) -> None:
        """Remove a marketplace."""
        await self._call("remove_marketplace", {"name": name}, NO_RESULT)

    async def refresh_marketplaces(self, name: str | None = None) -> None:
        """Refresh marketplaces."""
        await self._call("refresh_marketplaces", {"name": name}, NO_RESULT)

class HostExtensions(HostObject):
    """Installed extensions. Read-only, apart from this extension's own settings."""

    PATH = "extensions"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.installed: Property[list[ExtensionInfo]] = Property(self, "installed", list[ExtensionInfo], writable=False)
        """Every installed extension."""
        self.state_changed: Signal[ExtensionStateChange] = Signal("extension.state-changed", ExtensionStateChange)
        """An extension's health moved."""

    async def set_settings(self, values: dict[str, JsonValue]) -> SettingsChange:
        """Change this extension's own settings."""
        return cast(SettingsChange, await self._call("set_settings", {"values": values}, SettingsChange))

class HostHooks(HostObject):
    """Hooks."""

    PATH = "hooks"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.details: Property[list[JsonValue]] = Property(self, "details", list[JsonValue], writable=False)
        """Every hook."""
        self.changed: Signal[CollectionChange] = Signal("hooks.changed", CollectionChange)
        """Hooks were reloaded."""

    async def reload(self) -> None:
        """Reload hooks."""
        await self._call("reload", {}, NO_RESULT)

    async def fire_session_start(self) -> None:
        """Run the session-start hooks."""
        await self._call("fire_session_start", {}, NO_RESULT)

class HostPermissions(HostObject):
    """What the agent may do without asking."""

    PATH = "permissions"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.mode: Property[str] = Property(self, "mode", str, writable=False)
        """`default`, `plan`, `acceptEdits` or `bypassPermissions`."""
        self.mode_changed: Signal[PermissionModeChange] = Signal("permissions.mode_changed", PermissionModeChange)
        """The mode changed."""

    async def check(self, tool_name: str, func_name: str, tool_args: dict[str, JsonValue] | None = None) -> JsonValue:
        """Would this tool call be allowed, asked or denied?"""
        return cast(JsonValue, await self._call("check", {"tool_name": tool_name, "func_name": func_name, "tool_args": tool_args}, JsonValue))

    async def save_rule(self, rule: str, level: str) -> None:
        """Save a permission rule."""
        await self._call("save_rule", {"rule": rule, "level": level}, NO_RESULT)

class HostPolicy(HostObject):
    """The group policy in force."""

    PATH = "policy"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.pack: Property[JsonValue] = Property(self, "pack", JsonValue, writable=False)
        """The group's policy pack."""

class HostAccount(HostObject):
    """Whether the person is signed in to igni. Never their session or a credential."""

    PATH = "account"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.status: Property[Account] = Property(self, "status", Account, writable=False)
        """Who is signed in."""
        self.changed: Signal[Account] = Signal("account.changed", Account)
        """Someone signed in or out."""

    async def sign_in(self) -> None:
        """Start the person's own sign-in. Its result is theirs."""
        await self._call("sign_in", {}, NO_RESULT)

    async def sign_out(self) -> Account:
        """Sign the person out."""
        return cast(Account, await self._call("sign_out", {}, Account))

class HostNotifications(HostObject):
    """The floor's notifications. Its own always; everyone's with `notifications`."""

    PATH = "notifications"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.list: Property[list[NotificationEntry]] = Property(self, "list", list[NotificationEntry], writable=False)
        """Newest first."""
        self.unread: Property[NotificationCounts] = Property(self, "unread", NotificationCounts, writable=False)
        """Unread counts."""
        self.changed: Signal[NotificationCounts] = Signal("notifications.changed", NotificationCounts)
        """The counts moved."""

    async def notify(self, notification: LocalNotification) -> None:
        """Tell the person at this machine something. It never leaves the machine."""
        await self._call("notify", {"notification": notification}, NO_RESULT)

    async def mark_read(self, ids: list[str] | None = None, source: str | None = None) -> NotificationCounts:
        """Mark notifications read: these ids, one source, or all of them."""
        return cast(NotificationCounts, await self._call("mark_read", {"ids": ids, "source": source}, NotificationCounts))

class HostSelf(HostObject):
    """This extension, as the host sees it."""

    PATH = "self"

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.id: Property[str] = Property(self, "id", str, writable=False)
        """Its manifest id."""
        self.settings: Property[dict[str, JsonValue]] = Property(self, "settings", dict[str, JsonValue], writable=True)
        """Its settings. Writable."""
        self.state: Property[ExtensionInfo] = Property(self, "state", ExtensionInfo, writable=False)
        """Its own entry."""
        self.web_url: Property[str | None] = Property(self, "web_url", str | None, writable=False)
        """Its home on the web: its service's URL, when it has one."""
        self.settings_changed: Signal[SettingsChange] = Signal("self.settings_changed", SettingsChange)
        """Its settings changed."""
        self.state_changed: Signal[ExtensionStateChange] = Signal("self.state_changed", ExtensionStateChange)
        """Its health moved."""

class Host(HostObject):
    """The root. `changed` reports every change anywhere in the model, for an extension that wants to see all of it; each delivery needs the grant of the member that changed."""

    PATH = ""

    def __init__(self, link: HostLink, window: str | None = None) -> None:
        super().__init__(link, window)
        self.app = HostApp(link, window)
        self.windows = HostWindows(link, window)
        self.ui = HostUi(link, window)
        self.sessions = HostSessions(link, window)
        self.chat = HostChat(link, window)
        self.plan = HostPlan(link, window)
        self.tasks = HostTasks(link, window)
        self.loop = HostLoop(link, window)
        self.commands = HostCommands(link, window)
        self.agents = HostAgents(link, window)
        self.workflows = HostWorkflows(link, window)
        self.scheduler = HostScheduler(link, window)
        self.processes = HostProcesses(link, window)
        self.workspace = HostWorkspace(link, window)
        self.models = HostModels(link, window)
        self.mcp = HostMcp(link, window)
        self.plugins = HostPlugins(link, window)
        self.extensions = HostExtensions(link, window)
        self.hooks = HostHooks(link, window)
        self.permissions = HostPermissions(link, window)
        self.policy = HostPolicy(link, window)
        self.account = HostAccount(link, window)
        self.notifications = HostNotifications(link, window)
        self.self = HostSelf(link, window)
        self.changed: Signal[Change] = Signal("changed", Change)
        """Anything changed. Carries the member's path and its new value."""

    async def decide(self, context: dict[str, str], questions: list[DecisionQuestion]) -> Decision:
        """Ask igni's local decision model (Laya) typed questions about a context, in one pass on this machine. `context` is named text fields — at most 8, 6,000 characters in all — that the questions quote in backticks; `questions` are 1–16 `DecisionQuestion`s. A context the model would have to cut is refused (`invalid`), never answered from part of it; a model that cannot be loaded answers `unavailable`. Zero-shot accuracy is modest: measure it on your own data before acting on an answer, and use the probabilities, not just the answer."""
        return cast(Decision, await self._call("decide", {"context": context, "questions": questions}, Decision))

    async def decide_batch(self, contexts: list[dict[str, str]], questions: list[DecisionQuestion]) -> list[Decision]:
        """`decide` for up to 16 contexts at once, sharing forward passes. One decision per context, in order."""
        return cast(list[Decision], await self._call("decide_batch", {"contexts": contexts, "questions": questions}, list[Decision]))


class Intercepts:
    """Every event an extension can intercept: `@ext.intercept(Intercepts.TOOLS_CALLING)`.

    The agent's decision points, then every write to the model by its
    member's name (`UI_NAVIGATE` is a write to `ui.navigate`)."""

    CHAT_SUBMITTING = Intercept("chat.submitting", ChatSubmitting, ("text",))
    """A message is about to start a run: the person's, or one an extension posted."""
    TOOLS_CALLING = Intercept("tools.calling", ToolCalling, ("args",))
    """The agent is about to run a tool. Intercepted before igni's own permission checks, which then run on whatever the interceptor left."""
    PLAN_SUBMITTING = Intercept("plan.submitting", PlanSubmitting, ("plan",))
    """The agent is about to put a plan to the person (`exit_plan_mode`)."""
    APP_SET_VERBOSE = Intercept("app.set_verbose", ModelWrite, ())
    APP_SET_OUTPUT_STYLE = Intercept("app.set_output_style", ModelWrite, ())
    APP_OPEN_EXTERNAL = Intercept("app.open_external", ModelWrite, ())
    UI_ROUTE = Intercept("ui.route", ModelWrite, ())
    UI_PANEL = Intercept("ui.panel", ModelWrite, ())
    UI_SIDEBAR = Intercept("ui.sidebar", ModelWrite, ())
    UI_PREVIEW = Intercept("ui.preview", ModelWrite, ())
    UI_SELECTION = Intercept("ui.selection", ModelWrite, ())
    UI_COMPOSER = Intercept("ui.composer", ModelWrite, ())
    UI_NAVIGATE = Intercept("ui.navigate", ModelWrite, ())
    UI_BACK = Intercept("ui.back", ModelWrite, ())
    UI_OPEN_PANEL = Intercept("ui.open_panel", ModelWrite, ())
    UI_CLOSE_PANEL = Intercept("ui.close_panel", ModelWrite, ())
    UI_SET_SIDEBAR = Intercept("ui.set_sidebar", ModelWrite, ())
    UI_OPEN_PREVIEW = Intercept("ui.open_preview", ModelWrite, ())
    UI_CLOSE_PREVIEW = Intercept("ui.close_preview", ModelWrite, ())
    UI_SELECT = Intercept("ui.select", ModelWrite, ())
    UI_SET_COMPOSER = Intercept("ui.set_composer", ModelWrite, ())
    UI_TOAST = Intercept("ui.toast", ModelWrite, ())
    UI_DISMISS_TOAST = Intercept("ui.dismiss_toast", ModelWrite, ())
    UI_SHOW_DIALOG = Intercept("ui.show_dialog", ModelWrite, ())
    UI_SCROLL_TO = Intercept("ui.scroll_to", ModelWrite, ())
    SESSIONS_CURRENT = Intercept("sessions.current", ModelWrite, ())
    SESSIONS_OPEN = Intercept("sessions.open", ModelWrite, ())
    SESSIONS_CREATE = Intercept("sessions.create", ModelWrite, ())
    SESSIONS_ATTACH = Intercept("sessions.attach", ModelWrite, ())
    CHAT_POST = Intercept("chat.post", ModelWrite, ())
    CHAT_QUEUE = Intercept("chat.queue", ModelWrite, ())
    CHAT_APPEND = Intercept("chat.append", ModelWrite, ())
    CHAT_REPLACE = Intercept("chat.replace", ModelWrite, ())
    CHAT_CANCEL = Intercept("chat.cancel", ModelWrite, ())
    CHAT_CANCEL_AGENT = Intercept("chat.cancel_agent", ModelWrite, ())
    CHAT_TRUNCATE = Intercept("chat.truncate", ModelWrite, ())
    CHAT_COMPACT = Intercept("chat.compact", ModelWrite, ())
    CHAT_EXTRACT_LEARNINGS = Intercept("chat.extract_learnings", ModelWrite, ())
    CHAT_DISCARD_INTERRUPTED = Intercept("chat.discard_interrupted", ModelWrite, ())
    CHAT_UPLOAD = Intercept("chat.upload", ModelWrite, ())
    CHAT_APPROVALS_ANSWER = Intercept("chat.approvals.answer", ModelWrite, ())
    CHAT_APPROVALS_ANSWER_ALL = Intercept("chat.approvals.answer_all", ModelWrite, ())
    CHAT_VIEWS_DISPATCH = Intercept("chat.views.dispatch", ModelWrite, ())
    PLAN_APPROVE = Intercept("plan.approve", ModelWrite, ())
    PLAN_DISMISS = Intercept("plan.dismiss", ModelWrite, ())
    LOOP_PAUSE = Intercept("loop.pause", ModelWrite, ())
    LOOP_RESUME = Intercept("loop.resume", ModelWrite, ())
    LOOP_CANCEL_PENDING = Intercept("loop.cancel_pending", ModelWrite, ())
    LOOP_POP_PENDING = Intercept("loop.pop_pending", ModelWrite, ())
    COMMANDS_RUN = Intercept("commands.run", ModelWrite, ())
    AGENTS_PROMOTE_EPHEMERAL = Intercept("agents.promote_ephemeral", ModelWrite, ())
    AGENTS_DISCARD_EPHEMERAL = Intercept("agents.discard_ephemeral", ModelWrite, ())
    WORKFLOWS_RUN = Intercept("workflows.run", ModelWrite, ())
    WORKFLOWS_CANCEL = Intercept("workflows.cancel", ModelWrite, ())
    SCHEDULER_START = Intercept("scheduler.start", ModelWrite, ())
    SCHEDULER_RUN_NOW = Intercept("scheduler.run_now", ModelWrite, ())
    SCHEDULER_CANCEL = Intercept("scheduler.cancel", ModelWrite, ())
    PROCESSES_STOP = Intercept("processes.stop", ModelWrite, ())
    PROCESSES_RUN = Intercept("processes.run", ModelWrite, ())
    WORKSPACE_PICK_DIR = Intercept("workspace.pick_dir", ModelWrite, ())
    MODELS_CURRENT = Intercept("models.current", ModelWrite, ())
    MCP_CONNECT = Intercept("mcp.connect", ModelWrite, ())
    MCP_DISCONNECT = Intercept("mcp.disconnect", ModelWrite, ())
    MCP_ENSURE = Intercept("mcp.ensure", ModelWrite, ())
    MCP_SET_TOOL_ENABLED = Intercept("mcp.set_tool_enabled", ModelWrite, ())
    PLUGINS_INSTALL = Intercept("plugins.install", ModelWrite, ())
    PLUGINS_UPDATE = Intercept("plugins.update", ModelWrite, ())
    PLUGINS_REMOVE = Intercept("plugins.remove", ModelWrite, ())
    PLUGINS_SET_ENABLED = Intercept("plugins.set_enabled", ModelWrite, ())
    PLUGINS_ADD_MARKETPLACE = Intercept("plugins.add_marketplace", ModelWrite, ())
    PLUGINS_REMOVE_MARKETPLACE = Intercept("plugins.remove_marketplace", ModelWrite, ())
    PLUGINS_REFRESH_MARKETPLACES = Intercept("plugins.refresh_marketplaces", ModelWrite, ())
    HOOKS_RELOAD = Intercept("hooks.reload", ModelWrite, ())
    HOOKS_FIRE_SESSION_START = Intercept("hooks.fire_session_start", ModelWrite, ())
    PERMISSIONS_SAVE_RULE = Intercept("permissions.save_rule", ModelWrite, ())
    ACCOUNT_SIGN_IN = Intercept("account.sign_in", ModelWrite, ())
    ACCOUNT_SIGN_OUT = Intercept("account.sign_out", ModelWrite, ())

__all__ = [
    "Account",
    "AgentEvent",
    "Approval",
    "ApprovalAnswer",
    "ApprovalDetails",
    "ApprovalResolved",
    "Change",
    "ChatMessage",
    "ChatSubmitting",
    "CollectionChange",
    "CommandOutcome",
    "Composer",
    "ComposerChange",
    "Connection",
    "ContentDelta",
    "ContextUsage",
    "Decision",
    "DecisionAnswer",
    "DecisionQuestion",
    "Dialog",
    "DialogChange",
    "Display",
    "ExtensionInfo",
    "ExtensionStateChange",
    "FileEdited",
    "FilesChanged",
    "Head",
    "HistoryChange",
    "Host",
    "HostAccount",
    "HostAgents",
    "HostApp",
    "HostAppState",
    "HostChat",
    "HostChatApprovals",
    "HostChatViews",
    "HostCommands",
    "HostExtensions",
    "HostHooks",
    "HostLoop",
    "HostMcp",
    "HostModels",
    "HostNotifications",
    "HostPermissions",
    "HostPlan",
    "HostPlugins",
    "HostPolicy",
    "HostProcesses",
    "HostScheduler",
    "HostSelf",
    "HostSessions",
    "HostTasks",
    "HostUi",
    "HostWindows",
    "HostWorkflows",
    "HostWorkspace",
    "Intercepts",
    "LocalNotification",
    "LoopStatus",
    "ModelChange",
    "ModelUsage",
    "ModelWrite",
    "Notice",
    "NotificationCounts",
    "NotificationEntry",
    "NotificationTarget",
    "Page",
    "Panel",
    "PanelChange",
    "PermissionModeChange",
    "Plan",
    "PlanChange",
    "PlanSubmitting",
    "Preview",
    "PreviewChange",
    "ProcessEvent",
    "Retry",
    "Route",
    "Run",
    "RunFailure",
    "RunPause",
    "RunRef",
    "RunStart",
    "SCHEMA_VERSION",
    "SchedulerEvent",
    "Scroll",
    "SelectionChange",
    "SelectionState",
    "SessionAttached",
    "SessionChange",
    "SessionCleared",
    "SessionInfo",
    "SessionList",
    "SessionNamed",
    "SettingsChange",
    "Sidebar",
    "SidebarChange",
    "StateChange",
    "Status",
    "TaskEvent",
    "TaskIteration",
    "TaskSnapshot",
    "Toast",
    "ToastChange",
    "Todos",
    "ToolCall",
    "ToolCalling",
    "ToolFailure",
    "ToolResult",
    "Typing",
    "UpdateInfo",
    "View",
    "ViewAction",
    "WindowInfo",
    "Withheld",
    "WorkflowRun",
]
