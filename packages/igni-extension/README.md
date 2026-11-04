# igni-extension

An igni extension's process, as its author writes it: tools igni can
call, its own service reached as the signed-in person, and the host it
runs in — without an address, a token or a protocol message in the
extension's code.

Building an extension for the first time? Follow
[Your first extension](../../docs/YOUR_FIRST_EXTENSION.md) from an empty
directory to a tool the agent calls, a page, a hook and a service; this
README is the reference.

**How an extension reaches people.** Its service uploads the signed
bundle to igni server at start-up. Two decisions then put it on a
person's machine, and both are needed: a **super admin approves the
service** (portal, *Services*), and an **org admin switches the
extension on for a group** (portal, *Extensions*). Every signed-in igni
of a person in that group then installs it the next time it syncs with igni server. Approval
alone installs it nowhere.

```python
from igni_extension import IgniExtension, LocalNotification, NotSignedIn, Target, ToolHints

ext = IgniExtension("acme.example")

READS = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)


@ext.tool(hints=READS)
async def summarise(slug: str) -> str:
    """Summarise one of the organisation's documents."""
    try:
        response = await ext.service.get(f"/documents/{slug}/summary")
    except NotSignedIn:
        return "Sign in to igni to read documents."
    response.raise_for_status()
    return response.json()["summary"]


@ext.tool(hints=ToolHints(read_only=False, destructive=False, idempotent=False, open_world=False))
async def export(slug: str) -> str:
    """Export a document to PDF and say so when it is ready."""
    export_id = await render_pdf(slug)
    await ext.host.notifications.notify(
        LocalNotification(
            kind="export_ready",
            title=f"{slug}.pdf",
            body="Your export is ready",
            target=Target(page="acme.example", params={"export": export_id}),
            event_key=f"export:{export_id}",
        )
    )
    return export_id


async def render_pdf(slug: str) -> str:
    return f"export-{slug}"   # your own work


def main() -> None:   # the console script the manifest's `python.entry` names
    ext.run()
```

`ext.run()` serves igni over stdio. Started from a terminal rather than by
igni, it says so on stderr — how to run it inside igni, and how to try a
tool without it (*Testing an extension*) — and then waits for MCP as
before.

## Tools, and what each says it is

