#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
sh "$ROOT/start.sh" backups
printf '恢复点编号：'
IFS= read -r ATLAS_SNAPSHOT
sh "$ROOT/start.sh" restore "$ATLAS_SNAPSHOT"
exec sh "$ROOT/start.sh" start
