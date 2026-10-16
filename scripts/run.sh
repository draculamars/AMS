#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT"
export ASSET_DATA_DIR="${ASSET_DATA_DIR:-$HOME/AssetVault/catalog}"
export ASSET_BLOB_DIR="${ASSET_BLOB_DIR:-$HOME/AssetVault/archive}"
exec "$APP_ROOT/.venv/bin/python" -m asset_manager serve --host "${ASSET_HOST:-127.0.0.1}" --port "${ASSET_PORT:-8765}"
