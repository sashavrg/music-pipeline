#!/usr/bin/env python3
"""
pipeline-weekly-digest.py — Push a weekly activity summary to Telegram.

Queries the pipeline DB and beets library for the past 7 days and enqueues
a single 'weekly_digest' notification. The Telegram bot renders it on next drain.
Designed to run Sunday evenings via systemd timer.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, '/usr/local/bin')
import pipeline_config as cfg
import pipeline_db

LOG_FILE = cfg.WEEKLY_DIGEST_LOG

_log_fh = None


def setup_logging():
    global _log_fh
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _log_fh = open(LOG_FILE, 'a', encoding='utf-8')


def log(msg: str, level: str = 'INFO'):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} [{level}] {msg}'
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + '\n')
        _log_fh.flush()


def main():
    setup_logging()
    pipeline_db.init_db()
    log('===== pipeline-weekly-digest start =====')

    stats = pipeline_db.get_weekly_stats()

    log(
        f'new_albums={len(stats["new_albums"])} '
        f'new_tracks={stats["new_tracks"]} '
        f'held={len(stats["held"])} '
        f'events={sum(stats["events_by_type"].values())}'
    )

    pipeline_db.push_notification('weekly_digest', '', **stats)
    log('Weekly digest notification pushed')

    # Prune notify_queue rows older than 30 days (delivered only).
    pruned = pipeline_db.prune_notify_queue(retain_days=30)
    if pruned:
        log(f'Pruned {pruned} delivered notify_queue rows older than 30 days')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
