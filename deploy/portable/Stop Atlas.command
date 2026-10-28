#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec /bin/sh "$ROOT/start.sh" stop
