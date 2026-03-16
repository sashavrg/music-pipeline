#!/usr/bin/env python3
import argparse
import re
import shutil
import time
from pathlib import Path

import mutagen

SRC = Path('/mnt/scratch/slskd/complete')
DST = Path('/mnt/scratch/slskd/ready')
AUDIO_EXTS = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.alac', '.aiff', '.wma'}


def log(msg: str):
    print(msg, flush=True)


def parse_int_first(v):
    m = re.search(r'(\d+)', str(v or ''))
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
    return int(m.group(1)) if m else 0


def folder_audio_files(folder: Path):
    return [
        p for p in folder.rglob('*')
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith('._') and not p.name.startswith('.')
    ]


def is_settled(folder: Path, min_age_minutes: int) -> bool:
    cutoff = time.time() - (min_age_minutes * 60)
    for p in folder.rglob('*'):
        if p.is_file() and p.stat().st_mtime >= cutoff:
            return False
    return True


def album_looks_incomplete(files):
    nums = set()
    totals = []
    parseable = 0

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
        except Exception:
            pass

        if trk == 0:
            trk = track_from_filename(p)

        if trk > 0:
            nums.add(trk)
            parseable += 1

    if len(nums) < 3:
        return False, 'insufficient-tracknums'

    expected = max(totals) if totals else 0
    if expected > 0 and len(nums) < expected:
        missing = [n for n in range(1, expected + 1) if n not in nums]
        return True, f'tag-total present={len(nums)}/{expected} missing={missing[:12]}'

    maxn = max(nums)
    gaps = [n for n in range(1, maxn + 1) if n not in nums]
    if maxn >= 6 and gaps and (len(nums) / maxn) < 0.9:
        return True, f'gap-heuristic present={len(nums)}/{maxn} missing={gaps[:12]}'

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
    args = ap.parse_args()

    SRC.mkdir(parents=True, exist_ok=True)
    DST.mkdir(parents=True, exist_ok=True)

    promoted = 0
    skipped_unsettled = 0
    skipped_incomplete = 0
    skipped_empty = 0

    for folder in sorted([p for p in SRC.iterdir() if p.is_dir()]):
        if not is_settled(folder, args.min_age_minutes):
            skipped_unsettled += 1
            continue

        files = folder_audio_files(folder)
        if not files:
            skipped_empty += 1
            continue

        incomplete, reason = album_looks_incomplete(files)
        if incomplete:
            skipped_incomplete += 1
            log(f'[HOLD] {folder} reason={reason}')
            continue

        target = unique_target(DST, folder.name)
        shutil.move(str(folder), str(target))
        promoted += 1
        log(f'[PROMOTE] {folder} -> {target}')

    log(
        f'[SUMMARY] promoted={promoted} '
        f'skipped_unsettled={skipped_unsettled} '
        f'skipped_incomplete={skipped_incomplete} '
        f'skipped_empty={skipped_empty}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
