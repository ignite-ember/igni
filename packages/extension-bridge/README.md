# `@ignite-ember/extension-bridge`

The guest half of the bridge between an igni extension's page and igni:
`connect()`, RPC to the extension's tools, navigation, and the theme. The
host imports the same `protocol`, so there is one description of the wire.

```ts
import { connect } from "@ignite-ember/extension-bridge";

const bridge = await connect();          // igni has said hello; the theme is applied
bridge.onParams((params) => render(params));
const found = await bridge.rpc("ext.example.teamnotes.search", { query: "" });
```

Writing a page for the first time? [Your first extension](../../docs/YOUR_FIRST_EXTENSION.md)
builds one end to end. A page needs no build step at all to start: it can
post the wire messages itself (`igni ext init` writes one that does);
this package is that, with types.

`connect()` resolves once igni has told the page which extension it is,
its launch params and the theme. Subpaths: `@ignite-ember/extension-bridge/protocol`
(the wire format) and `@ignite-ember/extension-bridge/tokens` (the design tokens).

## The host: `bridge.host`

`bridge.host` is igni as the page sees it — the **host object model**
(`docs/HOST_OBJECT_MODEL.md` in igni), the same model the extension's
process reaches as `ext.host`. Names are the schema's, as in Python.

```ts
const busy = await bridge.host.chat.processing.get();      // a property
await bridge.host.chat.post("Two issues in auth.py.");     // a method
await bridge.host.ui.navigate("plugins");                   // this page's window
const off = bridge.on(bridge.host.chat.run_completed, (run) => {
  if (run.changed_files.length) render(run.changed_files);
});
```

Every object needs its grant in the manifest's `permissions.host`; one
that is not granted rejects with a `HostError` whose `code` is
`not_granted`, and its signals never arrive. A page's `host.ui` is the
window it is framed in. The types (`Run`, `SessionChange`, …) are
exported from the package; `hostModel.generated.ts` is generated from
igni's host schema and `SCHEMA_VERSION` says which version.

A page observes; it cannot answer a hook. Being asked before something
happens (igni's hook events, `@ext.hook` in `igni-extension`) is the
extension's process's, because igni would otherwise wait on whatever tab
happens to be open.

## Its own service: `bridge.service`

The page talks to the service that shipped the extension directly, as
the signed-in person. igni fetches a ten-minute token for it — the same
exchange as the process's `ext.service`, audience that service and no
other — keeps it and renews it before it expires.

```ts
bridge.service.url;                                    // null when it has none (below)
const res = await bridge.service.fetch("/v1/notes");   // Authorization: Bearer …
const socket = await bridge.service.websocket("/v1/rooms/a");
socket.addEventListener("message", (e) => render(JSON.parse(e.data)));
```

`url` is `null` when the extension has no service a page may reach, and
then `fetch` and `websocket` reject with `no_service`. An extension igni
distributed always has one: the service that shipped it. A development
install has one only when its manifest declares a `serviceUrl` setting
(`contributes.settings`) and it is set; a plain-`http` URL counts only for
this machine (`localhost`, `127.0.0.1`, `::1`) and only with
`IGNI_DEV_EXTENSIONS=1`, so a service on another host must be `https`.

`websocket` resolves once the socket is open and has sent
`{"type": "auth", "token": …}` as its first frame (a browser socket
cannot set a header); pass `{ authFrame: (token) => … }` for a service
that reads something else. `fetch` retries once with a new token after
a `401`. Both refuse a path on any other origin with a `ServiceError`
(`other_origin`): the token goes to the service only. No
`permissions.host` grant is needed. The frame's CSP admits the
service's origin over `https`/`wss` and the hosts declared in
`permissions.egress`, nothing else. `fetch` sends the token and never
cookies (`credentials: "omit"`); a service on igni-service answers it
cross-origin with nothing to configure (`igni.install(app)`).

## Installing

> **Until it is on npm: install it from a checkout.** The package is not
> published yet, so `npm install @ignite-ember/extension-bridge` finds
> nothing. Pack it from an igni checkout and install the tarball:
>
> ```bash
> git clone https://github.com/ignite-ember/igni.git
> npm pack --pack-destination ./sdk-packages ./igni/packages/extension-bridge
> npm install ./sdk-packages/ignite-ember-extension-bridge-0.1.0.tgz
> ```
>
> (`mkdir sdk-packages` first.) It ships TypeScript source (*Source, not a
> build*, below), so the tarball is all there is.

Once published, from npm:

```bash
npm install @ignite-ember/extension-bridge
```

Each release's tarballs are also attached to igni's GitHub release
(`sdk-v<version>`), for a build that cannot reach npmjs. Versions are in
igni's `docs/PUBLISHING_THE_SDKS.md`.

## Source, not a build

Like `@ignite-ember/ui-react`, this package ships its TypeScript source. A bundler
that compiles TypeScript (Vite, esbuild, Rollup with a TS plugin) consumes
it as is; one that does not compile `node_modules` (Next.js, Jest) needs
it listed for transpilation (`transpilePackages`, `transformIgnorePatterns`).
