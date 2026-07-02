#!/usr/bin/env python3
"""reconcile.py — the ONE GATE / ONE WRITER for the music library.

Sole caller of `beet import` / `beet remove`; the only code permitted to mutate
the beets library. Turns a candidate (a disk folder in cfg.INBOX_DIR / the frozen
backlog, OR an in-library album_id from pipeline_dup_plan.json) into a correct
library state with duplicates made *mathematically impossible* (identity-keyed,
not discipline-dependent) and every destructive op reversible via a same-filesystem
dated graveyard.

DRY-RUN IS DEFAULT. --execute is required to write anything.

Design: see memory music-pipeline-rethink (phase 4) and the synthesized blueprint
(/root/reconcile_design_result.json). Built against identity.py (the validated
3-tier resolver) and grounded in live-verified facts:
  - beets 2.7.1, ffprobe n8.1; library + graveyard on st_dev 67, scratch on 2112.
  - NO sqlite_sequence -> rowid reuse possible -> never capture new album_id by
    max-id delta; capture by items.path LIKE <dest-prefix>.
  - identity.load_albums decodes items.path with errors='replace' (LOSSY) -> for
    any FILE operation reconcile reads items.path BLOB as RAW BYTES itself.
  - resolve a candidate's key by build_index(library + [candidate]) so it is keyed
    by the IDENTICAL algorithm as library rows (convergence/disc-suffix/ch-body).

This module is import-safe (no side effects at import); all action is under main().
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import config as cfg          # noqa: E402
from pipeline import identity               # noqa: E402

# ─── Paths (live-verified devices) ───────────────────────────────────────────
BEETS_DB = cfg.BEETS_DB
LIBRARY_ROOT = Path("/mnt/storage/share/media/music/music")     # beets `directory`
LIB_GRAVEYARD = Path("/mnt/storage/share/media/music/_graveyard")   # dev 67 (== library)
INBOUND_GRAVEYARD = Path(str(cfg.SCRATCH_ROOT)) / "_graveyard"      # dev 2112 (== inbox/scratch)
PARK_DIR = Path(str(cfg.SCRATCH_ROOT)) / "_parked"                  # dev 2112
RECON_DURABLE = Path("/mnt/storage/share/media/music/_reconcile")  # journal/sentinel/ledger (reboot-survivable, NOT tmpfs)
PLAN_ROOT = Path(str(cfg.LOG_DIR)) / "reconcile"                   # plans + logs

LOSSLESS_CODECS = {
    'flac', 'alac', 'wav', 'pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_s16be',
    'pcm_s24be', 'aiff', 'ape', 'wavpack', 'tak', 'tta', 'truehd', 'mlp', 'pcm_f32le',
}
DISC_DIR_RE = re.compile(r'^(disc|cd|disk|disque)\s*0*(\d+)$', re.I)
_COLLAB_RE = re.compile(r'\s+(?:&|feat\.?|featuring|vs\.?|with|/|,|\+|x)\s+', re.I)
ROUTES = ('NEW', 'UPGRADE', 'DUPLICATE', 'DUPPLAN_DROP', 'PARK', 'SKIPPED_UNSETTLED')


# ─── Read-only sqlite (raw bytes) ────────────────────────────────────────────
def _ro_conn(db_path: str = BEETS_DB) -> sqlite3.Connection:
    """Immutable read-only connection; never blocks on / mutates the live DB."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    return conn


def db_data_version(db_path: str = BEETS_DB) -> int:
    """PRAGMA data_version — optimistic-concurrency + plan-freshness fingerprint
    (NOT file mtime; beets uses WAL and the main file mtime can lag)."""
    conn = _ro_conn(db_path)
    try:
        return conn.execute("PRAGMA data_version").fetchone()[0]
    finally:
        conn.close()


