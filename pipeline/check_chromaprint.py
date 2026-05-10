#!/usr/bin/env python3
import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

AUDIO_EXTS = {'.flac', '.mp3', '.ogg', '.m4a', '.aac', '.wav', '.opus', '.wma', '.aiff', '.alac'}

WORKER_CODE = r'''
import sys
import acoustid
path = sys.argv[1]
try:
    acoustid.fingerprint_file(path, maxlength=0)
except Exception:
    sys.exit(1)
sys.exit(0)
'''


def iter_audio_files(root: Path) -> Iterable[Path]:
    for path in root.rglob('*'):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            yield path


def album_dir_for_file(base_dir: Path, audio_file: Path) -> Path:
    rel = audio_file.relative_to(base_dir)
    if len(rel.parts) >= 2:
        return base_dir / rel.parts[0]
    return base_dir


def run_fingerprint_check(path: Path, timeout: int) -> int:
    proc = subprocess.run(
        [sys.executable, '-c', WORKER_CODE, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    return proc.returncode


def unique_destination(quarantine_dir: Path, name: str) -> Path:
    target = quarantine_dir / name
    if not target.exists():
        return target
    stamp = time.strftime('%Y%m%d-%H%M%S')
    candidate = quarantine_dir / f'{name}__{stamp}'
    idx = 1
    while candidate.exists():
        candidate = quarantine_dir / f'{name}__{stamp}-{idx}'
        idx += 1
    return candidate


def quarantine_album(album_dir: Path, quarantine_dir: Path) -> Path:
    destination = unique_destination(quarantine_dir, album_dir.name)
    shutil.move(str(album_dir), str(destination))
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Check audio files for chromaprint crashers and quarantine bad albums.')
    parser.add_argument('dirs', nargs='+', help='Album or batch directories to scan')
    parser.add_argument('--quarantine-dir', required=True, help='Where bad album directories are moved')
    parser.add_argument('--clean-output', required=True, help='Write clean directories (one per line)')
    parser.add_argument('--workers', type=int, default=max(2, min(8, (os.cpu_count() or 4))), help='Parallel workers')
    parser.add_argument('--timeout', type=int, default=20, help='Seconds per file fingerprint timeout')
    parser.add_argument('--dry-run', action='store_true', help='Do not move directories, only report')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    quarantine_dir = Path(args.quarantine_dir)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    bases = []
    for d in args.dirs:
        p = Path(d)
        if p.exists() and p.is_dir():
            bases.append(p)
        else:
            print(f'[WARN] skipping missing/non-dir path: {p}')

    if not bases:
        Path(args.clean_output).write_text('', encoding='utf-8')
        print('[INFO] no valid input directories')
        return 0

    all_files = []
    for base in bases:
        files = list(iter_audio_files(base))
        all_files.extend((base, f) for f in files)

    if not all_files:
        Path(args.clean_output).write_text('\n'.join(str(b) for b in bases) + '\n', encoding='utf-8')
        print('[INFO] no audio files found; all dirs considered clean')
        return 0

    crashed_albums = set()
    error_files = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(run_fingerprint_check, file_path, args.timeout): (base, file_path)
            for base, file_path in all_files
        }
        for idx, fut in enumerate(concurrent.futures.as_completed(future_map), start=1):
            base, file_path = future_map[fut]
            try:
                rc = fut.result()
            except subprocess.TimeoutExpired:
                rc = 124
            except Exception:
                rc = 125

            if rc not in (0, 1):
                album = album_dir_for_file(base, file_path)
                crashed_albums.add(album)
                print(f'[CRASH] rc={rc} file={file_path}')
            elif rc == 1:
                error_files += 1

            if idx % 200 == 0 or idx == len(all_files):
                print(f'[PROGRESS] checked={idx}/{len(all_files)} crashes={len(crashed_albums)} errors={error_files}')

    quarantined = 0
    for album in sorted(crashed_albums):
        if not album.exists():
            continue
        if args.dry_run:
            print(f'[DRYRUN] would quarantine {album}')
            quarantined += 1
            continue
        try:
            dest = quarantine_album(album, quarantine_dir)
            print(f'[QUARANTINE] moved {album} -> {dest}')
            quarantined += 1
        except Exception as exc:
            print(f'[ERROR] failed to quarantine {album}: {exc}')

    clean_dirs = [d for d in bases if d.exists() and d not in crashed_albums]
    Path(args.clean_output).write_text('\n'.join(str(d) for d in clean_dirs) + ('\n' if clean_dirs else ''), encoding='utf-8')

    print(
        '[SUMMARY] '
        f'input_dirs={len(bases)} files={len(all_files)} crash_albums={len(crashed_albums)} '
        f'quarantined={quarantined} clean_dirs={len(clean_dirs)} error_files={error_files}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
