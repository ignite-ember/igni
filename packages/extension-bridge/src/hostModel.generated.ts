// GENERATED from the igni host schema 1.3.0 by ember_code.extensions.host.codegen — do not edit. Run `make host-sdks` after changing the schema.
//
// `bridge.host` as an extension's page sees it. Read
// `docs/HOST_OBJECT_MODEL.md` in igni for what each object is.

import { HostObject, Property, Signal, type HostLink, type JsonValue } from "./hostModel";

export const SCHEMA_VERSION = "1.3.0";

/** One change anywhere in the model, as `host.changed` reports it. */
export interface Change {
  /** The member that changed, `object.member`. */
  path: string;
  /** Its new value. */
  value: JsonValue;
  /** The window, for a per-window member. */
  window?: string | null;
}

/** How igni presents the conversation. */
export interface Display {
  /** Whether routing detail is shown. */
  verbose: boolean;
  /** The active output style; empty for none. */
  output_style: string;
  /** The styles that can be chosen. */
  output_styles: string[];
  /** `auto`, `light` or `dark`. */
  color_theme: string;
  show_reasoning: boolean;
  show_tool_calls: boolean;
}

/** Whether igni can reach a model and its server. */
export interface Connection {
  cloud_connected: boolean;
  /** False when runs would reach no model. */
  model_configured: boolean;
}

/** A newer igni than the one running. */
export interface UpdateInfo {
  current_version: string;
  latest_version: string;
  download_url: string;
}

/** One key of an extension's own state. */
export interface StateChange {
  key: string;
  /** `None` when the key was deleted. */
  value?: string | null;
}

/** One igni window: a desktop window, an IDE panel or a browser tab. */
export interface WindowInfo {
  /** Stable across reloads of the same window. */
  id: string;
  /** `tauri`, `vscode`, `jetbrains` or `web`. */
  kind: string;
  connected: boolean;
  /** Whether the person is using this window: it has focus. */
  focused: boolean;
  /** The session it shows. */
  session_id?: string | null;
  project_dir?: string | null;
}

/** One level of a window's page stack. */
export interface Page {
  /** `plugins`, `extensions`, an extension's page kind, … */
  kind: string;
  label: string;
  params?: Record<string, string>;
}

/** Where a window is: its page stack, empty for the chat. */
export interface Route {
  window: string;
  pages: Page[];
}

/** The panel open over a window's chat: `notifications`, `search` (find in the conversation), `login` or `dir-picker`. One at a time. */
export interface Panel {
  kind: string;
  params?: Record<string, JsonValue>;
}

/** A window's panel opened or closed. */
export interface PanelChange {
  window: string;
  panel?: Panel | null;
}

/** A window's sidebar. */
export interface Sidebar {
  open: boolean;
  /** Its width in pixels; `None` for igni's own. */
  width?: number | null;
  /** The part of it in view: `links` (the destinations) or `sessions`. */
  section?: string | null;
}

/** A window's sidebar changed. */
export interface SidebarChange {
  window: string;
  sidebar: Sidebar;
}

/** The file previewed in a window. */
export interface Preview {
  path: string;
}

/** A window's preview changed. */
export interface PreviewChange {
  window: string;
  preview?: Preview | null;
}

/** What is selected in a window. Each part is independent; `None` for nothing. */
export interface SelectionState {
  /** The highlighted message, by the run it belongs to (`run_id` in `chat.messages` and the run signals). */
  message_id?: string | null;
  /** Text selected in the transcript. */
  text?: string | null;
  /** The file last picked: a file pill, a reference. */
  file?: string | null;
}

/** A window's selection changed. */
export interface SelectionChange {
  window: string;
  selection: SelectionState;
}

/** A window's composer. */
export interface Composer {
  /** The draft, without the mode's `/` or `$`. */
  text: string;
  /** The files the draft refers to (`@path`), in order. */
  attachments?: string[];
  /** `chat`, `command` (a slash command) or `shell` (a `$` line). */
  mode?: string | null;
}

/** A window's draft changed. */
export interface ComposerChange {
  window: string;
  composer: Composer;
}

/** A transient message on screen. */
export interface Toast {
  id: string;
  /** Its first line. */
  text: string;
  /** `info`, `success`, `warning` or `error`. */
  kind: string;
  /** A second line. */
  body?: string | null;
  /** Where clicking it goes. */
  target?: Page | null;
  /** The extension that showed it; `None` for igni. */
  source?: string | null;
}

/** A toast appeared or went away. */
export interface ToastChange {
  window: string;
  toast: Toast;
}

/** A dialog on screen: igni's own (`spec.kind` is `update`) or an extension's (`show_dialog`). One at a time; a new one replaces the last, which closes unanswered. */
export interface Dialog {
  id: string;
  spec: JsonValue;
  /** The extension that asked; `None` for igni. */
  source?: string | null;
}

/** A dialog opened or closed. */
export interface DialogChange {
  window: string;
  dialog: Dialog;
  /** What the person answered, when it closed: `{button, value}`; `None` when it was dismissed or replaced. */
  answer?: JsonValue;
}

/** Where a window's transcript is scrolled to. */
export interface Scroll {
  /** The message at the top of the view, by its run; `None` for none. */
  message_id?: string | null;
  /** Following the newest message. */
  at_bottom: boolean;
  /** The window, in a signal. */
  window?: string | null;
}

/** One session, as the session list shows it. */
export interface SessionInfo {
  session_id: string;
  name: string;
  created_at: number;
  updated_at: number;
  run_count: number;
  summary: string;
  agent_name: string;
}

/** A window moved to another session. */
export interface SessionChange {
  session_id: string;
  window?: string | null;
}

/** The session list, as it was just read. */
export interface SessionList {
  sessions: SessionInfo[];
}

/** `/clear`: a session carries on under a new id. */
export interface SessionCleared {
  session_id: string;
  new_session_id: string;
  summary: string;
}

/** A session was given its name. */
export interface SessionNamed {
  session_id: string;
  name: string;
}

/** A session bound, and the folder it works in. */
export interface SessionAttached {
  session_id: string;
  project_dir: string;
}

/** A message posted in a chat. */
export interface ChatMessage {
  session_id: string;
  text: string;
  /** For the next turn rather than now. */
  queued: boolean;
  /** The extension that posted it; `None` for the person. */
  from_extension?: string | null;
}

/** Turns were removed from a session's history. */
export interface HistoryChange {
  session_id: string;
  /** `truncated` or `discarded`. */
  what: string;
  /** The run removed from, or the message discarded. */
  run_id: string;
}

/** A piece of the agent's answer as it streams. */
export interface ContentDelta {
  session_id: string;
  text: string;
  thinking: boolean;
}

/** Which run. */
export interface RunRef {
  session_id: string;
  run_id: string;
  /** Set for a sub-agent's run. */
  parent_run_id: string;
}