Every tool states MCP's four hints — `read_only`, `destructive`,
`idempotent`, `open_world` — because igni's permission modes decide on
them: plan mode runs a read-only tool and refuses one that changes
things, Accept Edits runs a write that destroys nothing and stays inside
its own system, the default mode asks before any write, Bypass runs
everything (igni's `docs/EXTENSIBILITY.md` has the whole table).
`ext.tool(hints=...)` requires them, and `ext.run()` refuses to start
while any tool on `ext.mcp` leaves one out (`ToolHintsMissing`) — a tool
added with `ext.mcp.add_tool` passes `annotations=hints.annotations()`.

A tool the agent calls is also listed in the manifest's
`permissions.tools`, with the same four:

```json
{ "name": "acme.example.summarise",
  "annotations": { "readOnlyHint": true, "destructiveHint": false,
                   "idempotentHint": true, "openWorldHint": false } }
```

The manifest's are the ones that count — an administrator approved
them. igni compares them with what the server reports and shows a
difference in its extensions panel.

**Write them once.** Leave `permissions.tools` out of `manifest.json` and
`igni-service-bundle` writes it into the bundle from the code; list them
and it checks them against the code, refusing a difference. A tool the
code registers and the manifest does not list is never offered to the
agent — what you want for one only your page calls. To see what the code
implies: `python -m igni_extension.manifest acme_example` (your module),
or `ext.manifest_tools()`.

### One tool, five names

| Where | The name | Example (`acme.tickets`, `def lookup_ticket`) |
|---|---|---|
| Your code, `HostHarness.call`, `ConcurrentCall` | the function's name, or `name=` | `lookup_ticket` |
| The manifest's `permissions.tools`, the Services page | the extension's id, a dot, the tool | `acme.tickets.lookup_ticket` |
| A page's `bridge.rpc(...)`, and the `permissions.rpc` that grants it | `ext.`, then the manifest's name | `ext.acme.tickets.lookup_ticket` |
| igni: its permission rules, settings-file hooks, transcripts; a hook's `tool_name` | `mcp_ext:` + id + `_` + tool | `mcp_ext:acme.tickets_lookup_ticket` |
| What the model is sent | igni's name with every character a provider refuses made `_` | `mcp_ext_acme_tickets_lookup_ticket` |

The model's name is made at the provider boundary, because OpenAI's and
Anthropic's tool names allow only letters, digits, `_` and `-`; igni maps
the model's call back to its own name, so nothing you write ever sees it.
A hook is handed both of the names it may want: `tool_name`, igni's
(`read_file` for one of igni's own), and `tool_manifest_name`, the
manifest's (empty for igni's own). `ext.tool_names("lookup_ticket")`
answers the first four, and a decorated tool carries its manifest name
as `lookup_ticket.manifest_name`.

A tool that raises reaches the model as an error result, with the
exception's message as its text; igni's `PostToolUse` hooks are not asked
about it, and `PostToolUseFailure` is told.

## Installing

> **Until the SDKs are on PyPI: build from a checkout.** `igni-extension`
> is not published yet, so `uv add igni-extension` finds nothing. Build
> its wheel from an igni checkout and install from it:
>
> ```bash
> git clone https://github.com/ignite-ember/igni.git
> uv build --wheel --out-dir ./sdk-wheels igni/packages/igni-extension
> uv add igni-extension --find-links ./sdk-wheels      # or: pip install --find-links ./sdk-wheels igni-extension
> ```
>
> Building the bundle needs the same directory:
> `igni-service-bundle --extension extension --out extension-bundle.tar.gz --find-links ./sdk-wheels`
> (igni-service's README).

Once published, from PyPI:

```bash
uv add igni-extension        # or: pip install igni-extension
```

Each release's wheels and sdists are also attached to igni's GitHub
release (`sdk-v<version>`), for a build that cannot reach PyPI. The
versions, and working against an igni checkout instead, are in igni's
`docs/PUBLISHING_THE_SDKS.md`.

## Its service: `ext.service`

An `httpx.AsyncClient` for the extension's own service, as the person
signed in to igni. Paths are relative to the service. The person's token
comes from the igni host, is attached to every request, and is renewed
before it expires — the extension never sees the person's igni session.
When nobody is signed in to igni, a request raises `NotSignedIn`.

`ext.service` raises `NotConfigured` when igni started the process with no
service URL (`IGNI_SERVICE_URL`) — and a page's `bridge.service.url` is
`null` for the same reason. An extension igni distributed always has
one: the service that shipped it. A development install has one only
when it declares a `serviceUrl` setting in `contributes.settings` and it
is set; a plain-`http` URL is given only for this machine (`localhost`),
and only with `IGNI_DEV_EXTENSIONS=1`.

- `ext.service.anonymous` — the same service with no token, for what
  anyone may read (a public link, a health check). It never asks the host
  for a token, so it works when nobody is signed in:
  `await ext.service.anonymous.get(f"/v1/public/{slug}")`.
- `ext.service.websocket(path)` — a WebSocket to the service, opened with
  the person's token in its `Authorization` header:

  <!-- continues -->
  ```python
  import json

  @ext.on_start
  async def follow_room() -> None:
      async with ext.service.websocket("/v1/rooms/runbook") as room:
          await room.send(json.dumps({"hello": "room"}))
          async for frame in room:
              print(frame)
  ```

- `await ext.tokens.token()` — the token itself, for a client neither of
  those covers.
- `ext.tokens.signed_in` — who the last token was for (`user_id`,
  `org_id`, `email`, `name`), read from it without checking it; `None`
  before the first token or once nobody is signed in. For showing who is
  signed in and noticing that it changed — the service checks the token,
  this never decides anything. Whether anyone is signed in to igni at all
  is `await ext.host.account.status` (with the `account` grant).

## Whom a call is for: `Caller`

Every call igni makes — a tool call, a page's call, a hook — names whom
it is for: a person (`kind="person"`, their `id`, `org` and the one
`group` they belong to) or, later, an app (no group). A tool or hook
handler that wants it declares a parameter typed `Caller`; the SDK fills
it from the call, and it is never part of a tool's input:

<!-- continues -->
```python
from igni_extension import Caller

@ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False))
async def mine(caller: Caller) -> list[str]:
    response = await ext.service.for_(caller).get("/v1/things")
    return response.json()
```

- **`ext.service.for_(caller)`** — the service as that caller. The token
  is fetched from the host for them and kept apart from every other
  caller's. On a machine serving one person, the host gives that
  person's token and refuses anybody else's (`HostError`, code
  `refused`).
- **`caller: Caller`** fails the call when igni sent none;
  **`caller: Caller | None`** runs with `None` instead.
- **`Caller.current()`** answers the same anywhere inside the call — in
  a helper, or a plain `def` running in a worker thread.
- **Work no caller started** — `on_start`, a reaction — has none, and
  `ext.service` there is the signed-in person.

The extension never decides who may call it: igni forwards only calls
the caller may make, to extensions an org admin enabled for them. Its
job is to serve calls in parallel and keep callers apart — a caller kept
in a module global is the bug to avoid. `ConcurrencyHarness` (below)
checks both.

## The host: `ext.host`

`ext.host` is igni as the extension sees it — the **host object model**
(`docs/HOST_OBJECT_MODEL.md` in igni): one object per part of the app,
each with properties, signals and methods. An extension can do anything
igni's own UI can do, and see everything that happens in it.

<!-- continues -->
```python
from igni_extension.host_model import Run, SessionChange

@ext.on(ext.host.sessions.current_changed)
async def follow(change: SessionChange) -> None:
    busy = await ext.host.chat.processing           # read a property
    print(change, busy)

@ext.on(ext.host.chat.run_completed)                 # handle a signal
async def review(run: Run) -> None:
    if run.changed_files:
        await ext.host.chat.post("Two issues in the files you just changed.")

@ext.tool(hints=ToolHints(read_only=False, destructive=False, idempotent=True, open_world=False))
async def show_session(session_id: str, window_id: str) -> str:
    await ext.host.sessions.current.set(session_id)           # write a property
    await ext.host.windows.ui(window_id).navigate("plugins")  # any window
    return "shown"
```

- **Properties** read with `await prop` (or `prop.get()`) and, when
  writable, write with `await prop.set(value)`.
- **Signals** are handled with `@ext.on(signal)`; the value they carry is
  typed (`igni_extension.host_model` has every type). A handler may read
  and write the model while it runs.
- **Methods** are `async`; their results are typed.
- `ext.host.chat.post(text)` posts in the chat **labelled as the
  extension**; the agent is told who sent it and answers as usual.
  `ext.host.chat.append(view)` shows a json-render block that is never
  sent to the model.
- `ext.host.sessions.chat(session_id)` is the same chat for any session
  of the project, without switching what the person is looking at.
  A post there starts a run, or waits for the run already going, and
  never answers an approval. It needs `sessions.post` as well as
  `chat.post` (or `chat.append`):

  <!-- continues -->
  ```python
  @ext.on(ext.host.chat.run_completed)
  async def review_where_it_ran(run: Run) -> None:
      if run.changed_files:
          # The session the run was in, even if the person has moved on.
          chat = ext.host.sessions.chat(run.session_id)
          await chat.post("Two issues in the files you just changed.")
  ```
- `ext.host.agents.promote_ephemeral(name, target)` keeps an agent the
  orchestrator made for one task, as `/agents promote` does: `target` is
  `"project"` (the default — `.igni/agents/`, committed and the team's,
  `--project`) or `"personal"` (`.igni/agents.local/`, kept out of git,
  `--personal`). Leaving it out means the project. It needs the `agents`
  grant:

  <!-- continues -->
  ```python
  async def keep_for_me(name: str) -> None:
      await ext.host.agents.promote_ephemeral(name, target="personal")
  ```
- `await ext.host.notifications.notify(LocalNotification(...))` tells
  the person at this machine something — it never leaves the machine;
  igni opens `target` when it is clicked and shows a repeated `event_key`
  once. To tell *other* people, the extension's service uses
  `host.notify` in `igni-service`.

**Every object needs its grant** in the manifest's `permissions.host`
(`"host": ["sessions", "chat.read", "chat.post"]`); `host.workspace` is
granted by `permissions.workspace`. An object not granted raises
`HostError` with `code == "not_granted"`, and its signals never arrive.
Other codes: `not_found`, `invalid`, `read_only`, `refused` (the person
could not have done it either), `stage_2` (defined, not yet built — no
member today), `unavailable`, `failed`. `code` is a `HostErrorCode`, a
`StrEnum`, so `error.code == "not_granted"` and
`error.code is HostErrorCode.NOT_GRANTED` both hold.

`ext.host.ui` is the window the person last used: its page, panel,
sidebar, preview, selection, composer, toasts, dialog and scroll — the
same model igni's own views bind to, so a write shows in the window and
whatever the person does there is a signal. A dialog waits for the
person:

<!-- continues -->
```python
@ext.tool(hints=ToolHints(read_only=False, destructive=False, idempotent=False, open_world=False))
async def ship() -> str:
    answer = await ext.host.ui.show_dialog(
        {"title": "Ship it?", "buttons": [{"id": "yes", "label": "Ship", "primary": True}]}
    )
    if answer and answer["button"] == "yes":
        await ext.host.ui.toast("Shipping.", "success")
        return "shipped"
    return "not shipped"
```

`ext.host` travels on a loopback socket igni gives the process
(`IGNI_HOST_URL`, `IGNI_HOST_SECRET`). `ext.run()` connects and stays
connected so signals arrive; a script or test that does not call `run()`
calls `await ext.connect()`. Run outside igni, `ext.host` raises
`NotConfigured` — except `notifications.notify`, which then travels as
the MCP notification igni has always understood.

`igni_extension.host_model` is generated from igni's host schema; its
`SCHEMA_VERSION` is the model version it was generated for.

## Having a say: `@ext.hook`

igni's hook events (`docs/HOOKS.md` in igni) are where an extension gets
a say before something happens, in one pipeline with igni's own
settings-file hooks:

<!-- continues -->
```python
from igni_extension import Hooks, Modify, Reject
from igni_extension.hooks import PlanSubmit, PreToolUse

@ext.hook(Hooks.PLAN_SUBMIT)
async def grounded(plan: PlanSubmit) -> Reject | None:
    if plan.attempt < 2 and plan.plan.count("src/") < 2:
        return Reject("Cite the files the plan changes.")   # back to the agent
    return None                                             # allowed

@ext.hook(Hooks.PRE_TOOL_USE)
async def quieter(call: PreToolUse) -> Modify | None:
    if call.tool_name == "run_shell_command":
        command = call.tool_args.get("command", "") + " --quiet"
        return Modify(call.model_copy(update={"tool_args": {**call.tool_args, "command": command}}))
    return None
```

Five events decide something — `PRE_TOOL_USE` (may change `tool_args`),
`POST_TOOL_USE` (`result`), `USER_PROMPT_SUBMIT` (`message`), `STOP`,
`PLAN_SUBMIT` (`plan`). A handler answers `Allow()` (or `None`),
`Reject(reason)` — the reason reaches the agent or the person, so write
it for them — or `Modify(value)`: the whole value, with only the fields
the event lets it change changed. Every other event (`SESSION_START`,
`SUBAGENT_STOP`, `PRE_COMPACT`, …) is only told: the handler hears it,
and what it returns is not read.

igni asks only about the events in the manifest's `permissions.hooks`
(`["PreToolUse", "PlanSubmit"]`), in the super admin's priority order
of extensions, each handed what the one before left, before igni's own
configured hooks. igni sets the deadline (two seconds) and decides what
a silence means: a handler that raises, or is too slow, has not
answered, and a tool call or a message then does not go ahead, while
everything else does. Three silences in a row, hooks and tool calls
alike, make the extension faulty: igni restarts it and stops waiting on
its hooks until it answers again. One handler per event.
`FakeHost.hook(event, value)` asks a test's extension as igni would, and
`FakeHost.tell(event, value)` tells it one that decides nothing.

## What your service sends: events and messages

Your service reaches a producer's igni — on their desktop, or in their
Fleet VM, woken for it — two ways (igni server's service-identity §15,
igni's `docs/CLOUD.md` §6). Send a message when the service knows what
the agent should read; send an event when this extension should decide.

**An event is yours** (`agent.send_event` in `igni-service`). igni hands
it to your handler for its type and posts nothing. The handler does what
it wants through its host calls, and the agent runs only if it posts.
To have the agent work on it in a chat of its own, open one and post
there:

<!-- continues -->
```python
from igni_extension import ServiceEvent

@ext.on_service_event("index.ready")
async def index_ready(event: ServiceEvent) -> None:
    repo = event.payload["repo"]
    created = await ext.host.sessions.create(title=f"Index: {repo}")
    await ext.host.sessions.chat(created.session_id).post(
        f"The index for {repo} is ready; review the open changes."
    )
```

`sessions.create` returns the new session (`created.session_id`) and
moves no window, so the person stays where they are; the title shows in
their session list at once, and no auto-name replaces it. Pass
`focus=True` to show it in the caller's window instead. Creating needs
the `sessions` grant; posting into a session that is not on screen needs
`sessions.post` as well as `chat.post`:

```json
"permissions": {"host": ["sessions", "chat.post", "sessions.post"]}
```

Without a type the handler takes every type
no other handler names. An event with no handler for its type, one that
reaches no running extension, or one whose handler raises is *not
handled*: your service reads `not_handled`, with the reason, when it
polls. igni waits up to 10 s; delivery is at least once, and igni drops a
repeat by `event_id`. A handler that returns text used to have igni post
it; igni no longer does — the text is ignored, with a warning in the log.

**A message is the agent's** (`agent.send_message`, `new_chat`,
`chat(thread)`). igni posts it exactly as sent, labelled as this
extension's, in the chat its thread names or in this extension's own
chat; no handler is asked, and your extension cannot rewrite, redirect
or drop it. It sees it, as any post, on `chat.message_added`, with
`from_extension` set to this extension's id and `from_service` to your
service's client id. A post this extension made itself has
`from_service` set to `None`:

<!-- continues -->
```python
from igni_extension.host_model import ChatMessage

@ext.on(ext.host.chat.message_added)
async def seen(message: ChatMessage) -> None:
    if message.from_service is not None:
        print("the service sent it, as a message item:", message)
```

What your service may send beyond a plain event, you declare here:

```json
"permissions": {"service": ["messages", "attachments", "chats"]}
```

`messages` lets it post text, `attachments` send files, `chats` name
threads (each opens a chat), open chats and act on them. The super admin
approves them with the service, and igni server refuses the rest.

## Staying awake: `ext.busy`

In the cloud igni's VM sleeps when nothing is busy. Work your process
does on its own — indexing, a sync — says so:

<!-- continues -->
```python
async def index_everything() -> None: ...   # your own work


@ext.on_start
async def index() -> None:
    async with ext.busy_while("indexing"):
        await index_everything()
```

`ext.busy(reason, ttl)` takes a lease (at most 600 s) and renews it when
called again; `ext.done(reason)` lets it go; `busy_while` does both and
renews in between. A lease nobody renews expires, so a stuck process
cannot keep the VM up.

## Plain functions run off the loop

The process serves igni on one event loop. A plain `def` you hand the
SDK — a tool, a hook handler, a reaction (`@ext.on`), a background job
(`@ext.on_start`) — runs in a worker thread, so a slow one never holds
the calls that arrive meanwhile; an `async def` is called as it is. A
tool's schema is FastMCP's, from the function as you wrote it.

Two things this does not do: blocking inside an `async def` still holds
the loop, and threads share the GIL — CPU-heavy work belongs in a
process pool you run (`concurrent.futures.ProcessPoolExecutor`). The
thread pool is anyio's default (40); it is your process, so change it if
you need to:

<!-- continues -->
```python
import anyio.to_thread

anyio.to_thread.current_default_thread_limiter().total_tokens = 8
```

## The agent's prompt: `ext.prompt`

A manifest's `contributes.prompt` overrides a prompt — the main prompt,
plan mode, any agent — with a whole one of its own, or edits it: appends a
section, inserts one before or after a named section, replaces a named
section (an agent's header is the section `frontmatter`). Each may hold
only while a condition holds (`"when": "index-covers-head"`). The rules —
what stacks, what collides, what an override leaves out — are in igni's
`docs/EXTENSIBILITY.md`. Only the extension can judge the condition, so it
says when it changes:

<!-- continues -->
```python
await ext.prompt.hold("index-covers-head", True)
await ext.prompt.set({"index-covers-head": False, "signed-in": True})
```

igni rebuilds the agent with the prompt as it now is. It travels on the
process's own MCP connection (`notifications/igni/conditions`), works
before igni has finished connecting, and sends only what changed. Nothing
holds until it is said, and nothing survives a restart: say it again when
the process starts.

Text a contribution takes from a tool (`"source": "tool"`) is fetched when
the extension starts, and so is a `promptFragments` anchor's. When what it
says changes — CodeIndex's project map, when HEAD moves to another indexed
commit — have igni fetch it again:

<!-- continues -->
```python
await ext.prompt.refresh()
```

## Its own work, and its own directory

Work the extension does for as long as it runs — a sync loop, processes it
starts — goes in `@ext.on_start`. It starts with `ext.run()` and is
cancelled when igni closes the extension, so it cleans up in its own
`finally`:

<!-- continues -->
```python
import asyncio

class Index:  # your own work
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

index = Index()


@ext.on_start
async def follow_head() -> None:
    try:
        await index.start()
        await asyncio.Event().wait()
    finally:
        await index.stop()
```

igni provides no database, vector store or model to extensions. What one
keeps on this machine goes in `ext.environment.data_dir`
(`IGNI_EXTENSION_DATA`): a directory that is this extension's alone and
survives reinstalls and updates of its bundle.

What it *runs* that is not Python — a database, a JVM, a model — it
ships, as `assets` in its manifest, so it installs with no network. igni
checks each against the digest the signed manifest pins and lays it out
read-only, one directory per asset (`IGNI_EXTENSION_ASSETS`):

<!-- continues -->
```python
@ext.on_start
def where_things_are() -> None:
    neo4j_home = ext.assets.path("neo4j")                 # an unpacked archive
    model = ext.assets.file("minilm", "model.onnx")       # one file, as shipped
    print(neo4j_home, model)
```

Both raise `NotConfigured` for a name igni did not lay out — one the
manifest does not declare, or a process running outside igni.

**The process is sandboxed to its grants.** igni starts it in an OS
sandbox written from its manifest — Seatbelt on macOS, bubblewrap on
Linux. It reaches its own service and its `permissions.egress` hosts
through igni's egress proxy and nothing else (only hosts inside the
network when air-gapped; on macOS, services on this machine stay
reachable); it reads its bundle, its runtime, its data directory and the
workspace when granted, and not the rest of the person's home; it writes
its data directory, its temp directory and the workspace for `write`. On
Windows it is not sandboxed yet, and where no sandbox is available it runs
unconfined with a warning — unless managed settings set
`extensions.require_sandbox`, which refuses it. `extensions.sandbox:
false` (managed or user settings) turns it off while you develop. The
sandbox confines; it does not vouch: approving the service is still the
decision to run its code, and an org admin switching the extension on for
a group is the decision to run it for those people.

## Testing an extension

**One tool, as igni calls it:** `HostHarness(ext)` is igni's side of the
MCP session `ext.run()` serves, and `host.call(tool, args, caller=...)`
calls one tool for one caller. A `Caller` is whom igni says the call is
for — build one per person a test plays:

```python
import json

from igni_extension import Caller, CallerKind, IgniExtension, ToolHints
from igni_extension.testing import ConcurrencyHarness, ConcurrentCall, HostHarness

ext = IgniExtension("acme.example")
READS = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)


@ext.tool(hints=READS)
async def mine(caller: Caller) -> str:
    return json.dumps({"for": caller.id})


ada = Caller(kind=CallerKind.PERSON, id="u-ada", org="org-1", group="g-engineering")
grace = Caller(kind=CallerKind.PERSON, id="u-grace", org="org-1", group="g-engineering")

async with HostHarness(ext) as host:
    result = await host.call("mine", {}, caller=ada)
assert json.loads(result.content[0].text) == {"for": "u-ada"}
```

**Many callers at once:** `ConcurrencyHarness(ext).run([...])` makes
every call concurrently, each as its own caller, and raises
`ConcurrencyProblem` if the event loop was held longer than
`stall_limit` (0.1 s), or if a tool saw a caller other than the one its
call was sent with:

<!-- continues -->
```python
report = await ConcurrencyHarness(ext).run([
    ConcurrentCall(tool="mine", caller=ada),
    ConcurrentCall(tool="mine", caller=grace),
])
assert report.results[0].result != report.results[1].result
```

`FakeHost.hook(event, value, caller=...)` asks a hook the same way.

**Its service, and what it tells the host:** `FakeHostTokenEndpoint`
hands out tokens the way the igni host does, and answers as igni does
when nobody is signed in (`host_tokens.sign_out()`). Give the extension
your service as an `httpx.MockTransport`:

```python
import httpx

from igni_extension import IgniExtension, LocalNotification, Target, ToolHints
from igni_extension.testing import FakeHostTokenEndpoint, HostHarness


def my_fake_service(request: httpx.Request) -> httpx.Response:
    """The extension's service, as the test wants it to answer."""
    assert request.headers["authorization"].startswith("Bearer ")
    return httpx.Response(200, json={"summary": f"{request.url.path} in one line"})


host_tokens = FakeHostTokenEndpoint()
ext = IgniExtension(
    "acme.example",
    environ=host_tokens.environment(),
    token_transport=host_tokens.transport,
    service_transport=httpx.MockTransport(my_fake_service),
)
READS = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)


@ext.tool(hints=READS)
async def summarise(slug: str) -> str:
    response = await ext.service.get(f"/documents/{slug}/summary")
    return str(response.json()["summary"])


@ext.tool(hints=ToolHints(read_only=False, destructive=False, idempotent=False, open_world=False))
async def export(slug: str) -> str:
    await ext.host.notifications.notify(
        LocalNotification(
            kind="export_ready",
            title=f"{slug}.pdf",
            body="Your export is ready",
            target=Target(page="acme.example", params={"export": slug}),
            event_key=f"export:{slug}",
        )
    )
    return slug


async with HostHarness(ext) as host:              # igni's side of the MCP session
    summary = await host.call("summarise", {"slug": "runbook"})
    exported = await host.call("export", {"slug": "runbook"})
    [notification] = await host.received(1)

assert summary.content[0].text == "/documents/runbook/summary in one line"
assert not exported.isError
assert notification.title == "runbook.pdf"
```

Its tokens are unsigned JWTs of the person's claims; to test against a
service that checks signatures — one built on `igni-service`, with its
`FakeIgni` — pass `minter`, which is given the person's `TokenClaims` and
returns the token to hand out (this needs `igni-service` installed beside
`igni-extension`):

<!-- illustrative -->
```python
from igni_extension.testing import FakeHostTokenEndpoint
from igni_service.testing import FakeIgni

igni = FakeIgni()
host_tokens = FakeHostTokenEndpoint(
    minter=lambda claims: igni.person_token(claims.sub, org_id=claims.org)
)
```

`HostHarness` connects to the extension over the same MCP session
`ext.run()` serves, and records what it tells the host.

**The host:** `FakeHost` is igni's host object model on a loopback
socket, speaking the protocol igni speaks:

```python
import asyncio

from igni_extension import IgniExtension
from igni_extension.host_model import Run
from igni_extension.testing import FakeHost

async with FakeHost(values={"chat.processing": False}, answers={"chat.post": None}) as host:
    ext = IgniExtension("acme.example", environ=host.environment())

    @ext.on(ext.host.chat.run_completed)
    async def review(run: Run) -> None:
        if run.changed_files:
            await ext.host.chat.post(f"I read {', '.join(run.changed_files)}.")

    assert await ext.host.chat.processing is False
    await ext.connect()
    await host.wait_subscribed("chat.run_completed")
    await host.emit(
        "chat.run_completed",
        {"session_id": "s-1", "run_id": "r-1", "parent_run_id": "", "input_tokens": 10,
         "output_tokens": 5, "reasoning_tokens": 0, "duration": 1.5,
         "changed_files": ["auth.py"]},
    )                                               # your handler runs
    for _ in range(100):
        if any(call.path == "chat.post" for call in host.calls):
            break
        await asyncio.sleep(0.01)
    await ext.aclose()

assert any(call.path == "chat.post" for call in host.calls)  # everything the extension asked
```
