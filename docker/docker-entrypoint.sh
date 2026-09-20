#!/bin/sh
set -e
mkdir -p /app/data
PORT="${WOFFU_PORT:-40}"
if [ "$(id -u)" = "0" ]; then
  chown -R woffu:woffu /app/data 2>/dev/null || true
  # Privileged ports (<1024) need root; otherwise drop to woffu.
  case "$PORT" in
    ''|*[!0-9]*) PORT=40 ;;
  esac
  if [ "$PORT" -ge 1024 ]; then
    exec gosu woffu "$@"
  fi
fi
exec "$@"
