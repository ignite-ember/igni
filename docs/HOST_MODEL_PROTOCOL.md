# The host model's wire — for SDK maintainers

**Extension authors never read this.** They write `ext.host.chat.post(…)`
(`igni-extension`) or `bridge.host.chat.post(…)` (`@igni/extension-bridge`)
and the SDK does the rest. This page is for whoever maintains those SDKs,
or writes one for another language. The model itself — its objects,
members, permissions and stages — is `docs/HOST_OBJECT_MODEL.md`, and is
described once in `ember_code.extensions.host.definition`; the protocol
version below changes only when these messages do.

There are two channels and one request shape. The host answers both
through `HostModel.handle` (`src/ember_code/extensions/host/model.py`),
so a request means the same thing on either.

## The process's channel: a loopback WebSocket

igni starts an extension's process (`python` or `dev` packaging; never an
`endpoint`, which is somebody else's process) with:

| Variable | Value |
|---|---|
| `IGNI_HOST_URL` | `ws://127.0.0.1:<port>/v1/host` |
| `IGNI_HOST_SECRET` | this process's secret — the same value as `IGNI_SERVICE_TOKEN_SECRET`, when the extension has a service |

Connect with `Authorization: Bearer <IGNI_HOST_SECRET>` on the upgrade
request. The secret is the only thing that names the extension; there is
no extension id parameter, so a process can only ever be itself. A
restarted process gets a new secret, and the old one stops working; a
removed extension's connection is closed (code 4001).

On POSIX, also send `X-Igni-Process-Group: <the process's group id>`
(`os.getpgid(0)`). igni starts an extension's process in a session of
its own; if the process hangs and igni has to restart it, it ends that
group, having checked that it started the group's leader. A claim about
any other process is ignored.

The listener refuses, before upgrading: any path but `/v1/host` (404); a
request carrying an `Origin` header (403 — browsers send one, SDKs must
not, so a web page cannot reach the socket); a `Host` other than the
loopback address and port (421 — DNS rebinding); a secret it did not
issue (401). Frames are JSON text, at most 8 MiB.

The host speaks first:

```json
{"op": "hello", "protocol": 1, "schema_version": "1.0.0",
 "extension_id": "acme.reviewer", "granted": ["chat.read", "sessions", "workspace"]}
```

`granted` is every model permission the extension holds (`workspace`
when `permissions.workspace` is `read` or `write`). It lets an SDK warn
early; the host checks every request regardless.

## Requests

SDK → host, any number in flight; replies may arrive in any order.

```json
{"id": 7, "op": "get",  "path": "chat.processing"}
{"id": 8, "op": "set",  "path": "sessions.current", "value": "s-2"}
{"id": 9, "op": "call", "path": "chat.post", "params": {"text": "Done."}}
{"id": 10, "op": "call", "path": "ui.navigate", "window": "w-2", "params": {"page": "plugins"}}
{"id": 11, "op": "subscribe",   "signals": ["chat.run_completed", "session.switched"]}
{"id": 12, "op": "unsubscribe", "signals": ["session.switched"]}
```

* `id` — any JSON number or string; echoed, never interpreted.
* `path` — `object.member`, the schema's names (`chat.approvals.answer`).
  The root's members are bare (`changed`).
* `window` — only for a per-window object (`ui`): the window to address.
  Absent, the process's `host.ui` is the active window — the one the
  person last used.
* `params` — a method's parameters by name. Optional parameters may be
  left out or sent as `null`; an unknown one is refused.
* `value` — the value of a `set`.
* `signals` — signal **wire names** (below).

An SDK may give up on a reply that does not come (both shipped SDKs do,
after a while), with one exception: `ui.show_dialog` answers when the
person does, so it has no limit.

## Replies

```json
{"op": "reply", "id": 9, "ok": true, "result": null}
{"op": "reply", "id": 7, "ok": false,
 "error": {"code": "not_granted", "message": "'acme.reviewer' is not granted 'chat.read', …"}}
```

`code` is one of `not_granted`, `not_found`, `invalid`, `read_only`,
`stage_2`, `refused`, `unavailable`, `failed` (`ErrorCode` in
`ember_code.extensions.host.spec`). An SDK raises one error type carrying
`code` and the message; the message is a sentence for the extension's
author and is not meant to be parsed.

A `subscribe` answers `{"subscribed": […], "not_granted": […], "unknown": […]}`.
Subscribing to a signal whose object is not granted is **not** an error —
it is recorded, and nothing ever arrives — so an SDK should log the
`not_granted` list for its author, not raise.

## Signals

Host → SDK, whenever a subscribed signal fires and the extension may see
it:

```json
{"op": "signal", "signal": "chat.run_completed", "value": {"session_id": "…", "run_id": "…", …}}
```

`signal` is the wire name: `<object path>.<signal name>`
(`chat.approvals.requested`), except the four host events that predate
the model, which keep their topic strings — `session.switched`
(`sessions.current_changed`), `workspace.head-changed`,
`workspace.files-changed`, `extension.state-changed`. The root signal
`changed` carries `{"path", "value", "window"}`. Generated SDKs carry each
signal's wire name; do not derive it.

