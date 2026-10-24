# The host object model

**An extension can do anything in igni that igni's own UI can do, and
see everything that happens in it: every navigation, every data write,
every UI change.** The
host exposes everything to extensions as one model in the style of QML:
**objects** with **properties**, **signals** and **methods**. An
extension reacts to a signal and, while handling it, reads and writes the
model.

```python
from igni_extension import IgniExtension
from igni_extension.host_model import Run, SessionChange

ext = IgniExtension("acme.reviewer")

@ext.on(ext.host.sessions.current_changed)
async def follow(change: SessionChange) -> None:
    card = {"type": "Card", "props": {"title": f"Reviewer: watching {change.session_id}"}}
    await ext.host.chat.append({"root": "c", "elements": {"c": card}})

@ext.on(ext.host.chat.run_completed)
async def review(run: Run) -> None:
    if run.changed_files:
        await ext.host.chat.post("Two issues in the files you just changed: …")
```

The extension's page sees the same model through `@igni/extension-bridge`
(`await bridge.host.sessions.current.get()`, `bridge.on(bridge.host.chat.run_completed, fn)`),
so the process and the page never disagree about what igni looks like.
A property is read with `await prop` (Python) or `prop.get()`, written
with `prop.set(value)`; names are the schema's, snake_case, in both SDKs.

## The UI is part of the model

igni's views do not keep their own state. Which page is open, which
panel, the sidebar, what is selected or highlighted, the composer's
draft, the toasts on screen, the dialogs: all of it is model state that
the host owns, per window. The views bind to it, as QML views bind to
their model. So an extension observes every UI change and can make any
of them, by the same path the view uses. igni's own UI is then just the
first client of the model.

**The window's model.** Every window has one, kept by the host
(`ember_code.extensions.host.windows`). Its fields are what the window
shows:

| Field | Member | Lasts |
|---|---|---|
| the page stack | `ui.route` (`navigate`, `back`) | across restarts |
| the panel over the chat — `notifications`, `search`, `login`, `dir-picker` | `ui.panel` (`open_panel`, `close_panel`) | while igni runs |
| the sidebar: open, width, the section in view | `ui.sidebar` (`set_sidebar`) | across restarts |
| the file previewed | `ui.preview` (`open_preview`, `close_preview`) | while igni runs |
| the selection: the picked-out message, text selected in the transcript, the file last opened | `ui.selection` (`select`) | while igni runs |
| the composer: draft, the files it refers to, its mode (`chat`, `command`, `shell`) | `ui.composer` (`set_composer`) | across restarts, per session |
| toasts | `ui.toasts` (`toast`, `dismiss_toast`) | while igni runs |
| the dialog | `ui.dialog` (`show_dialog`) | while igni runs |
| where the transcript is | `ui.scroll` (`scroll_to`) | while igni runs |
| focus | `windows.focused` | while igni runs |
| the session it shows | `sessions.current` | across restarts |

"While igni runs" survives the window reloading — a dialog an extension
is waiting on is still there when the window comes back — and "across
restarts" is written to the client-state store as well.

**One path for every writer.** igni's web client holds none of this in
its components. It hydrates the model when it connects, binds its views
to the copy it keeps (`clients/web/src/clientState.ts`), and writes every
change it makes back over one RPC, `window_model` — into the same
`Windows` an extension's `host.ui` writes. Each change, whoever made it,
is stored, pushed to every window as `window_model_changed`, and
signalled (`ui.navigated`, `ui.panel_changed`, … and `host.changed`). So
an extension sees every navigation, every panel, every keystroke in the
composer (after the short pause typing always had), every toast and
dialog; and what it writes, the window draws exactly as its own.

**Responsive, and the host's.** A window shows its own write at once and
sends it with a token of its own. The host applies writes in the order
they reach it and pushes each with its revision and the writer's token.
The window keeps showing its write until its echo (or the RPC's answer,
which carries the same revision) arrives; a change somebody else made
replaces a write still in the composer's pause, and is ordered before a
write already sent. Every window ends on what the host applied last. A
write that does not fit the field's schema type is refused and the
window shows the model's value.

**Dialogs answer.** `show_dialog(spec)` puts the dialog in `ui.dialog`
and waits: the window draws it — title, body, up to four buttons, an
optional line of input, labelled with the extension's name — and the
person's answer comes back as `{button, value}`, or `None` when it is
dismissed. One dialog at a time: a new one (igni's own update prompt
too) closes the open one unanswered.

**Messages by their run.** `selection.message_id`, `scroll.message_id`
and `scroll_to` name a message by the run it belongs to — the `run_id`
of `chat.messages` and the run signals — and `scroll_to("bottom")`
follows the newest message again.

