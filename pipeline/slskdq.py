#!/usr/bin/env python3
"""
slskdq.py — the single in-flight ledger + admission gate for slskd downloads.

WHY THIS EXISTS
---------------
Before rebuild phase 6, five producers — recover, incomplete-watchdog,
quarantine-requeue, fill-missing-tracks, wishlist-fulfillment — each POSTed to
slskd's one download queue using its own per-stage cooldown table, blind to the
others. That is structural fault line #3 of the 2026-06-13 rethink: "no global
in-flight ledger". The same album gets queued by two stages; shared fixes
(generic-query guard, MB-canonical retry) live in only some paths; nothing
coordinates capacity. This module is the chokepoint every producer calls FIRST,
and only POSTs to slskd if admitted.

THE GATE — enqueue() refuses if ANY of these trips, in this order:
  1. generic-query — (artist, album) is too generic/empty to search safely
  2. in-library   — we already own this album (identity oracle, convergence-aware)
  3. cooling      — a recent terminal event for this identity (completed-settle or
                    failure back-off) means: don't re-queue yet
  4. capacity     — slskd already has >= MAX_PENDING_DL transfers in flight
  5. in-flight    — a live ledger row already exists for this identity. Enforced
                    ATOMICALLY by the partial-unique index (db.ledger_insert ->
                    None on a race), so two producers cannot both win.

IDENTITY KEYING
---------------
A wanted album (we don't have the bytes yet — only artist+album strings) is keyed
exactly the way reconcile keys candidates: an AlbumRow built from (artist, album)
-> identity.base_identity (a deterministic ch: content-hash key). Keying by
IDENTITY rather than folder name is what lets the gate see across producers AND
match the library. The in-library check goes one step further and runs
identity.build_index(library + candidate) so a bare ch: candidate still matches a
library album that happens to be keyed by MBID/release-group (the convergence
pass) — same logic reconcile uses, so the gate and the writer agree.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not pick WHICH folder to download — each producer keeps its own quality
scoring and passes a `post` callable that performs the actual slskd POST
(recover.queue_download / incomplete_watchdog.queue_download). slskdq only decides
admit/refuse, records the ledger row, and (in poll) advances live rows toward a
terminal state from slskd's transfer list so cooldowns arm and the in-flight slot
frees. poll() is best-effort: the AUTHORITATIVE "do we already have it" is the
in-library check at admit time, so an imperfect poll can never create a duplicate
— only a slightly slower retry.

SAFETY: the autonomy timers that drive the producers are still DISABLED. This
module is inert until an operator re-enables them one at a time. Re-enabling
autonomy is the exact failure that triggered the rebuild — it is a separate,
deliberately-gated step, not part of shipping the ledger.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Callable, Optional

from . import config as cfg
from . import db
from . import identity

BEETS_DB = identity.BEETS_DB

# Producers and the ledger 'source' column values.
SOURCES = ('recover', 'incomplete', 'quarantine', 'fill', 'wishlist', 'manual')

# Decision.state values (machine-readable outcomes).
ADMITTED        = 'admitted'
WOULD_ADMIT     = 'would_admit'        # dry-run: the gate would have admitted
REFUSED_GENERIC = 'refused:generic'
REFUSED_LIBRARY = 'refused:in_library'
REFUSED_COOLING = 'refused:cooling'
REFUSED_CAPACITY = 'refused:capacity'
REFUSED_INFLIGHT = 'refused:in_flight'
POST_FAILED     = 'post_failed'


@dataclass
class Decision:
    admitted: bool          # True only when a real download was actually enqueued
    state: str              # one of the constants above
    identity_key: str
    tier: int
    reason: str
    rowid: Optional[int] = None

    def __bool__(self) -> bool:
        return self.admitted


# ─────────────────────────────────────────────────────────────────────────────
# slskd REST (minimal, urllib — keeps this a leaf module: no producer imports,
# so there is no import cycle. Mirrors recover.api_get's auth + timeout.)
# ─────────────────────────────────────────────────────────────────────────────
def _api_get(path: str):
    req = urllib.request.Request(cfg.SLSKD_URL + path, headers=db.slskd_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def _transfers() -> list:
    data = _api_get('/api/v0/transfers/downloads')
    return data if isinstance(data, list) else []


# slskd file-state strings look like "Completed, Succeeded", "Completed, Errored",
# "Completed, Cancelled", "InProgress", "Queued, Remotely", "Requested", ...
def _is_active(s: str) -> bool:
    return any(k in s for k in ('InProgress', 'Initializing', 'Requested'))


def _is_live_state(s: str) -> bool:
    return 'Completed' not in s and any(
        k in s for k in ('Queued', 'InProgress', 'Requested', 'Initializing'))


def _is_success(s: str) -> bool:
    return 'Succeeded' in s


def pending_download_count() -> int:
    """slskd's own count of transfers still queued or in progress (capacity gate)."""
    n = 0
    for u in _transfers():
        for d in u.get('directories', []):
            for f in d.get('files', []):
                if _is_live_state(f.get('state', '')):
                    n += 1
    return n


