#!/usr/bin/env python3
"""
slskd-quarantine-requeue.py — Periodic re-queue attempt for quarantined albums.

Scans the top-level quarantine dirs and the quarantine/incomplete/ subdir.
For each album folder:
  1. If the album is already in the beets library -> move the quarantine copy
     into the reconcile INBOX and notify: the gate decides its fate (DUPLICATE
     -> reversible graveyard, better-quality -> UPGRADE park). Nothing here
     hard-deletes — the gate is the one writer AND the one discarder.
  2. If not in library and cooldown has passed -> search slskd and re-queue
     the best result. Cooldown is 7 days between retries; 14 days after a
     successful queue (to let the download complete).
  3. If 0 audio files -> skip silently.

Identity: artist/album come from the files' EMBEDDED TAGS when readable
(reconcile.scan_folder_to_albumrow — modal tags + validated MBIDs), with the
cleaned folder name as fallback only. Folder names lie (2026-07-02: a mangled
name made both this module's old LIKE matcher and the admission gate miss a
library album, re-downloading it in full); tags mostly don't.

State persisted in pipeline SQLite DB. Designed to run weekly (or on-demand).
"""

import os
import re
import sys
import time
from pathlib import Path

from . import config as cfg
from . import db as pipeline_db
from . import identity
from . import recover
from . import reconcile
from . import slskdq

# ── Config ────────────────────────────────────────────────────────────────────

QUARANTINE_ROOT    = cfg.QUARANTINE_DIR
QUARANTINE_SUBDIRS = ['incomplete']
SKIP_SUBDIRS       = {'unparsed'}
BEETS_DB           = cfg.BEETS_DB
LOG_FILE           = cfg.QUARANTINE_REQUEUE_LOG

RETRY_COOLDOWN_H   = 168    # 7 days between search retries
QUEUED_COOLDOWN_H  = 336    # 14 days cooldown after a successful queue
MAX_PENDING_DL     = cfg.FILL_MAX_PENDING_DL
# consecutive fruitless cycles (no results / no quality / gate-refused) before
# an item stops being retried forever and retires to unparsed/ for a human.
# At the weekly cadence the default is ~6 weeks of failures.
DEAD_LETTER_AFTER  = int(os.environ.get('QUARANTINE_DEAD_LETTER_AFTER', '6'))

AUDIO_EXTS = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus',
              '.wav', '.alac', '.aiff', '.wma', '.ape', '.wv'}

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

# ── Beets library check (identity oracle — the same brain as the gate) ───────

_LIB_CACHE = None

def _library_rows():
    global _LIB_CACHE
    if _LIB_CACHE is None:
        _LIB_CACHE = identity.load_albums(BEETS_DB)
    return _LIB_CACHE


def folder_candidate(folder: Path, artist: str, album: str):
    """AlbumRow for this quarantine folder: embedded tags first (modal
    artist/album + validated MBIDs via reconcile's scanner), cleaned folder
    name only as fallback. Sentinel id -1 (library ids are positive)."""
    try:
        row, _meta = reconcile.scan_folder_to_albumrow(str(folder), sentinel_id=-1)
    except Exception:
        row = None
    if row is not None and (row.album or '').strip():
        return row
    return identity.AlbumRow(-1, artist or '', album or '', None, '', '', [])


def in_library(row) -> bool:
    """True when the library already contains this album — decided by
    build_index(library + candidate), the identical convergence reconcile and
    the admission gate apply. Replaces the old substring-LIKE + folder-scan
    matcher, whose false negatives re-acquired owned albums and whose false
    positives gated a hard delete."""
    idx = identity.build_index(_library_rows() + [row])
    by_album = idx['by_album']
    key = by_album.get(row.album_id)
    return any(aid != row.album_id and k == key for aid, k in by_album.items())

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
# our own re-queue marker (incomplete_watchdog renames stalled folders); left
# in the album name it forks the identity key -> the same release gets queued
# twice (2026-07-02: proven, one remote folder downloaded twice)
_REQUEUED_RE    = re.compile(r'\.requeued-\d{8}-\d{4,6}')
# "artists" that are really structure tokens: a date fragment ("05-21", left
# over from a YYYY-MM-DD prefix) or a disc marker ("DISC 3", "CD2"). Searching
# slskd with these is noise; better no artist (and let the generic-query
# guard refuse) than a wrong one.
_PSEUDO_ARTIST_RE = re.compile(r'(?i)^(\d{1,2}[-–]\d{1,2}|(disc|disk|cd|vol(ume)?)\s*\.?\s*\d+)$')

def clean_name(s: str) -> str:
    s = _REQUEUED_RE.sub('', s)
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
        artist = artist.strip()
        if not re.fullmatch(r'\d{4}', artist) and not _PSEUDO_ARTIST_RE.fullmatch(artist):
            # "Artist - 2022 - Album": the year survives the split as an album
            # prefix and forks the identity key
            return artist, _YEAR_PREFIX_RE.sub('', album.strip()).strip()
    return '', name

