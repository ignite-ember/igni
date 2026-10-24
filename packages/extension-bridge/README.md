# `@igni/extension-bridge`

The guest half of the bridge between an igni extension's page and igni:
`connect()`, RPC to the extension's tools, navigation, and the theme. The
host imports the same `protocol`, so there is one description of the wire.

```ts
import { connect } from "@igni/extension-bridge";

const bridge = await connect();          // igni has said hello; the theme is applied
bridge.onParams((params) => render(params));
const found = await bridge.rpc("ext.example.teamnotes.search", { query: "" });
```

`connect()` resolves once igni has told the page which extension it is,
its launch params and the theme. Subpaths: `@igni/extension-bridge/protocol`
(the wire format) and `@igni/extension-bridge/tokens` (the design tokens).

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

## Installing

From igni's npm registry, not npmjs — the `@igni` scope is mapped to it,
so no `@igni/*` package is ever taken from anywhere else:

```ini
# .npmrc, beside your package.json
@igni:registry=https://us-central1-npm.pkg.dev/ember-prod-491411/ember-prod-npm/
//us-central1-npm.pkg.dev/ember-prod-491411/ember-prod-npm/:_authToken=${IGNI_INDEX_TOKEN}
```

```bash
export IGNI_INDEX_TOKEN="$(gcloud auth print-access-token)"   # an hour
npm install @igni/extension-bridge
```

Your Google account needs read access to the registry (igni's developers'
group has it). Versions and the other ways to authenticate are in igni's
`docs/PUBLISHING_THE_SDKS.md`.

## Source, not a build

Like `@igni/ui-react`, this package ships its TypeScript source. A bundler
that compiles TypeScript (Vite, esbuild, Rollup with a TS plugin) consumes
it as is; one that does not compile `node_modules` (Next.js, Jest) needs
it listed for transpilation (`transpilePackages`, `transformIgnorePatterns`).
