# Running music-pipeline in Docker

Brings up slskd + the full pipeline (10 services) via `docker compose`. The pipeline writes imported albums to a host directory that Plex watches.

## Prerequisites

- Docker 20.10+ with the Compose plugin (`docker compose ...`, not `docker-compose`)
- A Soulseek account (https://www.slsknet.org/news/) — slskd connects with these creds
- A Plex server with a Music library, plus its [X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- A Telegram bot from [@BotFather](https://t.me/botfather) and your numeric chat ID
- A [Discogs API token](https://www.discogs.com/settings/developers) (used by the beets `discogs` plugin)

## Setup

```sh
git clone https://github.com/sashavrg/music-pipeline.git
cd music-pipeline
cp .env.example .env
$EDITOR .env   # fill in every blank — install.sh would refuse to start without them, compose just errors louder
```

Set `MUSIC_LIBRARY_ROOT` in the host environment (or in `.env`) to the host path where Plex reads the music library — that's the directory the pipeline will write imported albums into:

```sh
export MUSIC_LIBRARY_ROOT=/srv/media/music
```

Bring it up:

```sh
docker compose up -d
docker compose logs -f telegram-bot      # confirm the bot is polling
```

Send `/status` to your bot in Telegram. You should get a reply.

## What each container does

| Service | Image | Cadence | Purpose |
|---|---|---|---|
| `slskd` | `slskd/slskd:latest` | always | Soulseek client + HTTP API |
| `telegram-bot` | `music-pipeline` | always | Reads `/queue`, `/wish`, `/status`, `/scan` commands; pushes notifications |
| `promote-ready` | `music-pipeline` | 3 min | Moves settled `complete/` folders into `ready/`, holds incomplete ones |
| `beets-import` | `music-pipeline` | 15 min | chromaprint pre-check → quality prefilter → `beet import` |
| `incomplete-watchdog` | `music-pipeline` | 30 min | Detects stalled `incomplete/` downloads, requeues or quarantines |
| `fill-missing-tracks` | `music-pipeline` | 6 h | Targeted re-download of missing tracks for held folders |
| `quarantine-requeue` | `music-pipeline` | weekly | Retries quarantined albums |
| `wishlist-check` | `music-pipeline` | daily | Searches slskd for pending wishlist items |
| `weekly-digest` | `music-pipeline` | weekly | Pushes Sunday digest to Telegram |
| `healthcheck` | `music-pipeline` | 30 min | Surfaces backlogs, stuck downloads, error trends |

Calendar-aligned timers on the host install (wishlist-check @ 14:00, weekly-digest @ Sun 20:00) become interval-based here — first fire ~shortly after container start, then every INTERVAL.

## Volumes

| Volume | Mountpoint | Shared with |
|---|---|---|
| `scratch` | `/data/scratch` | slskd container (so completed downloads reach the pipeline) |
| `state` | `/data/state` | pipeline_db SQLite (held folders, fill attempts, notify queue, wishlist) |
| `beets` | `/data/beets` | beets library DB + config |
| `logs` | `/data/logs` | only used if `LOG_TO_STDOUT=false` (default: stdout / `docker logs`) |
| bind: `$MUSIC_LIBRARY_ROOT` | `/data/library` | Plex (on the host) reads here |

## Common operations

**Request an album:**
Send `/queue Artist - Album` to the bot in Telegram.

**Add to wishlist (pipeline will keep trying):**
`/wish Artist - Album`

**Force a beets-import cycle now:**
`docker compose exec beets-import beets-import.sh`

**Tail one service:**
`docker compose logs -f beets-import`

**Update to latest:**
```sh
git pull
docker compose pull         # upstream slskd
docker compose build        # rebuild the pipeline image
docker compose up -d
```

## Differences from the host install

- **Logs:** all services log to stdout (`docker logs <service>`), not `/var/log/*.log`. Set `LOG_TO_STDOUT=false` in `.env` to restore file logging into the `logs` volume.
- **Calendar timers** are now interval-based (see above).
- **`Conflicts=` between promote-ready and beets-import** is gone — both services serialize via the `flock` on `/data/state/ready-dir.lock` (same as the host install post-2026-05-10 fix).
- **Plex integration:** the bind-mounted `${MUSIC_LIBRARY_ROOT}` is where the pipeline writes imports. Make sure your Plex Music library scans that same host path.

## Troubleshooting

| Symptom | Check |
|---|---|
| Bot doesn't reply | `docker compose logs telegram-bot` — common cause is bad `TELEGRAM_BOT_TOKEN` or `TELEGRAM_ALLOWED_CHAT_ID` |
| Downloads sit in `complete/` forever | `docker compose logs promote-ready` — likely a missing tag count |
| Nothing imports | `docker compose logs beets-import` — usually a chromaprint crash on one file, see `quarantine/` |
| `/status` shows held folders >24h | `docker compose logs fill-missing-tracks` — the targeted-fill flow should clear them |
| Plex doesn't see imported albums | beets `plexupdate` plugin needs `PLEX_HOST` reachable from the container — for Plex-on-host use `host.docker.internal` or the host's LAN IP |

## Tearing down

```sh
docker compose down            # stop containers, keep volumes
docker compose down -v         # also remove the named volumes (DESTRUCTIVE — wipes beets DB + state + scratch)
```

The `MUSIC_LIBRARY_ROOT` bind mount is never touched by `down -v`.
