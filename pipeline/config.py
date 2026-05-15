"""
pipeline_config.py — Central configuration for the music-pipeline.

All paths and tunable constants are read from environment variables at module
import time. Defaults match the historical host install so existing deployments
behave identically when no env is set.

Other scripts MUST import from here rather than redefining constants:

    import pipeline_config as cfg
    ... cfg.LIBRARY_ROOT ...

This module is the single source of truth — eliminating the dual-source-of-truth
problem flagged in .env.example (where path overrides only landed in install.sh,
never in the running scripts).
"""
from __future__ import annotations

import os
from pathlib import Path


def _path(key: str, default: str) -> Path:
    return Path(os.environ.get(key) or default)


def _str(key: str, default: str) -> str:
    return os.environ.get(key) or default


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    return int(raw) if raw not in (None, "") else default


def _bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def log_to_stdout() -> bool:
    """When true, scripts skip writing to log files and rely on stdout (captured
    by systemd journal on the host, by docker logs in a container)."""
    return _bool("LOG_TO_STDOUT", default=False)


def open_log_file(path):
    """Returns an appendable file handle for `path`, or None when LOG_TO_STDOUT
    is set. Each script's setup_logging() should: `_log_fh = cfg.open_log_file(LOG_FILE)`
    and then guard writes with `if _log_fh:`. The log() function should always
    print() to stdout regardless — that side stays the same in both modes."""
    if log_to_stdout():
        return None
    from pathlib import Path as _P
    _P(path).parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a", encoding="utf-8")


# ─── Roots ──────────────────────────────────────────────────────────────────
# Env var names align with .env.example so install.sh / docker-compose can pass
# them through unchanged. Python code uses the shorter attribute names below.
LIBRARY_ROOT       = _path("MUSIC_LIBRARY_ROOT", "/mnt/storage/share/media/music/music")
AUDIOBOOKS_LIBRARY_ROOT = _path("AUDIOBOOKS_LIBRARY_ROOT", "/mnt/storage/share/media/audiobooks")

# Audiobookshelf integration (auto-match worker)
ABS_URL        = _str("ABS_URL",        "http://localhost:13378")
# Token lookup falls back to the ABS sqlite DB if this env is unset.
ABS_TOKEN      = _str("ABS_TOKEN",      "")
ABS_DB_PATH    = _path("ABS_DB_PATH",   "/srv/audiobookshelf/config/absdatabase.sqlite")
ABS_PROVIDER   = _str("ABS_PROVIDER",   "openlibrary")
SCRATCH_ROOT       = _path("SCRATCH_ROOT",       "/mnt/scratch/slskd")
BEETS_CONFIG_DIR   = _path("BEETS_CONFIG_DIR",   "/root/.config/beets")
PIPELINE_STATE_DIR = _path("PIPELINE_STATE_DIR", "/var/lib/pipeline")
LOG_DIR            = _path("LOG_DIR",            "/var/log")

# ─── Beets ──────────────────────────────────────────────────────────────────
BEETS_DB = _str("BEETS_DB", str(BEETS_CONFIG_DIR / "library.db"))

# ─── slskd ──────────────────────────────────────────────────────────────────
SLSKD_URL          = _str("SLSKD_URL",          "http://localhost:5030")
SLSKD_API_KEY_PATH = _path("SLSKD_API_KEY_PATH", "/etc/slskd-api.key")

# ─── Scratch tree ──────────────────────────────────────────────────────────
# Each dir defaults to a child of SCRATCH_ROOT, but can be overridden individually
# (matches the existing .env.example shape: SLSKD_COMPLETE_DIR, SLSKD_READY_DIR, ...).
COMPLETE_DIR              = _path("SLSKD_COMPLETE_DIR",   str(SCRATCH_ROOT / "complete"))
READY_DIR                 = _path("SLSKD_READY_DIR",      str(SCRATCH_ROOT / "ready"))
INCOMPLETE_DIR            = _path("SLSKD_INCOMPLETE_DIR", str(SCRATCH_ROOT / "incomplete"))
QUARANTINE_DIR            = _path("SLSKD_QUARANTINE_DIR", str(SCRATCH_ROOT / "quarantine"))
QUARANTINE_INCOMPLETE_DIR = QUARANTINE_DIR / "incomplete"
QUARANTINE_UNPARSED_DIR   = QUARANTINE_DIR / "unparsed"

