"""
Auto-match unmatched Audiobookshelf items via an external metadata provider
(default: OpenLibrary).

Run modes:
  match-all              walk every Book-type library, match anything without
                          an OLID/ISBN/ASIN. Idempotent — safe to run on a timer.
  match-item <itemId>    target one specific library item.
  scan                    trigger a library-folder scan and exit (used right
                          after the pipeline routes a new audiobook).
  scan-and-match          scan, wait briefly, then match-all (one-shot for
                          ad-hoc backfills).

Fail-open everywhere: any network / API error is logged and shrugged off.
This is best-effort cosmetic enrichment, not load-bearing pipeline state.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config as cfg

LOG_FILE       = cfg.ABS_AUTOMATCH_LOG
RATE_LIMIT_S   = 1.5          # be a good citizen with OpenLibrary
MATCH_TIMEOUT  = 30
SCAN_SETTLE_S  = 8            # seconds to wait after triggering a scan

_log_fh = None


def setup_logging():
    global _log_fh
    _log_fh = cfg.open_log_file(LOG_FILE)


def log(msg: str, level: str = 'INFO'):
    ts   = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} [{level}] {msg}'
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + '\n')
        _log_fh.flush()


def _token() -> str:
    """Resolve the ABS API token. Prefer env, fall back to root user's token
    in the local sqlite DB. Token rotation is the user's call — we just read
    whatever is there."""
    if cfg.ABS_TOKEN:
        return cfg.ABS_TOKEN
    try:
        with sqlite3.connect(str(cfg.ABS_DB_PATH)) as con:
            row = con.execute(
                "SELECT token FROM users WHERE type='root' AND token IS NOT NULL "
                "ORDER BY createdAt LIMIT 1"
            ).fetchone()
            return row[0] if row else ''
    except sqlite3.Error as e:
        log(f'could not read ABS sqlite DB: {e}', 'WARN')
        return ''


def _request(method: str, path: str, body: dict | None = None,
             timeout: int = MATCH_TIMEOUT):
    url     = cfg.ABS_URL.rstrip('/') + path
    headers = {'Authorization': f'Bearer {_token()}'}
    data    = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read()
        return json.loads(text) if text else {}


def list_book_libraries() -> list[dict]:
    payload = _request('GET', '/api/libraries')
    return [lib for lib in payload.get('libraries', []) if lib.get('mediaType') == 'book']


def iter_library_items(library_id: str):
    """Paginate all items in a library."""
    page  = 0
    limit = 100
    while True:
        params = urllib.parse.urlencode({'limit': limit, 'page': page})
        payload = _request('GET', f'/api/libraries/{library_id}/items?{params}')
        results = payload.get('results', [])
        if not results:
            return
        for item in results:
            yield item
        if len(results) < limit:
            return
        page += 1


def _author_name(meta: dict) -> str:
    """Read author from the structured `authors` array, falling back to
    the legacy `authorName` shortcut. ABS populates either depending on age."""
    authors = meta.get('authors') or []
    if authors and isinstance(authors, list):
        first = authors[0]
        if isinstance(first, dict) and first.get('name'):
            return first['name'].strip()
    return (meta.get('authorName') or '').strip()


def needs_match(item: dict) -> bool:
    """Return True if the item still looks unmatched. ABS 2.34 does not write
    an `olid` for OpenLibrary matches, so we fall back to a description-as-proxy
    heuristic: a non-trivial description means the metadata fetch has run at
    least once. Avoids re-matching the same item every timer tick."""
    meta = (item.get('media') or {}).get('metadata') or {}
    if any(meta.get(k) for k in ('asin', 'isbn', 'olid')):
        return False
    desc = (meta.get('description') or '').strip()
    return len(desc) < 80  # arbitrary "looks empty" threshold


def match_item(item: dict, provider: str) -> bool:
    """POST a single-item match against `provider`. Returns True if updated."""
    item_id = item.get('id')
    meta    = (item.get('media') or {}).get('metadata') or {}
    title   = (meta.get('title') or '').strip()
    author  = _author_name(meta)
    if not item_id or not title:
        return False
    # overrideCover=true is safe — fetched covers replace nothing of value.
    # overrideDetails=false preserves local author/series/etc when the
    # provider response is sparse (OpenLibrary often returns thin payloads).
    body = {
        'provider':        provider,
        'title':           title,
        'author':          author,
        'overrideCover':   True,
        'overrideDetails': False,
    }
    try:
        resp = _request('POST', f'/api/items/{item_id}/match', body=body)
    except urllib.error.HTTPError as e:
        log(f'[MATCH-FAIL] {item_id} "{title}" — HTTP {e.code}', 'WARN')
        return False
    except Exception as e:
        log(f'[MATCH-FAIL] {item_id} "{title}" — {e}', 'WARN')
        return False
    return bool(resp.get('updated'))


def match_library(library: dict, provider: str) -> tuple[int, int, int]:
    """Walk one library and match any items lacking IDs. Returns (matched, skipped, errors)."""
    matched = skipped = errors = 0
    for item in iter_library_items(library['id']):
        meta = (item.get('media') or {}).get('metadata') or {}
        title = meta.get('title') or '?'
        if not needs_match(item):
            skipped += 1
            continue
        ok = match_item(item, provider)
        if ok:
            matched += 1
            log(f'[MATCH] {library["name"]} "{title}" ← {provider}')
        else:
            errors += 1
            log(f'[NO-MATCH] {library["name"]} "{title}" (provider={provider})')
        time.sleep(RATE_LIMIT_S)
    return matched, skipped, errors


def scan_library(library_id: str) -> bool:
    try:
        _request('POST', f'/api/libraries/{library_id}/scan')
        return True
    except Exception as e:
        log(f'[SCAN-FAIL] library={library_id} — {e}', 'WARN')
        return False


def trigger_scan_all() -> int:
    """Kick off a scan on every book library. Used by the post-route hook."""
    libs = list_book_libraries()
    n = 0
    for lib in libs:
        if scan_library(lib['id']):
            log(f'[SCAN] triggered for "{lib["name"]}"')
            n += 1
    return n


def match_all(provider: str | None = None) -> dict:
    if provider is None:
        provider = cfg.ABS_PROVIDER
    libs = list_book_libraries()
    if not libs:
        log('no book libraries found')
        return {'libraries': 0, 'matched': 0, 'skipped': 0, 'errors': 0}
    total_m = total_s = total_e = 0
    for lib in libs:
        m, s, e = match_library(lib, provider)
        total_m += m; total_s += s; total_e += e
    log(f'[SUMMARY] libraries={len(libs)} matched={total_m} '
        f'already_matched={total_s} no_match={total_e}')
    return {'libraries': len(libs), 'matched': total_m,
            'skipped': total_s, 'errors': total_e}


def scan_and_match(provider: str | None = None,
                    settle_seconds: int = SCAN_SETTLE_S) -> dict:
    trigger_scan_all()
    time.sleep(settle_seconds)
    return match_all(provider)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('match-all', 'match-item', 'scan',
                                     'scan-and-match'),
                    nargs='?', default='match-all')
    ap.add_argument('arg', nargs='?', help='item-id for match-item')
    ap.add_argument('--provider', default=cfg.ABS_PROVIDER)
    args = ap.parse_args()

    if not _token():
        log('no ABS token available (set ABS_TOKEN env or check ABS_DB_PATH)', 'ERROR')
        return 1

    log(f'===== abs-automatch {args.cmd} provider={args.provider} =====')

    if args.cmd == 'match-all':
        match_all(args.provider)
    elif args.cmd == 'match-item':
        if not args.arg:
            log('match-item requires an item-id', 'ERROR')
            return 2
        try:
            payload = _request('GET', f'/api/items/{args.arg}')
        except Exception as e:
            log(f'fetch item failed: {e}', 'ERROR')
            return 3
        if not needs_match(payload):
            log(f'item {args.arg} already has an ID — nothing to do')
            return 0
        ok = match_item(payload, args.provider)
        log(f'[MATCH-ITEM] {args.arg} -> updated={ok}')
    elif args.cmd == 'scan':
        trigger_scan_all()
    elif args.cmd == 'scan-and-match':
        scan_and_match(args.provider)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
