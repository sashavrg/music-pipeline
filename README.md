# music-pipeline

Soulseek → beets → Plex automation for a self-hosted music library.

Album requests come in via a Telegram bot, get downloaded by [slskd](https://github.com/slskd/slskd), settle through a staging area, get fingerprinted/quality-checked, and finally land in the Plex library via [beets](https://beets.io). State is shared across scripts through a single SQLite DB; everything runs as systemd timers.

## Architecture

```
Telegram bot ──► slskd queue ──► /mnt/scratch/slskd/incomplete/
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

Cross-cutting state (held folders, wishlist, notification queue, fill attempts) lives in `/var/lib/pipeline/pipeline.db` (SQLite WAL) and is accessed via `pipeline_db.py`.

## Quick start

Tested on Arch + systemd. Adjust paths in `.env` for other layouts.

### 1. Prerequisites

- `python3` with `pyacoustid`, `mutagen`, `requests`, `pyyaml`
- `beets` (and a working beets config directory)
- `fpcalc` (chromaprint)
- `envsubst` (`gettext` package)
- A running [slskd](https://github.com/slskd/slskd) instance (Docker recommended)
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

Optional path overrides default to the homeserver layout (`/mnt/scratch/slskd/...`, `/mnt/storage/share/media/music/music`, etc.) — see `.env.example` for the full list.

### 3. Install

```bash
sudo ./install.sh
```

This will:

- Validate prerequisites and required env vars
- Copy `scripts/*` to `/usr/local/bin/`
- Install `systemd/*` units to `/etc/systemd/system/`
- Render config templates into beets / slskd / telegram-bot config files
- Create runtime directories under `/mnt/scratch/slskd/`, `/var/lib/beets-import/`, etc.
- Enable and start the core timers and the telegram bot service

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
├── install.sh                     # Idempotent deploy script (run as root)
├── .env.example                   # Required + optional config vars
├── config/                        # Templates rendered by install.sh
│   ├── beets.yaml.template
│   ├── slskd.yml.template
│   └── telegram-bot.env.template
├── scripts/                       # Source-of-truth Python/bash scripts
│   ├── pipeline_db.py             # Shared SQLite state module
│   ├── slskd-telegram-bot.py
│   ├── slskd-promote-ready.py
│   ├── slskd-incomplete-watchdog.py
│   ├── slskd-fill-missing-tracks.py
│   ├── slskd-quarantine-requeue.py
│   ├── slskd-recover.py           # Search/recover helpers (imported by bot)
│   ├── beets-import.sh            # Main import wrapper
│   ├── beets-quality-upgrade.py   # Quality-aware prefilter
│   ├── beets-apply-staged-deletions.py
│   ├── check_chromaprint.py       # Chromaprint pre-check
│   └── music-pipeline-healthcheck.sh
└── systemd/                       # *.service / *.timer units
```

## Editing scripts after install

Source-of-truth lives in `scripts/` and `systemd/`. The `Makefile` keeps the repo and `/usr/local/bin/` (plus `/etc/systemd/system/`) in sync:

```bash
sudo make deploy        # copy scripts/ + systemd/ to their deployed locations
sudo make deploy-scripts # scripts only (no daemon-reload)
sudo make sync           # pull deployed copies back into the repo (capture drift)
make diff                # full unified diff repo ↔ deployed
make check               # exit non-zero if anything drifts (CI-friendly)
make restart             # restart the bot
make status              # timers + bot status at a glance
```

Workflow: edit in `scripts/`, run `sudo make deploy`, commit. If something gets edited directly in `/usr/local/bin/`, run `sudo make sync` before your next commit so the repo stays authoritative.

## Logs and state

| Path | What |
|------|------|
| `/var/log/beets-import.log` | beets import wrapper output |
| `/var/log/slskd-telegram-bot.log` | bot activity |
| `/var/log/music-pipeline-health.log` | health check results |
| `/var/lib/pipeline/pipeline.db` | shared SQLite state (WAL) |
| `/var/lib/beets-import/seen-folders.json` | loopguard state for stuck imports |

## Status

Personal homeserver project, public for reference. No support promised, no upstream contributions expected — but issues are welcome if you're trying to run it yourself.

## License

MIT (see `LICENSE` if added).
