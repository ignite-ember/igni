/**
 * Every piece of state a view holds of its own, and why it is not the
 * window's model.
 *
 * `docs/HOST_OBJECT_MODEL.md`, *The UI is part of the model*: what a
 * window shows — its page, the panel over its chat, its sidebar, the
 * preview, the selection, the composer, toasts, the dialog, where its
 * transcript is, focus, the session it shows — is the window model's
 * (`clientState.ts`), which the host owns and extensions read and write.
 * A view binds to it; it does not keep a copy.
 *
 * `viewState.test.ts` holds that structurally. It finds every
 * `useState` and `useReducer` under `src/` and fails unless each is
 * listed here, in its file, under one of the {@link CATEGORIES} — a
 * closed set, none of which is something the model describes. A
 * listing with nothing behind it fails too, so this file is always
 * exactly what the views hold. And a state named like one of the
 * model's fields ({@link MODEL_NAMES}) fails unless {@link BECAUSE}
 * says, in a sentence, why it is not that.
 *
 * Adding state to a view means adding it here, under a category whose
 * reason is true of it — which is the moment to ask whether it is
 * really the model's.
 */

/** Why a view may hold a piece of state itself. */
export const CATEGORIES = {
  data:
    "A copy of something the host keeps — the transcript, the status, the sessions, a " +
    "page's rows — held to render it. The host model has it as a member (chat.messages, " +
    "chat.status, sessions.list, plugins.installed, …), and every change to it is a " +
    "write through an RPC or a run, which is signalled.",
  pending:
    "An operation in flight, or what the last attempt left: busy, loading, an error, an " +
    "upload not yet landed, a module still loading. It ends with the operation.",
  field:
    "Text or a choice being entered into a form before it is submitted; submitting it is " +
    "the write (an RPC, a model write, a dialog's answer). The composer is not a field: " +
    "its draft is ui.composer.",
  menu:
    "A menu, popover or inline confirmation open under the pointer — part of the gesture " +
    "that opened it, gone when the pointer moves on or the choice is made.",
  hover: "The pointer over something, or something being dragged over it.",
  animation:
    "A visual with a timer: a pulse, a closing animation, a 'copied' tick, a countdown, " +
    "a clock.",
  measure:
    "A DOM handle or a measurement — an element, its size, an overflow, a caret position, " +
    "the document's theme as it is.",
  disclosure:
    "Whether one block inside the transcript or a page shows its detail — a tool card's " +
    "output, a collapsed section, a card's tab. Presentational, per block, like hover.",
  device:
    "A preference of this device rather than of the window, kept in the browser's " +
    "storage: the light/dark toggle, the FPS overlay.",
  page:
    "A page's own working state below ui.route: the sub-view it shows (a plugin within " +
    "Plugins), its tab, its filter. The route names the page; " +
    "these levels are the page's. Known remainder: carrying them in the route's params " +
    "makes them the model's, and this category shrinks as pages move.",
} as const;

export type Category = keyof typeof CATEGORIES;

/** Folders not scanned, and why. */
export const EXCLUDED: Record<string, string> = {
  "dev/":
    "Development demos (`?demo=`), each a fixture with canned state for one component; " +
    "not igni's UI.",
};

/**
 * Names that say "the window model": a view state called one of these
 * is presumed to be the model's and fails unless {@link BECAUSE} says
 * otherwise.
 */
export const MODEL_NAMES =
  /^(route|pages?|panel|sidebar\w*|preview\w*|selection|highlight\w*|composer\w*|toasts?|dialog|scroll\w*|stickToBottom|focus|focused|session|sessionId|searchOpen|projectDir|draft|attachments|mode)$/i;

/** Why a state named like the model's is not the model's, by file. */
export const BECAUSE: Record<string, Record<string, string>> = {
  "App.tsx": {
    scrollEl: "The scroller element, for the indicator to measure — where it is scrolled is ui.scroll.",
  },
  "components/ChatItems.tsx": {
    draft: "The text of a sent message being edited in place, before it is resent.",
  },
  "components/Composer.tsx": {
    attachments: "Uploads in flight, as placeholders; a landed file joins the draft as @path.",
    draft: "What was typed before ArrowUp started recalling history, to come back to.",
  },
  "components/panels/ExtensionsPanel.tsx": {
    draft: "An extension's settings as edited, before they are saved.",
  },
};

