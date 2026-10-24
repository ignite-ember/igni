# The host object model

**An extension can do anything in igni that igni's own UI can do, and
see everything that happens in it: every navigation, every data write,
every UI change.** The
host exposes everything to extensions as one model in the style of QML:
**objects** with **properties**, **signals** and **methods**. An
extension reacts to a signal and, while handling it, reads and writes the
model.

```python
ext = IgniExtension("acme.reviewer")

@ext.on(ext.host.sessions.current_changed)
async def follow(session: Session) -> None:
    await ext.host.chat.append(Card(title="Reviewer", body=f"Watching {session.title}"))

@ext.on(ext.host.chat.run_completed)
async def review(run: Run) -> None:
    if run.changed_files:
        await ext.host.chat.post("Two issues in the files you just changed: …")
```

The extension's page sees the same model through `@igni/extension-bridge`
(`bridge.host.sessions.current`, `bridge.on(...)`), so the process and the
page never disagree about what igni looks like.

## The UI is part of the model

igni's views do not keep their own state. Which page is open, which
panel, the sidebar, what is selected or highlighted, the composer's
draft, the toasts on screen, the dialogs: all of it is model state that
the host owns, per window. The views bind to it, as QML views bind to
their model. So an extension observes every UI change and can make any
of them, by the same path the view uses. igni's own UI is then just the
first client of the model.

Today the web client keeps this state in its components (`useState` in
`clients/web/src/App.tsx`), and only some of it — the window's session,
the sidebar, drafts — round-trips to the host as client state. Moving the
rest into the model is part of building this.

## Every write is a signal

Every property has a change signal, and every method that writes data
signals what it changed. The data might be a session, a message, a
setting, a plugin, a permission rule, the UI state or anything else.
Besides each object's own signals, `host.changed(path, value)` reports
every change in the model, for an extension that wants to see all of it.

## Rules

- **Complete by construction.** Every RPC method (`RpcMethod`), every
  action the UI sends (`schemas/fe_actions.py`) and every event the host
  broadcasts (`schemas/be_events.py`, `schemas/mirroring.py`) is mapped to
  a member of this model, or is listed under *Not reachable* with its
  reason. So is every piece of UI state and every route: no view may hold
  state of its own that is not in the model. Tests enforce both. A feature added to igni without its member
  here fails the build, so the model cannot fall behind the app.
- **One schema, two SDKs.** The model is described once and versioned
  under the compatibility floor (EXTENSIBILITY §8). The Python SDK
  (`igni-extension`) and the TypeScript bridge are generated from it.
- **Granted per object.** Every object has one permission per row below.
  It is declared in the manifest (`permissions.host`) and shown on the
  Services page before a super admin approves. An ungranted object does
  not exist for the extension: reads fail, methods are refused, and its
  signals never arrive.
- **Signals carry the new value**, so a handler need not read back what it
  was told.
- **The host's internal calls stay private.** Extensions reach the model,
  never `RpcMethod` itself, so igni can refactor its UI wire without
  breaking an extension. The model is the promise; the wire is not.
- **The channel is the SDK's business.** The process uses a loopback
  WebSocket guarded by its per-process secret, and the page uses the
  bridge. No extension author sees either.

## The model