/** A run started. */
export interface RunStart {
  session_id: string;
  run_id: string;
  parent_run_id: string;
  agent_name: string;
  model: string;
}

/** A finished run. */
export interface Run {
  session_id: string;
  run_id: string;
  parent_run_id: string;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  /** Seconds. */
  duration: number;
  /** Files the run edited, relative to the project — only those within the extension's workspace grant, and none without one. */
  changed_files: string[];
}

/** A run stopped with an error. */
export interface RunFailure {
  session_id: string;
  error: string;
}

/** The agent asking to act. */
export interface Approval {
  requirement_id: string;
  session_id: string;
  tool_name: string;
  friendly_name: string;
  tool_args: JsonValue;
  details: string;
  /** Which sub-agent is asking, outermost first. */
  agent_path: string[];
}

/** A run waiting on approvals. */
export interface RunPause {
  session_id: string;
  run_id: string;
  requirements: Approval[];
}

/** An approval was answered, by anyone. */
export interface ApprovalResolved {
  session_id: string;
  requirement_id: string;
}

/** The reason for an approval, arriving after it. */
export interface ApprovalDetails {
  session_id: string;
  requirement_id: string;
  details: string;
}

/** One answer to one approval. */
export interface ApprovalAnswer {
  requirement_id: string;
  /** `confirm` or `reject`. */
  action: string;
  /** `once`, `always` or `similar`; empty with `reject`. */
  choice?: string;
}

/** The agent started calling a tool. */
export interface ToolCall {
  session_id: string;
  run_id: string;
  parent_run_id: string;
  tool_name: string;
  friendly_name: string;
  args_summary: string;
}

/** A tool call finished. */
export interface ToolResult {
  session_id: string;
  run_id: string;
  parent_run_id: string;
  summary: string;
  is_error: boolean;
  is_rejected: boolean;
}

/** A tool call failed. */
export interface ToolFailure {
  session_id: string;
  run_id: string;
  parent_run_id: string;
  error: string;
}

/** One model call's tokens. */
export interface ModelUsage {
  session_id: string;
  run_id: string;
  parent_run_id: string;
  input_tokens: number;
  output_tokens: number;
}

/** Someone's draft in a session, live. */
export interface Typing {
  session_id: string;
  text: string;
  window?: string | null;
}

/** A session's status line. */
export interface Status {
  session_id: string;
  model: string;
  model_configured: boolean;
  permission_mode: string;
  input_tokens: number;
  output_tokens: number;
  context_tokens: number;
  max_context: number;
}

/** An info or error line in a session. */
export interface Notice {
  session_id: string;
  text: string;
}

/** How full the context is, and with what. */
export interface ContextUsage {
  tokens: number;
  max: number;
  breakdown: JsonValue;
}

/** The agent wrote a file. */
export interface FileEdited {
  session_id: string;
  /** Relative to the project; only within the workspace grant. */
  path: string;
}

/** Progress of a sub-agent or a team, as the transcript shows it. */
export interface AgentEvent {
  session_id: string;
  kind: string;
  payload: JsonValue;
}

/** A network call being retried, and whether it recovered. */
export interface Retry {
  session_id: string;
  /** `attempt` or `succeeded`. */
  kind: string;
  payload: JsonValue;
}

/** A json-render block in the transcript. */
export interface View {
  view_id: string;
  session_id: string;
  title: string;
  /** A json-render spec: `{root, elements}`. */
  spec: JsonValue;
  /** `visualizer`, or the extension that appended it. */
  source: string;
}

/** The person used a control inside a view. */
export interface ViewAction {
  session_id: string;
  action: string;
  params: JsonValue;
}

/** The plan the agent proposed in plan mode. */
export interface Plan {
  latest: string;
  history: string[];
  tasks: JsonValue[];
  /** `pending`, or empty when there is none. */
  state: string;
}

/** A plan was submitted or decided. */
export interface PlanChange {
  session_id: string;
  /** `submitted` or `decided`. */
  kind: string;
  payload: JsonValue;
}

/** A sub-agent task created or updated. */
export interface TaskEvent {
  session_id: string;
  task_id: string;
  title: string;
  assignee: string;
  status: string;
}

/** A task loop's iteration count. */
export interface TaskIteration {
  session_id: string;
  iteration: number;
  max_iterations: number;
}

/** Every task, as it now stands. */
export interface TaskSnapshot {
  session_id: string;
  tasks: JsonValue[];
}

/** The agent's todo list. */
export interface Todos {
  session_id: string;
  todos: JsonValue[];
}

/** `/loop`, as it stands. */
export interface LoopStatus {
  active: boolean;
  paused: boolean;
  prompt: string;
  iteration_index: number;
  iterations_remaining: number;
  cap_explicit: boolean;
}

/** What a slash command answered. */
export interface CommandOutcome {
  kind: string;
  content: string;
  display_content: string;
}

/** Something in a collection changed; read it again. */
export interface CollectionChange {
  /** What happened: `connected`, `installed`, `event`, … */
  what: string;
  /** Which item, when there is one. */
  name?: string | null;
  /** The host's report of it. */
  payload?: JsonValue;
}

/** A workflow started. */
export interface WorkflowRun {
  workflow_run_id: string;
  name: string;
}

/** A scheduled task started, finished or changed. */
export interface SchedulerEvent {
  kind: string;
  task_id: string;
  description: string;
  result: string;
}

/** A background shell started, printed, or exited. */
export interface ProcessEvent {
  /** `started`, `line`, `exited` or `done`. */
  kind: string;
  payload: JsonValue;
}

/** The project's git HEAD. */
export interface Head {
  commit_sha: string;
  branch: string;
}

/** Files git sees as changed, within the extension's workspace grant. */
export interface FilesChanged {
  paths: string[];
  /** The list was capped: rescan rather than trust it. */
  truncated: boolean;
}

/** The model in use changed. */
export interface ModelChange {
  current: string;
}

/** An installed extension. */
export interface ExtensionInfo {
  id: string;
  name: string;
  version: string;
  /** `PREPARING`, `READY`, `FAILED` or `DISABLED`. */
  state: string;
  /** Why it is not READY, and what to do. */
  reason: string;
  /** Its home on the web — its service's URL, which is the service's `client_uri` — when one is known. */
  web_url?: string | null;
}

/** An extension's health moved. */
export interface ExtensionStateChange {
  extension_id: string;
  state: string;
  reason: string;
}

/** The session's permission mode changed. */
export interface PermissionModeChange {
  mode: string;
  previous: string;
}

/** Who is signed in to igni, without any credential. */
export interface Account {
  signed_in: boolean;
  email: string;
  name: string;
  org: string;
}

/** Where opening a notification goes: one of an extension's pages. */
export interface NotificationTarget {
  page: string;
  params?: Record<string, string>;
}