def read_items_raw(album_id: int, db_path: str = BEETS_DB) -> list:
    """item id + RAW path bytes (not 'replace'-decoded) + disc/track for safe
    file ops + inode/shared-folder checks."""
    conn = _ro_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, path, disc, track, title FROM items WHERE album_id=?",
            (album_id,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        p = r[1]
        if not isinstance(p, (bytes, bytearray)):
            p = os.fsencode(p)
        out.append({'item_id': r[0], 'path': bytes(p), 'disc': r[2], 'track': r[3], 'title': r[4]})
    return out


def album_meta(album_id: int, db_path: str = BEETS_DB) -> Optional[dict]:
    conn = _ro_conn(db_path)
    try:
        r = conn.execute(
            "SELECT id, albumartist, album, year, mb_albumid, mb_releasegroupid "
            "FROM albums WHERE id=?", (album_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    return {'album_id': r[0], 'albumartist': r[1] or '', 'album': r[2] or '',
            'year': r[3], 'mb_albumid': r[4] or '', 'mb_releasegroupid': r[5] or ''}


# ─── ffprobe codec truth (both sides, every run) ─────────────────────────────
def ffprobe_truth(path_bytes: bytes) -> dict:
    """Authoritative per-file audio facts. NEVER trust beets DB columns or the
    file extension (Opus mislabeled .flac with null samplerate/bitdepth lives in
    the DB). Always select the FIRST audio stream (embedded art is a separate
    mjpeg/png stream)."""
    out = {'codec': None, 'lossless': False, 'bitdepth': None, 'samplerate': None,
           'bitrate': None, 'duration': None, 'channels': None, 'ok': False,
           'path': path_bytes}
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=codec_name,codec_type,sample_rate,'
             'bits_per_raw_sample,bits_per_sample,channels',
             '-show_entries', 'format=bit_rate,duration', '-of', 'json',
             os.fsdecode(path_bytes)],
            capture_output=True, timeout=120)
        if proc.returncode != 0:
            return out
        data = json.loads(proc.stdout or b'{}')
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return out
    streams = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
    if not streams:
        return out
    s = streams[0]
    codec = (s.get('codec_name') or '').lower()
    out['codec'] = codec
    out['lossless'] = codec in LOSSLESS_CODECS
    bd = s.get('bits_per_raw_sample') or s.get('bits_per_sample')
    try:
        bd = int(bd) if bd not in (None, '', '0', 0) else None
    except (TypeError, ValueError):
        bd = None
    out['bitdepth'] = bd
    try:
        out['samplerate'] = int(s.get('sample_rate')) if s.get('sample_rate') else None
    except (TypeError, ValueError):
        out['samplerate'] = None
    try:
        out['channels'] = int(s.get('channels')) if s.get('channels') else None
    except (TypeError, ValueError):
        out['channels'] = None
    fmt = data.get('format', {})
    try:
        out['bitrate'] = int(fmt.get('bit_rate')) if fmt.get('bit_rate') else None
    except (TypeError, ValueError):
        out['bitrate'] = None
    try:
        out['duration'] = float(fmt.get('duration')) if fmt.get('duration') else None
    except (TypeError, ValueError):
        out['duration'] = None
    # ok = a real, non-truncated audio stream. A lossless file with null bitdepth
    # OR a duration of 0/None is suspicious -> not ok (vetoes the candidate).
    out['ok'] = bool(out['codec']) and (out['duration'] is None or out['duration'] > 0.5)
    if out['lossless'] and out['samplerate'] in (None, 0):
        out['ok'] = False   # mislabeled-lossless signal
    return out


def album_quality(item_paths: list, cache: dict) -> dict:
    """Aggregate ffprobe_truth over an album's files. Worst-track governs.
    `cache` keyed on (path_bytes, mtime, size)."""
    probes = []
    any_mislabel = False
    for pb in item_paths:
        try:
            st = os.stat(pb)
            key = (pb, int(st.st_mtime), st.st_size)
        except OSError:
            key = (pb, 0, 0)
        if key not in cache:
            cache[key] = ffprobe_truth(pb)
        q = cache[key]
        probes.append(q)
        ext = os.path.splitext(os.fsdecode(pb))[1].lower().lstrip('.')
        ext_lossless = ext in ('flac', 'alac', 'wav', 'aiff', 'ape', 'wv', 'tak', 'tta')
        if (ext_lossless and not q['lossless']) or (q['lossless'] and q['samplerate'] in (None, 0)):
            any_mislabel = True
    valid = [q for q in probes if q['ok']]
    lossless_all = bool(probes) and all(q['lossless'] for q in probes)
    bds = [q['bitdepth'] for q in valid if q['bitdepth']]
    srs = [q['samplerate'] for q in valid if q['samplerate']]
    brs = [q['bitrate'] for q in valid if q['bitrate']]
    return {
        'n_files': len(probes),
        'lossless_all': lossless_all,
        'min_bitdepth': min(bds) if bds else None,
        'min_samplerate': min(srs) if srs else None,
        'min_bitrate': min(brs) if brs else None,
        'codec_set': sorted({q['codec'] for q in probes if q['codec']}),
        'any_mislabel': any_mislabel,
        'any_probe_fail': any(not q['ok'] for q in probes),
        'basis': 'ffprobe-both-sides',
    }


# ─── Quality decision ladder ─────────────────────────────────────────────────
def strictly_better(cand_q: dict, lib_q: dict, cand_is_fragment: bool,
                    cand_trackset: set, lib_trackset: set) -> str:
    """'STRICTLY_BETTER'|'WORSE'|'EQUAL'|'AMBIGUOUS'. Completeness hard-gate first;
    tie-breaks deliberately favor the non-destructive outcome."""
    # (0) WHOLE-ALBUM VETO on candidate corruption/mislabel
    if cand_q['any_probe_fail'] or cand_q['any_mislabel']:
        return 'AMBIGUOUS'
    superset = lib_trackset.issubset(cand_trackset) if lib_trackset else True
    # (1) COMPLETENESS HARD GATE
    if cand_is_fragment and len(lib_trackset) > 1:
        return 'WORSE'
    if not superset:
        # candidate is missing tracks the library has -> can never be strictly better
        if cand_trackset and cand_trackset < lib_trackset:
            return 'WORSE'
        return 'AMBIGUOUS'   # divergent (neither subset) -> needs eyes
    # (2) PROVEN-CORRUPT LIBRARY: good replaces verified-corrupt
    if (lib_q['any_probe_fail'] or lib_q['any_mislabel']) and not cand_q['any_probe_fail'] \
            and not cand_q['any_mislabel']:
        return 'STRICTLY_BETTER'
    # (3) LOSSLESS RULE
    if cand_q['lossless_all'] and not lib_q['lossless_all']:
        return 'STRICTLY_BETTER'
    if lib_q['lossless_all'] and not cand_q['lossless_all']:
        return 'WORSE'
    # (4) both lossless -> compare (bitdepth, samplerate) lexicographically
    if cand_q['lossless_all'] and lib_q['lossless_all']:
        cb, lb = cand_q['min_bitdepth'] or 0, lib_q['min_bitdepth'] or 0
        cs, ls = cand_q['min_samplerate'] or 0, lib_q['min_samplerate'] or 0
        if (cb >= lb and cs >= ls) and (cb > lb or cs > ls):
            return 'STRICTLY_BETTER'
        if cb < lb or cs < ls:
            return 'WORSE'
        # equal res -> fall through to completeness
    else:
        # (5) both lossy
        cbr, lbr = cand_q['min_bitrate'] or 0, lib_q['min_bitrate'] or 0
        if cbr - lbr > 32000 and len(cand_trackset) >= len(lib_trackset):
            return 'STRICTLY_BETTER'
        if lbr - cbr > 32000:
            return 'WORSE'
    # (6) more complete at >= equal codec class
    if cand_trackset > lib_trackset:
        return 'STRICTLY_BETTER'
    # (7) exact equal
    if cand_trackset == lib_trackset:
        return 'EQUAL'
    return 'AMBIGUOUS'


# ─── Candidate scanning (disk folder -> AlbumRow) ────────────────────────────
@dataclass
class ScanMeta:
    folder: str
    n_audio: int = 0
    is_fragment: bool = False
    intra_collision: bool = False
    orphan_disc: bool = False
    synthesized_name: bool = False
    collision_losers: list = field(default_factory=list)   # raw-bytes paths
    untagged: list = field(default_factory=list)
    reason: Optional[str] = None
    item_paths: list = field(default_factory=list)          # kept audio, raw bytes
    disc_dirs: list = field(default_factory=list)


def _tag_first(tags, *keys):
    if tags is None:
        return None
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v:
            if isinstance(v, list):
                v = v[0]
            v = str(v).strip()
            if v:
                return v
    return None


def _year_of(s):
    if not s:
        return None
    m = re.search(r'(19|20)\d{2}', str(s))
    return int(m.group(0)) if m else None


def _int_before_slash(s):
    if s is None:
        return None
    m = re.match(r'\s*(\d+)', str(s))
    return int(m.group(1)) if m else None


def _read_tags(path_bytes: bytes) -> dict:
    """Extract album-relevant tags from one file via mutagen (easy=False so
    mb_releasegroupid is visible). Returns {} on unreadable."""
    try:
        import mutagen
        mf = mutagen.File(os.fsdecode(path_bytes), easy=False)
    except Exception:
        return {}
    if mf is None:
        return {}
    t = mf.tags
    if t is None:
        return {'_len': getattr(getattr(mf, 'info', None), 'length', None)}
    out = {}
    # normalize a flat dict of upper-cased keys -> first value, for vorbis/ID3/MP4
    def g(*keys):
        return _tag_first(t, *keys)
    out['albumartist'] = g('albumartist', 'ALBUMARTIST', 'TPE2', 'aART', '\xa9wrt')
    out['artist'] = g('artist', 'ARTIST', 'TPE1', '\xa9ART')
    out['album'] = g('album', 'ALBUM', 'TALB', '\xa9alb')
    out['date'] = g('date', 'DATE', 'year', 'YEAR', 'TDRC', 'TYER', '\xa9day', 'originaldate')
    out['title'] = g('title', 'TITLE', 'TIT2', '\xa9nam')
    out['disc'] = _int_before_slash(g('discnumber', 'DISCNUMBER', 'TPOS', 'disk'))
    out['track'] = _int_before_slash(g('tracknumber', 'TRACKNUMBER', 'TRCK', 'trkn'))
    out['tracktotal'] = _int_before_slash(g('tracktotal', 'TRACKTOTAL', 'totaltracks'))
    mbid = g('musicbrainz_albumid', 'MUSICBRAINZ_ALBUMID', 'TXXX:MusicBrainz Album Id',
             '----:com.apple.iTunes:MusicBrainz Album Id')
    rg = g('musicbrainz_releasegroupid', 'MUSICBRAINZ_RELEASEGROUPID',
           'TXXX:MusicBrainz Release Group Id',
           '----:com.apple.iTunes:MusicBrainz Release Group Id')
    out['mb_albumid'] = mbid if (mbid and identity._valid(mbid)) else ''
    out['mb_releasegroupid'] = rg if (rg and identity._valid(rg)) else ''
    return out


def _modal(values):
    vals = [v for v in values if v]
    if not vals:
        return ''
    return Counter(vals).most_common(1)[0][0]


def scan_folder_to_albumrow(folder: str, sentinel_id: int):
    """Disk folder -> (identity.AlbumRow|None, ScanMeta). Conservative: any
    structural ambiguity -> AlbumRow with empty album (tier-3) or None + reason."""
    meta = ScanMeta(folder=folder)
    fb = os.fsencode(folder)
    audio = []          # (path_bytes, tags, disc_from_dir)
    disc_dirs = set()
    try:
        # recurse; capture disc-subdir context
        for root, dirs, files in os.walk(fb):
            rootname = os.path.basename(os.fsdecode(root))
            dm = DISC_DIR_RE.match(rootname)
            disc_from_dir = int(dm.group(2)) if dm else None
            if dm:
                disc_dirs.add(disc_from_dir)
            for fn in files:
                pb = os.path.join(root, fn)
                base = os.fsdecode(fn)
                if base.startswith('.') or base.startswith('._'):
                    continue
                ext = os.path.splitext(base)[1].lower()
                if ext not in identity.AUDIO_EXTS:
                    continue
                try:
                    if os.stat(pb).st_size == 0:
                        continue
                except OSError:
                    continue
                audio.append((pb, _read_tags(pb), disc_from_dir))
    except OSError as e:
        meta.reason = f'scan-error:{e}'
        return None, meta

    meta.disc_dirs = sorted(disc_dirs)
    meta.n_audio = len(audio)
    if not audio:
        meta.reason = 'no-audio-files'
        return None, meta

    # lone Disc-N folder with no real album context and a disc-label basename
    base_folder = os.path.basename(os.path.normpath(folder))
    if DISC_DIR_RE.match(base_folder) and not disc_dirs:
        meta.orphan_disc = True
        meta.reason = 'orphan-disc-folder'
        return None, meta

    # INTRA-FOLDER COLLISION DEDUP by (disc,track) slot via ffprobe-best (NEVER
    # filename-suffix regex — '08 - 2.5.flac'/'.1.flac' false-positives).
    cache: dict = {}
    by_slot = defaultdict(list)
    for pb, tags, dfd in audio:
        disc = tags.get('disc') or dfd or 0
        track = tags.get('track')
        by_slot[(disc, track)].append((pb, tags))
    kept = []
    for slot, members in by_slot.items():
        if len(members) == 1 or slot[1] is None:
            kept.extend(members)
            if slot[1] is None and len(members) > 1:
                # untracked files: keep all but they weaken completeness
                meta.untagged.extend(os.fsdecode(m[0]) for m in members)
            continue
        # differing normalized titles in one slot -> cannot safely pick -> PARK
        titles = {identity.normalize(m[1].get('title') or os.fsdecode(m[0]), 'track') for m in members}
        if len(titles) > 1:
            meta.intra_collision = True
            meta.reason = 'intra-folder-title-collision'
            return None, meta
        # same slot, same title -> keep ffprobe-best, rest are collision losers
        ranked = sorted(members, key=lambda m: _file_rank(m[0], cache), reverse=True)
        kept.append(ranked[0])
        meta.collision_losers.extend(m[0] for m in ranked[1:])

    # album-level modal fields (one mistagged file must not split identity)
    albumartist = _modal([t.get('albumartist') for _, t in kept]) \
        or _modal([t.get('artist') for _, t in kept])
    album = _modal([t.get('album') for _, t in kept])
    year = _year_of(_modal([t.get('date') for _, t in kept]))
    # mb_* only if present in >=50% of kept files AND uuid-valid
    def modal_mb(field_):
        present = [t.get(field_) for _, t in kept if t.get(field_)]
        if not present or len(present) < (len(kept) + 1) // 2:
            return ''
        v = Counter(present).most_common(1)[0][0]
        return v if identity._valid(v) else ''
    mb_albumid = modal_mb('mb_albumid')
    mb_rg = modal_mb('mb_releasegroupid')

    if not album:
        meta.synthesized_name = True   # no usable album tag
        meta.reason = 'no-album-tag'
        # AlbumRow with empty album -> identity returns tier-3 review (caller parks)
        return identity.AlbumRow(sentinel_id, albumartist, '', year, mb_albumid, mb_rg,
                                 _items_from_kept(kept)), meta

    items = _items_from_kept(kept)
    # FRAGMENT signal (recorded on meta, NOT identity)
    tracks_present = sorted({it['track'] for it in items if it['track']})
    tracktotal = _modal([str(t.get('tracktotal')) for _, t in kept if t.get('tracktotal')])
    tracktotal = int(tracktotal) if str(tracktotal).isdigit() else None
    n_real = len(items)
    contiguous = bool(tracks_present) and tracks_present == list(range(1, len(tracks_present) + 1))
    meta.is_fragment = (
        n_real == 1
        or (tracks_present and not contiguous)
        or (tracktotal and n_real < tracktotal)
    )
    meta.item_paths = [it['path'] for it in items]
    row = identity.AlbumRow(sentinel_id, albumartist, album, year, mb_albumid, mb_rg, items)
    return row, meta


def _items_from_kept(kept):
    items = []
    for pb, t in kept:
        items.append({'disc': t.get('disc') or 0, 'track': t.get('track'),
                      'title': t.get('title') or os.path.splitext(os.path.basename(os.fsdecode(pb)))[0],
                      'path': pb})   # NOTE: path kept as RAW BYTES for file ops
    return items


def _file_rank(path_bytes: bytes, cache: dict):
    q = ffprobe_truth(path_bytes) if path_bytes not in cache else cache[path_bytes]
    cache[path_bytes] = q
    try:
        size = os.stat(path_bytes).st_size
    except OSError:
        size = 0
    return (1 if q['lossless'] else 0, q['bitdepth'] or 0, q['samplerate'] or 0,
            q['duration'] or 0, size)


# ─── Identity resolution against the live library ────────────────────────────
def _ch_body(albumartist: str, album: str) -> Optional[str]:
    """Replicate identity's content-hash body (artist_key + SEP + album_key)."""
    artist_key = identity.VA_SENTINEL if identity.is_va(albumartist) \
        else identity.normalize(albumartist, 'artist')
    album_key = identity.normalize(album, 'album')
    if not album_key:
        return None
    return 'ch:' + hashlib.sha256(
        (artist_key + identity.SEP + album_key).encode('utf-8')).hexdigest()[:16]


def _identity_from_key(final_key, cand_row):
    base = identity.base_identity(cand_row)
    tier = 1 if final_key.startswith(('mbrg:', 'mbid:')) else (3 if final_key.startswith('review:') else 2)
    return identity.Identity(key=final_key, tier=tier, confidence=base.confidence,
                             review_reason=base.review_reason, canonical_mbid=base.canonical_mbid)


def _family_for(final_key, cand_body, by_album, library_albums):
    """LIBRARY albums (positive ids only) matching the candidate's final key OR its
    content-hash body (catches cross-tier/disc convergence)."""
    fam_ids, fam_keys = set(), set()
    for a in library_albums:
        if a.album_id < 0:
            continue
        lk = by_album[a.album_id]
        match = (lk == final_key)
        if not match and cand_body:
            lb = _ch_body(a.albumartist, a.album)
            match = bool(lb and lb == cand_body)
        if match:
            fam_ids.add(a.album_id)
            fam_keys.add(lk)
    if not fam_ids:
        return None
    return {'album_ids': sorted(fam_ids), 'keys': sorted(fam_keys), 'final_key': final_key}


def resolve_candidate_keys(cand_row, library_albums, by_album_lib=None):
    """Single-candidate resolution: key the candidate by the IDENTICAL algorithm as
    library rows via build_index(library + [candidate]). (build_plan uses the
    batched _resolve_batched so convergence applies ACROSS candidates too.)"""
    idx = identity.build_index(library_albums + [cand_row])
    by = idx['by_album']
    final_key = by[cand_row.album_id]
    ident = _identity_from_key(final_key, cand_row)
    family = _family_for(final_key, _ch_body(cand_row.albumartist, cand_row.album), by, library_albums)
    return ident, family


def _resolve_batched(scanned, library_albums):
    """Resolve ALL candidates in ONE build_index(library + every candidate row) so
    cross-tier (MBID vs no-MBID) and disc-suffix convergence applies across
    candidates AND library uniformly — the fix for two folders of one release that
    differ only by MBID-presence both routing NEW. Returns {id(scan): (Identity, family)}."""
    cand_rows = [s['row'] for s in scanned if s['row'] is not None]
    idx = identity.build_index(library_albums + cand_rows)
    by = idx['by_album']
    out = {}
    for s in scanned:
        row = s['row']
        if row is None:
            out[id(s)] = (None, None)
            continue
        final_key = by[row.album_id]
        ident = _identity_from_key(final_key, row)
        family = _family_for(final_key, _ch_body(row.albumartist, row.album), by, library_albums)
        out[id(s)] = (ident, family)
    return out


def _near_dup_in_library(cand_row, library_albums):
    """For a tier-2 (content-hash) candidate with no exact family: is there a library
    album by the SAME artist whose album name is a near-variant (substring/superset
    or token-subset)? Such a candidate is likely the same release under a different
    edition/separator/truncation -> PARK for eyes rather than import a silent dup."""
    a_key = identity.VA_SENTINEL if identity.is_va(cand_row.albumartist) \
        else identity.normalize(cand_row.albumartist, 'artist')
    al_key = identity.normalize(cand_row.album, 'album')
    if not al_key or not a_key:
        return None
    at = set(al_key.split())
    for lib in library_albums:
        if lib.album_id < 0:
            continue
        la_key = identity.VA_SENTINEL if identity.is_va(lib.albumartist) \
            else identity.normalize(lib.albumartist, 'artist')
        if la_key != a_key:
            continue
        ll_key = identity.normalize(lib.album, 'album')
        if not ll_key or ll_key == al_key:
            continue
        if al_key in ll_key or ll_key in al_key:
            return lib.album_id
        lt = set(ll_key.split())
        if at and lt and (at <= lt or lt <= at) \
                and min(len(at), len(lt)) / max(len(at), len(lt)) >= 0.5:
            return lib.album_id
    return None


# ─── Pure routing ────────────────────────────────────────────────────────────
def route(ident, family, scan_meta: ScanMeta, verdict: Optional[str],
          cand_row, reserved_keys: set, trust_names: bool, near_dup_id=None):
    """PURE routing decision -> (ROUTE, reason). No I/O."""
    # structural PARK gates (scan already set reasons)
    if ident is None or scan_meta.reason in (
            'no-audio-files', 'orphan-disc-folder', 'intra-folder-title-collision', 'scan-error'):
        return 'PARK', scan_meta.reason or 'unscannable'
    if scan_meta.synthesized_name and not trust_names:
        return 'PARK', 'no-album-tag (folder-name synthesis disabled under autonomy)'
    if ident.tier == 3 or ident.key.startswith('review:'):
        return 'PARK', f'tier-3-review:{ident.review_reason or ident.key}'
    # fragments NEVER import as NEW and NEVER upgrade (FRAGMENT_FILL removed)
    if scan_meta.is_fragment:
        if family:
            return 'PARK', 'fragment-of-existing (acquire full release; no album surgery)'
        return 'PARK', 'orphan-fragment (incomplete; acquire full release)'
    # in-run self-dedup: this key already claimed NEW this run
    if ident.key in reserved_keys and not family:
        return 'DUPLICATE', 'intra-run-duplicate (another candidate already owns this key)'
    if not family:
        # tier-2 near-duplicate of a differently-named library album -> PARK for eyes
        if ident.tier == 2 and near_dup_id is not None:
            return 'PARK', (f'near-duplicate of library album {near_dup_id} '
                            f'(tier-2 content-hash; possible edition/truncation variant — needs eyes)')
        return 'NEW', f'not-in-library (tier-{ident.tier})'
    # in library
    if len(family['album_ids']) > 1:
        return 'PARK', f"multi-id-family ({family['album_ids']}); collapse via dup-plan first"
    if verdict == 'STRICTLY_BETTER':
        # tier-2 destructive guard: collaboration-credit artist needs MBID confirmation
        if ident.tier == 2 and _COLLAB_RE.search(cand_row.albumartist or ''):
            return 'PARK', 'tier-2 upgrade on collaboration-credit artist (needs MBID)'
        return 'UPGRADE', 'in-library; candidate strictly better + complete superset'
    if verdict in ('WORSE', 'EQUAL'):
        if ident.tier == 2 and _COLLAB_RE.search(cand_row.albumartist or ''):
            return 'PARK', 'tier-2 duplicate on collaboration-credit artist (needs MBID)'
        return 'DUPLICATE', f'in-library; candidate {verdict.lower()} (discard)'
    return 'PARK', f'quality-{(verdict or "unknown").lower()} (needs eyes)'


# ─── Plan building (read-only) ───────────────────────────────────────────────
def _norm_disc(d):
    """Collapse the library's pervasive disc-0-vs-1 inconsistency (417 albums on
    disc 0, 650 on disc 1) to a single 'disc 1' sentinel for completeness
    comparison; keep disc>1 distinct so true multi-disc (Genshin 1/2/3) stays
    split. Mirrors identity._disc_set treating disc 0 as unknown/first."""
    return d if (d and d > 1) else 1


def _trackset(items):
    return {(_norm_disc(it.get('disc')), it['track']) for it in items if it.get('track')}


def _winner_rank(s, qcache):
    """Best candidate within a self-dedup key-group: non-fragment > lossless >
    bitdepth > samplerate > track count."""
    m = s['meta']
    cand_q = album_quality(m.item_paths, qcache) if m.item_paths else \
        {'lossless_all': False, 'min_bitdepth': 0, 'min_samplerate': 0, 'min_bitrate': 0, 'any_probe_fail': True}
    return (0 if m.is_fragment else 1,
            1 if cand_q.get('lossless_all') else 0,
            cand_q.get('min_bitdepth') or 0,
            cand_q.get('min_samplerate') or 0,
            len(_trackset(s['row'].items)) if s['row'] else 0)


def build_plan(candidate_folders: list, db_path: str, trust_names: bool):
    """The single plan builder used by dry-run AND execute. Read-only.
    Resolves ALL candidates in one batched build_index (cross-candidate cross-tier
    convergence), then self-dedups by resolved key (one winner per key)."""
    library_albums = identity.load_albums(db_path)
    qcache: dict = {}
    scanned = []
    sentinel = -1
    for folder in candidate_folders:
        row, meta = scan_folder_to_albumrow(folder, sentinel)
        sentinel -= 1
        scanned.append({'folder': folder, 'row': row, 'meta': meta})

    resolved = _resolve_batched(scanned, library_albums)
    for s in scanned:
        s['ident'], s['family'] = resolved[id(s)]

    # SELF-DEDUP: group by resolved key; pick a single winner per key.
    by_key = defaultdict(list)
    for s in scanned:
        k = s['ident'].key if s['ident'] else f"_unscannable:{s['folder']}"
        by_key[k].append(s)

    reserved_keys: set = set()
    plan = []
    for key, group in by_key.items():
        ordered = sorted(group, key=lambda s: _winner_rank(s, qcache), reverse=True)
        for i, s in enumerate(ordered):
            entry = _plan_one(s, library_albums, qcache, reserved_keys, trust_names, i == 0)
            if entry['route'] == 'NEW':
                reserved_keys.add(s['ident'].key)
            plan.append(entry)
    return plan, library_albums


def _plan_one(s, library_albums, qcache, reserved_keys, trust_names, is_winner):
    row, meta, ident, family = s['row'], s['meta'], s['ident'], s['family']
    cand_q = lib_q = None
    verdict = None
    matched = None
    lib_trackset = set()
    cand_trackset = _trackset(row.items) if row else set()
    if family:
        # ffprobe library side (the matched family's files) live, raw bytes
        lib_paths = []
        for aid in family['album_ids']:
            lt = read_items_raw(aid)
            lib_paths.extend(it['path'] for it in lt)
            lib_trackset |= {(_norm_disc(it.get('disc')), it['track']) for it in lt if it['track']}
        lib_q = album_quality(lib_paths, qcache)
        cand_q = album_quality(meta.item_paths, qcache) if meta.item_paths else None
        if cand_q:
            verdict = strictly_better(cand_q, lib_q, meta.is_fragment, cand_trackset, lib_trackset)
        la = identity.in_library(family['final_key'], identity.BEETS_DB) if False else None
        matched = {'album_ids': family['album_ids'], 'keys': family['keys']}
        m0 = album_meta(family['album_ids'][0])
        if m0:
            matched.update({'albumartist': m0['albumartist'], 'album': m0['album'], 'year': m0['year']})
    near_dup_id = None
    if ident is not None and ident.tier == 2 and not family and not meta.is_fragment and row is not None:
        near_dup_id = _near_dup_in_library(row, library_albums)
    if not is_winner and ident is not None:
        r, reason = 'DUPLICATE', 'intra-run-duplicate (lost self-dedup)'
    else:
        r, reason = route(ident, family, meta, verdict, row, reserved_keys, trust_names, near_dup_id)
    return {
        'candidate': {
            'kind': 'folder', 'path': meta.folder, 'n_audio_files': meta.n_audio,
            'is_fragment': meta.is_fragment, 'intra_collision': meta.intra_collision,
            'orphan_disc': meta.orphan_disc, 'synthesized_name': meta.synthesized_name,
            'collision_losers': [os.fsdecode(p) for p in meta.collision_losers],
            'scanned_albumartist': row.albumartist if row else None,
            'scanned_album': row.album if row else None,
            'scanned_year': row.year if row else None,
        },
        'identity': (None if ident is None else {
            'key': ident.key, 'tier': ident.tier, 'confidence': ident.confidence,
            'review_reason': ident.review_reason, 'canonical_mbid': ident.canonical_mbid,
        }),
        'family': family,
        'route': r,
        'route_reason': reason,
        'matched_library_album': matched,
        'quality': (None if not family else {
            'candidate': cand_q, 'library': lib_q, 'verdict': verdict,
            'completeness_superset': lib_trackset.issubset(cand_trackset) if lib_trackset else None,
            'cand_trackset_n': len(cand_trackset), 'lib_trackset_n': len(lib_trackset),
        }),
    }


# ─── dup-plan mode (in-library dedup THROUGH the gate) ───────────────────────
def _realpaths(paths_bytes):
    out = set()
    for pb in paths_bytes:
        try:
            out.add(os.path.realpath(pb))
        except OSError:
            out.add(pb)
    return out


def resolve_dup_plan(entry, library_albums, by_album_lib, ledger):
    """Validate keep+drops against the LIVE DB (ignore JSON metadata, re-measure).
    A drop is removable ONLY if: exists; trackset ⊆ keep trackset; files DISJOINT
    from keep on disk (the 35/41 commingled-folder hazard); same identity family
    as keep; and albumartist/album still match (rowid-reuse guard). Else PARK."""
    keep_id = entry['keep']['album_id']
    keep_meta = album_meta(keep_id)
    results = []
    if not keep_meta:
        return [{'keep_id': keep_id, 'route': 'PARK', 'reason': 'keep-album-absent (stale plan)'}]
    keep_items = read_items_raw(keep_id)
    keep_trackset = {(_norm_disc(it['disc']), it['track']) for it in keep_items if it['track']}
    keep_files = _realpaths([it['path'] for it in keep_items])
    keep_key = by_album_lib.get(keep_id)

    for drop in entry.get('drop', []):
        did = drop['album_id']
        dmeta = album_meta(did)
        if not dmeta:
            results.append({'keep_id': keep_id, 'drop_id': did, 'route': 'DUPPLAN_DONE',
                            'reason': 'drop-absent (already removed / stale)'})
            continue
        ditems = read_items_raw(did)
        dtrackset = {(_norm_disc(it['disc']), it['track']) for it in ditems if it['track']}
        dfiles = _realpaths([it['path'] for it in ditems])
        drop_key = by_album_lib.get(did)
        reasons = []
        # rowid-reuse guard
        if not (dmeta['albumartist'] == drop.get('artist', dmeta['albumartist'])
                or dmeta['album']):
            reasons.append('rowid-identity-mismatch')
        # same identity family as keep?
        same_family = (drop_key == keep_key) or (
            _ch_body(dmeta['albumartist'], dmeta['album'])
            and _ch_body(dmeta['albumartist'], dmeta['album']) == _ch_body(keep_meta['albumartist'], keep_meta['album']))
        if not same_family:
            reasons.append('not-same-identity-family')
        # disjoint-disc / split-album: drop trackset must be subset of keep
        if dtrackset and not dtrackset.issubset(keep_trackset):
            reasons.append('drop-trackset-not-subset-of-keep (split-album / distinct-disc)')
        # commingle guard: drop files must be DISJOINT from keep files on disk
        if dfiles & keep_files:
            reasons.append('drop-files-COMMINGLED-with-keep (moving would hole the keeper)')
        if reasons:
            results.append({'keep_id': keep_id, 'drop_id': did, 'route': 'PARK',
                            'reason': '; '.join(reasons), 'drop_n': len(ditems), 'keep_n': len(keep_items)})
        else:
            results.append({'keep_id': keep_id, 'drop_id': did, 'route': 'DUPPLAN_DROP',
                            'reason': 'safe row-only removal (files disjoint, subset, same family)',
                            'remove_cmd': ['beet', 'remove', '-a', '-f', f'id:{did}'],
                            'drop_n': len(ditems), 'keep_n': len(keep_items)})
    return results


def build_dup_plan(dup_plan_path: str, db_path: str):
    with open(dup_plan_path, encoding='utf-8') as f:
        plan = json.load(f)
    library_albums = identity.load_albums(db_path)
    by_album_lib = identity.build_index(library_albums)['by_album']
    ledger: dict = {}
    out = []
    for entry in plan:
        out.extend(resolve_dup_plan(entry, library_albums, by_album_lib, ledger))
    return out


# ─── Plan emission ───────────────────────────────────────────────────────────
def summarize(plan):
    c = Counter(e['route'] for e in plan)
    return {r: c.get(r, 0) for r in set(list(c.keys()) + list(ROUTES))}


def write_plan(plan, run_dir: Path, mode: str, db_path: str):
    run_dir.mkdir(parents=True, exist_ok=True)
    fp = db_data_version(db_path)
    top = {'schema': 2, 'mode': mode, 'dry_run': True, 'db_data_version': fp,
           'summary': summarize(plan), 'candidates': plan}
    (run_dir / 'plan.jsonl').write_text(
        '\n'.join(json.dumps(e, default=str) for e in plan), encoding='utf-8')
    (run_dir / 'plan.json').write_text(json.dumps(top, indent=1, default=str), encoding='utf-8')
    return top


def print_plan_table(plan, mode):
    print(f"\n=== reconcile PLAN ({mode}, DRY-RUN — no writes) ===")
    summ = summarize(plan)
    for k in sorted(summ):
        if summ[k]:
            print(f"  {k:16} {summ[k]}")
    print("-" * 100)
    if mode == 'dup-plan':
        for e in plan:
            print(f"  [{e['route']:12}] keep={e.get('keep_id')} drop={e.get('drop_id')}  "
                  f"({e.get('drop_n','?')}->{e.get('keep_n','?')})  {e['reason']}")
        return
    for e in sorted(plan, key=lambda x: (x['route'], x['candidate']['path'])):
        c = e['candidate']
        ik = (e['identity'] or {}).get('key', '—')
        q = e.get('quality') or {}
        vd = q.get('verdict', '')
        name = os.path.basename(c['path'].rstrip('/'))
        print(f"  [{e['route']:10}] {name[:54]:54}  {ik[:22]:22} {vd:14} {e['route_reason'][:60]}")


# ════════════════════════════════════════════════════════════════════════════
#  EXECUTE PATH — GATED. Only reachable via --execute. NOT run until the beets-
#  behavior probes pass (test plan probes a–d) AND the operator approves Phase 5.
#  Implemented faithfully to the blueprint for review; import-new-before-retire-old.
# ════════════════════════════════════════════════════════════════════════════
class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, state: str, op: str, **kw):
        rec = {'state': state, 'op': op, **kw}
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, default=str) + '\n')
            f.flush()
            os.fsync(f.fileno())


