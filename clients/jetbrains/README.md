# igni — JetBrains plugin

Tool window (right anchor) hosting the shared web UI (`clients/web`)
in a JCEF browser, backed by a per-project Ember backend process over
a loopback WebSocket.

## Build

Requires JDK 17+:

```bash
# From the repo root — refreshes web UI for both VSCode + JetBrains:
scripts/build-clients.sh

# Then build the plugin:
cd clients/jetbrains
gradle buildPlugin     # or: gradle runIde for a sandbox IDE
```

The plugin zip lands in `build/distributions/`.

Set `EMBER_PYTHON` if `ignite-ember` lives in a venv.

## Run (development)

```bash
cd clients/jetbrains
gradle runIde
```

This launches a sandbox IntelliJ IDEA Community with the plugin loaded.
Open any project, then open the **igni** tool window on the right.

## Smoke-test checklist

After `gradle runIde` + opening the igni tool window:

- [ ] Panel shows the Ember UI (sidebar + composer, dark theme).
- [ ] Status bar shows a non-zero `ctx N · X%` counter.
- [ ] Sending `hi` produces a streamed reply.
- [ ] `/ctx` shows the runs/floor breakdown.
- [ ] Clicking a file pill in chat **opens the file in an IntelliJ
      editor tab** (not the in-app preview).
- [ ] A `/schedule` notification (fire a 1-minute task, then leave the
      panel) surfaces as an IntelliJ balloon (group "EmberCode").

## How the host bridge works

The shared web UI calls `host.openFile(path)` /
`host.notify({title, body})`. When loaded in JCEF it detects
`window.cefQuery` and ships a JSON request like
`{"type": "ember:openFile", "path": "/abs/path"}`. The Kotlin side
(`EmberToolWindowFactory.installHostBridge`) registers a
`JBCefJSQuery` and dispatches `ember:openFile` →
`FileEditorManager.openFile` and `ember:notify` →
`NotificationGroupManager` (group `EmberCode`, declared in
`plugin.xml`). See `../web/src/lib/host.ts` for the FE side.

> NOTE: not compiled in CI yet — requires a JDK, which this dev machine
> doesn't have. The web UI and backend protocol are covered by tests;
> the Kotlin shell is spawn/JCEF glue.