/** One notification, from igni server or from an extension on this machine. */
export interface NotificationEntry {
  id: string;
  /** `server` or `local`. */
  origin: string;
  /** The extension it belongs to. */
  source: string;
  kind: string;
  title: string;
  body: string;
  actor: string;
  target?: NotificationTarget | null;
  /** ISO 8601. */
  created_at?: string | null;
  read: boolean;
}

/** Unread notifications, in all and per extension. */
export interface NotificationCounts {
  unread: number;
  by_source: Record<string, number>;
}

/** Something to tell the person at this machine. It never leaves it. */
export interface LocalNotification {
  /** The extension's word for what happened. */
  kind: string;
  title: string;
  /** One line. */
  body?: string;
  /** One of this extension's pages. */
  target: NotificationTarget;
  /** The same event is shown once, however often it is sent. */
  event_key: string;
}

/** An extension's settings, as now configured. */
export interface SettingsChange {
  values: Record<string, JsonValue>;
}

/** One typed question about a context. It quotes the context's fields by name in backticks, as in: Is `diff` only a refactor? */
export interface DecisionQuestion {
  /** Its name in the answers: a lowercase name. */
  id: string;
  /** `choice` (one of `options`), `score` (one of `levels`, lowest first) or `noul` (yes or no — Laya's name for it). */
  kind: string;
  /** The question, up to 500 characters. */
  question: string;
  /** `choice` only: 2–20 labels, each to what it means (empty when the label says it). */
  options?: Record<string, string> | null;
  /** `score` only: 2–10 level labels, lowest first. */
  levels?: string[] | null;
}

/** One question's answer, with the probabilities it came from. */
export interface DecisionAnswer {
  kind: string;
  /** The option chosen, the level reached, or `yes` / `no`. */
  answer: string;
  /** The probability of that answer. */
  confidence: number;
  /** Every option's, level's, or `yes` and `no`'s probability. */
  probabilities: Record<string, number>;
  /** `score` only: the expected level, 0 the lowest. */
  score?: number | null;
}

/** Every question's answer about one context. */
export interface Decision {
  /** By question id. */
  answers: Record<string, DecisionAnswer>;
  /** The Laya checkpoint that answered: `english` or `multilingual`. */
  model: string;
}

/** Key–value state of the extension's own, kept by the host across restarts. An extension sees only its own keys. */
export class HostAppState extends HostObject {
  static readonly PATH = "app.state";
  /** Every key and value. */
  readonly values: Property<Record<string, string>>;
  /** A key was set or deleted. */
  readonly changed: Signal<StateChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "app.state", window);
    this.values = new Property(this, "values", false);
    this.changed = new Signal("app.state.changed");
  }

  /** One value, or `None`. */
  get(key: string): Promise<string | null> {
    return this.call<string | null>("get", { key });
  }

  /** Store one value. */
  set(key: string, value: string): Promise<void> {
    return this.call<void>("set", { key, value });
  }

  /** Remove one key. */
  delete(key: string): Promise<void> {
    return this.call<void>("delete", { key });
  }
}

/** The agent asking to act, and the answers. */
export class HostChatApprovals extends HostObject {
  static readonly PATH = "chat.approvals";
  /** Requests not yet answered. */
  readonly waiting: Property<Approval[]>;
  /** The agent asked. */
  readonly requested: Signal<Approval>;
  /** Somebody answered. */
  readonly resolved: Signal<ApprovalResolved>;
  /** The reason arrived. */
  readonly details: Signal<ApprovalDetails>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "chat.approvals", window);
    this.waiting = new Property(this, "waiting", false);
    this.requested = new Signal("chat.approvals.requested");
    this.resolved = new Signal("chat.approvals.resolved");
    this.details = new Signal("chat.approvals.details");
  }

  /** Answer one request, as the person's dialog does. */
  answer(requirement_id: string, action: string, choice?: string): Promise<void> {
    return this.call<void>("answer", { requirement_id, action, choice });
  }

  /** Answer every request of a pause at once. */
  answer_all(answers: ApprovalAnswer[]): Promise<void> {
    return this.call<void>("answer_all", { answers });
  }
}

/** json-render blocks in the transcript — the visualizer's and extensions'. */
export class HostChatViews extends HostObject {
  static readonly PATH = "chat.views";
  /** The views in the current session. */
  readonly list: Property<View[]>;
  /** A view appeared or was replaced. */
  readonly shown: Signal<View>;
  /** The person used a control in a view. */
  readonly action: Signal<ViewAction>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "chat.views", window);
    this.list = new Property(this, "list", false);
    this.shown = new Signal("chat.views.shown");
    this.action = new Signal("chat.views.action");
  }

  /** Act on a view as if the person had. */
  dispatch(view_id: string, action: string, params?: Record<string, JsonValue>): Promise<JsonValue> {
    return this.call<JsonValue>("dispatch", { view_id, action, params });
  }
}

/** igni itself: how it presents things, which version, whether it is connected. */
export class HostApp extends HostObject {
  static readonly PATH = "app";
  readonly state: HostAppState;
  /** Theme, verbosity, output style. */
  readonly display: Property<Display>;
  /** The igni version running. */
  readonly version: Property<string>;
  /** A newer igni, as last checked; `None` when there is none or it was not checked yet. */
  readonly update_available: Property<UpdateInfo | null>;
  /** Model and server reachability. */
  readonly connection: Property<Connection>;
  /** Verbosity or output style changed. */
  readonly display_changed: Signal<Display>;
  /** The connection changed. */
  readonly connection_changed: Signal<Connection>;
  /** An update check answered. */
  readonly update_changed: Signal<UpdateInfo | null>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "app", window);
    this.state = new HostAppState(link, window);
    this.display = new Property(this, "display", false);
    this.version = new Property(this, "version", false);
    this.update_available = new Property(this, "update_available", false);
    this.connection = new Property(this, "connection", false);
    this.display_changed = new Signal("app.display_changed");
    this.connection_changed = new Signal("app.connection_changed");
    this.update_changed = new Signal("app.update_changed");
  }

  /** Show or hide routing detail. */
  set_verbose(on: boolean): Promise<Display> {
    return this.call<Display>("set_verbose", { on });
  }

  /** Switch the output style, as `/output-style <name>` does. */
  set_output_style(name: string): Promise<Display> {
    return this.call<Display>("set_output_style", { name });
  }

  /** Ask whether a newer igni exists. */
  check_for_update(): Promise<UpdateInfo | null> {
    return this.call<UpdateInfo | null>("check_for_update", {});
  }

  /** Open an https link in the person's browser, the way igni opens every link outside it. The window asked about opens it: a page's own, else the active one. Anything but an absolute `https://` URL with a host and no credentials in it is refused (`invalid`). */
  open_external(url: string): Promise<void> {
    return this.call<void>("open_external", { url });
  }
}

