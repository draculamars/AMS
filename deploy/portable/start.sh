#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SYSTEM=$(uname -s)
ARCH=$(uname -m)
TARGET="$SYSTEM-$ARCH"
case "$TARGET" in Darwin-arm64|Darwin-x86_64|Linux-x86_64|Linux-aarch64) ;; *) echo "Unsupported platform: $TARGET"; exit 1 ;; esac
if [ ! -f "$ROOT/portable.json" ]; then
  echo "Atlas export is incomplete. See reports and export-state.json."
  exit 1
fi
ARCHIVE="$ROOT/runtimes/$TARGET.tar.gz"
EXPECTED=$(cat "$ROOT/runtimes/$TARGET.sha256")
case "$EXPECTED" in *[!0-9a-f]*|'') echo "Invalid runtime checksum"; exit 1 ;; esac
if [ "$SYSTEM" = Darwin ]; then
  ACTUAL=$(shasum -a 256 "$ARCHIVE" | cut -d ' ' -f 1)
  # ExFAT archives contain regular files only; no host disk space is needed.
  CACHE=${ATLAS_RUNTIME_CACHE:-"$ROOT/runtimes/expanded"}
else
  ACTUAL=$(sha256sum "$ARCHIVE" | cut -d ' ' -f 1)
  # Ubuntu removable volumes may be mounted noexec. Execute on a native cache.
  CACHE=${ATLAS_RUNTIME_CACHE:-"${XDG_CACHE_HOME:-$HOME/.cache}/atlas-portable/runtimes"}
fi
[ "$ACTUAL" = "$EXPECTED" ] || { echo "Runtime checksum mismatch. Refusing to start."; exit 1; }
DEST="$CACHE/$TARGET-$EXPECTED"
if [ ! -f "$DEST/.complete" ]; then
  mkdir -p "$CACHE"
  if ! mkdir "$DEST.extracting" 2>/dev/null; then
    echo "Runtime extraction is already running or interrupted: $DEST.extracting"
    echo "Wait for the first launcher; remove this empty lock directory only after confirming it stopped."
    exit 1
  fi
  TEMP="$DEST.staging.$$"
  trap 'rm -rf "$TEMP"; rmdir "$DEST.extracting" 2>/dev/null || true' EXIT HUP INT TERM
  mkdir "$TEMP"
  tar -xzf "$ARCHIVE" -C "$TEMP"
  "$TEMP/python/bin/python3.12" -I -c 'import sqlite3, ssl, fastapi, uvicorn, pydantic_core'
  touch "$TEMP/.complete"
  mv "$TEMP" "$DEST"
  rmdir "$DEST.extracting"
  trap - EXIT HUP INT TERM
fi
export ATLAS_RUNTIME_DIR="$DEST"
export PATH="$DEST/git/bin:$PATH"
if [ "$SYSTEM" = Linux ]; then
  export LD_LIBRARY_PATH="$DEST/git/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec "$DEST/python/bin/python3.12" -I "$ROOT/launcher.py" "$ROOT" "${1:-start}" "${2:-}"
