#!/bin/sh
# Lock down kinkyscheduler docker files so only the owner can read them.
# Run from the docker/ directory (or pass the docker dir as $1).
# Does not affect people using the web UI — only other Linux accounts on this machine.
set -e

DIR="${1:-.}"
cd "$DIR"

echo "Locking $PWD ..."

chmod 700 .

if [ -f .env ]; then
  chmod 600 .env
fi
if [ -f docker-compose.yml ]; then
  chmod 600 docker-compose.yml
fi
if [ -f .env.example ]; then
  chmod 640 .env.example 2>/dev/null || chmod 600 .env.example
fi

if [ -d data ]; then
  chmod 700 data
  chmod -R u+rwX,go-rwx data
  find data -type f \( -name 'secrets.json' -o -name 'accounts.json' \) -exec chmod 600 {} \;
  [ -f data/accounts.json ] && chmod 600 data/accounts.json
fi

echo "Done."
echo "  dirs:  700 (owner only)"
echo "  .env / compose / secrets / accounts: 600"
echo "Docker and the app can still write; browser users are unchanged."