/** Every open igni window. Address one window's UI with `host.windows.ui(id)`. */
export class HostWindows extends HostObject {
  static readonly PATH = "windows";
  /** Every window connected now. */
  readonly list: Property<WindowInfo[]>;
  /** The window the person is using — the one with focus; `None` when igni is in the background. */
  readonly focused: Property<string | null>;
  /** A window connected. */
  readonly opened: Signal<WindowInfo>;
  /** A window went away. */
  readonly closed: Signal<WindowInfo>;
  /** A window took focus or lost it; `focused` says which. */
  readonly focus_changed: Signal<WindowInfo>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "windows", window);
    this.list = new Property(this, "list", false);
    this.focused = new Property(this, "focused", false);
    this.opened = new Signal("windows.opened");
    this.closed = new Signal("windows.closed");
    this.focus_changed = new Signal("windows.focus_changed");
  }

  /** `host.ui` of one window, by its id. */
  ui(windowId: string): HostUi {
    return new HostUi(this.link, windowId);
  }
}

/** What a window shows — the model its views bind to. `host.ui` is the active window, the one the person last used; `host.windows.ui(id)` any window. igni's own views write here too, by the same path, so every change below is a signal whoever made it. */
export class HostUi extends HostObject {
  static readonly PATH = "ui";
  /** The page stack; empty when the chat is showing. Writable: setting it navigates. */
  readonly route: Property<Page[]>;
  /** The panel open over the chat. Writable: setting it opens one, `None` closes it. */
  readonly panel: Property<Panel | null>;
  /** The sidebar. Writable. */
  readonly sidebar: Property<Sidebar>;
  /** The file previewed. Writable: `None` closes it. */
  readonly preview: Property<Preview | null>;
  /** What is selected. Writable. */
  readonly selection: Property<SelectionState>;
  /** The composer: draft, the files it refers to, its mode. Writable. */
  readonly composer: Property<Composer>;
  /** Toasts on screen, oldest first. */
  readonly toasts: Property<Toast[]>;
  /** The open dialog. */
  readonly dialog: Property<Dialog | null>;
  /** Where the transcript is. */
  readonly scroll: Property<Scroll>;
  /** The page stack changed. */
  readonly navigated: Signal<Route>;
  /** A panel opened or closed. */
  readonly panel_changed: Signal<PanelChange>;
  /** The sidebar changed. */
  readonly sidebar_changed: Signal<SidebarChange>;
  /** The preview opened, changed or closed. */
  readonly preview_changed: Signal<PreviewChange>;
  /** The selection changed. */
  readonly selection_changed: Signal<SelectionChange>;
  /** The draft changed. */
  readonly composer_changed: Signal<ComposerChange>;
  /** A toast appeared. */
  readonly toast_shown: Signal<ToastChange>;
  /** A toast went away. */
  readonly toast_dismissed: Signal<ToastChange>;
  /** A dialog opened. */
  readonly dialog_opened: Signal<DialogChange>;
  /** A dialog closed, with the answer. */
  readonly dialog_closed: Signal<DialogChange>;
  /** The transcript came to rest somewhere else, or started or stopped following the newest message. */
  readonly scrolled: Signal<Scroll>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "ui", window);
    this.route = new Property(this, "route", true);
    this.panel = new Property(this, "panel", true);
    this.sidebar = new Property(this, "sidebar", true);
    this.preview = new Property(this, "preview", true);
    this.selection = new Property(this, "selection", true);
    this.composer = new Property(this, "composer", true);
    this.toasts = new Property(this, "toasts", false);
    this.dialog = new Property(this, "dialog", false);
    this.scroll = new Property(this, "scroll", false);
    this.navigated = new Signal("ui.navigated");
    this.panel_changed = new Signal("ui.panel_changed");
    this.sidebar_changed = new Signal("ui.sidebar_changed");
    this.preview_changed = new Signal("ui.preview_changed");
    this.selection_changed = new Signal("ui.selection_changed");
    this.composer_changed = new Signal("ui.composer_changed");
    this.toast_shown = new Signal("ui.toast_shown");
    this.toast_dismissed = new Signal("ui.toast_dismissed");
    this.dialog_opened = new Signal("ui.dialog_opened");
    this.dialog_closed = new Signal("ui.dialog_closed");
    this.scrolled = new Signal("ui.scrolled");
  }

  /** Open a page at the top of the stack, as a header link does. */
  navigate(page: string, params?: Record<string, string>): Promise<Page[]> {
    return this.call<Page[]>("navigate", { page, params });
  }

  /** Go back one level; at the root, back to the chat. */
  back(): Promise<Page[]> {
    return this.call<Page[]>("back", {});
  }

  /** Open a panel over the chat, closing the one that was open. */
  open_panel(kind: string, params?: Record<string, JsonValue>): Promise<Panel> {
    return this.call<Panel>("open_panel", { kind, params });
  }

  /** Close the panel. */
  close_panel(): Promise<void> {
    return this.call<void>("close_panel", {});
  }

  /** Open or close the sidebar, and size it or bring a section into view. */
  set_sidebar(open: boolean, width?: number, section?: string): Promise<Sidebar> {
    return this.call<Sidebar>("set_sidebar", { open, width, section });
  }

  /** Preview a file of the project. (`preview` is the property.) */
  open_preview(path: string): Promise<Preview> {
    return this.call<Preview>("open_preview", { path });
  }

  /** Close the preview. */
  close_preview(): Promise<void> {
    return this.call<void>("close_preview", {});
  }

  /** Select a message, text or file. The parts left out are cleared. */
  select(selection: SelectionState): Promise<SelectionState> {
    return this.call<SelectionState>("select", { selection });
  }

  /** Replace the draft. */
  set_composer(text: string, attachments?: string[], mode?: string): Promise<Composer> {
    return this.call<Composer>("set_composer", { text, attachments, mode });
  }

  /** Show a transient message, labelled as this extension's. */
  toast(text: string, kind?: string, body?: string, target?: Page): Promise<Toast> {
    return this.call<Toast>("toast", { text, kind, body, target });
  }

  /** Take one of this extension's toasts away. */
  dismiss_toast(toast_id: string): Promise<void> {
    return this.call<void>("dismiss_toast", { toast_id });
  }

  /** Show a dialog and wait for the person's answer. `spec` is `{title, body, buttons: [{id, label, primary}], input: {label, placeholder, value}}` — `buttons` defaults to one OK, `input` is optional. Answers `{button, value}`, or `None` when the dialog was dismissed, or replaced by another. (`dialog` is the property.) */
  show_dialog(spec: JsonValue): Promise<JsonValue> {
    return this.call<JsonValue>("show_dialog", { spec });
  }

  /** Scroll the transcript to a message, by its run; `bottom` for the newest. */
  scroll_to(message_id: string): Promise<Scroll> {
    return this.call<Scroll>("scroll_to", { message_id });
  }
}

