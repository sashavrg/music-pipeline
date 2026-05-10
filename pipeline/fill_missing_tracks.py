#!/usr/bin/env python3
"""
slskd-fill-missing-tracks.py — Targeted re-download of missing tracks in held folders.

Reads the promote-ready hold state to find exact missing track numbers.
For each held folder:
  1. Parse missing track numbers from the hold reason.
  2. Search slskd for the album.
  3. Score ALL result folders (multi-source) using the quality scorer.
  4. For each missing track, pick the best available source.
  5. Queue missing tracks from up to top-5 sources in a single pass.

Multi-source fill: if peer A has tracks 14-15 and peer B has tracks 16-23,
both are queued in the same cycle rather than waiting 12h per source.
"""

import json
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from dataclasses import replace as dc_replace
from pathlib import Path

from . import config as cfg
from . import db as pipeline_db
from . import recover

# ── Config ────────────────────────────────────────────────────────────────────

LOG_FILE        = cfg.FILL_MISSING_LOG
# Cooldown per folder after at least one successful queue attempt in this cycle.
QUEUE_COOLDOWN_H = cfg.FILL_QUEUE_COOLDOWN_H

# Minimum hold age before we try to fill — give slskd time to finish on its own.
MIN_HOLD_AGE_H   = cfg.FILL_MIN_HOLD_AGE_H

MAX_PENDING_DL   = cfg.FILL_MAX_PENDING_DL
MAX_SOURCES      = cfg.FILL_MAX_SOURCES   # try at most this many peers per cycle

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

# ── Missing track extraction ──────────────────────────────────────────────────

def parse_missing_tracks(reason: str) -> list[int]:
    m = re.search(r'missing=\[([^\]]+)\]', reason)
    if not m:
        return []
    try:
        return [int(x.strip()) for x in m.group(1).split(',') if x.strip().isdigit()]
    except Exception:
        return []

def parse_present_count(reason: str) -> tuple[int, int]:
    m = re.search(r'present=(\d+)/(\d+)', reason)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0

# ── Folder name → query ───────────────────────────────────────────────────────

_NOISE_RE = re.compile(
    r'\s*[\[\(](FLAC|MP3|WAV|WEB|CD|Hi[- ]?Res|24[- ]?bit|\d{2,3}kHz'
    r'|Lossless|Remaster(?:ed)?|Deluxe|Expanded|Limited)[^\]\)]*[\]\)]',
    re.IGNORECASE,
)
_YEAR_PREFIX_RE  = re.compile(r'^\d{4}[-\s,]+')
_CURLY_RE        = re.compile(r'\s*\{[^}]*\}')

def clean_name(s: str) -> str:
    s = _NOISE_RE.sub('', s)
    s = _YEAR_PREFIX_RE.sub('', s)
    s = _CURLY_RE.sub('', s)
    return re.sub(r'\s+', ' ', s).strip(' -_.')

def folder_to_query(name: str) -> str:
    clean = clean_name(name)
    if ' - ' in clean:
        parts = clean.split(' - ', 1)
        if not re.fullmatch(r'\d{4}', parts[0].strip()):
            clean = ' '.join(parts)
    q = clean.strip()
    if len(q) > 60:
        q = q[:60].rsplit(' ', 1)[0]
    return q

# ── Track-number extraction from slskd filenames ──────────────────────────────

def track_num_from_filename(filename: str) -> int:
    basename = re.split(r'[/\\]', filename)[-1]
    stem = re.sub(r'\.[^.]+$', '', basename)
    m = re.match(r'^\s*(\d{1,3})[\s._-]', stem)
    return int(m.group(1)) if m else 0

# ── recover module loader ─────────────────────────────────────────────────────

# ── Multi-source fill logic ───────────────────────────────────────────────────

def assign_tracks_to_sources(all_folders: list, missing_set: set[int]) -> list:
    """
    For each missing track number, find the highest-scored source that has it.
    Returns [(folder_result, [file_dict, ...]), ...] in score order.
    """
    remaining = set(missing_set)
    source_files: list = []   # [(folder_result, [file_dict])]

    for folder in all_folders[:MAX_SOURCES]:
        if not remaining:
            break
        matched = []
        for f in folder.files:
            trk = track_num_from_filename(f.get('filename', ''))
            if trk in remaining:
                matched.append(f)
                remaining.discard(trk)
        if matched:
            source_files.append((folder, matched))

    return source_files


