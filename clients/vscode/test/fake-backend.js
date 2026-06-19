#!/usr/bin/env node
/**
 * Fake "Python with ignite-ember" used by the extension test suite.
 *
 * The extension spawns ``<pythonPath> -m ember_code.backend
 * --ws-port 0 --project-dir <dir>`` and waits for a JSON ready line
 * on stdout. We mimic that here so the activation path can complete
 * without the real Python/uv/ignite-ember bootstrap. The fake
 * doesn't start a real WS server — the webview just won't connect
 * (which is fine; the tests don't drive the webview).
 *
 * Set ``emberCode.pythonPath`` to this file's absolute path in the
 * test setup. Note: this file lives in ``test/`` so the published
 * VSIX never bundles it (excluded by ``.vscodeignore`` via the
 * ``src/**`` and ``test/**`` filters).
 */

// Print ready immediately. ``ws_port: 0`` is fine — the FE will try
// to connect, fail, and surface a "Connecting…" placeholder. Tests
// assert on extension-host state (commands registered, panel mounted)
// rather than webview DOM.
//
// We emit the FULL multi-field ready envelope the real BE produces
// (``ws_url`` alongside ``ws_port``) so the extension's stdout
// parser is exercised against the production shape — protects
// against a future tightening that accidentally rejects unknown
// fields and breaks the BE handshake.
process.stdout.write(
  JSON.stringify({
    status: "ready",
    ws_port: 65535,
    ws_url: "ws://127.0.0.1:65535",
  }) + "\n",
);

// Idle. The extension's process_manager kills us when the workspace
// closes; the test harness times out anyway. Keep stdin open so we
// don't exit on parent stdio close.
process.stdin.resume();
