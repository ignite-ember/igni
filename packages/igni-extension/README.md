# igni-extension

An igni extension's process, as its author writes it: tools igni can
call, its own service reached as the signed-in person, and the host it
runs in — without an address, a token or a protocol message in the
extension's code.

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


def main() -> None:   # the console script the manifest's `python.entry` names
    ext.run()
```

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

## Installing

From igni's package index, not PyPI. Name the index and pin the package
to it, so nothing called `igni-extension` is ever taken from anywhere else:

```toml
[[tool.uv.index]]
name = "igni"
url = "https://us-central1-python.pkg.dev/ember-prod-491411/ember-prod-python/simple/"
explicit = true

[tool.uv.sources]
igni-extension = { index = "igni" }
```

```bash
export UV_INDEX_IGNI_USERNAME=oauth2accesstoken
export UV_INDEX_IGNI_PASSWORD="$(gcloud auth print-access-token)"   # an hour
uv add igni-extension
```

Your Google account needs read access to the index (igni's developers'
group has it). The versions, the other ways to authenticate, and working
against an igni checkout instead are in igni's
`docs/PUBLISHING_THE_SDKS.md`.

## Its service: `ext.service`

An `httpx.AsyncClient` for the extension's own service, as the person
signed in to igni. Paths are relative to the service. The person's token
comes from the igni host, is attached to every request, and is renewed
before it expires — the extension never sees the person's igni session.
When nobody is signed in to igni, a request raises `NotSignedIn`.

- `ext.service.anonymous` — the same service with no token, for what
  anyone may read (a public link, a health check). It never asks the host
  for a token, so it works when nobody is signed in:
  `await ext.service.anonymous.get(f"/v1/public/{slug}")`.
- `ext.service.websocket(path)` — a WebSocket to the service, opened with
  the person's token in its `Authorization` header:

  ```python
  async with ext.service.websocket(f"/v1/rooms/{slug}") as room:
      await room.send(json.dumps({"hello": "room"}))
      async for frame in room: ...
  ```

- `await ext.tokens.token()` — the token itself, for a client neither of
  those covers.
- `ext.tokens.signed_in` — who the last token was for (`user_id`,
  `org_id`, `email`, `name`), read from it without checking it; `None`
  before the first token or once nobody is signed in. For showing who is
  signed in and noticing that it changed — the service checks the token,
  this never decides anything. Whether anyone is signed in to igni at all
  is `await ext.host.account.status` (with the `account` grant).

## The host: `ext.host`

`ext.host` is igni as the extension sees it — the **host object model**
(`docs/HOST_OBJECT_MODEL.md` in igni): one object per part of the app,
each with properties, signals and methods. An extension can do anything
igni's own UI can do, and see everything that happens in it.

```python
from igni_extension.host_model import Run, SessionChange

@ext.on(ext.host.sessions.current_changed)
async def follow(change: SessionChange) -> None:
    busy = await ext.host.chat.processing           # read a property
    ...

@ext.on(ext.host.chat.run_completed)                 # handle a signal
async def review(run: Run) -> None:
    if run.changed_files:
        await ext.host.chat.post("Two issues in the files you just changed.")

await ext.host.sessions.current.set(session_id)      # write a property
await ext.host.windows.ui(window_id).navigate("plugins")  # any window
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
member today), `failed`.

`ext.host.ui` is the window the person last used: its page, panel,
sidebar, preview, selection, composer, toasts, dialog and scroll — the
same model igni's own views bind to, so a write shows in the window and
whatever the person does there is a signal. A dialog waits for the
person:

```python
answer = await ext.host.ui.show_dialog(
    {"title": "Ship it?", "buttons": [{"id": "yes", "label": "Ship", "primary": True}]}
)
if answer and answer["button"] == "yes":
    await ext.host.ui.toast("Shipping.", "success")
```

`ext.host` travels on a loopback socket igni gives the process
(`IGNI_HOST_URL`, `IGNI_HOST_SECRET`). `ext.run()` connects and stays
connected so signals arrive; a script or test that does not call `run()`
calls `await ext.connect()`. Run outside igni, `ext.host` raises
`NotConfigured` — except `notifications.notify`, which then travels as
the MCP notification igni has always understood.

`igni_extension.host_model` is generated from igni's host schema; its
`SCHEMA_VERSION` is the model version it was generated for.

## Before it happens: `@ext.intercept`

A signal says what happened; an intercept is asked first, and its answer
counts:

