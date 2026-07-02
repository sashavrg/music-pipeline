"""
MusicBrainz album-size lookup (telegram_bot canonical-release lookup) when local tag-total
is missing and the gap heuristic can't fire (contiguous prefix of tracks).

Fail-open: any network/parse error returns None, so callers fall back to
their existing 'looks complete' verdict.
"""
import json
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter

from . import db as pipeline_db

_UA = 'music-pipeline/1.0 (sasha@renoon.com)'
_MB_URL = 'https://musicbrainz.org/ws/2/release/'
_CACHE_TTL_S = 30 * 24 * 3600  # 30 days
_MIN_SCORE = 80
_RATE_LIMIT_S = 1.05

_rate_lock = threading.Lock()
_last_call_ts = 0.0


def _rate_limit():
    global _last_call_ts
    with _rate_lock:
        wait = _RATE_LIMIT_S - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


def _query_mb(artist: str, album: str):
    q = f'artist:"{artist}" AND release:"{album}"'
    url = f'{_MB_URL}?{urllib.parse.urlencode({"query": q, "fmt": "json", "limit": 10})}'
    _rate_limit()
    req = urllib.request.Request(url, headers={'User-Agent': _UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _track_counts_from_payload(payload) -> list[int]:
    """Pull track-counts from releases whose score clears _MIN_SCORE."""
    counts = []
    for rel in (payload or {}).get('releases', []) or []:
        try:
            score = int(rel.get('score', 0))
        except (TypeError, ValueError):
            score = 0
        if score < _MIN_SCORE:
            continue
        total = 0
        for medium in rel.get('media', []) or []:
            tc = medium.get('track-count')
            if isinstance(tc, int) and tc > 0:
                total += tc
        if total > 0:
            counts.append(total)
    return counts


def _extract_canonical(payload):
    """Pick the highest-scoring MB release (>= _MIN_SCORE) and return its
    canonical artist-credit phrase, title, MBID, and score. None if no
    confident match.

    MB returns artist-credit as an ordered list of {artist, joinphrase} dicts
    (with bare strings as separators). Concatenating name + joinphrase gives
    the human-readable credit — that's how 'Marvin Gaye' resolves to
    'Marvin Gaye & Tammi Terrell' for the *United* album.
    """
    best = None
    best_score = -1
    for rel in (payload or {}).get('releases', []) or []:
        try:
            score = int(rel.get('score', 0))
        except (TypeError, ValueError):
            score = 0
        if score < _MIN_SCORE or score <= best_score:
            continue
        credit = ''
        for piece in rel.get('artist-credit', []) or []:
            if isinstance(piece, dict):
                if isinstance(piece.get('artist'), dict):
                    credit += piece['artist'].get('name', '') or ''
                credit += piece.get('joinphrase', '') or ''
            elif isinstance(piece, str):
                credit += piece
        credit = credit.strip()
        title = (rel.get('title') or '').strip()
        if not credit or not title:
            continue
        best = {
            'artist_credit': credit,
            'title':         title,
            'mbid':          rel.get('id'),
            'score':         score,
        }
        best_score = score
    return best


def lookup_canonical_release(artist: str, album: str):
    """
    Return {'artist_credit','title','mbid','score'} for the top-scoring MB
    release matching (artist, album), or None if no confident match / network
    failure. Cached for _CACHE_TTL_S including negative results.

    Used by the telegram bot to retry a 0-result search with the canonical
    artist-credit and title (e.g. 'Marvin Gaye - United' resolves to
    'Marvin Gaye & Tammi Terrell - United').
    """
    artist = (artist or '').strip()
    album = (album or '').strip()
    if not artist or not album:
        return None
    if artist.lower() in {'unknown', 'unknown artist', 'various artists'}:
        return None

    cached = pipeline_db.get_canonical_cached(artist, album, _CACHE_TTL_S)
    if cached is not None:
        canon, is_fresh = cached
        if is_fresh:
            return canon  # may be None (cached negative)

    try:
        payload = _query_mb(artist, album)
    except Exception:
        pipeline_db.upsert_canonical_cache(artist, album, None)
        return None

    canon = _extract_canonical(payload)
    pipeline_db.upsert_canonical_cache(artist, album, canon)
    return canon


def lookup_track_count(artist: str, album: str):
    """
    Return canonical track count for (artist, album), or None if unknown.
    Uses the mode of track counts across top-scored MB releases — robust to
    deluxe/anniversary outliers while still detecting a missing side.
    Cached for _CACHE_TTL_S (including negative results).
    """
    artist = (artist or '').strip()
    album = (album or '').strip()
    if not artist or not album:
        return None
    if artist.lower() in {'unknown', 'unknown artist', 'various artists'}:
        return None

    cached = pipeline_db.get_mb_cached_track_count(artist, album, _CACHE_TTL_S)
    if cached is not None:
        count, is_fresh = cached
        if is_fresh:
            return count  # may be None (cached negative)

    try:
        payload = _query_mb(artist, album)
    except Exception:
        pipeline_db.upsert_mb_cache(artist, album, None)
        return None

    counts = _track_counts_from_payload(payload)
    if not counts:
        pipeline_db.upsert_mb_cache(artist, album, None)
        return None

    mode_count = Counter(counts).most_common(1)[0][0]
    pipeline_db.upsert_mb_cache(artist, album, mode_count)
    return mode_count