/** The sessions of this project, and which one the active window shows. */
export class HostSessions extends HostObject {
  static readonly PATH = "sessions";
  /** Every session, newest first. */
  readonly list: Property<SessionInfo[]>;
  /** The session the active window shows. Writable: setting it switches, and is refused where the person's switch would be. */
  readonly current: Property<string>;
  /** A window moved to another session. */
  readonly current_changed: Signal<SessionChange>;
  /** The session list was read. */
  readonly listed: Signal<SessionList>;
  /** `/clear` renewed a session. */
  readonly cleared: Signal<SessionCleared>;
  /** A session got its name. */
  readonly named: Signal<SessionNamed>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "sessions", window);
    this.list = new Property(this, "list", false);
    this.current = new Property(this, "current", true);
    this.current_changed = new Signal("session.switched");
    this.listed = new Signal("sessions.listed");
    this.cleared = new Signal("sessions.cleared");
    this.named = new Signal("sessions.named");
  }

  /** Show a session in the active window. */
  open(session_id: string): Promise<SessionChange> {
    return this.call<SessionChange>("open", { session_id });
  }

  /** Start a new session, in a folder or in this project. */
  create(project_dir?: string): Promise<SessionAttached> {
    return this.call<SessionAttached>("create", { project_dir });
  }

  /** Make sure a session is live, without showing it. */
  attach(session_id: string, project_dir?: string): Promise<SessionAttached> {
    return this.call<SessionAttached>("attach", { session_id, project_dir });
  }
}

/** The conversation in the current session. Signals carry the session they happened in, and arrive for every live session. */
export class HostChat extends HostObject {
  static readonly PATH = "chat";
  readonly approvals: HostChatApprovals;
  readonly views: HostChatViews;
  /** The current session. */
  readonly session_id: Property<string>;
  /** The transcript, turn by turn. */
  readonly messages: Property<JsonValue[]>;
  /** Messages waiting for a turn. */
  readonly pending: Property<JsonValue[]>;
  /** Whether a run is going. */
  readonly processing: Property<boolean>;
  /** The status line. */
  readonly status: Property<Status>;
  /** Seconds a run may take. */
  readonly timeout: Property<number>;
  /** Runs that stopped part-way. */
  readonly interrupted_runs: Property<JsonValue[]>;
  /** Token counts and what fills them. */
  readonly context: Property<ContextUsage>;
  /** A message was posted — by the person or an extension. */
  readonly message_added: Signal<ChatMessage>;
  /** The agent's answer, streaming. */
  readonly content_delta: Signal<ContentDelta>;
  /** The agent started thinking. */
  readonly reasoning_started: Signal<RunRef>;
  /** A run started. */
  readonly run_started: Signal<RunStart>;
  /** A run finished, with the files it changed. */
  readonly run_completed: Signal<Run>;
  /** A run stopped with an error. */
  readonly run_error: Signal<RunFailure>;
  /** A run is waiting for approvals. */
  readonly run_paused: Signal<RunPause>;
  /** The answer finished streaming. */
  readonly streaming_done: Signal<RunRef>;
  /** Somebody's draft, live. */
  readonly typing: Signal<Typing>;
  /** The status line changed. */
  readonly status_changed: Signal<Status>;
  /** An info line. */
  readonly info: Signal<Notice>;
  /** An error line. */
  readonly error: Signal<Notice>;
  /** The agent called a tool. */
  readonly tool_started: Signal<ToolCall>;
  /** A tool call finished. */
  readonly tool_completed: Signal<ToolResult>;
  /** A tool call failed. */
  readonly tool_error: Signal<ToolFailure>;
  /** A model call finished. */
  readonly model_completed: Signal<ModelUsage>;
  /** The agent wrote a file. */
  readonly file_edited: Signal<FileEdited>;
  /** A sub-agent or team moved on. */
  readonly agent_event: Signal<AgentEvent>;
  /** A network call is being retried. */
  readonly retry: Signal<Retry>;
  /** Turns were truncated or discarded. */
  readonly history_changed: Signal<HistoryChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "chat", window);
    this.approvals = new HostChatApprovals(link, window);
    this.views = new HostChatViews(link, window);
    this.session_id = new Property(this, "session_id", false);
    this.messages = new Property(this, "messages", false);
    this.pending = new Property(this, "pending", false);
    this.processing = new Property(this, "processing", false);
    this.status = new Property(this, "status", false);
    this.timeout = new Property(this, "timeout", false);
    this.interrupted_runs = new Property(this, "interrupted_runs", false);
    this.context = new Property(this, "context", false);
    this.message_added = new Signal("chat.message_added");
    this.content_delta = new Signal("chat.content_delta");
    this.reasoning_started = new Signal("chat.reasoning_started");
    this.run_started = new Signal("chat.run_started");
    this.run_completed = new Signal("chat.run_completed");
    this.run_error = new Signal("chat.run_error");
    this.run_paused = new Signal("chat.run_paused");
    this.streaming_done = new Signal("chat.streaming_done");
    this.typing = new Signal("chat.typing");
    this.status_changed = new Signal("chat.status_changed");
    this.info = new Signal("chat.info");
    this.error = new Signal("chat.error");
    this.tool_started = new Signal("chat.tool_started");
    this.tool_completed = new Signal("chat.tool_completed");
    this.tool_error = new Signal("chat.tool_error");
    this.model_completed = new Signal("chat.model_completed");
    this.file_edited = new Signal("chat.file_edited");
    this.agent_event = new Signal("chat.agent_event");
    this.retry = new Signal("chat.retry");
    this.history_changed = new Signal("chat.history_changed");
  }

  /** Post a message in the chat, labelled as this extension. The agent sees it and answers in the chat as usual. */
  post(text: string, attachments?: Record<string, string>): Promise<void> {
    return this.call<void>("post", { text, attachments });
  }

  /** Post a message for the next turn, labelled as this extension. */
  queue(text: string): Promise<void> {
    return this.call<void>("queue", { text });
  }

  /** Show a json-render block in the transcript. Shown to the person, never sent to the model. Returns its id. */
  append(view: JsonValue, title?: string): Promise<string> {
    return this.call<string>("append", { view, title });
  }

  /** Replace a block this extension appended. */
  replace(view_id: string, view: JsonValue, title?: string): Promise<void> {
    return this.call<void>("replace", { view_id, view, title });
  }

  /** Search the transcript. */
  search(query: string, limit?: number): Promise<JsonValue[]> {
    return this.call<JsonValue[]>("search", { query, limit });
  }

  /** Stop the run. */
  cancel(): Promise<void> {
    return this.call<void>("cancel", {});
  }

  /** Stop one sub-agent's run. */
  cancel_agent(run_id: string): Promise<void> {
    return this.call<void>("cancel_agent", { run_id });
  }

  /** Remove a run and everything after it. */
  truncate(run_id: string): Promise<JsonValue> {
    return this.call<JsonValue>("truncate", { run_id });
  }

  /** Compact the conversation if it needs it, as igni does after a run. */
  compact(): Promise<SessionCleared | null> {
    return this.call<SessionCleared | null>("compact", {});
  }

  /** Have the agent note what a turn taught it. */
  extract_learnings(user_msg: string, assistant_msg: string): Promise<void> {
    return this.call<void>("extract_learnings", { user_msg, assistant_msg });
  }

  /** Forget an interrupted run. */
  discard_interrupted(message_id: string): Promise<JsonValue> {
    return this.call<JsonValue>("discard_interrupted", { message_id });
  }

  /** Upload an attachment for a later `post`. */
  upload(filename: string, content_base64: string): Promise<JsonValue> {
    return this.call<JsonValue>("upload", { filename, content_base64 });
  }
}

