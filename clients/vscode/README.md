# igni — VSCode extension

Hosts the shared web UI (`clients/web`) in a webview panel; spawns the
Python backend for the open workspace over a loopback WebSocket.

## Build

```bash
cd clients/vscode
npm install
npm run build       # builds ../web, copies dist → media/, compiles TS
```

Or use the repo-root helper (refreshes VSCode + JetBrains assets in one
shot):

```bash
scripts/build-clients.sh
```

## Run (development)

1. Open `clients/vscode/` as a folder in VSCode.
2. Press **F5** — VSCode launches a new "Extension Development Host"
   window with the plugin loaded.
3. In the host window, open any project folder, then run
   **igni: Open** from the command palette (`⇧⌘P`).

Set `emberCode.pythonPath` (Settings → Extensions → igni) if
`ignite-ember` lives in a venv.

## Smoke-test checklist

After F5 + `igni: Open`:

- [ ] Webview shows the Ember UI (sidebar + composer, dark theme).
- [ ] Status bar bottom-right shows a non-zero `ctx N · X%` counter.
- [ ] Sending `hi` produces a streamed reply.
- [ ] `/ctx` shows the runs/floor breakdown.
- [ ] Clicking a file pill in chat **opens the file in a VSCode editor
      tab** (not the in-app preview).
- [ ] A `/schedule` notification (fire a 1-minute task, then leave the
      panel) surfaces as a native VSCode information toast.

## How the host bridge works

The shared web UI calls `host.openFile(path)` /
`host.notify({title, body})`. When it detects a VSCode webview (via
`acquireVsCodeApi`) it `postMessage`s `ember:openFile` /
`ember:notify`, and `extension.ts:registerHostBridge` maps those to
`vscode.window.showTextDocument` and
`vscode.window.showInformationMessage`. See
`../web/src/lib/host.ts` for the FE side.