def audio_count(folder: Path) -> int:
    return sum(
        1 for p in folder.rglob('*')
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith('.')
    )

# ── slskd recover loader ──────────────────────────────────────────────────────

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

# ── Dead-letter ───────────────────────────────────────────────────────────────

def dead_letter(folder: Path, key: str, count: int) -> bool:
    """Retire a terminally-fruitless item: move it to unparsed/ (which
    collect_folders never scans) and notify ONCE. Without this, an item the
    gate refuses forever is re-evaluated and re-refused weekly, invisibly,
    forever. Returns True when the folder was moved."""
    dest_dir = QUARANTINE_ROOT / 'unparsed'
    dest = dest_dir / folder.name
    if dest.exists():
        dest = dest_dir / f'{folder.name}.dead-{int(time.time())}'
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.rename(str(folder), str(dest))
    except OSError as e:
        log(f'[WARN] dead-letter move failed for {folder}: {e}')
        return False
    pipeline_db.delete_quarantine_state(key)
    pipeline_db.push_notification('quarantine_dead_letter', key,
                                  attempts=count, moved_to=str(dest))
    log(f'[DEAD-LETTER] {key} — {count} fruitless cycles, retired to unparsed/ '
        f'(needs a human)')
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_logging()
    pipeline_db.init_db()
    log('===== slskd-quarantine-requeue start =====')

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
        # tags override the folder-name parse when readable — folder names lie
        cand = folder_candidate(folder, artist, album)
        if cand.album_id == -1 and (cand.albumartist or cand.album) and \
                (cand.albumartist, cand.album) != (artist, album):
            artist, album = (cand.albumartist or artist), (cand.album or album)
        log(f'[CHECK] {key} | artist="{artist}" album="{album}" | files={n_audio}')

        # ── Already in library? ───────────────────────────────────────────────
        if in_library(cand):
            log(f'[IN-LIBRARY] {key} — routing quarantine copy to the gate '
                f'(reconcile decides: duplicate-discard or upgrade-park)')
            dest = Path(str(cfg.INBOX_DIR)) / folder.name
            if dest.exists():
                dest = dest.with_name(f'{folder.name}.fromquar-{int(now)}')
            try:
                os.rename(str(folder), str(dest))   # same fs (both under slskd/)
                pipeline_db.delete_quarantine_state(key)
            except OSError as e:
                log(f'[WARN] could not move {folder} to inbox: {e}')
            already_in_lib += 1
            pipeline_db.push_notification('quarantine_cleared', key,
                                          reason='in_library_routed_to_gate')
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
        fruitless = int(entry.get('fruitless', 0))

        def _fruitless_cycle():
            """One more cycle that can never succeed on its own; at the
            threshold the item retires to unparsed/ instead of retrying
            weekly forever."""
            n = fruitless + 1
            if n >= DEAD_LETTER_AFTER and dead_letter(folder, key, n):
                return
            pipeline_db.upsert_quarantine_state(key, new_last_attempt, last_queued,
                                                fruitless=n)

        try:
            pending = recover.pending_download_count()
            if pending >= MAX_PENDING_DL:
                log(f'[SEARCH-SKIP] queue busy ({pending}), skipping {key}')
                pipeline_db.upsert_quarantine_state(key, new_last_attempt, last_queued)
                continue

            responses = recover.slskd_search(query)
            if not responses:
                log(f'[NO-RESULTS] {key}')
                no_results += 1
                pipeline_db.push_notification('quarantine_no_results', key, query=query)
                _fruitless_cycle()
                continue

            best = recover.find_best_folder(responses, query=query)
            if best is None:
                log(f'[NO-QUALITY] {key} — results found but none passed quality filters')
                no_results += 1
                pipeline_db.push_notification('quarantine_no_results', key, query=query)
                _fruitless_cycle()
                continue

            # Phase-6 gate: route through the slskd in-flight ledger. quarantine
            # keeps its own 72h re-queue cooldown, so skip the ledger's cooldown
            # (skip_cooldown) but honor in-library / in-flight / capacity. A
            # refusal is not an error.
            d = slskdq.enqueue(artist, album, source='quarantine',
                               post=lambda: recover.queue_download(best),
                               username=best.username, remote_dir=best.directory,
                               file_count=best.file_count, skip_cooldown=True)
            if d.admitted:
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
            elif d.state == slskdq.POST_FAILED:
                errors += 1
                log(f'[QUEUE-FAIL] {key}', 'WARN')
                pipeline_db.upsert_quarantine_state(key, new_last_attempt, last_queued)
            elif d.state == slskdq.REFUSED_GENERIC:
                # can never self-resolve: the query will be exactly as generic
                # next week
                log(f'[LEDGER] skip {key} — {d.reason}')
                _fruitless_cycle()
            else:
                log(f'[LEDGER] skip {key} — {d.reason}')
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
