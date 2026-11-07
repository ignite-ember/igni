# igni GUI clients

One shared web UI, served as the web client and hosted by one thin
native shell — all speaking the same FE↔BE protocol the TUI used, over a
loopback WebSocket added in```
                       ┌──────────────────────────────┐
                       │   python -m igni_core.backend │
                       │   --ws-port 0                 │
                       └──────────────┬───────────────┘
                                      │ ws://127.0.0.1:<port>
                     ┌────────────────┴────────────────┐
                     │                                 │
            ┌────────┴───────┐                ┌────────┴───────┐
            │  tauri/        │                │  browser tab   │
            │  native window │                │  (web client)  │
            └────────┬───────┘                └────────┬───────┘
                     └────────────────┬────────────────┘
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
  from `?ws=` (set by the Tauri shell), else the dev port.
- **tauri/** — Tauri v2 standalone app; spawns the BE, opens the UI.

The VS Code and JetBrains plugins that used to live here were dropped
(D19); they are in the git history.

The shell spawns `python -m igni_core.backend --ws-port 0
--project-dir <dir>` (interpreter override: `IGNI_PYTHON`), parses the
JSON ready line for `ws_port`, and points the web UI at it. Process
lifecycle: killed by the shell on close + BE's `IGNI_PARENT_PID`
watchdog as backstop.

## Build status

| Client    | Builds verified here | Needs |
|-----------|----------------------|-------|
| web       | yes (`npm run build`, e2e against real BE) | Node 20+ |
| tauri     | scaffold only — no Rust toolchain on this machine | rustup |

no JDK on this machine | JDK 17+ |

## First-pass scope (core)

Streaming chat, tool rendering, HITL dialogs, slash commands, model
picker, sessions, status bar, cancel, queued messages. Not yet ported
from the TUI: login flow, MCP/codeindex/queue/task panels, shell mode,
@-file autocomplete, /loop and /schedule panels.
