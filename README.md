# music-pipeline

Soulseek → beets → Plex automation for a self-hosted music library.

Album requests come in via a Telegram bot, get downloaded by [slskd](https://github.com/slskd/slskd), settle through a staging area, get fingerprinted and quality-checked, and finally land in the Plex library via [beets](https://beets.io). State is shared across processes through a single SQLite DB.

Runs as either:
- a **docker compose stack** (10 services, recommended for new installs) — see [DOCKER.md](DOCKER.md)
- a **host install** with systemd timers (covered below)

## Architecture

```
Telegram bot ──► slskd queue ──► <scratch>/incomplete/
                                       │
                                       ▼
                                  /complete/  ──► slskd-promote-ready (3 min)
                                       │              │
                                       │              ▼
                                       │         /ready/  ──► beets-import (15 min)
                                       │                          │
                                       │                          ▼
                                       │                     Plex library
                                       ▼
                                  /quarantine/  ◄── chromaprint failures, stuck imports,
                                                    incomplete albums past their hold window
```

Cross-cutting state (held folders, wishlist, notification queue, fill attempts) lives in `pipeline.db` (SQLite WAL) and is accessed via the `pipeline.db` module.

## Quick start: docker (recommended)

```sh
git clone https://github.com/sashavrg/music-pipeline.git
cd music-pipeline
cp .env.example .env
$EDITOR .env                       # fill in tokens, soulseek creds, paths
export MUSIC_LIBRARY_ROOT=/path/to/plex/music
docker compose up -d
docker compose logs -f telegram-bot
```

Full instructions, service table, volumes, and troubleshooting in **[DOCKER.md](DOCKER.md)**.

## Quick start: host install (Arch + systemd)

Tested on Arch + systemd. Adjust paths in `.env` for other layouts.

### 1. Prerequisites

- `python3` with `pyacoustid`, `mutagen`, `requests`, `pyyaml`, `beautifulsoup4`, `python3-discogs-client`, `pillow`, `pylast`
- `beets` (and a working beets config directory)
- `fpcalc` (chromaprint)
- `envsubst` (`gettext` package)
- A running [slskd](https://github.com/slskd/slskd) instance
- A Plex server with a music library
- A Telegram bot token (from `@BotFather`) and your numeric chat ID

### 2. Clone and configure

```bash
sudo git clone https://github.com/sashavrg/music-pipeline.git /opt/music-pipeline
cd /opt/music-pipeline
sudo cp .env.example .env
sudo $EDITOR .env   # fill in tokens, soulseek creds, paths
```

Required `.env` values:

| Variable | What it is |
|----------|------------|
| `TELEGRAM_BOT_TOKEN` | from `@BotFather` |
| `TELEGRAM_ALLOWED_CHAT_ID` | your numeric chat ID — only this ID can command the bot |
| `DISCOGS_USER_TOKEN` | from <https://www.discogs.com/settings/developers> |
| `PLEX_TOKEN` | see Plex docs on `X-Plex-Token` |
| `SOULSEEK_USERNAME`, `SOULSEEK_PASSWORD` | your Soulseek account |
| `SLSKD_WEB_USERNAME`, `SLSKD_WEB_PASSWORD` | slskd web UI creds (don't use the upstream `slskd/slskd` default) |

Optional path overrides default to the homeserver layout (`/mnt/scratch/slskd/...`, `/mnt/storage/share/media/music/music`, etc.) — see `.env.example` for the full list and tuning constants.

### 3. Install

```bash
sudo ./install.sh
```

This will:

- Validate prerequisites and required env vars
- Install `bin/*` to `/usr/local/bin/` (wrappers + bash scripts)
- Install `systemd/*` to `/etc/systemd/system/`
- Render config templates into beets / slskd / telegram-bot config files
- Write `/etc/music-pipeline.env` (sourced by every systemd service)
- Create runtime directories
- Enable + start core timers and the telegram bot

The wrappers in `/usr/local/bin/` reference the `pipeline/` package at `$MUSIC_PIPELINE_ROOT` (default `/opt/music-pipeline`). Edits to `pipeline/*.py` take effect immediately — no redeploy needed.

### 4. Verify

```bash
systemctl list-timers '*pipeline*' '*beets*' '*slskd*'
journalctl -u slskd-telegram-bot.service -f
tail -f /var/log/beets-import.log
```

Send your bot a message like `/queue Earth Wind Fire - All N All` to confirm end-to-end.

## Repo layout

```
.
├── install.sh                    # Idempotent host deploy (run as root)
├── Dockerfile                    # Container build for the docker compose stack
├── docker-compose.yml            # 10-service stack: slskd + 9 pipeline services
├── docker-entrypoint.sh          # Renders configs from env at container start
├── docker/loop.sh                # Tiny SIGTERM-aware periodic-task wrapper
├── DOCKER.md                     # docker-specific quick start + ops
├── pyproject.toml                # Used by container `pip install .`; deps + console_scripts
├── .env.example                  # Required + optional config vars
├── Makefile                      # deploy / sync / diff / check / docker-build / ...
├── config/                       # YAML templates rendered by install.sh / entrypoint
│   ├── beets.yaml.template
│   ├── slskd.yml.template
│   └── telegram-bot.env.template
├── pipeline/                     # Python package — single source of truth for logic
│   ├── __init__.py
│   ├── config.py                 # Central env-driven config (paths + tuning)
│   ├── db.py                     # Shared SQLite state (held folders, wishlist, ...)
│   ├── recover.py                # slskd search + queue + quality scoring
│   ├── promote_ready.py
│   ├── incomplete_watchdog.py
│   ├── fill_missing_tracks.py
│   ├── quarantine_requeue.py
│   ├── wishlist_check.py
│   ├── weekly_digest.py
│   ├── telegram_bot.py
│   ├── beets_quality_upgrade.py  # Quality-aware prefilter
│   ├── beets_apply_staged_deletions.py
│   └── check_chromaprint.py
├── bin/                          # Entry-point scripts → /usr/local/bin/
│   ├── slskd-promote-ready        (Python wrapper: 5-line sys.path.insert + main())
│   ├── slskd-recover, ...         (one wrapper per pipeline module)
│   ├── beets-import.sh            (bash; chromaprint pre-check → prefilter → beet import)
│   └── music-pipeline-healthcheck.sh
└── systemd/                      # *.service / *.timer units
```

## Editing the codebase

**Python logic lives in `pipeline/`.** Edits to those files take effect immediately — the wrappers in `/usr/local/bin/` import from `$MUSIC_PIPELINE_ROOT` (which is your repo checkout) on every invocation. No `make deploy` needed for pipeline/ changes.

**Entry-point wrappers (`bin/`) and systemd units (`systemd/`) DO need redeploy** if you change them. The `Makefile` keeps the repo and `/usr/local/bin/` (plus `/etc/systemd/system/`) in sync:

```bash
sudo make deploy        # copy bin/ + systemd/ to deployed locations
sudo make deploy-scripts # bin/ only (no daemon-reload)
sudo make sync           # pull deployed copies back into the repo (capture drift)
make diff                # full unified diff repo ↔ deployed
make check               # exit non-zero if anything drifts (CI-friendly)
make restart             # restart the bot
make status              # timers + bot status at a glance
make docker-build        # build the container image
make docker-config       # validate docker-compose.yml
```

Workflow: edit in the repo, run `sudo make deploy` if you touched `bin/` or `systemd/`, commit. If something got edited directly in `/usr/local/bin/`, run `sudo make sync` before your next commit so the repo stays authoritative.

## Configuration

Every path and tuning constant is read from environment variables at module import time (see `pipeline/config.py`). Defaults match the historical host install. On the host, `install.sh` writes `/etc/music-pipeline.env`, which every systemd service loads via `EnvironmentFile=-`. In docker, compose sets the env directly.

Notable env vars beyond `.env.example`:

| Var | Default | Effect |
|---|---|---|
| `LOG_TO_STDOUT` | `false` (host) / `true` (docker) | Skip writing to `/var/log/*.log`; rely on stdout for systemd journal / `docker logs` |
| `MUSIC_PIPELINE_ROOT` | `/opt/music-pipeline` | Where the wrappers look for the `pipeline/` package |
| `SCRATCH_ROOT` | `/mnt/scratch/slskd` | Parent of `complete/`, `ready/`, `incomplete/`, `quarantine/` |
| `MIN_UPLOAD_SPEED` | `2000000` (2 MB/s) | Slskd peer filter threshold |

Full list with defaults at the bottom of `.env.example`.

## Logs and state (host install)

| Path | What |
|------|------|
| `/var/log/beets-import.log` | beets import wrapper output |
| `/var/log/slskd-telegram-bot.log` | bot activity |
| `/var/log/music-pipeline-health.log` | health check results |
| `/var/lib/pipeline/pipeline.db` | shared SQLite state (WAL) |
| `/var/lib/beets-import/seen-folders.json` | loopguard state for stuck imports |

In docker, all of these live in named volumes (`logs`, `state`) and logging is stdout-only by default (`docker logs <service>`).

## Status

Personal homeserver project, public for reference. No support promised, no upstream contributions expected — but issues are welcome if you're trying to run it yourself.

## License

MIT (see `LICENSE` if added).
