#!/usr/bin/env python3
"""
slskd-incomplete-watchdog.py — Monitor slskd's incomplete/ staging dir.

slskd writes partial files to incomplete/ during download; on success it
moves them to complete/. Folders that linger in incomplete/ are stalled or
abandoned transfers. This script:

  any  library check: if album already exists in beets/library, delete incomplete copy
  >6h  stalled (no active slskd transfer, no recent writes): alert via Telegram once
  >48h stalled: attempt a re-search + re-queue via slskd API (24h cooldown between attempts)
  >7d  stalled: give up, quarantine with reason file + notify

State is persisted in the pipeline SQLite DB.
"""

import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config as cfg
from . import db as pipeline_db

# ── Config ────────────────────────────────────────────────────────────────────

INCOMPLETE_DIR     = cfg.INCOMPLETE_DIR
QUARANTINE_DIR     = cfg.QUARANTINE_INCOMPLETE_DIR
LOG_FILE           = cfg.INCOMPLETE_WATCHDOG_LOG

BEETS_DB           = cfg.BEETS_DB
LIBRARY_ROOT       = str(cfg.LIBRARY_ROOT)
SLSKD_URL          = cfg.SLSKD_URL

ALERT_AFTER_H        = cfg.ALERT_AFTER_H
REQUEUE_AFTER_H      = cfg.REQUEUE_AFTER_H
QUARANTINE_AFTER_H   = cfg.QUARANTINE_AFTER_H
SEARCH_COOLDOWN_H    = cfg.SEARCH_COOLDOWN_H
REREQUEUE_COOLDOWN_H = cfg.REREQUEUE_COOLDOWN_H

MIN_UPLOAD_SPEED   = cfg.MIN_UPLOAD_SPEED
MAX_PENDING_DL     = cfg.MAX_PENDING_DL

FORMAT_SCORES = {
    'flac': 500, 'wav': 450, 'aiff': 450, 'aif': 450,
    'ape': 420, 'wv': 420, 'alac': 420,
    'opus': 300, 'mp3': 200,
}
MP3_MIN_KBPS = 310
LOSSLESS_MIN_SCORE = 400

# ── Logging ───────────────────────────────────────────────────────────────────

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

# ── slskd API helpers ─────────────────────────────────────────────────────────