| Object | Properties | Signals | Methods | Permission |
|---|---|---|---|---|
| `host.app` | `display` (theme, verbosity, output style), `version`, `update_available`, `connection` | `display_changed`, `connection_changed` | `set_verbose(on)`, `set_output_style(name)`, `check_for_update()` | `app` |
| `host.windows` | every open window: id, kind (desktop, VS Code, JetBrains, browser), `focused` | `opened`, `closed`, `focus_changed` | — | `ui` |
| `host.ui` — the focused window, or `host.windows[id].ui` | `route` (the page stack: kind and params of each), `panel` (which side panel is open, and its params), `sidebar` (open, width, section), `preview` (the file or document previewed), `selection` (the highlighted message, selected text, selected file), `composer` (draft text, attachments, mode), `toasts`, `dialog` (the open one), `scroll` (where the transcript is) | `navigated`, `panel_changed`, `sidebar_changed`, `preview_changed`, `selection_changed`, `composer_changed`, `toast_shown`, `toast_dismissed`, `dialog_opened`, `dialog_closed` | `navigate(page, params)`, `back()`, `open_panel(kind, params)`, `close_panel()`, `set_sidebar(...)`, `preview(path)`, `select(...)`, `set_composer(text, attachments)`, `toast(text, kind)`, `dialog(spec)` → the answer, `scroll_to(message)` | `ui` |
| `host.app.state` | key–value client state (its own keys) | `changed` | `get`, `set`, `delete` | always (own keys) |
| `host.sessions` | `list`, `current` (writable — switches) | `current_changed`, `listed`, `cleared` | `open(id)`, `create()`, `attach(id)` | `sessions` |
| `host.chat` — the current session | `session_id`, `messages` (list model), `pending` (queued messages), `processing`, `status`, `timeout`, `interrupted_runs`, `context` (token counts and breakdown) | `message_added`, `content_delta`, `reasoning_started`, `run_started`, `run_completed` (with the files changed), `run_error`, `run_paused`, `streaming_done`, `typing`, `status_changed`, `info`, `error` | `post(text, attachments)` — **as the extension**; the agent answers. `queue(text)` — for the next turn, as the extension. `append(view)`, `replace(view_id, view)` — json-render blocks in the transcript, not sent to the model. `search(query)`, `cancel()`, `cancel_agent(id)`, `truncate(after)`, `compact()`, `extract_learnings()`, `discard_interrupted(id)`, `upload(file)` | `chat.read`, `chat.post`, `chat.append`, `chat.control` |
| `host.chat.approvals` — the agent asking to act | `waiting` (requests and details) | `requested`, `resolved` | `answer(id, choice)`, `answer_all(choices)` | `chat.approvals` |
| `host.chat.views` — json-render in the chat | the views the run produced | `action` | `dispatch(view_id, action)` | `chat.append` |
| `host.plan` | `latest` | `changed` | `approve()`, `dismiss()` | `chat.control` |
| `host.tasks` — the agent's todo list and sub-agent tasks | `todos`, `tasks` | `created`, `updated`, `iteration`, `snapshot` | — | `chat.read` |
| `host.loop` | `status` | `changed` | `pause()`, `resume()`, `cancel_pending()`, `pop_pending()` | `chat.control` |
| `host.commands` | slash commands, skills (names, definitions, details) | — | `run(command)` — as the extension | `commands` |
| `host.agents` | definitions and details | — | `promote_ephemeral(name)`, `discard_ephemeral(name)` | `agents` |
| `host.workflows` | `list` | `changed` | `run(name, input)`, `cancel(id)` | `workflows` |
| `host.scheduler` | `tasks` | `event` | `start()`, `run_now(id)`, `cancel(id)` | `scheduler` |
| `host.processes` — background shells | `list` | `changed` | `tail(id)`, `stop(id)`, `run(command)` | `processes` |
| `host.workspace` | `root`, `head {sha, branch}` | `head_changed`, `files_changed` | `read(path)`, `list(dir)`, `complete(prefix)`, `search(query)`, `pick_dir()` | `workspace` (as today, with its paths) |
| `host.models` | `registry`, `current` (writable — switches) | `changed` | — | `models` |
| `host.mcp` | servers, status, details | `changed` | `connect(name)`, `disconnect(name)`, `ensure(name)`, `set_tool_enabled(server, tool, on)` | `mcp` |
| `host.plugins` | installed, details, contents, marketplaces | `changed` | `preview`, `install`, `update`, `remove`, `set_enabled`, `add_marketplace`, `remove_marketplace`, `refresh_marketplaces` | `plugins` |
| `host.extensions` | installed extensions and their state | `state_changed` | `set_settings(own)` | `extensions` (others read-only) |
| `host.hooks` | details | — | `reload()`, `fire_session_start()` | `hooks` |
| `host.permissions` | rules, mode | — | `check(action)`, `save_rule(rule)` | `permissions` |
| `host.policy` | the group policy in force | — | — | `policy` |
| `host.account` | signed in or not, server, organisation | `changed` | `sign_in()` (starts the person's own sign-in), `sign_out()` | `account` |
| `host.notifications` | `list`, `unread`, by source | `changed` | `notify(...)` to the person at this machine, `mark_read(...)` | always for its own; `notifications` for all |
| `host.self` | its own `settings` (writable), `state` | `settings_changed`, `state_changed` | — | always |

## Not reachable

| What | Why |
|---|---|
| The person's igni session and any credential (`login`, `reload_cloud_credentials`, `clear_cloud_credentials`, `set_server`, `discover_server` values) | An extension never holds the session (service-identity §9). It can start or end the person's sign-in through `host.account`, but never read what it produces. |
| Posting *as the person* (`UserMessage` from an extension) | Decided: an extension's messages are labelled as the extension's. |
| `shutdown` | Quitting igni is the person's decision. |
| `artifact_*`, `codeindex_*` | These are not the floor. They belong to the Artifacts and CodeIndex extensions, and they leave the host as those extensions take them over. |

Anything else missing from this table is a bug in it.

## The existing events

The four host events extensions receive today become signals:

| Event | Signal |
|---|---|
| `session.switched` | `sessions.current_changed` |
| `workspace.head-changed` | `workspace.head_changed` |
| `workspace.files-changed` | `workspace.files_changed` |
| `extension.state-changed` | `extensions.state_changed` |

Their topic strings remain the wire names.
