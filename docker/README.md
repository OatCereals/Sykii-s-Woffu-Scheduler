# Sykii's Woffu Scheduler (Docker)

Same app as the parent folder, packaged to run with Docker. Calendar, credentials, and punch state live in `./data` on the host so they survive rebuilds.

Works on a Raspberry Pi (ARM) and on a normal PC/VPS. Official `python:3.12-slim` images are multi-arch.

> **Security**: the UI has **no login**. Port 5000 is published on the LAN. Anyone on your Wi-Fi can open it. Do **not** port-forward 5000 on the router.

## Run

On the Pi (or any machine with Docker and Compose):

```bash
cd docker
# optional: cp .env.example .env  and put Woffu user/password there (or use the web UI)
docker compose up -d --build
```

From your PC, same network:

```
http://PI_IP:5000
```

The compose file uses `restart: unless-stopped`, so the container restarts after a crash. For a **power cut / reboot**, enable Docker on boot (Debian / Raspberry Pi):

```bash
sudo systemctl enable --now docker
```

Then start once with `docker compose up -d`. When the PC comes back on, Docker starts and brings `woffu-scheduler` up again. No extra systemd unit is needed.

Stop:

```bash
docker compose down
```

Update (new code, keep `data/`):

```bash
docker compose up -d --build
```

## Logs

```bash
docker compose logs -f
tail -f data/woffu.log
```

## Data

| Path | What |
|---|---|
| `data/schedule.json` | Calendar |
| `data/state.json` | What already punched |
| `data/settings.json` | Presets, jitter, rest/correction |
| `data/secrets.json` | Credentials if you saved them in the UI |
| `data/woffu.log` | Punch log |

Timezone is `Europe/Madrid` (`TZ` in compose / `.env`). Punches use the container clock.
