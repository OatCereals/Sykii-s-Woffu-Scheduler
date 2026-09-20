#!/usr/bin/env bash
# ============================================================
#  Sykii's Woffu Scheduler - installer for a Debian 12 VPS
#  Usage (as root):   bash install.sh
#  Re-run = update the code (does not touch data/).
# ============================================================
set -euo pipefail

APP_USER="woffu"
APP_DIR="/home/$APP_USER/woffu-scheduler"
SERVICE="woffu-scheduler"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "== Sykii's Woffu Scheduler :: install =="

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root  ->  sudo bash install.sh"; exit 1
fi

# check that the script has the project files next to it
for f in app.py requirements.txt templates/index.html templates/decoy.html templates/acceso.html; do
  if [ ! -f "$SRC/$f" ]; then
    echo "ERROR: cannot find $f next to the script. Run install.sh from inside the project folder."; exit 1
  fi
done

echo "-> System packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null

echo "-> Timezone Europe/Madrid..."
timedatectl set-timezone Europe/Madrid || true

if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "-> Creating user $APP_USER..."
  useradd -m -s /bin/bash "$APP_USER"
fi

echo "-> Copying app to $APP_DIR (leaving data/ untouched)..."
mkdir -p "$APP_DIR/templates"
cp "$SRC/app.py"              "$APP_DIR/app.py"
cp "$SRC/requirements.txt"   "$APP_DIR/requirements.txt"
cp "$SRC/templates/index.html" "$APP_DIR/templates/index.html"
cp "$SRC/templates/decoy.html" "$APP_DIR/templates/decoy.html"
cp "$SRC/templates/acceso.html" "$APP_DIR/templates/acceso.html"
if [ -f "$SRC/.env.example" ] && [ ! -f "$APP_DIR/.env" ]; then
  cp "$SRC/.env.example" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "-> Virtualenv + dependencies..."
sudo -u "$APP_USER" bash -c "
  set -e
  cd '$APP_DIR'
  [ -d venv ] || python3 -m venv venv
  venv/bin/pip install --quiet --upgrade pip
  venv/bin/pip install --quiet -r requirements.txt
"

echo "-> systemd service..."
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=kinkyscheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
# Allow binding to port 40 without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ExecStart=$APP_DIR/venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --quiet $SERVICE
systemctl restart $SERVICE
sleep 2

if systemctl is-active --quiet $SERVICE; then
  echo "   service active OK"
else
  echo "   WARNING: the service did not start. Check:  journalctl -u $SERVICE -n 30 --no-pager"
fi

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PUB_IP="$(curl -s --max-time 5 ifconfig.me 2>/dev/null || true)"
cat <<EOF

============================================================
 DONE. kinkyscheduler is running (listens on port 40).

 From YOUR PC, on the same network, open:

     http://${LAN_IP:-PI_IP}:40

 Decoy page at / ; real login at /acceso (invite code in .env).

 Optional SSH tunnel instead:

     ssh -L 40:localhost:40 root@${PUB_IP:-${LAN_IP}}

 Register/login, then each user saves their Woffu credentials
 and calendar in /app.

 Logs:  journalctl -u $SERVICE -f
        $APP_DIR/data/users/<user>/woffu.log
============================================================
EOF