# ─── Pipeline state (derived) ──────────────────────────────────────────────
PIPELINE_DB_PATH   = PIPELINE_STATE_DIR / "pipeline.db"
PIPELINE_LOCK_PATH = PIPELINE_STATE_DIR / "ready-dir.lock"

BEETS_IMPORT_STATE_DIR = _path("BEETS_IMPORT_STATE_DIR", "/var/lib/beets-import")
PENDING_DELETIONS_DIR  = BEETS_IMPORT_STATE_DIR / "pending-deletions"

BOT_STATE_DIR         = _path("BOT_STATE_DIR",         "/var/lib/slskd-telegram-bot")
HEALTHCHECK_STATE_DIR = _path("HEALTHCHECK_STATE_DIR", "/var/lib/music-pipeline-healthcheck")

# ─── Logs (derived from LOG_DIR) ────────────────────────────────────────────
BEETS_LOG               = LOG_DIR / "beets-import.log"
RECOVER_LOG             = LOG_DIR / "slskd-recover.log"
RECOVER_PROGRESS_FILE   = LOG_DIR / "slskd-recover-progress.json"
RECOVER_MISSING_REPORT  = LOG_DIR / "slskd-recover-missing.md"
TELEGRAM_BOT_LOG        = LOG_DIR / "slskd-telegram-bot.log"
INCOMPLETE_WATCHDOG_LOG = LOG_DIR / "slskd-incomplete-watchdog.log"
FILL_MISSING_LOG        = LOG_DIR / "slskd-fill-missing-tracks.log"
QUARANTINE_REQUEUE_LOG  = LOG_DIR / "slskd-quarantine-requeue.log"
WISHLIST_CHECK_LOG      = LOG_DIR / "slskd-wishlist-check.log"
WEEKLY_DIGEST_LOG       = LOG_DIR / "pipeline-weekly-digest.log"
HEALTHCHECK_LOG         = LOG_DIR / "music-pipeline-health.log"
ABS_AUTOMATCH_LOG       = LOG_DIR / "abs-automatch.log"

# ─── Other library files ────────────────────────────────────────────────────
LOST_ALBUMS_FILE = _str("LOST_ALBUMS_FILE", str(LIBRARY_ROOT / "LOST_ALBUMS.md"))

# ─── Tuning constants (env-overridable, sane defaults) ─────────────────────
# Soulseek peer filtering (shared by recover.py and incomplete-watchdog.py)
MIN_UPLOAD_SPEED = _int("MIN_UPLOAD_SPEED", 2_000_000)   # bytes/s
MAX_PENDING_DL   = _int("MAX_PENDING_DL", 100)

# slskd search/poll behaviour (recover.py)
SEARCH_DELAY    = _int("SEARCH_DELAY",    10)
POLL_INTERVAL   = _int("POLL_INTERVAL",   4)
SEARCH_TIMEOUT  = _int("SEARCH_TIMEOUT",  35)
QUEUE_POLL_WAIT = _int("QUEUE_POLL_WAIT", 30)

# Incomplete watchdog
ALERT_AFTER_H        = _int("ALERT_AFTER_H",        6)
REQUEUE_AFTER_H      = _int("REQUEUE_AFTER_H",      48)
QUARANTINE_AFTER_H   = _int("QUARANTINE_AFTER_H",   168)
SEARCH_COOLDOWN_H    = _int("SEARCH_COOLDOWN_H",    24)
REREQUEUE_COOLDOWN_H = _int("REREQUEUE_COOLDOWN_H", 72)

# Fill-missing-tracks (different cap than recover/watchdog)
FILL_QUEUE_COOLDOWN_H = _int("FILL_QUEUE_COOLDOWN_H", 12)
FILL_MIN_HOLD_AGE_H   = _int("FILL_MIN_HOLD_AGE_H",   2)
FILL_MAX_PENDING_DL   = _int("FILL_MAX_PENDING_DL",   80)
FILL_MAX_SOURCES      = _int("FILL_MAX_SOURCES",      5)

# Telegram bot
TELEGRAM_POLL_TIMEOUT = _int("TELEGRAM_POLL_TIMEOUT", 45)
TELEGRAM_POLL_WAIT    = _int("TELEGRAM_POLL_WAIT",    2)
