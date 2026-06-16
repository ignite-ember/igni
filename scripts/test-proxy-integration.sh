#!/usr/bin/env bash
# Integration test for the IDE-proxy propagation fix.
#
# What we're verifying:
#
#   The plugin code (EmberRuntime.kt + runtime.ts + extension.ts)
#   extracts the IDE's proxy setting and layers HTTPS_PROXY /
#   HTTP_PROXY env vars onto subprocesses. The subprocesses
#   themselves (uv, curl, the BE) honor those env vars natively.
#
#   This script proves the env-var path actually flows: we start a
#   tiny logging proxy, point HTTPS_PROXY at it, run the SAME
#   download command runtime.ts uses, and assert the proxy logged
#   a CONNECT to github.com — i.e. the request did not bypass the
#   proxy.
#
# What this does NOT test:
#
#   - The Kotlin / TypeScript code that reads IDE settings and
#     builds the env-var map. Those are covered by unit tests
#     (EmberRuntimeTest, etc.).
#   - HTTP authentication. The proxy is open; real corporate
#     proxies usually require Basic auth. The plumbing supports it
#     (the env var format ``http://user:pass@host:port`` is what
#     all clients accept) but we don't exercise it here.
#
# Usage:
#
#   scripts/test-proxy-integration.sh
#
# Exit 0 on pass, non-zero on fail. Logs go to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Start proxy ─────────────────────────────────────────────────────
PROXY_LOG="$(mktemp -t ember-proxy-log-XXXXXX)"
PROXY_OUT="$(mktemp -t ember-proxy-stdout-XXXXXX)"
node "$SCRIPT_DIR/test-proxy.mjs" --port 0 --log-file "$PROXY_LOG" \
    >"$PROXY_OUT" 2>/dev/null &
PROXY_PID=$!

cleanup() {
    kill -TERM "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
    rm -f "$PROXY_LOG" "$PROXY_OUT"
}
trap cleanup EXIT

# Wait for "listening on 127.0.0.1:<port>".
# Note: ``[[ -n "$PORT" ]] && break`` is a chained command that
# ``set -e`` will exit on if the test returns false. Use an
# explicit ``if`` so the loop doesn't bail on the first miss.
PORT=""
for _ in $(seq 1 20); do
    PORT="$(grep -oE 'listening on 127\.0\.0\.1:[0-9]+' "$PROXY_OUT" 2>/dev/null | head -1 | cut -d: -f2 || true)"
    if [[ -n "$PORT" ]]; then break; fi
    sleep 0.2
done
if [[ -z "$PORT" ]]; then
    echo "✗ proxy never reported a listening port" >&2
    cat "$PROXY_OUT" >&2
    exit 1
fi
echo "→ proxy listening on 127.0.0.1:$PORT"

# ── Run the same kind of download runtime.ts does ──────────────────
# When proxyEnv is set in runtime.ts the uv download falls back to
# ``curl -fsSL --retry 3 -o <tmp> <url>`` (see ``downloadUv``).
# Mirror that command exactly.
UV_URL="https://github.com/astral-sh/uv/releases/download/0.5.7/uv-aarch64-apple-darwin.tar.gz"
OUT="$(mktemp -t ember-uv-via-proxy-XXXXXX)"
trap 'cleanup; rm -f "$OUT"' EXIT

echo "→ Downloading uv release through the test proxy"
HTTPS_PROXY="http://127.0.0.1:$PORT" \
HTTP_PROXY="http://127.0.0.1:$PORT" \
    curl -fsSL --retry 3 -o "$OUT" "$UV_URL"

if [[ ! -s "$OUT" ]]; then
    echo "✗ download produced an empty file" >&2
    exit 1
fi
SIZE="$(wc -c <"$OUT" | tr -d ' ')"
echo "  download succeeded: $SIZE bytes"

# ── Assert: proxy SAW a CONNECT to github.com ──────────────────────
# Without proxy propagation the request would go direct, and the
# log would be empty.
if ! grep -q '"kind":"connect"' "$PROXY_LOG"; then
    echo "✗ proxy never logged a CONNECT — env vars did not propagate" >&2
    echo "--- proxy log ---" >&2
    cat "$PROXY_LOG" >&2
    exit 1
fi

if ! grep -q '"host":"github.com"' "$PROXY_LOG"; then
    echo "✗ proxy log did not show a CONNECT to github.com" >&2
    echo "--- proxy log ---" >&2
    cat "$PROXY_LOG" >&2
    exit 1
fi
echo "  proxy log confirms CONNECT to github.com"

echo
echo "✓ Proxy propagation verified: HTTPS_PROXY env var flows through to subprocess HTTPS traffic."
