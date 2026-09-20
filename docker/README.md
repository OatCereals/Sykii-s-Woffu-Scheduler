# Sykii's Woffu Scheduler (Docker)

Same app as the parent folder, packaged to run with Docker. Calendar, credentials, and punch state live in `./data` on the host so they survive rebuilds.

Works on a Raspberry Pi (ARM) and on a normal PC/VPS. Official `python:3.12-slim` images are multi-arch.

> **Security**: decoy at `/`, login at `/acceso`. Port **40** is published on the LAN. Do **not** port-forward on the router unless you intend to.

## Run

On the Pi (or any machine with Docker and Compose):

```bash
cd docker
# optional: cp .env.example .env  and put Woffu user/password there (or use the web UI)
docker compose up -d --build
```

From your PC, same network:

```
http://PI_IP:40
```

The compose file uses `restart: unless-stopped`, so the container restarts after a crash. For a **power cut / reboot**, enable Docker on boot (Debian / Raspberry Pi):

```bash
sudo systemctl enable --now docker
```

Then start once with `docker compose up -d`. When the PC comes back on, Docker starts and brings `kinkyscheduler` up again. No extra systemd unit is needed.

## Lock down files on the Pi

Only you (the Linux owner) can read project/data files. Web users are unchanged; Docker can still write.

```bash
cd docker
chmod +x lock-perms.sh
./lock-perms.sh
```

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