def graveyard_move(src_bytes: bytes, dst_bytes: bytes, journal: Journal):
    """Atomic same-fs rename ONLY. HARD-FAIL on EXDEV (never copy+unlink — a crash
    mid-copy could destroy the only copy)."""
    src_dev = os.stat(src_bytes).st_dev
    dst_parent = os.path.dirname(dst_bytes)
    os.makedirs(dst_parent, exist_ok=True)
    dst_dev = os.stat(dst_parent).st_dev
    if src_dev != dst_dev:
        raise RuntimeError(f"EXDEV refuse: {os.fsdecode(src_bytes)} dev={src_dev} -> dev={dst_dev}")
    # Files (UPGRADE old-file moves) get a content hash for the undo manifest;
    # directories (DUPLICATE/PARK whole-folder moves) record a file count instead —
    # _sha256 would IsADirectoryError on a dir, and a same-fs dir rename is atomic.
    is_file = os.path.isfile(src_bytes)
    sha = _sha256(src_bytes) if is_file else None
    nfiles = None if is_file else sum(len(fs) for _, _, fs in os.walk(src_bytes))
    journal.record('PENDING', 'graveyard_move', src=os.fsdecode(src_bytes),
                   dst=os.fsdecode(dst_bytes), sha256=sha, n_files=nfiles)
    os.rename(src_bytes, dst_bytes)
    journal.record('DONE', 'graveyard_move', dst=os.fsdecode(dst_bytes), sha256=sha, n_files=nfiles)