/** Plan mode: the plan the agent proposed. */
export class HostPlan extends HostObject {
  static readonly PATH = "plan";
  /** The latest plan. */
  readonly latest: Property<Plan>;
  /** A plan was submitted or decided. */
  readonly changed: Signal<PlanChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "plan", window);
    this.latest = new Property(this, "latest", false);
    this.changed = new Signal("plan.changed");
  }

  /** Approve the plan. */
  approve(run_id?: string): Promise<JsonValue> {
    return this.call<JsonValue>("approve", { run_id });
  }

  /** Send the plan back. */
  dismiss(run_id?: string): Promise<JsonValue> {
    return this.call<JsonValue>("dismiss", { run_id });
  }
}

/** The agent's todo list and its sub-agent tasks. */
export class HostTasks extends HostObject {
  static readonly PATH = "tasks";
  /** The todo list. */
  readonly todos: Property<JsonValue[]>;
  /** A task was created. */
  readonly created: Signal<TaskEvent>;
  /** A task changed. */
  readonly updated: Signal<TaskEvent>;
  /** A task loop moved on. */
  readonly iteration: Signal<TaskIteration>;
  /** Every task, as it now stands. */
  readonly snapshot: Signal<TaskSnapshot>;
  /** The todo list changed. */
  readonly todos_changed: Signal<Todos>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "tasks", window);
    this.todos = new Property(this, "todos", false);
    this.created = new Signal("tasks.created");
    this.updated = new Signal("tasks.updated");
    this.iteration = new Signal("tasks.iteration");
    this.snapshot = new Signal("tasks.snapshot");
    this.todos_changed = new Signal("tasks.todos_changed");
  }
}

/** `/loop`: a prompt repeated until done. */
export class HostLoop extends HostObject {
  static readonly PATH = "loop";
  /** Where it is. */
  readonly status: Property<LoopStatus>;
  /** It paused, resumed, advanced or stopped. */
  readonly changed: Signal<LoopStatus>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "loop", window);
    this.status = new Property(this, "status", false);
    this.changed = new Signal("loop.changed");
  }

  /** Pause after this iteration. */
  pause(): Promise<boolean> {
    return this.call<boolean>("pause", {});
  }

  /** Carry on. */
  resume(): Promise<string> {
    return this.call<string>("resume", {});
  }

  /** Stop the loop. */
  cancel_pending(): Promise<boolean> {
    return this.call<boolean>("cancel_pending", {});
  }

  /** Take the next iteration. */
  pop_pending(): Promise<JsonValue> {
    return this.call<JsonValue>("pop_pending", {});
  }
}

/** Slash commands and skills. */
export class HostCommands extends HostObject {
  static readonly PATH = "commands";
  /** Every slash command. */
  readonly list: Property<JsonValue[]>;
  /** Skill names. */
  readonly skills: Property<string[]>;
  /** Each skill's description and prompt. */
  readonly skill_definitions: Property<JsonValue[]>;
  /** Each skill's details. */
  readonly skill_details: Property<JsonValue[]>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "commands", window);
    this.list = new Property(this, "list", false);
    this.skills = new Property(this, "skills", false);
    this.skill_definitions = new Property(this, "skill_definitions", false);
    this.skill_details = new Property(this, "skill_details", false);
  }

  /** Run a slash command, as the extension. The answer comes back here. */
  run(command: string): Promise<CommandOutcome> {
    return this.call<CommandOutcome>("run", { command });
  }
}

/** Agent definitions. */
export class HostAgents extends HostObject {
  static readonly PATH = "agents";
  /** Every agent. */
  readonly details: Property<JsonValue[]>;
  /** An agent was promoted or discarded. */
  readonly changed: Signal<CollectionChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "agents", window);
    this.details = new Property(this, "details", false);
    this.changed = new Signal("agents.changed");
  }

  /** Keep an agent made for one task. */
  promote_ephemeral(name: string): Promise<JsonValue> {
    return this.call<JsonValue>("promote_ephemeral", { name });
  }

  /** Drop an agent made for one task. */
  discard_ephemeral(name: string): Promise<JsonValue> {
    return this.call<JsonValue>("discard_ephemeral", { name });
  }
}

/** Workflows: scripted runs. */
export class HostWorkflows extends HostObject {
  static readonly PATH = "workflows";
  /** Every workflow. */
  readonly list: Property<JsonValue[]>;
  /** A workflow run moved on. */
  readonly changed: Signal<CollectionChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "workflows", window);
    this.list = new Property(this, "list", false);
    this.changed = new Signal("workflows.changed");
  }

  /** Start a workflow. */
  run(name: string, input?: Record<string, JsonValue>): Promise<WorkflowRun> {
    return this.call<WorkflowRun>("run", { name, input });
  }

  /** Stop a workflow run. */
  cancel(workflow_run_id: string): Promise<boolean> {
    return this.call<boolean>("cancel", { workflow_run_id });
  }
}

/** Scheduled tasks. */
export class HostScheduler extends HostObject {
  static readonly PATH = "scheduler";
  /** Every scheduled task. */
  readonly tasks: Property<JsonValue[]>;
  /** A task started, finished or changed. */
  readonly event: Signal<SchedulerEvent>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "scheduler", window);
    this.tasks = new Property(this, "tasks", false);
    this.event = new Signal("scheduler.event");
  }

  /** Start the scheduler. */
  start(): Promise<void> {
    return this.call<void>("start", {});
  }

  /** Run a scheduled task now. */
  run_now(task_id: string): Promise<string> {
    return this.call<string>("run_now", { task_id });
  }

  /** Cancel a scheduled task. */
  cancel(task_id: string): Promise<void> {
    return this.call<void>("cancel", { task_id });
  }
}