def _dirkey(s: str) -> str:
    """Last path component, case-folded, slash-normalized — slskd peer dirs use
    backslashes; we match on basename so host/peer path-style differences don't
    break poll mapping."""
    s = (s or '').replace('\\', '/').rstrip('/').lower()
    return s.rsplit('/', 1)[-1]


def busy_local_dirs() -> Optional[set]:
    """Local download-folder names (lowercased basenames) that may still be
    receiving files and must NOT be swept by reconcile: every slskd download
    dir with a non-terminal file, plus the expected dir of every live ledger
    row (covers the admit→first-transfer-record window). slskd quiesces the
    local folder between per-file moves from its incomplete dir, so folder
    mtime alone cannot distinguish "settled" from "mid-transfer" — this set is
    the ground truth. Returns None when the transfers API is unreachable:
    unknown is NOT the same as empty, and the caller must treat it as
    unsafe-to-sweep."""
    data = _api_get('/api/v0/transfers/downloads')
    if not isinstance(data, list):
        return None
    busy = set()
    for u in data:
        for d in u.get('directories', []):
            if any(_is_live_state(f.get('state', '')) for f in d.get('files', [])):
                busy.add(_dirkey(d.get('directory', '')))
    db.init_db()
    for row in db.ledger_live_rows():
        if row['remote_dir']:
            busy.add(_dirkey(row['remote_dir']))
    return busy


def _dir_file_states(transfers: list, username: str, remote_dir: str) -> list:
    """slskd file-state strings for username+remote_dir, or [] if not present."""
    want = _dirkey(remote_dir)
    for u in transfers:
        if u.get('username') != username:
            continue
        for d in u.get('directories', []):
            if not remote_dir or _dirkey(d.get('directory', '')) == want:
                return [f.get('state', '') for f in d.get('files', [])]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Identity resolution for a WANTED album
# ─────────────────────────────────────────────────────────────────────────────
_LIB_CACHE: Optional[list] = None


def _library_albums(db_path: str) -> list:
    """Cached library AlbumRows (per process). Producers are short-lived script
    runs, so a per-process cache is safe; call refresh_library() to invalidate."""
    global _LIB_CACHE
    if _LIB_CACHE is None:
        _LIB_CACHE = identity.load_albums(db_path)
    return _LIB_CACHE


def refresh_library():
    global _LIB_CACHE
    _LIB_CACHE = None


def _candidate_row(artist: str, album: str):
    """The same AlbumRow shape reconcile builds for a scan candidate, with a
    negative sentinel id (library ids are positive, so it can't collide)."""
    return identity.AlbumRow(-1, artist or '', album or '', 0, '', '', [])


def ledger_key(artist: str, album: str) -> str:
    """Deterministic in-flight key for a wanted album — independent of library
    state, so every producer computes the SAME key for the same (artist, album)
    and the in-flight gate dedups them."""
    return identity.base_identity(_candidate_row(artist, album)).key


