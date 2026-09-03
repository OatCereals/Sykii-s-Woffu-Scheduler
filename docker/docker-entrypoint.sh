#!/bin/sh
set -e
mkdir -p /app/data
if [ "$(id -u)" = "0" ]; then
  chown -R woffu:woffu /app/data
  exec gosu woffu "$@"
fi
exec "$@"
