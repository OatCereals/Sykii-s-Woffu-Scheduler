#!/bin/sh
# Generate a self-signed cert for LAN HTTPS (run once on the Pi).
set -e
cd "$(dirname "$0")"
mkdir -p certs
chmod 700 certs

if [ -f certs/cert.pem ] && [ -f certs/key.pem ]; then
  echo "certs already exist — delete certs/ to regenerate"
  exit 0
fi

# Extra names: edit if your hostname differs
NAME="${1:-iphonedefido}"

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
  -keyout certs/key.pem \
  -out certs/cert.pem \
  -subj "/CN=${NAME}" \
  -addext "subjectAltName=DNS:${NAME},DNS:${NAME}.local,DNS:localhost,IP:127.0.0.1"

chmod 600 certs/key.pem certs/cert.pem
echo "OK — wrote certs/cert.pem and certs/key.pem"
echo "Then: sudo docker compose up -d"
