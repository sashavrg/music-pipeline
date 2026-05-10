#!/bin/bash
# docker-entrypoint.sh — render config files from env at container start.
#
# install.sh runs envsubst on the YAML templates once at install time. That's
# fine on a host install but breaks the docker model: a user changing
# MUSIC_LIBRARY_ROOT or PLEX_TOKEN via compose env should NOT have to rebuild
# the image. So when the pipeline runs in a container, this entrypoint
# re-renders the configs at every container start, then execs the real command.
#
# The Dockerfile sets:
#   ENTRYPOINT ["/opt/music-pipeline/docker-entrypoint.sh"]
#   CMD       ["slskd-telegram-bot"]   # or whatever the service is
#
# This script is a no-op when the templates are missing (e.g. running outside
# the package), so it's safe to keep alongside the host install too.
set -euo pipefail

REPO="${MUSIC_PIPELINE_ROOT:-/opt/music-pipeline}"
TEMPLATES="$REPO/config"

# ─── Defaults for path-shaped env vars ────────────────────────────────────────
# Inside a container we default everything under /data/ instead of the host's
# /mnt/, /var/, and /root/ paths. Compose can override any of these.
export BEETS_CONFIG_DIR="${BEETS_CONFIG_DIR:-/data/beets}"
export MUSIC_LIBRARY_ROOT="${MUSIC_LIBRARY_ROOT:-/data/library}"
export SCRATCH_ROOT="${SCRATCH_ROOT:-/data/scratch}"
export PIPELINE_STATE_DIR="${PIPELINE_STATE_DIR:-/data/state}"
export LOG_DIR="${LOG_DIR:-/data/logs}"
export SLSKD_URL="${SLSKD_URL:-http://slskd:5030}"
export LOG_TO_STDOUT="${LOG_TO_STDOUT:-true}"

mkdir -p "$BEETS_CONFIG_DIR" "$SCRATCH_ROOT" "$PIPELINE_STATE_DIR" "$LOG_DIR" \
         "$SCRATCH_ROOT/complete" "$SCRATCH_ROOT/ready" \
         "$SCRATCH_ROOT/incomplete" "$SCRATCH_ROOT/quarantine"

# ─── Render beets config ──────────────────────────────────────────────────────
if [ -f "$TEMPLATES/beets.yaml.template" ]; then
    : "${PLEX_HOST:=plex}"
    : "${PLEX_PORT:=32400}"
    : "${PLEX_LIBRARY_NAME:=Music}"
    : "${DISCOGS_USER_TOKEN:=}"
    : "${PLEX_TOKEN:=}"
    export PLEX_HOST PLEX_PORT PLEX_LIBRARY_NAME DISCOGS_USER_TOKEN PLEX_TOKEN

    envsubst '$MUSIC_LIBRARY_ROOT $BEETS_CONFIG_DIR $DISCOGS_USER_TOKEN $PLEX_TOKEN $PLEX_HOST $PLEX_PORT $PLEX_LIBRARY_NAME' \
        < "$TEMPLATES/beets.yaml.template" \
        > "$BEETS_CONFIG_DIR/config.yaml"
fi

# ─── Render slskd config ──────────────────────────────────────────────────────
# Only when slskd shares this volume (e.g. a sidecar bind-mount); skip when
# slskd is a separate container managing its own config.
if [ -n "${SLSKD_CONFIG_DIR:-}" ] && [ -f "$TEMPLATES/slskd.yml.template" ] && [ -d "$SLSKD_CONFIG_DIR" ]; then
    : "${SOULSEEK_USERNAME:=}"
    : "${SOULSEEK_PASSWORD:=}"
    : "${SLSKD_WEB_USERNAME:=slskd}"
    : "${SLSKD_WEB_PASSWORD:=}"
    : "${SLSKD_METRICS_USERNAME:=metrics}"
    : "${SLSKD_METRICS_PASSWORD:=}"
    export SOULSEEK_USERNAME SOULSEEK_PASSWORD SLSKD_WEB_USERNAME SLSKD_WEB_PASSWORD SLSKD_METRICS_USERNAME SLSKD_METRICS_PASSWORD

    envsubst '$SOULSEEK_USERNAME $SOULSEEK_PASSWORD $SLSKD_WEB_USERNAME $SLSKD_WEB_PASSWORD $SLSKD_METRICS_USERNAME $SLSKD_METRICS_PASSWORD' \
        < "$TEMPLATES/slskd.yml.template" \
        > "$SLSKD_CONFIG_DIR/slskd.yml"
    chmod 600 "$SLSKD_CONFIG_DIR/slskd.yml"
fi

# ─── Hand off to the real command ─────────────────────────────────────────────
exec "$@"