def api_get(path: str):
    req = urllib.request.Request(SLSKD_URL + path, headers=pipeline_db.slskd_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def api_post(path: str, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        SLSKD_URL + path, data=data, method='POST',
        headers=pipeline_db.slskd_headers({'Content-Type': 'application/json'}),
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'HTTP {e.code} on POST {path}: {e.read().decode(errors="replace")}') from e

# ── Active download detection ─────────────────────────────────────────────────

_ACTIVE_STATES = {'InProgress', 'Queued', 'Requested'}

def get_active_incomplete_basenames() -> set:
    active = set()
    try:
        transfers = api_get('/api/v0/transfers/downloads')
        for user_entry in transfers:
            for directory in user_entry.get('directories', []):
                for f in directory.get('files', []):
                    state_desc = f.get('stateDescription', '')
                    base_state = state_desc.split(',')[0].strip()
                    if base_state not in _ACTIVE_STATES:
                        continue
                    filename = f.get('filename', '')
                    parts = re.split(r'[/\\]', filename)
                    for part in parts:
                        part = part.strip()
                        if part and (INCOMPLETE_DIR / part).is_dir():
                            active.add(part)
    except Exception as e:
        log(f'Could not fetch active transfers: {e}', 'WARN')
    return active

# ── Folder helpers ────────────────────────────────────────────────────────────

def folder_newest_mtime(folder: Path) -> float:
    latest = folder.stat().st_mtime
    for p in folder.rglob('*'):
        try:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            pass
    return latest

def folder_audio_count(folder: Path) -> int:
    exts = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.alac', '.aiff', '.wma'}
    return sum(
        1 for p in folder.rglob('*')
        if p.is_file() and p.suffix.lower() in exts and not p.name.startswith('.')
    )

# ── Query cleaning ────────────────────────────────────────────────────────────

_NOISE_RE = re.compile(
    r'\s*[\[\(](FLAC|MP3|WAV|WEB|CD|Hi[- ]?Res|24[- ]?bit|\d{2,3}kHz'
    r'|Lossless|Remaster(?:ed)?|Deluxe|Expanded|Limited'
    r'|APMU\d+|\d{4}-\d{2})[^\]\)]*[\]\)]',
    re.IGNORECASE,
)
_YEAR_PREFIX_RE = re.compile(r'^\d{4}[-\s]+')
_DISC_CODE_RE   = re.compile(r'\b[A-Z]{2,5}\d{3,}\b')

def clean_folder_name(name: str) -> str:
    s = _NOISE_RE.sub('', name)
    s = _YEAR_PREFIX_RE.sub('', s)
    s = _DISC_CODE_RE.sub('', s)
    return re.sub(r'\s+', ' ', s).strip(' -_.')

def folder_to_query(name: str) -> str:
    q = clean_folder_name(name)
    if len(q) > 60:
        q = q[:60].rsplit(' ', 1)[0]
    return q

# ── Beets / library check ─────────────────────────────────────────────────────

_LIB_NOISE_RE = re.compile(
    r'\s*[\[\(](FLAC|MP3|WAV|WEB|CD|Hi[- ]?Res|24[- ]?bit|\d{2,3}kHz'
    r'|Lossless|Remaster(?:ed)?|Deluxe|Expanded|Limited)[^\]\)]*[\]\)]',
    re.IGNORECASE,
)
_LIB_YEAR_RE  = re.compile(r'^\d{4}[-\s,]+')
_LIB_CURLY_RE = re.compile(r'\s*\{[^}]*\}')

def _clean_lib(s: str) -> str:
    s = _LIB_NOISE_RE.sub('', s)
    s = _LIB_YEAR_RE.sub('', s)
    s = _LIB_CURLY_RE.sub('', s)
    return re.sub(r'\s+', ' ', s).strip(' -_.')

def _beets_track_count(album_clean: str) -> int:
    try:
        with sqlite3.connect(BEETS_DB, timeout=10) as conn:
            rows = conn.execute(
                "SELECT album, path FROM items WHERE album LIKE ?",
                (f'%{album_clean[:20]}%',)
            ).fetchall()
        count = 0
        for album_val, path_val in rows:
            if _clean_lib(album_val or '').lower() == album_clean.lower():
                if path_val and os.path.exists(path_val.decode() if isinstance(path_val, bytes) else path_val):
                    count += 1
        return count
    except Exception:
        return 0

def _fs_track_count(album_clean: str) -> int:
    audio_exts = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus',
                  '.wav', '.alac', '.aiff', '.wma', '.ape', '.wv'}
    try:
        lib = Path(LIBRARY_ROOT)
        for artist_dir in lib.iterdir():
            if not artist_dir.is_dir():
                continue
            for album_dir in artist_dir.iterdir():
                if not album_dir.is_dir():
                    continue
                if _clean_lib(album_dir.name).lower() == album_clean.lower():
                    return sum(
                        1 for p in album_dir.rglob('*')
                        if p.is_file() and p.suffix.lower() in audio_exts
                    )
    except Exception:
        pass
    return 0

def in_library(folder_name: str) -> int:
    album_clean = _clean_lib(folder_name)
    return max(_beets_track_count(album_clean), _fs_track_count(album_clean))

# ── slskd search + queue ──────────────────────────────────────────────────────

SEARCH_DELAY   = 5
POLL_INTERVAL  = 3
SEARCH_TIMEOUT = 30

def slskd_search(query: str):
    try:
        result = api_post('/api/v0/searches', {
            'searchText': query, 'fileLimit': 5000,
            'filterResponses': True, 'minimumResponseFileCount': 1,
        })
    except Exception as e:
        log(f'Search error for "{query}": {e}', 'WARN')
        return None

    if not result or 'id' not in result:
        return None

    search_id = result['id']
    deadline = time.time() + SEARCH_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            status = api_get(f'/api/v0/searches/{search_id}')
            if status.get('isComplete'):
                return status.get('responses', [])
        except Exception:
            pass
    try:
        status = api_get(f'/api/v0/searches/{search_id}')
        return status.get('responses', [])
    except Exception:
        return []

def pending_download_count() -> int:
    try:
        transfers = api_get('/api/v0/transfers/downloads')
        count = 0
        for user_entry in transfers:
            for directory in user_entry.get('directories', []):
                for f in directory.get('files', []):
                    s = f.get('stateDescription', '').split(',')[0].strip()
                    if s in _ACTIVE_STATES:
                        count += 1
        return count
    except Exception:
        return 0

def score_folder(files: list, upload_speed: int) -> int:
    fmt_scores = []
    for f in files:
        fname = f.get('filename', '')
        ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
        fs = FORMAT_SCORES.get(ext, 0)
        if fs == 0:
            continue
        if fs < LOSSLESS_MIN_SCORE:
            size = f.get('size', 0)
            length = f.get('length', 0)
            if length and size:
                kbps = (size * 8) / (length * 1000)
                if kbps < MP3_MIN_KBPS:
                    continue
        fmt_scores.append(fs)
    if not fmt_scores:
        return 0
    speed_bonus = min(int(upload_speed / 1_000_000) * 5, 50)
    return int(sum(fmt_scores) / len(fmt_scores)) + speed_bonus

