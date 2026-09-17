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
#   SMOKE_READY_TIMEOUT=5 scripts/...            # shorten the step-6 ready wait (default 60s)
#   SMOKE_LOG_DIR=/path scripts/...              # where backend logs go (default: <repo>/.smoke-logs).
#                                                # Kept on failure, swept on success.

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
            sed -n '2,32p' "$0"
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

# ── Log dir ─────────────────────────────────────────────────────────
# The backend's stdout/stderr go HERE, deliberately NOT under the temp
# cache below, and this directory survives a failing run. Two separate
# things used to make the CI "Upload smoke logs on failure" step upload
# nothing at all:
#
#   1. cleanup() runs on EVERY exit, failures included. Logs written
#      into $CACHE_DIR were therefore deleted a moment before the
#      workflow's failure step went looking for them.
#   2. ``mktemp -d -t`` honours $TMPDIR. On macOS that is
#      /var/folders/<...>/T, not /tmp — so a workflow globbing
#      /tmp/ember-onboarding-*/ was wrong on the macOS leg even when a
#      directory did survive.
#
# A fixed, workspace-relative default kills both: the path is identical
# on every platform, and it is reported (below) instead of guessed.
# ``*.log`` is already gitignored, so this leaves no untracked noise.
LOG_DIR="${SMOKE_LOG_DIR:-$REPO_ROOT/.smoke-logs}"
mkdir -p "$LOG_DIR"
# Drop logs from an earlier run. Without this, a stale be-stdout.log
# still containing a ready line would satisfy step 6's poll instantly
# and the run would "pass" without ever reading this run's backend.
rm -f "$LOG_DIR"/be-*.log
# Tell the CI job where the logs actually are rather than making the
# workflow reconstruct the path. Written before any step that can fail,
# so the failure-only upload step always has a value to read.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "log-dir=$LOG_DIR" >> "$GITHUB_OUTPUT"
fi
echo "→ Backend logs: $LOG_DIR"

# ── Temp cache ──────────────────────────────────────────────────────
CACHE_DIR="$(mktemp -d -t ember-onboarding-XXXXXX)"
echo "→ Using temp cache: $CACHE_DIR"
# Step 6 makes a second temp dir. It is declared here, empty, so the one
# EXIT handler below owns every path this script creates: bash keeps a
# single handler per signal, so a second ``trap … EXIT`` further down
# silently unregisters this one. That is what happened — ``--keep`` went
# inert and the cache dir (uv tarball + binary, the venv, the HF model
# under $CACHE_DIR/hf — hundreds of MB) was never removed on any run
# that reached step 6. Anything needing cleanup goes in this function,
# never in a second trap.
PROJECT_DIR=""
cleanup() {
    # Must be the first statement: $? here is the script's exit status,
    # and the branches below need to know whether the run failed.
    local status=$?

    if [[ "$KEEP" == "true" ]]; then
        echo "↳ Keeping cache at $CACHE_DIR (--keep)"
        if [[ -n "$PROJECT_DIR" ]]; then
            echo "↳ Keeping project dir at $PROJECT_DIR (--keep)"
        fi
    else
        rm -rf "$CACHE_DIR"
        if [[ -n "$PROJECT_DIR" ]]; then
            rm -rf "$PROJECT_DIR"
        fi
    fi

    # Logs outlive a failure — that is the only run where anyone wants
    # them, and it is the run where this handler fires. A clean pass
    # sweeps them up unless --keep asked otherwise.
    if [[ "$status" -ne 0 ]]; then
        echo "↳ Backend logs kept for diagnosis: $LOG_DIR" >&2
    elif [[ "$KEEP" == "true" ]]; then
        echo "↳ Keeping backend logs at $LOG_DIR (--keep)"
    else
        rm -rf "$LOG_DIR"
    fi

    return "$status"
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
# No ``trap`` here — cleanup() above already removes this dir. Adding a
# second EXIT trap would replace the first one (see the note there).
PROJECT_DIR="$(mktemp -d -t ember-be-test-XXXXXX)"

# Spawn BE in background, capture stdout, wait for ready JSON.
# These live in $LOG_DIR, not $CACHE_DIR, so cleanup() can drop the
# cache on a failing run while still handing the logs to CI.
BE_STDOUT="$LOG_DIR/be-stdout.log"
BE_STDERR="$LOG_DIR/be-stderr.log"
HF_HOME="$CACHE_DIR/hf" "$VENV_PYTHON" -m ember_code.backend \
    --ws-port 0 \
    --project-dir "$PROJECT_DIR" \
    >"$BE_STDOUT" 2>"$BE_STDERR" &
BE_PID=$!

# Poll for the ready line. 60s by default; SMOKE_READY_TIMEOUT exists so
# the test harness can run this block without a minute of dead wall time.
READY_TIMEOUT="${SMOKE_READY_TIMEOUT:-60}"
READY=false
for _ in $(seq 1 "$READY_TIMEOUT"); do
    if grep -q '"status":\s*"ready"' "$BE_STDOUT" 2>/dev/null; then
        STEP_TIME=$(($(date +%s) - STEP_START))
        WS_PORT=$(grep -o '"ws_port":\s*[0-9]\+' "$BE_STDOUT" | head -1 | grep -o '[0-9]\+')
        echo "  backend ready on ws://127.0.0.1:$WS_PORT (${STEP_TIME}s)"
        READY=true
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

# A backend that boots and then hangs never trips the died-before-ready
# branch above, so without this the loop simply fell through to the
# "✓ Onboarding smoke passed" line and exit 0 — step 6, the only
# assertion in the script that a fresh user reaches a *running*
# backend, could not fail. release-readiness.yml ran this on
# ubuntu-latest and macos-latest and reported green.
# onboarding-smoke.ps1 has had this branch all along.
if [[ "$READY" != true ]]; then
    echo "✗ backend never signalled ready within ${READY_TIMEOUT}s" >&2
    echo "--- stdout ---" >&2
    cat "$BE_STDOUT" >&2
    echo "--- stderr ---" >&2
    cat "$BE_STDERR" >&2
    kill -9 "$BE_PID" 2>/dev/null || true
    exit 1
fi

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
