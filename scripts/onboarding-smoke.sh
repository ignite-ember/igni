#!/usr/bin/env bash
# Onboarding smoke test — exercises the exact path a new user takes
# the first time they install the JetBrains plugin, VSCode extension,
# or Tauri app. Replicates what ``EmberRuntime.ensureBackendPython``
# (Kotlin) and ``runtime.ts::ensureBackendPython`` (Node) do at first
# launch.
#
# Steps:
#
#   1. Download uv for the current OS/arch from GitHub releases.
#   2. Use uv to install a pinned CPython.
#   3. Create a managed venv.
#   4. Install ignite-ember at the pinned version (or from a local
#      source tree, see ``--local``).
#   5. Prefetch the sentence-transformer embedding model.
#   6. Spawn ``python -m ember_code.backend --ws-port 0`` and confirm
#      the ready JSON line lands on stdout.
#
# If any step fails the script exits non-zero with the failing step's
# output. Successful runs print a one-line summary with timings.
#
# Usage:
#
#   scripts/onboarding-smoke.sh                  # online install, version from pyproject.toml
#   scripts/onboarding-smoke.sh --version 0.6.0  # pin a specific PyPI version
#   scripts/onboarding-smoke.sh --local          # use ``pip install -e .`` against the working tree
#                                                # (lets you smoke-test BEFORE publishing to PyPI)
#   scripts/onboarding-smoke.sh --keep           # leave the cache dir on disk for inspection

set -euo pipefail

# ── Args ─────────────────────────────────────────────────────────────
LOCAL=false
KEEP=false
VERSION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local) LOCAL=true; shift ;;
        --keep) KEEP=true; shift ;;
        --version) VERSION="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# ── Repo root + version resolution ──────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -z "$VERSION" ]]; then
    VERSION="$(grep -E '^version\s*=' "$REPO_ROOT/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
fi
echo "→ Targeting ignite-ember version: $VERSION (local source: $LOCAL)"

# ── Temp cache ──────────────────────────────────────────────────────
CACHE_DIR="$(mktemp -d -t ember-onboarding-XXXXXX)"
echo "→ Using temp cache: $CACHE_DIR"
cleanup() {
    if [[ "$KEEP" == "true" ]]; then
        echo "↳ Keeping cache at $CACHE_DIR (--keep)"
    else
        rm -rf "$CACHE_DIR"
    fi
}
trap cleanup EXIT

# ── Platform detection ──────────────────────────────────────────────
UV_VERSION="0.5.7"
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)   TRIPLE="aarch64-apple-darwin";       EXT="tar.gz" ;;
    Darwin-x86_64)  TRIPLE="x86_64-apple-darwin";        EXT="tar.gz" ;;
    Linux-x86_64)   TRIPLE="x86_64-unknown-linux-gnu";   EXT="tar.gz" ;;
    Linux-aarch64)  TRIPLE="aarch64-unknown-linux-gnu";  EXT="tar.gz" ;;
    *)
        echo "Unsupported platform: $(uname -s) $(uname -m)" >&2
        exit 1
        ;;
esac

# ── Step 1: uv ──────────────────────────────────────────────────────
STEP_START=$(date +%s)
echo "→ Step 1: downloading uv $UV_VERSION for $TRIPLE"
UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$TRIPLE.$EXT"
curl -fsSL "$UV_URL" -o "$CACHE_DIR/uv.$EXT"
tar -xzf "$CACHE_DIR/uv.$EXT" -C "$CACHE_DIR"
UV="$(find "$CACHE_DIR" -name uv -type f -perm -u+x | head -1)"
if [[ -z "$UV" ]]; then
    echo "uv binary not found in extracted archive" >&2
    exit 1
fi
STEP_TIME=$(($(date +%s) - STEP_START))
echo "  uv ready at $UV (${STEP_TIME}s)"

# ── Step 2: Python ──────────────────────────────────────────────────
STEP_START=$(date +%s)
echo "→ Step 2: installing Python 3.12 via uv"
"$UV" python install 3.12
STEP_TIME=$(($(date +%s) - STEP_START))
echo "  Python installed (${STEP_TIME}s)"

