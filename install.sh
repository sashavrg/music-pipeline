#!/usr/bin/env bash
# install.sh — deploy music-pipeline to this machine
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
die()  { echo -e "  ${RED}✗${NC} $*"; exit 1; }
hdr()  { echo -e "\n${BOLD}$*${NC}"; }

# ─── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Run as root (sudo or su)"

# ─── .env ──────────────────────────────────────────────────────────────────────
hdr "Loading config"
if [[ ! -f "$REPO/.env" ]]; then
    die "No .env file found. Copy .env.example to .env and fill in all values."
fi
set -a
# shellcheck source=/dev/null
source "$REPO/.env"
set +a
ok ".env loaded"

# ─── Validate required secrets ─────────────────────────────────────────────────
required_vars=(
    TELEGRAM_BOT_TOKEN
    TELEGRAM_ALLOWED_CHAT_ID
    DISCOGS_USER_TOKEN
    PLEX_TOKEN
    SOULSEEK_USERNAME
    SOULSEEK_PASSWORD
)
for var in "${required_vars[@]}"; do
    [[ -z "${!var:-}" ]] && die "Required variable not set in .env: $var"
done
ok "All required secrets present"

# ─── Defaults for optional path vars ───────────────────────────────────────────
BEETS_CONFIG_DIR="${BEETS_CONFIG_DIR:-/root/.config/beets}"
MUSIC_LIBRARY_ROOT="${MUSIC_LIBRARY_ROOT:-/mnt/storage/share/media/music/music}"
SLSKD_COMPLETE_DIR="${SLSKD_COMPLETE_DIR:-/mnt/scratch/slskd/complete}"
SLSKD_READY_DIR="${SLSKD_READY_DIR:-/mnt/scratch/slskd/ready}"
SLSKD_QUARANTINE_DIR="${SLSKD_QUARANTINE_DIR:-/mnt/scratch/slskd/quarantine}"
SLSKD_CONFIG_DIR="${SLSKD_CONFIG_DIR:-/home/docker/slskd}"
PLEX_HOST="${PLEX_HOST:-localhost}"
PLEX_PORT="${PLEX_PORT:-32400}"
PLEX_LIBRARY_NAME="${PLEX_LIBRARY_NAME:-Music}"
TELEGRAM_POLL_TIMEOUT="${TELEGRAM_POLL_TIMEOUT:-45}"
TELEGRAM_POLL_WAIT="${TELEGRAM_POLL_WAIT:-2}"
export BEETS_CONFIG_DIR MUSIC_LIBRARY_ROOT SLSKD_COMPLETE_DIR SLSKD_READY_DIR \
       SLSKD_QUARANTINE_DIR SLSKD_CONFIG_DIR PLEX_HOST PLEX_PORT PLEX_LIBRARY_NAME \
       TELEGRAM_POLL_TIMEOUT TELEGRAM_POLL_WAIT

# ─── Prerequisites ─────────────────────────────────────────────────────────────
hdr "Checking prerequisites"
missing=0
for bin in python3 beet fpcalc systemctl envsubst; do
    if command -v "$bin" &>/dev/null; then
        ok "$bin found"
    else
        warn "$bin not found — install before running the pipeline"
        missing=$((missing + 1))
    fi
done
python3 -c "import acoustid, mutagen" 2>/dev/null \
    && ok "python3 acoustid + mutagen modules present" \
    || warn "python3 modules missing: pip install pyacoustid mutagen"
[[ $missing -gt 0 ]] && warn "$missing prerequisite(s) missing — continuing anyway"

# ─── Scripts ───────────────────────────────────────────────────────────────────
hdr "Installing scripts → /usr/local/bin/"
install -m 755 "$REPO/scripts/"*.py "$REPO/scripts/"*.sh /usr/local/bin/
ok "$(ls "$REPO/scripts/" | wc -l) scripts installed"

# ─── Directories ───────────────────────────────────────────────────────────────
hdr "Creating runtime directories"
mkdir -p \
    "$SLSKD_COMPLETE_DIR" \
    "$SLSKD_READY_DIR" \
    "$SLSKD_QUARANTINE_DIR/unparsed" \
    "$SLSKD_QUARANTINE_DIR/incomplete" \
    "$BEETS_CONFIG_DIR" \
    "$SLSKD_CONFIG_DIR" \
    /var/lib/beets-import \
    /var/lib/slskd-telegram-bot
ok "Directories created"

# ─── Systemd units ─────────────────────────────────────────────────────────────
hdr "Installing systemd units → /etc/systemd/system/"
install -m 644 "$REPO/systemd/"* /etc/systemd/system/
ok "$(ls "$REPO/systemd/" | wc -l) units installed"

# ─── Config files ──────────────────────────────────────────────────────────────
hdr "Generating config files"

# beets config — only expand the variables we own; leave beets $albumartist etc alone
envsubst '$MUSIC_LIBRARY_ROOT $BEETS_CONFIG_DIR $DISCOGS_USER_TOKEN $PLEX_TOKEN $PLEX_HOST $PLEX_PORT $PLEX_LIBRARY_NAME' \
    < "$REPO/config/beets.yaml.template" \
    > "$BEETS_CONFIG_DIR/config.yaml"
ok "beets config → $BEETS_CONFIG_DIR/config.yaml"

# slskd config
envsubst '$SOULSEEK_USERNAME $SOULSEEK_PASSWORD' \
    < "$REPO/config/slskd.yml.template" \
    > "$SLSKD_CONFIG_DIR/slskd.yml"
ok "slskd config → $SLSKD_CONFIG_DIR/slskd.yml"

# telegram bot env
envsubst '$TELEGRAM_BOT_TOKEN $TELEGRAM_ALLOWED_CHAT_ID $TELEGRAM_POLL_TIMEOUT $TELEGRAM_POLL_WAIT' \
    < "$REPO/config/telegram-bot.env.template" \
    > /etc/default/slskd-telegram-bot
chmod 600 /etc/default/slskd-telegram-bot
ok "telegram bot env → /etc/default/slskd-telegram-bot"

# ─── Systemd enable ────────────────────────────────────────────────────────────
hdr "Enabling services"
systemctl daemon-reload
systemctl enable --now beets-import.timer
ok "beets-import.timer (every 15 min)"
systemctl enable --now slskd-promote-ready.timer
ok "slskd-promote-ready.timer (every 3 min)"
systemctl enable --now music-pipeline-healthcheck.timer
ok "music-pipeline-healthcheck.timer (every 30 min)"
systemctl enable --now slskd-telegram-bot.service
ok "slskd-telegram-bot.service"

# ─── Done ──────────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}${BOLD}Install complete.${NC}"
echo "  Logs:    /var/log/beets-import.log, /var/log/slskd-telegram-bot.log"
echo "  State:   /var/lib/beets-import/, /var/lib/slskd-telegram-bot/"
echo "  Configs: $BEETS_CONFIG_DIR/config.yaml, $SLSKD_CONFIG_DIR/slskd.yml"