```python
from igni_extension import Modify, Reject
from igni_extension.host_model import Intercepts, PlanSubmitting, ToolCalling

@ext.intercept(Intercepts.PLAN_SUBMITTING)
async def grounded(plan: PlanSubmitting) -> Reject | None:
    if plan.attempt < 2 and plan.plan.count("src/") < 2:
        return Reject("Cite the files the plan changes.")   # back to the agent
    return None                                             # allowed

@ext.intercept(Intercepts.TOOLS_CALLING)
async def quieter(call: ToolCalling) -> Modify | None:
    if call.tool == "run_shell_command":
        command = call.args.get("command", "") + " --quiet"
        return Modify(call.model_copy(update={"args": {**call.args, "command": command}}))
    return None
```

A handler answers `Allow()` (or `None`), `Reject(reason)` — the reason
reaches the agent or the person, so write it for them — or
`Modify(value)`: the whole value, with only the fields the event lets it
change changed. `Intercepts` lists every event: `CHAT_SUBMITTING`,
`TOOLS_CALLING`, `PLAN_SUBMITTING`, and every write to the model by its
member (`UI_NAVIGATE`, `SESSIONS_CURRENT`, …), which can be rejected but
not changed.

igni asks only about the events in the manifest's
`permissions.intercepts` — `{"event": "tools.calling", "modify": true}`
to change one, not just reject it — and only while the extension holds
the grant the event needs in `permissions.host`. igni sets the deadline
(two seconds) and decides what a silence means: a handler that raises,
or is too slow, has not answered, and a tool call or a message then does
not go ahead, while a plan or a write does. Several extensions
intercepting one event are asked in the super admin's priority order,
each handed what the one before left. One handler per event; an
extension is never asked about its own writes. `FakeHost.intercept(event,
value)` asks a test's extension as igni would. The rules are
`docs/HOST_OBJECT_MODEL.md`, *Intercepting*, in igni.

## The agent's prompt: `ext.prompt`

A manifest's `contributes.prompt` overrides a prompt — the main prompt,
plan mode, any agent — with a whole one of its own, or edits it: appends a
section, inserts one before or after a named section, replaces a named
section (an agent's header is the section `frontmatter`). Each may hold
only while a condition holds (`"when": "index-covers-head"`). The rules —
what stacks, what collides, what an override leaves out — are in igni's
`docs/EXTENSIBILITY.md`. Only the extension can judge the condition, so it
says when it changes:
Only the extension can judge the condition, so it says when it changes:

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

```python
await ext.prompt.refresh()
```

## Its own work, and its own directory

Work the extension does for as long as it runs — a sync loop, processes it
starts — goes in `@ext.on_start`. It starts with `ext.run()` and is
cancelled when igni closes the extension, so it cleans up in its own
`finally`:

```python
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

```python
neo4j_home = ext.assets.path("neo4j")                 # an unpacked archive
model = ext.assets.file("minilm", "model.onnx")       # one file, as shipped
```

Both raise `NotConfigured` for a name igni did not lay out — one the
manifest does not declare, or a process running outside igni.

**The process is trusted, not confined.** It is the service's own code;
`permissions.egress` sets what the extension's *pages* may connect to,
not where this process may. Approving the service is the decision to
trust it.

## Testing an extension

```python
from igni_extension.testing import FakeHost, FakeHostTokenEndpoint, HostHarness

host_tokens = FakeHostTokenEndpoint()
ext = IgniExtension(
    "acme.example",
    environ=host_tokens.environment(),
    token_transport=host_tokens.transport,
    service_transport=httpx.MockTransport(my_fake_service),
)

async with HostHarness(ext) as host:              # igni's side of the MCP session
    await host.session.call_tool("export", {"slug": "runbook"})
    [notification] = await host.received(1)
```

`FakeHostTokenEndpoint` hands out tokens the way the igni host does, and
answers as igni does when nobody is signed in (`host_tokens.sign_out()`).
Its tokens are unsigned JWTs of the person's claims; to test against a
service that checks signatures, pass `minter`, which is given the
person's `TokenClaims` and returns the token to hand out:

```python
igni = FakeIgni(...)                               # igni_service.testing
host_tokens = FakeHostTokenEndpoint(
    minter=lambda claims: igni.person_token(claims.sub, org_id=claims.org)
)
```

`HostHarness` connects to the extension over the same MCP session
`ext.run()` serves, and records what it tells the host.

`FakeHost` is igni's host object model on a loopback socket, speaking
the protocol igni speaks:

```python
async with FakeHost(values={"chat.processing": False}, answers={"chat.append": "v1"}) as host:
    ext = IgniExtension("acme.example", environ=host.environment())
    assert await ext.host.chat.processing is False
    await ext.connect()
    await host.wait_subscribed("chat.run_completed")
    await host.emit("chat.run_completed", {...})   # your handler runs
    host.calls                                      # everything the extension asked
```
