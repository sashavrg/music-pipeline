#!/usr/bin/env python3
"""
slskd-quarantine-requeue.py — Periodic re-queue attempt for quarantined albums.

Scans the top-level quarantine dirs and the quarantine/incomplete/ subdir.
For each album folder:
  1. If the album is already in the beets library -> remove the quarantine
     copy and send a Telegram notification (stale quarantine cleanup).
  2. If not in library and cooldown has passed -> search slskd and re-queue
     the best result. Cooldown is 7 days between retries; 14 days after a
     successful queue (to let the download complete).
  3. If 0 audio files -> skip silently.

State persisted in pipeline SQLite DB. Designed to run weekly (or on-demand).
"""

import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, '/usr/local/bin')
import pipeline_config as cfg
import pipeline_db

# ── Config ────────────────────────────────────────────────────────────────────

QUARANTINE_ROOT    = cfg.QUARANTINE_DIR
QUARANTINE_SUBDIRS = ['incomplete']
SKIP_SUBDIRS       = {'unparsed'}
BEETS_DB           = cfg.BEETS_DB
LIBRARY_ROOT       = str(cfg.LIBRARY_ROOT)
RECOVER_PATH       = '/usr/local/bin/slskd-recover.py'
LOG_FILE           = cfg.QUARANTINE_REQUEUE_LOG

RETRY_COOLDOWN_H   = 168    # 7 days between search retries
QUEUED_COOLDOWN_H  = 336    # 14 days cooldown after a successful queue
MAX_PENDING_DL     = cfg.FILL_MAX_PENDING_DL

AUDIO_EXTS = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus',
              '.wav', '.alac', '.aiff', '.wma', '.ape', '.wv'}

# ── Logging ───────────────────────────────────────────────────────────────────

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

# ── Beets library check ───────────────────────────────────────────────────────

def _decode(v) -> str:
    return v.decode('utf-8', errors='replace') if isinstance(v, bytes) else str(v)

def beets_track_count(artist: str, album: str) -> int:
    try:
        conn = sqlite3.connect(BEETS_DB, timeout=10)
        conds, params = [], []
        if artist:
            conds.append('(LOWER(albumartist) LIKE ? OR LOWER(artist) LIKE ?)')
            params += [f'%{artist.lower()}%', f'%{artist.lower()}%']
        if album:
            conds.append('LOWER(album) LIKE ?')
            params.append(f'%{album.lower()}%')
        where = ' AND '.join(conds) if conds else '1=1'
        rows = conn.execute(f'SELECT path FROM items WHERE {where}', params).fetchall()
        conn.close()
        return sum(1 for (p,) in rows if os.path.exists(_decode(p)))
    except Exception:
        return 0

def fs_track_count(artist: str, album: str) -> int:
    if not os.path.isdir(LIBRARY_ROOT):
        return 0
    artist_l = artist.lower().strip() if artist else ''
    album_l  = album.lower().strip()
    for adir in os.scandir(LIBRARY_ROOT):
        if not adir.is_dir():
            continue
        if artist_l and artist_l not in adir.name.lower():
            continue
        for bdir in os.scandir(adir.path):
            if not bdir.is_dir():
                continue
            clean = re.sub(r'\s*[\(\[].*?[\)\]]', '', bdir.name).strip().lower()
            if album_l not in bdir.name.lower() and album_l not in clean:
                continue
            count = sum(
                1 for f in os.scandir(bdir.path)
                if f.is_file() and os.path.splitext(f.name)[1].lower() in AUDIO_EXTS
            )
            if count > 0:
                return count
    return 0

def in_library(artist: str, album: str) -> int:
    return max(beets_track_count(artist, album), fs_track_count(artist, album))

# ── Folder metadata extraction ────────────────────────────────────────────────

_NOISE_RE = re.compile(
    r'\s*[\[\(](FLAC|MP3|WAV|WEB|CD|Hi[- ]?Res|24[- ]?bit|\d{2,3}kHz'
    r'|Lossless|Remaster(?:ed)?|Deluxe|Expanded|Limited'
    r'|APMU\d+|\d{4}-\d{2}|WEB-FLAC)[^\]\)]*[\]\)]',
    re.IGNORECASE,
)
_YEAR_PREFIX_RE = re.compile(r'^\d{4}[-\s,]+')
_BRACKET_YEAR_RE = re.compile(r'\s*[\[\(]\d{4}[\]\)]')
_DISC_CODE_RE   = re.compile(r'\b[A-Z]{2,5}\d{3,}\b')
_CURLY_RE       = re.compile(r'\s*\{[^}]*\}')

def clean_name(s: str) -> str:
    s = _NOISE_RE.sub('', s)
    s = _BRACKET_YEAR_RE.sub('', s)
    s = _YEAR_PREFIX_RE.sub('', s)
    s = _DISC_CODE_RE.sub('', s)
    s = _CURLY_RE.sub('', s)
    return re.sub(r'\s+', ' ', s).strip(' -_.')

def parse_folder(folder: Path) -> tuple[str, str]:
    name = clean_name(folder.name)
    if ' - ' in name:
        artist, album = name.split(' - ', 1)
        if not re.fullmatch(r'\d{4}', artist.strip()):
            return artist.strip(), album.strip()
    return '', name

def audio_count(folder: Path) -> int:
    return sum(
        1 for p in folder.rglob('*')
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith('.')
    )

# ── slskd recover loader ──────────────────────────────────────────────────────

