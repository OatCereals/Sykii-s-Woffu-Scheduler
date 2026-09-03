# Sykii's Woffu Scheduler (Docker)

Same app as the parent folder, packaged to run with Docker. Calendar, credentials, and punch state live in `./data` on the host so they survive rebuilds.

Works on a Raspberry Pi (ARM) and on a normal PC/VPS. Official `python:3.12-slim` images are multi-arch.

> **Security**: the UI has **no login**. Port 5000 is published on the LAN. Anyone on your Wi-Fi can open it. Do **not** port-forward 5000 on the router.

## Run

On the Pi (or any machine with Docker and Compose):

```bash
cd docker
cp .env.example .env          # optional: put Woffu user/password here
docker compose up -d --build
```

From your PC, same network:

```
http://PI_IP:5000
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