A window is identified by the stable id it keeps its model under;
`host.ui` is **the active window** — the one the person last used, which
is the focused one whenever a window has focus — and
`host.windows.ui(id)` any open window. A page's `host.ui` is the window
it is framed in.

**Below the route.** The model names the page; a page's own levels —
the plugin within Plugins, a page's tab and filter — are still the page's. They are the next to move, into
the route's `params`, and the rule below lists them as such.

## Every write is a signal

Every property has a change signal, and every method that writes data
signals what it changed. The data might be a session, a message, a
setting, a plugin, a permission rule, the UI state or anything else.
Besides each object's own signals, `host.changed(path, value)` reports
every change in the model, for an extension that wants to see all of it:
`path` is the property that changed and `value` is what its own change
signal carries. A write is signalled whoever made it — the person in a
window, another extension, or this one.

## Rules

- **Complete by construction.** Every RPC method (`RpcMethod`), every
  action the UI sends (`schemas/fe_actions.py`) and every event the host
  broadcasts (`schemas/be_events.py`, `schemas/mirroring.py`, and the
  push channels) is mapped to a member of this model, or is listed under
  *Not reachable* with its reason
  (`tests/test_the_host_model_covers_the_app.py`). So is every piece of
  UI state: no view may hold state the window's model describes.
  `clients/web/src/viewState.test.ts` parses the web client and finds
  every `useState` and `useReducer`; each must be listed in
  `viewState.allowlist.ts` under one of a closed set of reasons — a copy
  of host data held to render it, an operation in flight, a form field
  before it is submitted, a menu under the pointer, hover, an animation,
  a measurement, a block's disclosure, a device preference, or a page's
  own levels below the route — none of which is something the model
  describes. A listing with nothing behind it fails, and a state named
  like a model field (`panel`, `toasts`, `draft`, `scroll…`) fails unless
  the allowlist says in a sentence why it is not that. A feature added
  to igni without its member — or with a view keeping what should be
  the model's — fails the build, so the model cannot fall behind the
  app.
- **One schema, two SDKs.** The model is described once —
  `ember_code.extensions.host.definition`, version **1.1.0** — and
  versioned under the compatibility floor (EXTENSIBILITY §8): a new
  member is a minor version, a changed or removed one a major. The Python
  SDK (`igni-extension`, `igni_extension.host_model`) and the TypeScript
  bridge (`hostModel.generated.ts`) are generated from it by `make
  host-sdks`, and a test fails when what is checked in is stale.
- **Granted per object.** Every object has one permission per row below.
  It is declared in the manifest (`permissions.host`) and shown on the
  Services page before a super admin approves, and by `igni ext install`.
  An entry is a permission name or a `*`-suffixed prefix (`chat.*`).
  An ungranted object does not exist for the extension: reads fail
  (`not_granted`), methods are refused, and its signals never arrive —
  subscribing to one is not an error, it simply never fires.
  `workspace` is not a `permissions.host` entry: `permissions.workspace`
  (`read` or `write`) grants `host.workspace`, limited to
  `workspacePaths` — a changed file outside them is not in any list an
  extension is handed. An extension's own state, settings and
  notifications need no grant.
- **Writes are the person's writes.** A member writes through the same
  RPC handler, UI action or store the person's window uses, so a write
  the person could not make — another project's session, a model that is
  not configured, an approval nobody asked for — is refused the same way,
  with `refused`.
- **Signals carry the new value**, so a handler need not read back what it
  was told.
- **The host's internal calls stay private.** Extensions reach the model,
  never `RpcMethod` itself, so igni can refactor its UI wire without
  breaking an extension. The model is the promise; the wire is not.
  Members typed `json` are the host's report of something whose shape is
  not part of the promise yet (a plugin's details, an MCP server's
  tools); typing one is a minor version.
- **The channel is the SDK's business.** The process uses a loopback
  WebSocket guarded by its per-process secret (`IGNI_HOST_URL`,
  `IGNI_HOST_SECRET` — the same secret the service-token endpoint takes),
  and the page uses the bridge, which the window forwards as the
  `host_model` RPC with the frame's extension as its caller. No extension
  author sees either; `docs/HOST_MODEL_PROTOCOL.md` describes them for
  SDK maintainers.

Errors carry a code both SDKs raise (`HostError.code`): `not_granted`,
`not_found`, `invalid`, `read_only`, `stage_2`, `refused`, `failed`.

## The model