/** Every view state, by file and category. */
export const VIEW_STATE: Record<string, Partial<Record<Category, string[]>>> = {
  "App.tsx": {
    data: ["conn", "items", "runPhase", "status", "hitl", "extensions", "pendingUpdate", "sessions", "skills", "backendCommands", "cloudName", "historyIndexToItemIndex"],
    menu: ["modelMenuSignal", "accountMenu"],
    measure: ["scrollEl"],
    animation: ["pulsingItemId"],
  },
  "components/ChatItems.tsx": {
    animation: ["copied"],
    disclosure: ["collapsed", "open", "open", "open", "open", "expanded", "open"],
    measure: ["overflowing"],
    menu: ["retryOpen", "confirmingDelete"],
    field: ["retryDraft", "editing", "draft"],
  },
  "components/ChatSearchBar.tsx": {
    field: ["query"],
    data: ["matches"],
    page: ["activeIdx"],
    pending: ["pending"],
  },
  "components/CodeIndexIndicator.tsx": {
    data: ["status"],
  },
  "components/Composer.tsx": {
    pending: ["attachments"],
    hover: ["dragHot"],
    menu: ["menu", "toolsOpen", "modelMenu", "modeMenu"],
    data: ["history"],
    field: ["histIdx", "draft"],
  },
  "components/FPSCounter.tsx": {
    measure: ["fps"],
    device: ["visible"],
  },
  "components/FilePill.tsx": {
    menu: ["menuOpen"],
    animation: ["copied"],
  },
  "components/FilePreview.tsx": {
    data: ["data"],
    pending: ["loading"],
  },
  "components/FileRefPicker.tsx": {
    field: ["query"],
    data: ["results"],
    menu: ["active"],
  },
  "components/GroupIndicator.tsx": {
    data: ["policy"],
  },
  "components/HitlDialog.tsx": {
    field: ["index", "decisions"],
  },
  "components/HostDialog.tsx": {
    field: ["value"],
  },
  "components/JsonRenderView.tsx": {
    disclosure: ["idx", "open", "active", "open", "open"],
    field: ["checked", "val"],
  },
  "components/NotificationsPanel.tsx": {
    data: ["rows", "unread"],
    pending: ["failed"],
  },
  "components/RetryBanner.tsx": {
    data: ["retries"],
    animation: ["countdownPercent"],
  },
  "components/ScrollIndicator.tsx": {
    measure: ["thumb"],
    animation: ["active"],
  },
  "components/SignInWall.tsx": {
    page: ["step"],
    field: ["email", "server"],
    data: ["found"],
    pending: ["busy", "failed"],
  },
  "components/StatusBits.tsx": {
    animation: ["copied"],
  },
  "components/ThemeToggle.tsx": {
    device: ["theme"],
  },
  "components/Toasts.tsx": {
    animation: ["closing"],
  },
  "components/UpdatePrompt.tsx": {
    pending: ["busy", "error"],
  },
  "components/WatcherIndicator.tsx": {
    data: ["pids"],
  },
  "components/WorkflowRun.tsx": {
    disclosure: ["collapsed"],
  },
  "components/pages/ContextPage.tsx": {
    data: ["breakdown"],
    pending: ["error", "loading"],
  },
  "components/pages/WorkflowsPage.tsx": {
    data: ["workflows"],
    pending: ["error"],
  },
  "components/panels/AgentsPanel.tsx": {
    data: ["agents"],
    page: ["selected"],
    pending: ["busy"],
  },
  "components/panels/CodeIndexPanel.tsx": {
    data: ["status", "breakdown", "activity"],
    pending: ["busy", "runningVerb"],
    hover: ["hover", "hover"],
  },
  "components/panels/DirectoryPicker.tsx": {
    data: ["listing"],
    field: ["pathInput"],
    disclosure: ["showHidden"],
    pending: ["loading"],
  },
  "components/panels/ExtensionsPanel.tsx": {
    field: ["draft"],
    pending: ["busy", "error"],
  },
  "components/panels/HooksPanel.tsx": {
    data: ["rows"],
    pending: ["error"],
  },
  "components/panels/LoginPanel.tsx": {
    data: ["statusText"],
  },
  "components/panels/LoopPanel.tsx": {
    data: ["status"],
  },
  "components/panels/McpPanel.tsx": {
    disclosure: ["expanded"],
    measure: ["overflowing"],
    menu: ["open"],
    data: ["servers"],
    pending: ["busy"],
  },
  "components/panels/PluginsPanel.tsx": {
    data: ["plugins", "markets", "contents"],
    page: ["tab", "selected", "view"],
    field: ["installRef", "marketUrl", "query"],
    pending: ["busy", "error", "loading"],
  },
  "components/panels/SchedulePanel.tsx": {
    menu: ["open"],
    data: ["tasks"],
    page: ["filter"],
    field: ["query"],
  },
  "components/panels/SkillsPanel.tsx": {
    data: ["skills"],
  },
  "components/panels/WatcherPanel.tsx": {
    data: ["rows", "logsByPid"],
    page: ["selectedPid", "filter", "autoScroll"],
    animation: ["now"],
  },
  "extensions/ExtensionIndicator.tsx": {
    data: ["state"],
  },
  "extensions/useDocumentTheme.ts": {
    measure: ["theme"],
  },
  "extensions/useExtensionTraffic.ts": {
    data: ["signals"],
  },
  "notifications/model.ts": {
    data: ["counts"],
  },
};
