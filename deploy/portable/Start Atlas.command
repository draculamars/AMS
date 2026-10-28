#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
/bin/sh "$ROOT/start.sh" start
RESULT=$?
if [ "$RESULT" != 0 ]; then printf '\nAtlas did not start. Press Enter to close. '; read -r answer; fi
exit "$RESULT"
