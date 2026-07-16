#!/usr/bin/env python3
"""reconcile_sync.py — post-cleanup DB↔disk consolidator.

Run this AFTER a manual Plex cleanup session (you browsed/listened/deleted
unwanted or duplicate albums in the Plex UI). Plex-UI deletes remove the FILES
but never touch the beets library.db, so the DB accumulates stale rows —
phantom albums that pollute the identity oracle. This makes the DB match the
new on-disk shape, in one gated pass:

  1. GUARDS (abort-if-unsafe). The dangerous confusion is a stale bind / USB
     hiccup (see the pipeline's history) where EVERY file transiently looks
     "missing" — pruning then would nuke the whole library. So we (a) require
     the library root and the durable reconcile dir to be on the SAME live
     device, both stat-able, and (b) refuse to prune if the missing fraction
     exceeds a ceiling (default 30%) unless --force-mass-prune. On --execute we
     snapshot library.db first (bin/pipeline-db-backup).
  2. PRUNE. beets rows whose files are gone (whole albums + partial tracks).
     Row-only removals — the files are already gone; nothing on disk is touched.
  3. DEDUP DETECT. Re-index identity over the pruned library; any final_key
     with >1 surviving album is a duplicate family. We emit a dup-plan and run
     it through reconcile's EXISTING verified path (resolve_dup_plan): only
     provably-safe drops (drop-trackset ⊆ keep, files DISJOINT on disk, same
     identity family, rowid-guarded) are eligible; everything else PARKs for
     your eyes. Dedup EXECUTION is opt-in (--dedup) and row-only.

DRY-RUN by default; --execute writes. Participates in reconcile's writer_lock
(the single-writer invariant) and journals every mutation.

NB — Plex's *Merge* is metadata-only: it changes nothing on disk, so this
script cannot and does not reflect Plex merges. Dedupe by DELETING the
redundant folder in Plex, not by Plex-merging.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import config as cfg          # noqa: E402
from pipeline import identity               # noqa: E402
from pipeline import reconcile as R         # noqa: E402  (helpers reused, not reimplemented)

BEETS_DB = R.BEETS_DB
LIBRARY_ROOT = R.LIBRARY_ROOT
RECON_DURABLE = R.RECON_DURABLE
PLAN_ROOT = R.PLAN_ROOT
DB_BACKUP_BIN = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / 'bin' / 'pipeline-db-backup'

MASS_MISSING_FRAC = float(os.environ.get('SYNC_MASS_MISSING_FRAC', '0.30'))


def over_mass_ceiling(n_gone: int, total: int) -> bool:
    """True when the fully-missing fraction exceeds the safety ceiling — the
    stale-mount / accidental-catastrophe signature that must block auto-prune."""
    return bool(total) and (n_gone / total) > MASS_MISSING_FRAC


# ─── Guards ──────────────────────────────────────────────────────────────────
def assert_mount_healthy() -> None:
    """Refuse to run against a stale/wrong mount — the failure mode where every
    file falsely reads missing. Cheap, before any file walk."""
    try:
        lib_dev = os.stat(LIBRARY_ROOT).st_dev
    except OSError as e:
        raise SystemExit(f"ABORT: cannot stat library root {LIBRARY_ROOT} ({e}) — "
                         f"stale/unmounted? refusing to prune.")
    try:
        dur_dev = os.stat(RECON_DURABLE).st_dev
    except OSError:
        # durable dir absent is fine on a first run; fall back to the library dev
        dur_dev = lib_dev
    if lib_dev != dur_dev:
        raise SystemExit(f"ABORT: library dev {lib_dev} != reconcile-durable dev {dur_dev} — "
                         f"wrong or half-mounted filesystem; refusing to prune.")


def backup_db() -> None:
    """Consistent library.db snapshot before any write. Hard-abort on failure —
    the whole point is that a bad sync is reversible."""
    if not DB_BACKUP_BIN.exists():
        raise SystemExit(f"ABORT: backup helper missing at {DB_BACKUP_BIN}; refusing to write.")
    env = dict(os.environ, BEETS_DB=BEETS_DB)
    proc = subprocess.run([str(DB_BACKUP_BIN)], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"ABORT: db backup failed (rc={proc.returncode}):\n{proc.stderr}")
    print(f"[backup] {proc.stdout.strip()}")


# ─── Detect: what disk says is gone ──────────────────────────────────────────
def _abs(raw: bytes) -> bytes:
    """Resolve a beets item path to absolute. beets stores paths RELATIVE to its
    `directory` and the pipeline runs with NO chdir, so a bare os.path.exists on
    the stored bytes always fails — every file would read 'missing'. Join
    relatives to LIBRARY_ROOT; pass absolutes (legacy rows) through."""
    root_b = os.fsencode(str(LIBRARY_ROOT))
    return raw if raw.startswith(b'/') else os.path.join(root_b, raw)


def probe_readable(albums, db_path: str, sample: int = 50) -> int:
    """How many of the first `sample` albums have a real, readable first file at
    the resolved path. Zero-while-albums-exist means the mount/paths are broken,
    not that the user culled — a state where NO amount of --force should prune."""
    seen = ok = 0
    for a in albums:
        items = R.read_items_raw(a.album_id, db_path)
        if not items:
            continue
        seen += 1
        if os.path.exists(_abs(items[0]['path'])):
            ok += 1
        if seen >= sample:
            break
    return ok


def _missing_items(album_id: int, db_path: str) -> tuple:
    """(total_items, [missing item rows]) using RAW path bytes (never the
    lossy-decoded identity paths — file ops must see true bytes)."""
    items = R.read_items_raw(album_id, db_path)
    missing = [it for it in items if not os.path.exists(_abs(it['path']))]
    return len(items), missing


def build_prune_plan(albums, db_path: str) -> list:
    """One entry per album that lost files: PRUNE_ALBUM (all gone) or
    PRUNE_ITEMS (some gone). Albums fully intact are omitted."""
    plan = []
    for a in albums:
        total, missing = _missing_items(a.album_id, db_path)
        if not missing:
            continue
        meta = R.album_meta(a.album_id, db_path) or {}
        label = f"{meta.get('albumartist','?')} — {meta.get('album','?')}"
        if total and len(missing) == total:
            plan.append({'route': 'PRUNE_ALBUM', 'album_id': a.album_id, 'label': label,
                         'n_missing': len(missing), 'n_total': total,
                         'remove_cmd': ['beet', 'remove', '-a', '-f', f'id:{a.album_id}']})
        else:
            plan.append({'route': 'PRUNE_ITEMS', 'album_id': a.album_id, 'label': label,
                         'n_missing': len(missing), 'n_total': total,
                         'item_ids': [it['item_id'] for it in missing],
                         'remove_cmds': [['beet', 'remove', '-f', f'id:{it["item_id"]}']
                                         for it in missing]})
    return plan


def build_dedup_plan(albums, pruned_album_ids: set, db_path: str) -> tuple:
    """Detect duplicate families among SURVIVING albums (final_key shared by >1),
    then hand a synthesized dup-plan to reconcile.resolve_dup_plan for the real,
    live-DB-verified DROP/PARK classification. Returns (classified_entries,
    raw_families). keep = the most-complete survivor (n_items desc, id asc);
    ambiguity is resolved conservatively by resolve_dup_plan, not here."""
    by_album = identity.build_index(albums)['by_album']
    n_items = {a.album_id: len(a.items) for a in albums}
    fam = defaultdict(list)
    for aid, key in by_album.items():
        if aid in pruned_album_ids or key is None:
            continue
        fam[key].append(aid)
    families = {k: v for k, v in fam.items() if len(v) > 1}
    if not families:
        return [], {}
    dup_plan = []
    for key, ids in families.items():
        ordered = sorted(ids, key=lambda i: (-n_items.get(i, 0), i))
        keep, drops = ordered[0], ordered[1:]
        dup_plan.append({'keep': {'album_id': keep},
                         'drop': [{'album_id': d} for d in drops]})
    # reuse the gate's verifier via a temp plan file (build_dup_plan reads a path)
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as tf:
        json.dump(dup_plan, tf)
        tmp = tf.name
    try:
        classified = R.build_dup_plan(tmp, db_path)
    finally:
        os.unlink(tmp)
    return classified, families


# ─── Report ──────────────────────────────────────────────────────────────────
def print_report(prune, dedup, total_albums, do_dedup):
    print(f"\n=== reconcile-sync PLAN (DRY-RUN — no writes) ===")
    pc = Counter(e['route'] for e in prune)
    print(f"  library albums:      {total_albums}")
    print(f"  PRUNE_ALBUM (gone):  {pc.get('PRUNE_ALBUM', 0)}")
    print(f"  PRUNE_ITEMS (partial):{pc.get('PRUNE_ITEMS', 0)}")
    if prune:
        print("-" * 100)
        for e in sorted(prune, key=lambda x: x['route']):
            print(f"  [{e['route']:12}] id={e['album_id']:<6} {e['n_missing']}/{e['n_total']} gone  {e['label'][:60]}")
    if do_dedup:
        dc = Counter(e['route'] for e in dedup)
        print("-" * 100)
        print(f"  DEDUP families → DUPPLAN_DROP={dc.get('DUPPLAN_DROP',0)} "
              f"PARK={dc.get('PARK',0)} DONE={dc.get('DUPPLAN_DONE',0)}")
        for e in dedup:
            print(f"  [{e['route']:12}] keep={e.get('keep_id')} drop={e.get('drop_id')}  {e.get('reason','')[:70]}")
    else:
        print(f"  (dedup detection skipped — pass --dedup to detect duplicate families)")


# ─── Execute ─────────────────────────────────────────────────────────────────
def _run(cmd) -> int:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300).returncode


def execute(prune, dedup, journal: R.Journal, do_dedup: bool) -> dict:
    stats = Counter()
    for e in prune:
        if e['route'] == 'PRUNE_ALBUM':
            journal.record('PENDING', 'PRUNE_ALBUM', album_id=e['album_id'], label=e['label'])
            rc = _run(e['remove_cmd'])
            journal.record('DONE', 'PRUNE_ALBUM', album_id=e['album_id'], rc=rc)
            stats['prune_album'] += 1
        else:
            journal.record('PENDING', 'PRUNE_ITEMS', album_id=e['album_id'],
                           item_ids=e['item_ids'], label=e['label'])
            for cmd in e['remove_cmds']:
                _run(cmd)
            journal.record('DONE', 'PRUNE_ITEMS', album_id=e['album_id'], n=len(e['item_ids']))
            stats['prune_items'] += 1
    if do_dedup:
        for e in dedup:
            if e['route'] == 'DUPPLAN_DROP' and e.get('remove_cmd'):
                journal.record('PENDING', 'DUPPLAN_DROP', drop_id=e.get('drop_id'), keep_id=e.get('keep_id'))
                rc = _run(e['remove_cmd'])
                journal.record('DONE', 'DUPPLAN_DROP', drop_id=e.get('drop_id'), rc=rc)
                stats['dup_drop'] += 1
            else:
                stats['dup_park' if e['route'] == 'PARK' else 'dup_skip'] += 1
    return dict(stats)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="reconcile-sync — make beets match disk after a Plex cleanup (DRY-RUN default)")
    ap.add_argument('--execute', action='store_true', help='WRITE (default is dry-run)')
    ap.add_argument('--dedup', action='store_true', help='also detect duplicate families (and, with --execute, run the verified safe drops)')
    ap.add_argument('--force-mass-prune', action='store_true', help='override the mass-missing safety ceiling')
    ap.add_argument('--db', default=BEETS_DB)
    ap.add_argument('--run-id', default='sync')
    args = ap.parse_args(argv)

    R.check_preconditions()
    assert_mount_healthy()

    albums = identity.load_albums(args.db)
    total = len(albums)
    if total == 0:
        raise SystemExit("ABORT: 0 albums loaded — empty/unreadable DB; refusing to prune.")

    if probe_readable(albums, args.db) == 0:
        raise SystemExit(
            "ABORT: none of the sampled library files are readable at their resolved paths — "
            "a stale mount or path-resolution problem, NOT a cull. Refusing to touch the DB "
            "(no --force override: this state means the disk view is wrong, not that you deleted).")

    prune = build_prune_plan(albums, args.db)
    gone_albums = {e['album_id'] for e in prune if e['route'] == 'PRUNE_ALBUM'}
    if over_mass_ceiling(len(gone_albums), total) and not args.force_mass_prune:
        frac = len(gone_albums) / total
        raise SystemExit(
            f"ABORT: {len(gone_albums)}/{total} albums ({frac:.0%}) look fully missing — "
            f"above the {MASS_MISSING_FRAC:.0%} ceiling. This is the stale-mount signature. "
            f"Verify the mount, or pass --force-mass-prune if this cull was really that large.")

    dedup, _families = ([], {})
    if args.dedup:
        dedup, _families = build_dedup_plan(albums, gone_albums, args.db)

    run_dir = PLAN_ROOT / args.run_id
    with R.writer_lock(execute=args.execute):
        print_report(prune, dedup, total, args.dedup)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / 'sync_plan.json').write_text(
            json.dumps({'total_albums': total, 'prune': prune, 'dedup': dedup},
                       indent=1, default=str), encoding='utf-8')
        if args.execute:
            backup_db()
            journal = R.Journal(run_dir / 'sync_journal.jsonl')
            journal.record('BEGIN', 'sync', total_albums=total, prune=len(prune), dedup=len(dedup))
            print("\n=== EXECUTE (writing to library.db) ===")
            stats = execute(prune, dedup, journal, args.dedup)
            journal.record('END', 'sync', **stats)
            print(f"  applied: {dict(stats)}")
            print(f"  journal: {run_dir / 'sync_journal.jsonl'}")

    print(f"\nplan written: {run_dir / 'sync_plan.json'}")
    if args.dedup and any(e['route'] == 'PARK' for e in dedup):
        return 4   # parked dup families need human review
    return 0


if __name__ == '__main__':
    sys.exit(main())