def _sha256(path_bytes: bytes) -> str:
    h = hashlib.sha256()
    with open(path_bytes, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def upsert_identity_flexattrs(album_id: int, ident, db_path: str = BEETS_DB):
    """Direct sqlite upsert of identity_key/tier/conf. RE-DERIVED from the imported
    DB row, NOT the pre-import guess (autotag rewrites tags -> key can change)."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        for k, v in (('identity_key', ident.key), ('identity_tier', str(ident.tier)),
                     ('identity_conf', f'{ident.confidence:.3f}')):
            conn.execute(
                "INSERT INTO album_attributes(entity_id,key,value) VALUES(?,?,?) "
                "ON CONFLICT(entity_id,key) DO UPDATE SET value=excluded.value",
                (album_id, k, v))
        conn.commit()
    finally:
        conn.close()


def _upsert_attr(album_id: int, key: str, value: str, db_path: str = BEETS_DB):
    """Direct sqlite upsert of one album flexattr (DB-only)."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            "INSERT INTO album_attributes(entity_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(entity_id,key) DO UPDATE SET value=excluded.value",
            (album_id, key, value))
        conn.commit()
    finally:
        conn.close()


def new_album_id_by_path(dest_prefix_bytes: bytes, db_path: str = BEETS_DB) -> list:
    """Capture the just-imported album_id by path-prefix (NOT max-id delta —
    rowid reuse is possible: no sqlite_sequence)."""
    conn = _ro_conn(db_path)
    try:
        like = dest_prefix_bytes + b'%'
        rows = conn.execute(
            "SELECT DISTINCT album_id FROM items WHERE path LIKE ?", (like,)).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _write_cfg_overlay(run_dir: Path) -> str:
    """Ephemeral beets config overlay applied via `beet -c` (LAYERS over the real
    user config — plugins/library/directory inherited). Forces the gate's import
    semantics. duplicate_action MUST be 'skip' (probe-confirmed; 'no' is invalid)."""
    p = run_dir / 'cfg_overlay.yaml'
    p.write_text("import:\n  duplicate_action: skip\n  move: yes\n  write: yes\n  "
                 "quiet: yes\n  resume: no\n  incremental: no\n", encoding='utf-8')
    return str(p)


def _album_id_set(db_path: str) -> set:
    conn = _ro_conn(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT id FROM albums")}
    finally:
        conn.close()


def _albumrow_for(album_id: int, db_path: str):
    for a in identity.load_albums(db_path):
        if a.album_id == album_id:
            return a
    return None


class RunContext:
    def __init__(self, db_path, run_dir, journal, read_only_source=False):
        self.db_path = db_path
        self.run_dir = run_dir
        self.journal = journal
        self.cfg_overlay = _write_cfg_overlay(run_dir)
        self.lib_grave = LIB_GRAVEYARD / run_dir.name
        self.inbound_grave = INBOUND_GRAVEYARD / run_dir.name
        self.park = PARK_DIR / run_dir.name
        self.reserved_keys: set = set()
        self.qcache: dict = {}
        self.read_only_source = read_only_source


def _beet_import(folder: str, ctx: RunContext):
    """Gated import. Overlay forces duplicate_action:skip + move:yes. Uses
    --quiet-fallback=ASIS (decided 2026-06-14): reconcile has ALREADY gated this
    candidate (confident non-tier-3 identity, complete non-fragment, not-in-library,
    no near-dup, self-dedup winner), so when beets autotag has no confident match it
    imports with the folder's existing tags rather than parking every multi-candidate
    release. Safety is unchanged: duplicate_action:skip still skips anything beets
    recognizes as a dup -> 0-added -> reconcile routes REVIEW (now meaning a beets-vs-
    oracle disagreement, not merely 'no match'). asis imports are flagged for later
    enrichment (see do_new). Returns (new_album_ids, proc); new_album_ids = id
    set-difference before/after (robust to rowid reuse — no removes happen between)."""
    pre = _album_id_set(ctx.db_path)
    cmd = ['beet', '-c', ctx.cfg_overlay, 'import', '-q', '--quiet-fallback=asis',
           '--noincremental', '--', str(folder)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    post = _album_id_set(ctx.db_path)
    return sorted(post - pre), proc


def do_park(entry, ctx: RunContext, reason: str):
    """Move a candidate folder to the same-fs park dir for human review. In
    read-only-source mode (--backlog) record-only, never move the frozen corpus."""
    folder = entry['candidate']['path']
    ctx.journal.record('PARK', 'PARK', folder=folder, reason=reason)
    if ctx.read_only_source:
        return {'route': 'PARK', 'reason': reason, 'moved': False}
    dst = ctx.park / os.path.basename(folder.rstrip('/'))
    graveyard_move(os.fsencode(folder), os.fsencode(str(dst)), ctx.journal)
    return {'route': 'PARK', 'reason': reason, 'moved': True, 'dst': str(dst)}


def do_duplicate(entry, ctx: RunContext):
    """Discard a duplicate candidate to the same-fs inbound graveyard (reversible).
    No beets write."""
    folder = entry['candidate']['path']
    ctx.journal.record('PENDING', 'DUPLICATE', folder=folder)
    if ctx.read_only_source:
        ctx.journal.record('DONE', 'DUPLICATE', folder=folder, moved=False)
        return {'route': 'DUPLICATE', 'moved': False}
    dst = ctx.inbound_grave / 'duplicates' / os.path.basename(folder.rstrip('/'))
    graveyard_move(os.fsencode(folder), os.fsencode(str(dst)), ctx.journal)
    ctx.journal.record('DONE', 'DUPLICATE', folder=folder, dst=str(dst))
    return {'route': 'DUPLICATE', 'moved': True, 'dst': str(dst)}


def do_new(entry, ctx: RunContext):
    """Import a genuinely-new album (--quiet-fallback=asis, see _beet_import).
    Capture the new album_id by id set-difference. Under asis, 0-added no longer
    means 'no match' — it means beets' duplicate_action:skip skipped it as a dup,
    i.e. a beets-vs-oracle DISAGREEMENT -> REVIEW. >1 -> REVIEW (auto-split).
    Identity is RE-DERIVED from the imported DB row, not the scan key. An import that
    landed WITHOUT a valid MB release id (i.e. came in asis) is flagged
    reconcile_import=asis so it can be bulk-enriched (beet mbsync) and audited later."""
    folder = entry['candidate']['path']
    key = (entry['identity'] or {}).get('key')
    ctx.journal.record('PENDING', 'NEW', folder=folder, scan_key=key)
    new_ids, proc = _beet_import(folder, ctx)
    if len(new_ids) == 0:
        ctx.journal.record('REVIEW', 'NEW', folder=folder, reason='0-added-beets-dup-skip',
                           rc=proc.returncode)
        return do_park(entry, ctx, '0-added: beets skipped as duplicate (oracle said NEW '
                                   '— beets disagrees; investigate identity)')
    if len(new_ids) > 1:
        ctx.journal.record('REVIEW', 'NEW', folder=folder, reason=f'multi-album-{new_ids}')
        return {'route': 'NEW->REVIEW', 'reason': f'folder auto-split into {new_ids}; investigate'}
    aid = new_ids[0]
    row = _albumrow_for(aid, ctx.db_path)
    idx = identity.build_index(identity.load_albums(ctx.db_path))
    final_key = idx['by_album'].get(aid)
    rid = _identity_from_key(final_key, row) if row else None
    if rid:
        upsert_identity_flexattrs(aid, rid, ctx.db_path)
        ctx.reserved_keys.add(final_key)
    # audit marker: did MB autotag enrich it, or did it come in asis (no MB release id)?
    mb_matched = bool(row and (identity._valid(row.mb_albumid) or identity._valid(row.mb_releasegroupid)))
    mode = 'autotag' if mb_matched else 'asis'
    _upsert_attr(aid, 'reconcile_import', mode, ctx.db_path)
    ctx.journal.record('DONE', 'NEW', folder=folder, album_id=aid, key=final_key, import_mode=mode)
    return {'route': 'NEW', 'album_id': aid, 'key': final_key, 'import_mode': mode}


def do_upgrade(entry, ctx: RunContext):
    """UPGRADE-in-place is the only irreversible-swap operation and does not occur
    in the validated backlog/dup-plan. It is intentionally NOT auto-executed: it
    parks to review so a human enables the swap deliberately per-candidate. The
    full import-new-before-retire-old saga (PHASE 0 guards -> import new -> verify
    superset -> graveyard old -> remove old rows) is specified in the blueprint
    (/root/reconcile_design_result.json) and will be wired behind an explicit
    --enable-upgrade flag once a real upgrade candidate exists to validate against."""
    return do_park(entry, ctx, 'UPGRADE-in-place not auto-enabled (irreversible swap — '
                               'wire behind --enable-upgrade with a real candidate + operator OK)')


def do_dupplan_drop(result, ctx: RunContext):
    """Remove a duplicate album ROW by id (no -d — files already proven disjoint and
    left in place). Re-verifies the drop's identity against the DB before removal
    (rowid-reuse guard)."""
    did = result['drop_id']
    meta = album_meta(did, ctx.db_path)
    if not meta:
        ctx.journal.record('DONE', 'DUPPLAN_DROP', drop_id=did, note='already-absent')
        return {'route': 'DUPPLAN_DROP', 'drop_id': did, 'skipped': 'already-absent'}
    ctx.journal.record('PENDING', 'DUPPLAN_DROP', drop_id=did,
                       albumartist=meta['albumartist'], album=meta['album'])
    cmd = ['beet', '-c', ctx.cfg_overlay, 'remove', '-a', '-f', f'id:{did}']
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    ctx.journal.record('DONE', 'DUPPLAN_DROP', drop_id=did, rc=proc.returncode)
    return {'route': 'DUPPLAN_DROP', 'drop_id': did, 'rc': proc.returncode}


_DISPATCH = {'NEW': do_new, 'DUPLICATE': do_duplicate, 'UPGRADE': do_upgrade}


def execute_plan(plan, run_dir: Path, db_path: str, mode: str, read_only_source: bool):
    """Apply a plan. Re-validates each NEW against the live oracle immediately
    before importing (rebuilds the index after every committed write so a candidate
    that an earlier write just satisfied flips NEW->DUPLICATE). The library is only
    ever written here, by one process under the writer lock."""
    journal = Journal(run_dir / 'journal.jsonl')
    ctx = RunContext(db_path, run_dir, journal, read_only_source=read_only_source)
    journal.record('BEGIN', 'execute', mode=mode, n=len(plan),
                   db_data_version=db_data_version(db_path))
    results = []
    if mode == 'dup-plan':
        for r in plan:
            if r['route'] == 'DUPPLAN_DROP':
                results.append(do_dupplan_drop(r, ctx))
            else:
                journal.record('SKIP', r['route'], **{k: r.get(k) for k in ('keep_id', 'drop_id')})
        journal.record('END', 'execute', committed=len(results))
        return results
    for entry in plan:
        route_ = entry['route']
        if route_ == 'PARK':
            results.append(do_park(entry, ctx, entry.get('route_reason', 'park')))
            continue
        if route_ == 'NEW':
            # re-validate live: an earlier NEW this run may have satisfied this key
            row = entry.get('_row')
            if entry['identity'] and entry['identity']['key'] in ctx.reserved_keys:
                results.append(do_duplicate(entry, ctx))
                continue
            lib = identity.in_library(entry['identity']['key'], db_path) if entry['identity'] else None
            if lib:
                results.append(do_duplicate(entry, ctx))
                continue
        fn = _DISPATCH.get(route_)
        results.append(fn(entry, ctx) if fn else do_park(entry, ctx, f'unknown-route-{route_}'))
    journal.record('END', 'execute', committed=len(results))
    return results


# ─── Lock + preconditions ────────────────────────────────────────────────────
@contextlib.contextmanager
def writer_lock(execute: bool):
    RECON_DURABLE.mkdir(parents=True, exist_ok=True)
    lock_path = RECON_DURABLE / 'reconcile.lock'
    f = open(lock_path, 'w')
    try:
        mode = fcntl.LOCK_EX if execute else fcntl.LOCK_SH
        try:
            fcntl.flock(f, mode | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(2)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def check_preconditions():
    for tool in ('ffprobe', 'beet'):
        if shutil.which(tool) is None:
            print(f"FATAL: {tool} not on PATH", file=sys.stderr)
            raise SystemExit(3)


# ─── CLI ─────────────────────────────────────────────────────────────────────
def _settled(folder: str, min_age_min: int) -> bool:
    import time
    cutoff = time.time() - min_age_min * 60
    fb = os.fsencode(folder)
    for root, _, files in os.walk(fb):
        for fn in files:
            try:
                if os.stat(os.path.join(root, fn)).st_mtime > cutoff:
                    return False
            except OSError:
                pass
    return True


def _list_candidate_dirs(source: str, skip_names=()) -> list:
    """Candidate folders under source: real dirs, not _/.-prefixed, and not in
    skip_names (case-insensitive basenames). reconcile-import passes the dirs
    slskd is still downloading into — slskd quiesces a folder between per-file
    moves, so mtime alone would sweep a mid-transfer album as a fragment."""
    skip = {s.lower() for s in skip_names}
    return sorted(
        os.path.join(source, d) for d in os.listdir(source)
        if os.path.isdir(os.path.join(source, d))
        and not d.startswith('_') and not d.startswith('.')
        and d.lower() not in skip)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="reconcile.py — the one gate / one writer (DRY-RUN default)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument('--inbox', nargs='?', const=str(cfg.INBOX_DIR), help='reconcile folders in INBOX_DIR (default source)')
    src.add_argument('--backlog', help='reconcile a folder of candidate dirs (READ-ONLY source; never written even with --execute)')
    src.add_argument('--dup-plan', help='in-library dedup from a pipeline_dup_plan.json')
    ap.add_argument('--execute', action='store_true', help='WRITE (gated; default is dry-run)')
    ap.add_argument('--trust-folder-names', action='store_true')
    ap.add_argument('--min-age-min', type=int, default=10)
    ap.add_argument('--skip-dir', action='append', default=[], metavar='NAME',
                    help='folder basename to exclude from this run (repeatable, '
                         'case-insensitive); reconcile-import shields active '
                         'slskd downloads with this')
    ap.add_argument('--db', default=BEETS_DB)
    ap.add_argument('--run-id', default='run')
    args = ap.parse_args(argv)

    check_preconditions()
    mode = 'dup-plan' if args.dup_plan else ('backlog' if args.backlog else 'disk')
    source = args.dup_plan or args.backlog or args.inbox or str(cfg.INBOX_DIR)

    if args.execute and (mode == 'backlog'):
        print("--backlog is READ-ONLY-SOURCE; refusing --execute on the frozen validation set", file=sys.stderr)
        return 3

    run_dir = PLAN_ROOT / args.run_id
    with writer_lock(execute=args.execute):
        if mode == 'dup-plan':
            plan = build_dup_plan(source, args.db)
            top = write_plan(plan, run_dir, mode, args.db)
            print_plan_table(plan, mode)
        else:
            folders = _list_candidate_dirs(source, args.skip_dir)
            if mode != 'backlog':
                kept = [f for f in folders if _settled(f, args.min_age_min)]
            else:
                kept = folders
            plan, _ = build_plan(kept, args.db, args.trust_folder_names)
            top = write_plan(plan, run_dir, mode, args.db)
            print_plan_table(plan, mode)

        if args.execute:
            run_dir.mkdir(parents=True, exist_ok=True)
            print("\n=== EXECUTE (writing to library) ===")
            res = execute_plan(plan, run_dir, args.db, mode, read_only_source=(mode == 'backlog'))
            print(f"executed {len(res)} entries; journal: {run_dir}/journal.jsonl")

    print(f"\nplan written: {run_dir}/plan.json")
    summ = summarize(plan)
    return 4 if summ.get('PARK') else 0


if __name__ == '__main__':
    sys.exit(main())
