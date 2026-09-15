#!/usr/bin/env bash
#
# The desktop app's visual baselines, in the environment that owns them.
#
#   ./scripts/baselines-linux.sh            # compare (what CI does)
#   ./scripts/baselines-linux.sh --update   # regenerate and write them
#
# E5. Playwright names baselines per-platform. The portal learned this
# the expensive way (E4): darwin-only baselines meant the one platform
# CI runs had nothing to compare against, and the tests were excluded
# with a filter that produced no skipped test.
#
# **Only the Linux baselines are committed.** Two sets means every
# intentional design change has to regenerate both, and whoever
# regenerates one leaves CI red for reasons unrelated to their change.
# One set, owned by the environment that gates. On a Mac these tests
# declare themselves skipped (`@needs-baseline`) and the run says so
# every time.
#
# The container is the same image CI uses, pinned to the Playwright
# version in package.json, so the renderer and the fonts match. A local
# Linux VM or a different image tag produces baselines that disagree
# with CI's rendering and then fail forever on antialiasing.
#
# The repository is mounted read-only and copied inside, because
# `npm ci` in the container would otherwise replace the host's
# darwin-built node_modules. Only the PNGs come back out.
#
set -euo pipefail

MODE="compare"
if [[ "${1:-}" == "--update" ]]; then
  MODE="update"
elif [[ -n "${1:-}" ]]; then
  echo "usage: $(basename "$0") [--update]" >&2
  exit 2
fi

CLIENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOTS="$CLIENT_DIR/e2e/app-visual.spec.ts-snapshots"

# Pinned to the version in the *lockfile*, which is what `npm ci`
# installs inside the container — not the range in package.json. Those
# differ: this client's package.json says ^1.61.0 while the lockfile
# resolves 1.61.1, and Playwright refuses to run a browser build that
# does not match its library ("Please update docker image as well").
# The portal's two happened to be equal, which is how a wrong rule
# looks right until it does not.
PLAYWRIGHT_VERSION="$(node -p "require('$CLIENT_DIR/package-lock.json').packages['node_modules/@playwright/test'].version")"
IMAGE="mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble"

mkdir -p "$SNAPSHOTS"
echo "$MODE, in $IMAGE"

if [[ "$MODE" == "update" ]]; then
  INNER='npx playwright test e2e/app-visual.spec.ts --update-snapshots --reporter=line
    cp e2e/app-visual.spec.ts-snapshots/*-linux.png /out/'
  MOUNT_OUT=(-v "$SNAPSHOTS":/out)
else
  # Read-only: a comparison run must not be able to bless itself.
  INNER='npx playwright test e2e/app-visual.spec.ts --reporter=line'
  # Left unset rather than empty: macOS ships bash 3.2, where
  # expanding an empty array under `set -u` is an error.
  MOUNT_OUT=()
fi

# --platform: CI runners are x86_64, and the rendering is not
# architecture-independent. The desktop app's 34 baselines,
# generated on an arm64 Mac, failed all 34 when compared under
# x86_64 emulation — while the portal's 32 passed, which is luck
# rather than portability. Pinned so the whole environment is
# stated: image, version, architecture.
docker run --rm --platform linux/amd64 \
  -v "$CLIENT_DIR":/src:ro \
  ${MOUNT_OUT[@]+"${MOUNT_OUT[@]}"} \
  -e CI= \
  "$IMAGE" \
  bash -euc "
    cp -r /src /work
    cd /work
    npm ci --no-audit --no-fund > /dev/null
    # No backend needed: every screen is a ?demo= route driven from
    # canned events in src/dev/.
    $INNER
  "

if [[ "$MODE" == "update" ]]; then
  echo
  echo "$(ls -1 "$SNAPSHOTS" | grep -c -- '-linux.png') Linux baseline(s) in $SNAPSHOTS"
  echo "Review the diff before committing: these are what CI compares against."
fi