/** Background shells. */
export class HostProcesses extends HostObject {
  static readonly PATH = "processes";
  /** Every background shell. */
  readonly list: Property<JsonValue[]>;
  /** A shell started, printed or exited. */
  readonly changed: Signal<ProcessEvent>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "processes", window);
    this.list = new Property(this, "list", false);
    this.changed = new Signal("processes.changed");
  }

  /** A shell's last lines. */
  tail(pid: number, lines?: number): Promise<JsonValue> {
    return this.call<JsonValue>("tail", { pid, lines });
  }

  /** Stop a shell. */
  stop(pid: number): Promise<JsonValue> {
    return this.call<JsonValue>("stop", { pid });
  }

  /** Run a command in the project and wait for it. */
  run(command: string): Promise<JsonValue> {
    return this.call<JsonValue>("run", { command });
  }
}

/** The project, as far as the extension's workspace grant reaches. */
export class HostWorkspace extends HostObject {
  static readonly PATH = "workspace";
  /** The project directory. */
  readonly root: Property<string>;
  /** Git HEAD; `None` outside a repository. */
  readonly head: Property<Head | null>;
  /** HEAD moved. */
  readonly head_changed: Signal<Head>;
  /** Files changed, as git sees them, within the grant. */
  readonly files_changed: Signal<FilesChanged>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "workspace", window);
    this.root = new Property(this, "root", false);
    this.head = new Property(this, "head", false);
    this.head_changed = new Signal("workspace.head-changed");
    this.files_changed = new Signal("workspace.files-changed");
  }

  /** Read a file in the project. */
  read(path: string): Promise<JsonValue> {
    return this.call<JsonValue>("read", { path });
  }

  /** List a directory in the project. */
  list(dir?: string): Promise<JsonValue> {
    return this.call<JsonValue>("list", { dir });
  }

  /** Complete a path, as `@` does. */
  complete(prefix: string): Promise<JsonValue> {
    return this.call<JsonValue>("complete", { prefix });
  }

  /** Search the code. */
  search(query: string): Promise<JsonValue> {
    return this.call<JsonValue>("search", { query });
  }

  /** Ask the person to pick a folder. */
  pick_dir(): Promise<JsonValue> {
    return this.call<JsonValue>("pick_dir", {});
  }
}

/** Models. */
export class HostModels extends HostObject {
  static readonly PATH = "models";
  /** Every model igni knows. */
  readonly registry: Property<JsonValue>;
  /** The model in use. Writable: setting it switches. */
  readonly current: Property<string>;
  /** The model in use changed. */
  readonly changed: Signal<ModelChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "models", window);
    this.registry = new Property(this, "registry", false);
    this.current = new Property(this, "current", true);
    this.changed = new Signal("models.changed");
  }
}

/** MCP servers. */
export class HostMcp extends HostObject {
  static readonly PATH = "mcp";
  /** Every server. */
  readonly servers: Property<JsonValue[]>;
  /** Each server's status. */
  readonly status: Property<JsonValue[]>;
  /** Each server's tools and details. */
  readonly details: Property<JsonValue[]>;
  /** A server connected, disconnected or changed tools. */
  readonly changed: Signal<CollectionChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "mcp", window);
    this.servers = new Property(this, "servers", false);
    this.status = new Property(this, "status", false);
    this.details = new Property(this, "details", false);
    this.changed = new Signal("mcp.changed");
  }

  /** Connect a server. */
  connect(name: string): Promise<void> {
    return this.call<void>("connect", { name });
  }

  /** Disconnect a server. */
  disconnect(name: string): Promise<void> {
    return this.call<void>("disconnect", { name });
  }

  /** Start the servers that should be running. */
  ensure(): Promise<JsonValue> {
    return this.call<JsonValue>("ensure", {});
  }

  /** Offer or withhold one tool. */
  set_tool_enabled(server: string, tool: string, on: boolean): Promise<JsonValue> {
    return this.call<JsonValue>("set_tool_enabled", { server, tool, on });
  }
}

/** Plugins and marketplaces. */
export class HostPlugins extends HostObject {
  static readonly PATH = "plugins";
  /** Every installed plugin. */
  readonly installed: Property<JsonValue[]>;
  /** Every marketplace. */
  readonly marketplaces: Property<JsonValue[]>;
  /** A plugin or marketplace changed. */
  readonly changed: Signal<CollectionChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "plugins", window);
    this.installed = new Property(this, "installed", false);
    this.marketplaces = new Property(this, "marketplaces", false);
    this.changed = new Signal("plugins.changed");
  }

  /** What a plugin carries. */
  contents(name: string): Promise<JsonValue> {
    return this.call<JsonValue>("contents", { name });
  }

  /** What a plugin would install. */
  preview(source: string, branch?: string, subdir?: string): Promise<JsonValue> {
    return this.call<JsonValue>("preview", { source, branch, subdir });
  }

  /** Install a plugin. */
  install(ref: string, install_ref?: string): Promise<void> {
    return this.call<void>("install", { ref, install_ref });
  }

  /** Update a plugin. */
  update(name: string, install_ref?: string): Promise<void> {
    return this.call<void>("update", { name, install_ref });
  }

  /** Remove a plugin. */
  remove(name: string): Promise<void> {
    return this.call<void>("remove", { name });
  }

  /** Enable or disable a plugin. */
  set_enabled(name: string, on: boolean): Promise<void> {
    return this.call<void>("set_enabled", { name, on });
  }

  /** Add a marketplace. */
  add_marketplace(url: string): Promise<void> {
    return this.call<void>("add_marketplace", { url });
  }

  /** Remove a marketplace. */
  remove_marketplace(name: string): Promise<void> {
    return this.call<void>("remove_marketplace", { name });
  }

  /** Refresh marketplaces. */
  refresh_marketplaces(name?: string): Promise<void> {
    return this.call<void>("refresh_marketplaces", { name });
  }
}

/** Installed extensions. Read-only, apart from this extension's own settings. */
export class HostExtensions extends HostObject {
  static readonly PATH = "extensions";
  /** Every installed extension. */
  readonly installed: Property<ExtensionInfo[]>;
  /** An extension's health moved. */
  readonly state_changed: Signal<ExtensionStateChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "extensions", window);
    this.installed = new Property(this, "installed", false);
    this.state_changed = new Signal("extension.state-changed");
  }

  /** Change this extension's own settings. */
  set_settings(values: Record<string, JsonValue>): Promise<SettingsChange> {
    return this.call<SettingsChange>("set_settings", { values });
  }
}

/** Hooks. */
export class HostHooks extends HostObject {
  static readonly PATH = "hooks";
  /** Every hook. */
  readonly details: Property<JsonValue[]>;
  /** Hooks were reloaded. */
  readonly changed: Signal<CollectionChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "hooks", window);
    this.details = new Property(this, "details", false);
    this.changed = new Signal("hooks.changed");
  }

  /** Reload hooks. */
  reload(): Promise<void> {
    return this.call<void>("reload", {});
  }

  /** Run the session-start hooks. */
  fire_session_start(): Promise<void> {
    return this.call<void>("fire_session_start", {});
  }
}

