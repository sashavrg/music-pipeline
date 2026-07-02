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

from . import config as cfg
from . import db as pipeline_db
from . import parks as parks_mod

LOG_FILE = cfg.WEEKLY_DIGEST_LOG

_log_fh = None


def setup_logging():
    global _log_fh
    _log_fh = cfg.open_log_file(LOG_FILE)


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

    # fail-to-review visibility: the digest is the pressure gauge on the
    # safety valve — parked folders, stuck ledger rows, items headed for
    # dead-letter. Without this they rot invisibly.
    parked = parks_mod.collect_parks()
    stats['parks'] = [
        {'name': p['name'], 'age_days': round(p['age_days'], 1),
         'n_files': p['n_files'], 'reason': p['reason']}
        for p in parked[:15]
    ]
    stats['parks_total'] = len(parked)
    stats['ledger_stuck'] = [
        {'artist': r['artist'], 'album': r['album'], 'state': r['state'],
         'age_h': round((time.time() - r['queued_at']) / 3600.0, 1)}
        for r in pipeline_db.ledger_live_rows()
        if (time.time() - r['queued_at']) > 24 * 3600
    ]
    qstate = pipeline_db.get_quarantine_state()
    stats['quarantine_failing'] = sum(
        1 for v in qstate.values() if int(v.get('fruitless', 0)) > 0)

    log(
        f'new_albums={len(stats["new_albums"])} '
        f'new_tracks={stats["new_tracks"]} '
        f'held={len(stats["held"])} '
        f'events={sum(stats["events_by_type"].values())} '
        f'parked={stats["parks_total"]} '
        f'ledger_stuck={len(stats["ledger_stuck"])} '
        f'quarantine_failing={stats["quarantine_failing"]}'
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
