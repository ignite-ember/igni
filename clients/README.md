# igni GUI clients

One shared web UI, three thin native shells — all speaking the same
FE↔BE protocol the TUI uses, over a loopback WebSocket added in
`ember_code.transport.websocket`:

```
                       ┌──────────────────────────────┐
                       │   python -m ember_code.backend │
                       │   --ws-port 0                 │
                       └──────────────┬───────────────┘
                                      │ ws://127.0.0.1:<port>
            ┌─────────────────────────┼─────────────────────────┐
            │                         │                         │
   ┌────────┴───────┐        ┌────────┴───────┐        ┌────────┴───────┐
   │  tauri/        │        │  vscode/       │        │  jetbrains/    │
   │  native window │        │  webview panel │        │  JCEF panel    │
   └────────┬───────┘        └────────┬───────┘        └────────┬───────┘
            └─────────────────────────┼─────────────────────────┘
                                      │
                              ┌───────┴────────┐
                              │  web/          │
                              │  React chat UI │
                              └────────────────┘
```

- **web/** — React + Vite + TS chat UI styled after the portal's
  LandingPage (ember orange→red gradient, Primer-style tokens).
  Streaming chat, thinking blocks, tool-call cards, HITL permission
  dialogs, slash commands, model picker, sessions. The WS URL comes
  from `?ws=` or `window.__EMBER_WS_URL__` (injected by shells).
- **tauri/** — Tauri v2 standalone app; spawns the BE, opens the UI.
- **vscode/** — extension command `igni: Open Chat`; bundles the
  UI into a webview, spawns the BE for the workspace folder.
- **jetbrains/** — tool window with JCEF; per-project BE service.

Each shell does only: spawn `python -m ember_code.backend --ws-port 0
--project-dir <dir>` (interpreter override: `EMBER_PYTHON`), parse the
JSON ready line for `ws_port`, point the web UI at it. Process
lifecycle: killed by the shell on close + BE's `EMBER_PARENT_PID`
watchdog as backstop.

## Build status

| Client    | Builds verified here | Needs |
|-----------|----------------------|-------|
| web       | yes (`npm run build`, e2e against real BE) | Node 20+ |
| vscode    | yes (`npm run build`) | Node 20+ |
| tauri     | scaffold only — no Rust toolchain on this machine | rustup |
| jetbrains | scaffold only — no JDK on this machine | JDK 17+ |

## First-pass scope (core)

Streaming chat, tool rendering, HITL dialogs, slash commands, model
picker, sessions, status bar, cancel, queued messages. Not yet ported
from the TUI: login flow, MCP/codeindex/queue/task panels, shell mode,
@-file autocomplete, /loop and /schedule panels.
