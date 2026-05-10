#!/usr/bin/env python3
"""
slskd-wishlist-check.py — Daily search for pending wishlist items.

For each pending wishlist entry:
  1. Skip if queued within 14 days (let download complete before retrying).
  2. Skip if searched within 24h (search cooldown).
  3. Search slskd and queue the best matching result.
  4. Record attempt/queue timestamps; push notification on success.
"""

import importlib.util
import sys
import time
from pathlib import Path

sys.path.insert(0, '/usr/local/bin')
import pipeline_config as cfg
import pipeline_db

LOG_FILE     = cfg.WISHLIST_CHECK_LOG
RECOVER_PATH = '/usr/local/bin/slskd-recover.py'

SEARCH_COOLDOWN_H = 24
QUEUED_COOLDOWN_H = 336   # 14 days
MAX_PENDING_DL    = cfg.FILL_MAX_PENDING_DL

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


def load_recover():
    spec = importlib.util.spec_from_file_location('slskd_recover', RECOVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load {RECOVER_PATH}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    setup_logging()
    pipeline_db.init_db()
    log('===== slskd-wishlist-check start =====')

    items = pipeline_db.get_wishlist_pending()
    if not items:
        log('Wishlist is empty — nothing to do')
        return 0

    log(f'Found {len(items)} pending wishlist item(s)')

    try:
        rec = load_recover()
    except Exception as e:
        log(f'Failed to load slskd-recover module: {e}', 'ERROR')
        return 1

    now = time.time()
    searched = queued_count = skipped_cooldown = no_results = errors = 0

    for item in items:
        wid          = item['id']
        artist       = item['artist']
        album        = item['album']
        last_attempt = item['last_attempt'] or 0
        last_queued  = item['last_queued']  or 0

        if last_queued and (now - last_queued) / 3600 < QUEUED_COOLDOWN_H:
            elapsed_h = (now - last_queued) / 3600
            log(f'[COOLDOWN] #{wid} "{artist} - {album}" queued {elapsed_h:.0f}h ago')
            skipped_cooldown += 1
            continue

        if last_attempt and (now - last_attempt) / 3600 < SEARCH_COOLDOWN_H:
            elapsed_h = (now - last_attempt) / 3600
            log(f'[COOLDOWN] #{wid} "{artist} - {album}" last tried {elapsed_h:.0f}h ago')
            skipped_cooldown += 1
            continue

        pending = rec.pending_download_count()
        if pending >= MAX_PENDING_DL:
            log(f'[BUSY] queue has {pending} pending, skipping #{wid}')
            continue

        query = f'{artist} {album}' if artist else album
        if len(query) > 60:
            query = query[:60].rsplit(' ', 1)[0]

        log(f'[SEARCH] #{wid} "{artist} - {album}" | query="{query}"')
        searched += 1

        try:
            responses = rec.slskd_search(query)
        except Exception as e:
            log(f'[ERROR] search failed for #{wid}: {e}', 'ERROR')
            errors += 1
            pipeline_db.update_wishlist_attempt(wid, now)
            continue

        if not responses:
            log(f'[NO-RESULTS] #{wid} "{artist} - {album}"')
            no_results += 1
            pipeline_db.update_wishlist_attempt(wid, now)
            continue

        best = rec.find_best_folder(responses)
        if best is None:
            log(f'[NO-QUALITY] #{wid} "{artist} - {album}" — no results passed quality filters')
            no_results += 1
            pipeline_db.update_wishlist_attempt(wid, now)
            continue

        ok_flag = rec.queue_download(best)
        if ok_flag:
            speed_mb = best.upload_speed / 1_000_000
            queued_count += 1
            log(
                f'[QUEUED] #{wid} "{artist} - {album}" | '
                f'user={best.username} fmt={best.fmt} files={best.file_count} '
                f'score={best.score} {speed_mb:.1f}MB/s'
            )
            pipeline_db.update_wishlist_attempt(wid, now, last_queued=now)
            pipeline_db.push_notification(
                'wishlist_queued', album,
                artist=artist, wid=wid,
                user=best.username, fmt=best.fmt.upper(),
                files=best.file_count, score=best.score,
                speed_mb=round(speed_mb, 1),
            )
        else:
            errors += 1
            log(f'[QUEUE-FAIL] #{wid} "{artist} - {album}"', 'WARN')
            pipeline_db.update_wishlist_attempt(wid, now)

        time.sleep(12)

    log(
        f'[SUMMARY] total={len(items)} searched={searched} queued={queued_count} '
        f'no_results={no_results} cooldown={skipped_cooldown} errors={errors}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
