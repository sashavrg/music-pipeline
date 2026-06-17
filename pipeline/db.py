#!/usr/bin/env python3
"""
pipeline_db.py — Shared SQLite state for the slskd/beets music pipeline.

All scripts import this module and call init_db() at startup.
State that used to live in separate JSON files now lives in a single WAL-mode
SQLite database at /var/lib/pipeline/pipeline.db.

Tables:
  held_folders      — promote-ready hold state (also read by fill-missing, healthcheck)
  fill_attempts     — fill-missing-tracks history
  quarantine_state  — quarantine-requeue cooldowns
  incomplete_state  — incomplete-watchdog per-folder tracking
  notify_queue      — telegram notification delivery queue (multi-writer, one consumer)
  wishlist          — user's album wishlist (telegram bot + wishlist-check)
"""

import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from . import config as cfg

DB_DIR  = cfg.PIPELINE_STATE_DIR
DB_PATH = cfg.PIPELINE_DB_PATH

SLSKD_API_KEY_PATH = cfg.SLSKD_API_KEY_PATH
_slskd_api_key_cache: str | None = None


def slskd_api_key() -> str:
    """
    Read the slskd API key once per process. Returns '' if the key file
    isn't present (slskd auth disabled — older deployments).
    """
    global _slskd_api_key_cache
    if _slskd_api_key_cache is None:
        try:
            _slskd_api_key_cache = SLSKD_API_KEY_PATH.read_text(encoding='utf-8').strip()
        except Exception:
            _slskd_api_key_cache = ''
    return _slskd_api_key_cache


def slskd_headers(extra: dict | None = None) -> dict:
    """Standard headers for slskd REST API requests, including auth."""
    h = {'Accept': 'application/json'}
    key = slskd_api_key()
    if key:
        h['X-API-Key'] = key
    if extra:
        h.update(extra)
    return h