def resolve_in_library(artist: str, album: str, db_path: str = BEETS_DB):
    """Returns (resolved_key, owned: bool). owned=True means the library already
    contains this album. Uses build_index(library + candidate) so a bare ch:
    candidate still matches an MBID/release-group-keyed library album (the same
    convergence reconcile applies)."""
    cand = _candidate_row(artist, album)
    idx = identity.build_index(_library_albums(db_path) + [cand])
    by_album = idx['by_album']
    cand_key = by_album.get(cand.album_id)
    owned = any(aid != cand.album_id and k == cand_key for aid, k in by_album.items())
    return cand_key, owned


# ─────────────────────────────────────────────────────────────────────────────
# Generic-query guard (consolidated here — was duplicated/missing across stages)
# ─────────────────────────────────────────────────────────────────────────────
def is_generic_query(artist: str, album: str) -> bool:
    """A query too generic to target a specific release safely. Conservative
    backstop: trips only on clearly-unsafe input (empty artist, or an album that
    normalizes to empty / a single character). A real 'Artist / Greatest Hits'
    passes — the non-empty artist makes it specific enough."""
    a = identity.normalize(artist or '', 'artist')
    al = identity.normalize(album or '', 'album')
    if not a or not al:
        return True
    if len(al) < 2:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown
# ─────────────────────────────────────────────────────────────────────────────
def _cooldown_reason(identity_key: str, now: float) -> Optional[str]:
    """If the identity is inside a cooldown window, return a human reason; else
    None. completed -> short settle (awaiting import); failure -> back-off."""
    last = db.ledger_last_terminal(identity_key)
    if not last:
        return None
    state, t = last
    age_h = (now - t) / 3600.0
    if state == 'completed' and age_h < cfg.LEDGER_COMPLETED_SETTLE_H:
        return f'completed {age_h:.1f}h ago, settle<{cfg.LEDGER_COMPLETED_SETTLE_H}h (awaiting import)'
    if state in ('failed', 'cancelled', 'expired') and age_h < cfg.LEDGER_FAILURE_COOLDOWN_H:
        return f'{state} {age_h:.1f}h ago, cooldown<{cfg.LEDGER_FAILURE_COOLDOWN_H}h'
    return None


# ─────────────────────────────────────────────────────────────────────────────
# THE GATE
# ─────────────────────────────────────────────────────────────────────────────
def _gate(artist: str, album: str, key0: str, tier: int, now: float, *,
          skip_in_library: bool, skip_cooldown: bool,
          max_pending: Optional[int], db_path: str):
    """Run checks 1-4 (everything except the atomic in-flight INSERT). Returns
    (resolved_key, refusal_Decision_or_None). A None refusal means the gate is
    clear up to the in-flight claim."""
    def refuse(state, reason, key):
        return key, Decision(False, state, key, tier, reason)

    # 1. generic query
    if is_generic_query(artist, album):
        return refuse(REFUSED_GENERIC, f'query too generic: {artist!r} / {album!r}', key0)

    # 2. already in library (convergence-aware oracle). fill-missing legitimately
    #    re-downloads tracks for an album we partially own -> skip_in_library.
    resolved_key, owned = resolve_in_library(artist, album, db_path)
    key = resolved_key or key0
    if owned and not skip_in_library:
        return refuse(REFUSED_LIBRARY, 'already in library', key)

    # 3. cooldown (completed-settle / failure back-off)
    if not skip_cooldown:
        cool = _cooldown_reason(key, now)
        if cool:
            return refuse(REFUSED_COOLING, cool, key)

    # 4. capacity
    cap = cfg.MAX_PENDING_DL if max_pending is None else max_pending
    pending = pending_download_count()
    if pending >= cap:
        return refuse(REFUSED_CAPACITY, f'{pending} in flight >= cap {cap}', key)

    # 5. in-flight (cheap pre-check; the atomic guard is the INSERT in claim())
    if db.ledger_live(key) is not None:
        return refuse(REFUSED_INFLIGHT, 'identity already in flight', key)

    return key, None