A value is already filtered to what the extension may see — a changed
file outside `workspacePaths` is not in the list — and a value may carry
fields this SDK's schema does not know yet (a newer igni); ignore them.
Run a signal's handlers off the read loop: a handler that reads the
model needs the loop free to receive the reply.

## Hook events

Host → SDK, for one of igni's hook events (`docs/HOOKS.md`) the
extension declared in `permissions.hooks`. A deciding event carries an
`id`, and igni waits for the answer; one that decides nothing carries
`"id": null`, and nothing is sent back:

```json
{"op": "hook", "id": "h-12", "event": "PreToolUse",
 "value": {"session_id": "…", "tool_name": "run_shell_command", "tool_args": {"command": "ls"}},
 "principal": {"kind": "person", "id": "u-1", "org": "o-1", "groups": ["g-1"], "session": null}}
{"op": "hook", "id": null, "event": "SessionStart", "value": {"session_id": "…", "resumed": false},
 "principal": {"kind": "person", "id": "u-1", "org": "o-1", "groups": ["g-1"], "session": null}}
```

`principal` is whom the event is for — the shape a tool call carries in
its MCP request's `_meta["igni/principal"]`
(`docs/EXTENSION_ROUTING_AND_CONCURRENCY.md` §6); `null` when nobody is
signed in. The SDK hands it to a handler that declares a `Caller`.

The process answers a deciding one with an `answer` request, whose reply
is an ordinary one:

```json
{"id": 31, "op": "answer", "params": {"hook": "h-12", "outcome": "allow"}}
{"id": 32, "op": "answer", "params": {"hook": "h-13", "outcome": "reject", "reason": "No shell on Fridays."}}
{"id": 33, "op": "answer", "params": {"hook": "h-14", "outcome": "modify", "value": {…the whole value…}}}
{"id": 34, "op": "answer", "params": {"hook": "h-15", "outcome": "failed", "reason": "index unreachable"}}
```

* `hook` — the `id` it answers. One answer each; an answer to something
  already answered, or that ran out of time, is refused (`not_found`).
* `outcome` — `allow`, `reject` (with a `reason`, which reaches the
  agent or the person — an empty one is not an answer), `modify` (with
  the whole `value`, only the event's changeable fields changed), or
  `failed` (the handler raised; igni treats it as a silence).

igni gives each answer a deadline it sets (two seconds); what a silence
means is the event's, in `docs/HOOKS.md`, *Extensions*. Handle hook
events off the read loop, as signals are handled, and keep handlers
short. A page's channel takes no `answer`: a page cannot answer a hook.

## The page's channel: the bridge

A page talks to the window it is framed in by `postMessage`
(`packages/extension-bridge/src/protocol.ts`), with the nonce from its
`hello`:

```ts
{igni: 1, kind: "host", nonce, id: "h1", op: "call", path: "chat.post", params: {text: "Done."}}
{igni: 1, kind: "host-reply", id: "h1", ok: true, result: null}
{igni: 1, kind: "signal", signal: "chat.run_completed", value: {…}}
```

The window (`ExtensionFrame`) checks the message's source, origin and
nonce, then forwards the request as the `host_model` RPC with the frame's
extension as `caller` — set by the window, never by the page — and its
own window id as `home_window`, so a page's `host.ui` is its own window.
The backend's reply is the same `{op, id, ok, result, error}` object,
returned as the RPC's result (an RPC error string would lose `code`).

A page's subscriptions are one set per extension, kept by the host; its
signals reach the windows as the `host_signal` push
(`{extension_id, signal, value}`), and each window posts them into that
extension's frames, where the bridge calls the handlers it has.

### Opening a link

`host.app.open_external` is an ordinary host-model call. The backend
checks the URL, then pushes `open_external` (`{window, url, source}`) to
every window; the window it names checks the URL again (`https:` with a
host, no credentials) and opens it as it opens any link outside igni:
the desktop app's opener bridge (`__EMBER_HOST__.openUrl`), else
`window.open(url, "_blank", "noopener,noreferrer")`. The page never
opens anything itself — its sandbox has no `allow-popups`.

### The service token

`bridge.service` asks the window for a token for the extension's own
service — not a host-model request, and no `permissions.host` grant:

```ts
{igni: 1, kind: "service-token", nonce, id: "s1"}
{igni: 1, kind: "service-token-reply", id: "s1", ok: true, token: "…", expiresIn: 600}
```

The message names no audience. The window forwards it as the
`extension_service_token` RPC with the frame's extension as `caller`,
and the backend answers for the service recorded on that extension's
origin (`PageServiceTokens`) from the same exchange and cache as the
process's token endpoint. `hello` carries that service as `serviceUrl`
(`null` when there is none). The bridge keeps the token until a minute
before it expires.

## Versions

`protocol` (in `hello`) is this page's version: bumped only for a change
an SDK cannot ignore. `schema_version` is the model's: semver under
EXTENSIBILITY §8 — a minor adds members, a major changes or removes them
with a deprecation window. Both generated SDKs carry the schema version
they were generated from as `SCHEMA_VERSION`.