def find_best_folder(responses: list):
    best = None
    best_score = 0
    for resp in responses:
        username = resp.get('username', '')
        upload_speed = resp.get('uploadSpeed', 0)
        if upload_speed < MIN_UPLOAD_SPEED:
            continue
        files = resp.get('files', [])
        if not files:
            continue
        dirs = {}
        for f in files:
            fname = f.get('filename', '')
            parts = re.split(r'[/\\]', fname)
            dir_key = '\\'.join(parts[:-1]) if len(parts) > 1 else ''
            dirs.setdefault(dir_key, []).append(f)
        for dir_key, dir_files in dirs.items():
            s = score_folder(dir_files, upload_speed)
            if s > best_score:
                best_score = s
                best = (username, dir_key, dir_files, s)
    return best

def queue_download(username: str, files: list) -> bool:
    try:
        api_post(f'/api/v0/transfers/downloads/{urllib.parse.quote(username)}',
                 [{'filename': f['filename'], 'size': f.get('size', 0)} for f in files])
        return True
    except Exception as e:
        log(f'Queue error for {username}: {e}', 'WARN')
        return False

# ── Unique quarantine target ──────────────────────────────────────────────────

def unique_target(dst_root: Path, name: str) -> Path:
    t = dst_root / name
    if not t.exists():
        return t
    stamp = time.strftime('%Y%m%d-%H%M%S')
    c = dst_root / f'{name}__{stamp}'
    i = 1
    while c.exists():
        c = dst_root / f'{name}__{stamp}-{i}'
        i += 1
    return c

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_logging()
    pipeline_db.init_db()
    log('===== slskd-incomplete-watchdog start =====')

    if not INCOMPLETE_DIR.is_dir():
        log(f'Incomplete dir not found: {INCOMPLETE_DIR}', 'WARN')
        return

    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    state = pipeline_db.get_incomplete_state()
    now   = time.time()

    active_basenames = get_active_incomplete_basenames()
    log(f'Active slskd downloads matching incomplete/: {active_basenames or "none"}')

    folders = sorted([p for p in INCOMPLETE_DIR.iterdir() if p.is_dir()])
    log(f'Scanning {len(folders)} folder(s) in incomplete/')

    alerted = skipped_active = requeued = quarantined = unchanged = 0
    current_names = set()

    for folder in folders:
        name = folder.name
        current_names.add(name)

        if name in active_basenames:
            skipped_active += 1
            log(f'[ACTIVE] {name} — slskd transfer in progress, skipping')
            pipeline_db.delete_incomplete_state(name)
            continue

        newest_mtime = folder_newest_mtime(folder)
        age_h        = (now - newest_mtime) / 3600
        entry        = state.get(name, {})
        first_seen   = entry.get('first_seen', now)
        total_age_h  = (now - first_seen) / 3600
        audio_count  = folder_audio_count(folder)

        log(f'[SCAN] {name} | age={age_h:.1f}h | total_stalled={total_age_h:.1f}h | files={audio_count}')

        # ── Already in library? ───────────────────────────────────────────────
        lib_count = in_library(name)
        if lib_count:
            log(f'[IN-LIBRARY] {name} has {lib_count} track(s) in library — removing incomplete copy')
            try:
                shutil.rmtree(str(folder))
            except Exception as e:
                log(f'[WARN] could not remove {folder}: {e}')
            pipeline_db.delete_incomplete_state(name)
            current_names.discard(name)
            pipeline_db.push_notification('incomplete_in_library_removed', name, lib_tracks=lib_count)
            continue

        # ── Quarantine (>7 days) ──────────────────────────────────────────────
        if total_age_h >= QUARANTINE_AFTER_H:
            reason_file = folder / '_WATCHDOG_REASON.txt'
            try:
                reason_file.write_text(
                    f'Quarantined by incomplete-watchdog: {time.strftime("%Y-%m-%d %H:%M:%S")}\n'
                    f'Total stalled: {total_age_h:.1f}h\n'
                    f'Last write: {age_h:.1f}h ago\n'
                    f'Audio files present: {audio_count}\n',
                    encoding='utf-8',
                )
            except Exception as e:
                log(f'[WARN] could not write reason file: {e}')
            target = unique_target(QUARANTINE_DIR, name)
            shutil.move(str(folder), str(target))
            pipeline_db.delete_incomplete_state(name)
            current_names.discard(name)
            quarantined += 1
            log(f'[QUARANTINE] {name} stalled {total_age_h:.1f}h -> {target}')
            pipeline_db.push_notification('incomplete_quarantined', name,
                                          stalled_hours=round(total_age_h, 1),
                                          audio_files=audio_count)
            continue

        # ── Re-queue (>48h) ───────────────────────────────────────────────────
        last_search_time   = entry.get('last_search_time', 0)
        hours_since_search = (now - last_search_time) / 3600
        already_requeued   = entry.get('requeued', False)

        # Allow a second requeue if the first one didn't unblock the folder:
        # if requeued is True and the search cooldown is REREQUEUE_COOLDOWN_H+ old,
        # the original requeue clearly didn't help — try again before quarantine.
        cooldown_h = REREQUEUE_COOLDOWN_H if already_requeued else SEARCH_COOLDOWN_H
        eligible = (not already_requeued) or hours_since_search >= REREQUEUE_COOLDOWN_H

        if total_age_h >= REQUEUE_AFTER_H and eligible and hours_since_search >= cooldown_h:
            pending = pending_download_count()
            if pending >= MAX_PENDING_DL:
                log(f'[REQUEUE-SKIP] queue busy ({pending}), will retry next cycle')
            else:
                query = folder_to_query(name)
                log(f'[REQUEUE] searching for: {query}')
                responses = slskd_search(query)
                new_requeued   = entry.get('requeued', False)
                new_requeue_ts = entry.get('requeue_time', '')
                if responses:
                    best = find_best_folder(responses)
                    if best:
                        username, directory, files, score = best
                        ok = queue_download(username, files)
                        if ok:
                            new_requeued   = True
                            new_requeue_ts = time.strftime('%Y-%m-%d %H:%M:%S')
                            requeued += 1
                            log(f'[REQUEUE] queued {len(files)} files from {username} score={score}')
                            pipeline_db.push_notification('incomplete_requeued', name,
                                                          files=len(files), user=username, score=score,
                                                          stalled_hours=round(total_age_h, 1))
                        else:
                            log(f'[REQUEUE-FAIL] could not queue for {name}', 'WARN')
                    else:
                        log(f'[REQUEUE] no quality results for: {query}')
                        pipeline_db.push_notification('incomplete_no_results', name,
                                                      query=query, stalled_hours=round(total_age_h, 1))
                else:
                    log(f'[REQUEUE] search returned nothing for: {query}')
                pipeline_db.upsert_incomplete_state(
                    name, first_seen=first_seen,
                    alerted=entry.get('alerted', False),
                    requeued=new_requeued,
                    last_search_time=now,
                    requeue_time=new_requeue_ts,
                )
                continue
        elif total_age_h >= REQUEUE_AFTER_H and eligible and hours_since_search < cooldown_h:
            log(f'[REQUEUE-COOLDOWN] {name} — searched {hours_since_search:.1f}h ago, need {cooldown_h}h '
                f'(already_requeued={already_requeued})')

        # ── Alert (>6h, first time) ───────────────────────────────────────────
        elif age_h >= ALERT_AFTER_H and not entry.get('alerted'):
            alerted += 1
            log(f'[ALERT] {name} stalled {age_h:.1f}h with {audio_count} file(s)')
            pipeline_db.push_notification('incomplete_stalled', name,
                                          stalled_hours=round(age_h, 1), audio_files=audio_count)
            pipeline_db.upsert_incomplete_state(
                name, first_seen=first_seen, alerted=True,
                requeued=entry.get('requeued', False),
                last_search_time=entry.get('last_search_time', 0),
                requeue_time=entry.get('requeue_time', ''),
            )
            continue
        else:
            unchanged += 1

        pipeline_db.upsert_incomplete_state(
            name, first_seen=first_seen,
            alerted=entry.get('alerted', False),
            requeued=entry.get('requeued', False),
            last_search_time=entry.get('last_search_time', 0),
            requeue_time=entry.get('requeue_time', ''),
        )

    # Prune state entries for folders that are gone
    stale = pipeline_db.prune_incomplete_state(current_names)
    for k in stale:
        log(f'[STATE] pruning removed folder: {k}')

    log(
        f'[SUMMARY] scanned={len(folders)} active={skipped_active} '
        f'alerted={alerted} requeued={requeued} '
        f'quarantined={quarantined} unchanged={unchanged}'
    )


if __name__ == '__main__':
    main()