def claim(artist: str, album: str, *, source: str,
          skip_in_library: bool = False, skip_cooldown: bool = False,
          username: str = '', remote_dir: str = '', file_count: int = 0,
          max_pending: Optional[int] = None, db_path: str = BEETS_DB) -> Decision:
    """Run the gate and, if clear, ATOMICALLY claim the in-flight slot WITHOUT
    posting. Returns a Decision with .rowid set on success. The caller then
    performs its own slskd POST(s) and MUST call settle(rowid, ok) afterward.

    Use this for producers that queue from MULTIPLE sources per album in one run
    (fill-missing-tracks): one ledger row covers the whole album so the second
    source isn't refused as in-flight against the first. Single-POST producers
    should use the enqueue() wrapper instead."""
    db.init_db()
    now = time.time()
    key0 = ledger_key(artist, album)
    tier = identity.base_identity(_candidate_row(artist, album)).tier
    key, refusal = _gate(artist, album, key0, tier, now,
                         skip_in_library=skip_in_library, skip_cooldown=skip_cooldown,
                         max_pending=max_pending, db_path=db_path)
    if refusal is not None:
        return refusal
    rowid = db.ledger_insert(key, artist, album, source, username, remote_dir, file_count)
    if rowid is None:                     # lost the atomic race to another producer
        return Decision(False, REFUSED_INFLIGHT, key, tier, 'lost in-flight race')
    return Decision(True, ADMITTED, key, tier, 'claimed', rowid)


def settle(rowid: int, ok: bool, note: str = ''):
    """Finalize a claim() after the caller's POST(s). ok=True leaves the row LIVE
    ('queued') for poll() to terminalize against slskd's transfer list; ok=False
    removes the claim entirely — a failed POST is a transient API hiccup, not a
    download failure, so it imposes no cooldown and the next run retries."""
    if ok:
        if note:
            db.ledger_set_state(rowid, 'queued', note)
    else:
        db.ledger_delete(rowid)


def enqueue(artist: str, album: str, *, source: str,
            post: Optional[Callable[[], bool]] = None,
            skip_in_library: bool = False, skip_cooldown: bool = False,
            username: str = '', remote_dir: str = '', file_count: int = 0,
            max_pending: Optional[int] = None, execute: bool = True,
            db_path: str = BEETS_DB) -> Decision:
    """Admit-or-refuse a wanted album and, when admitted, claim the in-flight slot
    and run `post` to POST the download to slskd. The single-POST convenience
    wrapper around claim()+settle().

    post: zero-arg callable returning True on a successful slskd POST. The producer
          keeps full control of WHICH folder/files to send; slskdq only gates and
          records. Required when execute=True.
    execute=False: dry-run — run the gate and report what WOULD happen, touching
          neither the ledger nor slskd (used by `slskdq --check`).

    bool(Decision) is True only on a real, successful enqueue.
    """
    if not execute:
        db.init_db()
        now = time.time()
        key0 = ledger_key(artist, album)
        tier = identity.base_identity(_candidate_row(artist, album)).tier
        key, refusal = _gate(artist, album, key0, tier, now,
                             skip_in_library=skip_in_library, skip_cooldown=skip_cooldown,
                             max_pending=max_pending, db_path=db_path)
        # NB: a refusal Decision is falsy (__bool__ == admitted), so test None
        # explicitly — `refusal or ...` would silently swallow refusals.
        if refusal is not None:
            return refusal
        return Decision(False, WOULD_ADMIT, key, tier, 'gate clear (dry-run)')

    if post is None:
        raise ValueError('enqueue(execute=True) requires a post callable')

    d = claim(artist, album, source=source, skip_in_library=skip_in_library,
              skip_cooldown=skip_cooldown, username=username, remote_dir=remote_dir,
              file_count=file_count, max_pending=max_pending, db_path=db_path)
    if not d.admitted:
        return d

    try:
        ok = bool(post())
    except Exception as e:
        settle(d.rowid, False)            # transient POST failure: no cooldown, retry next run
        return Decision(False, POST_FAILED, d.identity_key, d.tier, f'post raised: {e}', d.rowid)
    if not ok:
        settle(d.rowid, False)
        return Decision(False, POST_FAILED, d.identity_key, d.tier, 'post returned False', d.rowid)
    return Decision(True, ADMITTED, d.identity_key, d.tier, 'enqueued', d.rowid)


