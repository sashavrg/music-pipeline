"""Mark wishlist rows as fulfilled when their albums land in the beets library.

The pipeline historically never wrote `wishlist.fulfilled_at`; only
`last_queued` updated. That meant once-fulfilled albums stayed pending forever
and the daily wishlist-check re-queried them every cooldown cycle.

This module supplies two entry points:

  * `mark_fulfilled_since(pre_max_id)` — called from beets-import.sh right
    after the `[VERIFY] beet import added N items` line, passing the
    `MAX(items.id)` captured before `beet import`. Fast: pulls only items
    with id beyond that watermark. MUST be max-id, never count(*) — items.id
    is autoincrement and `beet remove` leaves gaps.

  * CLI `--retroactive` — one-shot pass over ALL beets albums, useful right
    after this code lands so existing pending entries that already have a
    library match get cleaned up immediately.

Matching is intentionally conservative: a wishlist row is only fulfilled when
the normalized albumartist tokens overlap AND the normalized album either
fully matches or is a strong prefix. False positives are worse than misses —
a missed fulfillment merely re-queries on the next cycle.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unicodedata

from . import config as cfg
from . import db as pipeline_db


# Wishlist-side artist labels that should match any beets albumartist for the
# same album. Soulseek + beets disagree all the time on Various Artists vs the
# actual featured artist credit, so we don't gate on artist for these.
_VARIOUS_LABELS = {'various', 'various artists', 'va', ''}


def _strip_diacritics(s: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFKD', s)
        if not unicodedata.combining(c)
    )


def normalize(s: str | None) -> str:
    """Lowercase, strip diacritics, drop punctuation, collapse whitespace."""
    if not s:
        return ''
    s = _strip_diacritics(s).lower()
    # treat slash / hyphen / underscore as word breaks
    s = re.sub(r'[\W_]+', ' ', s, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', s).strip()


def _tokens(s: str) -> set[str]:
    return {t for t in normalize(s).split() if len(t) > 1}


def artist_matches(wish_artist: str, lib_albumartist: str) -> bool:
    wa = normalize(wish_artist)
    la = normalize(lib_albumartist)
    if not wa or not la:
        return False
    if wa in _VARIOUS_LABELS or la in _VARIOUS_LABELS:
        # If either side declares Various, defer to album-only match.
        return True
    # Token-subset either direction handles "Torus" vs "DJ LOSTBOI & Torus"
    # and "blink-182" vs "Blink 182".
    wt, lt = _tokens(wish_artist), _tokens(lib_albumartist)
    if not wt or not lt:
        return False
    return wt.issubset(lt) or lt.issubset(wt)


def album_matches(wish_album: str, lib_album: str) -> bool:
    wa = normalize(wish_album)
    la = normalize(lib_album)
    if not wa or not la:
        return False
    if wa == la:
        return True
    # Strong prefix in either direction (handles "Kern Vol.3" vs "Kern, Vol. 3"
    # or wishlist carrying extra "(Deluxe Edition)" suffix the library lacks).
    short, long = (wa, la) if len(wa) <= len(la) else (la, wa)
    if len(short) >= 6 and long.startswith(short):
        return True
    # Token-subset for re-orderings — require shared signal: shorter side has
    # at least 2 tokens AND every short-side token appears in the long side.
    wt = [t for t in wa.split() if len(t) > 1]
    lt = [t for t in la.split() if len(t) > 1]
    short_t, long_t = (wt, lt) if len(wt) <= len(lt) else (lt, wt)
    if len(short_t) >= 2 and all(t in long_t for t in short_t):
        return True
    return False


def find_fulfilled_ids(pending: list[dict],
                       library_albums: list[tuple[str, str]]) -> set[int]:
    """Given pending wishlist rows and (albumartist, album) tuples from beets,
    return the set of wishlist ids that are now satisfied."""
    matched: set[int] = set()
    for row in pending:
        for laa, lal in library_albums:
            if artist_matches(row['artist'], laa) and album_matches(row['album'], lal):
                matched.add(row['id'])
                break
    return matched


# ── Beets DB access ──────────────────────────────────────────────────────────

def _beets_albums_since(pre_max_id: int) -> list[tuple[str, str]]:
    """Pull (albumartist, album) tuples for items whose id > pre_max_id.

    Beets autoincrements items.id on import. The caller must pass MAX(id)
    captured BEFORE the import, not COUNT(*) — the two diverge whenever
    `beet remove` has run (which leaves id gaps). Joining via items.album_id
    keeps us inside the albums table for canonical album-level metadata.
    """
    with sqlite3.connect(cfg.BEETS_DB, timeout=15) as con:
        rows = con.execute(
            'SELECT DISTINCT a.albumartist, a.album '
            'FROM items i JOIN albums a ON i.album_id = a.id '
            'WHERE i.id > ?',
            (pre_max_id,),
        ).fetchall()
    return [(aa or '', al or '') for aa, al in rows]


def _beets_all_albums() -> list[tuple[str, str]]:
    with sqlite3.connect(cfg.BEETS_DB, timeout=30) as con:
        rows = con.execute('SELECT albumartist, album FROM albums').fetchall()
    return [(aa or '', al or '') for aa, al in rows]


# ── Entry points ─────────────────────────────────────────────────────────────

def _mark(ids: set[int], pending: list[dict]) -> list[dict]:
    """Mark each id as fulfilled; return the rows that were marked (for logging)."""
    by_id = {r['id']: r for r in pending}
    marked = []
    for wid in sorted(ids):
        pipeline_db.fulfill_wishlist(wid)
        if wid in by_id:
            marked.append(by_id[wid])
    return marked


def mark_fulfilled_since(pre_max_id: int) -> list[dict]:
    pipeline_db.init_db()
    pending = pipeline_db.get_wishlist_pending()
    if not pending:
        return []
    albums = _beets_albums_since(pre_max_id)
    if not albums:
        return []
    return _mark(find_fulfilled_ids(pending, albums), pending)


def mark_fulfilled_retroactive() -> list[dict]:
    pipeline_db.init_db()
    pending = pipeline_db.get_wishlist_pending()
    if not pending:
        return []
    return _mark(find_fulfilled_ids(pending, _beets_all_albums()), pending)


def main():
    parser = argparse.ArgumentParser(
        description='Mark wishlist rows fulfilled by matching beets library.'
    )
    parser.add_argument(
        '--since', type=int, metavar='MAX_ID',
        help='Only consider beets items with id > MAX_ID (post-import mode). '
             'Pass the MAX(items.id) captured before `beet import` ran.',
    )
    parser.add_argument(
        '--retroactive', action='store_true',
        help='Scan the entire beets library against pending wishlist.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show matches without marking them fulfilled.',
    )
    args = parser.parse_args()

    if args.since is None and not args.retroactive:
        parser.error('pick --since N or --retroactive')

    pipeline_db.init_db()
    pending = pipeline_db.get_wishlist_pending()
    if not pending:
        print('No pending wishlist entries.', file=sys.stderr)
        return 0

    albums = (_beets_all_albums() if args.retroactive
              else _beets_albums_since(args.since))
    ids = find_fulfilled_ids(pending, albums)

    by_id = {r['id']: r for r in pending}
    for wid in sorted(ids):
        r = by_id[wid]
        verb = 'WOULD-FULFILL' if args.dry_run else 'FULFILLED'
        print(f'[{verb}] #{wid} {r["artist"]} - {r["album"]}')
        if not args.dry_run:
            pipeline_db.fulfill_wishlist(wid)
    print(f'[SUMMARY] {len(ids)}/{len(pending)} wishlist rows matched '
          f'({"dry-run" if args.dry_run else "marked fulfilled"})',
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
