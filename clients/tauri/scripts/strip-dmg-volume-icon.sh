#!/usr/bin/env bash
# Strip the volume icon from a Tauri-built .dmg so Finder renders it
# with the generic disk-image icon instead of the bundled app icon.
#
# Tauri's bundler copies the app .icns into the DMG as
# ``.VolumeIcon.icns`` and sets the volume's ``kHasCustomIcon``
# attribute. Both are needed for Finder to show the flame on the .dmg
# file itself. Removing them (after the bundle is otherwise final)
# reverts the file icon to the system default — what users expect to
# see on a download — without touching the app icon inside.
#
# Usage: strip-dmg-volume-icon.sh <path-to.dmg>
#
# macOS only. Idempotent: a .dmg without a custom icon is left alone.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <path-to.dmg>" >&2
  exit 2
fi

DMG="$1"
if [[ ! -f "$DMG" ]]; then
  echo "error: $DMG does not exist" >&2
  exit 2
fi

WORK="$(mktemp -d -t ember-dmg)"
trap 'rm -rf "$WORK"' EXIT

RW="$WORK/rw.dmg"
hdiutil convert "$DMG" -format UDRW -o "$RW" >/dev/null

# ``hdiutil attach`` prints multiple lines; the last column on the
# last line is the mount point. Use ``-nobrowse`` so Finder doesn't
# pop the volume open during CI.
MOUNT_OUTPUT="$(hdiutil attach "$RW" -nobrowse -noverify -noautoopen)"
MOUNT_POINT="$(echo "$MOUNT_OUTPUT" | awk -F'\t' 'NF>=3 { mp=$NF } END { print mp }')"
if [[ -z "$MOUNT_POINT" || ! -d "$MOUNT_POINT" ]]; then
  echo "error: could not determine mount point from hdiutil output:" >&2
  echo "$MOUNT_OUTPUT" >&2
  exit 1
fi

cleanup_mount() {
  hdiutil detach "$MOUNT_POINT" -quiet >/dev/null 2>&1 || true
}
trap 'cleanup_mount; rm -rf "$WORK"' EXIT

rm -f "$MOUNT_POINT/.VolumeIcon.icns"
# Clear the custom-icon flag on the volume so Finder falls back to the
# default disk image icon. Lowercase ``c`` clears; uppercase sets.
SetFile -a c "$MOUNT_POINT" 2>/dev/null || true

cleanup_mount
trap 'rm -rf "$WORK"' EXIT

REPACK="$WORK/repack.dmg"
hdiutil convert "$RW" -format UDZO -imagekey zlib-level=9 -o "$REPACK" >/dev/null
mv "$REPACK" "$DMG"

echo "stripped volume icon from $(basename "$DMG")"