# ── Step 3: venv ────────────────────────────────────────────────────
STEP_START=$(date +%s)
echo "→ Step 3: creating venv"
VENV="$CACHE_DIR/venv"
"$UV" venv --python 3.12 "$VENV"
VENV_PYTHON="$VENV/bin/python"
STEP_TIME=$(($(date +%s) - STEP_START))
echo "  venv ready at $VENV (${STEP_TIME}s)"

# ── Step 4: ignite-ember ────────────────────────────────────────────
STEP_START=$(date +%s)
if [[ "$LOCAL" == "true" ]]; then
    echo "→ Step 4: installing ignite-ember from local source ($REPO_ROOT)"
    "$UV" pip install --python "$VENV_PYTHON" -e "$REPO_ROOT"
else
    echo "→ Step 4: installing ignite-ember==$VERSION from PyPI"
    "$UV" pip install --python "$VENV_PYTHON" "ignite-ember==$VERSION"
fi
STEP_TIME=$(($(date +%s) - STEP_START))
echo "  ignite-ember installed (${STEP_TIME}s)"

# Sanity: import works.
"$VENV_PYTHON" -c "import ember_code; print(f'  ember_code v{ember_code.__version__}')"

# ── Step 5: prefetch model ──────────────────────────────────────────
STEP_START=$(date +%s)
echo "→ Step 5: prefetching embedding model"
HF_HOME="$CACHE_DIR/hf" "$VENV_PYTHON" -m ember_code.prefetch_models
STEP_TIME=$(($(date +%s) - STEP_START))
echo "  model warmed (${STEP_TIME}s)"

# ── Step 6: launch BE, await ready ──────────────────────────────────
STEP_START=$(date +%s)
echo "→ Step 6: launching backend and waiting for ready line"
PROJECT_DIR="$(mktemp -d -t ember-be-test-XXXXXX)"
trap 'rm -rf "$PROJECT_DIR"' EXIT

# Spawn BE in background, capture stdout, wait for ready JSON
BE_STDOUT="$CACHE_DIR/be-stdout.log"
BE_STDERR="$CACHE_DIR/be-stderr.log"
HF_HOME="$CACHE_DIR/hf" "$VENV_PYTHON" -m ember_code.backend \
    --ws-port 0 \
    --project-dir "$PROJECT_DIR" \
    >"$BE_STDOUT" 2>"$BE_STDERR" &
BE_PID=$!

# Poll for the ready line for up to 60s.
for _ in $(seq 1 60); do
    if grep -q '"status":\s*"ready"' "$BE_STDOUT" 2>/dev/null; then
        STEP_TIME=$(($(date +%s) - STEP_START))
        WS_PORT=$(grep -o '"ws_port":\s*[0-9]\+' "$BE_STDOUT" | head -1 | grep -o '[0-9]\+')
        echo "  backend ready on ws://127.0.0.1:$WS_PORT (${STEP_TIME}s)"
        break
    fi
    if ! kill -0 "$BE_PID" 2>/dev/null; then
        echo "✗ backend exited before signalling ready" >&2
        echo "--- stdout ---" >&2
        cat "$BE_STDOUT" >&2
        echo "--- stderr ---" >&2
        cat "$BE_STDERR" >&2
        exit 1
    fi
    sleep 1
done

if ! kill -0 "$BE_PID" 2>/dev/null; then
    echo "✗ backend died after ready signal" >&2
    exit 1
fi

# Clean shutdown. SIGTERM first; escalate to SIGKILL if it doesn't
# exit within 5s. ``wait`` on a bg job can block indefinitely on
# some bash versions if the child re-parents, so we poll instead.
kill -TERM "$BE_PID" 2>/dev/null || true
for _ in $(seq 1 5); do
    if ! kill -0 "$BE_PID" 2>/dev/null; then break; fi
    sleep 1
done
if kill -0 "$BE_PID" 2>/dev/null; then
    kill -9 "$BE_PID" 2>/dev/null || true
fi

echo
echo "✓ Onboarding smoke passed. A fresh user reaches a running backend through this exact path."