def load_recover():
    spec = importlib.util.spec_from_file_location('slskd_recover', RECOVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load {RECOVER_PATH}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── Folder collection ─────────────────────────────────────────────────────────

def collect_folders() -> list[tuple[Path, str]]:
    items = []
    for entry in sorted(QUARANTINE_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in SKIP_SUBDIRS:
            continue
        if entry.name in QUARANTINE_SUBDIRS:
            for sub in sorted(entry.iterdir()):
                if sub.is_dir():
                    items.append((sub, f'{entry.name}/{sub.name}'))
        else:
            items.append((entry, entry.name))
    return items

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_logging()
    pipeline_db.init_db()
    log('===== slskd-quarantine-requeue start =====')

    try:
        rec = load_recover()
    except Exception as e:
        log(f'Failed to load slskd-recover module: {e}', 'ERROR')
        return 1

    state  = pipeline_db.get_quarantine_state()
    now    = time.time()
    items  = collect_folders()

    log(f'Found {len(items)} quarantine item(s) to evaluate')

    already_in_lib = skipped_empty = skipped_cooldown = 0
    requeued = no_results = errors = 0

    for folder, key in items:
        n_audio = audio_count(folder)
        if n_audio == 0:
            skipped_empty += 1
            log(f'[SKIP-EMPTY] {key}')
            continue

        artist, album = parse_folder(folder)
        log(f'[CHECK] {key} | artist="{artist}" album="{album}" | files={n_audio}')

        # ── Already in library? ───────────────────────────────────────────────
        lib_count = in_library(artist, album)
        if lib_count > 0:
            log(f'[IN-LIBRARY] {key} has {lib_count} track(s) in library — removing quarantine copy')
            try:
                shutil.rmtree(str(folder))
                pipeline_db.delete_quarantine_state(key)
            except Exception as e:
                log(f'[WARN] could not remove {folder}: {e}')
            already_in_lib += 1
            pipeline_db.push_notification('quarantine_cleared', key,
                                          reason='already_in_library', lib_tracks=lib_count)
            continue

        # ── Cooldown check ────────────────────────────────────────────────────
        entry      = state.get(key, {})
        last_attempt = entry.get('last_attempt', 0)
        last_queued  = entry.get('last_queued', 0)
        was_queued   = bool(last_queued)

        cooldown_h = QUEUED_COOLDOWN_H if was_queued else RETRY_COOLDOWN_H
        elapsed_h  = (now - max(last_attempt, last_queued)) / 3600

        if elapsed_h < cooldown_h:
            skipped_cooldown += 1
            log(f'[COOLDOWN] {key} — {elapsed_h:.0f}h elapsed, need {cooldown_h}h')
            continue

        # ── Search + queue ────────────────────────────────────────────────────
        query = album if not artist else f'{artist} {album}'
        if len(query) > 60:
            query = query[:60].rsplit(' ', 1)[0]

        log(f'[SEARCH] {key} | query="{query}"')
        new_last_attempt = now

        try:
            pending = rec.pending_download_count()
            if pending >= MAX_PENDING_DL:
                log(f'[SEARCH-SKIP] queue busy ({pending}), skipping {key}')
                pipeline_db.upsert_quarantine_state(key, new_last_attempt, last_queued)
                continue

            responses = rec.slskd_search(query)
            if not responses:
                log(f'[NO-RESULTS] {key}')
                no_results += 1
                pipeline_db.push_notification('quarantine_no_results', key, query=query)
                pipeline_db.upsert_quarantine_state(key, new_last_attempt, last_queued)
                continue

            best = rec.find_best_folder(responses)
            if best is None:
                log(f'[NO-QUALITY] {key} — results found but none passed quality filters')
                no_results += 1
                pipeline_db.push_notification('quarantine_no_results', key, query=query)
                pipeline_db.upsert_quarantine_state(key, new_last_attempt, last_queued)
                continue

            ok = rec.queue_download(best)
            if ok:
                new_last_queued = now
                requeued += 1
                speed_mb = best.upload_speed / 1_000_000
                log(
                    f'[REQUEUED] {key} | user={best.username} '
                    f'fmt={best.fmt} files={best.file_count} '
                    f'score={best.score} speed={speed_mb:.1f}MB/s'
                )
                pipeline_db.push_notification('quarantine_requeued', key,
                                              user=best.username, fmt=best.fmt.upper(),
                                              files=best.file_count, score=best.score,
                                              speed_mb=round(speed_mb, 1))
                pipeline_db.upsert_quarantine_state(key, new_last_attempt, new_last_queued)
            else:
                errors += 1
                log(f'[QUEUE-FAIL] {key}', 'WARN')
                pipeline_db.upsert_quarantine_state(key, new_last_attempt, last_queued)

        except Exception as e:
            errors += 1
            log(f'[ERROR] {key}: {e}', 'ERROR')
            pipeline_db.upsert_quarantine_state(key, new_last_attempt, last_queued)

        time.sleep(12)

    # Prune state for items no longer in quarantine
    current_keys = {k for _, k in items}
    stale = pipeline_db.prune_quarantine_state(current_keys)
    for k in stale:
        log(f'[STATE] pruning stale entry: {k}')

    log(
        f'[SUMMARY] total={len(items)} '
        f'in_library={already_in_lib} '
        f'requeued={requeued} '
        f'no_results={no_results} '
        f'cooldown={skipped_cooldown} '
        f'empty={skipped_empty} '
        f'errors={errors}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
