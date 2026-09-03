# Sykii's Woffu Scheduler (web, for a Debian 12 VPS or Raspberry Pi)

Headless version: you manage it from the browser and the scheduler runs in a background thread. Built for a machine that stays on (VPS or Raspberry Pi). Includes jitter, shifts that cross midnight, night-shift corrections, rest-day absence requests, and 6 shift presets.

> **Security**: the UI has **no login**. By default it listens on all interfaces (`0.0.0.0:5000`) so you can open `http://PI_IP:5000` from your PC on the same network. Anyone on that LAN/Wi-Fi can punch and see credentials. Do **not** port-forward 5000 on your router. For localhost-only, set `WOFFU_HOST=127.0.0.1` in `.env`.

## Quick install (automatic script)

On a freshly created Debian 12 VPS, as **root**:

```bash
apt update && apt install -y unzip
unzip woffu-scheduler-vps.zip
cd woffu-scheduler
bash install.sh
```

The script does everything: packages, timezone (Europe/Madrid), `woffu` user, virtualenv, dependencies, systemd service, and start. When it finishes it prints the LAN URL (and an SSH tunnel command if you prefer that).

**It also updates**: if it is already installed, upload the new zip and re-run `bash install.sh` — it copies the new code without touching your `data/` folder (calendar, credentials, state). That is the easy way to update.

If you prefer to do it by hand, follow the sections below.

## 1. Prepare the VPS (Debian 12)

Connect over SSH and:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

# IMPORTANT: set Spain's timezone, or punches will go out at the wrong hour
sudo timedatectl set-timezone Europe/Madrid

# dedicated user (recommended)
sudo useradd -m -s /bin/bash woffu
```

## 2. Upload the project

Copy this folder to `/home/woffu/woffu-scheduler` (scp, git, whatever you prefer). Then:

```bash
sudo -u woffu -i
cd ~/woffu-scheduler
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 3. Credentials

Two options (pick one):

- **From a file** (recommended): create `/home/woffu/woffu-scheduler/.env` with:
  ```
  WOFFU_USERNAME=you@company.com
  WOFFU_PASSWORD=your_password
  ```
  and protect it: `chmod 600 .env`
- **From the web UI**: leave the file empty and enter username/password in the interface (they are stored in `data/secrets.json`).

## 4. Run as a service (systemd)

```bash
exit   # back to your sudo user
sudo cp /home/woffu/woffu-scheduler/woffu-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now woffu-scheduler
sudo systemctl status woffu-scheduler      # should show "active (running)"
```

It starts on VPS reboot and restarts if it crashes.

## 5. Open the UI

From your PC, on the same network as the Pi/VPS:

```
http://PI_OR_VPS_LAN_IP:5000
```

Example: `http://192.168.1.50:5000`. The scheduler keeps punching even if you close the browser; you only need the web UI to edit shifts.

If you prefer not to expose port 5000 on the LAN, set `WOFFU_HOST=127.0.0.1` in `.env` and use an SSH tunnel instead:

```bash
ssh -L 5000:localhost:5000 woffu@PI_OR_VPS_IP
```

Then open **http://localhost:5000**.

## 6. Logs

```bash
journalctl -u woffu-scheduler -f         # live service log
tail -f /home/woffu/woffu-scheduler/data/woffu.log   # punch events only
```

## How it works

- **Jitter**: each punch goes out 0:00 to N:00 after the shift time (default 4 → up to 4 minutes, e.g. 07:52:13, never 07:54:13). Deterministic per day, unique per install.
- **Midnight shifts** (22:50→06:50): clocks in that night and clocks out at 06:50 the next morning. Because the VPS stays on, this does not fail.
- **Days**: only days you mark on the calendar are punched. Holidays included if you mark them.
- **Night-shift correction**: Woffu can auto-close a night shift at a 7h15 cap even if you clocked out later. After clock-out, the app waits 10–20 minutes (configurable) and PUTs the real end time.

## Rest days (automatic absence request)

Click a day → **"Mark as rest"**. That day, instead of punching, the app sends Woffu an absence request with reason "Descanso" (`POST /api/requests`), once.

- It is sent at a random time inside `descanso.window_start`–`descanso.window_end` (default **10:00–18:00**), using the same deterministic-hash style as jitter.
- On Saturday/Sunday/holidays it does **not** send a request if `skip_on_nonworking` is on (the company already marks those as rest).
- On the calendar they show a gold border/label and a "✓ sent" once requested.
- The reason is pinned to your company's "Descanso" event (`agreementEventId 2886718`). To change it, edit `data/settings.json` → `descanso.agreementEvent`.
- **When testing**: there is no dry-run; sending a rest day creates a **real request** in Woffu. For a test, mark a day, lower `window_end` so it fires soon, watch `woffu.log`, and **cancel that request in Woffu** if it was a test.

## Limitations

- It does not read your real status from Woffu (no reliable public endpoint); the "once per action" control is local. Do not punch by hand and via the app on the same day.
- It uses Woffu's password grant (the one the frontend uses): it works today, it could change.
- **Watch the IP**: punches come from this machine's public IP. On a home Raspberry Pi that is usually your home IP. On a VPS it is the datacenter IP; if Woffu or your company looks at IPs, a VPS shows.

## Notice

Time records are a legal document. What is punched should match the hours you actually worked.

## Run with Docker

Needs Docker and Compose. Calendar, credentials, and punch state live in `docker/data` on the host so they survive rebuilds.

```bash
cd docker
# optional: cp .env.example .env  and put Woffu user/password there (or use the web UI)
docker compose up -d --build
```

Open `http://PI_OR_VPS_LAN_IP:5000` from your PC on the same network.

The compose file already uses `restart: unless-stopped`, so the container comes back after a crash. For that to survive a **power cut / reboot**, Docker itself must start on boot (Debian / Raspberry Pi):

```bash
sudo systemctl enable --now docker
```

After that, `docker compose up -d` once is enough: when the machine powers on again, Docker starts and brings `woffu-scheduler` back up. No extra systemd unit is needed.

```bash
docker compose logs -f        # live container log
docker compose down           # stop (will stay down until you up again)
docker compose up -d --build  # update (keeps data/)
```

Timezone is `Europe/Madrid` (`TZ` in compose / `.env`). More detail: `docker/README.md`.
