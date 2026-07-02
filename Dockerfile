# music-pipeline — slskd → beets → Plex
#
# Build context: the repo root.
# Pairs with the upstream `slskd/slskd` image (see docker-compose.yml) — this
# image does NOT bundle slskd; the pipeline talks to slskd's HTTP API.

FROM python:3.12-slim

# ─── System deps ──────────────────────────────────────────────────────────────
#   libchromaprint-tools  fpcalc binary used by the chroma beets plugin
#   gettext-base          envsubst, used by docker-entrypoint.sh
#   sqlite3               healthcheck queries the slskd ledger
#   ca-certificates       slskd API + discogs/last.fm/etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libchromaprint-tools \
        gettext-base \
        sqlite3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/music-pipeline

# ─── Source (kept whole so pyproject.toml's setuptools build sees the package) ─
COPY pyproject.toml ./
COPY pipeline/                pipeline/
COPY bin/                     bin/
COPY config/                  config/
COPY docker/loop.sh           /usr/local/bin/loop
COPY docker-entrypoint.sh     ./

# ─── Install ─────────────────────────────────────────────────────────────────
# `pip install .` resolves every dep listed in pyproject.toml (beets + plugin
# extras: mutagen, pyacoustid, beautifulsoup4, python3-discogs-client, pillow,
# pylast, pyyaml, requests) AND installs the `pipeline` package + the
# console_scripts (slskd-recover, beets-quality-upgrade, etc.) into
# /usr/local/bin/.
RUN pip install --no-cache-dir . \
 && install -m 755 bin/music-pipeline-healthcheck.sh /usr/local/bin/ \
 && chmod +x /usr/local/bin/loop docker-entrypoint.sh

ENV MUSIC_PIPELINE_ROOT=/opt/music-pipeline \
    LOG_TO_STDOUT=true \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

VOLUME ["/data/scratch", "/data/state", "/data/beets", "/data/library", "/data/logs"]

ENTRYPOINT ["/opt/music-pipeline/docker-entrypoint.sh"]
CMD ["slskd-telegram-bot"]