# Legacy paths for one-time JSON migration
_LEGACY = {
    'hold':       Path('/var/lib/slskd-promote-ready/hold-state.json'),
    'fill':       Path('/var/lib/slskd-fill-missing/state.json'),
    'quarantine': Path('/var/lib/slskd-quarantine-requeue/state.json'),
    'incomplete': Path('/var/lib/slskd-incomplete-watchdog/state.json'),
    'notify':     Path('/var/lib/slskd-telegram-bot/notify-queue.jsonl'),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS held_folders (
    folder_name   TEXT PRIMARY KEY,
    first_seen    REAL NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    files_present INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fill_attempts (
    folder_name   TEXT PRIMARY KEY,
    last_queued   REAL NOT NULL DEFAULT 0,
    queued_tracks TEXT NOT NULL DEFAULT '[]',
    queued_files  INTEGER NOT NULL DEFAULT 0,
    source_user   TEXT NOT NULL DEFAULT '',
    source_fmt    TEXT NOT NULL DEFAULT '',
    source_score  INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_state (
    key           TEXT PRIMARY KEY,
    last_attempt  REAL NOT NULL DEFAULT 0,
    last_queued   REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incomplete_state (
    folder_name       TEXT PRIMARY KEY,
    first_seen        REAL NOT NULL,
    alerted           INTEGER NOT NULL DEFAULT 0,
    requeued          INTEGER NOT NULL DEFAULT 0,
    last_search_time  REAL NOT NULL DEFAULT 0,
    requeue_time      TEXT NOT NULL DEFAULT '',
    updated_at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notify_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event        TEXT NOT NULL,
    folder       TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL,
    delivered_at REAL
);
CREATE TABLE IF NOT EXISTS wishlist (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    artist       TEXT NOT NULL,
    album        TEXT NOT NULL,
    added_at     REAL NOT NULL,
    added_by     TEXT NOT NULL DEFAULT 'telegram',
    last_attempt REAL,
    last_queued  REAL,
    fulfilled_at REAL,
    note         TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'music'
);
CREATE TABLE IF NOT EXISTS schema_version (
    name        TEXT PRIMARY KEY,
    applied_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mb_album_cache (
    artist_key   TEXT NOT NULL,
    album_key    TEXT NOT NULL,
    track_count  INTEGER,
    queried_at   REAL NOT NULL,
    PRIMARY KEY (artist_key, album_key)
);
-- Canonical release info (artist-credit phrase, release title, MBID) for the
-- best-scoring MB release matching (artist, album). Used by the telegram bot's
-- broad-query retry: when the bot's user-typed `artist album` query returns
-- 0 results, retry with the MB-canonical artist credit + title. Cached
-- 30 days, including negative results (NULL canonical_artist).
CREATE TABLE IF NOT EXISTS mb_canonical_cache (
    artist_key       TEXT NOT NULL,
    album_key        TEXT NOT NULL,
    canonical_artist TEXT,
    canonical_title  TEXT,
    canonical_mbid   TEXT,
    canonical_score  INTEGER,
    queried_at       REAL NOT NULL,
    PRIMARY KEY (artist_key, album_key)
);
CREATE INDEX IF NOT EXISTS ix_notify_pending ON notify_queue (delivered_at) WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_wishlist_pending ON wishlist (fulfilled_at) WHERE fulfilled_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_notify_delivered ON notify_queue (delivered_at) WHERE delivered_at IS NOT NULL;
-- slskd in-flight ledger (rebuild phase 6) — the single identity-keyed table
-- every producer must pass through before POSTing a download to slskd. Replaces
-- the 5 per-stage cooldown tables that could not see each other's in-flight work
-- (structural fault line #3 of the 2026-06-13 rethink). Keyed by identity (the
-- same key reconcile/identity.py compute), NOT folder name, so the gate sees
-- across producers and matches the library oracle. slskdq.py is its only writer.
CREATE TABLE IF NOT EXISTS slskd_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key  TEXT    NOT NULL,
    artist        TEXT    NOT NULL DEFAULT '',
    album         TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT '',   -- producer: recover|incomplete|quarantine|fill|wishlist
    username      TEXT    NOT NULL DEFAULT '',    -- slskd peer (for poll mapping)
    remote_dir    TEXT    NOT NULL DEFAULT '',    -- slskd directory string (for poll mapping)
    file_count    INTEGER NOT NULL DEFAULT 0,
    state         TEXT    NOT NULL DEFAULT 'queued', -- queued|downloading|completed|failed|cancelled|expired
    attempt       INTEGER NOT NULL DEFAULT 1,
    queued_at     REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    terminal_at   REAL,                            -- set on reaching a terminal state (drives cooldown)
    note          TEXT    NOT NULL DEFAULT ''
);
-- THE core invariant: at most ONE live (queued/downloading) row per identity.
-- A partial-unique index makes the in-flight refusal race-proof at the schema
-- level — a second producer's INSERT fails with IntegrityError, not a TOCTOU win.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_live_identity
    ON slskd_ledger (identity_key) WHERE state IN ('queued','downloading');
CREATE INDEX IF NOT EXISTS ix_ledger_state ON slskd_ledger (state);
CREATE INDEX IF NOT EXISTS ix_ledger_identity ON slskd_ledger (identity_key);
"""

# Notification events that can be batched when many arrive together
_BATCH_EVENTS = frozenset({
    'quarantine_cleared',
    'quarantine_requeued',
    'quarantine_no_results',
    'incomplete_in_library_removed',
})
_BATCH_THRESHOLD = 3


@contextmanager
def _db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=5000')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema init + JSON migration ──────────────────────────────────────────────

def init_db():
    """Create tables (idempotent) and migrate from legacy JSON files once."""
    with _db() as con:
        con.executescript(_SCHEMA)
        _maybe_migrate(con)
        _maybe_add_columns(con)


def _maybe_add_columns(con):
    """Add columns introduced after the original schema. ALTER TABLE ADD COLUMN
    raises if the column already exists, so probe PRAGMA first."""
    cols = {r[1] for r in con.execute('PRAGMA table_info(wishlist)')}
    if 'kind' not in cols:
        con.execute("ALTER TABLE wishlist ADD COLUMN kind TEXT NOT NULL DEFAULT 'music'")


def _load_json_safe(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _maybe_migrate(con):
    """Import legacy JSON state files into SQLite. Runs only once per DB."""
    if con.execute(
        "SELECT 1 FROM schema_version WHERE name='legacy_json_import'"
    ).fetchone():
        return

    now = time.time()

    # held_folders
    for name, entry in _load_json_safe(_LEGACY['hold']).items():
        if isinstance(entry, (int, float)):
            entry = {'first_seen': float(entry)}
        con.execute(
            'INSERT OR IGNORE INTO held_folders VALUES (?,?,?,?,?)',
            (name, entry.get('first_seen', now), entry.get('reason', ''),
             entry.get('files_present', 0), now),
        )

    # fill_attempts
    for name, entry in _load_json_safe(_LEGACY['fill']).items():
        if isinstance(entry, dict):
            con.execute(
                'INSERT OR IGNORE INTO fill_attempts VALUES (?,?,?,?,?,?,?,?)',
                (name, entry.get('last_queued', 0),
                 json.dumps(entry.get('queued_tracks', [])),
                 entry.get('queued_files', 0),
                 entry.get('user', ''), entry.get('fmt', ''),
                 entry.get('score', 0), now),
            )

    # quarantine_state
    for key, entry in _load_json_safe(_LEGACY['quarantine']).items():
        if isinstance(entry, dict):
            con.execute(
                'INSERT OR IGNORE INTO quarantine_state VALUES (?,?,?,?)',
                (key, entry.get('last_attempt', 0), entry.get('last_queued', 0), now),
            )

    # incomplete_state
    for name, entry in _load_json_safe(_LEGACY['incomplete']).items():
        if isinstance(entry, dict):
            con.execute(
                'INSERT OR IGNORE INTO incomplete_state VALUES (?,?,?,?,?,?,?)',
                (name, entry.get('first_seen', now),
                 int(bool(entry.get('alerted', False))),
                 int(bool(entry.get('requeued', False))),
                 entry.get('last_search_time', 0),
                 entry.get('requeue_time', ''), now),
            )

    # notify_queue — import any pending JSONL items
    try:
        for line in _LEGACY['notify'].read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                payload = {k: v for k, v in e.items() if k not in ('event', 'folder', 'time')}
                con.execute(
                    'INSERT INTO notify_queue (event, folder, payload, created_at) VALUES (?,?,?,?)',
                    (e.get('event', ''), e.get('folder', ''), json.dumps(payload), now - 1),
                )
            except Exception:
                pass
    except Exception:
        pass

    con.execute(
        'INSERT OR REPLACE INTO schema_version (name, applied_at) VALUES (?, ?)',
        ('legacy_json_import', now),
    )


# ── held_folders ──────────────────────────────────────────────────────────────

def get_held_folders() -> dict:
    """Return {folder_name: {first_seen, reason, files_present}}."""
    with _db() as con:
        rows = con.execute(
            'SELECT folder_name, first_seen, reason, files_present FROM held_folders'
        ).fetchall()
    return {r['folder_name']: {
        'first_seen':    r['first_seen'],
        'reason':        r['reason'],
        'files_present': r['files_present'],
    } for r in rows}


def upsert_held_folder(folder_name: str, first_seen: float, reason: str, files_present: int):
    with _db() as con:
        con.execute(
            'INSERT INTO held_folders VALUES (?,?,?,?,?) '
            'ON CONFLICT(folder_name) DO UPDATE SET '
            'reason=excluded.reason, files_present=excluded.files_present, '
            'updated_at=excluded.updated_at',
            (folder_name, first_seen, reason, files_present, time.time()),
        )


def delete_held_folder(folder_name: str):
    with _db() as con:
        con.execute('DELETE FROM held_folders WHERE folder_name=?', (folder_name,))


def prune_held_folders(keep: set) -> set:
    """Delete entries whose folder_name is not in keep. Returns pruned names."""
    with _db() as con:
        existing = {r[0] for r in con.execute('SELECT folder_name FROM held_folders')}
        stale = existing - keep
        for name in stale:
            con.execute('DELETE FROM held_folders WHERE folder_name=?', (name,))
    return stale


# ── fill_attempts ─────────────────────────────────────────────────────────────

def get_fill_state() -> dict:
    """Return {folder_name: {last_queued, queued_tracks, queued_files, user, fmt, score}}."""
    with _db() as con:
        rows = con.execute('SELECT * FROM fill_attempts').fetchall()
    result = {}
    for r in rows:
        result[r['folder_name']] = {
            'last_queued':   r['last_queued'],
            'queued_tracks': json.loads(r['queued_tracks']),
            'queued_files':  r['queued_files'],
            'user':          r['source_user'],
            'fmt':           r['source_fmt'],
            'score':         r['source_score'],
        }
    return result


def upsert_fill_attempt(folder_name: str, last_queued: float,
                        queued_tracks: list, queued_files: int,
                        user: str, fmt: str, score: int):
    with _db() as con:
        con.execute(
            'INSERT INTO fill_attempts VALUES (?,?,?,?,?,?,?,?) '
            'ON CONFLICT(folder_name) DO UPDATE SET '
            'last_queued=excluded.last_queued, queued_tracks=excluded.queued_tracks, '
            'queued_files=excluded.queued_files, source_user=excluded.source_user, '
            'source_fmt=excluded.source_fmt, source_score=excluded.source_score, '
            'updated_at=excluded.updated_at',
            (folder_name, last_queued, json.dumps(queued_tracks),
             queued_files, user, fmt, score, time.time()),
        )


# ── quarantine_state ──────────────────────────────────────────────────────────

def get_quarantine_state() -> dict:
    with _db() as con:
        rows = con.execute(
            'SELECT key, last_attempt, last_queued FROM quarantine_state'
        ).fetchall()
    return {r['key']: {'last_attempt': r['last_attempt'],
                       'last_queued':  r['last_queued']} for r in rows}


def upsert_quarantine_state(key: str, last_attempt: float, last_queued: float = 0):
    with _db() as con:
        con.execute(
            'INSERT INTO quarantine_state VALUES (?,?,?,?) '
            'ON CONFLICT(key) DO UPDATE SET '
            'last_attempt=excluded.last_attempt, last_queued=excluded.last_queued, '
            'updated_at=excluded.updated_at',
            (key, last_attempt, last_queued, time.time()),
        )


def delete_quarantine_state(key: str):
    with _db() as con:
        con.execute('DELETE FROM quarantine_state WHERE key=?', (key,))


def prune_quarantine_state(keep: set) -> set:
    with _db() as con:
        existing = {r[0] for r in con.execute('SELECT key FROM quarantine_state')}
        stale = existing - keep
        for k in stale:
            con.execute('DELETE FROM quarantine_state WHERE key=?', (k,))
    return stale


# ── incomplete_state ──────────────────────────────────────────────────────────

def get_incomplete_state() -> dict:
    with _db() as con:
        rows = con.execute('SELECT * FROM incomplete_state').fetchall()
    return {r['folder_name']: {
        'first_seen':       r['first_seen'],
        'alerted':          bool(r['alerted']),
        'requeued':         bool(r['requeued']),
        'last_search_time': r['last_search_time'],
        'requeue_time':     r['requeue_time'],
    } for r in rows}


def upsert_incomplete_state(folder_name: str, first_seen: float,
                             alerted: bool = False, requeued: bool = False,
                             last_search_time: float = 0, requeue_time: str = ''):
    with _db() as con:
        con.execute(
            'INSERT INTO incomplete_state VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(folder_name) DO UPDATE SET '
            'first_seen=excluded.first_seen, alerted=excluded.alerted, '
            'requeued=excluded.requeued, last_search_time=excluded.last_search_time, '
            'requeue_time=excluded.requeue_time, updated_at=excluded.updated_at',
            (folder_name, first_seen, int(alerted), int(requeued),
             last_search_time, requeue_time, time.time()),
        )


def delete_incomplete_state(folder_name: str):
    with _db() as con:
        con.execute('DELETE FROM incomplete_state WHERE folder_name=?', (folder_name,))


def prune_incomplete_state(keep: set) -> set:
    with _db() as con:
        existing = {r[0] for r in con.execute('SELECT folder_name FROM incomplete_state')}
        stale = existing - keep
        for name in stale:
            con.execute('DELETE FROM incomplete_state WHERE folder_name=?', (name,))
    return stale


# ── notify_queue ──────────────────────────────────────────────────────────────

def push_notification(event: str, folder: str = '', **kwargs):
    """Append a notification to the queue (non-blocking, safe for concurrent callers)."""
    with _db() as con:
        con.execute(
            'INSERT INTO notify_queue (event, folder, payload, created_at) VALUES (?,?,?,?)',
            (event, folder, json.dumps(kwargs), time.time()),
        )


def claim_notifications() -> list[tuple[dict, list[int]]]:
    """
    Read pending notifications and return [(notif, [row_ids]), ...] with batching.
    Does NOT mark anything delivered — caller must call mark_delivered(ids) per
    notif AFTER successful Telegram send. This guarantees at-least-once delivery
    (rows stay pending if the bot crashes between claim and mark).

    Events in _BATCH_EVENTS are collapsed into a summary dict when >= _BATCH_THRESHOLD;
    the caller marks all underlying row ids together.
    """
    with _db() as con:
        rows = con.execute(
            'SELECT id, event, folder, payload FROM notify_queue '
            'WHERE delivered_at IS NULL ORDER BY created_at'
        ).fetchall()
    if not rows:
        return []

    events = []
    for r in rows:
        try:
            payload = json.loads(r['payload'])
        except Exception:
            payload = {}
        events.append((r['id'], {'event': r['event'], 'folder': r['folder'], **payload}))

    event_counts = Counter(e['event'] for _, e in events if e['event'] in _BATCH_EVENTS)
    batched: dict[str, list] = defaultdict(list)
    batched_ids: dict[str, list[int]] = defaultdict(list)
    result: list[tuple[dict, list[int]]] = []

    for rid, e in events:
        ev = e['event']
        if ev in _BATCH_EVENTS and event_counts[ev] >= _BATCH_THRESHOLD:
            batched[ev].append(e)
            batched_ids[ev].append(rid)
        else:
            result.append((e, [rid]))

    for ev, group in batched.items():
        result.append(
            ({'event': f'{ev}_batch', 'count': len(group), 'items': group},
             batched_ids[ev]),
        )

    return result


def mark_delivered(ids: list[int]):
    """Mark notify_queue rows delivered. Call only after successful send."""
    if not ids:
        return
    now = time.time()
    with _db() as con:
        con.execute(
            f'UPDATE notify_queue SET delivered_at=? WHERE id IN ({",".join("?"*len(ids))})',
            [now, *ids],
        )


def drain_notifications() -> list[dict]:
    """
    DEPRECATED: legacy at-most-once API. Marks rows delivered before send,
    so messages are lost on crash. Kept for backwards compat — prefer
    claim_notifications() + mark_delivered().
    """
    claimed = claim_notifications()
    all_ids: list[int] = []
    for _, ids in claimed:
        all_ids.extend(ids)
    mark_delivered(all_ids)
    return [notif for notif, _ in claimed]


def prune_notify_queue(retain_days: int = 30) -> int:
    """Delete delivered notify_queue rows older than retain_days. Returns row count."""
    cutoff = time.time() - retain_days * 86400
    with _db() as con:
        cur = con.execute(
            'DELETE FROM notify_queue WHERE delivered_at IS NOT NULL AND delivered_at < ?',
            (cutoff,),
        )
        return cur.rowcount


# ── wishlist ──────────────────────────────────────────────────────────────────

def add_wishlist(artist: str, album: str, added_by: str = 'telegram',
                  kind: str = 'music') -> tuple[int, bool]:
    """
    Add an item to the wishlist.
    Returns (id, is_new) — is_new=False means it was already there.
    `kind` selects the quality profile applied at search time
    (currently 'music' or 'audiobook').
    """
    with _db() as con:
        existing = con.execute(
            'SELECT id FROM wishlist WHERE LOWER(artist)=LOWER(?) AND LOWER(album)=LOWER(?) '
            'AND fulfilled_at IS NULL',
            (artist, album),
        ).fetchone()
        if existing:
            return existing['id'], False
        cur = con.execute(
            'INSERT INTO wishlist (artist, album, added_at, added_by, kind) VALUES (?,?,?,?,?)',
            (artist, album, time.time(), added_by, kind),
        )
        return cur.lastrowid, True


def get_wishlist_pending() -> list[dict]:
    with _db() as con:
        rows = con.execute(
            'SELECT id, artist, album, added_at, last_attempt, last_queued, kind '
            'FROM wishlist WHERE fulfilled_at IS NULL ORDER BY added_at'
        ).fetchall()
    return [dict(r) for r in rows]


def update_wishlist_attempt(wid: int, last_attempt: float, last_queued: float | None = None):
    with _db() as con:
        if last_queued is not None:
            con.execute(
                'UPDATE wishlist SET last_attempt=?, last_queued=? WHERE id=?',
                (last_attempt, last_queued, wid),
            )
        else:
            con.execute('UPDATE wishlist SET last_attempt=? WHERE id=?', (last_attempt, wid))


def fulfill_wishlist(wid: int):
    with _db() as con:
        con.execute('UPDATE wishlist SET fulfilled_at=? WHERE id=?', (time.time(), wid))


def remove_wishlist(wid: int) -> bool:
    with _db() as con:
        cur = con.execute('DELETE FROM wishlist WHERE id=?', (wid,))
        return cur.rowcount > 0


# ── slskd in-flight ledger (phase 6) ──────────────────────────────────────────
# Storage only; the admission gate / state machine live in slskdq.py. Live states
# (a download still in flight) are queued + downloading; everything else is
# terminal. ledger_insert is the atomic in-flight claim: a duplicate live row for
# the same identity is rejected by ux_ledger_live_identity (returns None).

LEDGER_LIVE_STATES = ('queued', 'downloading')


def ledger_live(identity_key: str):
    """Return the live (queued/downloading) ledger row for an identity, or None."""
    with _db() as con:
        return con.execute(
            "SELECT * FROM slskd_ledger WHERE identity_key=? "
            "AND state IN ('queued','downloading') ORDER BY id DESC LIMIT 1",
            (identity_key,),
        ).fetchone()


def ledger_insert(identity_key: str, artist: str, album: str, source: str,
                  username: str = '', remote_dir: str = '',
                  file_count: int = 0) -> int | None:
    """Atomically claim the in-flight slot for an identity. Returns the new rowid,
    or None if a live row already exists (the partial-unique index rejects it).
    This is the race-proof in-flight gate — callers MUST treat None as 'refused,
    already in flight' and NOT POST to slskd."""
    now = time.time()
    try:
        with _db() as con:
            cur = con.execute(
                "INSERT INTO slskd_ledger "
                "(identity_key, artist, album, source, username, remote_dir, "
                " file_count, state, attempt, queued_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'queued', 1, ?, ?)",
                (identity_key, artist, album, source, username, remote_dir,
                 int(file_count), now, now),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def ledger_set_state(rowid: int, state: str, note: str | None = None):
    """Transition a ledger row. terminal_at is stamped iff the new state is
    terminal (anything other than queued/downloading), which arms cooldown."""
    now = time.time()
    terminal = 1 if state not in LEDGER_LIVE_STATES else 0
    with _db() as con:
        if note is None:
            con.execute(
                "UPDATE slskd_ledger SET state=?, updated_at=?, "
                "terminal_at=CASE WHEN ?=1 THEN ? ELSE terminal_at END "
                "WHERE id=?",
                (state, now, terminal, now, rowid),
            )
        else:
            con.execute(
                "UPDATE slskd_ledger SET state=?, note=?, updated_at=?, "
                "terminal_at=CASE WHEN ?=1 THEN ? ELSE terminal_at END "
                "WHERE id=?",
                (state, note, now, terminal, now, rowid),
            )


def ledger_live_rows() -> list:
    """All currently-live rows, oldest first (for the poll state machine)."""
    with _db() as con:
        return con.execute(
            "SELECT * FROM slskd_ledger WHERE state IN ('queued','downloading') "
            "ORDER BY id"
        ).fetchall()


def ledger_last_terminal(identity_key: str):
    """(state, terminal_at) of the most recent TERMINAL row for an identity, or
    None. slskdq turns this into a cooldown decision (a just-completed download
    awaiting import, or a recent failure to back off from)."""
    with _db() as con:
        r = con.execute(
            "SELECT state, terminal_at FROM slskd_ledger WHERE identity_key=? "
            "AND state NOT IN ('queued','downloading') AND terminal_at IS NOT NULL "
            "ORDER BY terminal_at DESC LIMIT 1",
            (identity_key,),
        ).fetchone()
    return (r['state'], r['terminal_at']) if r else None


def ledger_delete(rowid: int) -> bool:
    """Remove a ledger row outright. Used to roll back the in-flight claim when
    the slskd POST itself fails (a transient API hiccup, not a download failure) —
    no cooldown should be imposed, the next run retries immediately."""
    with _db() as con:
        cur = con.execute('DELETE FROM slskd_ledger WHERE id=?', (rowid,))
        return cur.rowcount > 0


def ledger_counts() -> dict:
    """{state: count} histogram across the whole ledger (for --status)."""
    with _db() as con:
        rows = con.execute(
            "SELECT state, COUNT(*) AS c FROM slskd_ledger GROUP BY state"
        ).fetchall()
    return {r['state']: r['c'] for r in rows}


def ledger_recent(limit: int = 50) -> list:
    with _db() as con:
        return con.execute(
            "SELECT * FROM slskd_ledger ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()


def ledger_prune(retain_days: int = 30) -> int:
    """Drop terminal rows older than retain_days. Live rows are never pruned."""
    cutoff = time.time() - retain_days * 86400
    with _db() as con:
        cur = con.execute(
            "DELETE FROM slskd_ledger WHERE state NOT IN ('queued','downloading') "
            "AND updated_at < ?",
            (cutoff,),
        )
        return cur.rowcount


# ── MusicBrainz album-size cache ──────────────────────────────────────────────

def _mb_key(s: str) -> str:
    return ' '.join((s or '').lower().split())


def get_mb_cached_track_count(artist: str, album: str, ttl_seconds: float):
    """
    Return (track_count, is_fresh) or None if no usable cache entry.
    track_count may be None (cached negative result — MB had nothing).
    is_fresh=False means the row exists but is past TTL.
    """
    with _db() as con:
        row = con.execute(
            'SELECT track_count, queried_at FROM mb_album_cache '
            'WHERE artist_key=? AND album_key=?',
            (_mb_key(artist), _mb_key(album)),
        ).fetchone()
    if not row:
        return None
    fresh = (time.time() - row['queried_at']) < ttl_seconds
    return (row['track_count'], fresh)


def upsert_mb_cache(artist: str, album: str, track_count):
    with _db() as con:
        con.execute(
            'INSERT OR REPLACE INTO mb_album_cache '
            '(artist_key, album_key, track_count, queried_at) VALUES (?,?,?,?)',
            (_mb_key(artist), _mb_key(album), track_count, time.time()),
        )


def get_canonical_cached(artist: str, album: str, ttl_seconds: float):
    """
    Return (canonical_dict_or_None, is_fresh) or None if no row exists.
    canonical_dict has keys: artist_credit, title, mbid, score.
    A row with NULL canonical_artist represents a cached negative — MB had no
    confident match for this (artist, album). is_fresh=False means past TTL.
    """
    with _db() as con:
        row = con.execute(
            'SELECT canonical_artist, canonical_title, canonical_mbid, '
            '       canonical_score, queried_at FROM mb_canonical_cache '
            'WHERE artist_key=? AND album_key=?',
            (_mb_key(artist), _mb_key(album)),
        ).fetchone()
    if not row:
        return None
    fresh = (time.time() - row['queried_at']) < ttl_seconds
    if row['canonical_artist'] is None:
        return (None, fresh)
    canon = {
        'artist_credit': row['canonical_artist'],
        'title':         row['canonical_title'],
        'mbid':          row['canonical_mbid'],
        'score':         row['canonical_score'],
    }
    return (canon, fresh)


def upsert_canonical_cache(artist: str, album: str, canon):
    """canon is a dict {artist_credit,title,mbid,score} or None (negative)."""
    if canon is None:
        ca = ct = cm = cs = None
    else:
        ca, ct = canon.get('artist_credit'), canon.get('title')
        cm, cs = canon.get('mbid'), canon.get('score')
    with _db() as con:
        con.execute(
            'INSERT OR REPLACE INTO mb_canonical_cache '
            '(artist_key, album_key, canonical_artist, canonical_title, '
            ' canonical_mbid, canonical_score, queried_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (_mb_key(artist), _mb_key(album), ca, ct, cm, cs, time.time()),
        )


# ── weekly digest helpers ─────────────────────────────────────────────────────

def get_weekly_stats(beets_db_path: str | None = None) -> dict:
    if beets_db_path is None:
        beets_db_path = cfg.BEETS_DB
    """
    Pull stats for the past 7 days to build a weekly digest.
    Returns dict with: new_albums, new_tracks, events_by_type, held, quarantine_count.
    """
    import sqlite3 as _sq
    week_ago = time.time() - 7 * 86400
    stats: dict = {
        'new_albums':     [],
        'new_tracks':     0,
        'events_by_type': {},
        'held':           [],
        'quarantine_count': 0,
    }

    # New albums from beets
    try:
        con = _sq.connect(beets_db_path, timeout=10)
        try:
            con.row_factory = _sq.Row
            rows = con.execute(
                'SELECT albumartist, album, COUNT(*) AS cnt FROM items '
                'WHERE added > ? GROUP BY albumartist, album ORDER BY MAX(added) DESC LIMIT 30',
                (week_ago,),
            ).fetchall()
            stats['new_tracks'] = sum(r['cnt'] for r in rows)
            stats['new_albums'] = [
                {'artist': r['albumartist'] or '', 'album': r['album'] or '', 'tracks': r['cnt']}
                for r in rows
            ]
        finally:
            con.close()
    except Exception:
        pass

    # Events by type from notify_queue
    with _db() as con:
        rows = con.execute(
            'SELECT event, COUNT(*) AS cnt FROM notify_queue '
            'WHERE created_at > ? GROUP BY event',
            (week_ago,),
        ).fetchall()
        stats['events_by_type'] = {r['event']: r['cnt'] for r in rows}

        # Current hold state
        held = con.execute(
            'SELECT folder_name, first_seen FROM held_folders ORDER BY first_seen'
        ).fetchall()
        stats['held'] = [{'name': r['folder_name'],
                          'age_h': round((time.time() - r['first_seen']) / 3600, 1)}
                         for r in held]

    return stats