# ─────────────────────────────────────────────────────────────────────────────
# poll() — advance live rows toward terminal state from slskd's transfer list
# ─────────────────────────────────────────────────────────────────────────────
def _classify_row(row, transfers: list, now: float):
    """Return (new_state, note) or (None, '') if the row should stay live."""
    states = _dir_file_states(transfers, row['username'], row['remote_dir'])
    if states:
        if any(_is_live_state(s) for s in states):
            if row['state'] == 'queued' and any(_is_active(s) for s in states):
                return 'downloading', 'in progress'
            return None, ''
        if all(_is_success(s) for s in states):
            return 'completed', f'{len(states)} file(s) succeeded'
        return 'failed', 'slskd reported errored/cancelled/partial'
    # Absent from slskd's list.
    age_h = (now - row['queued_at']) / 3600.0
    if age_h >= cfg.LEDGER_STALE_EXPIRE_H:
        return 'expired', f'absent from slskd > {cfg.LEDGER_STALE_EXPIRE_H}h'
    return None, ''


def poll(execute: bool = True) -> list:
    """Reconcile live ledger rows against slskd. Returns a list of
    (rowid, identity_key, old_state, new_state) transitions."""
    db.init_db()
    transfers = _transfers()
    now = time.time()
    changes = []
    for row in db.ledger_live_rows():
        new, note = _classify_row(row, transfers, now)
        if new and new != row['state']:
            if execute:
                db.ledger_set_state(row['id'], new, note)
            changes.append((row['id'], row['identity_key'], row['state'], new))
    return changes


# ─────────────────────────────────────────────────────────────────────────────
# CLI — operator visibility + manual ops (does not enqueue downloads)
# ─────────────────────────────────────────────────────────────────────────────
def _print_status():
    db.init_db()
    counts = db.ledger_counts()
    print('slskd ledger state counts:')
    if not counts:
        print('  (empty)')
    for st in ('queued', 'downloading', 'completed', 'failed', 'cancelled', 'expired'):
        if st in counts:
            print(f'  {st:<12} {counts[st]}')
    for st, n in counts.items():
        if st not in ('queued', 'downloading', 'completed', 'failed', 'cancelled', 'expired'):
            print(f'  {st:<12} {n}')
    live = db.ledger_live_rows()
    if live:
        print(f'\nin flight ({len(live)}):')
        for r in live:
            age = (time.time() - r['queued_at']) / 3600.0
            print(f"  [{r['state']:<11}] {r['source']:<10} {r['artist']} — {r['album']}  ({age:.1f}h)")
    recent = [r for r in db.ledger_recent(10)]
    if recent:
        print('\nrecent (newest first):')
        for r in recent:
            print(f"  #{r['id']} {r['state']:<11} {r['source']:<10} {r['artist']} — {r['album']}  {r['note']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description='slskd in-flight ledger / admission gate')
    ap.add_argument('--status', action='store_true', help='show ledger state (default)')
    ap.add_argument('--poll', action='store_true', help='advance live rows from slskd transfers')
    ap.add_argument('--dry-run', action='store_true', help='with --poll: report transitions, do not write')
    ap.add_argument('--gc', type=int, metavar='DAYS', nargs='?', const=30,
                    help='prune terminal rows older than DAYS (default 30)')
    ap.add_argument('--check', nargs=2, metavar=('ARTIST', 'ALBUM'),
                    help='dry-run the admission gate for one album (no enqueue)')
    args = ap.parse_args(argv)

    if args.check:
        d = enqueue(args.check[0], args.check[1], source='manual', execute=False)
        verdict = 'WOULD ADMIT' if d.state == WOULD_ADMIT else f'REFUSE ({d.state})'
        print(f'{verdict}\n  key:    {d.identity_key} (tier {d.tier})\n  reason: {d.reason}')
        return 0

    if args.gc is not None:
        n = db.ledger_prune(args.gc)
        print(f'pruned {n} terminal row(s) older than {args.gc}d')
        return 0

    if args.poll:
        changes = poll(execute=not args.dry_run)
        tag = '(dry-run) ' if args.dry_run else ''
        print(f'{tag}{len(changes)} transition(s):')
        for rowid, key, old, new in changes:
            print(f'  #{rowid} {old} -> {new}  {key}')
        return 0

    _print_status()
    return 0


if __name__ == '__main__':
    sys.exit(main())
