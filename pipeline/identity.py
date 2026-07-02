#!/usr/bin/env python3
"""identity.py — the SINGLE source of truth for "which release is this?"

Replaces the 4 divergent in_library/count matchers (recover.count_existing_tracks,
quarantine.in_library, the retired promote_ready, incomplete_watchdog.in_library)
and the 2 operator dedup matchers. Designed + red-teamed against the live library
(see memory: music-pipeline-rethink; spec validated on 1142 albums / 11693 items).

THREE-TIER release identity:
  TIER-1  MusicBrainz IDs — release-group dominates album-id; UUID-shape-validated.
  TIER-2  deterministic content-hash over exactly TWO normalized fields
          (artist_key, album_key) + a conditional disc-suffix. year, trackcount,
          per-track titles, format, composer are EXCLUDED (each verified to cause a
          false-merge or false-split).
  TIER-3  human review (fail-to-review) whenever the safe answer can't be auto-decided.

Group-level decisions (disc-suffix, MBID convergence, fail-to-review) are computed by
build_index() over the whole library, NOT per album in isolation.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
BEETS_DB = "/root/.config/beets/library.db"

UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

# Superset of the four divergent AUDIO_EXTS sets (recover wrongly lacked aac/alac/wma).
AUDIO_EXTS = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav',
              '.alac', '.aiff', '.aif', '.wma', '.ape', '.wv', '.m4b'}

SEP = '\x1f'          # unit separator — field boundary that punctuation can't forge
VA_SENTINEL = '\x01VA'

# Dash + curly-quote folding (shared album/artist pre-pass)
_DASHES = {0x2013: '-', 0x2014: '-', 0x2015: '-', 0x2212: '-'}
_QUOTES = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'}
_TRANS = {**_DASHES, **_QUOTES}

# A bracket is stripped (album scope) only when EVERY token inside is format-noise
# AND at least one is a real format WORD — so '[WEB FLAC 16 44]' and '[FLAC]' go,
# but '[Disc 1]', '(2024)', '(10th Anniversary Edition)' are preserved.
_FORMAT_WORDS = {'flac', 'mp3', 'wav', 'aac', 'alac', 'ogg', 'opus', 'm4a', 'm4b',
                 'web', 'web-flac', 'cd', 'sacd', 'vinyl', 'lp', 'lossless', 'hd',
                 'hires', 'hi-res', 'kbps', 'kbit'}
_FMT_NUM_RE = re.compile(r'^\d{1,4}(?:bit|k|khz|hz|kbps|kbit)?$')
_BRACKET_GROUP_RE = re.compile(r'[\[\(]([^\[\]\(\)]*)[\]\)]')
_APMU_RE = re.compile(r'\bAPMU\d+\b', re.I)


def _strip_format_brackets(s: str) -> str:
    def repl(m):
        toks = [t for t in re.split(r'[\s,/&+_.-]+', m.group(1).strip()) if t]
        if not toks:
            return m.group(0)
        low = [t.lower() for t in toks]
        has_word = any(t in _FORMAT_WORDS for t in low)
        all_noise = all(t in _FORMAT_WORDS or _FMT_NUM_RE.match(t) for t in low)
        return '' if (has_word and all_noise) else m.group(0)
    prev = None
    while prev != s:
        prev = s
        s = _BRACKET_GROUP_RE.sub(repl, s)
    return s
_CURLY_RE = re.compile(r'\{[^}]*\}')
_TRAIL_YEAR_RE = re.compile(r'\s*[\[\(](?:19|20)\d{2}[\]\)]\s*$')   # single trailing bare year

# Artist-credit separators (drop trailing feat/vs/presents/&-credit; KEEP and/with).
_ARTIST_SEPS_RE = re.compile(r'\s+(?:&|feat\.?|featuring|vs\.?|presents)\s+', re.I)
_VA_RE = re.compile(r'^(?:various(?:\s*artists?)?|v\.?\s?a\.?|compilation)$', re.I)

# track scope (verbatim dedup_pipeline.norm_title)
_TRACK_FEAT_RE = re.compile(r'\(feat[^)]*\)|\bfeat\.?\b.*$|\(live[^)]*\)|\(.*?remaster.*?\)', re.I)
_NONALNUM_RE = re.compile(r'[^a-z0-9]+')

_UNKNOWN_ARTISTS = {'', 'unknown', 'unknown artist'}


# ─────────────────────────────────────────────────────────────────────────────
# normalize() — ONE normalizer, three scopes
# ─────────────────────────────────────────────────────────────────────────────
def _prepass(s: str) -> str:
    s = unicodedata.normalize('NFC', s or '')
    return s.translate(_TRANS)


def normalize(s: str, scope: str = 'album') -> str:
    """The single normalizer. scope in {'album','track','artist'}.

    album  : strips ONLY format/quality/source noise + a single trailing bare year;
             PRESERVES every edition/version/volume/disc token. casefolded.
    track  : aggressive — strips feat/live/remaster parentheticals to bare [a-z0-9].
    artist : primary-credit only (drops trailing feat/vs/presents/&-credit).
    """
    if scope == 'track':
        t = (s or '').lower()
        t = _TRACK_FEAT_RE.sub('', t)
        return _NONALNUM_RE.sub('', t)

    s = _prepass(s)

    if scope == 'artist':
        s = re.sub(r'\s+', ' ', s).strip()
        s = s.casefold()
        s = _ARTIST_SEPS_RE.split(s)[0]
        return s.strip(" -_.")

    # scope == 'album'
    s = _APMU_RE.sub('', s)
    s = _CURLY_RE.sub('', s)
    s = _strip_format_brackets(s)           # bracket = pure format-noise only
    s = _TRAIL_YEAR_RE.sub('', s)           # single trailing bare year only
    s = re.sub(r'\s+', ' ', s).strip()
    s = s.casefold()
    return s.strip(" -_.")


def is_va(albumartist: str) -> bool:
    a = normalize(albumartist, 'artist')
    return a == '' or bool(_VA_RE.match(a))


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Identity:
    key: str
    tier: int                       # 1=MBID, 2=content-hash, 3=review
    confidence: float
    review_reason: Optional[str] = None
    canonical_mbid: Optional[str] = None


@dataclass
class AlbumRow:
    album_id: int
    albumartist: str
    album: str
    year: Optional[int]
    mb_albumid: str
    mb_releasegroupid: str
    items: list = field(default_factory=list)   # list[dict(disc,track,title,path)]


@dataclass
class LibraryAlbum:
    identity_key: str
    album_ids: list
    albumartist: str
    album: str
    year: Optional[int]
    have_paths: list
    tier: int
    confidence: float


# ─────────────────────────────────────────────────────────────────────────────
# Per-album base identity (no group context)
# ─────────────────────────────────────────────────────────────────────────────
def _valid(mbid: str) -> bool:
    return bool(mbid) and bool(UUID_RE.match(mbid.strip()))


def base_identity(a: AlbumRow) -> Identity:
    """Tier-1 (MBID) or tier-2 (content-hash base, no disc-suffix/convergence).
    Group-level passes in build_index() refine this."""
    rg = (a.mb_releasegroupid or '').strip()
    al = (a.mb_albumid or '').strip()
    if _valid(rg):
        return Identity('mbrg:' + rg.lower(), 1, 1.0,
                        canonical_mbid=al if _valid(al) else None)
    if _valid(al):
        return Identity('mbid:' + al.lower(), 1, 1.0, canonical_mbid=al.lower())

    artist_key = VA_SENTINEL if is_va(a.albumartist) else normalize(a.albumartist, 'artist')
    album_key = normalize(a.album, 'album')

    # hard fail-to-review gates that don't need siblings
    if album_key == '':
        return Identity(f'review:{a.album_id}', 3, 0.0, review_reason='empty-album-key')
    if artist_key in _UNKNOWN_ARTISTS and not (_valid(rg) or _valid(al)):
        if artist_key == '':
            return Identity(f'review:{a.album_id}', 3, 0.0, review_reason='no-artist')

    body = hashlib.sha256((artist_key + SEP + album_key).encode('utf-8')).hexdigest()[:16]
    return Identity('ch:' + body, 2, 0.95, canonical_mbid=al.lower() if _valid(al) else None)


# convenience for inbound (single) albums / tags
def release_identity(a: AlbumRow) -> Identity:
    """Resolve a standalone album/tags to its base identity (the inbound-matching
    common case). Disc-suffix + convergence apply at index time; for a single
    inbound folder the base key is what you match against in_library()."""
    return base_identity(a)


# ─────────────────────────────────────────────────────────────────────────────
# Group-level helpers
# ─────────────────────────────────────────────────────────────────────────────
def _disc_set(a: AlbumRow) -> set:
    return {(it.get('disc') or 0) for it in a.items}


def _norm_titleset(a: AlbumRow) -> set:
    return {normalize(it.get('title') or '', 'track') for it in a.items
            if (it.get('title') or '').strip()}


def _is_corrupt(a: AlbumRow) -> bool:
    """intra-row (disc,track) collisions holding DIFFERENT normalized titles."""
    seen = {}
    for it in a.items:
        slot = (it.get('disc') or 0, it.get('track') or 0)
        nt = normalize(it.get('title') or '', 'track')
        if slot in seen and seen[slot] != nt:
            return True
        seen[slot] = nt
    return False


# ─────────────────────────────────────────────────────────────────────────────
# build_index() — the oracle. release_identity over EVERY album, then group passes.
# ─────────────────────────────────────────────────────────────────────────────
def build_index(albums: list) -> dict:
    """albums: list[AlbumRow]. Returns dict with:
       'by_album': {album_id: final_key}
       'index':    {key: LibraryAlbum}
    """
    base = {a.album_id: base_identity(a) for a in albums}
    rows = {a.album_id: a for a in albums}
    final_key = {}

    # 1. base pass (tier-1 keys are final unless RG; tier-3 stay tier-3)
    for aid, ident in base.items():
        final_key[aid] = ident.key

    # 2. CONVERGENCE pass: a tier-2 base bucket containing a tier-1 member
    #    (MBID/RG) collapses the bare members onto the tier-1 key when their
    #    (artist_key, album_key) match (= same content-hash body).
    #    We attach bare members whose ch: body matches a sibling that ALSO carries
    #    a valid MBID. Build: ch-body -> {tier1 keys present, member ids}.
    ch_bucket = defaultdict(list)
    for aid, ident in base.items():
        if ident.tier == 2:
            ch_bucket[ident.key].append(aid)
    # for tier-1 members, compute what their ch-body WOULD be so bare siblings attach
    tier1_chbody = {}
    for aid, ident in base.items():
        if ident.tier == 1:
            a = rows[aid]
            artist_key = VA_SENTINEL if is_va(a.albumartist) else normalize(a.albumartist, 'artist')
            album_key = normalize(a.album, 'album')
            if album_key:
                body = 'ch:' + hashlib.sha256((artist_key + SEP + album_key).encode('utf-8')).hexdigest()[:16]
                tier1_chbody.setdefault(body, []).append((aid, ident.key))

    for chkey, members in ch_bucket.items():
        if chkey in tier1_chbody:
            # attach all tier-2 members of this body to the tier-1 key
            t1key = tier1_chbody[chkey][0][1]
            for aid in members:
                final_key[aid] = t1key

    # 3. DISC-SUFFIX pass: within a base-ch bucket (only the still-tier-2 ones),
    #    if >=2 album_ids each occupy a SINGLE distinct disc > 1 (pairwise disjoint),
    #    suffix each with #disc=N so they get distinct identities (Genshin).
    live_buckets = defaultdict(list)
    for aid, ident in base.items():
        if final_key[aid] == ident.key and ident.tier == 2:   # still bare tier-2
            live_buckets[ident.key].append(aid)
    for chkey, members in live_buckets.items():
        if len(members) < 2:
            continue
        singleton_disc = {}
        for aid in members:
            ds = _disc_set(rows[aid])
            if len(ds) == 1:
                d = next(iter(ds))
                if d and d > 1:
                    singleton_disc[aid] = d
        # need >=2 members each on a distinct disc>1
        if len(singleton_disc) >= 2 and len(set(singleton_disc.values())) == len(singleton_disc):
            for aid, d in singleton_disc.items():
                final_key[aid] = f'{chkey}#disc={d}'

    # 4. FAIL-TO-REVIEW pass: corruption forces review of destructive ops but NOT
    #    of the identity grouping (per spec rule 3). We keep the grouping key but
    #    record corruption on the LibraryAlbum. Other hard gates already set tier-3
    #    in base_identity. (Heuristic gates 6/7/8/10 left for reconcile to apply
    #    with full sibling context.)

    # 5. assemble index
    groups = defaultdict(list)
    for aid in final_key:
        groups[final_key[aid]].append(aid)

    index = {}
    for key, aids in groups.items():
        members = [rows[a] for a in aids]
        # pick representative metadata: prefer an MBID-bearing member
        rep = next((m for m in members if _valid(m.mb_albumid) or _valid(m.mb_releasegroupid)), members[0])
        tier = min(base[a].tier for a in aids)
        conf = max(base[a].confidence for a in aids)
        have_paths = []
        for m in members:
            for it in m.items:
                p = it.get('path')
                if p:
                    have_paths.append(p)
        years = [m.year for m in members if m.year]
        index[key] = LibraryAlbum(
            identity_key=key, album_ids=sorted(aids),
            albumartist=rep.albumartist, album=rep.album,
            year=(max(set(years), key=years.count) if years else None),
            have_paths=have_paths, tier=tier, confidence=conf)

    return {'by_album': final_key, 'index': index}


# ─────────────────────────────────────────────────────────────────────────────
# Loading from beets + the public oracle
# ─────────────────────────────────────────────────────────────────────────────
def load_albums(db_path: str = BEETS_DB) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    arows = conn.execute(
        "SELECT id, albumartist, album, year, mb_albumid, mb_releasegroupid FROM albums").fetchall()
    items_by_album = defaultdict(list)
    for r in conn.execute("SELECT album_id, disc, track, title, path FROM items"):
        p = r[4]
        if isinstance(p, bytes):
            p = p.decode('utf-8', 'replace')
        items_by_album[r[0]].append({'disc': r[1], 'track': r[2], 'title': r[3], 'path': p})
    conn.close()
    out = []
    for r in arows:
        out.append(AlbumRow(
            album_id=r['id'], albumartist=r['albumartist'] or '', album=r['album'] or '',
            year=r['year'], mb_albumid=r['mb_albumid'] or '',
            mb_releasegroupid=r['mb_releasegroupid'] or '',
            items=items_by_album.get(r['id'], [])))
    return out


_INDEX_CACHE = None

def get_index(db_path: str = BEETS_DB, rebuild: bool = False) -> dict:
    global _INDEX_CACHE
    if _INDEX_CACHE is None or rebuild:
        _INDEX_CACHE = build_index(load_albums(db_path))
    return _INDEX_CACHE


def in_library(key: str, db_path: str = BEETS_DB) -> Optional[LibraryAlbum]:
    """The single oracle. Returns the LibraryAlbum for an identity key, or None."""
    return get_index(db_path)['index'].get(key)