| Object | Properties | Signals | Methods | Permission |
|---|---|---|---|---|
| `host` (the root) | — | `changed` (path, value) | — | each delivery needs the changed member's |
| `host.app` | `display` (theme, verbosity, output style), `version`, `update_available`, `connection` | `display_changed`, `connection_changed`, `update_changed` | `set_verbose(on)`, `set_output_style(name)`, `check_for_update()` | `app` |
| `host.app.state` | `values` — key–value state (its own keys) | `changed` | `get(key)`, `set(key, value)`, `delete(key)` | always (own keys) |
| `host.windows` | `list` — every open window: id, kind (`tauri`, `vscode`, `jetbrains`, `web`), session, `focused`; `focused` — the window with focus | `opened`, `closed`, `focus_changed` | `ui(id)` — that window's `host.ui` | `ui` |
| `host.ui` — the active window, or `host.windows.ui(id)` | `route` (the page stack: kind, label and params of each), `panel`, `sidebar` (open, width, section), `preview`, `selection` (message, text, file), `composer` (draft, attachments, mode) — all writable; `toasts`, `dialog`, `scroll` | `navigated`, `panel_changed`, `sidebar_changed`, `preview_changed`, `selection_changed`, `composer_changed`, `toast_shown`, `toast_dismissed`, `dialog_opened`, `dialog_closed` (with the answer), `scrolled` | `navigate(page, params)`, `back()`, `open_panel(kind, params)`, `close_panel()`, `set_sidebar(open, width, section)`, `open_preview(path)`, `close_preview()`, `select(selection)`, `set_composer(text, attachments, mode)`, `toast(text, kind, body, target)`, `dismiss_toast(toast_id)` (its own), `show_dialog(spec)` → the person's answer, `scroll_to(message_id)` | `ui`; `open_preview` also needs the path within `permissions.workspace` |
| `host.sessions` | `list`, `current` (writable — switches the active window) | `current_changed`, `listed`, `cleared`, `named` | `open(session_id)`, `create(project_dir)`, `attach(session_id, project_dir)` | `sessions` |
| `host.chat` — the current session | `session_id`, `messages` (the transcript), `pending` (queued messages), `processing`, `status`, `timeout`, `interrupted_runs`, `context` (token counts and breakdown) | `message_added` (with `from_extension`), `content_delta`, `reasoning_started`, `run_started`, `run_completed` (with the files changed), `run_error`, `run_paused`, `streaming_done`, `typing`, `status_changed`, `info`, `error`, `tool_started`, `tool_completed`, `tool_error`, `model_completed`, `file_edited` (needs `workspace`), `agent_event`, `retry`, `history_changed` | `post(text, attachments)` — **as the extension**; the agent answers. `queue(text)` — for the next turn, as the extension. `append(view, title)` → its id, `replace(view_id, view, title)` — json-render blocks in the transcript, not sent to the model. `search(query, limit)`, `cancel()`, `cancel_agent(run_id)`, `truncate(run_id)`, `compact()`, `extract_learnings(user_msg, assistant_msg)`, `discard_interrupted(message_id)`, `upload(filename, content_base64)` | `chat.read`; `post`, `queue`, `upload`: `chat.post`; `append`, `replace`: `chat.append`; the rest of the methods: `chat.control` |
| `host.chat.approvals` — the agent asking to act | `waiting` (requests and details) | `requested`, `resolved`, `details` | `answer(requirement_id, action, choice)`, `answer_all(answers)` | `chat.approvals` |
| `host.chat.views` — json-render in the chat | `list` — the views in the session, the visualizer's and extensions' | `shown`, `action` | `dispatch(view_id, action, params)` | `chat.append` |
| `host.plan` | `latest` | `changed` | `approve(run_id)`, `dismiss(run_id)` | `chat.control` |
| `host.tasks` — the agent's todo list and sub-agent tasks | `todos` | `created`, `updated`, `iteration`, `snapshot`, `todos_changed` | — | `chat.read` |
| `host.loop` | `status` | `changed` | `pause()`, `resume()`, `cancel_pending()`, `pop_pending()` | `chat.control` |
| `host.commands` | `list` (slash commands), `skills`, `skill_definitions`, `skill_details` | — (they change with `plugins.changed`) | `run(command)` — as the extension; the answer comes back to it | `commands` |
| `host.agents` | `details` | `changed` | `promote_ephemeral(name)`, `discard_ephemeral(name)` | `agents` |
| `host.workflows` | `list` | `changed` | `run(name, input)`, `cancel(workflow_run_id)` | `workflows` |
| `host.scheduler` | `tasks` | `event` | `start()`, `run_now(task_id)`, `cancel(task_id)` | `scheduler` |
| `host.processes` — background shells | `list` | `changed` | `tail(pid, lines)`, `stop(pid)`, `run(command)` | `processes` |
| `host.workspace` | `root`, `head {commit_sha, branch}` | `head_changed`, `files_changed` | `read(path)`, `list(dir)`, `complete(prefix)`, `search(query)`, `pick_dir()` | `workspace` (`permissions.workspace`, with its paths) |
| `host.models` | `registry`, `current` (writable — switches) | `changed` | — | `models` |
| `host.mcp` | `servers`, `status`, `details` | `changed` | `connect(name)`, `disconnect(name)`, `ensure()`, `set_tool_enabled(server, tool, on)` | `mcp` |
| `host.plugins` | `installed`, `marketplaces` | `changed` | `contents(name)`, `preview(source, branch, subdir)`, `install`, `update`, `remove`, `set_enabled`, `add_marketplace`, `remove_marketplace`, `refresh_marketplaces` | `plugins` |
| `host.extensions` | `installed` — every extension: id, name, version, state, reason, `web_url` | `state_changed` | `set_settings(values)` (its own) | `extensions` (read-only; `set_settings` always, own only) |
| `host.hooks` | `details` | `changed` | `reload()`, `fire_session_start()` | `hooks` |
| `host.permissions` | `mode` | `mode_changed` | `check(tool_name, func_name, tool_args)`, `save_rule(rule, level)` | `permissions` |
| `host.policy` | `pack` — the group policy in force | — (changes with `account.changed`) | — | `policy` |
| `host.account` | `status` — signed in or not, email, name, organisation | `changed` | `sign_in()` (starts the person's own sign-in), `sign_out()` | `account` |
| `host.notifications` | `list`, `unread` (by source) | `changed` | `notify(notification)` to the person at this machine, `mark_read(ids, source)` | always for its own; `notifications` for all |
| `host.self` | `id`, its own `settings` (writable), `state`, `web_url` | `settings_changed`, `state_changed` | — | always |

`host.permissions` has no `rules` property: igni has no read of the rule
set to back it. `check` answers for one call, and `save_rule` writes one.

`web_url` is an extension's home on the web: its service's URL — the
distribution record's `service_url`, which is the service's `client_uri`,
RFC 7591's "home page of the client" — or, for a development install,
its `serviceUrl` setting. igni shows it as **Open on the web** on the
extension's row and above its page.

## Not reachable

| What | Why |
|---|---|
| The person's igni session and any credential (`login`, `reload_cloud_credentials`, `clear_cloud_credentials`, `set_server`, `discover_server` values, `cancel_login`) | An extension never holds the session (service-identity §9). It can start or end the person's sign-in through `host.account`, but never read what it produces. |
| Posting *as the person* (`UserMessage` from an extension) | Decided: an extension's messages are labelled as the extension's (`host.chat.post`). |
| `shutdown` | Quitting igni is the person's decision. |
| `codeindex_*` | These are not the floor. They belong to the CodeIndex extension, and they leave the host as it takes them over. |
| The model's own plumbing: the `host_model` and `window_model` RPCs, and the `host_signal` and `window_model_changed` pushes | They are how the model reaches a page and a window, not things igni does. |

Anything else missing from this table is a bug in it.

## Messages from an extension

`host.chat.post(text)` puts a message in the current chat labelled as
coming from the extension. Every window paints it with the extension's
name on the bubble — it is nobody's echo — and the agent is told, in a
block the windows do not show, that the extension posted it, not the
person; then it answers in the chat as usual. Nothing posts as the
person, and nothing calls a model behind the person's back.
`host.chat.append(view)` and `replace(view_id, view)` put json-render
blocks in the transcript — the same path the visualizer's cards take,
kept in the session's log so a reload shows them again — shown to the
person and never sent to the model. An extension replaces only its own.

## The existing events

The four host events extensions heard before the model are signals of
it:

| Event | Signal |
|---|---|
| `session.switched` | `sessions.current_changed` |
| `workspace.head-changed` | `workspace.head_changed` |
| `workspace.files-changed` | `workspace.files_changed` |
| `extension.state-changed` | `extensions.state_changed` |

Their topic strings are the signals' wire names. They are granted by
`permissions.host` like every other signal (`sessions`; `workspace` from
`permissions.workspace`; `extensions`, which hears every extension's
state — `self.state_changed` is an extension's own, always). The earlier
delivery of the same topics — to a page's `onEvent` and to a service's
`igni_on_event` tool, granted by `permissions.events` — is gone: a
manifest that still declares `permissions.events` does not validate. A
process subscribes with `@ext.on(ext.host.workspace.head_changed)`, a
page with `bridge.on(bridge.host.workspace.head_changed, fn)`.
