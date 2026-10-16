#!/usr/bin/env bash
set -euo pipefail
: "${AMS_SSH_HOST:?Set AMS_SSH_HOST to your configured SSH target}"
AMS_LOCAL_PORT="${AMS_LOCAL_PORT:-8767}"
AMS_REMOTE_PORT="${AMS_REMOTE_PORT:-8765}"
[[ "$AMS_LOCAL_PORT" =~ ^[0-9]+$ && "$AMS_REMOTE_PORT" =~ ^[0-9]+$ ]] || exit 2
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -N -f \
  -L "127.0.0.1:${AMS_LOCAL_PORT}:127.0.0.1:${AMS_REMOTE_PORT}" "$AMS_SSH_HOST"
printf 'AMS: http://127.0.0.1:%s\n' "$AMS_LOCAL_PORT"
