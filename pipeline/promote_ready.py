#!/usr/bin/env python3
import argparse
import fcntl
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import mutagen

from . import config as cfg
from . import db as pipeline_db
from . import musicbrainz as mb

PIPELINE_LOCK_PATH = str(cfg.PIPELINE_LOCK_PATH)


def acquire_pipeline_lock():
    """Serialize against beets-import.sh; both touch the ready/ dir."""
    Path(PIPELINE_LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    fh = open(PIPELINE_LOCK_PATH, 'w')
    deadline = time.time() + 30
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(f'{os.getpid()}\n')
            fh.flush()
            return fh
        except BlockingIOError:
            if time.time() >= deadline:
                raise SystemExit(
                    f'promote-ready: could not acquire {PIPELINE_LOCK_PATH} within 30s; '
                    'beets-import is likely running. Skipping cycle.'
                )
            time.sleep(0.5)

SRC = cfg.COMPLETE_DIR
DST = cfg.READY_DIR
QUARANTINE_INCOMPLETE = cfg.QUARANTINE_INCOMPLETE_DIR
BEETS_DB   = cfg.BEETS_DB
AUDIO_EXTS = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.alac', '.aiff', '.wma'}


def log(msg: str):
    print(msg, flush=True)


# ── Library dedup check ───────────────────────────────────────────────────────

_NOISE_RE = re.compile(
    r'\s*[\[\(](FLAC|MP3|WAV|WEB|CD|Hi[- ]?Res|24[- ]?bit|\d{2,3}kHz'
    r'|Lossless|Remaster(?:ed)?|Deluxe|Expanded|Limited)[^\]\)]*[\]\)]',
    re.IGNORECASE,
)

def _clean(s: str) -> str:
    s = _NOISE_RE.sub('', s)
    return re.sub(r'\s+', ' ', s).strip(' -_.')

def _parse_artist_album(folder_name: str) -> tuple[str, str]:
    name = _clean(folder_name)
    if ' - ' in name:
        a, b = name.split(' - ', 1)
        if not re.fullmatch(r'\d{4}', a.strip()):
            return a.strip(), b.strip()
    return '', name

def library_track_count(artist: str, album: str) -> int:
    if not artist and not album:
        return 0
    try:
        con = sqlite3.connect(BEETS_DB, timeout=5)
        conds, params = [], []
        if artist:
            conds.append('(LOWER(albumartist) LIKE ? OR LOWER(artist) LIKE ?)')
            params += [f'%{artist.lower()}%', f'%{artist.lower()}%']
        if album:
            conds.append('LOWER(album) LIKE ?')
            params.append(f'%{album.lower()}%')
        where = ' AND '.join(conds)
        rows = con.execute(f'SELECT path FROM items WHERE {where}', params).fetchall()
        con.close()
        return sum(
            1 for (p,) in rows
            if (p.decode() if isinstance(p, bytes) else p) and
               Path(p.decode() if isinstance(p, bytes) else p).exists()
        )
    except Exception:
        return 0


# ── Tag parsing helpers ───────────────────────────────────────────────────────

def parse_int_first(v):
    s = str(v or '').strip()
    if s and s[0].isalpha() and len(s) > 1 and s[1:].lstrip().isdigit():
        s = s[1:].lstrip()
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else 0

def get_first(tags, keys):
    for k in keys:
        try:
            if k in tags and tags[k]:
                v = tags[k]
                return str(v[0]) if isinstance(v, list) else str(v)
        except Exception:
            continue
    return ''

def track_from_filename(path: Path) -> int:
    stem = path.stem
    m = re.match(r"^\s*(\d{1,2})[\s._-]+", stem)
    if m:
        return int(m.group(1))
    m = re.match(r"^\s*[A-Za-z]\s*(\d{1,2})[\s._-]+", stem)
    return int(m.group(1)) if m else 0

def folder_audio_files(folder: Path):
    return [
        p for p in folder.rglob('*')
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
        and not p.name.startswith('._') and not p.name.startswith('.')
    ]

def is_settled(folder: Path, min_age_minutes: int) -> bool:
    cutoff = time.time() - (min_age_minutes * 60)
    for p in folder.rglob('*'):
        if p.is_file() and p.stat().st_mtime >= cutoff:
            return False
    return True

def group_by_disc(files, folder):
    subdir_files = {}
    root_files = []
    for f in files:
        try:
            rel = f.relative_to(folder)
        except ValueError:
            root_files.append(f)
            continue
        if len(rel.parts) > 1:
            subdir_files.setdefault(rel.parts[0], []).append(f)
        else:
            root_files.append(f)
    if subdir_files and not root_files:
        return list(subdir_files.values())
    return [files]

def check_group_incomplete(files, skip_tag_total=False):
    nums = set()
    totals = []
    artists = []
    albums = []
    for p in files:
        trk = 0
        try:
            mf = mutagen.File(str(p), easy=False)
            if mf:
                t = mf.tags or {}
                trk = parse_int_first(get_first(t, ['tracknumber', 'TRCK', 'trkn']))
                tot = parse_int_first(get_first(t, ['tracktotal', 'TOTALTRACKS']))
                if tot == 0:
                    trks = get_first(t, ['tracknumber', 'TRCK'])
                    m = re.search(r'\d+\s*/\s*(\d+)', trks)
                    if m:
                        tot = int(m.group(1))
                if tot > 0:
                    totals.append(tot)
                a = get_first(t, ['albumartist', 'ALBUMARTIST', 'TPE2',
                                  'artist', 'ARTIST', 'TPE1'])
                al = get_first(t, ['album', 'ALBUM', 'TALB'])
                if a:
                    artists.append(a.strip())
                if al:
                    albums.append(al.strip())
        except Exception:
            pass
        if trk == 0:
            trk = track_from_filename(p)
        if trk > 0:
            nums.add(trk)

    if not nums:
        return False, 'no-tracknums'

    if totals and not skip_tag_total:
        counts = Counter(totals)
        mode_total = counts.most_common(1)[0][0]
        if mode_total <= 30 or mode_total <= len(files) * 3:
            # If we have at least as many files as the tag-total claims,
            # all tracks are present (bonus tracks may renumber 1-N).
            # Only flag missing tracks when files < mode_total.
            if len(nums) < mode_total and len(files) < mode_total:
                missing = [n for n in range(1, mode_total + 1) if n not in nums]
                return True, f'tag-total present={len(nums)}/{mode_total} missing={missing[:12]}'
        else:
            log(f'[PROMOTE-WARN] tag-total={mode_total} is suspicious '
                f'(files={len(files)}), ignoring (possible disc-counted tag)')

    maxn = max(nums)
    if maxn >= 3:
        gaps = [n for n in range(1, maxn + 1) if n not in nums]
        if gaps and (len(nums) / maxn) < 0.9:
            return True, f'gap-heuristic present={len(nums)}/{maxn} missing={gaps[:12]}'

    if len(files) <= 2 and maxn > len(files):
        return True, f'sparse-tracks track={maxn} files={len(files)}'

    # MusicBrainz fallback: contiguous tracks but no tag-total → ask MB how
    # many tracks the release should have. Catches "side A only" rips where
    # filenames are sequential 01-04 with no missing-number signal.
    if not totals and not skip_tag_total and 3 <= len(files) < 30 and artists and albums:
        artist = Counter(artists).most_common(1)[0][0]
        album = Counter(albums).most_common(1)[0][0]
        try:
            mb_total = mb.lookup_track_count(artist, album)
        except Exception as e:
            log(f'[PROMOTE-WARN] mb lookup failed for {artist!r}/{album!r}: {e}')
            mb_total = None
        if mb_total and mb_total > len(files):
            return True, (f'mb-lookup artist={artist!r} album={album!r} '
                          f'present={len(files)}/{mb_total}')

    return False, 'ok'

def album_looks_incomplete(files, folder):
    groups = group_by_disc(files, folder)
    multi_disc = len(groups) > 1
    for i, group in enumerate(groups):
        incomplete, reason = check_group_incomplete(group, skip_tag_total=multi_disc)
        if incomplete:
            label = f' (disc {i + 1})' if multi_disc else ''
            return True, reason + label
    return False, 'ok'

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--min-age-minutes', type=int, default=10)
    ap.add_argument('--max-hold-hours', type=int, default=48,
                    help='Hours before a persistently incomplete folder is escalated to quarantine')
    args = ap.parse_args()

    pipeline_db.init_db()
    _lock = acquire_pipeline_lock()  # noqa: F841 — held for process lifetime

    SRC.mkdir(parents=True, exist_ok=True)
    DST.mkdir(parents=True, exist_ok=True)
    QUARANTINE_INCOMPLETE.mkdir(parents=True, exist_ok=True)

    hold_state = pipeline_db.get_held_folders()
    now = time.time()
    max_hold_seconds = args.max_hold_hours * 3600

    promoted = skipped_unsettled = skipped_incomplete = skipped_empty = escalated = 0
    current_folders = set()

    for folder in sorted([p for p in SRC.iterdir() if p.is_dir()]):
        if not is_settled(folder, args.min_age_minutes):
            skipped_unsettled += 1
            continue

        files = folder_audio_files(folder)
        if not files:
            skipped_empty += 1
            continue

        incomplete, reason = album_looks_incomplete(files, folder)
        if incomplete:
            current_folders.add(folder.name)
            prev = hold_state.get(folder.name, {})
            if isinstance(prev, (int, float)):
                prev = {'first_seen': float(prev)}
            first_seen = prev.get('first_seen', now)

            pipeline_db.upsert_held_folder(folder.name, first_seen, reason, len(files))

            hold_age_seconds = now - first_seen
            hold_age_hours   = hold_age_seconds / 3600

            if hold_age_seconds >= max_hold_seconds:
                reason_file = folder / '_HOLD_REASON.txt'
                try:
                    reason_file.write_text(
                        f"Escalated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"Hold duration: {hold_age_hours:.1f}h\n"
                        f"Reason: {reason}\n"
                        f"Files present: {len(files)}\n",
                        encoding='utf-8',
                    )
                except Exception as e:
                    log(f"[WARN] could not write hold reason file: {e}")
                target = unique_target(QUARANTINE_INCOMPLETE, folder.name)
                shutil.move(str(folder), str(target))
                pipeline_db.delete_held_folder(folder.name)
                current_folders.discard(folder.name)
                escalated += 1
                log(f'[ESCALATE] {folder.name} held {hold_age_hours:.1f}h -> {target} reason={reason}')
                pipeline_db.push_notification('escalated', folder.name,
                                              reason=reason, hold_hours=round(hold_age_hours, 1))
            else:
                skipped_incomplete += 1
                log(f'[HOLD] {folder.name} age={hold_age_hours:.1f}h/{args.max_hold_hours}h '
                    f'files={len(files)} reason={reason}')
            continue

        # Folder looks complete — remove from hold state if it was tracked
        pipeline_db.delete_held_folder(folder.name)

        # ── Dedup check: notify if this album is already in library ──────────
        artist, album = _parse_artist_album(folder.name)
        lib_count = library_track_count(artist, album)
        if lib_count > 0:
            log(f'[DEDUP-WARN] {folder.name} — library already has {lib_count} track(s) for '
                f'"{artist} - {album}"; promoting anyway, beets will merge')
            pipeline_db.push_notification('dedup_detected', folder.name,
                                          lib_tracks=lib_count, artist=artist, album=album)

        target = unique_target(DST, folder.name)
        shutil.move(str(folder), str(target))
        promoted += 1
        log(f'[PROMOTE] {folder} -> {target}')
        pipeline_db.push_notification('promoted', folder.name, files=len(files))

    # Prune stale hold-state entries
    stale = pipeline_db.prune_held_folders(current_folders)
    for k in stale:
        log(f'[HOLD-STATE] pruning stale entry: {k}')

    log(
        f'[SUMMARY] promoted={promoted} '
        f'escalated={escalated} '
        f'skipped_unsettled={skipped_unsettled} '
        f'skipped_incomplete={skipped_incomplete} '
        f'skipped_empty={skipped_empty}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
