"""
Detect audiobook folders and route them out of the music pipeline.

Heuristics layer: any one *strong* signal (explicit "audiobook" or
"spoken word" genre tag) OR two *weak* signals. Weak signals are
"fiction"/"non-fiction" genre, mp3-only at <128 kbps, average track
duration >20 min, and a majority of titles matching "Part N"/"Chapter N".

Fail-closed: when uncertain, return False so the music pipeline gets
the folder. A false-positive silently disappears the album into
/audiobooks (away from Plex/beets); a false-negative just means
the user does what they did before — move it by hand.
"""
import re
import shutil
import time
import unicodedata
from collections import Counter
from pathlib import Path

import mutagen

from . import config as cfg

_STRONG_GENRES = re.compile(r'\b(audiobook|audio\s*book|spoken[\s-]?word)\b', re.IGNORECASE)
_WEAK_GENRES   = re.compile(r'\b(fiction|non[\s-]?fiction)\b', re.IGNORECASE)
_PART_RE       = re.compile(r'\b(part|chapter)\s*\d+\b', re.IGNORECASE)

_MAX_MUSIC_BITRATE_KBPS = 128
_MIN_AUDIOBOOK_DURATION_S = 20 * 60  # 20 minutes per track on average


def _get_first(tags, keys):
    if not tags:
        return ''
    for k in keys:
        v = tags.get(k)
        if v is None:
            continue
        if isinstance(v, list) and v:
            return str(v[0])
        if hasattr(v, 'text') and v.text:
            return str(v.text[0])
        s = str(v).strip()
        if s:
            return s
    return ''


def _probe_file(path: Path) -> dict:
    try:
        mf = mutagen.File(str(path), easy=False)
    except Exception:
        return {}
    if mf is None:
        return {}
    tags = mf.tags or {}
    info = getattr(mf, 'info', None)
    return {
        'genre':       _get_first(tags, ('genre', 'TCON')),
        'title':       _get_first(tags, ('title', 'TIT2')),
        'artist':      _get_first(tags, ('artist', 'TPE1')),
        'albumartist': _get_first(tags, ('albumartist', 'TPE2', 'ALBUMARTIST')),
        'album':       _get_first(tags, ('album', 'TALB')),
        'bitrate':     int(getattr(info, 'bitrate', 0) or 0) // 1000,
        'duration':    float(getattr(info, 'length', 0) or 0),
        'ext':         path.suffix.lower().lstrip('.'),
    }


def extract_signals(file_infos: list[dict]) -> dict:
    """Pure function: probe dicts → {'is_audiobook': bool, 'reason': str}."""
    if not file_infos:
        return {'is_audiobook': False, 'reason': 'no-files'}

    genres    = [fi.get('genre', '') for fi in file_infos]
    titles    = [fi.get('title', '') for fi in file_infos]
    bitrates  = [fi['bitrate'] for fi in file_infos if fi.get('bitrate')]
    durations = [fi['duration'] for fi in file_infos if fi.get('duration')]
    exts      = {fi.get('ext') for fi in file_infos if fi.get('ext')}

    strong_genre = any(_STRONG_GENRES.search(g) for g in genres)
    weak_genre   = any(_WEAK_GENRES.search(g) for g in genres)
    # Bitrate signal is only meaningful for mp3-family files — lossless
    # encodes have effective bitrates that swamp any audiobook threshold.
    mp3_only = bool(exts) and exts.issubset({'mp3'})
    low_bitrate = (
        mp3_only
        and bool(bitrates)
        and all(0 < b < _MAX_MUSIC_BITRATE_KBPS for b in bitrates)
    )
    avg_duration  = sum(durations) / len(durations) if durations else 0
    long_duration = avg_duration > _MIN_AUDIOBOOK_DURATION_S
    title_hits    = sum(1 for t in titles if _PART_RE.search(t or ''))
    title_pattern = title_hits >= max(1, len(file_infos) // 2)

    weak_score = sum([weak_genre, low_bitrate, long_duration, title_pattern])

    parts = []
    if strong_genre:  parts.append('genre=strong')
    if weak_genre:    parts.append('genre=weak')
    if low_bitrate:   parts.append(f'bitrate<{_MAX_MUSIC_BITRATE_KBPS}kbps')
    if long_duration: parts.append(f'avg-duration={avg_duration/60:.0f}min')
    if title_pattern: parts.append('title-part-pattern')

    is_audiobook = strong_genre or weak_score >= 2
    return {
        'is_audiobook': is_audiobook,
        'reason':       ', '.join(parts) or 'no-signals',
    }


def looks_like_audiobook(audio_files: list[Path]) -> tuple[bool, str]:
    infos = [_probe_file(p) for p in audio_files]
    sig = extract_signals(infos)
    return sig['is_audiobook'], sig['reason']


# "Vol 1 - 2014 - Annihilation" → "Annihilation"; "(2014) Foo" → "Foo"
_FOLDER_NOISE_RE = re.compile(
    r'^\s*(?:vol(?:ume)?\.?\s*\d+\s*[-_]?\s*)?(?:\(?\d{4}\)?\s*[-_]?\s*)?',
    re.IGNORECASE,
)


def _slug_safe(s: str) -> str:
    s = unicodedata.normalize('NFC', s).strip()
    s = re.sub(r'[\\/]+', ' ', s)
    return s.strip(' .') or 'Unknown'


def derive_author_title(folder: Path, infos: list[dict]) -> tuple[str, str]:
    artists = [i.get('albumartist') or i.get('artist') for i in infos]
    artists = [a for a in artists if a]
    albums  = [i.get('album') for i in infos if i.get('album')]
    author = Counter(artists).most_common(1)[0][0] if artists else 'Unknown Author'
    if albums:
        title = Counter(albums).most_common(1)[0][0]
    else:
        title = _FOLDER_NOISE_RE.sub('', folder.name).strip(' -_') or folder.name
    return _slug_safe(author), _slug_safe(title)


def route_audiobook(folder: Path, audio_files: list[Path],
                    library_root: Path | None = None) -> Path:
    """Move `folder` into <library_root>/<author>/<title>/."""
    if library_root is None:
        library_root = cfg.AUDIOBOOKS_LIBRARY_ROOT
    infos = [_probe_file(p) for p in audio_files]
    author, title = derive_author_title(folder, infos)
    dst_dir = library_root / author
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / title
    if target.exists():
        target = dst_dir / f'{title}__{time.strftime("%Y%m%d-%H%M%S")}'
    shutil.move(str(folder), str(target))
    return target