/** What the agent may do without asking. */
export class HostPermissions extends HostObject {
  static readonly PATH = "permissions";
  /** `default`, `plan`, `acceptEdits` or `bypassPermissions`. */
  readonly mode: Property<string>;
  /** The mode changed. */
  readonly mode_changed: Signal<PermissionModeChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "permissions", window);
    this.mode = new Property(this, "mode", false);
    this.mode_changed = new Signal("permissions.mode_changed");
  }

  /** Would this tool call be allowed, asked or denied? */
  check(tool_name: string, func_name: string, tool_args?: Record<string, JsonValue>): Promise<JsonValue> {
    return this.call<JsonValue>("check", { tool_name, func_name, tool_args });
  }

  /** Save a permission rule. */
  save_rule(rule: string, level: string): Promise<void> {
    return this.call<void>("save_rule", { rule, level });
  }
}

/** The group policy in force. */
export class HostPolicy extends HostObject {
  static readonly PATH = "policy";
  /** The group's policy pack. */
  readonly pack: Property<JsonValue>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "policy", window);
    this.pack = new Property(this, "pack", false);
  }
}

/** Whether the person is signed in to igni. Never their session or a credential. */
export class HostAccount extends HostObject {
  static readonly PATH = "account";
  /** Who is signed in. */
  readonly status: Property<Account>;
  /** Someone signed in or out. */
  readonly changed: Signal<Account>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "account", window);
    this.status = new Property(this, "status", false);
    this.changed = new Signal("account.changed");
  }

  /** Start the person's own sign-in. Its result is theirs. */
  sign_in(): Promise<void> {
    return this.call<void>("sign_in", {});
  }

  /** Sign the person out. */
  sign_out(): Promise<Account> {
    return this.call<Account>("sign_out", {});
  }
}

/** The floor's notifications. Its own always; everyone's with `notifications`. */
export class HostNotifications extends HostObject {
  static readonly PATH = "notifications";
  /** Newest first. */
  readonly list: Property<NotificationEntry[]>;
  /** Unread counts. */
  readonly unread: Property<NotificationCounts>;
  /** The counts moved. */
  readonly changed: Signal<NotificationCounts>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "notifications", window);
    this.list = new Property(this, "list", false);
    this.unread = new Property(this, "unread", false);
    this.changed = new Signal("notifications.changed");
  }

  /** Tell the person at this machine something. It never leaves the machine. */
  notify(notification: LocalNotification): Promise<void> {
    return this.call<void>("notify", { notification });
  }

  /** Mark notifications read: these ids, one source, or all of them. */
  mark_read(ids?: string[], source?: string): Promise<NotificationCounts> {
    return this.call<NotificationCounts>("mark_read", { ids, source });
  }
}

/** This extension, as the host sees it. */
export class HostSelf extends HostObject {
  static readonly PATH = "self";
  /** Its manifest id. */
  readonly id: Property<string>;
  /** Its settings. Writable. */
  readonly settings: Property<Record<string, JsonValue>>;
  /** Its own entry. */
  readonly state: Property<ExtensionInfo>;
  /** Its home on the web: its service's URL, when it has one. */
  readonly web_url: Property<string | null>;
  /** Its settings changed. */
  readonly settings_changed: Signal<SettingsChange>;
  /** Its health moved. */
  readonly state_changed: Signal<ExtensionStateChange>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "self", window);
    this.id = new Property(this, "id", false);
    this.settings = new Property(this, "settings", true);
    this.state = new Property(this, "state", false);
    this.web_url = new Property(this, "web_url", false);
    this.settings_changed = new Signal("self.settings_changed");
    this.state_changed = new Signal("self.state_changed");
  }
}

/** The root. `changed` reports every change anywhere in the model, for an extension that wants to see all of it; each delivery needs the grant of the member that changed. */
export class Host extends HostObject {
  static readonly PATH = "";
  readonly app: HostApp;
  readonly windows: HostWindows;
  readonly ui: HostUi;
  readonly sessions: HostSessions;
  readonly chat: HostChat;
  readonly plan: HostPlan;
  readonly tasks: HostTasks;
  readonly loop: HostLoop;
  readonly commands: HostCommands;
  readonly agents: HostAgents;
  readonly workflows: HostWorkflows;
  readonly scheduler: HostScheduler;
  readonly processes: HostProcesses;
  readonly workspace: HostWorkspace;
  readonly models: HostModels;
  readonly mcp: HostMcp;
  readonly plugins: HostPlugins;
  readonly extensions: HostExtensions;
  readonly hooks: HostHooks;
  readonly permissions: HostPermissions;
  readonly policy: HostPolicy;
  readonly account: HostAccount;
  readonly notifications: HostNotifications;
  readonly self: HostSelf;
  /** Anything changed. Carries the member's path and its new value. */
  readonly changed: Signal<Change>;

  constructor(link: HostLink, window: string | null = null) {
    super(link, "", window);
    this.app = new HostApp(link, window);
    this.windows = new HostWindows(link, window);
    this.ui = new HostUi(link, window);
    this.sessions = new HostSessions(link, window);
    this.chat = new HostChat(link, window);
    this.plan = new HostPlan(link, window);
    this.tasks = new HostTasks(link, window);
    this.loop = new HostLoop(link, window);
    this.commands = new HostCommands(link, window);
    this.agents = new HostAgents(link, window);
    this.workflows = new HostWorkflows(link, window);
    this.scheduler = new HostScheduler(link, window);
    this.processes = new HostProcesses(link, window);
    this.workspace = new HostWorkspace(link, window);
    this.models = new HostModels(link, window);
    this.mcp = new HostMcp(link, window);
    this.plugins = new HostPlugins(link, window);
    this.extensions = new HostExtensions(link, window);
    this.hooks = new HostHooks(link, window);
    this.permissions = new HostPermissions(link, window);
    this.policy = new HostPolicy(link, window);
    this.account = new HostAccount(link, window);
    this.notifications = new HostNotifications(link, window);
    this.self = new HostSelf(link, window);
    this.changed = new Signal("changed");
  }

  /** Ask igni's local decision model (Laya) typed questions about a context, in one pass on this machine. `context` is named text fields — at most 8, 6,000 characters in all — that the questions quote in backticks; `questions` are 1–16 `DecisionQuestion`s. A context the model would have to cut is refused (`invalid`), never answered from part of it; a model that cannot be loaded answers `unavailable`. Zero-shot accuracy is modest: measure it on your own data before acting on an answer, and use the probabilities, not just the answer. */
  decide(context: Record<string, string>, questions: DecisionQuestion[]): Promise<Decision> {
    return this.call<Decision>("decide", { context, questions });
  }

  /** `decide` for up to 16 contexts at once, sharing forward passes. One decision per context, in order. */
  decide_batch(contexts: (Record<string, string>)[], questions: DecisionQuestion[]): Promise<Decision[]> {
    return this.call<Decision[]>("decide_batch", { contexts, questions });
  }
}