def fallback_assign(all_folders: list) -> list:
    """When filenames lack track numbers, use the full top-scored folder."""
    if not all_folders:
        return []
    best = all_folders[0]
    return [(best, best.files)]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    setup_logging()
    pipeline_db.init_db()
    log('===== slskd-fill-missing-tracks start =====')

    hold_state = pipeline_db.get_held_folders()
    fill_state = pipeline_db.get_fill_state()
    now        = time.time()

    if not hold_state:
        log('Hold state empty — nothing to do')
        return 0

    queued = skipped_cooldown = skipped_young = skipped_no_tracks = no_results = errors = 0

    for folder_name, entry in sorted(hold_state.items()):
        if not isinstance(entry, dict):
            log(f'[SKIP] {folder_name} — legacy hold-state format')
            skipped_no_tracks += 1
            continue

        reason        = entry.get('reason', '')
        first_seen    = entry.get('first_seen', now)
        hold_age_h    = (now - first_seen) / 3600
        files_present = entry.get('files_present', 0)

        if hold_age_h < MIN_HOLD_AGE_H:
            log(f'[YOUNG] {folder_name} only held {hold_age_h:.1f}h — waiting')
            skipped_young += 1
            continue

        missing = parse_missing_tracks(reason)
        present, total = parse_present_count(reason)

        if not missing:
            log(f'[SKIP-NO-TRACKS] {folder_name} reason="{reason}" — cannot extract missing track numbers')
            skipped_no_tracks += 1
            continue

        log(f'[FILL] {folder_name} | held={hold_age_h:.1f}h | present={present}/{total} | missing={missing}')

        # ── Cooldown: skip if we queued something recently ────────────────────
        fill_entry  = fill_state.get(folder_name, {})
        last_queued = fill_entry.get('last_queued', 0)
        elapsed_h   = (now - last_queued) / 3600

        if last_queued and elapsed_h < QUEUE_COOLDOWN_H:
            log(f'[COOLDOWN] {folder_name} — {elapsed_h:.0f}h since last queue, need {QUEUE_COOLDOWN_H}h')
            skipped_cooldown += 1
            continue

        # ── Queue busy? ───────────────────────────────────────────────────────
        pending = recover.pending_download_count()
        if pending >= MAX_PENDING_DL:
            log(f'[BUSY] queue has {pending} pending, skipping {folder_name}')
            continue

        # ── Search ────────────────────────────────────────────────────────────
        query = folder_to_query(folder_name)
        log(f'[SEARCH] query="{query}" for {len(missing)} missing track(s): {missing}')

        try:
            responses = recover.slskd_search(query)
        except Exception as e:
            log(f'[ERROR] search failed for {folder_name}: {e}', 'ERROR')
            errors += 1
            continue

        if not responses:
            log(f'[NO-RESULTS] {folder_name}')
            no_results += 1
            pipeline_db.push_notification('fill_no_results', folder_name,
                                          query=query, missing=missing)
            continue

        # ── Get ALL qualifying folders (multi-source) ─────────────────────────
        all_folders = recover.find_all_folders(responses)

        if not all_folders:
            log(f'[NO-QUALITY] {folder_name} — results found but none passed quality filters')
            no_results += 1
            pipeline_db.push_notification('fill_no_results', folder_name,
                                          query=query, missing=missing)
            continue

        # ── Assign each missing track to the best source that has it ──────────
        missing_set = set(missing)
        source_assignments = assign_tracks_to_sources(all_folders, missing_set)

        if not source_assignments:
            # Filenames lack track numbers — fall back to full-folder from best source
            log(f'[FALLBACK] {folder_name} — no track-number matches, queuing all files from best source')
            source_assignments = fallback_assign(all_folders)

        # ── Queue from each source ─────────────────────────────────────────────
        cycle_queued_tracks: list[int] = []
        cycle_queued_files              = 0
        best_source                     = None
        any_ok                          = False

        for source_folder, targeted_files in source_assignments:
            found_tracks = sorted(
                {track_num_from_filename(f.get('filename', '')) for f in targeted_files} - {0}
            )
            speed_mb = source_folder.upload_speed / 1_000_000
            log(
                f'[TARGET] {folder_name} | '
                f'user={source_folder.username} fmt={source_folder.fmt} '
                f'score={source_folder.score} | '
                f'queuing {len(targeted_files)} file(s) for tracks {found_tracks}'
            )
            try:
                partial = dc_replace(source_folder, files=targeted_files,
                                     file_count=len(targeted_files))
                ok = recover.queue_download(partial)
            except Exception as e:
                log(f'[ERROR] queue failed for {source_folder.username}: {e}', 'ERROR')
                ok = False

            if ok:
                log(
                    f'[QUEUED] {folder_name} | '
                    f'{len(targeted_files)} file(s) from {source_folder.username} '
                    f'({source_folder.fmt.upper()}, score={source_folder.score}, {speed_mb:.1f}MB/s)'
                )
                cycle_queued_tracks.extend(found_tracks)
                cycle_queued_files += len(targeted_files)
                any_ok = True
                if best_source is None:
                    best_source = source_folder
                pipeline_db.push_notification(
                    'fill_queued', folder_name,
                    tracks=found_tracks, files=len(targeted_files),
                    user=source_folder.username, fmt=source_folder.fmt.upper(),
                    score=source_folder.score, speed_mb=round(speed_mb, 1),
                    missing_total=len(missing),
                )
            else:
                errors += 1
                log(f'[QUEUE-FAIL] {folder_name} from {source_folder.username}', 'WARN')

        if any_ok:
            queued += 1
            pipeline_db.upsert_fill_attempt(
                folder_name,
                last_queued=now,
                queued_tracks=sorted(cycle_queued_tracks),
                queued_files=cycle_queued_files,
                user=best_source.username if best_source else '',
                fmt=best_source.fmt if best_source else '',
                score=best_source.score if best_source else 0,
            )

        time.sleep(12)

    log(
        f'[SUMMARY] queued={queued} '
        f'cooldown={skipped_cooldown} '
        f'young={skipped_young} '
        f'no_tracks={skipped_no_tracks} '
        f'no_results={no_results} '
        f'errors={errors}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
